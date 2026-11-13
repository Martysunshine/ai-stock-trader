// End-to-end check and screenshots of the Sunshine Desk (light, dark, phone width).
//   node e2e/desk.mjs [outDir]
// The console must be running (npm run dev); the Desk is its /desk page. Uses the installed Microsoft Edge. It sends two real
// notes (short and long) into <repo>/.desk/notes, checks the vscode:// link each would open,
// plays Claude by marking one done, and removes both notes at the end.
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const out = process.argv[2] ?? 'test-results/desk';
const base = process.env.DESK_URL ?? 'http://127.0.0.1:4317/desk';
const origin = new URL(base).origin;
const PREFIX = 'vscode://anthropic.claude-code/open?prompt=';
mkdirSync(out, { recursive: true });

const fail = (msg) => {
  console.error('FAIL:', msg);
  process.exitCode = 1;
};
const errors = [];
const browser = await chromium.launch({ channel: 'msedge' });

// --------------------------------------------------------------------------- send flow
const ctx = await browser.newContext({ viewport: { width: 1360, height: 1000 } });
await ctx.addInitScript(() => {
  window.__opened = [];
  window.__deskOpen = (u) => window.__opened.push(u);
});
const page = await ctx.newPage();
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
page.on('pageerror', (e) => errors.push(e.message));
await page.goto(base, { waitUntil: 'load' }); // live streams never let the network go idle
await page.getByLabel('Note for Claude').waitFor({ timeout: 20000 });

const session = await (await page.request.get(`${origin}/api/desk/session`)).json();
const created = [];

const short = `e2e check ${Date.now()}: what should I do next?`;
await page.getByLabel('Note for Claude').fill(short);
await page.getByRole('button', { name: 'Send to Claude' }).click();
await page.waitForFunction(() => window.__opened.length === 1);
let opened = await page.evaluate(() => window.__opened);
if (!opened[0].startsWith(PREFIX)) fail(`unexpected link ${opened[0].slice(0, 60)}`);
const prompt = decodeURIComponent(opened[0].slice(PREFIX.length));
if (!prompt.startsWith(short)) fail('the short note is not inline in the prompt');
const id = /Desk note (\d{8}-\d{6}-[0-9a-f]{4})/.exec(prompt)?.[1];
if (!id) fail('no note id in the prompt');
const file = path.join(session.notesDir, `${id}.md`);
created.push(file);
await page.getByText(short).first().waitFor({ timeout: 5000 });
await page.getByText('Opened', { exact: true }).first().waitFor({ timeout: 5000 });

// Claude answers in the file; the page must update without a reload.
writeFileSync(file, readFileSync(file, 'utf8').replace('status: new', 'status: done') + 'Start with the `/mcp` sign-ins.\n');
await page.getByText('Start with the', { exact: false }).waitFor({ timeout: 5000 });
await page.getByText('Done', { exact: true }).first().waitFor({ timeout: 5000 });

const long = `e2e long note ${Date.now()} ` + 'Über Gold und Silber nachdenken. '.repeat(120);
await page.getByLabel('Note for Claude').fill(long);
await page.getByRole('button', { name: 'Send to Claude' }).click();
await page.waitForFunction(() => window.__opened.length === 2);
opened = await page.evaluate(() => window.__opened);
const pointer = decodeURIComponent(opened[1].slice(PREFIX.length));
if (opened[1].length > 1900) fail(`long-note link is ${opened[1].length} characters`);
if (!pointer.startsWith('Read my Desk note and act on it: ')) fail('the long note did not switch to a file pointer');
const longId = /Desk note (\d{8}-\d{6}-[0-9a-f]{4})/.exec(pointer)?.[1];
created.push(path.join(session.notesDir, `${longId}.md`));
if (!readFileSync(created[1], 'utf8').includes('Gold und Silber')) fail('the long note file does not hold the full text');
console.log('send flow ok: inline', opened[0].length, 'chars; pointer', opened[1].length, 'chars');
await ctx.close();

// --------------------------------------------------------------------------- screenshots
for (const [name, viewport, scheme] of [
  ['light', { width: 1360, height: 1000 }, 'light'],
  ['dark', { width: 1360, height: 1000 }, 'dark'],
  ['phone-light', { width: 390, height: 844 }, 'light'],
]) {
  const c = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1 });
  const p = await c.newPage();
  p.on('console', (m) => m.type() === 'error' && errors.push(`[${name}] ${m.text()}`));
  p.on('pageerror', (e) => errors.push(`[${name}] ${e.message}`));
  await p.goto(base, { waitUntil: 'load' });
  await p.getByLabel('Note for Claude').waitFor({ timeout: 20000 });
  await p.waitForTimeout(2200); // let the goal bar finish filling
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 0) fail(`${name}: page scrolls sideways by ${overflow}px`);
  await p.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  console.log('saved', `${name}.png`);
  await c.close();
}
await browser.close();

for (const f of created) rmSync(f, { force: true });
if (errors.length) fail('console errors:\n' + errors.join('\n'));
