// Screenshots of every page in light and dark, for visual review.
//   node e2e/shoot.mjs [outDir] [source] [routes...]
// Uses the installed Microsoft Edge (no browser download). The console must be running (pnpm dev).
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

const out = process.argv[2] ?? 'test-results/shots';
const source = process.argv[3] ?? 'replay:baseline';
const routes = process.argv.slice(4).length ? process.argv.slice(4) : ['/', '/decisions', '/markets', '/money', '/risk', '/learning', '/rules'];
const base = process.env.CONSOLE_URL ?? 'http://127.0.0.1:4317';
mkdirSync(out, { recursive: true });

// Headless has no GPU; SwiftShader gives software WebGL so the 3D Core renders in screenshots.
const browser = await chromium.launch({ channel: 'msedge', args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
const errors = [];
for (const scheme of ['light', 'dark']) {
  const height = Number(process.env.SHOT_H ?? 1000);
  const width = Number(process.env.SHOT_W ?? 1480);
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, deviceScaleFactor: 1 });
  if (process.env.SHOT_ACTIVITY === '1') await ctx.addInitScript(() => localStorage.setItem('console.activity', '1'));
  const page = await ctx.newPage();
  page.on('console', (m) => m.type() === 'error' && errors.push(`[${scheme}] ${m.text()}`));
  page.on('pageerror', (e) => errors.push(`[${scheme}] ${e.message}`));
  for (const r of routes) {
    const sep = r.includes('?') ? '&' : '?';
    await page.goto(`${base}${r}${sep}source=${encodeURIComponent(source)}`, { waitUntil: 'load' });
    await page.waitForTimeout(Number(process.env.SHOT_WAIT ?? 2500));
    // SHOT_CLICK="1Y,Candles": click radio buttons / buttons by accessible name before the shot.
    for (const name of (process.env.SHOT_CLICK ?? '').split(',').filter(Boolean)) {
      const target = page.getByRole('radio', { name, exact: true }).or(page.getByRole('button', { name, exact: true })).first();
      await target.click();
      await page.waitForTimeout(1500);
    }
    const name = `${scheme}-${r.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'overview'}.png`;
    // Software WebGL (SwiftShader) can starve the renderer; allow time and retry once.
    const shot = () => page.screenshot({ path: path.join(out, name), fullPage: process.env.SHOT_FULL === '1', timeout: 90_000 });
    await shot().catch(async () => {
      await page.waitForTimeout(3000);
      await shot();
    });
    console.log('saved', name);
  }
  await ctx.close();
}
await browser.close();
if (errors.length) {
  console.log('CONSOLE ERRORS:\n' + errors.join('\n'));
  process.exitCode = 1;
}
