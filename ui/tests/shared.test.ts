import { lastActivity, positionsBoard, RECENT_TRADE_HOURS } from '../shared/trades';
import { dailyResults, RED_STREAK_ALERT, type TradeStory } from '../shared/trades';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { aggregate, joinCandles, sessionDay, sessionsBackStart, type Ohlcv } from '../shared/aggregate';
import { listScenarios, ReplaySource } from '../server/sources/replay';
import { deriveActivity } from '../src/app/activity';
import type { Tables } from '../shared/schema';

/** A synthetic regular session: 78 five-minute bars from 13:30 UTC (09:30 EDT). */
function session(dayUtc: string, startPrice: number): Ohlcv[] {
  const open = Date.parse(`${dayUtc}T13:30:00Z`) / 1000;
  return Array.from({ length: 78 }, (_, i) => {
    const p = startPrice + i * 0.1;
    return { t: open + i * 300, o: p, h: p + 0.5, l: p - 0.5, c: p + 0.05, v: 100 };
  });
}

describe('aggregate (5m -> 15m / 1h / 1d)', () => {
  const bars = [...session('2026-10-29', 100), ...session('2026-10-30', 200)];

  it('keeps every session on one session day', () => {
    const days = new Set(bars.map((b) => sessionDay(b.t)));
    expect(days.size).toBe(2);
  });

  it('rolls up OHLCV without crossing sessions', () => {
    const daily = aggregate(bars, '1d');
    expect(daily).toHaveLength(2);
    expect(daily[0].o).toBe(100);
    expect(daily[0].c).toBeCloseTo(100 + 77 * 0.1 + 0.05);
    expect(daily[0].h).toBeCloseTo(100 + 77 * 0.1 + 0.5);
    expect(daily[0].l).toBeCloseTo(99.5);
    expect(daily[0].v).toBe(7800);
    expect(daily[1].o).toBe(200);
  });

  it('starts hourly buckets at the session open (09:30, 10:30, ...)', () => {
    const hourly = aggregate(bars, '1h');
    // 6.5 hours -> 7 buckets per session, the last one half an hour long.
    expect(hourly).toHaveLength(14);
    expect(hourly[0].t).toBe(bars[0].t);
    expect(hourly[1].t - hourly[0].t).toBe(3600);
    expect(hourly[6].v).toBe(600);
    expect(aggregate(bars, '15m')).toHaveLength(52);
  });

  it('finds the start of the n-th most recent session', () => {
    expect(sessionsBackStart(bars, 1)).toBe(bars[78].t);
    expect(sessionsBackStart(bars, 2)).toBe(bars[0].t);
    expect(sessionsBackStart(bars, 9)).toBe(bars[0].t);
  });
});

describe('aggregate on the UTC day rule (crypto and CFDs)', () => {
  /** Five-minute bars every 5 minutes from `from` (inclusive) for `count` bars. */
  function run(from: string, count: number): Ohlcv[] {
    const start = Date.parse(from) / 1000;
    return Array.from({ length: count }, (_, i) => ({ t: start + i * 300, o: i, h: i + 1, l: i - 1, c: i + 0.5, v: 2 }));
  }
  // 2026-11-08 22:00 UTC to 2026-11-09 02:00 UTC: one New York session day, two UTC days.
  const bars = run('2026-11-08T22:00:00Z', 48);
  const midnight = Date.parse('2026-11-09T00:00:00Z') / 1000;

  it('cuts daily candles at 00:00 UTC and stamps them there', () => {
    expect(new Set(bars.map((b) => sessionDay(b.t, 'utc'))).size).toBe(2);
    expect(new Set(bars.map((b) => sessionDay(b.t))).size).toBe(1); // nyse (default) is unchanged
    const daily = aggregate(bars, '1d', 'utc');
    expect(daily.map((d) => d.t)).toEqual([midnight - 86400, midnight]);
    expect(daily[0]).toMatchObject({ o: 0, c: 23.5, h: 24, l: -1, v: 48 });
    expect(daily[1]).toMatchObject({ o: 24, c: 47.5, h: 48, l: 23, v: 48 });
    expect(aggregate(bars, '1d')).toHaveLength(1);
  });

  it('puts intraday buckets on the UTC clock grid even when the first bar is late', () => {
    const late = run('2026-11-09T00:20:00Z', 12); // 00:20-01:15
    expect(aggregate(late, '1h', 'utc').map((b) => b.t)).toEqual([midnight, midnight + 3600]);
    expect(aggregate(late, '15m', 'utc')[0].t).toBe(midnight + 900);
    expect(aggregate(late, '1h')[0].t).toBe(late[0].t); // nyse anchors at the first bar, as before
  });

  it('counts UTC days back for ranges', () => {
    expect(sessionsBackStart(bars, 1, 'utc')).toBe(midnight);
    expect(sessionsBackStart(bars, 2, 'utc')).toBe(bars[0].t);
    expect(sessionsBackStart(bars, 1)).toBe(bars[0].t);
  });
});

describe('joining history, the market feed and the bot bars', () => {
  const bar = (t: number, c: number): Ohlcv => ({ t, o: c, h: c, l: c, c, v: 1 });
  it('lets each later source win from its first bar on', () => {
    const history = [bar(1, 10), bar(2, 11), bar(3, 12), bar(4, 13)];
    const feed = [bar(3, 22), bar(4, 23), bar(5, 24)];
    const live = [bar(5, 35), bar(6, 36)];
    expect(joinCandles([history, feed, live]).map((b) => [b.t, b.c])).toEqual([
      [1, 10],
      [2, 11],
      [3, 22],
      [4, 23],
      [5, 35],
      [6, 36],
    ]);
    expect(joinCandles([history, [], live]).map((b) => b.t)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(joinCandles([[], feed, []])).toEqual(feed);
  });
});

describe('activity feed', () => {
  const scenario = listScenarios().find((s) => s.id === 'baseline' && s.path.includes(`${path.sep}demo${path.sep}`));

  it('lists every decision, fill and pause of a recorded session, newest first', () => {
    const tables = new ReplaySource(scenario!).finalTables() as unknown as Tables;
    const feed = deriveActivity(tables);
    expect(feed.filter((a) => a.kind === 'decision')).toHaveLength(tables.risk_decisions.length);
    expect(feed.filter((a) => a.kind === 'fill')).toHaveLength(tables.fills.length);
    expect(feed.filter((a) => a.kind === 'pause')).toHaveLength(tables.pause_state.length);
    for (let i = 1; i < feed.length; i++) expect(feed[i - 1].at >= feed[i].at).toBe(true);
    expect(new Set(feed.map((a) => a.id)).size).toBe(feed.length);
  });

  it('links every decision to its trail', () => {
    const tables = new ReplaySource(scenario!).finalTables() as unknown as Tables;
    for (const a of deriveActivity(tables).filter((x) => x.kind === 'decision')) expect(a.link).toMatch(/^\/decisions\?c=cand-/);
  });
});

describe('taken, skipped, blocked (shared/verdict.ts)', () => {
  const decision = (approved: boolean, reasons: string, details: string[] = []) =>
    ({ approved, reasons, payload: { details } }) as unknown as import('../shared/schema').DecisionRow;

  it('reads a decision the way a person would', async () => {
    const { decisionVerdict } = await import('../shared/verdict');
    expect(decisionVerdict(decision(true, ''))).toMatchObject({ verdict: 'taken', label: 'Taken' });
    const busy = decision(false, 'ENTRY_LIMIT_REACHED,RISK_LIMIT', ['max open positions reached', 'a position or pending entry already exists in this instrument']);
    expect(decisionVerdict(busy)).toMatchObject({ verdict: 'skipped', why: 'already in this stock' });
    expect(decisionVerdict(decision(false, 'ENTRY_LIMIT_REACHED', ['max open positions reached'])).why).toBe('every position slot is in use');
    expect(decisionVerdict(decision(false, 'ENTRY_LIMIT_REACHED', ['max new entries per session reached'])).why).toBe("today's trades are used");
    expect(decisionVerdict(decision(false, 'SPREAD_TOO_WIDE', ['spread 14.28 bps > 10 bps'])).why).toBe('the spread is too wide right now');
    expect(decisionVerdict(decision(false, 'TOO_COSTLY'))).toMatchObject({ verdict: 'skipped' });
    expect(decisionVerdict(decision(false, 'NO_EDGE'))).toMatchObject({ verdict: 'skipped', why: 'no proven edge for this signal' });
  });

  it('calls it blocked only when the system was stopped, and that wins over routine reasons', async () => {
    const { decisionVerdict, verdictCounts } = await import('../shared/verdict');
    const paused = decision(false, 'PAUSED,ENTRY_LIMIT_REACHED', ['entries paused: FEED_GAP', 'max new entries per session reached']);
    expect(decisionVerdict(paused)).toMatchObject({ verdict: 'blocked', label: 'Blocked', why: 'new entries are paused' });
    expect(decisionVerdict(decision(false, 'STALE_DATA,CAPABILITY')).why).toBe('the price data was stale; this order type is not certified at the broker');
    const counts = verdictCounts([
      decision(true, ''),
      paused,
      decision(false, 'ENTRY_LIMIT_REACHED', ['max open positions reached']),
      decision(false, 'ENTRY_LIMIT_REACHED', ['max open positions reached']),
      decision(false, 'SPREAD_TOO_WIDE'),
    ]);
    expect(counts).toMatchObject({ taken: 1, skipped: 3, blocked: 1 });
    expect(counts.skippedWhy[0]).toEqual(['every position slot is in use', 2]);
  });
});

describe('daily results and losing days in a row (shared/trades.ts)', () => {
  const closed = (closedAt: string, pnl: number): TradeStory =>
    ({ sleeve: 'stocks', id: closedAt, state: 'closed', closedAt, pnl }) as unknown as TradeStory;
  const tz = 'Europe/Prague';
  const now = Date.parse('2026-11-13T20:00:00Z');

  it('groups closed trades by the local day they closed', () => {
    const r = dailyResults(
      [closed('2026-11-10T15:00:00Z', -0.2), closed('2026-11-10T20:59:00Z', 0.5), closed('2026-11-10T23:30:00Z', -0.1)],
      now,
      tz,
    );
    // 23:30 UTC is already 11 Nov in Prague
    expect(r.days.map((d) => d.day)).toEqual(['2026-11-10', '2026-11-11']);
    expect(r.days[0]).toMatchObject({ trades: 2, wins: 1 });
    expect(r.days[0].pnl).toBeCloseTo(0.3);
    expect(r.days[1].pnl).toBeCloseTo(-0.1);
  });

  it('counts losing days in a row back from the latest day, skipping days without a closed trade', () => {
    const trades = [closed('2026-11-09T16:00:00Z', 0.4), closed('2026-11-10T16:00:00Z', -0.2), closed('2026-11-12T16:00:00Z', -0.1), closed('2026-11-13T16:00:00Z', -0.3)];
    const r = dailyResults(trades, now, tz);
    expect(r.redStreak).toBe(3);
    expect(r.redStreak >= RED_STREAK_ALERT).toBe(true);
    expect(r.today?.pnl).toBeCloseTo(-0.3);
    expect(r.week).toBeCloseTo(-0.2);
  });

  it('a winning or flat latest day ends the run; open trades and other weeks do not count', () => {
    const open = { sleeve: 'stocks', id: 'o', state: 'open', closedAt: null, pnl: null } as unknown as TradeStory;
    const r = dailyResults([closed('2026-11-01T16:00:00Z', -5), closed('2026-11-12T16:00:00Z', -0.1), closed('2026-11-13T16:00:00Z', 0.05), open], now, tz);
    expect(r.redStreak).toBe(0);
    expect(r.week).toBeCloseTo(-0.05);
    expect(dailyResults([closed('2026-11-12T16:00:00Z', -1), closed('2026-11-13T16:00:00Z', 0)], now, tz).redStreak).toBe(0);
    const none = dailyResults([open], now, tz);
    expect(none).toEqual({ days: [], today: null, redStreak: 0, week: 0 });
  });
});

describe('positions board: open now and the last 48 hours (shared/trades.ts)', () => {
  const now = Date.parse('2026-11-10T18:00:00Z');
  const trade = (sleeve: string, id: string, state: string, at: Partial<Record<'plannedAt' | 'openedAt' | 'closedAt', string>>): TradeStory =>
    ({ sleeve, id, state, plannedAt: at.plannedAt ?? at.openedAt ?? at.closedAt, openedAt: at.openedAt ?? null, closedAt: at.closedAt ?? null }) as unknown as TradeStory;
  const trades = [
    trade('stocks', 'amzn', 'closed', { openedAt: '2026-11-10T15:05:00Z', closedAt: '2026-11-10T15:50:00Z' }),
    trade('crypto', 'btc', 'open', { openedAt: '2026-11-10T15:10:00Z' }),
    trade('crypto', 'eth', 'open', { openedAt: '2026-11-10T15:11:00Z' }),
    trade('cfd', 'us100', 'cancelled', { plannedAt: '2026-11-10T17:10:00Z' }),
    trade('cfd', 'gold-old', 'closed', { openedAt: '2026-11-07T10:00:00Z', closedAt: '2026-11-08T17:59:00Z' }),
    trade('stocks', 'nvda-old-open', 'open', { openedAt: '2026-11-01T15:00:00Z' }),
    trade('cfd', 'us100-resting', 'planned', { plannedAt: '2026-11-05T17:26:00Z' }),
  ];

  it('lists every open trade, whatever its age, newest first', () => {
    expect(positionsBoard(trades, now, 'all').open.map((t) => t.id)).toEqual(['eth', 'btc', 'nvda-old-open']);
    expect(positionsBoard(trades, now, 'crypto').open.map((t) => t.id)).toEqual(['eth', 'btc']);
    expect(positionsBoard(trades, now, 'cfd').open).toEqual([]);
  });

  it('keeps a planned trade under waiting, whatever its age, until it opens or is cancelled', () => {
    expect(positionsBoard(trades, now, 'all').waiting.map((t) => t.id)).toEqual(['us100-resting']);
    expect(positionsBoard(trades, now, 'crypto').waiting).toEqual([]);
    expect(positionsBoard(trades, now, 'all').recent.map((t) => t.id)).not.toContain('us100-resting');
  });

  it('lists trades that closed or were cancelled within 48 hours, and nothing older', () => {
    expect(RECENT_TRADE_HOURS).toBe(48);
    expect(positionsBoard(trades, now, 'all').recent.map((t) => t.id)).toEqual(['us100', 'amzn']);
    expect(positionsBoard(trades, now, 'stocks').recent.map((t) => t.id)).toEqual(['amzn']);
    // one minute later the trade closed 48 h and 1 min ago: still out; a wider window brings it back
    expect(positionsBoard(trades, now, 'cfd').recent.map((t) => t.id)).toEqual(['us100']);
    expect(positionsBoard(trades, now, 'cfd', 49).recent.map((t) => t.id)).toEqual(['us100', 'gold-old']);
    expect(lastActivity(trades[0])).toBe('2026-11-10T15:50:00Z');
  });
});
