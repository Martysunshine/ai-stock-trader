#!/usr/bin/env node
// `npm run trader`: start the practice trader (ADR-0056) deliberately, never from `npm run dev`,
// so a console restart does not stop it. It trades the crypto and CFD practice sleeves' setups on
// the Capital.com DEMO account by itself. `npm run trader -- --dry-run` decides without journalling
// or sending anything; other arguments are passed on (e.g. `--setup weekly_momentum_v1`).
// Ctrl+C stops it; stops and targets stay at the broker.
//
// It is restarted when it ends any other way (killed, crashed, lock still held): on 2026-11-11 it
// was killed from outside and nothing brought it back for an hour. A trade's flat-time close
// needs the trader running, so the launcher keeps it up; only Ctrl+C or a clean exit ends it.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const args = process.argv.slice(2);
const dry = args.includes('--dry-run');
const rest = args.filter((a) => a !== '--dry-run');
const venv = path.join(REPO, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = existsSync(venv) ? venv : 'python';
const argv = ['-u', '-m', 'trader.practice', 'trade', ...(dry ? [] : ['--live']), ...rest];
const RESTART_SECONDS = 20;
console.log(`\n  Practice trader (${dry ? 'dry run' : 'LIVE on the Capital.com demo account'}): ${['python', ...argv].join(' ')}`);
console.log('  Log: runs/practice/trader-<date>.log · state: runs/practice/trader.json');
console.log('  (Ctrl+C stops it; stops and targets stay at the broker. Killed or crashed, it restarts.)\n');

let stopping = false;
process.on('SIGINT', () => {
  stopping = true; // the child gets the same Ctrl+C and ends by itself
});

function start() {
  const child = spawn(python, argv, { cwd: REPO, stdio: 'inherit', env: { PYTHONUTF8: '1', ...process.env } });
  child.on('exit', (code) => {
    if (stopping || code === 0) process.exit(code ?? 0);
    console.log(`\n  The practice trader ended (exit ${code ?? 'killed'}). Restarting in ${RESTART_SECONDS} s; Ctrl+C to stop.\n`);
    setTimeout(start, RESTART_SECONDS * 1000);
  });
}
start();
