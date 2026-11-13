/**
 * NYSE session phases, from the bot's own calendar (shared/nyse-sessions.json is exported from
 * trader.data.calendars.ExchangeCalendar). Windows follow data/calendars.py: entries from open+30
 * to close-30 minutes, the close procedure from close-15 minutes.
 */
import calendar from '../../shared/nyse-sessions.json';

export type Phase = 'closed' | 'pre_open' | 'opening' | 'entry' | 'no_new_entries' | 'close_procedure';

export interface SessionInfo {
  phase: Phase;
  label: string;
  detail: string;
  /** Today's session, if any. */
  open: number | null;
  close: number | null;
  entryFrom: number | null;
  entryTo: number | null;
  closeProcedureFrom: number | null;
  /** The next regular open after `now`. */
  nextOpen: number | null;
  calendarEndsAt: number;
}

const MIN = 60_000;
const sessions = calendar.sessions as [string, number, number][];
const lastClose = sessions[sessions.length - 1][2];

export interface SessionWindow {
  date: string;
  open: number;
  close: number;
  entryFrom: number;
  entryTo: number;
  closeProcedureFrom: number;
  state: 'upcoming' | 'live' | 'done';
}

/** The session worth drawing at `now`: today's (from 6 h before the open), else the last finished one. */
export function displaySession(now: number): SessionWindow | null {
  let pick = sessions.find(([, o, c]) => now >= o - 6 * 3600_000 && now < c);
  let state: SessionWindow['state'] = 'live';
  if (pick) state = now < pick[1] ? 'upcoming' : 'live';
  else {
    const past = sessions.filter(([, , c]) => c <= now);
    pick = past[past.length - 1];
    state = 'done';
  }
  if (!pick) return null;
  const [date, open, close] = pick;
  return {
    date,
    open,
    close,
    entryFrom: open + calendar.entry_exclusion_open_min * MIN,
    entryTo: close - calendar.entry_exclusion_close_min * MIN,
    closeProcedureFrom: close - calendar.close_procedure_lead_min * MIN,
    state,
  };
}

export function sessionAt(now: number): SessionInfo {
  // "Today" is a session that has not closed yet and opens within the next 6 hours (pre-open).
  const today = sessions.find(([, o, c]) => now >= o - 6 * 3600_000 && now < c) ?? null;
  const nextOpen = sessions.find(([, o]) => o > now)?.[1] ?? null;
  const base = { nextOpen, calendarEndsAt: lastClose };
  if (!today) {
    return { ...base, phase: 'closed', label: 'Market closed', detail: '', open: null, close: null, entryFrom: null, entryTo: null, closeProcedureFrom: null };
  }
  const [, open, close] = today;
  const entryFrom = open + calendar.entry_exclusion_open_min * MIN;
  const entryTo = close - calendar.entry_exclusion_close_min * MIN;
  const cp = close - calendar.close_procedure_lead_min * MIN;
  const win = { open, close, entryFrom, entryTo, closeProcedureFrom: cp };
  if (now < open) return { ...base, ...win, phase: 'pre_open', label: 'Pre-open', detail: 'Session starts soon' };
  if (now < entryFrom) return { ...base, ...win, phase: 'opening', label: 'Open', detail: 'First 30 minutes: no new entries' };
  if (now < entryTo) return { ...base, ...win, phase: 'entry', label: 'Open', detail: 'Entry window' };
  if (now < cp) return { ...base, ...win, phase: 'no_new_entries', label: 'Open', detail: 'Last 30 minutes: no new entries' };
  return { ...base, ...win, phase: 'close_procedure', label: 'Closing', detail: 'Close procedure: exits only' };
}
