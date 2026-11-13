/**
 * The dashboard's market feed (ADR-0045), read-only: data/market/index.json and one JSON file per
 * instrument, written by `python -m trader.market watch` (docs/observability.md section 6).
 *
 * - The console never fetches prices itself; it only reads these files.
 * - Only instruments listed in the index are served. A request names an id (`cfd:GOLD`); the file
 *   path comes from the index entry, is checked against a strict pattern and must stay inside the
 *   market folder. Nothing from the request is ever used as a path.
 * - The index is cached by modification time; parsed instrument files sit in a small LRU cache,
 *   also keyed by modification time, so a refresh by the feed is picked up on the next request.
 */
import { readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { aggregate, TF_SECONDS, type DayRule, type Ohlcv, type Timeframe } from '../../shared/aggregate';
import { repoPath } from '../config';

export type SeriesName = '5m' | '1h' | '1d';
const SERIES: SeriesName[] = ['5m', '1h', '1d'];
const SERIES_SECONDS: Record<SeriesName, number> = { '5m': 300, '1h': 3600, '1d': 86400 };
export const MARKET_TIMEFRAMES: Timeframe[] = ['5m', '15m', '1h', '1d'];
export const MAX_MARKET_BARS = 20000;
const ID_RE = /^[a-z][a-z0-9]{0,15}:[A-Z0-9_]{1,16}$/;
const FILE_RE = /^[a-z][a-z0-9]{0,15}\/[A-Z0-9_]{1,16}\.json$/;
const LRU_SIZE = 8;

export interface MarketInstrument {
  id: string;
  symbol: string;
  name: string;
  source_symbol?: string;
  file: string;
  precision: number;
  has_volume: boolean;
  basis: 'trade' | 'mid';
  min_deal?: string;
  last_price?: string | null;
  last_at?: number | null;
  series: Record<SeriesName, { first: number | null; last: number | null; count: number }>;
}

export interface MarketGroup {
  id: string;
  title: string;
  source: string;
  source_kind?: string;
  day_rule: DayRule;
  basis?: 'trade' | 'mid';
  status: 'ok' | 'partial' | 'error';
  error: string | null;
  updated_at: string | null;
  checked_at?: string | null;
  retry_after?: string | null;
  instruments: MarketInstrument[];
}

export interface MarketIndex {
  version: number;
  generated_at: string;
  every_minutes: number;
  groups: MarketGroup[];
}

export type MarketIndexReply = (MarketIndex & { available: true }) | { available: false; reason: string; groups: [] };

export interface MarketBarsReply {
  id: string;
  /** The timeframe asked for. */
  tf: Timeframe;
  /** The timeframe of the bars returned: coarser than `tf` when no finer series covers the range. */
  barTf: Timeframe;
  /** The stored series the bars were built from. */
  series: SeriesName;
  dayRule: DayRule;
  bars: Ohlcv[];
}

/** The folder the feed writes (the console only reads it). */
export const marketRoot = () => repoPath('data', 'market');

// ------------------------------------------------------------------------------------- index
let indexCache: { root: string; mtimeMs: number; index: MarketIndex } | null = null;

function isIndex(x: unknown): x is MarketIndex {
  const i = x as MarketIndex;
  return !!i && typeof i === 'object' && Array.isArray(i.groups) && typeof i.every_minutes === 'number';
}

/** The parsed index, or null when the feed has not written one (or it is unreadable). */
export function readMarketIndex(root = marketRoot()): MarketIndex | null {
  const file = path.join(root, 'index.json');
  let mtimeMs: number;
  try {
    mtimeMs = statSync(file).mtimeMs;
  } catch {
    return null;
  }
  if (indexCache && indexCache.root === root && indexCache.mtimeMs === mtimeMs) return indexCache.index;
  try {
    const parsed: unknown = JSON.parse(readFileSync(file, 'utf8'));
    if (!isIndex(parsed)) return null;
    indexCache = { root, mtimeMs, index: parsed };
    return parsed;
  } catch {
    return null; // a half-written file cannot happen (the feed renames), but stay calm anyway
  }
}

export function marketIndexReply(root = marketRoot()): MarketIndexReply {
  const index = readMarketIndex(root);
  if (!index) {
    return {
      available: false,
      reason: 'No data/market/index.json yet. `npm run dev` starts the market feed; or run `python -m uv run python -m trader.market refresh`.',
      groups: [],
    };
  }
  return { ...index, available: true };
}

/** The index entry and group of `id`, only if the index lists it. */
export function findInstrument(id: string, root = marketRoot()): { group: MarketGroup; instrument: MarketInstrument } | null {
  if (!ID_RE.test(id)) return null;
  const index = readMarketIndex(root);
  for (const group of index?.groups ?? []) {
    const instrument = group.instruments?.find((i) => i.id === id);
    if (instrument) return { group, instrument };
  }
  return null;
}

/** The instrument file of an index entry, or null when it is not a plain `<group>/<SYMBOL>.json`. */
export function instrumentPath(root: string, file: string): string | null {
  if (!FILE_RE.test(file)) return null;
  const resolved = path.resolve(root, file);
  const base = path.resolve(root) + path.sep;
  return resolved.startsWith(base) ? resolved : null;
}

// ------------------------------------------------------------------------------------- files (LRU)
type Parsed = Record<SeriesName, Ohlcv[]>;
const lru = new Map<string, { mtimeMs: number; series: Parsed; derived: Map<string, Ohlcv[]> }>();

function toBars(rows: unknown): Ohlcv[] {
  if (!Array.isArray(rows)) return [];
  const out: Ohlcv[] = [];
  for (const r of rows) {
    if (!Array.isArray(r) || r.length < 6) continue;
    const bar = { t: Number(r[0]), o: Number(r[1]), h: Number(r[2]), l: Number(r[3]), c: Number(r[4]), v: Number(r[5]) };
    if (Object.values(bar).every(Number.isFinite)) out.push(bar);
  }
  return out;
}

function loadFile(file: string, id: string): { series: Parsed; derived: Map<string, Ohlcv[]> } | null {
  let mtimeMs: number;
  try {
    mtimeMs = statSync(file).mtimeMs;
  } catch {
    return null;
  }
  const hit = lru.get(file);
  if (hit && hit.mtimeMs === mtimeMs) {
    lru.delete(file); // most recently used goes last
    lru.set(file, hit);
    return hit;
  }
  let data: { id?: unknown; series?: Record<string, unknown> };
  try {
    data = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
  if (data.id !== id) return null;
  const series = Object.fromEntries(SERIES.map((s) => [s, toBars(data.series?.[s])])) as Parsed;
  const entry = { mtimeMs, series, derived: new Map<string, Ohlcv[]>() };
  lru.delete(file);
  lru.set(file, entry);
  while (lru.size > LRU_SIZE) lru.delete(lru.keys().next().value!);
  return entry;
}

/** Instrument files currently held in memory (for tests). */
export const marketCacheSize = () => lru.size;

// ------------------------------------------------------------------------------------- bars
const TF_ORDER: Timeframe[] = ['5m', '15m', '1h', '1d'];
const seriesTf = (s: SeriesName): Timeframe => s;

/**
 * The newest bars of `id` at `tf` covering `limit` bars of wall time, from the finest stored series
 * that covers that range (aggregated with the group's day rule). When no series finer than or equal
 * to `tf` reaches back far enough, a coarser one that does is used and `barTf` says so; when none
 * does, the series reaching furthest back. Returns null for an id the index does not list.
 */
export function marketBars(id: string, tf: Timeframe, limit: number, root = marketRoot()): MarketBarsReply | null {
  const found = findInstrument(id, root);
  if (!found) return null;
  const file = instrumentPath(root, found.instrument.file);
  if (!file) return null;
  const loaded = loadFile(file, id);
  if (!loaded) return null;
  const dayRule: DayRule = found.group.day_rule === 'utc' ? 'utc' : 'nyse';
  const n = Math.min(MAX_MARKET_BARS, Math.max(1, Math.floor(limit)));
  const span = n * TF_SECONDS[tf];
  const stored = SERIES.filter((s) => loaded.series[s].length > 0);
  const empty: MarketBarsReply = { id, tf, barTf: tf, series: '5m', dayRule, bars: [] };
  if (!stored.length) return empty;
  const newest = Math.max(...stored.map((s) => loaded.series[s].at(-1)!.t + SERIES_SECONDS[s]));
  const from = newest - span;
  const covers = (s: SeriesName) => loaded.series[s][0].t <= from + SERIES_SECONDS[s];
  const fine = stored.filter((s) => TF_ORDER.indexOf(seriesTf(s)) <= TF_ORDER.indexOf(tf));
  const coarse = stored.filter((s) => !fine.includes(s));
  const pick =
    fine.find(covers) ?? coarse.find(covers) ?? [...stored].sort((a, b) => loaded.series[a][0].t - loaded.series[b][0].t)[0];
  const barTf: Timeframe = TF_ORDER.indexOf(seriesTf(pick)) > TF_ORDER.indexOf(tf) ? seriesTf(pick) : tf;
  const key = `${pick}|${barTf}|${dayRule}`;
  let bars = loaded.derived.get(key);
  if (!bars) {
    bars = barTf === seriesTf(pick) ? loaded.series[pick] : aggregate(loaded.series[pick], barTf, dayRule);
    loaded.derived.set(key, bars);
  }
  let start = bars.length;
  while (start > 0 && bars[start - 1].t >= from) start--;
  return { id, tf, barTf, series: pick, dayRule, bars: bars.slice(Math.max(start, bars.length - n)) };
}
