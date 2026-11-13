// Which stocks book the console and `npm run bot` follow, and which database that is.
//
// One setting names the book's paper configuration: TRADER_PAPER_CONFIG, a path below configs/.
// Its default is the exploration book (ADR-0057, ADR-0064): from 2026-11-12 the Alpaca paper
// account belongs to it. That file names an environment variable (database.url_env) that holds
// the bot's database URL. The console must read that same database, or it shows an account that
// is not trading. Pure helpers for dev.mjs, bot.mjs and day.mjs: no I/O, so tests import them.

/** The stocks book when TRADER_PAPER_CONFIG is not set. The server has the same default
 *  (ui/server/config.ts); a test keeps the two equal. */
export const DEFAULT_PAPER_CONFIG = 'configs/paper_explore.yaml';

export const PRACTICE_URL_ENV = 'TRADER_PRACTICE_DATABASE_URL';
/** The practice database of ADR-0043, on the same server as the pilot database. */
export const PRACTICE_DATABASE = 'trader_practice';

/** A book's own database on the pilot's server, used when .env has no line for its variable. */
export const BOOK_DATABASES = {
  [PRACTICE_URL_ENV]: PRACTICE_DATABASE,
  TRADER_EXPLORE_DATABASE_URL: 'trader_explore',
};

/** The paper configuration to follow: the first candidate that is a plain file below configs/. */
export function paperConfigPath(...candidates) {
  for (const c of candidates) {
    const value = (c ?? '').trim().replaceAll('\\', '/');
    if (/^configs\/[A-Za-z0-9_]+\.yaml$/.test(value)) return value;
  }
  return DEFAULT_PAPER_CONFIG;
}

/** The first account alias in a paper configuration, or 'paper': names the bot's log file. */
export function paperAlias(paperYaml) {
  const m = /^\s*(?:-\s+)?alias:\s*([a-z0-9_-]+)/m.exec(paperYaml ?? '');
  return m ? m[1] : 'paper';
}

/** The environment variable a paper configuration reads its database URL from. */
export function paperUrlEnv(paperYaml) {
  const m = /^\s*url_env:\s*([A-Z][A-Z0-9_]*)\s*(?:#.*)?$/m.exec(paperYaml ?? '');
  return m ? m[1] : 'TRADER_DATABASE_URL';
}

/** The database name at the end of a PostgreSQL URL, or null. */
export function databaseOf(url) {
  const m = /^[a-z][a-z0-9+]*:\/\/[^/]+\/([A-Za-z0-9_]+)(?:\?.*)?$/i.exec((url ?? '').trim());
  return m ? m[1] : null;
}

/** The same URL, pointing at another database on the same server. */
export function withDatabase(url, db) {
  return url.trim().replace(/\/[A-Za-z0-9_]+(\?.*)?$/, (_all, query) => `/${db}${query ?? ''}`);
}

/** The database the paper bot writes: the console reads this one. */
export function botDatabase(paperYaml, env) {
  const name = paperUrlEnv(paperYaml);
  return databaseOf(env[name]) ?? BOOK_DATABASES[name] ?? env.TRADER_PG_DB ?? 'trader';
}

/**
 * The bot's database URL for a launcher. When .env has no line for a book's variable, the book's
 * own database on the pilot's server is used: never the pilot database itself (ADR-0043 and
 * ADR-0057 keep each book apart).
 */
export function botDatabaseUrl(paperYaml, env) {
  const name = paperUrlEnv(paperYaml);
  if (env[name]) return { name, url: env[name], derived: false };
  if (BOOK_DATABASES[name] && databaseOf(env.TRADER_DATABASE_URL)) {
    return { name, url: withDatabase(env.TRADER_DATABASE_URL, BOOK_DATABASES[name]), derived: true };
  }
  return { name, url: null, derived: false };
}

/** True when a process command line is a running paper bot (`trader paper run`), of any book. */
export function isPaperBot(commandLine) {
  return /\bpaper\s+run\b/.test(commandLine ?? '') && !/\bbot\.mjs\b|\bday\.mjs\b/.test(commandLine ?? '');
}

/** True when a process command line is the crypto and CFD practice trader. */
export function isPracticeTrader(commandLine) {
  return /trader\.practice\s+trade\b|practice-trader\.mjs/.test(commandLine ?? '');
}
