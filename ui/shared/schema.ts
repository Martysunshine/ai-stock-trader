/**
 * The bot's runtime tables as the console sees them (src/trader/storage/schema.py).
 *
 * Both sources (live PostgreSQL and SQLite replay) normalise raw rows through `normaliseRow`, so
 * the client never has to know where a row came from:
 *  - decimals stay exact strings ("50", "0.098726429"), never floats;
 *  - timestamps become ISO-8601 UTC strings ending in "Z";
 *  - `*_json` columns are parsed and renamed without the suffix (payload_json -> payload);
 *  - SQLite 0/1 booleans become true/false.
 */

export type Dec = string;
export type Iso = string;

interface TableSpec {
  /** Primary-key columns; joined with "|" to form the row key. */
  key: readonly string[];
  ts?: readonly string[];
  dates?: readonly string[];
  dec?: readonly string[];
  bool?: readonly string[];
  json?: readonly string[];
}

export const TABLES = {
  portfolios: { key: ['portfolio_id'], ts: ['created_at'], dec: ['initial_capital'] },
  accounts: { key: ['account_id'], ts: ['created_at'], dec: ['allocated_capital', 'broker_cash_baseline'] },
  instruments: { key: ['instrument_id'], ts: ['updated_at'], json: ['spec_json'] },
  policy_versions: { key: ['policy_hash'], ts: ['first_seen_at'], json: ['canonical_json'] },
  candidates: { key: ['candidate_id'], ts: ['decision_time'], json: ['payload_json'] },
  risk_decisions: { key: ['decision_id'], ts: ['decided_at'], bool: ['approved'], json: ['payload_json'] },
  order_intents: {
    key: ['intent_id'],
    ts: ['valid_until', 'entry_ttl_until', 'created_at', 'updated_at'],
    dec: ['quantity', 'limit_price', 'cum_filled_qty', 'final_cum_qty'],
    json: ['plan_json'],
  },
  submission_outbox: { key: ['outbox_id'], ts: ['created_at', 'updated_at'] },
  broker_order_events: {
    key: ['id'],
    ts: ['event_time', 'received_at'],
    dec: ['cum_filled_qty'],
    json: ['raw_json'],
  },
  fills: {
    key: ['id'],
    ts: ['exec_time', 'receipt_time'],
    dec: ['quantity', 'price', 'fee', 'realized_gross', 'realized_net'],
  },
  positions: {
    key: ['account_id', 'instrument_id'],
    ts: ['opened_at', 'updated_at'],
    dec: ['quantity', 'cost_basis', 'open_entry_fees', 'realized_gross', 'realized_net'],
  },
  exit_plans: {
    key: ['entry_intent_id'],
    ts: ['max_hold_until', 'created_at'],
    dec: ['stop_price', 'target_price', 'planned_loss_usd'],
    bool: ['active'],
  },
  ledger_entries: {
    key: ['id'],
    ts: ['entry_time'],
    dec: ['cash_delta', 'quantity_delta', 'price', 'fee', 'realized_net'],
  },
  cash_lots: { key: ['id'], dates: ['trade_date', 'settles_on'], dec: ['amount'] },
  fee_accruals: { key: ['id'], ts: ['updated_at'], dec: ['estimated', 'final'] },
  risk_reservations: {
    key: ['reservation_id'],
    ts: ['created_at', 'updated_at'],
    dec: ['reserved_qty', 'consumed_qty', 'released_qty', 'notional_usd', 'fees_usd', 'planned_loss_usd'],
  },
  equity_snapshots: {
    key: ['id'],
    ts: ['ts'],
    dec: ['equity', 'cum_external_flows', 'high_water_mark'],
  },
  loss_windows: {
    key: ['scope', 'scope_id', 'kind', 'window_key'],
    ts: ['start_time', 'breached_at'],
    dec: ['start_equity', 'start_flows'],
  },
  pause_state: { key: ['flag_id'], ts: ['raised_at', 'cleared_at'], bool: ['active'] },
  owner_reviews: { key: ['review_id'], ts: ['reviewed_at'], json: ['figures_json'] },
  alerts: { key: ['id'], ts: ['raised_at'] },
  reconciliation_runs: { key: ['run_id'], ts: ['started_at', 'finished_at'], json: ['summary_json'] },
  bars: {
    key: ['instrument_id', 'feed', 'minutes', 'start'],
    ts: ['start', 'end', 'available_time'],
    dec: ['open', 'high', 'low', 'close', 'volume', 'vwap'],
  },
  /** The `events` table (quotes and trades). Written by the replay runner only. */
  ticks: { key: ['id'], ts: ['event_time', 'receipt_time'], json: ['payload_json'] },
} as const satisfies Record<string, TableSpec>;

export type TableName = keyof typeof TABLES;
export const TABLE_NAMES = Object.keys(TABLES) as TableName[];

/** Source table for each console table (only `ticks` differs). */
export function sqlTable(t: TableName): string {
  return t === 'ticks' ? 'events' : t;
}

export function rowKey(table: TableName, row: Record<string, unknown>): string {
  return TABLES[table].key.map((k) => String(row[k])).join('|');
}

/** "50.000000000000000000" -> "50", "-0.0100" -> "-0.01", "-0" -> "0". */
export function normDec(v: unknown): Dec | null {
  if (v === null || v === undefined) return null;
  let s = typeof v === 'string' ? v.trim() : String(v);
  if (s.includes('e') || s.includes('E')) {
    // Only reachable if a driver hands back a float; keep it exact-looking and flag it loudly.
    s = Number(s).toFixed(18);
  }
  if (s.includes('.')) s = s.replace(/0+$/, '').replace(/\.$/, '');
  if (s === '-0' || s === '') s = '0';
  return s;
}

/** Date | "2026-11-27 14:29:00.120000" (SQLite, naive UTC) | ISO string -> ISO UTC "...Z". */
export function normTs(v: unknown): Iso | null {
  if (v === null || v === undefined) return null;
  if (v instanceof Date) return v.toISOString();
  const s = String(v).trim();
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s)) {
    // Naive timestamp: the bot stores UTC.
    const [d, t] = s.split(/[ T]/);
    const [hms, frac = ''] = t.split('.');
    return `${d}T${hms}.${(frac + '000').slice(0, 3)}Z`;
  }
  const parsed = new Date(s);
  if (Number.isNaN(parsed.getTime())) return s;
  return parsed.toISOString();
}

function normDate(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  return String(v).slice(0, 10);
}

export type Row = Record<string, unknown>;

export function normaliseRow(table: TableName, raw: Row): Row {
  const spec: TableSpec = TABLES[table];
  const out: Row = { ...raw };
  for (const c of spec.ts ?? []) if (c in out) out[c] = normTs(out[c]);
  for (const c of spec.dates ?? []) if (c in out) out[c] = normDate(out[c]);
  for (const c of spec.dec ?? []) if (c in out) out[c] = normDec(out[c]);
  for (const c of spec.bool ?? []) if (c in out) out[c] = out[c] === null ? null : Boolean(out[c]);
  for (const c of spec.json ?? []) {
    if (!(c in out)) continue;
    const text = out[c];
    delete out[c];
    const name = c.replace(/_json$/, '');
    try {
      out[name] = typeof text === 'string' ? JSON.parse(text) : text;
    } catch {
      out[name] = { unparsed: String(text) };
    }
  }
  for (const [k, v] of Object.entries(out)) if (typeof v === 'bigint') out[k] = Number(v);
  return out;
}

// ---------------------------------------------------------------------------------------------
// Row types for the columns the console reads. Optional JSON fields are typed loosely on purpose:
// the bot owns their shape and may add keys.

export interface PortfolioRow { portfolio_id: string; base_currency: string; initial_capital: Dec; version: number; created_at: Iso }
export interface AccountRow {
  account_id: string; portfolio_id: string; alias: string; track: string; broker: string; mode: string;
  currency: string; rule_profile: string; legal_type: string; allocated_capital: Dec;
  broker_cash_baseline: Dec | null; version: number; created_at: Iso;
}
export interface InstrumentSpec {
  tradable?: boolean; evidence?: string; fractionable?: boolean; min_notional?: Dec; price_tick?: Dec;
  identity?: { symbol?: string; venue_mic?: string; broker?: string; currency?: string };
  source?: string; as_of?: string;
}
export interface InstrumentRow { instrument_id: string; track: string; product_type: string; spec_hash: string; spec: InstrumentSpec; updated_at: Iso }
export interface PolicyVersionRow { policy_hash: string; canonical: Record<string, unknown>; source: string; first_seen_at: Iso }

export interface CandidatePayload {
  bar_end?: Iso; direction?: string; reference_price?: Dec; stop_price?: Dec; target_price?: Dec;
  snapshot_hash?: string; spec_hash?: string; strategy_version?: number; expires_at?: Iso;
  max_hold_minutes?: number; entry_limit_offset_bps?: Dec; entry_ttl_seconds?: number; feed?: string; track?: string;
}
export interface CandidateRow { candidate_id: string; strategy_id: string; instrument_id: string; decision_time: Iso; payload: CandidatePayload }

export interface DecisionPayload {
  reasons?: string[]; details?: string[]; model_hash?: string; evidence?: string; quantity?: Dec;
  entry_limit_price?: Dec | null; notional_usd?: Dec; planned_loss_usd?: Dec; estimated_costs_usd?: Dec;
  expected_net_usd?: Dec; effective_sizing_equity_usd?: Dec; binding_constraint?: string; intent_id?: string | null;
}
export interface DecisionRow {
  decision_id: string; seq: number; candidate_id: string; portfolio_id: string; account_id: string;
  approved: boolean; reasons: string; decided_at: Iso; policy_hash: string; model_hash: string; payload: DecisionPayload;
}

export interface IntentPlan {
  intent?: { reason?: string; [k: string]: unknown };
  reserved_cash_usd?: Dec; reserved_risk_usd?: Dec; quote_time?: Iso; valid_until?: Iso; exit_method?: string;
  [k: string]: unknown;
}
export interface IntentRow {
  intent_id: string; seq: number; portfolio_id: string; account_id: string; client_order_id: string;
  instrument_id: string; track: string; side: 'BUY' | 'SELL'; purpose: string; order_type: string;
  quantity: Dec; limit_price: Dec | null; time_in_force: string; state: string; decision_id: string | null;
  candidate_id: string | null; parent_intent_id: string | null; plan: IntentPlan; valid_until: Iso;
  entry_ttl_until: Iso | null; broker_order_id: string | null; cum_filled_qty: Dec; final_cum_qty: Dec | null;
  created_at: Iso; updated_at: Iso;
}
export interface OutboxRow { outbox_id: string; intent_id: string; status: string; attempts: number; created_at: Iso; updated_at: Iso }
export interface BrokerEventRow {
  id: number; broker: string; account_id: string; event_id: string; broker_order_id: string; client_order_id: string;
  status: string; cum_filled_qty: Dec; broker_version: number; event_time: Iso; received_at: Iso; disposition: string;
}
export interface FillRow {
  id: number; broker: string; account_id: string; execution_id: string; intent_id: string | null;
  client_order_id: string; instrument_id: string; side: 'BUY' | 'SELL'; quantity: Dec; price: Dec; currency: string;
  fee: Dec; fee_status: string; exec_time: Iso; receipt_time: Iso; realized_gross: Dec | null; realized_net: Dec | null;
}
export interface PositionRow {
  account_id: string; instrument_id: string; owner_track: string; currency: string; quantity: Dec; cost_basis: Dec;
  open_entry_fees: Dec; realized_gross: Dec; realized_net: Dec; opened_at: Iso | null; updated_at: Iso;
}
export interface ExitPlanRow {
  entry_intent_id: string; account_id: string; instrument_id: string; stop_price: Dec; target_price: Dec | null;
  max_hold_until: Iso; planned_loss_usd: Dec; active: boolean; created_at: Iso;
}
export interface LedgerRow {
  id: number; seq: number; account_id: string; entry_time: Iso; kind: string; currency: string; cash_delta: Dec;
  instrument_id: string | null; quantity_delta: Dec | null; price: Dec | null; fee: Dec | null; realized_net: Dec | null; ref: string;
}
export interface CashLotRow { id: number; account_id: string; currency: string; amount: Dec; trade_date: string; settles_on: string; source: string }
export interface FeeAccrualRow { id: number; broker: string; account_id: string; execution_id: string; estimated: Dec; final: Dec | null; status: string; updated_at: Iso }
export interface ReservationRow {
  reservation_id: string; account_id: string; intent_id: string; kind: string; state: string; instrument_id: string;
  reserved_qty: Dec; consumed_qty: Dec; released_qty: Dec; notional_usd: Dec; fees_usd: Dec; planned_loss_usd: Dec;
  created_at: Iso; updated_at: Iso;
}
export interface EquityRow { id: number; scope: 'PORTFOLIO' | 'ACCOUNT'; scope_id: string; ts: Iso; equity: Dec; cum_external_flows: Dec; high_water_mark: Dec }
export interface LossWindowRow {
  scope: 'PORTFOLIO' | 'ACCOUNT'; scope_id: string; kind: string; window_key: string; start_time: Iso;
  start_equity: Dec; start_flows: Dec; breached_at: Iso | null;
}
export interface PauseRow {
  flag_id: string; scope: string; scope_id: string; category: string; cause: string; raised_at: Iso; details: string;
  active: boolean; cleared_at: Iso | null; cleared_by: string | null; clear_evidence: string | null;
}
export interface OwnerReviewRow { review_id: string; flag_id: string; reviewer: string; note: string; reviewed_at: Iso }
export interface AlertRow { id: number; raised_at: Iso; severity: string; code: string; account_id: string | null; message: string }
export interface ReconRow {
  run_id: string; account_id: string; started_at: Iso; finished_at: Iso; status: 'PASSED' | 'FAILED';
  summary: { notes?: string[]; problems?: string[] };
}
export interface BarRow {
  instrument_id: string; feed: string; minutes: number; start: Iso; end: Iso; available_time: Iso;
  open: Dec; high: Dec; low: Dec; close: Dec; volume: Dec; trade_count: number | null; vwap: Dec | null;
}
export interface TickPayload { kind?: string; price?: Dec; size?: Dec; bid?: Dec; ask?: Dec; bid_size?: Dec; ask_size?: Dec }
export interface TickRow { id: number; feed: string; instrument_id: string; kind: string; sequence: number; event_time: Iso; receipt_time: Iso; payload: TickPayload }

export interface RowTypes {
  portfolios: PortfolioRow; accounts: AccountRow; instruments: InstrumentRow; policy_versions: PolicyVersionRow;
  candidates: CandidateRow; risk_decisions: DecisionRow; order_intents: IntentRow; submission_outbox: OutboxRow;
  broker_order_events: BrokerEventRow; fills: FillRow; positions: PositionRow; exit_plans: ExitPlanRow;
  ledger_entries: LedgerRow; cash_lots: CashLotRow; fee_accruals: FeeAccrualRow; risk_reservations: ReservationRow;
  equity_snapshots: EquityRow; loss_windows: LossWindowRow; pause_state: PauseRow; owner_reviews: OwnerReviewRow;
  alerts: AlertRow; reconciliation_runs: ReconRow; bars: BarRow; ticks: TickRow;
}

export type Tables = { [K in TableName]: RowTypes[K][] };

export function emptyTables(): Tables {
  return Object.fromEntries(TABLE_NAMES.map((t) => [t, []])) as unknown as Tables;
}
