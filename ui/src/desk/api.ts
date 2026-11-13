import type { DeskPayload, DeskSession } from '../../shared/desk';

export async function getSession(): Promise<DeskSession> {
  const r = await fetch('/api/desk/session', { cache: 'no-store' });
  if (!r.ok) throw new Error(`Desk server answered ${r.status}`);
  return (await r.json()) as DeskSession;
}

export async function getDesk(): Promise<DeskPayload> {
  const r = await fetch('/api/desk/state', { cache: 'no-store' });
  if (!r.ok) throw new Error(`Desk server answered ${r.status}`);
  return (await r.json()) as DeskPayload;
}

function post(session: DeskSession, url: string, body: unknown): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Desk-Token': session.token },
    body: JSON.stringify(body),
    keepalive: true, // the page may hand focus to VS Code while this is in flight
  });
}

export function postNote(session: DeskSession, id: string, text: string): Promise<Response> {
  return post(session, '/api/desk/notes', { id, text, opened: true });
}

export function postOpened(session: DeskSession, id: string): void {
  void post(session, `/api/desk/notes/${id}/opened`, {}).catch(() => undefined);
}

/** Calls back when anything under .desk/ changes (a new note, Claude's reply, the sheet). */
export function subscribe(onChange: () => void): () => void {
  const es = new EventSource('/api/desk/stream');
  es.addEventListener('changed', onChange);
  return () => es.close();
}

export function randomHex4(): string {
  return crypto.getRandomValues(new Uint16Array(1))[0].toString(16).padStart(4, '0');
}
