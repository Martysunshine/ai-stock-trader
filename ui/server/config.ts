import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

/** ui/ */
export const UI_ROOT = path.resolve(here, '..');

/**
 * Only ui/.env.local is read. The repository's own .env holds the bot's read-write database
 * password and is deliberately never loaded (docs/observability.md section 1).
 */
const envFile = path.join(UI_ROOT, '.env.local');
if (existsSync(envFile)) process.loadEnvFile(envFile);

/**
 * The repository the console observes (it only ever reads below it). Defaults to the parent of
 * ui/; UI_REPO_ROOT points a second checkout (e.g. a git worktree) at the real runs/ and data/.
 */
export const REPO_ROOT = process.env.UI_REPO_ROOT?.trim()
  ? path.resolve(process.env.UI_REPO_ROOT.trim())
  : path.resolve(UI_ROOT, '..');

export const HOST = '127.0.0.1';
export const PORT = Number(process.env.UI_PORT ?? 4317);
export const DATABASE_URL = process.env.UI_DATABASE_URL?.trim() || null;

/** The stocks book when TRADER_PAPER_CONFIG is not set: the exploration book (ADR-0057). The same
 *  default as scripts/console-db.mjs; tests/console-db.test.ts keeps the two equal. */
export const DEFAULT_PAPER_CONFIG = 'configs/paper_explore.yaml';

/** A repository-relative paper configuration: a plain file below configs/, else the default. */
export function paperConfigPath(value: string | undefined): string {
  const v = (value ?? '').trim().replaceAll('\\', '/');
  return /^configs\/[A-Za-z0-9_]+\.yaml$/.test(v) ? v : DEFAULT_PAPER_CONFIG;
}

/**
 * The stocks book the console shows: its portfolio, policy, instruments and fees. `npm run dev`
 * passes the setting on (scripts/dev.mjs), and `npm run bot` runs the same file, so the pages
 * describe the bot that is trading.
 */
export const PAPER_CONFIG = paperConfigPath(process.env.TRADER_PAPER_CONFIG);

/** The paper account whose leadership lock signals a running worker: the first account_id in
 *  the stocks book's configuration, overridable. */
export const PAPER_ACCOUNT_ID = process.env.UI_PAPER_ACCOUNT_ID?.trim() || paperAccountId();
export const POLL_MS = Math.min(5000, Math.max(2000, Number(process.env.UI_POLL_MS ?? 2000)));

export const repoPath = (...parts: string[]) => path.join(REPO_ROOT, ...parts);

/** The first account_id in a paper configuration's text, or null. */
export function accountIdFromPaperYaml(text: string): string | null {
  const m = /^\s*(?:-\s+)?account_id:\s*([a-z0-9-]+)/m.exec(text);
  return m ? m[1] : null;
}

/** The first account_id in the stocks book's configuration; the exploration account when it
 *  cannot be read. */
function paperAccountId(): string {
  try {
    const id = accountIdFromPaperYaml(readFileSync(path.join(REPO_ROOT, PAPER_CONFIG), 'utf8'));
    if (id) return id;
  } catch {
    // fall through to the default
  }
  return 'acct-alpaca-paper-explore';
}
