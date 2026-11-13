/**
 * Figures derived from the bot's rows. Exact decimal arithmetic throughout (decimal.js); results
 * are decimal strings. Formulas mirror the bot's own code where one exists:
 *  - loss windows and drawdown: src/trader/risk/loss_limits.py (evaluate)
 *  - average cost: positions.cost_basis / quantity (cost_basis is a total)
 */
import Decimal from 'decimal.js';
import type {
  Dec,
  DecisionRow,
  EquityRow,
  IntentRow,
  Iso,
  LossWindowRow,
  Tables,
} from './schema';

Decimal.set({ precision: 40, rounding: Decimal.ROUND_HALF_EVEN });

export const D = (v: Dec | null | undefined): Decimal => new Decimal(v ?? 0);
const str = (d: Decimal): Dec => {
  const s = d.toFixed();
  return s === '-0' ? '0' : s;
};

export function sumDec(values: (Dec | null | undefined)[]): Dec {
  return str(values.reduce<Decimal>((acc, v) => acc.plus(D(v)), new Decimal(0)));
}

export function latestBy<T>(rows: T[], ts: (r: T) => string | null | undefined): T | null {
  let best: T | null = null;
  let bestTs = '';
  for (const r of rows) {
    const t = ts(r) ?? '';
    if (best === null || t >= bestTs) {
      best = r;
      bestTs = t;
    }
  }
  return best;
}

export const reasonsOf = (d: DecisionRow): string[] =>
  d.reasons ? d.reasons.split(',').map((s) => s.trim()).filter(Boolean) : [];

/** Policy values the console needs for meters (configs/policy/base.yaml). */
export interface PolicyLimits {
  initialCapital: Dec;
  dailyLossFraction: Dec;
  weeklyLossFraction: Dec;
  maxDrawdownFraction: Dec;
  capitalFloor: Dec;
  minCashReserveFraction: Dec;
  minCashReserveAbsolute: Dec;
  maxGrossExposureFraction: Dec;
  maxSymbolExposureFraction: Dec;
  maxOpenPositions: number;
  maxNewEntriesPerSession: number;
  riskPerTradeFraction: Dec;
}

export const DEFAULT_LIMITS: PolicyLimits = {
  initialCapital: '50',
  dailyLossFraction: '0.01',
  weeklyLossFraction: '0.025',
  maxDrawdownFraction: '0.10',
  capitalFloor: '45',
  minCashReserveFraction: '0.60',
  minCashReserveAbsolute: '30',
  maxGrossExposureFraction: '0.40',
  maxSymbolExposureFraction: '0.20',
  maxOpenPositions: 2,
  maxNewEntriesPerSession: 2,
  riskPerTradeFraction: '0.0025',
};

// ---------------------------------------------------------------------------------------------
// Money

export interface MoneySummary {
  initialCapital: Dec;
  externalFlows: Dec;
  /** Ledger cash: sum of every ledger cash_delta. */
  cash: Dec;
  /** Cost basis of open positions. */
  invested: Dec;
  /** Cash held back for active entry reservations (notional + fees). */
  reserved: Dec;
  /** Required cash reserve: max(fraction x equity, absolute). */
  requiredReserve: Dec;
  /** Cash that is neither reserved for orders nor inside the required reserve (never negative). */
  freeCash: Dec;
  realizedGross: Dec;
  realizedNet: Dec;
  /** Sum of fees on fills. */
  fees: Dec;
  /** Latest conservative equity snapshot (bid-marked, net of estimated exit fees). */
  equity: Dec | null;
  equityAt: Iso | null;
  highWaterMark: Dec | null;
  /** Cash-flow-adjusted equity: equity - (flows - initial capital). */
  adjustedEquity: Dec | null;
  drawdownFraction: Dec | null;
  /** adjusted equity - capital floor. */
  floorDistance: Dec | null;
  /** equity - external flows (whole-project P&L). */
  totalPnl: Dec | null;
  openPositions: number;
}

export function latestEquity(rows: EquityRow[], scope: 'PORTFOLIO' | 'ACCOUNT' = 'PORTFOLIO'): EquityRow | null {
  const scoped = rows.filter((r) => r.scope === scope);
  return latestBy(scoped.length ? scoped : rows, (r) => `${r.ts}|${String(r.id).padStart(12, '0')}`);
}

export function moneySummary(t: Tables, limits: PolicyLimits = DEFAULT_LIMITS): MoneySummary {
  const initialCapital = t.portfolios[0]?.initial_capital ?? limits.initialCapital;
  const flows = t.ledger_entries.filter((l) => l.kind.startsWith('FLOW_'));
  const externalFlows = flows.length ? sumDec(flows.map((l) => l.cash_delta)) : initialCapital;
  const cash = sumDec(t.ledger_entries.map((l) => l.cash_delta));
  const open = t.positions.filter((p) => D(p.quantity).gt(0));
  const invested = sumDec(open.map((p) => p.cost_basis));
  const reserved = sumDec(
    t.risk_reservations
      .filter((r) => r.state === 'ACTIVE')
      .map((r) => {
        const remaining = D(r.reserved_qty).minus(D(r.consumed_qty)).minus(D(r.released_qty));
        const frac = D(r.reserved_qty).gt(0) ? remaining.div(D(r.reserved_qty)) : new Decimal(0);
        return str(D(r.notional_usd).plus(D(r.fees_usd)).times(Decimal.max(frac, 0)));
      }),
  );
  const eq = latestEquity(t.equity_snapshots);
  const equity = eq?.equity ?? null;
  const equityBase = equity !== null ? D(equity) : D(cash).plus(D(invested));
  const requiredReserve = str(
    Decimal.max(equityBase.times(D(limits.minCashReserveFraction)), D(limits.minCashReserveAbsolute)),
  );
  const freeCash = str(Decimal.max(D(cash).minus(D(reserved)).minus(D(requiredReserve)), 0));
  let adjusted: Decimal | null = null;
  let drawdown: Dec | null = null;
  let floorDistance: Dec | null = null;
  let totalPnl: Dec | null = null;
  if (eq) {
    adjusted = D(eq.equity).minus(D(eq.cum_external_flows).minus(D(initialCapital)));
    const hwm = D(eq.high_water_mark);
    drawdown = hwm.gt(0) ? str(Decimal.max(hwm.minus(adjusted).div(hwm), 0)) : '0';
    floorDistance = str(adjusted.minus(D(limits.capitalFloor)));
    totalPnl = str(D(eq.equity).minus(D(eq.cum_external_flows)));
  }
  return {
    initialCapital,
    externalFlows,
    cash,
    invested,
    reserved,
    requiredReserve,
    freeCash,
    realizedGross: sumDec(t.positions.map((p) => p.realized_gross)),
    realizedNet: sumDec(t.positions.map((p) => p.realized_net)),
    fees: sumDec(t.fills.map((f) => f.fee)),
    equity,
    equityAt: eq?.ts ?? null,
    highWaterMark: eq?.high_water_mark ?? null,
    adjustedEquity: adjusted ? str(adjusted) : null,
    drawdownFraction: drawdown,
    floorDistance,
    totalPnl,
    openPositions: open.length,
  };
}

// ---------------------------------------------------------------------------------------------
// Loss windows (risk/loss_limits.py: loss = start_equity + (flows - start_flows) - equity)

export interface LossWindowUsage {
  kind: string;
  scope: 'PORTFOLIO' | 'ACCOUNT';
  windowKey: string;
  startEquity: Dec;
  lossUsd: Dec;
  limitUsd: Dec;
  /** loss / limit, clamped at 0 (a gain uses none of the limit). */
  usedFraction: number;
  breachedAt: Iso | null;
}

export const LOSS_WINDOW_ORDER = ['NY_SESSION', 'NY_WEEK', 'UTC_DAY', 'UTC_WEEK'] as const;

export function lossWindowUsage(t: Tables, limits: PolicyLimits = DEFAULT_LIMITS): LossWindowUsage[] {
  const out: LossWindowUsage[] = [];
  for (const kind of LOSS_WINDOW_ORDER) {
    const w: LossWindowRow | null = latestBy(
      t.loss_windows.filter((r) => r.kind === kind),
      (r) => r.start_time,
    );
    if (!w) continue;
    const snaps = t.equity_snapshots.filter((s) => s.scope === w.scope && s.scope_id === w.scope_id);
    const eq = latestBy(snaps, (s) => `${s.ts}|${String(s.id).padStart(12, '0')}`);
    const equity = eq ? D(eq.equity) : D(w.start_equity);
    const flows = eq ? D(eq.cum_external_flows) : D(w.start_flows);
    const loss = D(w.start_equity).plus(flows.minus(D(w.start_flows))).minus(equity);
    const frac = kind === 'NY_SESSION' || kind === 'UTC_DAY' ? limits.dailyLossFraction : limits.weeklyLossFraction;
    const limit = D(frac).times(D(w.start_equity));
    out.push({
      kind,
      scope: w.scope,
      windowKey: w.window_key,
      startEquity: w.start_equity,
      lossUsd: str(loss),
      limitUsd: str(limit),
      usedFraction: limit.gt(0) ? Math.max(0, loss.div(limit).toNumber()) : 0,
      breachedAt: w.breached_at,
    });
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Decisions

export interface Funnel {
  candidates: number;
  approved: number;
  submitted: number;
  filled: number;
  exited: number;
}

const isEntry = (i: IntentRow) => i.purpose === 'ENTRY';

export function funnel(t: Tables): Funnel {
  const entries = t.order_intents.filter(isEntry);
  const sentIntents = new Set(
    t.submission_outbox.filter((o) => o.status === 'DONE' || o.status === 'CLAIMED').map((o) => o.intent_id),
  );
  const submitted = entries.filter((i) => sentIntents.has(i.intent_id) || i.broker_order_id !== null).length;
  const filled = entries.filter((i) => D(i.cum_filled_qty).gt(0)).length;
  const exitedParents = new Set(
    t.order_intents
      .filter((i) => !isEntry(i) && i.parent_intent_id && D(i.cum_filled_qty).gt(0))
      .map((i) => i.parent_intent_id as string),
  );
  return {
    candidates: t.candidates.length,
    approved: t.risk_decisions.filter((d) => d.approved).length,
    submitted,
    filled,
    exited: entries.filter((i) => exitedParents.has(i.intent_id)).length,
  };
}

export function rejectionCounts(decisions: DecisionRow[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const d of decisions) {
    if (d.approved) continue;
    for (const r of reasonsOf(d)) counts.set(r, (counts.get(r) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

// ---------------------------------------------------------------------------------------------
// The demonstrator's own report (runs/demo/<scenario>/report.json), recomputed from rows.

export interface RunTotals {
  decisions: number;
  approved: number;
  rejection_reasons: Record<string, number>;
  orders: number;
  order_states: Record<string, number>;
  fills: number;
  gross_trading_pnl: Dec;
  fees: Dec;
  net_trading_pnl: Dec;
  ending_cash: Dec;
}

export function runTotals(t: Tables): RunTotals {
  const states: Record<string, number> = {};
  for (const i of t.order_intents) states[i.state] = (states[i.state] ?? 0) + 1;
  return {
    decisions: t.risk_decisions.length,
    approved: t.risk_decisions.filter((d) => d.approved).length,
    rejection_reasons: Object.fromEntries(rejectionCounts(t.risk_decisions)),
    orders: t.order_intents.length,
    order_states: states,
    fills: t.fills.length,
    gross_trading_pnl: sumDec(t.positions.map((p) => p.realized_gross)),
    fees: sumDec(t.fills.map((f) => f.fee)),
    net_trading_pnl: sumDec(t.positions.map((p) => p.realized_net)),
    ending_cash: sumDec(t.ledger_entries.map((l) => l.cash_delta)),
  };
}

// ---------------------------------------------------------------------------------------------
// Positions

export interface PositionView {
  instrumentId: string;
  symbol: string;
  quantity: Dec;
  costBasis: Dec;
  avgCost: Dec | null;
  lastPrice: Dec | null;
  lastPriceAt: Iso | null;
  unrealized: Dec | null;
  stop: Dec | null;
  target: Dec | null;
  maxHoldUntil: Iso | null;
  openedAt: Iso | null;
}

export const symbolOf = (instrumentId: string) => instrumentId.split(':').pop() ?? instrumentId;

/** Latest known price per instrument: newest tick (replay) or bar close (live). */
export function lastPrices(t: Tables): Map<string, { price: Dec; at: Iso; source: 'tick' | 'bar' }> {
  const out = new Map<string, { price: Dec; at: Iso; source: 'tick' | 'bar' }>();
  for (const b of t.bars) {
    const cur = out.get(b.instrument_id);
    if (!cur || b.end > cur.at) out.set(b.instrument_id, { price: b.close, at: b.end, source: 'bar' });
  }
  for (const k of t.ticks) {
    const p = k.payload;
    const price = p.price ?? (p.bid && p.ask ? str(D(p.bid).plus(D(p.ask)).div(2)) : null);
    if (!price) continue;
    const cur = out.get(k.instrument_id);
    if (!cur || k.event_time >= cur.at) out.set(k.instrument_id, { price, at: k.event_time, source: 'tick' });
  }
  return out;
}

export function positionViews(t: Tables): PositionView[] {
  const prices = lastPrices(t);
  return t.positions
    .filter((p) => D(p.quantity).gt(0))
    .map((p) => {
      const plan = latestBy(
        t.exit_plans.filter((e) => e.instrument_id === p.instrument_id && e.active),
        (e) => e.created_at,
      );
      const last = prices.get(p.instrument_id) ?? null;
      const qty = D(p.quantity);
      const unrealized = last
        ? str(qty.times(D(last.price)).minus(D(p.cost_basis)).minus(D(p.open_entry_fees)))
        : null;
      return {
        instrumentId: p.instrument_id,
        symbol: symbolOf(p.instrument_id),
        quantity: p.quantity,
        costBasis: p.cost_basis,
        avgCost: qty.gt(0) ? str(D(p.cost_basis).div(qty)) : null,
        lastPrice: last?.price ?? null,
        lastPriceAt: last?.at ?? null,
        unrealized,
        stop: plan?.stop_price ?? null,
        target: plan?.target_price ?? null,
        maxHoldUntil: plan?.max_hold_until ?? null,
        openedAt: p.opened_at,
      };
    });
}

// ---------------------------------------------------------------------------------------------
// Decision trail: everything that happened because of one candidate.

export function decisionTrail(t: Tables, candidateId: string) {
  const candidate = t.candidates.find((c) => c.candidate_id === candidateId) ?? null;
  const decision = t.risk_decisions.find((d) => d.candidate_id === candidateId) ?? null;
  const entry = t.order_intents.find((i) => i.candidate_id === candidateId && isEntry(i)) ?? null;
  const exits = entry ? t.order_intents.filter((i) => i.parent_intent_id === entry.intent_id) : [];
  const intents = [entry, ...exits].filter(Boolean) as IntentRow[];
  const ids = new Set(intents.map((i) => i.intent_id));
  const clientIds = new Set(intents.map((i) => i.client_order_id));
  return {
    candidate,
    decision,
    entry,
    exits,
    outbox: t.submission_outbox.filter((o) => ids.has(o.intent_id)),
    brokerEvents: t.broker_order_events
      .filter((e) => clientIds.has(e.client_order_id))
      .sort((a, b) => a.event_time.localeCompare(b.event_time) || a.id - b.id),
    fills: t.fills
      .filter((f) => (f.intent_id && ids.has(f.intent_id)) || clientIds.has(f.client_order_id))
      .sort((a, b) => a.exec_time.localeCompare(b.exec_time)),
    exitPlan: entry ? (t.exit_plans.find((e) => e.entry_intent_id === entry.intent_id) ?? null) : null,
    reservation: entry ? (t.risk_reservations.find((r) => r.intent_id === entry.intent_id) ?? null) : null,
  };
}
export type DecisionTrail = ReturnType<typeof decisionTrail>;
