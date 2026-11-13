/** The goal bar's number: the latest PORTFOLIO equity snapshot, read through the SELECT-only role. */
import type pg from 'pg';
import type { DeskGoalState } from '../../shared/desk';
import { readOnlyPool } from '../db';
import { Q } from '../queries';

export class GoalReader {
  private readonly pool: pg.Pool | null;
  private cached: { at: number; value: DeskGoalState } | null = null;

  constructor(pool: pg.Pool | null = readOnlyPool('trader-desk (read-only)')) {
    this.pool = pool;
  }

  async read(): Promise<DeskGoalState> {
    if (this.cached && Date.now() - this.cached.at < 5000) return this.cached.value;
    const value = await this.query();
    this.cached = { at: Date.now(), value };
    return value;
  }

  private async query(): Promise<DeskGoalState> {
    if (!this.pool) return { equity: null, at: null, source: 'not-configured' };
    try {
      const ready = await this.pool.query<{ ready: boolean }>(Q.equityReady);
      if (!ready.rows[0]?.ready) return { equity: null, at: null, source: 'no-snapshots' };
      const r = await this.pool.query<{ equity: string; ts: Date; scope_id: string }>(Q.latestPortfolioEquity);
      const row = r.rows[0];
      if (!row) return { equity: null, at: null, source: 'no-snapshots' };
      return { equity: row.equity, at: new Date(row.ts).toISOString(), source: 'database', portfolioId: row.scope_id };
    } catch {
      return { equity: null, at: null, source: 'database-unavailable' };
    }
  }

  async close(): Promise<void> {
    await this.pool?.end().catch(() => undefined);
  }
}
