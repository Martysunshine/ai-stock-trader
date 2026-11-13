import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { FastifyInstance } from 'fastify';
import {
  buildLaunch,
  goalProgress,
  isDeskSheet,
  MAX_URI,
  newNoteId,
  NOTE_ID_RE,
  noteFile,
  parseNote,
  VSCODE_OPEN,
  type DeskPayload,
} from '../shared/desk';
import { buildDeskServer, BODY_LIMIT } from '../server/desk/index';
import { deskOrigins, isDeskOrigin, isJson, tokenMatches } from '../server/desk/guard';

const NOTE_PATH = 'C:\\Users\\owner\\Documents\\trading stockbot\\.desk\\notes\\20261109-101500-a1b2.md';

describe('Desk: note ids', () => {
  it('are UTC timestamps plus 4 hex digits, and nothing else passes', () => {
    const id = newNoteId(new Date(Date.UTC(2026, 10, 9, 7, 5, 9)), '0a1f');
    expect(id).toBe('20261109-070509-0a1f');
    expect(NOTE_ID_RE.test(id)).toBe(true);
    for (const bad of ['../x', '20261109-070509-0A1F', '20261109-070509-0a1f.md', '2026119-070509-0a1f', '']) {
      expect(NOTE_ID_RE.test(bad), bad).toBe(false);
    }
    expect(() => newNoteId(new Date(), 'zz')).toThrow();
  });
});

describe('Desk: the VS Code link', () => {
  it('puts a short note inline, prefilled for a new Claude Code tab', () => {
    const l = buildLaunch('20261109-101500-a1b2', 'What should I do next?', NOTE_PATH);
    expect(l.mode).toBe('inline');
    expect(l.uri.startsWith(VSCODE_OPEN)).toBe(true);
    expect(l.uri.length).toBeLessThanOrEqual(MAX_URI);
    const prompt = decodeURIComponent(l.uri.slice(VSCODE_OPEN.length));
    expect(prompt).toBe(l.prompt);
    expect(prompt.startsWith('What should I do next?')).toBe(true);
    expect(prompt).toContain(NOTE_PATH);
    expect(prompt).toContain('status: done');
  });

  it('switches to a pointer to the note file exactly when the link would pass the limit', () => {
    const footerOnly = buildLaunch('20261109-101500-a1b2', 'x', NOTE_PATH).uri.length - 1;
    const fits = 'a'.repeat(MAX_URI - footerOnly);
    expect(buildLaunch('20261109-101500-a1b2', fits, NOTE_PATH).mode).toBe('inline');
    expect(buildLaunch('20261109-101500-a1b2', fits + 'a', NOTE_PATH).mode).toBe('pointer');
  });

  it('keeps long and non-ASCII notes under the Windows limit and names the file', () => {
    for (const text of ['ä'.repeat(400), '日本語のメモ'.repeat(80), 'word '.repeat(3000), '🙂'.repeat(500)]) {
      const l = buildLaunch('20261109-101500-a1b2', text, NOTE_PATH);
      expect(l.mode).toBe('pointer');
      expect(l.uri.length).toBeLessThanOrEqual(MAX_URI);
      expect(decodeURIComponent(l.uri.slice(VSCODE_OPEN.length))).toContain(NOTE_PATH);
      expect(() => decodeURIComponent(l.uri)).not.toThrow(); // never cuts a character in half
    }
  });
});

describe('Desk: note files', () => {
  it('round-trips, and a reply written by Claude is read back', () => {
    const raw = noteFile('20261109-101500-a1b2', '2026-11-09T10:15:00Z', 'Line one\r\nLine two\n## Reply\nnot a heading');
    const n = parseNote('20261109-101500-a1b2', raw, NOTE_PATH);
    expect(n.status).toBe('new');
    expect(n.text).toBe('Line one\nLine two\n## Reply (quoted)\nnot a heading');
    expect(n.reply).toBe('');
    const answered = raw.replace('status: new', 'status: done') + 'Done: the Desk is running.\n';
    const a = parseNote('20261109-101500-a1b2', answered.replace(/\n/g, '\r\n'), NOTE_PATH);
    expect(a.status).toBe('done');
    expect(a.reply).toBe('Done: the Desk is running.');
    expect(a.created).toBe('2026-11-09T10:15:00Z');
  });
});

describe('Desk: goal bar arithmetic (USD 50 to 10,000, log scale)', () => {
  it('measures doublings, clamps, and reports a loss below the start', () => {
    expect(goalProgress(50, 50, 10_000).fraction).toBe(0);
    expect(goalProgress(100, 50, 10_000).fraction).toBeCloseTo(1 / Math.log2(200), 10);
    expect(goalProgress(10_000, 50, 10_000).fraction).toBe(1);
    expect(goalProgress(25_000, 50, 10_000).fraction).toBe(1);
    const down = goalProgress(30, 50, 10_000);
    expect(down.fraction).toBe(0);
    expect(down.belowStartBy).toBe(20);
    expect(goalProgress(0, 50, 10_000).fraction).toBe(0);
    const none = goalProgress(null, 50, 10_000);
    expect(none.hasData).toBe(false);
    expect(none.equity).toBe(50);
    const ticks = none.ticks.map((t) => t.value);
    expect(ticks).toEqual([100, 200, 400, 800, 1600, 3200, 6400, 10_000]);
    // from USD 300 the last doubling (9,600) would sit on the target's label: it is dropped
    expect(goalProgress(300, 300, 10_000).ticks.map((t) => t.value)).toEqual([600, 1200, 2400, 4800, 10_000]);
    expect(none.doublingsNeeded).toBeCloseTo(7.64, 2);
  });
});

describe('Desk: write guard helpers', () => {
  it('accepts only the exact console origin (the Desk lives inside it) and a JSON body', () => {
    const origins = deskOrigins(4317);
    expect(isDeskOrigin('http://127.0.0.1:4317', origins)).toBe(true);
    expect(isDeskOrigin('http://localhost:4317', origins)).toBe(true);
    // The old standalone Desk port, the old dev ports and anything else are refused.
    for (const o of [undefined, 'null', 'http://127.0.0.1:4320', 'http://127.0.0.1:5174', 'http://127.0.0.1:5173', 'https://evil.example', 'http://127.0.0.1:43170']) {
      expect(isDeskOrigin(o, origins), String(o)).toBe(false);
    }
    expect(isJson('application/json; charset=utf-8')).toBe(true);
    for (const c of [undefined, 'text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data', 'application/jsonp']) {
      expect(isJson(c), String(c)).toBe(false);
    }
    expect(tokenMatches('abc', 'abc')).toBe(true);
    expect(tokenMatches('abd', 'abc')).toBe(false);
    expect(tokenMatches(undefined, 'abc')).toBe(false);
  });
});

describe('Desk server', () => {
  const TOKEN = 'a'.repeat(48);
  const ORIGIN = 'http://127.0.0.1:4317';
  let dir: string;
  let root: string;
  let app: FastifyInstance;

  beforeEach(async () => {
    dir = mkdtempSync(path.join(os.tmpdir(), 'desk-test-'));
    root = path.join(dir, '.desk');
    app = buildDeskServer({
      root,
      port: 4317,
      token: TOKEN,
      goal: async () => ({ equity: null, at: null, source: 'no-snapshots' }),
      now: () => new Date(Date.UTC(2026, 10, 9, 10, 15, 0)),
    });
    await app.ready();
  });

  afterEach(async () => {
    await app.close();
    rmSync(dir, { recursive: true, force: true });
  });

  const write = (url: string, body: unknown, headers: Record<string, string> = {}) =>
    app.inject({
      method: 'POST',
      url,
      headers: { host: '127.0.0.1:4317', origin: ORIGIN, 'content-type': 'application/json', 'x-desk-token': TOKEN, ...headers },
      payload: typeof body === 'string' ? body : JSON.stringify(body),
    });

  it('refuses foreign hosts, missing or foreign origins, wrong tokens and non-JSON bodies', async () => {
    const note = { id: '20261109-101500-a1b2', text: 'hi' };
    expect((await app.inject({ method: 'GET', url: '/api/desk/session', headers: { host: 'evil.example:4317' } })).statusCode).toBe(403);
    expect((await write('/api/desk/notes', note, { host: 'evil.example' })).statusCode).toBe(403);
    expect((await write('/api/desk/notes', note, { origin: 'https://evil.example' })).statusCode).toBe(403);
    expect((await write('/api/desk/notes', note, { origin: 'http://127.0.0.1:4320' })).statusCode).toBe(403); // the old standalone Desk
    const noOrigin = await app.inject({
      method: 'POST',
      url: '/api/desk/notes',
      headers: { host: '127.0.0.1:4317', 'content-type': 'application/json', 'x-desk-token': TOKEN },
      payload: JSON.stringify(note),
    });
    expect(noOrigin.statusCode).toBe(403);
    expect((await write('/api/desk/notes', note, { 'x-desk-token': 'b'.repeat(48) })).statusCode).toBe(403);
    expect((await write('/api/desk/notes', 'id=x', { 'content-type': 'application/x-www-form-urlencoded' })).statusCode).toBe(415);
    expect((await write('/api/desk/notes', 'hi', { 'content-type': 'text/plain' })).statusCode).toBe(415);
    expect(readdirSync(path.join(root, 'notes'))).toEqual([]);
  });

  it('refuses bodies over 64 KB, bad ids and empty notes', async () => {
    expect((await write('/api/desk/notes', { id: '20261109-101500-a1b2', text: 'x'.repeat(BODY_LIMIT) })).statusCode).toBe(413);
    for (const id of ['../../evil', '20261109-101500-A1B2', 'x']) {
      expect((await write('/api/desk/notes', { id, text: 'hi' })).statusCode, id).toBe(400);
    }
    expect((await write('/api/desk/notes', { id: '20261109-101500-a1b2', text: '   ' })).statusCode).toBe(400);
    expect((await write('/api/desk/notes/..%2F..%2Fevil/opened', {})).statusCode).toBe(400);
    expect(readdirSync(path.join(root, 'notes'))).toEqual([]);
  });

  it('writes a note once, refuses a second write, and records it as opened', async () => {
    const id = '20261109-101500-a1b2';
    const created = await write('/api/desk/notes', { id, text: 'What should I do next?', opened: true });
    expect(created.statusCode).toBe(201);
    const file = path.join(root, 'notes', `${id}.md`);
    expect(created.json().path).toBe(file);
    const raw = readFileSync(file, 'utf8');
    expect(raw).toContain('status: new');
    expect(raw).toContain('What should I do next?');
    expect((await write('/api/desk/notes', { id, text: 'overwrite attempt' })).statusCode).toBe(409);
    expect(readFileSync(file, 'utf8')).toBe(raw);

    // Claude answers in the file; the Desk shows it.
    writeFileSync(file, raw.replace('status: new', 'status: done') + 'Here is the plan.\n');
    const payload = (await app.inject({ method: 'GET', url: '/api/desk/state', headers: { host: 'localhost:4317' } })).json() as DeskPayload;
    expect(payload.notes).toHaveLength(1);
    expect(payload.notes[0]).toMatchObject({ id, status: 'done', reply: 'Here is the plan.', opened: '2026-11-09T10:15:00.000Z' });
    expect(payload.sheet).toBeNull();
    expect(payload.sheetError).toMatch(/sheet\.json/);
    expect(payload.goal.source).toBe('no-snapshots');

    expect((await write(`/api/desk/notes/${id}/opened`, {})).statusCode).toBe(204);
    expect((await write('/api/desk/notes/20261109-101500-ffff/opened', {})).statusCode).toBe(404);
  });

  it('hands the token only to same-origin readers and writes nothing outside .desk', async () => {
    const r = await app.inject({ method: 'GET', url: '/api/desk/session', headers: { host: '127.0.0.1:4317', origin: 'https://evil.example' } });
    expect(r.statusCode).toBe(200);
    expect(r.headers['access-control-allow-origin']).toBeUndefined();
    expect(r.headers['cache-control']).toBe('no-store');
    expect(r.json()).toMatchObject({ token: TOKEN, notesDir: path.join(root, 'notes') });
    await write('/api/desk/notes', { id: '20261109-101500-a1b2', text: 'hi', opened: true });
    const all = (d: string): string[] => readdirSync(d).flatMap((f) => (statSync(path.join(d, f)).isDirectory() ? all(path.join(d, f)) : [path.join(d, f)]));
    for (const f of all(dir)) expect(f.startsWith(root + path.sep), f).toBe(true);
  });

  it('reads a valid sheet and reports a broken one', async () => {
    const sheet = {
      title: 'Sunshine Desk',
      updated: '2026-11-09',
      goal: { start: 50, target: 10000 },
      nextActions: [{ title: 'Do a thing', why: 'Because' }],
      sections: [{ id: 't', title: 'Tracker', columns: ['#', 'Work'], rows: [['1', 'x']] }],
      links: [],
    };
    expect(isDeskSheet(sheet)).toBe(true);
    writeFileSync(path.join(root, 'sheet.json'), JSON.stringify(sheet));
    let p = (await app.inject({ method: 'GET', url: '/api/desk/state', headers: { host: '127.0.0.1:4317' } })).json() as DeskPayload;
    expect(p.sheet?.title).toBe('Sunshine Desk');
    writeFileSync(path.join(root, 'sheet.json'), JSON.stringify({ ...sheet, goal: { start: 0, target: 1 } }));
    p = (await app.inject({ method: 'GET', url: '/api/desk/state', headers: { host: '127.0.0.1:4317' } })).json() as DeskPayload;
    expect(p.sheet).toBeNull();
    expect(p.sheetError).toMatch(/format/);
  });
});
