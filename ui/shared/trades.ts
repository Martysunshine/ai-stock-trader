/**
 * One shape for every trade of the mission, whichever sleeve made it: the bot's stock round trips
 * (from its own tables) and the assistant's crypto and CFD trades (from the practice journals).
 * The Money page lists them; the trade story page tells one from plan to lesson.
 */
import Decimal from 'decimal.js';
import { decisionTrail, symbolOf } from './derive';
import type { PracticeSleeve, PracticeTrade, SleeveId, TradeEvent, TradeMark } from './mission';
import type { Tables } from './schema';

export interface TradeStory {
  sleeve: SleeveId;
  id: string;
  symbol: string;
  /** Market-feed id for the price chart (stocks:AAPL, crypto:BTC, cfd:GOLD). */
  marketId: string;
  /** The bot's own instrument id (stocks only): its bars can be drawn too. */
  instrumentId: string | null;
  direction: 'long' | 'short';
  size: string;
  state: 'planned' | 'open' | 'closed' | 'cancelled';
  plannedAt: string;
  openedAt: string | null;
  closedAt: string | null;
  /** Fill price when filled, else the planned entry. */
  entry: number | null;
  exit: number | null;
  /** Net result in USD after fees and financing; null while open. */
  pnl: number | null;
  /** While open: the trade valued at the latest feed price (practice sleeves); else null. */
  mark: TradeMark | null;
  fees: number;
  financing: number;
  /** Planned loss at the stop, USD. */
  risk: number | null;
  reason: string | null;
  plan: {
    entry: number | null;
    stop: number | null;
    target: number | null;
    thesis: string;
    setup: string;
    timeframe: string;
    invalidation: string;
    /** Intel references the plan cited (ADR-0047); empty for the bot's trades. */
    evidence: string[];
  };
  events: TradeEvent[];
  lesson: { text: string; followedPlan: boolean } | null;
  /** Who made it: the bot or Claude. */
  by: string;
  /** Stocks: the candidate behind it (the Decisions page shows its full trail). */
  candidateId: string | null;
}

const n = (v: unknown): number | null => {
  if (v === null || v === undefined || v === '') return null;
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
};

export const marketIdOf = (sleeve: SleeveId, symbol: string): string =>
  sleeve === 'crypto' ? `crypto:${symbol.replace(/\/?USD$/, '')}` : `${sleeve}:${symbol}`;

export const tradeHref = (t: Pick<TradeStory, 'sleeve' | 'id'>): string => `/trades/${t.sleeve}/${encodeURIComponent(t.id)}`;

// ------------------------------------------------------------------------------------- practice
export function practiceStory(sleeve: Exclude<SleeveId, 'stocks'>, t: PracticeTrade): TradeStory {
  return {
    sleeve,
    id: t.tradeId,
    symbol: t.epic,
    marketId: marketIdOf(sleeve, t.epic),
    instrumentId: null,
    direction: t.direction === 'BUY' ? 'long' : 'short',
    size: t.size,
    state: t.state,
    plannedAt: t.plannedAt,
    openedAt: t.openedAt,
    closedAt: t.closedAt,
    entry: n(t.fill) ?? n(t.entry),
    exit: n(t.closeLevel),
    pnl: n(t.pnlUsd),
    fees: n(t.feesUsd) ?? 0,
    financing: n(t.financingUsd) ?? 0,
    risk: n(t.riskUsd),
    reason: t.closeReason,
    plan: {
      entry: n(t.entry),
      stop: n(t.plannedStop),
      target: n(t.plannedTarget),
      thesis: t.thesis,
      setup: t.setup,
      timeframe: t.timeframe,
      invalidation: t.invalidation,
      evidence: t.evidence ?? [],
    },
    events: t.events,
    lesson: t.lesson !== null ? { text: t.lesson, followedPlan: t.followedPlan === true } : null,
    by: 'Claude',
    candidateId: null,
    mark: t.state === 'open' ? t.mark : null,
  };
}

export const practiceStories = (sleeves: PracticeSleeve[]): TradeStory[] =>
  sleeves.flatMap((p) => p.trades.map((t) => practiceStory(p.id, t)));

// ------------------------------------------------------------------------------------- stocks
/** A number as the bot stored it, shortened for reading (at most 4 decimals, no trailing zeros). */
const short = (v: unknown): string => {
  if (v === null || v === undefined || v === '') return '-';
  try {
    return new Decimal(v as Decimal.Value).toDecimalPlaces(4).toString();
  } catch {
    return String(v);
  }
};

/** candidate_a_trend_continuation -> Candidate A trend continuation */
const ruleName = (id: string): string => {
  const words = id.replace(/_/g, ' ').trim();
  return words.replace(/^candidate (\w)/i, (_m, c: string) => `Candidate ${c.toUpperCase()}`);
};

const EXIT_WORDS: Record<string, string> = {
  EXIT_STOP: 'Stop hit',
  EXIT_TARGET: 'Target reached',
  EXIT_TIME: 'Time limit',
  EXIT_SESSION: 'Session close',
  EXIT_RISK_REDUCTION: 'Risk reduction',
  EXIT_LIQUIDATION: 'Liquidation',
};

/** The bot's filled entries as round trips: entry fills, exit fills, fees and net result. */
export function stockStories(t: Tables): TradeStory[] {
  const out: TradeStory[] = [];
  for (const entry of t.order_intents) {
    if (entry.purpose !== 'ENTRY' || !entry.candidate_id) continue;
    const trail = decisionTrail(t, entry.candidate_id);
    const buys = trail.fills.filter((f) => f.intent_id === entry.intent_id || (f.client_order_id === entry.client_order_id && f.side === 'BUY'));
    if (!buys.length) continue; // an entry that never filled is not a trade
    const sells = trail.fills.filter((f) => !buys.includes(f));
    const qtyIn = Decimal.sum(...buys.map((f) => f.quantity));
    const cost = Decimal.sum(...buys.map((f) => new Decimal(f.quantity).mul(f.price)));
    const qtyOut = sells.length ? Decimal.sum(...sells.map((f) => f.quantity)) : new Decimal(0);
    const proceeds = sells.length ? Decimal.sum(...sells.map((f) => new Decimal(f.quantity).mul(f.price))) : new Decimal(0);
    const fees = Decimal.sum(new Decimal(0), ...trail.fills.map((f) => f.fee ?? 0));
    const closed = qtyOut.gte(qtyIn.minus('0.000000001'));
    const pnl = closed ? proceeds.minus(cost).minus(fees) : null;
    const cand = trail.candidate;
    const stop = n(trail.exitPlan?.stop_price) ?? n(cand?.payload.stop_price);
    const target = n(trail.exitPlan?.target_price) ?? n(cand?.payload.target_price);
    const lastExit = trail.exits.filter((x) => Number(x.cum_filled_qty) > 0).at(-1) ?? null;
    const events: TradeEvent[] = [];
    if (trail.decision) {
      const p = trail.decision.payload;
      events.push({
        at: trail.decision.decided_at,
        kind: 'plan',
        title: 'Approved by the risk engine',
        detail: `size ${short(p.quantity ?? entry.quantity)}, limit ${short(p.entry_limit_price ?? entry.limit_price)}, planned loss ${short(p.planned_loss_usd)} USD, costs ${short(p.estimated_costs_usd)} USD`,
      });
    }
    for (const f of buys) events.push({ at: f.exec_time, kind: 'open', title: 'Bought', detail: `${short(f.quantity)} at ${short(f.price)}, fee ${short(f.fee)}` });
    for (const f of sells) {
      const intent = trail.exits.find((x) => x.intent_id === f.intent_id);
      events.push({
        at: f.exec_time,
        kind: 'close',
        title: `Sold · ${EXIT_WORDS[intent?.purpose ?? ''] ?? 'Exit'}`,
        detail: `${short(f.quantity)} at ${short(f.price)}, fee ${short(f.fee)}`,
      });
    }
    events.sort((a, b) => a.at.localeCompare(b.at));
    const symbol = symbolOf(entry.instrument_id);
    out.push({
      sleeve: 'stocks',
      id: entry.intent_id,
      symbol,
      marketId: marketIdOf('stocks', symbol),
      instrumentId: entry.instrument_id,
      direction: entry.side === 'BUY' ? 'long' : 'short',
      size: qtyIn.toDecimalPlaces(4).toString(),
      state: closed ? 'closed' : 'open',
      plannedAt: trail.decision?.decided_at ?? entry.created_at,
      openedAt: buys[0].exec_time,
      closedAt: closed ? sells[sells.length - 1].exec_time : null,
      entry: cost.div(qtyIn).toNumber(),
      exit: qtyOut.gt(0) ? proceeds.div(qtyOut).toNumber() : null,
      pnl: pnl ? pnl.toNumber() : null,
      fees: fees.toNumber(),
      financing: 0,
      risk: n(trail.decision?.payload.planned_loss_usd) ?? n(trail.exitPlan?.planned_loss_usd),
      reason: lastExit ? (EXIT_WORDS[lastExit.purpose] ?? lastExit.purpose) : null,
      plan: {
        entry: n(cand?.payload.reference_price),
        stop,
        target,
        thesis: cand
          ? `The ${ruleName(cand.strategy_id)} rule signalled a ${entry.side === 'BUY' ? 'long' : 'short'} entry on a completed 5-minute bar, and the risk engine approved it.`
          : 'A strategy signal approved by the risk engine.',
        setup: cand ? ruleName(cand.strategy_id) : 'strategy',
        timeframe: '5-minute bars, same-day exit',
        invalidation: stop !== null ? `price at or below the stop ${stop}` : 'the stop',
        evidence: [],
      },
      events,
      lesson: null,
      by: 'the bot',
      candidateId: entry.candidate_id,
      mark: null,
    });
  }
  return out.sort((a, b) => (a.openedAt ?? a.plannedAt).localeCompare(b.openedAt ?? b.plannedAt));
}

// ------------------------------------------------------------------------------------- numbers
export interface TradeStats {
  closed: number;
  open: number;
  wins: number;
  /** 0..1, or null before any closed trade. */
  winRate: number | null;
  pnl: number;
  fees: number;
  financing: number;
  best: number | null;
  worst: number | null;
  /** Average result in multiples of the planned risk, over closed trades that planned one. */
  avgR: number | null;
}

export function tradeStats(trades: TradeStory[]): TradeStats {
  const closed = trades.filter((t) => t.state === 'closed' && t.pnl !== null);
  const results = closed.map((t) => t.pnl as number);
  const rs = closed.filter((t) => t.risk && t.risk > 0).map((t) => (t.pnl as number) / (t.risk as number));
  const wins = results.filter((x) => x > 0).length;
  return {
    closed: closed.length,
    open: trades.filter((t) => t.state === 'open').length,
    wins,
    winRate: closed.length ? wins / closed.length : null,
    pnl: results.reduce((a, b) => a + b, 0),
    fees: closed.reduce((a, t) => a + t.fees, 0),
    financing: closed.reduce((a, t) => a + t.financing, 0),
    best: results.length ? Math.max(...results) : null,
    worst: results.length ? Math.min(...results) : null,
    avgR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
  };
}

/** Result in multiples of the planned risk ("+1.6R"); null without a planned risk or a result. */
export const rMultiple = (t: TradeStory): number | null => (t.pnl !== null && t.risk && t.risk > 0 ? t.pnl / t.risk : null);

// ------------------------------------------------------------------------------------- days
/** The owner's rule of thumb (10 Nov 2026): this many losing days in a row need an explanation. */
export const RED_STREAK_ALERT = 3;

export interface DayResult {
  /** YYYY-MM-DD, the day the trades closed (local time of whoever reads the console). */
  day: string;
  /** Net result of the trades closed that day, after fees and financing. */
  pnl: number;
  trades: number;
  wins: number;
}

export interface DailyResults {
  /** Days with at least one closed trade, oldest first. */
  days: DayResult[];
  /** Today's result; null when no trade has closed today. */
  today: DayResult | null;
  /** Losing days in a row, counted back from the latest day with a closed trade. Days without a
   *  closed trade are skipped: they neither add to the run nor end it. */
  redStreak: number;
  /** Net result of the last seven calendar days, today included. */
  week: number;
}

/** YYYY-MM-DD of a moment in a time zone (the runtime's own zone when none is given). */
export const localDay = (at: string | number | Date, timeZone?: string): string =>
  new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(at));

/** Closed trades grouped by the day they closed: each day's result and the run of losing days. */
export function dailyResults(trades: TradeStory[], now: number, timeZone?: string): DailyResults {
  const byDay = new Map<string, DayResult>();
  for (const t of trades) {
    if (t.state !== 'closed' || t.pnl === null || !t.closedAt) continue;
    const day = localDay(t.closedAt, timeZone);
    const d = byDay.get(day) ?? { day, pnl: 0, trades: 0, wins: 0 };
    d.pnl += t.pnl;
    d.trades += 1;
    if (t.pnl > 0) d.wins += 1;
    byDay.set(day, d);
  }
  const days = [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
  let redStreak = 0;
  for (let i = days.length - 1; i >= 0 && days[i].pnl < 0; i -= 1) redStreak += 1;
  const weekStart = localDay(now - 6 * 86_400_000, timeZone);
  return {
    days,
    today: byDay.get(localDay(now, timeZone)) ?? null,
    redStreak,
    week: days.filter((d) => d.day >= weekStart).reduce((a, d) => a + d.pnl, 0),
  };
}

// ------------------------------------------------------------------------------------- board
/** The Positions card lists trades that ended, or were planned, within this many hours. */
export const RECENT_TRADE_HOURS = 48;

/** When a trade last did something: closed, else opened, else planned. */
export const lastActivity = (t: TradeStory): string => t.closedAt ?? t.openedAt ?? t.plannedAt;

export interface PositionsBoard {
  /** Trades open now, newest first. */
  open: TradeStory[];
  /** Planned trades that have not opened: a plan about to be placed, or an order resting at the venue. */
  waiting: TradeStory[];
  /** Trades that ended (closed or cancelled) within the window, newest first. */
  recent: TradeStory[];
}

/** What is open now and what happened lately, for one sleeve or for all three. */
export function positionsBoard(trades: TradeStory[], now: number, sleeve: SleeveId | 'all', hours = RECENT_TRADE_HOURS): PositionsBoard {
  const mine = trades.filter((t) => sleeve === 'all' || t.sleeve === sleeve);
  const newest = (a: TradeStory, b: TradeStory) => Date.parse(lastActivity(b)) - Date.parse(lastActivity(a));
  const since = now - hours * 3_600_000;
  return {
    open: mine.filter((t) => t.state === 'open').sort(newest),
    waiting: mine.filter((t) => t.state === 'planned').sort(newest),
    recent: mine.filter((t) => (t.state === 'closed' || t.state === 'cancelled') && Date.parse(lastActivity(t)) >= since).sort(newest),
  };
}
