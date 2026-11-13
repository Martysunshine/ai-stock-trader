/**
 * Roll 5-minute bars up to 15m / 1h / 1d. Buckets never cross a day boundary, whose rule depends on
 * the market (`DayRule`):
 *
 * - `nyse` (the default; stocks): buckets start at each session's first bar, so hourly bars run
 *   09:30-10:30 like most charting tools. US regular hours run 13:30-21:00 UTC (EDT) or 14:30-21:00
 *   UTC (EST); shifting by 5 h keeps every bar of a session on one UTC date in both, without a
 *   time-zone lookup per bar.
 * - `utc` (crypto and CFDs): a day is 00:00-24:00 UTC and buckets sit on the UTC clock grid, so a
 *   daily candle cuts at 00:00 UTC and starts there even when the day's first bar is missing.
 */
export interface Ohlcv {
  /** Bar start, seconds since epoch. */
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export type Timeframe = '5m' | '15m' | '1h' | '1d';
export const TF_SECONDS: Record<Timeframe, number> = { '5m': 300, '15m': 900, '1h': 3600, '1d': 86400 };

export type DayRule = 'nyse' | 'utc';

/** The day a bar belongs to (days since the epoch): the New York session, or the UTC date. */
export const sessionDay = (t: number, rule: DayRule = 'nyse') => Math.floor((rule === 'utc' ? t : t - 5 * 3600) / 86400);

export function aggregate(bars: Ohlcv[], tf: Timeframe, rule: DayRule = 'nyse'): Ohlcv[] {
  if (tf === '5m' || bars.length === 0) return bars;
  const size = TF_SECONDS[tf];
  const out: Ohlcv[] = [];
  let day = Number.NaN;
  let dayStart = 0;
  let cur: Ohlcv | null = null;
  let curKey = Number.NaN;
  for (const b of bars) {
    const d = sessionDay(b.t, rule);
    if (d !== day) {
      day = d;
      dayStart = rule === 'utc' ? d * 86400 : b.t;
    }
    const key = tf === '1d' ? d : d * 100000 + Math.floor((b.t - dayStart) / size);
    if (cur && key === curKey) {
      cur.h = Math.max(cur.h, b.h);
      cur.l = Math.min(cur.l, b.l);
      cur.c = b.c;
      cur.v += b.v;
    } else {
      if (cur) out.push(cur);
      const start = tf === '1d' ? dayStart : dayStart + Math.floor((b.t - dayStart) / size) * size;
      cur = { t: start, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v };
      curKey = key;
    }
  }
  if (cur) out.push(cur);
  return out;
}

/** Start time of the n-th most recent session (or UTC day) in `bars` (n = 1 is the last one). */
export function sessionsBackStart(bars: Ohlcv[], n: number, rule: DayRule = 'nyse'): number | null {
  let seen = 0;
  let lastDay = Number.NaN;
  for (let i = bars.length - 1; i >= 0; i--) {
    const d = sessionDay(bars[i].t, rule);
    if (d !== lastDay) {
      seen += 1;
      lastDay = d;
      if (seen > n) return bars[i + 1]?.t ?? null;
    }
  }
  return bars[0]?.t ?? null;
}

/**
 * Join candle series that overlap in time, later sources winning from their first candle on:
 * research history, then the market feed, then the bot's own bars (each newer and closer to what
 * the bot saw). Inputs are oldest first; the result is too.
 */
export function joinCandles<T extends { t: number }>(parts: T[][]): T[] {
  let out: T[] = [];
  for (const part of parts) {
    if (!part.length) continue;
    const first = part[0].t;
    out = [...out.filter((b) => b.t < first), ...part];
  }
  return out;
}
