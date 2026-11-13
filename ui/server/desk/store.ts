/**
 * The Desk's files, all inside `<repo>/.desk/` (gitignored), and nothing else:
 *
 * - `sheet.json`: the setup sheet, maintained by Claude (tracker, next actions, decisions...).
 * - `notes/<id>.md`: one file per note. The Desk writes it ONCE (exclusive create); afterwards it
 *   belongs to Claude, who sets `status: done` and writes the answer under "## Reply".
 * - `state.json`: the Desk's own bookkeeping (when a note was opened in Claude Code), kept apart
 *   so it never races Claude's edits to a note.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, renameSync, watch, writeFileSync } from 'node:fs';
import path from 'node:path';
import { isDeskSheet, NOTE_ID_RE, noteFile, parseNote, type DeskNote, type DeskSheet } from '../../shared/desk';

interface DeskState {
  opened: Record<string, string>;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export class NoteExistsError extends Error {}
export class NoteMissingError extends Error {}

export class DeskStore {
  readonly notesDir: string;
  private readonly statePath: string;

  constructor(readonly root: string) {
    this.notesDir = path.join(root, 'notes');
    this.statePath = path.join(root, 'state.json');
  }

  ensure(): void {
    mkdirSync(this.notesDir, { recursive: true });
  }

  notePath(id: string): string {
    if (!NOTE_ID_RE.test(id)) throw new Error('invalid note id');
    return path.join(this.notesDir, `${id}.md`);
  }

  readSheet(): { sheet: DeskSheet | null; error: string | null } {
    const p = path.join(this.root, 'sheet.json');
    if (!existsSync(p)) return { sheet: null, error: 'No .desk/sheet.json yet. Ask Claude to create it.' };
    try {
      const parsed: unknown = JSON.parse(readFileSync(p, 'utf8'));
      return isDeskSheet(parsed) ? { sheet: parsed, error: null } : { sheet: null, error: '.desk/sheet.json does not match the Desk format.' };
    } catch (e) {
      return { sheet: null, error: `.desk/sheet.json could not be read: ${(e as Error).message}` };
    }
  }

  private readState(): DeskState {
    try {
      const s = JSON.parse(readFileSync(this.statePath, 'utf8')) as Partial<DeskState>;
      return { opened: s.opened && typeof s.opened === 'object' ? s.opened : {} };
    } catch {
      return { opened: {} };
    }
  }

  /** Newest first. Files whose name is not a note id are ignored. */
  listNotes(limit = 300): DeskNote[] {
    if (!existsSync(this.notesDir)) return [];
    const opened = this.readState().opened;
    const ids = readdirSync(this.notesDir)
      .filter((f) => f.endsWith('.md') && NOTE_ID_RE.test(f.slice(0, -3)))
      .map((f) => f.slice(0, -3))
      .sort()
      .reverse()
      .slice(0, limit);
    const notes: DeskNote[] = [];
    for (const id of ids) {
      const p = this.notePath(id);
      try {
        notes.push({ ...parseNote(id, readFileSync(p, 'utf8'), p), opened: opened[id] });
      } catch {
        // Being written or removed right now; the next change event re-reads it.
      }
    }
    return notes;
  }

  /** Exclusive create: an existing note is never overwritten. */
  createNote(id: string, text: string, now: Date): string {
    this.ensure();
    const p = this.notePath(id);
    try {
      writeFileSync(p, noteFile(id, now.toISOString().replace(/\.\d{3}Z$/, 'Z'), text), { encoding: 'utf8', flag: 'wx' });
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === 'EEXIST') throw new NoteExistsError(id);
      throw e;
    }
    return p;
  }

  async markOpened(id: string, at: Date): Promise<void> {
    if (!existsSync(this.notePath(id))) throw new NoteMissingError(id);
    const state = this.readState();
    state.opened[id] = at.toISOString();
    await this.writeAtomic(this.statePath, JSON.stringify(state, null, 1) + '\n');
  }

  /** Temp file, then rename; Windows can refuse a rename while another process reads the target. */
  private async writeAtomic(target: string, text: string): Promise<void> {
    mkdirSync(path.dirname(target), { recursive: true });
    const tmp = `${target}.${process.pid}.tmp`;
    writeFileSync(tmp, text, 'utf8');
    for (let attempt = 0; ; attempt++) {
      try {
        renameSync(tmp, target);
        return;
      } catch (e) {
        const code = (e as NodeJS.ErrnoException).code;
        if (attempt >= 6 || !['EPERM', 'EBUSY', 'EACCES'].includes(code ?? '')) throw e;
        await sleep(25 * (attempt + 1));
      }
    }
  }

  /** Calls `onChange` (debounced) when anything under .desk/ changes, including Claude's edits. */
  watch(onChange: () => void): () => void {
    this.ensure();
    let timer: NodeJS.Timeout | null = null;
    // Real long path: Node aborts (libuv fs-event.c assertion) when a recursive watch is given an
    // 8.3 short path such as C:\Users\SOMEUS~1\..., which Windows uses for a user name with a space.
    const watcher = watch(realpathSync.native(this.root), { recursive: true }, (_event, name) => {
      if (name && String(name).endsWith('.tmp')) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(onChange, 150);
    });
    watcher.on('error', () => undefined);
    return () => {
      if (timer) clearTimeout(timer);
      watcher.close();
    };
  }
}
