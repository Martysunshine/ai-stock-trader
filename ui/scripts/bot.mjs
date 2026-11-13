#!/usr/bin/env node
// `npm run bot`: start the paper bot deliberately (never started by `npm run dev`).
//
// Runs `trader paper run --config <the stocks book>` through uv; extra arguments are passed on,
// e.g. `npm run bot -- --practice` or `npm run bot -- --practice --sessions 1`. The book is
// TRADER_PAPER_CONFIG (shell or .env), by default the exploration book (console-db.mjs);
// `npm run bot -- --config configs/paper.yaml --practice` names another one for this run.
//
// Both stock books trade the one Alpaca paper account and each keeps its account lock in its own
// database, so they cannot see each other: this launcher refuses to start while any paper bot is
// already running (ADR-0057). The output is also appended to runs/paper/sessions/<alias>.log.
// Uses `uv` when it is on PATH, else `python -m uv`.
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { botDatabaseUrl, databaseOf, isPaperBot, paperAlias, paperConfigPath } from './console-db.mjs';
import { runningProcesses, workers } from './processes.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const argv = process.argv.slice(2);
const at = argv.indexOf('--config');
const named = at >= 0 ? argv[at + 1] : undefined;
const extra = at >= 0 ? [...argv.slice(0, at), ...argv.slice(at + 2)] : argv;

// The trader CLI does not read .env: hand it the TRADER_* settings (never printed). The shell's
// own values win. Without a line for the book's database in .env, the book's own database on the
// same server is used, never the pilot database (ADR-0043, ADR-0057).
const fromDotenv = {};
const dotenv = path.join(REPO, '.env');
if (existsSync(dotenv)) {
  for (const l of readFileSync(dotenv, 'utf8').split(/\r?\n/)) {
    const m = l.match(/^\s*(TRADER_[A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m && m[2]) fromDotenv[m[1]] = m[2];
  }
}
const fromShell = Object.fromEntries(Object.entries(process.env).filter(([k, v]) => k.startsWith('TRADER_') && v));
const settings = { ...fromDotenv, ...fromShell };
const config = paperConfigPath(named, settings.TRADER_PAPER_CONFIG);
if (named && config !== named.replaceAll('\\', '/')) {
  console.error(`\n  --config must be a file below configs/ (got ${named}).\n`);
  process.exit(2);
}
let paperYaml = '';
try {
  paperYaml = readFileSync(path.join(REPO, config), 'utf8');
} catch {
  // the CLI reports a missing configuration itself
}

const already = workers(runningProcesses()).filter((p) => isPaperBot(p.cmd));
if (already.length) {
  console.error('\n  A paper bot is already running (both stock books trade one Alpaca paper account):');
  for (const p of already.slice(0, 3)) console.error(`    pid ${p.pid}  ${p.cmd.slice(0, 140)}`);
  console.error('  Stop it with Ctrl+C in its window first.\n');
  process.exit(1);
}

const target = botDatabaseUrl(paperYaml, settings);
if (target.url) settings[target.name] = target.url;
const alias = paperAlias(paperYaml);
const logDir = path.join(REPO, 'runs', 'paper', 'sessions');
const logFile = path.join(logDir, `${alias}.log`);
mkdirSync(logDir, { recursive: true });
const uv = spawnSync('uv --version', { shell: true, stdio: 'ignore' }).status === 0 ? 'uv' : 'python -m uv';
const line = `${uv} run trader paper run --config ${config} ${extra.map((a) => JSON.stringify(a)).join(' ')}`.trim();
if (process.stdout.isTTY) process.stdout.write(`\x1b]0;Paper bot: ${alias} book - keep this window open (Ctrl+C stops it)\x07`);
console.log(`\n  Paper bot: ${line}`);
if (target.url) console.log(`  Database:  ${databaseOf(target.url)}${target.derived ? ` (no ${target.name} line in .env: the book's own database on the same server)` : ''}`);
console.log(`  Log:       ${path.relative(REPO, logFile)}`);
console.log('  (Ctrl+C stops it gracefully; closing this window stops it too. The console keeps running.)\n');

const child = spawn(line, {
  cwd: REPO,
  shell: true,
  stdio: ['inherit', 'pipe', 'pipe'],
  // HTTPS scanning on some hosts breaks uv downloads without the system certificate store.
  env: { UV_SYSTEM_CERTS: '1', PYTHONUTF8: '1', PYTHONUNBUFFERED: '1', ...process.env, ...settings },
});
const tee = (stream, out) =>
  stream.on('data', (chunk) => {
    out.write(chunk);
    try {
      appendFileSync(logFile, chunk);
    } catch {
      // a locked log file must never stop the bot
    }
  });
tee(child.stdout, process.stdout);
tee(child.stderr, process.stderr);
// Ctrl+C reaches the bot directly (same console): wait for its graceful stop instead of dying first.
process.on('SIGINT', () => {});
child.on('exit', (code) => process.exit(code ?? 0));
