/**
 * Historical 5-minute bars from data/history/<dataset>/<VENUE>__<SYMBOL>.parquet (read-only).
 * Prices are raw (unadjusted) in the files; splits from research/data_manifest.json are applied
 * here so a chart does not show a fake 75% crash on a split date. Coarser timeframes are rolled up
 * from the 5-minute bars (shared/aggregate.ts) and cached.
 *
 * Files are keyed by dataset and symbol: one symbol can sit in several datasets (BTC_USD in the
 * Coinbase and Kraken ones). Requests name the dataset and default to the Alpaca stock dataset.
 */
import { existsSync, readdirSync } from 'node:fs';
import { Worker } from 'node:worker_threads';
import { aggregate, type Ohlcv, type Timeframe } from '../../shared/aggregate';
import { repoPath } from '../config';
import { corporateActions } from './files';

export type HistoryBar = Ohlcv;
export const DEFAULT_DATASET = 'alpaca_us_equities_5m_v1';
export const HISTORY_DATASETS = [DEFAULT_DATASET, 'synthetic_equities_5m_v1', 'coinbase_btc_eth_usd_15m_v1'];
const cache = new Map<string, Promise<HistoryBar[]>>();

export function historySymbols(): { symbol: string; dataset: string; file: string }[] {
  const out: { symbol: string; dataset: string; file: string }[] = [];
  for (const ds of HISTORY_DATASETS) {
    const dir = repoPath('data', 'history', ds);
    if (!existsSync(dir)) continue;
    for (const f of readdirSync(dir)) {
      if (!f.endsWith('.parquet')) continue;
      const symbol = f.replace(/\.parquet$/, '').split('__').pop()!;
      out.push({ symbol, dataset: ds, file: `${dir}/${f}` });
    }
  }
  return out;
}

// One long-lived decoding worker; requests are queued through it one file at a time.
let worker: Worker | null = null;
let nextId = 0;
const waiting = new Map<number, { resolve: (bars: HistoryBar[]) => void; reject: (e: Error) => void }>();

function decoder(): Worker {
  if (worker) return worker;
  worker = new Worker(new URL('./history-worker.mjs', import.meta.url));
  worker.unref(); // never keeps the process alive on shutdown
  worker.on('message', (m: { id: number; ok: boolean; error?: string; t: Float64Array; o: Float64Array; h: Float64Array; l: Float64Array; c: Float64Array; v: Float64Array }) => {
    const w = waiting.get(m.id);
    if (!w) return;
    waiting.delete(m.id);
    if (!m.ok) return w.reject(new Error(m.error));
    const bars: HistoryBar[] = new Array(m.t.length);
    for (let i = 0; i < m.t.length; i++) bars[i] = { t: m.t[i], o: m.o[i], h: m.h[i], l: m.l[i], c: m.c[i], v: m.v[i] };
    w.resolve(bars);
  });
  worker.on('error', (err) => {
    for (const w of waiting.values()) w.reject(err);
    waiting.clear();
    worker = null;
  });
  return worker;
}

async function loadAll(dataset: string, symbol: string): Promise<HistoryBar[]> {
  const entry = historySymbols().find((s) => s.dataset === dataset && s.symbol === symbol);
  if (!entry) return [];
  const splits = corporateActions()
    .filter((a) => a.instrument_id.split(':').pop() === symbol)
    .map((a) => ({ at: Date.parse(`${a.ex_date}T00:00:00Z`), ratio: a.ratio }));
  const id = ++nextId;
  return new Promise<HistoryBar[]>((resolve, reject) => {
    waiting.set(id, { resolve, reject });
    decoder().postMessage({ id, file: entry.file, splits });
  });
}

function series(dataset: string, symbol: string, tf: Timeframe): Promise<HistoryBar[]> {
  const key = `${dataset}|${symbol}|${tf}`;
  if (!cache.has(key)) {
    const baseKey = `${dataset}|${symbol}|5m`;
    const base = cache.get(baseKey) ?? loadAll(dataset, symbol);
    cache.set(baseKey, base);
    if (tf !== '5m') cache.set(key, base.then((b) => aggregate(b, tf)));
  }
  return cache.get(key)!;
}

export const TIMEFRAMES: Timeframe[] = ['5m', '15m', '1h', '1d'];

/** Decode every stock history file in the background so the first Markets visit is instant. */
export function warmHistory(): void {
  const stocks = historySymbols().filter((s) => s.dataset.startsWith('alpaca'));
  let chain = Promise.resolve();
  for (const s of stocks) {
    chain = chain.then(() => series(s.dataset, s.symbol, '5m').then(() => undefined)).catch(() => undefined);
  }
}

/** Bars strictly before `before` (seconds), newest `limit` of them, oldest first. */
export async function historyBars(
  symbol: string,
  before: number | null,
  limit: number,
  tf: Timeframe = '5m',
  dataset: string = DEFAULT_DATASET,
): Promise<HistoryBar[]> {
  if (!/^[A-Z0-9_]{1,12}$/.test(symbol) || !HISTORY_DATASETS.includes(dataset)) return [];
  const all = await series(dataset, symbol, tf);
  let end = all.length;
  if (before !== null) {
    let lo = 0;
    let hi = all.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (all[mid].t < before) lo = mid + 1;
      else hi = mid;
    }
    end = lo;
  }
  return all.slice(Math.max(0, end - limit), end);
}
