// Debug helper: node e2e/debug-page.mjs <path> — prints pending requests and main text.
import { chromium } from '@playwright/test';
const path = process.argv[2] ?? '/';
const b = await chromium.launch({ channel: 'msedge' });
const p = await b.newPage();
const pending = new Map();
p.on('request', (r) => pending.set(r, r.url()));
p.on('requestfinished', (r) => pending.delete(r));
p.on('requestfailed', (r) => { console.log(`[failed] ${r.url()} ${r.failure()?.errorText}`); pending.delete(r); });
p.on('pageerror', (e) => console.log(`[pageerror] ${e.message}`));
await p.goto(`http://127.0.0.1:4317${path}`, { waitUntil: 'load' });
await p.waitForTimeout(8000);
console.log('main:', JSON.stringify(await p.evaluate(() => document.querySelector('main')?.innerHTML.slice(0, 300))));
console.log('pending:', [...pending.values()].filter((u) => !u.includes('stream')).slice(0, 20));
await b.close();
