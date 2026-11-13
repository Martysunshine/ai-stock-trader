/**
 * Write protection for the Desk (on top of ../guard.ts, which keeps the bind on loopback and
 * refuses foreign Host headers, i.e. DNS rebinding).
 *
 * A write must carry an Origin that is exactly the console's own (a missing Origin is refused,
 * unlike the console's replay controls), a JSON body, and the per-process token from
 * GET /api/desk/session. Another site cannot read that token (no CORS headers), so it cannot
 * forge a note.
 */
import { randomBytes, timingSafeEqual } from 'node:crypto';

/** The Desk's old standalone address; the console redirects it to /desk. */
export const LEGACY_DESK_PORT = Number(process.env.UI_DESK_PORT ?? 4320);

export function deskOrigins(port: number): Set<string> {
  return new Set([`http://127.0.0.1:${port}`, `http://localhost:${port}`]);
}

export function isDeskOrigin(origin: string | undefined, allowed: Set<string>): boolean {
  return typeof origin === 'string' && allowed.has(origin.trim().toLowerCase());
}

export function newToken(): string {
  return randomBytes(24).toString('hex');
}

export function tokenMatches(given: string | string[] | undefined, token: string): boolean {
  if (typeof given !== 'string' || given.length !== token.length) return false;
  return timingSafeEqual(Buffer.from(given), Buffer.from(token));
}

export function isJson(contentType: string | undefined): boolean {
  return typeof contentType === 'string' && /^application\/json\s*(;|$)/i.test(contentType.trim());
}
