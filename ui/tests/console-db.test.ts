/**
 * The console must read the database the paper bot writes (scripts/console-db.mjs). On 10 Nov 2026
 * the bot moved to the practice database while `npm run dev` still pointed the console at the
 * pilot database: the console would have shown an account that was not trading.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
// @ts-expect-error plain Node module shared with scripts/dev.mjs (no type declarations)
import { DEFAULT_PAPER_CONFIG, botDatabase, botDatabaseUrl, databaseOf, isPaperBot, isPracticeTrader, paperAlias, paperConfigPath, paperUrlEnv, withDatabase } from '../scripts/console-db.mjs';
import { DEFAULT_PAPER_CONFIG as SERVER_DEFAULT, paperConfigPath as serverPaperConfigPath } from '../server/config';

const PILOT = 'postgresql+psycopg://trader:s3cret@127.0.0.1:5433/trader';
const PRACTICE_YAML = ['database:', '  backend: postgresql', '  # a comment', '  url_env: TRADER_PRACTICE_DATABASE_URL', ''].join('\n');
const PILOT_YAML = ['database:', '  url_env: TRADER_DATABASE_URL  # the pilot', ''].join('\n');

describe('which database the console reads', () => {
  it('reads the variable name from the paper configuration', () => {
    expect(paperUrlEnv(PRACTICE_YAML)).toBe('TRADER_PRACTICE_DATABASE_URL');
    expect(paperUrlEnv(PILOT_YAML)).toBe('TRADER_DATABASE_URL');
    expect(paperUrlEnv('')).toBe('TRADER_DATABASE_URL');
  });

  it('the repository paper configuration names the practice database variable', () => {
    const yaml = readFileSync(path.resolve(__dirname, '..', '..', 'configs', 'paper.yaml'), 'utf8');
    expect(paperUrlEnv(yaml)).toBe('TRADER_PRACTICE_DATABASE_URL');
  });

  it('takes the database name from a URL', () => {
    expect(databaseOf(PILOT)).toBe('trader');
    expect(databaseOf('postgresql://trader_ro:pw@127.0.0.1:5433/trader_practice?sslmode=disable')).toBe('trader_practice');
    expect(databaseOf('')).toBeNull();
    expect(databaseOf(undefined)).toBeNull();
    expect(databaseOf('not a url')).toBeNull();
  });

  it('moves a URL to another database and keeps everything else', () => {
    expect(withDatabase('postgresql://trader_ro:pw@127.0.0.1:5433/trader', 'trader_practice')).toBe(
      'postgresql://trader_ro:pw@127.0.0.1:5433/trader_practice',
    );
    expect(withDatabase('postgresql://u:p@h:1/trader?sslmode=disable', 'x')).toBe('postgresql://u:p@h:1/x?sslmode=disable');
  });

  it('follows the practice line in .env when it exists', () => {
    const env = { TRADER_DATABASE_URL: PILOT, TRADER_PRACTICE_DATABASE_URL: PILOT.replace(/trader$/, 'my_practice') };
    expect(botDatabase(PRACTICE_YAML, env)).toBe('my_practice');
    expect(botDatabaseUrl(PRACTICE_YAML, env)).toEqual({ name: 'TRADER_PRACTICE_DATABASE_URL', url: env.TRADER_PRACTICE_DATABASE_URL, derived: false });
  });

  it('without a practice line uses the practice database, never the pilot database', () => {
    const env = { TRADER_DATABASE_URL: PILOT, TRADER_PG_DB: 'trader' };
    expect(botDatabase(PRACTICE_YAML, env)).toBe('trader_practice');
    const target = botDatabaseUrl(PRACTICE_YAML, env);
    expect(target.derived).toBe(true);
    expect(target.url).toBe('postgresql+psycopg://trader:s3cret@127.0.0.1:5433/trader_practice');
    expect(databaseOf(target.url)).not.toBe('trader');
  });

  it('a pilot configuration reads the pilot database', () => {
    expect(botDatabase(PILOT_YAML, { TRADER_DATABASE_URL: PILOT })).toBe('trader');
    expect(botDatabase(PILOT_YAML, { TRADER_PG_DB: 'other' })).toBe('other');
    expect(botDatabase(PILOT_YAML, {})).toBe('trader');
    expect(botDatabaseUrl(PILOT_YAML, {})).toEqual({ name: 'TRADER_DATABASE_URL', url: null, derived: false });
  });
});

/**
 * From 12 Nov 2026 the Alpaca paper account belongs to the exploration book (ADR-0057, ADR-0064).
 * The console and `npm run bot` follow one setting, so the pages show the bot that trades.
 */
describe('which stocks book the console and the bot follow', () => {
  const repoFile = (rel: string) => readFileSync(path.resolve(__dirname, '..', '..', rel), 'utf8');

  it('defaults to the exploration book, in the scripts and in the server alike', () => {
    expect(DEFAULT_PAPER_CONFIG).toBe('configs/paper_explore.yaml');
    expect(SERVER_DEFAULT).toBe(DEFAULT_PAPER_CONFIG);
    expect(paperConfigPath()).toBe(DEFAULT_PAPER_CONFIG);
    expect(paperConfigPath(undefined, '', '  ')).toBe(DEFAULT_PAPER_CONFIG);
    expect(serverPaperConfigPath(undefined)).toBe(DEFAULT_PAPER_CONFIG);
  });

  it('takes the first plain file below configs/ and nothing else', () => {
    expect(paperConfigPath('configs/paper.yaml', 'configs/paper_explore.yaml')).toBe('configs/paper.yaml');
    expect(paperConfigPath(undefined, 'configs\\paper.yaml')).toBe('configs/paper.yaml');
    expect(paperConfigPath('../secrets.yaml', 'configs/policy/base.yaml', 'C:/x/paper.yaml')).toBe(DEFAULT_PAPER_CONFIG);
    expect(serverPaperConfigPath('configs/paper.yaml')).toBe('configs/paper.yaml');
    expect(serverPaperConfigPath('configs/../.env')).toBe(DEFAULT_PAPER_CONFIG);
  });

  it('the exploration book has its own database, never the pilot or the practice one', () => {
    const yaml = repoFile(DEFAULT_PAPER_CONFIG);
    expect(paperUrlEnv(yaml)).toBe('TRADER_EXPLORE_DATABASE_URL');
    expect(paperAlias(yaml)).toBe('explore');
    const env = { TRADER_DATABASE_URL: PILOT, TRADER_PG_DB: 'trader' };
    expect(botDatabase(yaml, env)).toBe('trader_explore');
    const target = botDatabaseUrl(yaml, env);
    expect(target).toEqual({ name: 'TRADER_EXPLORE_DATABASE_URL', url: 'postgresql+psycopg://trader:s3cret@127.0.0.1:5433/trader_explore', derived: true });
    const own = { ...env, TRADER_EXPLORE_DATABASE_URL: PILOT.replace(/trader$/, 'elsewhere') };
    expect(botDatabase(yaml, own)).toBe('elsewhere');
    expect(botDatabaseUrl(yaml, own).derived).toBe(false);
  });

  it('names the log file after the account alias', () => {
    expect(paperAlias(repoFile('configs/paper.yaml'))).toBe('practice');
    expect(paperAlias('')).toBe('paper');
  });

  it('recognises a running paper bot of either book, and never a launcher', () => {
    expect(isPaperBot('"C:\\repo\\.venv\\Scripts\\trader.exe" paper run --config configs/paper_explore.yaml --practice')).toBe(true);
    expect(isPaperBot('python -m uv run trader paper run --config configs/paper.yaml --practice --sessions 1')).toBe(true);
    expect(isPaperBot('node ui/scripts/bot.mjs --practice')).toBe(false);
    expect(isPaperBot('python -u -m trader.practice trade --live')).toBe(false);
    expect(isPaperBot('uv run trader paper day --config configs/paper_explore.yaml')).toBe(false);
    expect(isPaperBot(undefined)).toBe(false);
  });

  it('recognises the practice trader by its module or its launcher', () => {
    expect(isPracticeTrader('".venv\\Scripts\\python.exe" -u -m trader.practice trade --live')).toBe(true);
    expect(isPracticeTrader('node  ui\\scripts\\practice-trader.mjs')).toBe(true);
    expect(isPracticeTrader('python -m trader.practice watch --every 15')).toBe(false);
    expect(isPracticeTrader('python -m trader.market watch --every 5')).toBe(false);
  });
});
