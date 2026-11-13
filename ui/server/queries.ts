/**
 * Every SQL statement the console sends to PostgreSQL. SELECT/WITH only: tests/sql-guard.test.ts
 * fails the build if a write or DDL keyword appears here. Table names are interpolated only from
 * the fixed whitelist in shared/schema.ts (never from a request).
 */
import { sqlTable, type TableName } from '../shared/schema';

/** Tables paged by their auto-increment `id`. */
export const APPEND_BY_ID = [
  'broker_order_events',
  'fills',
  'ledger_entries',
  'equity_snapshots',
  'alerts',
  'cash_lots',
  'ticks',
] as const satisfies readonly TableName[];

/** Small tables whose rows change in place: re-read every poll and diffed by content. */
export const SMALL_MUTABLE = [
  'portfolios',
  'accounts',
  'instruments',
  'policy_versions',
  'submission_outbox',
  'positions',
  'exit_plans',
  'fee_accruals',
  'risk_reservations',
  'loss_windows',
  'pause_state',
  'owner_reviews',
] as const satisfies readonly TableName[];

const TERMINAL_STATES = ['FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'NOT_ACCEPTED', 'NOT_SENT'];

export const Q = {
  schemaReady: `SELECT to_regclass('public.risk_decisions') IS NOT NULL AS ready`,

  latestById: (t: TableName) =>
    `SELECT * FROM (SELECT * FROM ${sqlTable(t)} ORDER BY id DESC LIMIT $1) recent ORDER BY id`,
  afterId: (t: TableName) => `SELECT * FROM ${sqlTable(t)} WHERE id > $1 ORDER BY id LIMIT $2`,

  latestDecisions: `SELECT * FROM (SELECT * FROM risk_decisions ORDER BY seq DESC LIMIT $1) recent ORDER BY seq`,
  decisionsAfter: `SELECT * FROM risk_decisions WHERE seq > $1 ORDER BY seq LIMIT $2`,
  candidatesByIds: `SELECT * FROM candidates WHERE candidate_id = ANY($1::text[])`,

  intentsInitial: `SELECT * FROM order_intents
    WHERE seq > (SELECT coalesce(max(seq), 0) FROM order_intents) - $1
       OR state NOT IN (${TERMINAL_STATES.map((s) => `'${s}'`).join(', ')})
    ORDER BY seq`,
  intentsChanged: `SELECT * FROM order_intents WHERE seq > $1 OR updated_at >= $2 ORDER BY seq`,

  latestRecons: `SELECT * FROM (SELECT * FROM reconciliation_runs ORDER BY started_at DESC LIMIT $1) recent ORDER BY started_at`,
  reconsAfter: `SELECT * FROM reconciliation_runs WHERE started_at > $1 ORDER BY started_at LIMIT $2`,

  latestBars: `SELECT * FROM (SELECT * FROM bars ORDER BY available_time DESC LIMIT $1) recent ORDER BY available_time`,
  barsAfter: `SELECT * FROM bars WHERE available_time > $1 ORDER BY available_time LIMIT $2`,

  full: (t: TableName) => `SELECT * FROM ${sqlTable(t)} LIMIT 5000`,

  liveness: `SELECT
      (SELECT max(finished_at) FROM reconciliation_runs) AS last_recon,
      (SELECT max(available_time) FROM bars) AS last_bar,
      EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND classid = $1::oid AND objid = $2::oid AND objsubid = 1
      ) AS leader`,

  sessionReadOnly: `SHOW default_transaction_read_only`,

  /** Desk goal bar: the latest PORTFOLIO equity snapshot (paper), as exact text. */
  equityReady: `SELECT to_regclass('public.equity_snapshots') IS NOT NULL AS ready`,
  latestPortfolioEquity: `SELECT equity::text AS equity, ts, scope_id FROM equity_snapshots
    WHERE scope = 'PORTFOLIO' ORDER BY ts DESC, id DESC LIMIT 1`,
} as const;

/** Every statement in Q, expanded for every table it can be applied to (for the SQL guard test). */
export function allStatements(tables: readonly TableName[]): string[] {
  const out: string[] = [];
  for (const v of Object.values(Q)) {
    if (typeof v === 'string') out.push(v);
    else for (const t of tables) out.push(v(t));
  }
  return out;
}
