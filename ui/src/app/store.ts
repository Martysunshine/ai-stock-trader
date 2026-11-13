import { create } from 'zustand';
import { emptyTables, rowKey, TABLE_NAMES, type Row, type TableName, type Tables } from '../../shared/schema';
import type { SourceId, SourceStatus, StreamMessage } from '../../shared/protocol';
import type { Tz } from '../design/format';

export type Connection = 'connecting' | 'open' | 'retrying';
export type ThemePref = 'system' | 'light' | 'dark';

interface Pulse {
  /** Increments on every new risk decision; the Core and feeds animate on change. */
  decisions: number;
  fills: number;
  ledger: number;
  alerts: number;
  lastDecisionApproved: boolean | null;
}

interface ConsoleState {
  source: SourceId;
  connection: Connection;
  status: SourceStatus | null;
  tables: Tables;
  /** Wall-clock time of the last message from the server. */
  lastMessageAt: number | null;
  pulse: Pulse;
  tz: Tz;
  theme: ThemePref;
  /** Increments on every snapshot (source switch, reconnect, replay seek): not "new" activity. */
  snapshotVersion: number;
  activityOpen: boolean;
  /** Activity at or before this ISO time counts as seen. */
  activitySeenAt: string;
  notify: boolean;
  setActivityOpen: (open: boolean) => void;
  markActivitySeen: (at: string) => void;
  setNotify: (on: boolean) => void;
  setSource: (s: SourceId) => void;
  setConnection: (c: Connection) => void;
  setTz: (tz: Tz) => void;
  setTheme: (t: ThemePref) => void;
  receive: (m: StreamMessage) => void;
}

const SORT: Partial<Record<TableName, (a: Row, b: Row) => number>> = {
  risk_decisions: (a, b) => Number(a.seq) - Number(b.seq),
  order_intents: (a, b) => Number(a.seq) - Number(b.seq),
  candidates: (a, b) => String(a.decision_time).localeCompare(String(b.decision_time)),
  bars: (a, b) => String(a.start).localeCompare(String(b.start)),
  ticks: (a, b) => String(a.event_time).localeCompare(String(b.event_time)) || Number(a.id) - Number(b.id),
  equity_snapshots: (a, b) => String(a.ts).localeCompare(String(b.ts)) || Number(a.id) - Number(b.id),
  ledger_entries: (a, b) => Number(a.id) - Number(b.id),
  fills: (a, b) => Number(a.id) - Number(b.id),
  broker_order_events: (a, b) => Number(a.id) - Number(b.id),
  reconciliation_runs: (a, b) => String(a.started_at).localeCompare(String(b.started_at)),
  alerts: (a, b) => Number(a.id) - Number(b.id),
};

function merge(table: TableName, current: Row[], incoming: Row[]): Row[] {
  const byKey = new Map(current.map((r) => [rowKey(table, r), r]));
  for (const r of incoming) byKey.set(rowKey(table, r), r);
  const rows = [...byKey.values()];
  const sort = SORT[table];
  return sort ? rows.sort(sort) : rows;
}

const readPref = <T extends string>(key: string, fallback: T, allowed: readonly T[]): T => {
  try {
    const v = localStorage.getItem(key) as T | null;
    return v && allowed.includes(v) ? v : fallback;
  } catch {
    return fallback;
  }
};
const writePref = (key: string, v: string) => {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* storage unavailable: preference lasts for this tab only */
  }
};

const initialSource = (): SourceId => {
  const fromUrl = new URLSearchParams(location.search).get('source');
  if (fromUrl === 'live' || fromUrl?.startsWith('replay:')) return fromUrl as SourceId;
  try {
    const v = localStorage.getItem('console.source');
    if (v === 'live' || v?.startsWith('replay:')) return v as SourceId;
  } catch {
    /* ignore */
  }
  return 'live';
};

export const useConsole = create<ConsoleState>((set, get) => ({
  source: initialSource(),
  connection: 'connecting',
  status: null,
  tables: emptyTables(),
  lastMessageAt: null,
  pulse: { decisions: 0, fills: 0, ledger: 0, alerts: 0, lastDecisionApproved: null },
  tz: readPref<Tz>('console.tz', 'market', ['market', 'local']),
  theme: readPref<ThemePref>('console.theme', 'system', ['system', 'light', 'dark']),
  snapshotVersion: 0,
  activityOpen: readPref<'1' | '0'>('console.activity', '0', ['1', '0']) === '1',
  activitySeenAt: '',
  notify: readPref<'1' | '0'>('console.notify', '0', ['1', '0']) === '1',
  setActivityOpen: (activityOpen) => {
    writePref('console.activity', activityOpen ? '1' : '0');
    set({ activityOpen });
  },
  markActivitySeen: (activitySeenAt) => set((st) => (activitySeenAt > st.activitySeenAt ? { activitySeenAt } : {})),
  setNotify: (notify) => {
    writePref('console.notify', notify ? '1' : '0');
    set({ notify });
  },

  setSource: (source) => {
    writePref('console.source', source);
    set({ source, status: null, tables: emptyTables(), connection: 'connecting', activitySeenAt: '' });
  },
  setConnection: (connection) => set({ connection }),
  setTz: (tz) => {
    writePref('console.tz', tz);
    set({ tz });
  },
  setTheme: (theme) => {
    writePref('console.theme', theme);
    set({ theme });
  },

  receive: (m) => {
    const now = Date.now();
    if (m.type === 'snapshot') {
      const tables = emptyTables();
      for (const t of TABLE_NAMES) {
        const rows = (m.tables[t] ?? []) as Row[];
        (tables as unknown as Record<TableName, Row[]>)[t] = merge(t, [], rows);
      }
      set((st) => ({ status: m.status, tables, lastMessageAt: now, snapshotVersion: st.snapshotVersion + 1 }));
    } else if (m.type === 'status') {
      set({ status: m.status, lastMessageAt: now });
    } else if (m.type === 'delta') {
      const prev = get();
      const tables = { ...prev.tables } as unknown as Record<TableName, Row[]>;
      const pulse = { ...prev.pulse };
      for (const c of m.changes) {
        const before = tables[c.table].length;
        tables[c.table] = merge(c.table, tables[c.table], c.rows);
        const added = tables[c.table].length - before;
        if (c.table === 'risk_decisions' && added > 0) {
          pulse.decisions += added;
          pulse.lastDecisionApproved = Boolean(c.rows[c.rows.length - 1]?.approved);
        }
        if (c.table === 'fills' && added > 0) pulse.fills += added;
        if (c.table === 'ledger_entries' && added > 0) pulse.ledger += added;
        if (c.table === 'alerts' && added > 0) pulse.alerts += added;
      }
      set({ tables: tables as unknown as Tables, pulse, lastMessageAt: now });
    }
  },
}));

/** The "current time" of what is on screen: the replay clock in replay, the wall clock live. */
export function viewNow(status: SourceStatus | null, wall: number): number {
  if (status?.kind === 'replay' && status.replay) return Date.parse(status.replay.now);
  return wall;
}
