#!/usr/bin/env node
// One command for the whole console: `npm run dev` at the repository root (or `pnpm run dev` in ui/).
//
//   1. Docker running (starts Docker Desktop on Windows/macOS when it is not)
//   2. Postgres up and healthy (docker compose up -d postgres)
//   3. UI dependencies installed (only when node_modules is missing or older than the lockfile)
//   4. Read-only database role for the console, on the database the paper bot writes (the stocks
//      book of TRADER_PAPER_CONFIG, by default configs/paper_explore.yaml); writes ui/.env.local
//      on the first run and when that database changes
//   5. The console: API, Sunshine Desk and the page on ONE address, http://127.0.0.1:4317
//   6. The market feed for the Markets page (`python -m trader.market watch --every 5`), once the
//      console answers: it writes data/market/ every 5 minutes (ADR-0045)
//   7. The intel feed (`python -m trader.intel watch`): news, insider filings and pre-market
//      activity into data/intel/ (ADR-0047)
//   8. The practice watch (`python -m trader.practice watch --every 15`): the index dip
//      scan and every open practice trade's stop, target and time stop into
//      runs/practice/scan.json (ADR-0049)
//
// Ctrl+C stops the console, both feeds and the watch (their whole process trees). Postgres keeps
// running: `npm run db:stop`.
// Flags: --prod (build, then serve the build) · --no-docker (UI only; replays need no database)
//        --no-open · --open=/desk · --no-market (no market feed) · --no-intel (no intel feed)
//        --no-practice (no practice watch) · --setup (steps 1-4 only)
//        --db-up · --db-stop
// Plain Node, no dependencies. The paper bot is never started here (`npm run bot`). The market
// feed is a data tool with no order authority: it reads prices and writes data/market/ only.
// The intel feed is the same kind of tool: it reads news, filings and pre-market bars and writes
// data/intel/ only; nothing that decides a trade reads it. The practice watch reads the market
// feed's files and the practice journals and writes runs/practice/ only: it places no order.
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { botDatabase, databaseOf, paperConfigPath, withDatabase } from './console-db.mjs';

const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const has = (name) => args.includes(`--${name}`);
const value = (name) => args.find((a) => a.startsWith(`--${name}=`))?.slice(name.length + 3);
const isWin = process.platform === 'win32';
const PORT = Number(process.env.UI_PORT ?? 4317);
const BASE = `http://127.0.0.1:${PORT}`;
const OPEN_PATH = value('open') ?? '/';
const PROD = has('prod');
const NO_DOCKER = has('no-docker');
const NO_OPEN = has('no-open') || Boolean(process.env.CI);
const NO_MARKET = has('no-market');
const NO_INTEL = has('no-intel');
const NO_PRACTICE = has('no-practice');
const CONTAINER = 'ai-stock-trader-pg';

// ------------------------------------------------------------------------------------- output
const tty = process.stdout.isTTY;
const c = (code) => (s) => (tty ? `\x1b[${code}m${s}\x1b[0m` : s);
const dim = c('2');
const bold = c('1');
const green = c('32');
const red = c('31');
const yellow = c('33');
const blue = c('34');
const ok = (msg, note = '') => console.log(`  ${green('✓')} ${msg}${note ? dim(`  ${note}`) : ''}`);
const step = (msg) => console.log(`  ${dim('…')} ${msg}`);
const warn = (msg) => console.log(`  ${yellow('!')} ${msg}`);
function fail(msg, hint) {
  console.log(`\n  ${red('✗')} ${bold(msg)}`);
  if (hint) console.log(`    ${hint.split('\n').join('\n    ')}`);
  console.log('');
  process.exit(1);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------------------------- helpers
function readEnvFile(file) {
  if (!existsSync(file)) return {};
  const out = {};
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2];
  }
  return out;
}

/**
 * The repository whose database, .env and data the console observes: the parent of ui/, or
 * UI_REPO_ROOT (set in ui/.env.local by a second checkout such as a git worktree). Docker Compose
 * runs there, so every checkout drives the same Postgres container and project.
 */
const REPO = path.resolve(process.env.UI_REPO_ROOT?.trim() || readEnvFile(path.join(UI, '.env.local')).UI_REPO_ROOT || path.join(UI, '..'));

/**
 * The stocks book the console shows: TRADER_PAPER_CONFIG from the shell, the repository's .env or
 * ui/.env.local, else the default of console-db.mjs (the exploration book). `npm run bot` follows
 * the same setting, so the console reads the database the bot writes.
 */
const PAPER_CONFIG = paperConfigPath(
  process.env.TRADER_PAPER_CONFIG,
  readEnvFile(path.join(REPO, '.env')).TRADER_PAPER_CONFIG,
  readEnvFile(path.join(UI, '.env.local')).TRADER_PAPER_CONFIG,
);

const run = (cmd, argv, opts = {}) => spawnSync(cmd, argv, { cwd: REPO, encoding: 'utf8', windowsHide: true, ...opts });
/** pnpm is a .cmd shim on Windows: run it through the shell. */
const shell = (line, opts = {}) => spawnSync(line, { cwd: UI, encoding: 'utf8', shell: true, windowsHide: true, ...opts });

function get(url, timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout }, (res) => {
      let body = '';
      res.on('data', (d) => (body += d));
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });
    req.on('error', (e) => resolve({ error: e.code ?? String(e) }));
    req.on('timeout', () => req.destroy(Object.assign(new Error('timeout'), { code: 'TIMEOUT' })));
  });
}

function openBrowser(url) {
  if (NO_OPEN) return;
  const opts = { detached: true, stdio: 'ignore', windowsHide: true };
  if (isWin) spawn('cmd', ['/c', 'start', '""', url], opts).unref();
  else if (process.platform === 'darwin') spawn('open', [url], opts).unref();
  else spawn('xdg-open', [url], opts).unref();
}

// ------------------------------------------------------------------------------------- docker
/** The compose file (root today; deploy/ if the repository layout moves it). */
function composeArgs() {
  if (existsSync(path.join(REPO, 'docker-compose.yml'))) return ['compose'];
  if (existsSync(path.join(REPO, 'deploy', 'docker-compose.yml'))) {
    return ['compose', '-f', path.join('deploy', 'docker-compose.yml'), '--env-file', '.env'];
  }
  fail('No docker-compose.yml found', 'Expected it at the repository root.');
}

const dockerUp = () => run('docker', ['info', '--format', '{{.ServerVersion}}'], { timeout: 20_000 }).status === 0;

async function ensureDocker() {
  if (run('docker', ['--version']).error) {
    fail('Docker is not installed', 'Install Docker Desktop, or start without the database: npm run dev -- --no-docker');
  }
  if (dockerUp()) return ok('Docker is running');
  const desktop = isWin
    ? path.join(process.env.ProgramFiles ?? 'C:\\Program Files', 'Docker', 'Docker', 'Docker Desktop.exe')
    : process.platform === 'darwin'
      ? '/Applications/Docker.app'
      : null;
  if (desktop && existsSync(desktop)) {
    step('Starting Docker Desktop (this can take a minute)');
    if (isWin) spawn(desktop, [], { detached: true, stdio: 'ignore' }).unref();
    else spawn('open', ['-a', 'Docker'], { detached: true, stdio: 'ignore' }).unref();
  } else {
    step('Waiting for the Docker engine');
  }
  for (let i = 0; i < 90; i++) {
    await sleep(2000);
    if (dockerUp()) return ok('Docker is running', 'started');
  }
  fail('Docker did not start within 3 minutes', 'Start Docker Desktop yourself, then run npm run dev again.');
}

async function ensurePostgres() {
  if (!existsSync(path.join(REPO, '.env'))) {
    fail('The database needs .env', 'Copy .env.example to .env and choose a local database password (CONTRIBUTING.md).');
  }
  const up = run('docker', [...composeArgs(), 'up', '-d', 'postgres']);
  if (up.status !== 0) fail('docker compose up failed', (up.stderr || up.stdout || '').trim().split('\n').slice(-6).join('\n'));
  for (let i = 0; i < 60; i++) {
    const health = run('docker', ['inspect', '-f', '{{.State.Health.Status}}', CONTAINER]).stdout?.trim();
    if (health === 'healthy') return ok('Postgres is healthy', `127.0.0.1:${readEnvFile(path.join(REPO, '.env')).TRADER_PG_PORT ?? 5433}`);
    if (i === 3) step('Waiting for Postgres to accept connections');
    await sleep(1000);
  }
  fail('Postgres did not become healthy', `Check it with: docker logs ${CONTAINER}`);
}

// ------------------------------------------------------------------------------------- deps
function pnpmCommand() {
  if (shell('pnpm --version').status === 0) return 'pnpm';
  if (shell('corepack pnpm --version', { env: { ...process.env, COREPACK_ENABLE_DOWNLOAD_PROMPT: '0' } }).status === 0) return 'corepack pnpm';
  fail('pnpm is not available', 'Install it once with: npm install -g pnpm');
}

function ensureDeps() {
  const marker = path.join(UI, 'node_modules', '.modules.yaml');
  const lock = path.join(UI, 'pnpm-lock.yaml');
  const fresh = existsSync(marker) && statSync(marker).mtimeMs >= statSync(lock).mtimeMs;
  if (fresh) return ok('UI dependencies are installed');
  step('Installing UI dependencies (pnpm install)');
  const r = shell(`${pnpmCommand()} install --frozen-lockfile`, { stdio: 'inherit', env: { ...process.env, COREPACK_ENABLE_DOWNLOAD_PROMPT: '0' } });
  if (r.status !== 0) fail('pnpm install failed', 'See the output above.');
  ok('UI dependencies installed');
}

// ------------------------------------------------------------------------------------- read-only role
/**
 * The console reads the bot's database through a SELECT-only role (docs/observability.md §4).
 * Created once; the random password goes to psql on stdin (never on a command line) and is stored
 * only in ui/.env.local (gitignored). The bot's own .env is only read for the port and db name.
 */
/** True when this run pointed the console at another database: a running console must restart. */
let databaseChanged = false;

function ensureReadOnlyRole() {
  const envLocalPath = path.join(UI, '.env.local');
  const env = readEnvFile(path.join(REPO, '.env'));
  const port = env.TRADER_PG_PORT ?? '5433';
  let paperYaml = '';
  try {
    paperYaml = readFileSync(path.join(REPO, PAPER_CONFIG), 'utf8');
  } catch {
    // no paper configuration: the pilot database
  }
  // The console reads the database the paper bot writes, or it shows an account that is not trading.
  const db = botDatabase(paperYaml, env);
  if (!/^[A-Za-z0-9_]+$/.test(db)) return warn(`Unexpected database name "${db}"; the read-only role is unchanged.`);
  const current = readEnvFile(envLocalPath).UI_DATABASE_URL;
  const currentDb = databaseOf(current);
  if (current && currentDb === db) return ok('Read-only database role', `database ${db} (${PAPER_CONFIG})`);
  // The role is server-wide: moving to another database keeps its password and adds the grants.
  const moving = Boolean(current && currentDb);
  step(moving ? `Pointing the console at the bot's database ${db} (it was reading ${currentDb})` : 'Creating the read-only database role (first run)');
  const password = moving ? null : randomBytes(24).toString('base64url');
  const psql = (database, sql) =>
    run(
      'docker',
      [...composeArgs(), 'exec', '-T', 'postgres', 'sh', '-c', 'psql -v ON_ERROR_STOP=1 -q -v db="$0" -v owner="$POSTGRES_USER" -U "$POSTGRES_USER" -d "${0:-$POSTGRES_DB}"', database],
      { input: sql },
    );
  const lastLine = (r) => (r.stderr || '').trim().split('\n').pop() || `exit ${r.status}`;
  // The bot's database may not exist yet on a fresh machine: create it empty (the bot migrates it).
  const created = psql('', `SELECT 'CREATE DATABASE ${db}' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db}')\\gexec\n`);
  if (created.status !== 0) {
    warn(`Could not prepare the database ${db} (${lastLine(created)}).`);
    warn('The console still starts; live data shows how to connect it.');
    return;
  }
  const sql = [
    "SELECT 'CREATE ROLE trader_ro LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trader_ro')\\gexec",
    ...(password ? [`ALTER ROLE trader_ro WITH LOGIN PASSWORD '${password}';`] : []),
    'ALTER ROLE trader_ro SET default_transaction_read_only = on;',
    'GRANT CONNECT ON DATABASE :"db" TO trader_ro;',
    'GRANT USAGE ON SCHEMA public TO trader_ro;',
    'GRANT SELECT ON ALL TABLES IN SCHEMA public TO trader_ro;',
    'ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT SELECT ON TABLES TO trader_ro;',
    '',
  ].join('\n');
  const r = psql(db, sql);
  if (r.status !== 0) {
    warn(`Could not ${moving ? `grant the read-only role on ${db}` : 'create the read-only role'} (${lastLine(r)}).`);
    warn(moving ? `The console keeps reading ${currentDb}.` : 'The console still starts; live data shows how to connect it.');
    return;
  }
  const kept = existsSync(envLocalPath)
    ? readFileSync(envLocalPath, 'utf8').split(/\r?\n/).filter((l) => l.trim() && !/^\s*UI_DATABASE_URL\s*=/.test(l))
    : ['# Local and gitignored. Read-only role for the console (docs/observability.md section 4).'];
  kept.push(`UI_DATABASE_URL=${moving ? withDatabase(current, db) : `postgresql://trader_ro:${password}@127.0.0.1:${port}/${db}`}`);
  writeFileSync(envLocalPath, kept.join('\n') + '\n', 'utf8');
  databaseChanged = true;
  ok(moving ? `The console now reads the bot's database ${db}` : 'Read-only database role created', 'ui/.env.local');
}

// ------------------------------------------------------------------------------------- the console
async function portState() {
  const r = await get(`${BASE}/api/health`);
  if (r.error === 'ECONNREFUSED') return 'free';
  if (r.status === 200 && r.body.includes('"trader-console"')) return 'ours';
  if (r.status === 200 && r.body.includes('"ok":true')) return 'ours';
  return 'taken';
}

function tsxCli() {
  try {
    return createRequire(path.join(UI, 'package.json')).resolve('tsx/cli');
  } catch {
    fail('tsx is missing from ui/node_modules', 'Run: npm run setup');
  }
}

function killTree(child) {
  if (!child || child.exitCode !== null) return;
  if (isWin) spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
  else {
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch {
      child.kill('SIGTERM');
    }
  }
}

/** Relay a child's output line by line with a prefix. */
function relayLines(stream, out, prefix) {
  let buf = '';
  stream.on('data', (d) => {
    buf += d;
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() ?? '';
    for (const line of lines) if (line.trim()) out.write(prefix + line + '\n');
  });
}

/**
 * The market feed (ADR-0045): one resident Python child in the observed repository, refreshing
 * data/market/ every 5 minutes for the Markets page. Prices only; it holds no order authority, and
 * the console itself still makes no outbound call. Its keys stay in that process.
 */
function startMarketFeed() {
  if (NO_MARKET) {
    warn('Market feed skipped (--no-market): the Markets page shows the last files in data/market/.');
    return null;
  }
  const argv = ['-m', 'uv', 'run', '--frozen', '--quiet', 'python', '-m', 'trader.market', 'watch', '--every', '5'];
  const feed = spawn('python', argv, {
    cwd: REPO,
    env: { ...process.env, UV_SYSTEM_CERTS: '1', PYTHONUTF8: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: !isWin, // own process group on POSIX so killTree stops uv and Python together
    windowsHide: true,
  });
  const prefix = dim('  market  │ ');
  relayLines(feed.stdout, process.stdout, prefix);
  relayLines(feed.stderr, process.stderr, prefix);
  feed.on('error', (e) => warn(`Market feed not started (${e.code ?? e.message}). Run it yourself: python -m uv run python -m trader.market watch --every 5`));
  feed.on('exit', (code, signal) => {
    if (code !== null && code !== 0) warn(`The market feed exited (code ${code}); the Markets page keeps its last files.`);
    else if (signal === null && code === 0) step('The market feed stopped.');
  });
  ok('Market feed started', 'data/market/ every 5 min');
  return feed;
}

/**
 * The intel feed (ADR-0047): one resident Python child in the observed repository, reading news,
 * insider filings and pre-market bars into data/intel/ at each source's own cadence. Information
 * only; it holds no order authority, nothing that decides a trade reads its files, and the console
 * itself still makes no outbound call. Its keys and the SEC contact stay in that process.
 */
function startIntelFeed() {
  if (NO_INTEL) {
    warn('Intel feed skipped (--no-intel): the console shows the last files in data/intel/.');
    return null;
  }
  const argv = ['-m', 'uv', 'run', '--frozen', '--quiet', 'python', '-m', 'trader.intel', 'watch'];
  const feed = spawn('python', argv, {
    cwd: REPO,
    env: { ...process.env, UV_SYSTEM_CERTS: '1', PYTHONUTF8: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: !isWin, // own process group on POSIX so killTree stops uv and Python together
    windowsHide: true,
  });
  const prefix = dim('  intel   │ ');
  relayLines(feed.stdout, process.stdout, prefix);
  relayLines(feed.stderr, process.stderr, prefix);
  feed.on('error', (e) => warn(`Intel feed not started (${e.code ?? e.message}). Run it yourself: python -m uv run python -m trader.intel watch`));
  feed.on('exit', (code, signal) => {
    if (code !== null && code !== 0) warn(`The intel feed exited (code ${code}); the console keeps its last files.`);
    else if (signal === null && code === 0) step('The intel feed stopped.');
  });
  ok('Intel feed started', 'data/intel/ news, insider filings, pre-market');
  return feed;
}

/**
 * The practice watch (ADR-0049): one resident Python child in the observed repository that reads
 * the market feed's daily bars and the practice journals every 15 minutes and writes
 * runs/practice/scan.json: the index dip signal and each open practice trade against its stop,
 * target and time stop. It places and journals nothing and holds no keys.
 */
function startPracticeWatch() {
  if (NO_PRACTICE) {
    warn('Practice watch skipped (--no-practice): the console shows the last runs/practice/scan.json.');
    return null;
  }
  const argv = ['-m', 'uv', 'run', '--frozen', '--quiet', 'python', '-m', 'trader.practice', 'watch', '--every', '15'];
  const watch = spawn('python', argv, {
    cwd: REPO,
    env: { ...process.env, PYTHONUTF8: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: !isWin, // own process group on POSIX so killTree stops uv and Python together
    windowsHide: true,
  });
  const prefix = dim('  practice│ ');
  relayLines(watch.stdout, process.stdout, prefix);
  relayLines(watch.stderr, process.stderr, prefix);
  watch.on('error', (e) => warn(`Practice watch not started (${e.code ?? e.message}). Run it yourself: python -m uv run python -m trader.practice watch`));
  watch.on('exit', (code, signal) => {
    if (code !== null && code !== 0) warn(`The practice watch exited (code ${code}); the console keeps the last scan.`);
    else if (signal === null && code === 0) step('The practice watch stopped.');
  });
  ok('Practice watch started', 'runs/practice/scan.json every 15 min');
  return watch;
}

async function startConsole() {
  const state = await portState();
  if (state === 'ours') {
    ok('The console is already running', BASE);
    if (databaseChanged) warn('It still reads the old database. Restart it: press Ctrl+C in its window, then run npm run dev again.');
    openBrowser(BASE + OPEN_PATH);
    process.exit(0);
  }
  if (state === 'taken') {
    fail(`Port ${PORT} is taken by another program`, isWin ? `Find it with: netstat -ano | findstr :${PORT}` : `Find it with: lsof -i :${PORT}`);
  }

  if (PROD) {
    step('Building the page (vite build)');
    const b = shell(`${pnpmCommand()} run build`, { stdio: 'inherit' });
    if (b.status !== 0) fail('The build failed', 'See the output above.');
  }

  const argv = PROD ? [tsxCli(), 'server/index.ts', '--serve-dist'] : [tsxCli(), 'watch', '--clear-screen=false', 'server/index.ts', '--dev'];
  const child = spawn(process.execPath, argv, {
    cwd: UI,
    env: { ...process.env, TRADER_PAPER_CONFIG: PAPER_CONFIG, NODE_OPTIONS: `${process.env.NODE_OPTIONS ?? ''} --disable-warning=ExperimentalWarning`.trim() },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: !isWin, // own process group on POSIX so Ctrl+C can stop the whole tree
    windowsHide: true,
  });
  const prefix = dim('  console │ ');
  relayLines(child.stdout, process.stdout, prefix);
  relayLines(child.stderr, process.stderr, prefix);

  let market = null;
  let intel = null;
  let practice = null;
  let stopping = false;
  const stop = (code = 0) => {
    if (stopping) return;
    stopping = true;
    killTree(child);
    killTree(market);
    killTree(intel);
    killTree(practice);
    console.log(`\n  ${dim('Stopped the console.')} ${NO_DOCKER ? '' : dim('Postgres keeps running (npm run db:stop).')}\n`);
    process.exit(code);
  };
  process.on('SIGINT', () => stop(0));
  process.on('SIGTERM', () => stop(0));
  process.on('SIGHUP', () => stop(0)); // the terminal window was closed
  process.on('exit', () => {
    killTree(child);
    killTree(market);
    killTree(intel);
    killTree(practice);
  });
  child.on('exit', (code) => {
    if (!stopping) {
      console.log(`\n  ${red('✗')} The console exited (code ${code}).`);
      process.exit(code ?? 1);
    }
  });

  for (let i = 0; i < 120; i++) {
    await sleep(500);
    if ((await portState()) === 'ours') {
      console.log('');
      console.log(`  ${bold('Trader Console')}  ${blue(BASE)}${PROD ? dim('  (production build)') : dim('  (hot reload)')}`);
      console.log(`  ${dim('Desk')}            ${blue(`${BASE}/desk`)}`);
      console.log(`  ${dim(NO_DOCKER ? 'Ctrl+C stops the console.' : 'Ctrl+C stops the console; Postgres keeps running (npm run db:stop).')}\n`);
      openBrowser(BASE + OPEN_PATH);
      market = startMarketFeed();
      intel = startIntelFeed();
      practice = startPracticeWatch();
      return;
    }
  }
  warn('The console has not answered after 60 s; still waiting (see its output above).');
}

// ------------------------------------------------------------------------------------- main
console.log(`\n  ${bold('ai-stock-trader')} ${dim('· one command for the console')}\n`);

if (has('db-stop')) {
  const r = run('docker', [...composeArgs(), 'stop', 'postgres'], { stdio: 'inherit' });
  process.exit(r.status ?? 1);
}

if (!NO_DOCKER) {
  await ensureDocker();
  await ensurePostgres();
}
if (has('db-up')) process.exit(0);
ensureDeps();
if (!NO_DOCKER) ensureReadOnlyRole();
else warn('Skipping Docker (--no-docker): live data is off, replays work.');
if (has('setup')) {
  ok('Setup complete', 'start with: npm run dev');
  process.exit(0);
}
await startConsole();
