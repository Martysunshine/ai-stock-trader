import type { SourceStatus } from '../../shared/protocol';
import type { PauseRow } from '../../shared/schema';
import { PAUSE_CAUSES } from '../design/language';
import { sessionAt, type SessionInfo } from './session';

export type BotTone = 'running' | 'idle' | 'paused' | 'alert' | 'offline' | 'replay';

export interface BotStatus {
  tone: BotTone;
  label: string;
  detail: string;
}

/** One honest label for "what is the bot doing right now". */
export function botStatus(
  status: SourceStatus | null,
  pauses: PauseRow[],
  now: number,
  session: SessionInfo = sessionAt(now),
): BotStatus {
  const active = pauses.filter((p) => p.active);
  const pauseText = active.length
    ? active.map((p) => PAUSE_CAUSES[p.cause] ?? p.cause).join(' · ')
    : '';

  if (!status) return { tone: 'offline', label: 'Connecting', detail: 'Waiting for the console server' };

  if (status.kind === 'replay') {
    if (active.length) return { tone: 'paused', label: 'Paused', detail: pauseText };
    return {
      tone: 'replay',
      label: status.replay?.playing ? 'Replaying' : 'Replay paused',
      detail: 'Synthetic recorded session',
    };
  }

  if (status.state === 'not_configured') return { tone: 'offline', label: 'Not connected', detail: status.message ?? '' };
  if (status.state === 'unavailable') return { tone: 'alert', label: 'Database unreachable', detail: status.message ?? '' };
  if (status.state === 'uninitialized') return { tone: 'idle', label: 'Never run', detail: status.message ?? '' };

  const l = status.liveness;
  const leader = l?.leaderLockHeld === true;
  const reconAge = l?.lastReconAt ? now - Date.parse(l.lastReconAt) : Infinity;
  const open = session.phase !== 'closed' && session.phase !== 'pre_open';

  if (active.length) return { tone: 'paused', label: 'Entries paused', detail: pauseText };
  if (leader && open && reconAge > 3 * 60_000) {
    return { tone: 'alert', label: 'Stale', detail: 'Worker holds the lock but has not reconciled for over 3 minutes' };
  }
  if (leader && open) return { tone: 'running', label: 'Running', detail: session.detail };
  if (leader) return { tone: 'idle', label: 'Waiting for the open', detail: 'Worker is running; market closed' };
  if (open) return { tone: 'alert', label: 'Stopped', detail: 'Market is open but no worker holds the account lock' };
  return { tone: 'idle', label: 'Idle', detail: 'Market closed; no worker running' };
}
