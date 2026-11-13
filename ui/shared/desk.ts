/**
 * Sunshine Desk: the owner's local notes page (ui/server/desk). Pure logic shared by the page, the
 * Desk server and the tests: note ids, the prompt and VS Code link a note opens, note files, the
 * goal bar's arithmetic and the shape of `.desk/sheet.json`.
 *
 * A note opens a NEW Claude Code tab in VS Code with the prompt typed in but not sent
 * (vscode://anthropic.claude-code/open?prompt=...). The owner presses Enter; nothing runs on its own.
 */

// --------------------------------------------------------------------------- sheet.json
/** A table on the Desk: tracker, decisions, data choices, MCP status. Cells are short text; `code` in backticks. */
export interface DeskSection {
  id: string;
  title: string;
  lead?: string;
  columns: string[];
  rows: string[][];
  /** Rows get an "Answer" link that prefills a note naming the row's first cell. */
  answerable?: boolean;
}

export interface DeskAction {
  title: string;
  why: string;
  command?: string;
  status?: 'todo' | 'doing' | 'done';
}

/** `.desk/sheet.json`, maintained by Claude (the main copy of the old cloud setup sheet). */
export interface DeskSheet {
  title: string;
  /** ISO date of the last update. */
  updated: string;
  goal: { start: number; target: number; label?: string };
  nextActions: DeskAction[];
  sections: DeskSection[];
  links: { label: string; href: string }[];
}

export function isDeskSheet(v: unknown): v is DeskSheet {
  if (!v || typeof v !== 'object') return false;
  const s = v as Partial<DeskSheet>;
  const okGoal =
    !!s.goal && typeof s.goal.start === 'number' && typeof s.goal.target === 'number' && s.goal.start > 0 && s.goal.target > s.goal.start;
  const okSections =
    Array.isArray(s.sections) &&
    s.sections.every(
      (x) => x && typeof x.title === 'string' && Array.isArray(x.columns) && Array.isArray(x.rows) && x.rows.every((r) => Array.isArray(r)),
    );
  return typeof s.title === 'string' && typeof s.updated === 'string' && okGoal && Array.isArray(s.nextActions) && okSections && Array.isArray(s.links);
}

// --------------------------------------------------------------------------- notes
/** `YYYYMMDD-HHMMSS-xxxx` (UTC). Created by the page; the server accepts nothing else, so a request never names a path. */
export const NOTE_ID_RE = /^\d{8}-\d{6}-[0-9a-f]{4}$/;

export function newNoteId(now: Date, random4hex: string): string {
  const p = (n: number, w = 2) => String(n).padStart(w, '0');
  const d = `${now.getUTCFullYear()}${p(now.getUTCMonth() + 1)}${p(now.getUTCDate())}`;
  const t = `${p(now.getUTCHours())}${p(now.getUTCMinutes())}${p(now.getUTCSeconds())}`;
  if (!/^[0-9a-f]{4}$/.test(random4hex)) throw new Error('random part must be 4 lowercase hex digits');
  return `${d}-${t}-${random4hex}`;
}

export type NoteStatus = 'new' | 'opened' | 'in_progress' | 'done' | string;

export interface DeskNote {
  id: string;
  created: string;
  status: NoteStatus;
  text: string;
  /** Claude's answer, written under "## Reply" in the note file. Empty until then. */
  reply: string;
  /** Absolute path of the note file. */
  path: string;
  /** When the page last opened it in Claude Code (Desk state, not the note file). */
  opened?: string;
}

const REPLY_HEADING = '## Reply';
const REPLY_LINE = /^##\s+Reply\s*$/m;

/** The note file the Desk writes once; afterwards it belongs to Claude (status and reply). */
export function noteFile(id: string, created: string, text: string): string {
  // A line that looks like the reply heading inside the note would confuse the parser.
  const body = text.replace(/\r\n/g, '\n').replace(/^##\s+Reply\s*$/gm, '## Reply (quoted)').trimEnd();
  return `---\nid: ${id}\ncreated: ${created}\nstatus: new\n---\n\n${body}\n\n${REPLY_HEADING}\n\n`;
}

export function parseNote(id: string, raw: string, path: string): DeskNote {
  const text = raw.replace(/\r\n/g, '\n');
  const fm: Record<string, string> = {};
  let rest = text;
  const m = /^---\n([\s\S]*?)\n---\n?/.exec(text);
  if (m) {
    for (const line of m[1].split('\n')) {
      const i = line.indexOf(':');
      if (i > 0) fm[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, '');
    }
    rest = text.slice(m[0].length);
  }
  const r = REPLY_LINE.exec(rest);
  const body = r ? rest.slice(0, r.index) : rest;
  const reply = r ? rest.slice(r.index + r[0].length) : '';
  return {
    id,
    created: fm.created ?? '',
    status: (fm.status || 'new').toLowerCase(),
    text: body.trim(),
    reply: reply.trim(),
    path,
  };
}

// --------------------------------------------------------------------------- prompt and VS Code link
/**
 * Chromium on Windows drops external-protocol links longer than 2,048 characters without a word, so
 * the whole link stays under this. Encoding grows non-ASCII text 3-9x; long notes go by file path.
 */
export const MAX_URI = 1900;
export const VSCODE_OPEN = 'vscode://anthropic.claude-code/open?prompt=';

export function noteFooter(id: string, notePath: string): string {
  return `Desk note ${id} · ${notePath} — when done, set "status: done" in its front matter and write a short answer under "## Reply".`;
}

export interface Launch {
  mode: 'inline' | 'pointer';
  prompt: string;
  uri: string;
}

const uriFor = (prompt: string) => VSCODE_OPEN + encodeURIComponent(prompt);

/** The prompt and link for a note: the note itself while it fits, otherwise a pointer to its file. */
export function buildLaunch(id: string, text: string, notePath: string): Launch {
  const footer = noteFooter(id, notePath);
  const inline = `${text.trim()}\n\n---\n${footer}`;
  if (uriFor(inline).length <= MAX_URI) return { mode: 'inline', prompt: inline, uri: uriFor(inline) };
  const flat = text.trim().replace(/\s+/g, ' ');
  for (let n = 150; n >= 0; n -= 10) {
    const preview = n > 0 ? `\n\nIt starts: "${Array.from(flat).slice(0, n).join('')}…"` : '';
    const prompt = `Read my Desk note and act on it: ${notePath}${preview}\n\n---\n${footer}`;
    if (uriFor(prompt).length <= MAX_URI) return { mode: 'pointer', prompt, uri: uriFor(prompt) };
  }
  const bare = `Read my Desk note and act on it: ${notePath}`;
  return { mode: 'pointer', prompt: bare, uri: uriFor(bare) };
}

// --------------------------------------------------------------------------- goal bar
export interface GoalTick {
  value: number;
  /** Position on the bar, 0..1 (log scale: each tick is one doubling). */
  at: number;
}

export interface GoalProgress {
  equity: number;
  hasData: boolean;
  /** 0..1 on a log scale between start and target. */
  fraction: number;
  /** 0..1 on a plain linear scale, for the caption. */
  linearFraction: number;
  doublingsDone: number;
  doublingsNeeded: number;
  belowStartBy: number;
  ticks: GoalTick[];
}

const clamp01 = (x: number) => (Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0);

/** USD start -> target on a log scale, so each doubling moves the bar by the same distance. */
export function goalProgress(equity: number | null, start: number, target: number): GoalProgress {
  const e = equity ?? start;
  const span = Math.log(target / start);
  const done = e > 0 ? Math.log2(e / start) : -Infinity;
  const ticks: GoalTick[] = [];
  for (let v = start * 2; v < target; v *= 2) {
    const at = Math.log(v / start) / span;
    if (at < 0.95) ticks.push({ value: v, at }); // closer than that, its label would overprint the target's
  }
  ticks.push({ value: target, at: 1 });
  return {
    equity: e,
    hasData: equity !== null,
    fraction: e > 0 ? clamp01(Math.log(e / start) / span) : 0,
    linearFraction: clamp01((e - start) / (target - start)),
    doublingsDone: Math.max(0, done),
    doublingsNeeded: Math.log2(target / start),
    belowStartBy: e < start ? start - e : 0,
    ticks,
  };
}

// --------------------------------------------------------------------------- API shapes
export interface DeskSession {
  token: string;
  notesDir: string;
  sep: '\\' | '/';
}

export interface DeskGoalState {
  /** Latest PORTFOLIO equity snapshot (paper), or null when the bot has not run or the database is off. */
  equity: string | null;
  at: string | null;
  source: 'database' | 'no-snapshots' | 'database-unavailable' | 'not-configured';
  /** The portfolio the snapshot belongs to (the console may still be on an older database). */
  portfolioId?: string | null;
  /** The three practice sleeves (ADR-0046); the goal bar shows their sum. */
  sleeves?: DeskSleeve[];
}

export interface DeskSleeve {
  id: 'stocks' | 'crypto' | 'cfd';
  label: string;
  start: number;
  equity: number;
  detail: string;
}

export interface DeskPayload {
  sheet: DeskSheet | null;
  sheetError: string | null;
  notes: DeskNote[];
  goal: DeskGoalState;
}
