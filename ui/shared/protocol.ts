import type { Iso, Row, TableName } from './schema';

/** Where the console's runtime rows come from. */
export type SourceId = 'live' | `replay:${string}`;

export type SourceState =
  /** ui/.env.local has no UI_DATABASE_URL (the read-only role is not set up yet). */
  | 'not_configured'
  /** The database could not be reached or a query failed. */
  | 'unavailable'
  /** Reachable, but the bot has not created its tables yet (`trader paper run` has never run). */
  | 'uninitialized'
  | 'ok';

export interface Liveness {
  /** Latest reconciliation_runs.finished_at; about every 60 s while a paper worker runs. */
  lastReconAt: Iso | null;
  /** Whether a session holds the account's leadership advisory lock (PostgreSQL only). */
  leaderLockHeld: boolean | null;
  /** Latest bars.available_time. */
  lastBarAt: Iso | null;
  /** When the console last completed a poll (live) or the replay clock position (replay). */
  observedAt: Iso;
}

export interface ReplayClock {
  scenario: string;
  startAt: Iso;
  endAt: Iso;
  now: Iso;
  playing: boolean;
  speed: number;
  /** Scenario notes from runs/demo/<scenario>/report.json. */
  notes: string[];
  watermark: string | null;
}

export interface SourceStatus {
  id: SourceId;
  kind: 'live' | 'replay';
  state: SourceState;
  message: string | null;
  liveness: Liveness | null;
  replay: ReplayClock | null;
}

export interface ReplayScenario {
  id: string;
  path: string;
  notes: string[];
}

/** Server-sent events on /api/stream?source=<SourceId>. */
export type StreamMessage =
  | { type: 'snapshot'; status: SourceStatus; tables: Partial<Record<TableName, Row[]>> }
  | { type: 'delta'; changes: { table: TableName; rows: Row[] }[] }
  | { type: 'status'; status: SourceStatus }
  /** A watched file changed (research outputs, configs); the client refetches that scope. */
  | { type: 'files'; scopes: ('research' | 'rules')[] };

export interface ReplayControl {
  /** next: jump to 3 s before the next decision, fill, pause, alert or order. */
  action: 'play' | 'pause' | 'speed' | 'seek' | 'restart' | 'next';
  speed?: number;
  /** ISO time to seek to. */
  at?: Iso;
}

export const REPLAY_SPEEDS = [1, 10, 60, 300] as const;
