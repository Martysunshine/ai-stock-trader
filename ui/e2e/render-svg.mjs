// Render an SVG file to PNG for review: node e2e/render-svg.mjs in.svg out.png
import { chromium } from '@playwright/test';
import { readFileSync } from 'node:fs';
const [input, output] = process.argv.slice(2);
const b = await chromium.launch({ channel: 'msedge' });
const p = await b.newPage({ viewport: { width: 512, height: 512 } });
await p.setContent(`<body style="margin:0;background:#fff;display:grid;place-items:center;height:100vh">${readFileSync(input, 'utf8').replace(/<metadata>[\s\S]*?<\/metadata>/, '').replace(/width="\d+" height="\d+"/, 'width="400" height="400"')}</body>`);
await p.screenshot({ path: output });
await b.close();
