/**
 * The intel feed's shapes (ADR-0047): news, insider filings and pre-market activity, as written
 * by `python -m trader.intel watch` under data/intel/ (docs/observability.md section 7).
 *
 * Shared by the server (which reads the files) and the page (which shows them). Every title and
 * summary here was written by an outside publisher or filer: the page renders it as plain text,
 * and `safeLink` lets through only links to the two hosts the feed itself reads from.
 */

export type IntelSourceStatus = 'ok' | 'partial' | 'error' | 'waiting' | 'idle';
export type IntelKind = 'news' | 'insider' | 'premarket';

export interface IntelSourceState {
  id: string;
  kind: IntelKind;
  label: string;
  every_minutes: number;
  status: IntelSourceStatus;
  note: string | null;
  error: string | null;
  /** The last run that read the source successfully. */
  updated_at: string | null;
  /** The last run that tried. */
  checked_at: string | null;
  retry_after: string | null;
  new: number;
}

export interface IntelTarget {
  id: string;
  group: string;
  symbol: string;
  name: string;
  file: string;
  has: { news: boolean; insiders: boolean; premarket: boolean };
  /** Set when the news is about a stand-in (a fund), not the instrument itself. */
  news_proxy: string | null;
  news_count_24h: number | null;
  last_news_at: string | null;
  insider_net_value: string | null;
  premarket_session: string | null;
  premarket_gap_pct: string | null;
  snapshot_hash: string;
  snapshot_at: string;
}

export interface IntelIndex {
  version: number;
  generated_at: string;
  every_minutes: number;
  sources: IntelSourceState[];
  targets: IntelTarget[];
}

export type IntelIndexReply = (IntelIndex & { available: true }) | { available: false; reason: string; sources: []; targets: [] };

export interface IntelRef {
  id: string;
  title: string;
  category: string;
  at: string;
  url: string | null;
}

export interface IntelNewsSummary {
  count_2h: number;
  count_24h: number;
  categories_24h: Record<string, number>;
  last_at: string | null;
  latest: IntelRef[];
  proxy: string | null;
}

export interface IntelInsiderSummary {
  window_days: number;
  filings: number;
  buys: number;
  sells: number;
  buy_value: string;
  sell_value: string;
  net_value: string;
  last_at: string | null;
  latest: IntelRef[];
}

export interface IntelPremarket {
  session: string | null;
  prior_close?: string | null;
  last?: string | null;
  gap_pct?: string | null;
  volume?: string | null;
  high?: string | null;
  low?: string | null;
  bars?: number | null;
  through?: string | null;
}

/** What was known about one instrument at `as_of` (trader.intel.snapshot). */
export interface IntelSnapshot {
  version: string;
  id: string;
  as_of: string;
  hash: string;
  news: IntelNewsSummary | null;
  insiders: IntelInsiderSummary | null;
  premarket: IntelPremarket | null;
  sources: Record<string, string>;
}

export interface IntelNewsItem {
  id: string;
  rev: number;
  title: string;
  summary: string;
  category: string;
  tags: string[];
  at: string;
  first_seen_at: string;
  url: string | null;
  feed: string;
  author: string;
  proxy: string | null;
}

export interface IntelInsiderItem {
  id: string;
  rev: number;
  title: string;
  category: string;
  at: string;
  first_seen_at: string;
  url: string | null;
  facts: Record<string, unknown>;
}

/** The newest items of one instrument (data/intel/latest/<group>/<SYMBOL>.json). */
export interface IntelLatest {
  id: string;
  name: string;
  generated_at: string;
  snapshot: IntelSnapshot;
  news: IntelNewsItem[];
  insiders: IntelInsiderItem[];
}

/** What was known at a past moment, and whether the feed was running around then. */
export interface IntelKnownReply {
  id: string;
  at: string;
  /** The newest snapshot taken at or before `at`; null when the feed had not written one. */
  snapshot: IntelSnapshot | null;
  /** Seconds between that snapshot and `at`. A snapshot is written only when something changed. */
  snapshotAgeSeconds: number | null;
  /** The feed's last run at or before `at`: a large gap means the feed was not running. */
  lastRunAt: string | null;
  lastRunAgeSeconds: number | null;
  everyMinutes: number;
}

/** The newest row whose `as_of` is at or before `atMs` (rows in any order). */
export function asOf<T extends { as_of: string }>(rows: readonly T[], atMs: number): T | null {
  let best: T | null = null;
  let bestMs = -Infinity;
  for (const row of rows) {
    const ms = Date.parse(row.as_of);
    if (Number.isFinite(ms) && ms <= atMs && ms >= bestMs) {
      best = row;
      bestMs = ms;
    }
  }
  return best;
}

/** Links are shown only to these hosts: the news provider's site and the SEC. */
export const INTEL_LINK_HOSTS: readonly string[] = ['www.benzinga.com', 'www.sec.gov'];

/** `url` when it is a plain https link to one of the feed's own hosts, else null. */
export function safeLink(url: unknown): string | null {
  if (typeof url !== 'string' || url.length > 500 || /[\s\u0000-\u001f\u007f]/.test(url)) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port) return null;
  return INTEL_LINK_HOSTS.includes(parsed.hostname) ? parsed.toString() : null;
}

/** What a practice plan may cite (the same pattern as trader.practice.journal.EVIDENCE_REF). */
export const EVIDENCE_REF = /^(news|insider|premarket|snap):[A-Za-z0-9_.:@+-]{3,120}$/;

/** The well-formed references of a journal PLAN's `evidence`, at most twelve. */
export function evidenceRefs(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === 'string' && EVIDENCE_REF.test(v)).slice(0, 12);
}

/** A reference split into what it points at: a snapshot of an instrument, or a stored record. */
export function parseEvidenceRef(ref: string): { kind: 'snap'; id: string; at: string } | { kind: 'news' | 'insider' | 'premarket'; recordId: string } | null {
  if (!EVIDENCE_REF.test(ref)) return null;
  const cut = ref.indexOf(':');
  const kind = ref.slice(0, cut);
  const rest = ref.slice(cut + 1);
  if (kind === 'snap') {
    const split = rest.lastIndexOf('@');
    const id = rest.slice(0, split);
    const at = rest.slice(split + 1);
    if (split < 0 || !/^[a-z][a-z0-9]{0,15}:[A-Z0-9_]{1,16}$/.test(id) || !Number.isFinite(Date.parse(at))) return null;
    return { kind: 'snap', id, at };
  }
  return { kind: kind as 'news' | 'insider' | 'premarket', recordId: rest };
}

/** Seconds as "3 min", "2 h 5 min" or "4 days", for snapshot and run ages. */
export function ageText(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return 'unknown';
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  const minutes = Math.round(s / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} days`;
}

/** The feed counts as stopped at `at` when its last run is older than three cadences. */
export const feedWasStale = (known: Pick<IntelKnownReply, 'lastRunAgeSeconds' | 'everyMinutes'>): boolean =>
  known.lastRunAgeSeconds === null || known.lastRunAgeSeconds > 3 * known.everyMinutes * 60;
