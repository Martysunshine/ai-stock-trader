/**
 * The mission's practice sleeves, read-only (ADR-0046): the rules in configs/practice/*.yaml, the
 * hash-chained journals `trader journal` writes under runs/practice/ and the practice watch's
 * scan beside them (ADR-0049). The console never writes them. The stocks sleeve is the paper bot's own tables; only its start and portfolio id
 * come from here (the stocks book's configuration, config.PAPER_CONFIG, and the policy it names).
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { parse as parseYaml } from 'yaml';
import { evidenceRefs } from '../../shared/intel';
import type { EquityPoint, MissionState, PracticeScan, PracticeSleeve, PracticeTrade, PracticeTraderState, ScanTicket, TradeMark, TraderDecision } from '../../shared/mission';
import { PAPER_CONFIG, repoPath } from '../config';
import { readMarketIndex, type MarketIndex } from './market';

const PRACTICE: { id: PracticeSleeve['id']; rules: string }[] = [
  { id: 'crypto', rules: 'configs/practice/crypto_cfd.yaml' },
  { id: 'cfd', rules: 'configs/practice/capital_demo.yaml' },
];
const GENESIS = '0'.repeat(64);
const DEFAULT_START = 100;
const DEFAULT_TARGET = 10_000;

const num = (v: unknown, fallback: number): number => {
  const n = typeof v === 'string' || typeof v === 'number' ? Number(v) : NaN;
  return Number.isFinite(n) ? n : fallback;
};
const str = (v: unknown): string | null => (v === undefined || v === null ? null : String(v));

function yaml(rel: string): Record<string, unknown> {
  try {
    return (parseYaml(readFileSync(repoPath(rel), 'utf8')) ?? {}) as Record<string, unknown>;
  } catch {
    return {};
  }
}

interface JournalEvent {
  seq: number;
  event: string;
  at: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  record_hash: string;
}

/**
 * The journal's events and whether its chain verifies. Each line is canonical JSON (sorted keys,
 * compact, ASCII), so the hashed body is the line without its record_hash member.
 */
export function readJournal(file: string): { events: JournalEvent[]; chainOk: boolean } {
  if (!existsSync(file)) return { events: [], chainOk: true };
  const events: JournalEvent[] = [];
  let prev = GENESIS;
  let chainOk = true;
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    let e: JournalEvent;
    try {
      e = JSON.parse(line) as JournalEvent;
    } catch {
      return { events, chainOk: false };
    }
    const body = line.trim().replace(/,"record_hash":"[0-9a-f]{64}"/, '');
    const hash = createHash('sha256').update(body, 'utf8').digest('hex');
    if (e.prev_hash !== prev || hash !== e.record_hash || e.seq !== events.length + 1) chainOk = false;
    prev = e.record_hash;
    events.push(e);
  }
  return { events, chainOk };
}

const CLOSE_WORDS: Record<string, string> = {
  TARGET: 'Target reached',
  STOP: 'Stop hit',
  MANUAL: 'Closed by hand',
  TIME: 'Time limit',
  INVALIDATED: 'Idea invalidated',
  OTHER: 'Closed',
};

export function tradesOf(events: JournalEvent[]): PracticeTrade[] {
  const byId = new Map<string, PracticeTrade>();
  for (const e of events) {
    const p = e.payload;
    const id = str(p.trade_id);
    if (!id) continue;
    if (e.event === 'PLAN') {
      const stop = String(p.stop ?? '');
      const target = str(p.target);
      byId.set(id, {
        tradeId: id,
        epic: String(p.epic ?? ''),
        direction: p.direction === 'SELL' ? 'SELL' : 'BUY',
        size: String(p.size ?? ''),
        entry: String(p.entry ?? ''),
        stop,
        target,
        plannedStop: stop,
        plannedTarget: target,
        riskUsd: str(p.risk_usd),
        fxToUsd: String(p.fx_to_usd ?? '1'),
        mark: null,
        thesis: String(p.thesis ?? ''),
        setup: String(p.setup ?? ''),
        timeframe: String(p.timeframe ?? ''),
        invalidation: String(p.invalidation ?? ''),
        evidence: evidenceRefs(p.evidence),
        plannedAt: e.at,
        openedAt: null,
        fill: null,
        closedAt: null,
        closeLevel: null,
        closeReason: null,
        pnlUsd: null,
        feesUsd: null,
        financingUsd: null,
        lesson: null,
        followedPlan: null,
        events: [
          {
            at: e.at,
            kind: 'plan',
            title: 'Planned',
            detail: `${p.direction === 'SELL' ? 'short' : 'long'} ${p.size} at ${p.entry}, stop ${stop}${target ? `, target ${target}` : ''}, risk ${p.risk_usd ?? '-'} USD`,
          },
        ],
        state: 'planned',
      });
      continue;
    }
    const t = byId.get(id);
    if (!t) continue;
    if (e.event === 'OPEN') {
      Object.assign(t, { state: 'open', openedAt: e.at, fill: str(p.fill_level), size: String(p.size ?? t.size) });
      t.events.push({ at: e.at, kind: 'open', title: 'Opened', detail: `filled ${p.size ?? t.size} at ${p.fill_level} (planned ${t.entry})` });
    } else if (e.event === 'CLOSE') {
      const reason = CLOSE_WORDS[String(p.reason)] ?? String(p.reason ?? 'Closed');
      Object.assign(t, {
        state: 'closed',
        closedAt: e.at,
        closeLevel: str(p.level),
        closeReason: reason,
        pnlUsd: str(p.pnl_usd),
        feesUsd: str(p.fees_usd),
        financingUsd: str(p.financing_usd),
      });
      const notes = str(p.notes);
      t.events.push({
        at: e.at,
        kind: 'close',
        title: reason,
        detail: `closed at ${p.level}, result ${p.pnl_usd} USD (fees ${p.fees_usd ?? '0'}, financing ${p.financing_usd ?? '0'})${notes ? `. ${notes}` : ''}`,
      });
    } else if (e.event === 'CANCEL' && t.state === 'planned') {
      t.state = 'cancelled';
      t.events.push({ at: e.at, kind: 'cancel', title: 'Plan cancelled', detail: String(p.reason ?? '') });
    } else if (e.event === 'AMEND') {
      const changes = (p.changes ?? {}) as Record<string, unknown>;
      if (changes.stop !== undefined) t.stop = String(changes.stop);
      if (changes.target !== undefined) t.target = String(changes.target);
      const what = Object.entries(changes).map(([k, v]) => `${k} to ${String(v)}`).join(', ');
      t.events.push({ at: e.at, kind: 'amend', title: 'Changed', detail: `${what}. ${String(p.reason ?? '')}` });
    } else if (e.event === 'NOTE') {
      t.events.push({ at: e.at, kind: 'note', title: 'Note', detail: String(p.text ?? '') });
    } else if (e.event === 'REVIEW') {
      t.lesson = String(p.lesson ?? '');
      t.followedPlan = p.followed_plan === true;
      t.events.push({ at: e.at, kind: 'review', title: t.followedPlan ? 'Reviewed · plan followed' : 'Reviewed · plan not followed', detail: t.lesson });
    }
  }
  return [...byId.values()];
}

export interface LastPrice {
  price: number;
  /** Unix seconds. */
  at: number;
}

/**
 * The market feed's latest price per epic of a practice sleeve. Both sleeves trade Capital.com
 * CFDs (ADR-0049), so the broker's own quote in the `cfd` group comes first. Until the feed has
 * the crypto CFD epics, Kraken's spot price stands in for them (BTC is BTCUSD).
 */
export function latestPrices(id: PracticeSleeve['id'], index: MarketIndex | null = readMarketIndex()): Map<string, LastPrice> {
  const out = new Map<string, LastPrice>();
  const add = (groupId: string, epic: (symbol: string) => string) => {
    for (const i of index?.groups.find((g) => g.id === groupId)?.instruments ?? []) {
      const price = Number(i.last_price);
      if (!Number.isFinite(price) || price <= 0 || !i.last_at || out.has(epic(i.symbol))) continue;
      out.set(epic(i.symbol), { price, at: i.last_at });
    }
  };
  add('cfd', (symbol) => symbol);
  if (id === 'crypto') add('crypto', (symbol) => `${symbol}USD`);
  return out;
}

/**
 * The practice watch's scan (runs/practice/scan.json, ADR-0049): the index dip reading and each
 * open practice trade against its stop, target and time stop. Null when the file was never
 * written or cannot be read; a scan that could not run carries its reason in `error`.
 */
export function readScan(file: string): PracticeScan | null {
  let raw: Record<string, unknown>;
  try {
    raw = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || typeof raw.generated_at !== 'string') return null;
  const rows = (v: unknown) => (Array.isArray(v) ? (v as Record<string, unknown>[]) : []);
  const feed = (raw.feed ?? {}) as Record<string, unknown>;
  const ticket = (t: unknown): ScanTicket | null => {
    if (!t || typeof t !== 'object') return null;
    const v = t as Record<string, unknown>;
    const status = v.status === 'TRADE' || v.status === 'GROUP_BUSY' ? v.status : 'TOO_LARGE';
    const direction = v.direction === 'SHORT' ? 'SHORT' : 'LONG';
    return { status, direction, size: String(v.size ?? ''), stop: String(v.stop ?? ''), target: String(v.target ?? ''), riskUsd: String(v.risk_usd ?? ''), maxHoldHours: num(v.max_hold_hours, 0), note: String(v.note ?? '') };
  };
  return {
    generatedAt: raw.generated_at,
    error: typeof raw.error === 'string' ? raw.error : null,
    feedStale: feed.stale === true,
    feedUpdatedAt: str(feed.updated_at),
    riskLimitUsd: str(raw.risk_limit_usd),
    title: str(raw.title),
    nextDecisionAt: str(raw.next_decision_at),
    readings: rows(raw.readings).map((r) => ({
      epic: String(r.epic ?? ''),
      day: String(r.day ?? ''),
      stale: r.stale === true,
      close: String(r.close ?? ''),
      change: String(r.change ?? '0'),
      direction: r.direction === 'LONG' || r.direction === 'SHORT' ? r.direction : null,
      decisionBar: r.decision_bar === true,
      signal: r.signal === true,
      ticket: ticket(r.ticket),
    })),
    openTrades: rows(raw.open_trades).map((o) => ({
      sleeve: String(o.sleeve ?? ''),
      tradeId: String(o.trade_id ?? ''),
      epic: String(o.epic ?? ''),
      dueAt: str(o.due_at),
      overdue: o.overdue === true,
      beyondStop: o.beyond_stop === true,
      atTarget: o.at_target === true,
      exitSignal: typeof o.exit_signal === 'boolean' ? o.exit_signal : null,
    })),
  };
}

/** A heartbeat older than this means the practice trader is not running (it writes every minute). */
export const TRADER_STALE_SECONDS = 5 * 60;

/**
 * The practice trader's state (runs/practice/trader.json, ADR-0056): its heartbeat, its setups and
 * each one's last decision. Null when the file was never written or cannot be read.
 */
export function readTrader(file: string, nowMs: number = Date.now()): PracticeTraderState | null {
  let raw: Record<string, unknown>;
  try {
    raw = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || typeof raw.updated_at !== 'string') return null;
  const rows = (v: unknown) => (Array.isArray(v) ? (v as Record<string, unknown>[]) : []);
  const decision = (d: unknown): TraderDecision | null => {
    if (!d || typeof d !== 'object') return null;
    const v = d as Record<string, unknown>;
    return {
      setup: String(v.setup ?? ''),
      epic: String(v.epic ?? ''),
      decidedAt: String(v.decided_at ?? ''),
      direction: v.direction === 'BUY' || v.direction === 'SELL' ? v.direction : null,
      outcome: String(v.outcome ?? ''),
      detail: String(v.detail ?? ''),
    };
  };
  const updated = Date.parse(raw.updated_at);
  return {
    live: raw.live === true,
    updatedAt: raw.updated_at,
    fresh: Number.isFinite(updated) && nowMs - updated < TRADER_STALE_SECONDS * 1000,
    nextDecisionAt: str(raw.next_decision_at),
    hedging: raw.hedging === true,
    entriesPaused: str(raw.entries_paused),
    errors: Array.isArray(raw.errors) ? raw.errors.map(String) : [],
    setups: rows(raw.setups).map((x) => ({
      key: String(x.key ?? ''),
      title: String(x.title ?? x.key ?? ''),
      sleeve: String(x.sleeve ?? ''),
      status: x.status === 'tested' ? 'tested' : x.status === 'practice' ? 'practice' : 'forward_test',
      trial: String(x.trial ?? ''),
      enabled: x.enabled === true,
      decides: String(x.decides ?? ''),
      openTrades: Array.isArray(x.open_trades) ? x.open_trades.map(String) : [],
      forwardTrades: num(x.forward_trades, 0),
      forwardResultUsd: String(x.forward_result_usd ?? '0.00'),
      lossCapUsd: str(x.loss_cap_usd),
      switchedOff: str(x.switched_off),
      lastDecision: decision(x.last_decision),
    })),
  };
}

/**
 * An open trade as if it were closed at the latest price: the price move since the fill, minus
 * the fee paid on entry and the fee a close would cost (`feeRate` per side of the traded value).
 */
export function markTrade(t: PracticeTrade, last: LastPrice, feeRate: number): TradeMark | null {
  const fill = Number(t.fill);
  const size = Number(t.size);
  const fx = Number(t.fxToUsd) > 0 ? Number(t.fxToUsd) : 1;
  if (t.state !== 'open' || !(fill > 0) || !(size > 0)) return null;
  const side = t.direction === 'SELL' ? -1 : 1;
  const grossUsd = (last.price - fill) * size * side * fx;
  const feesUsd = feeRate * size * (fill + last.price) * fx;
  const stop = Number(t.stop);
  return {
    price: last.price,
    at: last.at,
    grossUsd,
    feesUsd,
    netUsd: grossUsd - feesUsd,
    toStop: stop > 0 ? ((last.price - stop) * side) / last.price : null,
  };
}

function practiceSleeve(id: PracticeSleeve['id'], rulesRel: string): PracticeSleeve {
  const rules = yaml(rulesRel);
  const start = num(rules.sleeve_start_usd, DEFAULT_START);
  const journalRel = typeof rules.journal === 'string' && /^runs\/practice\/[a-z0-9_]+\.jsonl$/.test(rules.journal) ? rules.journal : null;
  const { events, chainOk } = journalRel ? readJournal(repoPath(journalRel)) : { events: [], chainOk: true };
  const curve: EquityPoint[] = [];
  let equity = start;
  if (events.length) curve.push({ t: Date.parse(events[0].at) / 1000, v: start });
  for (const e of events) {
    if (e.event !== 'CLOSE') continue;
    equity += num(e.payload.pnl_usd, 0);
    curve.push({ t: Date.parse(e.at) / 1000, v: equity });
  }
  const trades = tradesOf(events);
  const prices = latestPrices(id);
  const feeRate = num(rules.taker_fee_rate, 0);
  let openPnl = 0;
  let markedAt: number | null = null;
  for (const t of trades) {
    const last = prices.get(t.epic);
    t.mark = last ? markTrade(t, last, feeRate) : null;
    if (!t.mark) continue;
    openPnl += t.mark.netUsd;
    markedAt = Math.max(markedAt ?? 0, t.mark.at);
  }
  return {
    id,
    start,
    equity: equity + openPnl,
    realized: equity,
    openPnl,
    markedAt,
    floor: start * num(rules.floor_fraction, 0.9),
    riskFraction: num(rules.risk_fraction, 0.01),
    maxOpenTrades: num(rules.max_open_trades, 2),
    curve,
    trades,
    lastEventAt: events.length ? events[events.length - 1].at : null,
    chainOk,
  };
}

/** The stocks book's portfolio (config.PAPER_CONFIG) and the capital and limits of its policy. */
export function stocksSleeve(): MissionState['stocks'] {
  const paper = yaml(PAPER_CONFIG);
  const ref = String(paper.policy_file ?? '').split('#')[0].trim();
  const policy = /^policy\/[a-z0-9_]+\.yaml$/.test(ref) ? yaml(`configs/${ref}`) : {};
  const exposure = (policy.exposure ?? {}) as Record<string, unknown>;
  return {
    start: num(policy.initial_trading_capital, DEFAULT_START),
    portfolioId: typeof paper.portfolio_id === 'string' ? paper.portfolio_id.split('#')[0].trim() : null,
    maxOpenPositions: num(exposure.max_open_positions, 2),
    maxEntriesPerSession: num(exposure.max_new_entries_per_session, 2),
  };
}

function target(): number {
  try {
    const sheet = JSON.parse(readFileSync(repoPath('.desk', 'sheet.json'), 'utf8')) as { goal?: { target?: unknown } };
    const t = num(sheet.goal?.target, DEFAULT_TARGET);
    return t > 0 ? t : DEFAULT_TARGET;
  } catch {
    return DEFAULT_TARGET;
  }
}

export function missionState(): MissionState {
  return {
    target: target(),
    stocks: stocksSleeve(),
    practice: PRACTICE.map(({ id, rules }) => practiceSleeve(id, rules)),
    scan: readScan(repoPath('runs', 'practice', 'scan.json')),
    trader: readTrader(repoPath('runs', 'practice', 'trader.json')),
  };
}
