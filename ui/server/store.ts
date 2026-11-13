import { rowKey, TABLE_NAMES, type Row, type TableName } from '../shared/schema';
import type { SourceStatus, StreamMessage } from '../shared/protocol';

/** Upper bounds per table so a long-running worker cannot grow the console's memory unbounded. */
const CAPS: Partial<Record<TableName, number>> = {
  ticks: 20000,
  bars: 6000,
  reconciliation_runs: 3000,
  equity_snapshots: 20000,
};

/** Rows per table keyed by primary key, with change detection by content. */
export class TableStore {
  private tables = new Map<TableName, Map<string, { row: Row; sig: string }>>();

  constructor() {
    for (const t of TABLE_NAMES) this.tables.set(t, new Map());
  }

  /** Insert or replace rows; returns only rows that are new or whose content changed. */
  upsert(table: TableName, rows: Row[]): Row[] {
    const m = this.tables.get(table)!;
    const changed: Row[] = [];
    for (const row of rows) {
      const k = rowKey(table, row);
      const sig = JSON.stringify(row);
      const prev = m.get(k);
      if (prev && prev.sig === sig) continue;
      if (prev) m.delete(k); // re-insert so Map order reflects recency for capping
      m.set(k, { row, sig });
      changed.push(row);
    }
    const cap = CAPS[table];
    if (cap && m.size > cap) {
      const excess = m.size - cap;
      let i = 0;
      for (const k of m.keys()) {
        if (i++ >= excess) break;
        m.delete(k);
      }
    }
    return changed;
  }

  all(table: TableName): Row[] {
    return [...this.tables.get(table)!.values()].map((v) => v.row);
  }

  snapshot(): Partial<Record<TableName, Row[]>> {
    const out: Partial<Record<TableName, Row[]>> = {};
    for (const t of TABLE_NAMES) out[t] = this.all(t);
    return out;
  }

  clear(): void {
    for (const m of this.tables.values()) m.clear();
  }
}

export type Listener = (msg: StreamMessage) => void;

export abstract class Source {
  protected listeners = new Set<Listener>();
  protected store = new TableStore();

  abstract status(): SourceStatus;
  abstract start(): void;
  abstract dispose(): void;

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn({ type: 'snapshot', status: this.status(), tables: this.store.snapshot() });
    return () => this.listeners.delete(fn);
  }

  get subscriberCount(): number {
    return this.listeners.size;
  }

  protected emit(msg: StreamMessage): void {
    for (const fn of this.listeners) fn(msg);
  }

  protected emitSnapshot(): void {
    this.emit({ type: 'snapshot', status: this.status(), tables: this.store.snapshot() });
  }

  /** Apply rows for several tables and broadcast only what changed. */
  protected apply(batches: { table: TableName; rows: Row[] }[]): void {
    const changes = batches
      .map(({ table, rows }) => ({ table, rows: this.store.upsert(table, rows) }))
      .filter((c) => c.rows.length > 0);
    if (changes.length) this.emit({ type: 'delta', changes });
  }
}
