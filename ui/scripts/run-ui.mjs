#!/usr/bin/env node
// Run a ui/ package script from the repository root with npm: `npm run test:ui` -> pnpm run test.
// Falls back to `corepack pnpm` when pnpm is not installed globally.
import { spawn, spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const script = process.argv[2];
const pnpm = spawnSync('pnpm --version', { shell: true, stdio: 'ignore' }).status === 0 ? 'pnpm' : 'corepack pnpm';
const child = spawn(`${pnpm} run ${script} ${process.argv.slice(3).join(' ')}`.trim(), {
  cwd: UI,
  shell: true,
  stdio: 'inherit',
  env: { COREPACK_ENABLE_DOWNLOAD_PROMPT: '0', ...process.env },
});
child.on('exit', (code) => process.exit(code ?? 0));
