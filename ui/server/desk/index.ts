/**
 * Sunshine Desk API, mounted inside the console server under /api/desk (one process, one address:
 * http://127.0.0.1:4317/desk). The owner's local notes page: it never touches the bot's database
 * except one SELECT for the goal bar.
 *
 * It writes only inside `<repo>/.desk/`. It never starts a process: the page opens
 * vscode://anthropic.claude-code/open?prompt=... itself, VS Code shows a new Claude Code tab with
 * the note typed in, and the owner presses Enter.
 *
 * Its writes stay stricter than anything else on the server (guard.ts): the exact console Origin
 * (a missing Origin is refused), a JSON body of at most 64 KB, and the per-process token.
 */
import path from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import { NOTE_ID_RE, type DeskGoalState, type DeskPayload, type DeskSession } from '../../shared/desk';
import { PORT, REPO_ROOT } from '../config';
import { isAllowedHost } from '../guard';
import { GoalReader } from './goal';
import { readSleeves } from './sleeves';
import { deskOrigins, isDeskOrigin, isJson, newToken, tokenMatches } from './guard';
import { DeskStore, NoteExistsError, NoteMissingError } from './store';

const withSleeves = (goal: DeskGoalState): DeskGoalState => ({ ...goal, sleeves: readSleeves(goal) });

export const BODY_LIMIT = 64 * 1024;
const MAX_NOTE_CHARS = 20_000;

export interface DeskOptions {
  /** The `.desk` folder (default `<repo>/.desk`). */
  root?: string;
  /** Port of the page the writes must come from (default: the console's port). */
  port?: number;
  token?: string;
  goal?: () => Promise<DeskGoalState>;
  now?: () => Date;
}

/** Registers the Desk routes under /api/desk on the console's Fastify instance. */
export function registerDesk(app: FastifyInstance, opts: DeskOptions = {}): void {
  const store = new DeskStore(opts.root ?? path.join(REPO_ROOT, '.desk'));
  store.ensure();
  const origins = deskOrigins(opts.port ?? PORT);
  const token = opts.token ?? newToken();
  const now = opts.now ?? (() => new Date());
  const goalReader = opts.goal ? null : new GoalReader();
  const readGoal = opts.goal ?? (() => goalReader!.read());

  void app.register(
    async (desk) => {
      // Scoped to /api/desk: the rest of the console keeps its own (read-only) rules.
      desk.addHook('onRequest', async (req, reply) => {
        if (req.method === 'GET' || req.method === 'HEAD') return;
        if (!isDeskOrigin(req.headers.origin, origins)) return reply.code(403).send({ error: 'cross-origin request refused' });
        if (!isJson(req.headers['content-type'])) return reply.code(415).send({ error: 'JSON only' });
        if (!tokenMatches(req.headers['x-desk-token'], token)) return reply.code(403).send({ error: 'missing or wrong Desk token' });
      });

      const sseClients = new Set<() => void>();
      const stopWatching = store.watch(() => {
        for (const notify of sseClients) notify();
      });
      desk.addHook('onClose', async () => {
        stopWatching();
        await goalReader?.close();
      });

      desk.get('/session', async (_req, reply) => {
        const session: DeskSession = { token, notesDir: store.notesDir, sep: path.sep === '\\' ? '\\' : '/' };
        return reply.header('Cache-Control', 'no-store').send(session);
      });

      desk.get('/state', async (_req, reply) => {
        const { sheet, error } = store.readSheet();
        const payload: DeskPayload = { sheet, sheetError: error, notes: store.listNotes(), goal: withSleeves(await readGoal()) };
        return reply.header('Cache-Control', 'no-store').send(payload);
      });

      desk.get('/stream', (req, reply) => {
        reply.hijack();
        const raw = reply.raw;
        raw.writeHead(200, {
          'Content-Type': 'text/event-stream; charset=utf-8',
          'Cache-Control': 'no-cache, no-transform',
          Connection: 'keep-alive',
        });
        const notify = () => raw.write('event: changed\ndata: {}\n\n');
        sseClients.add(notify);
        const keepAlive = setInterval(() => raw.write(': keep-alive\n\n'), 15_000);
        req.raw.on('close', () => {
          clearInterval(keepAlive);
          sseClients.delete(notify);
        });
      });

      desk.post<{ Body: { id?: unknown; text?: unknown; opened?: unknown } }>('/notes', { bodyLimit: BODY_LIMIT }, async (req, reply) => {
        const { id, text, opened } = req.body ?? {};
        if (typeof id !== 'string' || !NOTE_ID_RE.test(id)) return reply.code(400).send({ error: 'invalid note id' });
        if (typeof text !== 'string' || !text.trim()) return reply.code(400).send({ error: 'empty note' });
        if (text.length > MAX_NOTE_CHARS) return reply.code(400).send({ error: `notes are limited to ${MAX_NOTE_CHARS} characters` });
        let file: string;
        try {
          file = store.createNote(id, text, now());
        } catch (e) {
          if (e instanceof NoteExistsError) return reply.code(409).send({ error: 'a note with this id exists' });
          throw e;
        }
        if (opened === true) await store.markOpened(id, now());
        return reply.code(201).send({ id, path: file });
      });

      desk.post<{ Params: { id: string } }>('/notes/:id/opened', { bodyLimit: BODY_LIMIT }, async (req, reply) => {
        if (!NOTE_ID_RE.test(req.params.id)) return reply.code(400).send({ error: 'invalid note id' });
        try {
          await store.markOpened(req.params.id, now());
        } catch (e) {
          if (e instanceof NoteMissingError) return reply.code(404).send({ error: 'no such note' });
          throw e;
        }
        return reply.code(204).send();
      });
    },
    { prefix: '/api/desk' },
  );
}

/** A server with only the Desk on it (behind the same localhost Host guard). Used by tests. */
export function buildDeskServer(opts: DeskOptions = {}): FastifyInstance {
  const app = Fastify({ logger: false });
  app.addHook('onRequest', async (req, reply) => {
    if (!isAllowedHost(req.headers.host)) return reply.code(403).send({ error: 'localhost only' });
  });
  registerDesk(app, opts);
  return app;
}
