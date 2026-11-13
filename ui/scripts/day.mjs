#!/usr/bin/env node
// `npm run day`: bring the trading day up, and nothing twice.
//
//   1. Docker and Postgres (the same steps as `npm run db:up`)
//   2. The console (`npm run dev -- --no-open`) when nothing answers on its address
//   3. The stocks paper bot (`npm run bot -- --practice`) when no paper bot is running
//   4. The crypto and CFD practice trader (`npm run trader`) when neither it nor its launcher runs
//
// Each missing piece opens in its own visible window, so the owner sees it and Ctrl+C stops it.
// This script never stops, restarts or signals anything: a piece that is already up is left alone.
// Flags: --dry-run (say what would start, start nothing) · --no-console · --no-bot · --no-trader
// A file in the Windows Startup folder may call this after sign-in (docs/runbook.md).
import { spawn, spawnSync } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isPaperBot, isPracticeTrader } from './console-db.mjs';
import { runningProcesses, workers } from './processes.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const args = process.argv.slice(2);
const has = (name) => args.includes(`--${name}`);
const DRY = has('dry-run');
const isWin = process.platform === 'win32';
const PORT = Number(process.env.UI_PORT ?? 4317);

const say = (mark, msg) => console.log(`  ${mark} ${msg}`);

function consoleAnswers() {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PORT}/api/health`, { timeout: 2000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}

/** Open `npm run <script>` in its own window (Windows) or detached in the background (elsewhere). */
function open(title, npmArgs) {
  if (DRY) return say('·', `would start: npm run ${npmArgs}`);
  if (isWin) {
    // `start` gives the window its own console: it outlives this script and takes its own Ctrl+C.
    spawn(`start "${title}" cmd /k "npm run ${npmArgs}"`, { cwd: REPO, shell: true, detached: true, stdio: 'ignore' }).unref();
  } else {
    spawn(`npm run ${npmArgs}`, { cwd: REPO, shell: true, detached: true, stdio: 'ignore' }).unref();
  }
  say('+', `started: npm run ${npmArgs}`);
}

console.log(`\n  ai-stock-trader · the trading day${DRY ? ' (dry run)' : ''}\n`);

if (!DRY) {
  const db = spawnSync(process.execPath, [path.join('ui', 'scripts', 'dev.mjs'), '--db-up'], { cwd: REPO, stdio: 'inherit' });
  if (db.status !== 0) {
    say('x', 'The database is not up: nothing else was started. Fix the message above and run npm run day again.');
    process.exit(db.status ?? 1);
  }
}

const procs = workers(runningProcesses());

if (!has('no-console')) {
  if (await consoleAnswers()) say('=', `the console already answers on http://127.0.0.1:${PORT}`);
  else open('Trader Console (npm run dev)', 'dev -- --no-open');
}

if (!has('no-bot')) {
  const bots = procs.filter((p) => isPaperBot(p.cmd));
  if (bots.length) say('=', `a paper bot is already running (pid ${bots[0].pid})`);
  else open('Paper bot - keep this window open', 'bot -- --practice');
}

if (!has('no-trader')) {
  // The launcher restarts its own Python child, so a live launcher counts as running.
  const traders = procs.filter((p) => isPracticeTrader(p.cmd));
  if (traders.length) say('=', `the practice trader is already running (pid ${traders[0].pid})`);
  else open('Practice trader - Capital.com DEMO, live', 'trader');
}

console.log('\n  Leave the windows open: closing one stops that program.\n');
