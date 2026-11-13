/**
 * Live source: the paper runtime's PostgreSQL database, polled read-only every POLL_MS.
 *
 * Cursors (docs/observability.md section 3): append-only tables by `id`, decisions and intents by
 * their gap-free `seq`, reconciliations and bars by time; small tables that change in place are
 * re-read and diffed. The connection runs with default_transaction_read_only=on on top of the
 * SELECT-only role, so even a coding mistake here cannot write.
 */
import { createHash } from 'node:crypto';
import pg from 'pg';
import { normaliseRow, type Row, type TableName } from '../../shared/schema';
import type { Liveness, SourceState, SourceStatus } from '../../shared/protocol';
import { DATABASE_URL, PAPER_ACCOUNT_ID, POLL_MS } from '../config';
import { APPEND_BY_ID, Q, SMALL_MUTABLE } from '../queries';
import { Source } from '../store';

// Keep exact types: NUMERIC stays a string (pg default); DATE stays a calendar string.
pg.types.setTypeParser(1082, (v: string) => v);
pg.types.setTypeParser(20, (v: string) => Number(v)); // int8 ids/seq: safe below 2^53

const INITIAL_ROWS = 5000;
const BATCH = 5000;

/** trader.domain.ids.advisory_lock_key: uuid5(_NAMESPACE, "lock|" + name).int >> 65. */
export function advisoryLockKey(name: string): bigint {
  const ns = Buffer.from('6f1c2a0e7d3b5c9e9a412b8f3e6d5c10', 'hex');
  const h = createHash('sha1').update(Buffer.concat([ns, Buffer.from('lock|' + name, 'utf8')])).digest();
  const b = Buffer.from(h.subarray(0, 16));
  b[6] = (b[6] & 0x0f) | 0x50; // version 5
  b[8] = (b[8] & 0x3f) | 0x80; // RFC 4122 variant
  return BigInt('0x' + b.toString('hex')) >> 65n;
}

/** pg_locks shows a bigint advisory key split into (classid = high 32 bits, objid = low 32 bits). */
export function lockKeyParts(key: bigint): [string, string] {
  return [(key >> 32n).toString(), (key & 0xffffffffn).toString()];
}

export class LiveSource extends Source {
  readonly id = 'live' as const;
  private pool: pg.Pool | null = null;
  private timer: NodeJS.Timeout | null = null;
  private state: SourceState = DATABASE_URL ? 'unavailable' : 'not_configured';
  private message: string | null = DATABASE_URL
    ? 'Connecting...'
    : 'No read-only database URL. Run ui/scripts/create-readonly-role.ps1 once (docs/observability.md section 4).';
  private liveness: Liveness | null = null;
  private loaded = false;
  private cursors = new Map<string, number | string>();
  private polling = false;
  private readonly lockParts = lockKeyParts(advisoryLockKey(`account:${PAPER_ACCOUNT_ID}`));

  status(): SourceStatus {
    return { id: this.id, kind: 'live', state: this.state, message: this.message, liveness: this.liveness, replay: null };
  }

  start(): void {
    if (!DATABASE_URL) return;
    this.pool = new pg.Pool({
      connectionString: DATABASE_URL,
      max: 2,
      application_name: 'trader-console (read-only)',
      options: '-c default_transaction_read_only=on -c TimeZone=UTC -c statement_timeout=5000',
      connectionTimeoutMillis: 4000,
    });
    this.pool.on('error', () => undefined); // idle-client errors surface on the next poll
    const loop = async () => {
      await this.poll();
      this.timer = setTimeout(loop, POLL_MS);
    };
    void loop();
  }

  dispose(): void {
    if (this.timer) clearTimeout(this.timer);
    void this.pool?.end();
  }

  private async q(sql: string, params: unknown[] = []): Promise<Row[]> {
    const res = await this.pool!.query(sql, params);
    return res.rows as Row[];
  }

  private setState(state: SourceState, message: string | null): void {
    const changed = state !== this.state || message !== this.message;
    this.state = state;
    this.message = message;
    if (changed) this.emit({ type: 'status', status: this.status() });
  }

  private async poll(): Promise<void> {
    if (this.polling || !this.pool) return;
    this.polling = true;
    try {
      const [{ ready }] = await this.q(Q.schemaReady);
      if (!ready) {
        this.setState(
          'uninitialized',
          'Connected. The bot has not created its tables yet: `trader paper run` creates them on first start.',
        );
        return;
      }
      if (!this.loaded) await this.initialLoad();
      else await this.incremental();
      const [l] = await this.q(Q.liveness, this.lockParts);
      const liveness: Liveness = {
        lastReconAt: l.last_recon ? new Date(l.last_recon as string).toISOString() : null,
        lastBarAt: l.last_bar ? new Date(l.last_bar as string).toISOString() : null,
        leaderLockHeld: Boolean(l.leader),
        observedAt: new Date().toISOString(),
      };
      this.liveness = liveness;
      this.setState('ok', null);
      this.emit({ type: 'status', status: this.status() });
    } catch (err) {
      const e = err as { code?: string; message?: string };
      const msg =
        e.code === '28P01'
          ? 'Login failed for the read-only role (check UI_DATABASE_URL in ui/.env.local).'
          : e.code === 'ECONNREFUSED'
            ? 'PostgreSQL is not reachable on the configured port (is `docker compose up -d` running?).'
            : (e.message ?? String(err));
      this.setState('unavailable', msg);
    } finally {
      this.polling = false;
    }
  }

  private norm(table: TableName, rows: Row[]): Row[] {
    return rows.map((r) => normaliseRow(table, r));
  }

  private maxOf(rows: Row[], col: string, prev: number | string | undefined): number | string | undefined {
    let m = prev;
    for (const r of rows) {
      const v = r[col] as number | string;
      if (m === undefined || v > m) m = v;
    }
    return m;
  }

  private async initialLoad(): Promise<void> {
    this.store.clear();
    const batches: { table: TableName; rows: Row[] }[] = [];
    for (const t of APPEND_BY_ID) {
      const rows = await this.q(Q.latestById(t), [t === 'ticks' ? 2000 : INITIAL_ROWS]);
      this.cursors.set(t, (this.maxOf(rows, 'id', 0) as number) ?? 0);
      batches.push({ table: t, rows: this.norm(t, rows) });
    }
    const decisions = await this.q(Q.latestDecisions, [INITIAL_ROWS]);
    this.cursors.set('risk_decisions', (this.maxOf(decisions, 'seq', 0) as number) ?? 0);
    const cands = decisions.length
      ? await this.q(Q.candidatesByIds, [decisions.map((d) => d.candidate_id)])
      : [];
    batches.push({ table: 'candidates', rows: this.norm('candidates', cands) });
    batches.push({ table: 'risk_decisions', rows: this.norm('risk_decisions', decisions) });

    const intents = await this.q(Q.intentsInitial, [INITIAL_ROWS]);
    this.cursors.set('order_intents', (this.maxOf(intents, 'seq', 0) as number) ?? 0);
    this.cursors.set('order_intents_updated', this.isoMax(intents, 'updated_at'));
    batches.push({ table: 'order_intents', rows: this.norm('order_intents', intents) });

    const recons = await this.q(Q.latestRecons, [2000]);
    this.cursors.set('reconciliation_runs', this.isoMax(recons, 'started_at'));
    batches.push({ table: 'reconciliation_runs', rows: this.norm('reconciliation_runs', recons) });

    const bars = await this.q(Q.latestBars, [5000]);
    this.cursors.set('bars', this.isoMax(bars, 'available_time'));
    batches.push({ table: 'bars', rows: this.norm('bars', bars) });

    for (const t of SMALL_MUTABLE) batches.push({ table: t, rows: this.norm(t, await this.q(Q.full(t))) });

    for (const b of batches) this.store.upsert(b.table, b.rows);
    this.loaded = true;
    this.emitSnapshot();
  }

  private isoMax(rows: Row[], col: string): string {
    let m = '1970-01-01T00:00:00.000Z';
    for (const r of rows) {
      const v = r[col] instanceof Date ? (r[col] as Date).toISOString() : String(r[col] ?? '');
      if (v > m) m = v;
    }
    return m;
  }

  private async incremental(): Promise<void> {
    const batches: { table: TableName; rows: Row[] }[] = [];
    for (const t of APPEND_BY_ID) {
      const rows = await this.q(Q.afterId(t), [this.cursors.get(t) ?? 0, BATCH]);
      if (!rows.length) continue;
      this.cursors.set(t, this.maxOf(rows, 'id', this.cursors.get(t))!);
      batches.push({ table: t, rows: this.norm(t, rows) });
    }

    const decisions = await this.q(Q.decisionsAfter, [this.cursors.get('risk_decisions') ?? 0, BATCH]);
    if (decisions.length) {
      this.cursors.set('risk_decisions', this.maxOf(decisions, 'seq', this.cursors.get('risk_decisions'))!);
      const cands = await this.q(Q.candidatesByIds, [decisions.map((d) => d.candidate_id)]);
      batches.push({ table: 'candidates', rows: this.norm('candidates', cands) });
      batches.push({ table: 'risk_decisions', rows: this.norm('risk_decisions', decisions) });
    }

    // Overlap the updated_at cursor by 2 s; unchanged rows are dropped by the store's diff.
    const since = new Date(Date.parse(String(this.cursors.get('order_intents_updated'))) - 2000).toISOString();
    const intents = await this.q(Q.intentsChanged, [this.cursors.get('order_intents') ?? 0, since]);
    if (intents.length) {
      this.cursors.set('order_intents', this.maxOf(intents, 'seq', this.cursors.get('order_intents'))!);
      const u = this.isoMax(intents, 'updated_at');
      if (u > String(this.cursors.get('order_intents_updated'))) this.cursors.set('order_intents_updated', u);
      batches.push({ table: 'order_intents', rows: this.norm('order_intents', intents) });
    }

    const recons = await this.q(Q.reconsAfter, [this.cursors.get('reconciliation_runs'), BATCH]);
    if (recons.length) {
      this.cursors.set('reconciliation_runs', this.isoMax(recons, 'started_at'));
      batches.push({ table: 'reconciliation_runs', rows: this.norm('reconciliation_runs', recons) });
    }

    const bars = await this.q(Q.barsAfter, [this.cursors.get('bars'), BATCH]);
    if (bars.length) {
      this.cursors.set('bars', this.isoMax(bars, 'available_time'));
      batches.push({ table: 'bars', rows: this.norm('bars', bars) });
    }

    for (const t of SMALL_MUTABLE) batches.push({ table: t, rows: this.norm(t, await this.q(Q.full(t))) });
    this.apply(batches);
  }
}
