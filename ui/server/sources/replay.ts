/**
 * Replay source: a recorded demonstrator session (runs/demo/<scenario>/replay.db, the same schema
 * as PostgreSQL) streamed on a virtual clock through the same protocol as the live source.
 *
 * Append-only rows appear at their own timestamps. Rows the bot updates in place only keep their
 * final state in the database, so their earlier states are rebuilt from evidence in the same
 * database: an intent's state follows its broker events, outbox/reservation/pause rows appear at
 * creation and take their final values at `updated_at`/`cleared_at`, and positions are folded from
 * fills (the stored final row replaces the fold at the end). The whole source is labelled
 * SYNTHETIC REPLAY in the UI and never mixes with live data.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import Decimal from 'decimal.js';
import { normaliseRow, sqlTable, TABLE_NAMES, type Row, type TableName } from '../../shared/schema';
import type { ReplayControl, ReplayScenario, SourceStatus } from '../../shared/protocol';
import { repoPath } from '../config';
import { Source } from '../store';

interface Item {
  at: number;
  order: number;
  table: TableName;
  row: Row;
}

const BROKER_TO_INTENT_STATE: Record<string, string> = {
  ACCEPTED: 'ACKNOWLEDGED',
  PARTIALLY_FILLED: 'PARTIALLY_FILLED',
  FILLED: 'FILLED',
  PENDING_CANCEL: 'CANCEL_REQUESTED',
  CANCELED: 'CANCELED',
  EXPIRED: 'EXPIRED',
  REJECTED: 'REJECTED',
};

const ms = (iso: unknown) => Date.parse(String(iso));

export function listScenarios(): ReplayScenario[] {
  const out: ReplayScenario[] = [];
  const runs = repoPath('runs');
  const candidates: [string, string][] = [];
  const demo = path.join(runs, 'demo');
  if (existsSync(demo)) {
    for (const d of readdirSync(demo, { withFileTypes: true })) {
      if (d.isDirectory()) candidates.push([d.name, path.join(demo, d.name)]);
    }
  }
  if (existsSync(runs)) {
    for (const d of readdirSync(runs, { withFileTypes: true })) {
      if (d.isDirectory() && d.name.startsWith('dev-')) candidates.push([d.name, path.join(runs, d.name)]);
    }
  }
  for (const [id, dir] of candidates) {
    const db = path.join(dir, 'replay.db');
    if (!existsSync(db)) continue;
    let notes: string[] = [];
    try {
      notes = JSON.parse(readFileSync(path.join(dir, 'report.json'), 'utf8')).report?.notes ?? [];
    } catch {
      /* report.json is optional */
    }
    out.push({ id, path: db, notes });
  }
  return out;
}

function readReport(dbPath: string): { notes: string[]; watermark: string | null } {
  try {
    const r = JSON.parse(readFileSync(path.join(path.dirname(dbPath), 'report.json'), 'utf8')).report;
    return { notes: r?.notes ?? [], watermark: r?.watermark ?? null };
  } catch {
    return { notes: [], watermark: null };
  }
}

/** Load every console table from a replay database (read-only). */
export function loadReplayTables(dbPath: string): Record<TableName, Row[]> {
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    const out = {} as Record<TableName, Row[]>;
    for (const t of TABLE_NAMES) {
      try {
        out[t] = (db.prepare(`SELECT * FROM ${sqlTable(t)}`).all() as Row[]).map((r) => normaliseRow(t, r));
      } catch {
        out[t] = [];
      }
    }
    return out;
  } finally {
    db.close();
  }
}

/** Build the time-ordered list of row versions that a replay emits. */
export function buildTimeline(t: Record<TableName, Row[]>): Item[] {
  const items: Item[] = [];
  let order = 0;
  const push = (at: number, table: TableName, row: Row) => {
    if (Number.isFinite(at)) items.push({ at, order: order++, table, row });
  };

  const allTimes = [
    ...t.portfolios.map((r) => ms(r.created_at)),
    ...t.ledger_entries.map((r) => ms(r.entry_time)),
    ...t.equity_snapshots.map((r) => ms(r.ts)),
    ...t.ticks.map((r) => ms(r.receipt_time)),
  ].filter(Number.isFinite);
  const start = allTimes.length ? Math.min(...allTimes) : 0;

  for (const tbl of ['portfolios', 'accounts', 'instruments', 'policy_versions'] as const) {
    for (const r of t[tbl]) push(start, tbl, r);
  }
  for (const r of t.candidates) push(ms(r.decision_time), 'candidates', r);
  for (const r of t.risk_decisions) push(ms(r.decided_at), 'risk_decisions', r);
  for (const r of t.broker_order_events) push(ms(r.received_at), 'broker_order_events', r);
  for (const r of t.fills) push(ms(r.receipt_time), 'fills', r);
  for (const r of t.ledger_entries) push(ms(r.entry_time), 'ledger_entries', r);
  for (const r of t.equity_snapshots) push(ms(r.ts), 'equity_snapshots', r);
  for (const r of t.alerts) push(ms(r.raised_at), 'alerts', r);
  for (const r of t.reconciliation_runs) push(ms(r.finished_at), 'reconciliation_runs', r);
  for (const r of t.bars) push(ms(r.available_time), 'bars', r);
  for (const r of t.ticks) push(ms(r.receipt_time), 'ticks', r);
  for (const r of t.owner_reviews) push(ms(r.reviewed_at), 'owner_reviews', r);
  for (const r of t.fee_accruals) push(ms(r.updated_at), 'fee_accruals', r);

  // Intents: created as PENDING_SUBMIT, then follow their broker events, then the stored final row.
  for (const i of t.order_intents) {
    const created = ms(i.created_at);
    push(created, 'order_intents', { ...i, state: 'PENDING_SUBMIT', cum_filled_qty: '0', broker_order_id: null, updated_at: i.created_at });
    const events = t.broker_order_events
      .filter((e) => e.client_order_id === i.client_order_id)
      .sort((a, b) => ms(a.received_at) - ms(b.received_at));
    for (const e of events) {
      const state = BROKER_TO_INTENT_STATE[String(e.status)];
      if (!state) continue;
      push(ms(e.received_at), 'order_intents', {
        ...i,
        state,
        cum_filled_qty: e.cum_filled_qty,
        broker_order_id: e.broker_order_id,
        updated_at: e.received_at,
      });
    }
    push(Math.max(ms(i.updated_at), created), 'order_intents', i);
  }

  const created = (tbl: 'submission_outbox' | 'risk_reservations', initial: (r: Row) => Row) => {
    for (const r of t[tbl]) {
      push(ms(r.created_at), tbl, initial(r));
      push(Math.max(ms(r.updated_at), ms(r.created_at)), tbl, r);
    }
  };
  created('submission_outbox', (r) => ({ ...r, status: 'PENDING', attempts: 0, updated_at: r.created_at }));
  created('risk_reservations', (r) => ({ ...r, state: 'ACTIVE', consumed_qty: '0', released_qty: '0', updated_at: r.created_at }));

  for (const p of t.pause_state) {
    push(ms(p.raised_at), 'pause_state', { ...p, active: true, cleared_at: null, cleared_by: null, clear_evidence: null });
    if (p.cleared_at) push(ms(p.cleared_at), 'pause_state', p);
    else if (!p.active) push(ms(p.raised_at), 'pause_state', p);
  }

  for (const w of t.loss_windows) {
    push(ms(w.start_time), 'loss_windows', { ...w, breached_at: null });
    if (w.breached_at) push(ms(w.breached_at), 'loss_windows', w);
  }

  // Exit plans close with the last fill of an exit intent that belongs to their entry.
  for (const e of t.exit_plans) {
    push(ms(e.created_at), 'exit_plans', { ...e, active: true });
    if (!e.active) {
      const exitIntents = new Set(
        t.order_intents.filter((i) => i.parent_intent_id === e.entry_intent_id).map((i) => i.intent_id),
      );
      const closeTimes = t.fills.filter((f) => exitIntents.has(String(f.intent_id))).map((f) => ms(f.receipt_time));
      const at = closeTimes.length ? Math.max(...closeTimes) : ms(e.max_hold_until);
      push(at, 'exit_plans', e);
    }
  }

  // Cash lots appear with the fill they settle.
  for (const c of t.cash_lots) {
    const exec = String(c.source).replace(/^fill:/, '');
    const f = t.fills.find((x) => x.execution_id === exec);
    push(f ? ms(f.receipt_time) : start, 'cash_lots', c);
  }

  // Positions: fold fills (average cost), then the stored final row is authoritative.
  const pos = new Map<string, { qty: Decimal; cost: Decimal; fees: Decimal; gross: Decimal; net: Decimal; opened: unknown }>();
  for (const f of [...t.fills].sort((a, b) => ms(a.receipt_time) - ms(b.receipt_time))) {
    const key = `${f.account_id}|${f.instrument_id}`;
    const p = pos.get(key) ?? { qty: new Decimal(0), cost: new Decimal(0), fees: new Decimal(0), gross: new Decimal(0), net: new Decimal(0), opened: null };
    const q = new Decimal(String(f.quantity));
    const px = new Decimal(String(f.price));
    const fee = new Decimal(String(f.fee ?? 0));
    if (f.side === 'BUY') {
      if (p.qty.isZero()) p.opened = f.exec_time;
      p.qty = p.qty.plus(q);
      p.cost = p.cost.plus(q.times(px));
      p.fees = p.fees.plus(fee);
    } else if (p.qty.gt(0)) {
      const share = Decimal.min(q.div(p.qty), 1);
      const costOut = p.cost.times(share);
      const feesOut = p.fees.times(share);
      p.gross = p.gross.plus(q.times(px).minus(costOut));
      p.net = p.net.plus(q.times(px).minus(costOut).minus(feesOut).minus(fee));
      p.qty = p.qty.minus(q);
      p.cost = p.cost.minus(costOut);
      p.fees = p.fees.minus(feesOut);
    }
    pos.set(key, p);
    const stored = t.positions.find((x) => x.account_id === f.account_id && x.instrument_id === f.instrument_id);
    push(ms(f.receipt_time), 'positions', {
      account_id: f.account_id,
      instrument_id: f.instrument_id,
      owner_track: stored?.owner_track ?? 'STOCKS',
      currency: f.currency,
      quantity: p.qty.toFixed(),
      cost_basis: p.cost.toFixed(),
      open_entry_fees: p.fees.toFixed(),
      realized_gross: p.gross.toFixed(),
      realized_net: p.net.toFixed(),
      opened_at: p.qty.gt(0) ? p.opened : null,
      updated_at: f.receipt_time,
    });
  }
  for (const p of t.positions) push(Math.max(ms(p.updated_at), start), 'positions', p);

  items.sort((a, b) => a.at - b.at || a.order - b.order);
  return items;
}

export class ReplaySource extends Source {
  readonly id: `replay:${string}`;
  private items: Item[];
  private cursor = 0;
  private now: number;
  private readonly startAt: number;
  private readonly endAt: number;
  private playing = false;
  private speed = 60;
  private timer: NodeJS.Timeout | null = null;
  private readonly report: { notes: string[]; watermark: string | null };

  constructor(readonly scenario: ReplayScenario) {
    super();
    this.id = `replay:${scenario.id}`;
    this.items = buildTimeline(loadReplayTables(scenario.path));
    this.startAt = this.items.length ? this.items[0].at : Date.now();
    this.endAt = this.items.length ? this.items[this.items.length - 1].at : this.startAt;
    this.now = this.startAt;
    this.report = readReport(scenario.path);
  }

  status(): SourceStatus {
    const iso = (n: number) => new Date(n).toISOString();
    return {
      id: this.id,
      kind: 'replay',
      state: 'ok',
      message: null,
      liveness: null,
      replay: {
        scenario: this.scenario.id,
        startAt: iso(this.startAt),
        endAt: iso(this.endAt),
        now: iso(this.now),
        playing: this.playing,
        speed: this.speed,
        notes: this.report.notes,
        watermark: this.report.watermark,
      },
    };
  }

  start(): void {
    this.seekTo(this.startAt, false);
    this.playing = true;
    this.schedule();
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
  }

  private schedule(): void {
    if (this.timer) clearInterval(this.timer);
    const TICK = 100;
    let lastStatus = 0;
    this.timer = setInterval(() => {
      if (!this.playing) return;
      this.now = Math.min(this.endAt, this.now + TICK * this.speed);
      this.flush();
      if (this.now >= this.endAt) this.playing = false;
      const t = Date.now();
      if (t - lastStatus > 500 || !this.playing) {
        lastStatus = t;
        this.emit({ type: 'status', status: this.status() });
      }
    }, TICK);
  }

  /** Emit every item due by `now` as one delta. */
  private flush(): void {
    const batches = new Map<TableName, Row[]>();
    while (this.cursor < this.items.length && this.items[this.cursor].at <= this.now) {
      const it = this.items[this.cursor++];
      const list = batches.get(it.table) ?? [];
      list.push(it.row);
      batches.set(it.table, list);
    }
    if (batches.size) this.apply([...batches.entries()].map(([table, rows]) => ({ table, rows })));
  }

  private seekTo(at: number, broadcast = true): void {
    this.now = Math.max(this.startAt, Math.min(this.endAt, at));
    this.store.clear();
    this.cursor = 0;
    const batches = new Map<TableName, Row[]>();
    while (this.cursor < this.items.length && this.items[this.cursor].at <= this.now) {
      const it = this.items[this.cursor++];
      const list = batches.get(it.table) ?? [];
      list.push(it.row);
      batches.set(it.table, list);
    }
    for (const [table, rows] of batches) this.store.upsert(table, rows);
    if (broadcast) this.emitSnapshot();
  }

  control(c: ReplayControl): void {
    switch (c.action) {
      case 'play':
        if (this.now >= this.endAt) this.seekTo(this.startAt);
        this.playing = true;
        break;
      case 'pause':
        this.playing = false;
        break;
      case 'speed':
        if (c.speed && c.speed > 0 && c.speed <= 3600) this.speed = c.speed;
        break;
      case 'seek':
        if (c.at) this.seekTo(Date.parse(c.at));
        break;
      case 'next': {
        const NOTABLE = new Set<TableName>(['risk_decisions', 'fills', 'pause_state', 'alerts', 'order_intents']);
        const next = this.items.find((it) => it.at > this.now + 1000 && NOTABLE.has(it.table));
        if (next) this.seekTo(next.at - 3000);
        else this.seekTo(this.endAt);
        break;
      }
      case 'restart':
        this.seekTo(this.startAt);
        this.playing = true;
        break;
    }
    this.emit({ type: 'status', status: this.status() });
  }

  /** Final state of the whole session (used by tests to compare with report.json). */
  finalTables(): Record<TableName, Row[]> {
    this.seekTo(this.endAt, false);
    const snap = this.store.snapshot();
    return Object.fromEntries(TABLE_NAMES.map((t) => [t, snap[t] ?? []])) as Record<TableName, Row[]>;
  }
}
