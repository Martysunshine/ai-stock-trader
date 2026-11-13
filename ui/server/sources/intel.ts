/**
 * The intel feed (ADR-0047), read-only: data/intel/index.json, one `latest` file per instrument,
 * and the append-only snapshot and run logs, written by `python -m trader.intel watch`
 * (docs/observability.md section 7).
 *
 * - The console never fetches news, filings or prices itself; it only reads these files.
 * - Only instruments listed in the index are served. A request names an id (`stocks:NVDA`); every
 *   path is built from the index entry's own group and symbol, each checked against a strict
 *   pattern, and must stay inside the intel folder. Nothing from the request is used as a path.
 * - "What was known at a moment" is a lookup, not a calculation: the newest stored snapshot taken
 *   at or before that moment. The windows and sums live in one place, trader.intel.snapshot.
 * - Files are cached by modification time and size, so a refresh by the feed is picked up on the
 *   next request. An unfinished last line of a log (a write in progress) is skipped.
 */
import { readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  asOf,
  type IntelIndex,
  type IntelIndexReply,
  type IntelKnownReply,
  type IntelLatest,
  type IntelSnapshot,
  type IntelTarget,
} from '../../shared/intel';
import { repoPath } from '../config';

const ID_RE = /^[a-z][a-z0-9]{0,15}:[A-Z0-9_]{1,16}$/;
const GROUP_RE = /^[a-z][a-z0-9]{0,15}$/;
const SYMBOL_RE = /^[A-Z0-9_]{1,16}$/;
const LRU_SIZE = 16;
/** How many months back a snapshot or run is looked for (a quiet instrument changes rarely). */
const LOOKBACK_MONTHS = 3;
const MAX_ITEMS = 50;

/** The folder the feed writes (the console only reads it). */
export const intelRoot = () => repoPath('data', 'intel');

// ------------------------------------------------------------------------------------- files
const cache = new Map<string, { stamp: string; value: unknown }>();

/** `parse(text)` of a file, cached until the file changes; null when it cannot be read. */
function cached<T>(file: string, parse: (text: string) => T | null): T | null {
  let stamp: string;
  try {
    const st = statSync(file);
    stamp = `${st.mtimeMs}:${st.size}`;
  } catch {
    return null;
  }
  const hit = cache.get(file);
  if (hit && hit.stamp === stamp) {
    cache.delete(file); // most recently used goes last
    cache.set(file, hit);
    return hit.value as T | null;
  }
  let value: T | null;
  try {
    value = parse(readFileSync(file, 'utf8'));
  } catch {
    value = null;
  }
  cache.delete(file);
  cache.set(file, { stamp, value });
  while (cache.size > LRU_SIZE) cache.delete(cache.keys().next().value!);
  return value;
}

/** Files currently held in memory (for tests). */
export const intelCacheSize = () => cache.size;

/** Every complete JSON object line; a torn or foreign line is skipped. */
export function parseJsonLines(text: string): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    try {
      const row: unknown = JSON.parse(line);
      if (row && typeof row === 'object' && !Array.isArray(row)) rows.push(row as Record<string, unknown>);
    } catch {
      // an unfinished last line while the feed is writing
    }
  }
  return rows;
}

/** A path below `root` built from fixed folder names, or null if it would leave the folder. */
function inside(root: string, ...parts: string[]): string | null {
  const resolved = path.resolve(root, ...parts);
  return resolved.startsWith(path.resolve(root) + path.sep) ? resolved : null;
}

/** `YYYY-MM` of `ms` and the months before it, newest first. */
function monthsBack(ms: number, count: number): string[] {
  const d = new Date(ms);
  const out: string[] = [];
  let year = d.getUTCFullYear();
  let month = d.getUTCMonth(); // 0-based
  for (let i = 0; i < count; i++) {
    out.push(`${year}-${String(month + 1).padStart(2, '0')}`);
    month -= 1;
    if (month < 0) {
      month = 11;
      year -= 1;
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------- index
function isIndex(x: unknown): x is IntelIndex {
  const i = x as IntelIndex;
  return !!i && typeof i === 'object' && Array.isArray(i.targets) && Array.isArray(i.sources) && typeof i.every_minutes === 'number';
}

/** The parsed index, or null when the feed has not written one (or it is unreadable). */
export function readIntelIndex(root = intelRoot()): IntelIndex | null {
  return cached(path.join(root, 'index.json'), (text) => {
    const parsed: unknown = JSON.parse(text);
    return isIndex(parsed) ? parsed : null;
  });
}

export function intelIndexReply(root = intelRoot()): IntelIndexReply {
  const index = readIntelIndex(root);
  if (!index) {
    return {
      available: false,
      reason: 'No data/intel/index.json yet. `npm run dev` starts the intel feed; or run `python -m uv run python -m trader.intel refresh`.',
      sources: [],
      targets: [],
    };
  }
  // The read positions (cursors) are the feed's own business; the page does not need them.
  const sources = index.sources.map(({ id, kind, label, every_minutes, status, note, error, updated_at, checked_at, retry_after, ...rest }) => ({
    id,
    kind,
    label,
    every_minutes,
    status,
    note: note ?? null,
    error: error ?? null,
    updated_at: updated_at ?? null,
    checked_at: checked_at ?? null,
    retry_after: retry_after ?? null,
    new: Number(rest.new ?? 0),
  }));
  return { ...index, sources, available: true };
}

/** The index entry of `id`, only if the index lists it and its group and symbol are plain. */
export function findTarget(id: string, root = intelRoot()): IntelTarget | null {
  if (!ID_RE.test(id)) return null;
  const target = readIntelIndex(root)?.targets.find((t) => t.id === id);
  if (!target || !GROUP_RE.test(String(target.group)) || !SYMBOL_RE.test(String(target.symbol))) return null;
  return `${target.group}:${target.symbol}` === id ? target : null;
}

// ------------------------------------------------------------------------------------- latest
/** The newest items of one instrument, or null for an id the index does not list. */
export function intelFor(id: string, root = intelRoot()): IntelLatest | null {
  const target = findTarget(id, root);
  if (!target) return null;
  const file = inside(root, 'latest', target.group, `${target.symbol}.json`);
  if (!file) return null;
  const latest = cached(file, (text) => {
    const data = JSON.parse(text) as IntelLatest;
    if (!data || data.id !== id || !data.snapshot) return null;
    return {
      ...data,
      news: Array.isArray(data.news) ? data.news.slice(0, MAX_ITEMS) : [],
      insiders: Array.isArray(data.insiders) ? data.insiders.slice(0, MAX_ITEMS) : [],
    };
  });
  return latest;
}

// ------------------------------------------------------------------------------------- known
const isSnapshot = (row: Record<string, unknown>, id: string): boolean =>
  row.id === id && typeof row.as_of === 'string' && typeof row.hash === 'string';

/**
 * What was known about `id` at `atIso`: the newest stored snapshot at or before that moment, and
 * the feed's last run at or before it. Returns null for an unknown id or an unreadable time.
 */
export function intelKnown(id: string, atIso: string, root = intelRoot()): IntelKnownReply | null {
  const target = findTarget(id, root);
  const atMs = Date.parse(atIso);
  if (!target || !Number.isFinite(atMs) || !/^\d{4}-\d{2}-\d{2}T/.test(atIso)) return null;
  const months = monthsBack(atMs, LOOKBACK_MONTHS);

  let snapshot: IntelSnapshot | null = null;
  for (const month of months) {
    const file = inside(root, 'snapshots', target.group, target.symbol, `${month}.jsonl`);
    const rows = file ? cached(file, parseJsonLines) : null;
    const found = asOf((rows ?? []).filter((r) => isSnapshot(r, id)) as unknown as IntelSnapshot[], atMs);
    if (found) {
      snapshot = found;
      break; // months are newest first: an older month cannot hold a newer snapshot
    }
  }

  let lastRunAt: string | null = null;
  for (const month of months) {
    const file = inside(root, 'runs', `${month}.jsonl`);
    const rows = file ? cached(file, parseJsonLines) : null;
    const runs = (rows ?? []).filter((r) => typeof r.at === 'string').map((r) => ({ as_of: r.at as string }));
    const found = asOf(runs, atMs);
    if (found) {
      lastRunAt = found.as_of;
      break;
    }
  }

  const age = (iso: string | null) => (iso === null ? null : Math.max(0, Math.round((atMs - Date.parse(iso)) / 1000)));
  return {
    id,
    at: new Date(atMs).toISOString(),
    snapshot,
    snapshotAgeSeconds: age(snapshot?.as_of ?? null),
    lastRunAt,
    lastRunAgeSeconds: age(lastRunAt),
    everyMinutes: readIntelIndex(root)?.every_minutes ?? 5,
  };
}
