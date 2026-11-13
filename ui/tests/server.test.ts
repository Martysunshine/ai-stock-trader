import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import Decimal from 'decimal.js';
import { allStatements, APPEND_BY_ID, SMALL_MUTABLE } from '../server/queries';
import { advisoryLockKey, lockKeyParts } from '../server/sources/live';
import { buildTimeline, listScenarios, loadReplayTables, ReplaySource } from '../server/sources/replay';
import { isAllowedHost, isAllowedOrigin, assertLoopbackBind } from '../server/guard';
import { normDec, normTs, TABLE_NAMES, type Tables } from '../shared/schema';
import { runTotals } from '../shared/derive';
import { REPO_ROOT, UI_ROOT } from '../server/config';
import { findInstrument, instrumentPath, marketBars, marketCacheSize, marketIndexReply } from '../server/sources/market';
import { registerMarketRoutes } from '../server/index';
import { historyBars } from '../server/sources/history';
import Fastify from 'fastify';

describe('SQL guard: the console only reads', () => {
  const statements = allStatements([...APPEND_BY_ID, ...SMALL_MUTABLE, ...TABLE_NAMES]);
  const forbidden =
    /\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|COPY|CALL|DO|VACUUM|REINDEX|CLUSTER|COMMENT|SECURITY|EXECUTE|PREPARE|LISTEN|NOTIFY|pg_advisory_lock|pg_try_advisory_lock|set_config|nextval|setval)\b/i;

  it('every statement starts with SELECT, WITH or SHOW', () => {
    for (const s of statements) expect(s.trim()).toMatch(/^(SELECT|WITH|SHOW)\b/i);
  });

  it('no statement contains a write, DDL or lock-taking keyword', () => {
    for (const s of statements) expect(s, s).not.toMatch(forbidden);
  });

  it('no other server file sends SQL', () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const d of readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, d.name);
        if (d.isDirectory()) walk(p);
        else if (p.endsWith('.ts') && !p.endsWith('queries.ts')) {
          const src = readFileSync(p, 'utf8');
          if (/\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b/i.test(src)) offenders.push(p);
        }
      }
    };
    walk(path.join(UI_ROOT, 'server'));
    expect(offenders).toEqual([]);
  });

  it('never imports a broker client, keyring or HTTP client', () => {
    const banned = /from ['"](alpaca|@alpacahq|keytar|keyring|axios|node-fetch|undici)/;
    const walk = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
        d.isDirectory() ? walk(path.join(dir, d.name)) : [path.join(dir, d.name)],
      );
    for (const f of walk(path.join(UI_ROOT, 'server'))) {
      expect(readFileSync(f, 'utf8'), f).not.toMatch(banned);
    }
  });
});

describe('leadership lock key matches the bot (trader.domain.ids.advisory_lock_key)', () => {
  it('reproduces the Python values', () => {
    expect(advisoryLockKey('account:acct-alpaca-paper-pilot')).toBe(706600387555634825n);
    expect(advisoryLockKey('account:acct-replay-stocks')).toBe(1289023752219045481n);
  });
  it('splits into pg_locks classid/objid', () => {
    const k = 706600387555634825n;
    const [hi, lo] = lockKeyParts(k);
    expect((BigInt(hi) << 32n) + BigInt(lo)).toBe(k);
  });
});

describe('localhost guard', () => {
  it('accepts loopback hosts only', () => {
    expect(isAllowedHost('127.0.0.1:4317')).toBe(true);
    expect(isAllowedHost('localhost:5173')).toBe(true);
    expect(isAllowedHost('[::1]:4317')).toBe(true);
    expect(isAllowedHost('evil.example:4317')).toBe(false);
    expect(isAllowedHost('127.0.0.1.evil.example')).toBe(false);
    expect(isAllowedHost(undefined)).toBe(false);
  });
  it('refuses foreign origins for state-changing requests', () => {
    expect(isAllowedOrigin('http://127.0.0.1:5173')).toBe(true);
    expect(isAllowedOrigin('https://evil.example')).toBe(false);
    expect(isAllowedOrigin(undefined)).toBe(true);
  });
  it('refuses a non-loopback bind', () => {
    expect(() => assertLoopbackBind('0.0.0.0')).toThrow();
    expect(() => assertLoopbackBind('127.0.0.1')).not.toThrow();
  });
});

describe('row normalisation', () => {
  it('keeps decimals exact and canonical', () => {
    expect(normDec('50.000000000000000000')).toBe('50');
    expect(normDec('0.098726429000000000')).toBe('0.098726429');
    expect(normDec('-0.0000')).toBe('0');
    expect(normDec(null)).toBeNull();
  });
  it('turns SQLite naive UTC timestamps into ISO Z', () => {
    expect(normTs('2026-11-27 14:29:00.120000')).toBe('2026-11-27T14:29:00.120Z');
    expect(normTs('2026-11-27 15:20:01')).toBe('2026-11-27T15:20:01.000Z');
    expect(normTs(new Date(Date.UTC(2026, 10, 27, 15)))).toBe('2026-11-27T15:00:00.000Z');
  });
});

/** Equal at the precision the report string uses (the demonstrator rounds its figures). */
function sameAtReportPrecision(ui: string, report: string): boolean {
  const places = (report.split('.')[1] ?? '').length;
  return new Decimal(ui).toDecimalPlaces(places, Decimal.ROUND_HALF_EVEN).eq(new Decimal(report));
}

describe('replay totals equal the demonstrator report for every scenario', () => {
  const scenarios = listScenarios().filter((s) => s.path.includes(`${path.sep}demo${path.sep}`));

  it('finds the recorded scenarios', () => {
    expect(scenarios.length).toBeGreaterThanOrEqual(5);
  });

  for (const s of scenarios) {
    it(s.id, () => {
      const report = JSON.parse(readFileSync(path.join(path.dirname(s.path), 'report.json'), 'utf8')).report;
      // Stored final rows...
      const stored = runTotals(loadReplayTables(s.path) as unknown as Tables);
      // ...and the state the replay stream ends in.
      const streamed = runTotals(new ReplaySource(s).finalTables() as unknown as Tables);
      for (const t of [stored, streamed]) {
        expect(t.decisions).toBe(report.decisions);
        expect(t.approved).toBe(report.approved);
        expect(t.rejection_reasons).toEqual(report.rejection_reasons);
        expect(t.orders).toBe(report.orders);
        expect(t.order_states).toEqual(report.order_states);
        expect(t.fills).toBe(report.fills);
        expect(sameAtReportPrecision(t.gross_trading_pnl, report.gross_trading_pnl), `gross ${t.gross_trading_pnl}`).toBe(true);
        expect(sameAtReportPrecision(t.fees, report.fees), `fees ${t.fees}`).toBe(true);
        expect(sameAtReportPrecision(t.net_trading_pnl, report.net_trading_pnl), `net ${t.net_trading_pnl}`).toBe(true);
        expect(sameAtReportPrecision(t.ending_cash, report.ending_cash), `cash ${t.ending_cash}`).toBe(true);
      }
    });
  }

  it('emits rows in time order', () => {
    const items = buildTimeline(loadReplayTables(scenarios[0].path));
    for (let i = 1; i < items.length; i++) expect(items[i].at).toBeGreaterThanOrEqual(items[i - 1].at);
  });

  it('an intent passes through PENDING_SUBMIT before reaching its final state', () => {
    const items = buildTimeline(loadReplayTables(scenarios.find((s) => s.id === 'baseline')!.path));
    const states = items.filter((i) => i.table === 'order_intents' && i.row.purpose === 'ENTRY').map((i) => i.row.state);
    expect(states[0]).toBe('PENDING_SUBMIT');
    expect(states).toContain('PARTIALLY_FILLED');
    expect(states.at(-1)).toBe('CANCELED');
  });
});

describe('repository layout', () => {
  it('the console never reads the bot .env', () => {
    const src = readFileSync(path.join(UI_ROOT, 'server', 'config.ts'), 'utf8');
    // The only env file loaded is ui/.env.local.
    expect([...src.matchAll(/loadEnvFile\(([^)]*)\)/g)].map((m) => m[1])).toEqual(['envFile']);
    expect(src).toMatch(/const envFile = path\.join\(UI_ROOT, '\.env\.local'\)/);
    expect(src).not.toMatch(/REPO_ROOT,\s*['"]\.env['"]/);
  });

  it('observes the parent repository unless UI_REPO_ROOT points elsewhere', () => {
    const expected = process.env.UI_REPO_ROOT?.trim() ? path.resolve(process.env.UI_REPO_ROOT.trim()) : path.resolve(UI_ROOT, '..');
    expect(REPO_ROOT).toBe(expected);
  });
});

describe('paper configuration references (ADR-0043)', () => {
  it('reads the practice account id from paper.yaml text', async () => {
    const { accountIdFromPaperYaml } = await import('../server/config');
    const text = [
      'portfolio_id: pf-practice',
      'accounts:',
      '  - alias: practice',
      '    account_id: acct-alpaca-paper-practice',
    ].join('\n');
    expect(accountIdFromPaperYaml(text)).toBe('acct-alpaca-paper-practice');
    expect(accountIdFromPaperYaml('accounts: []')).toBeNull();
  });

  it('accepts only plain files of the expected configs folder', async () => {
    const { paperRef } = await import('../server/sources/files');
    expect(paperRef('policy/paper_practice_v1.yaml   # USD 1000', 'policy', 'configs/policy/base.yaml')).toBe(
      'configs/policy/paper_practice_v1.yaml',
    );
    for (const bad of ['../secrets.yaml', 'policy/../x.yaml', 'fees/a.yaml', 42, null]) {
      expect(paperRef(bad, 'policy', 'configs/policy/base.yaml')).toBe('configs/policy/base.yaml');
    }
  });
});

// ------------------------------------------------------------------------------------- market feed
/** A day of 5-minute bars from `from` (UTC ISO), close rising by 1 each bar. */
function rows(from: string, count: number, stepSec: number, start = 100): (number | string)[][] {
  const t0 = Date.parse(from) / 1000;
  return Array.from({ length: count }, (_, i) => {
    const p = start + i;
    return [t0 + i * stepSec, String(p), String(p + 0.5), String(p - 0.5), String(p + 0.25), '10'];
  });
}

describe('market feed files (data/market, read-only)', () => {
  let root = '';
  const now = Date.parse('2026-11-09T18:00:00Z') / 1000;
  const write = (rel: string, data: unknown) => {
    mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
    writeFileSync(path.join(root, rel), JSON.stringify(data));
  };
  const instrument = (id: string, file: string) => ({
    id,
    symbol: id.split(':')[1],
    name: id,
    file,
    precision: 2,
    has_volume: true,
    basis: 'trade',
    series: {},
  });

  beforeAll(() => {
    root = path.join(mkdtempSync(path.join(os.tmpdir(), 'market-test-')), 'data', 'market');
    write('index.json', {
      version: 1,
      generated_at: '2026-11-09T18:00:00Z',
      every_minutes: 5,
      groups: [
        { id: 'stocks', title: 'Stocks', source: 'Alpaca IEX', day_rule: 'nyse', status: 'ok', error: null, updated_at: null, instruments: [instrument('stocks:AAPL', 'stocks/AAPL.json')] },
        { id: 'crypto', title: 'Crypto', source: 'Kraken', day_rule: 'utc', status: 'ok', error: null, updated_at: null, instruments: [instrument('crypto:BTC', 'crypto/BTC.json')] },
        {
          id: 'cfd',
          title: 'CFDs',
          source: 'Capital.com demo',
          day_rule: 'utc',
          status: 'ok',
          error: null,
          updated_at: null,
          // A tampered index must not turn into a file read outside data/market.
          instruments: [instrument('cfd:EVIL', '../../../.env'), instrument('cfd:ODD', 'cfd/../../secret.json')],
        },
      ],
    });
    // BTC: 5m for the last 12 hours, 1h for 10 days, 1d for 60 days.
    write('crypto/BTC.json', {
      id: 'crypto:BTC',
      series: {
        '5m': rows(new Date((now - 12 * 3600) * 1000).toISOString(), 144, 300),
        '1h': rows(new Date((now - 10 * 86400) * 1000).toISOString(), 240, 3600),
        '1d': rows(new Date((now - 60 * 86400) * 1000).toISOString(), 60, 86400),
      },
    });
    // AAPL: one New York session (14:30-21:00 UTC, EST) of 5-minute bars.
    write('stocks/AAPL.json', { id: 'stocks:AAPL', series: { '5m': rows('2026-11-06T14:30:00Z', 78, 300), '1h': [], '1d': [] } });
    writeFileSync(path.join(root, '..', '..', '.env'), 'SECRET=must-never-be-served\n');
  });
  afterAll(() => rmSync(path.dirname(path.dirname(root)), { recursive: true, force: true }));

  it('serves the index as written, or says the feed has not run', () => {
    const reply = marketIndexReply(root);
    expect(reply.available).toBe(true);
    expect(reply.groups.map((g) => g.id)).toEqual(['stocks', 'crypto', 'cfd']);
    const missing = marketIndexReply(path.join(root, 'nowhere', 'market'));
    expect(missing.available).toBe(false);
  });

  it('serves only ids the index lists, never a path from the request', () => {
    expect(findInstrument('crypto:BTC', root)?.instrument.file).toBe('crypto/BTC.json');
    for (const bad of ['crypto:ETH', 'stocks:aapl', '../index', 'crypto:..', 'crypto:BTC/../../x', 'crypto:BTC.json', '', 'cfd:%2e%2e']) {
      expect(findInstrument(bad, root), bad).toBeNull();
      expect(marketBars(bad, '5m', 10, root), bad).toBeNull();
    }
    // Listed, but the index points outside the folder: refused.
    expect(marketBars('cfd:EVIL', '5m', 10, root)).toBeNull();
    expect(marketBars('cfd:ODD', '5m', 10, root)).toBeNull();
    expect(instrumentPath(root, '../../../.env')).toBeNull();
    expect(instrumentPath(root, 'crypto/BTC.json')).toBe(path.join(root, 'crypto', 'BTC.json'));
  });

  it('picks the finest stored series that covers the range, on the group day rule', () => {
    const day = marketBars('crypto:BTC', '15m', 40, root)!; // 10 hours: 5m covers it
    expect([day.series, day.barTf, day.dayRule]).toEqual(['5m', '15m', 'utc']);
    expect(day.bars.length).toBe(40);
    expect(day.bars.every((b) => b.t % 900 === 0)).toBe(true); // on the UTC clock grid
    const week = marketBars('crypto:BTC', '15m', 672, root)!; // 7 days: only 1h reaches back
    expect([week.series, week.barTf]).toEqual(['1h', '1h']);
    expect(week.bars.length).toBe(7 * 24);
    const month = marketBars('crypto:BTC', '1d', 30, root)!; // 30 days: 1d is the only cover
    expect([month.series, month.barTf]).toEqual(['1d', '1d']);
    const daily = marketBars('crypto:BTC', '1d', 5, root)!; // 5 days: hourly bars rolled up at 00:00 UTC
    expect(daily.series).toBe('1h');
    expect(daily.bars.every((b) => b.t % 86400 === 0)).toBe(true);
    const all = marketBars('crypto:BTC', '1d', 5000, root)!; // nothing covers: the deepest series
    expect([all.series, all.bars.length]).toEqual(['1d', 60]);
    const aapl = marketBars('stocks:AAPL', '1h', 7, root)!; // nyse: hours start at 09:30
    expect(aapl.dayRule).toBe('nyse');
    expect(aapl.bars.map((b) => new Date(b.t * 1000).toISOString().slice(11, 16))).toEqual(['14:30', '15:30', '16:30', '17:30', '18:30', '19:30', '20:30']);
    expect(marketCacheSize()).toBeLessThanOrEqual(8);
  });

  it('re-reads a file after the feed replaces it', () => {
    const before = marketBars('stocks:AAPL', '5m', 500, root)!.bars.length;
    write('stocks/AAPL.json', { id: 'stocks:AAPL', series: { '5m': rows('2026-11-06T14:30:00Z', 10, 300), '1h': [], '1d': [] } });
    const later = new Date(Date.now() + 5000);
    utimesSync(path.join(root, 'stocks', 'AAPL.json'), later, later);
    expect(before).toBe(78);
    expect(marketBars('stocks:AAPL', '5m', 500, root)!.bars.length).toBe(10);
  });

  it('routes serve listed ids only and refuse traversal attempts and bad timeframes', async () => {
    const app = Fastify();
    registerMarketRoutes(app, root);
    try {
      const get = (url: string) => app.inject({ method: 'GET', url });
      const index = await get('/api/market');
      expect(index.statusCode).toBe(200);
      expect(index.json().available).toBe(true);
      const btc = await get('/api/market/crypto:BTC?tf=15m&limit=40');
      expect(btc.statusCode).toBe(200);
      expect(btc.json().bars).toHaveLength(40);
      for (const url of ['/api/market/stocks:NOPE', '/api/market/cfd:EVIL', '/api/market/..%2F..%2F.env', '/api/market/%2e%2e%2f.env', '/api/market/cfd:..%5C..%5C.env']) {
        const r = await get(url);
        expect(r.statusCode, url).toBe(404);
        expect(r.body).not.toContain('must-never-be-served');
      }
      expect((await get('/api/market/crypto:BTC?tf=2m')).statusCode).toBe(400);
      expect((await get('/api/market/crypto:BTC?limit=lots')).statusCode).toBe(400);
    } finally {
      await app.close();
    }
  });

  it('history is keyed by dataset and refuses unknown datasets', async () => {
    expect(await historyBars('AAPL', null, 10, '5m', '../../.env')).toEqual([]);
    expect(await historyBars('BTC_USD', null, 10, '5m', 'not_a_dataset')).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------- mission
describe('mission sleeves read the practice journals (ADR-0046)', () => {
  const fixture = path.join(UI_ROOT, 'tests', 'fixtures', 'practice_journal.jsonl');

  it('verifies a journal written by the Python practice journal and rebuilds its trades', async () => {
    const { readJournal, tradesOf } = await import('../server/sources/mission');
    const { events, chainOk } = readJournal(fixture);
    expect(chainOk).toBe(true);
    expect(events.map((e) => e.event)).toEqual(['PLAN', 'OPEN', 'NOTE', 'AMEND', 'CLOSE', 'REVIEW', 'PLAN', 'OPEN']);
    const [gold, silver] = tradesOf(events);
    expect(gold).toMatchObject({ epic: 'GOLD', state: 'closed', pnlUsd: '1.27', stop: '4060', fill: '4101.5', riskUsd: '0.8' });
    expect(silver).toMatchObject({ epic: 'SILVER', state: 'open', direction: 'BUY', closedAt: null });
  });

  it('flags a journal whose line was edited', async () => {
    const { readJournal } = await import('../server/sources/mission');
    const dir = mkdtempSync(path.join(os.tmpdir(), 'mission-'));
    const tampered = path.join(dir, 'journal.jsonl');
    writeFileSync(tampered, readFileSync(fixture, 'utf8').replace('"pnl_usd":"1.27"', '"pnl_usd":"127"'));
    expect(readJournal(tampered).chainOk).toBe(false);
    expect(readJournal(path.join(dir, 'missing.jsonl'))).toEqual({ events: [], chainOk: true });
  });

  it('describes a sleeve truthfully and splits the total', async () => {
    const { practiceView, sleeveShares } = await import('../shared/mission');
    const { readJournal, tradesOf } = await import('../server/sources/mission');
    const trades = tradesOf(readJournal(fixture).events);
    const base = { id: 'cfd' as const, start: 100, realized: 100, openPnl: 0, markedAt: null, floor: 90, riskFraction: 0.01, maxOpenTrades: 2, curve: [], lastEventAt: null, chainOk: true };
    const view = practiceView({ ...base, equity: 101.27, trades });
    expect(view).toMatchObject({ openCount: 1, closedCount: 1, wins: 1, live: true, statusLine: '1 open: long SILVER', tally: '1 closed · 1 won' });
    expect(practiceView({ ...base, equity: 89, trades: [] }).statusLine).toMatch(/loss floor/);
    // a planned trade that has not opened is an order waiting to fill, not "no trade"
    const resting = { ...trades[1], state: 'planned' as const, epic: 'US100', entry: '27400' };
    expect(practiceView({ ...base, equity: 100, trades: [resting] })).toMatchObject({ statusLine: '1 waiting to fill: long US100 at 27400', live: true, openCount: 0 });
    expect(practiceView({ ...base, equity: 100, trades: [], chainOk: false }).statusLine).toMatch(/integrity/);
    const shares = sleeveShares([view, { ...view, equity: 100 }, { ...view, equity: 98.73 }]);
    expect(shares.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 10);
    expect(shares[0]).toBeGreaterThan(shares[2]);
  });

  it('values an open trade at the latest price, after the fee paid and the fee to close', async () => {
    const { readJournal, tradesOf, markTrade } = await import('../server/sources/mission');
    const { practiceView } = await import('../shared/mission');
    const [gold, silver] = tradesOf(readJournal(fixture).events);
    const long = { ...silver, fill: '50', size: '2', stop: '48' };
    const last = { price: 51, at: 1_794_330_000 };
    // no venue fee (CFDs): the price move is the result
    expect(markTrade(long, last, 0)).toMatchObject({ price: 51, at: last.at, grossUsd: 2, feesUsd: 0, netUsd: 2 });
    expect(markTrade(long, last, 0)?.toStop).toBeCloseTo(3 / 51, 10);
    // 0.8% a side (Kraken paper): both the entry fee and the fee to close now are taken off
    const withFees = markTrade(long, last, 0.008);
    expect(withFees?.feesUsd).toBeCloseTo(0.008 * 2 * (50 + 51), 10);
    expect(withFees?.netUsd).toBeCloseTo(2 - 1.616, 10);
    // a short gains when the price falls, and a quote in another currency is converted
    const short = { ...long, direction: 'SELL' as const, stop: '52', fxToUsd: '1.1' };
    expect(markTrade(short, { price: 49, at: last.at }, 0)).toMatchObject({ grossUsd: expect.closeTo(2.2, 10) });
    expect(markTrade(short, { price: 49, at: last.at }, 0)?.toStop).toBeCloseTo(-3 / 49 * -1, 10);
    // only open, filled trades are valued
    expect(markTrade(gold, last, 0)).toBeNull();
    expect(markTrade({ ...long, fill: null }, last, 0)).toBeNull();
    // the sleeve's line ends at its value now
    const curve = [{ t: 1_794_300_000, v: 100 }];
    const base = { id: 'crypto' as const, start: 100, realized: 100, floor: 90, riskFraction: 0.01, maxOpenTrades: 2, lastEventAt: null, chainOk: true, trades: [] };
    expect(practiceView({ ...base, curve, equity: 99.4, openPnl: -0.6, markedAt: last.at }).curve).toEqual([...curve, { t: last.at, v: 99.4 }]);
    expect(practiceView({ ...base, curve, equity: 100, openPnl: 0, markedAt: null }).curve).toEqual(curve);
  });

  it('values crypto CFD trades at the broker quote, with the Kraken price only as a stand-in (ADR-0049)', async () => {
    const { latestPrices } = await import('../server/sources/mission');
    const item = (symbol: string, last_price: string | null) => ({ id: `x:${symbol}`, symbol, name: symbol, file: `x/${symbol}.json`, precision: 2, has_volume: false, basis: 'mid' as const, last_price, last_at: 1_794_330_000, series: {} as never });
    const group = (id: string, instruments: ReturnType<typeof item>[]) => ({ id, title: id, source: id, day_rule: 'utc' as const, status: 'ok' as const, error: null, updated_at: null, instruments });
    const index = (cfd: ReturnType<typeof item>[]) => ({ version: 1, generated_at: '2026-11-10T21:00:00Z', every_minutes: 5, groups: [group('crypto', [item('BTC', '73900'), item('ETH', '2050')]), group('cfd', cfd)] }) as never;
    const withCfds = index([item('GOLD', '4030'), item('BTCUSD', '73550'), item('ETHUSD', null)]);
    expect(latestPrices('crypto', withCfds).get('BTCUSD')?.price).toBe(73550); // Capital.com's quote
    expect(latestPrices('crypto', withCfds).get('ETHUSD')?.price).toBe(2050); // no CFD price yet: Kraken's
    expect(latestPrices('cfd', withCfds).get('GOLD')?.price).toBe(4030);
    expect(latestPrices('cfd', withCfds).has('ETHUSD')).toBe(false);
    expect(latestPrices('crypto', index([])).get('BTCUSD')?.price).toBe(73900);
    expect(latestPrices('cfd', null).size).toBe(0);
  });

  it('reads the practice scan as the watcher wrote it, or says why it could not run', async () => {
    const { readScan } = await import('../server/sources/mission');
    const dir = mkdtempSync(path.join(os.tmpdir(), 'scan-'));
    const file = path.join(dir, 'scan.json');
    expect(readScan(file)).toBeNull();
    writeFileSync(file, 'not json');
    expect(readScan(file)).toBeNull();
    const ticket = { status: 'TRADE', direction: 'SHORT', size: '0.003', stop: '2420.00', target: '1655.00', risk_usd: '0.86', max_hold_hours: 168, note: 'inside the limit' };
    const reading = { epic: 'ETHUSD', day: '2026-11-15', stale: false, close: '2132.22', change: '-0.052100', atr: '97.57', direction: 'SHORT', decision_bar: true, signal: true, ticket };
    const open = { sleeve: 'crypto-cfd', trade_id: 'E1', epic: 'BTCUSD', direction: 'BUY', due_at: '2026-11-16T00:05:00Z', overdue: true, beyond_stop: false, at_target: false, exit_signal: null };
    writeFileSync(file, JSON.stringify({ version: 2, generated_at: '2026-11-16T00:10:00Z', title: 'Weekly momentum on the Bitcoin and Ether CFDs', next_decision_at: '2026-11-23T00:00:00Z', risk_limit_usd: '0.99', feed: { status: 'ok', updated_at: '2026-11-16T00:05:15Z', stale: false }, readings: [reading, { ...reading, epic: 'BTCUSD', decision_bar: false, signal: false, direction: null, ticket: null }], open_trades: [open] }));
    const scan = readScan(file);
    expect(scan).toMatchObject({ generatedAt: '2026-11-16T00:10:00Z', error: null, feedStale: false, riskLimitUsd: '0.99', title: 'Weekly momentum on the Bitcoin and Ether CFDs', nextDecisionAt: '2026-11-23T00:00:00Z' });
    expect(scan?.readings[0]).toMatchObject({ epic: 'ETHUSD', change: '-0.052100', direction: 'SHORT', decisionBar: true, ticket: { status: 'TRADE', direction: 'SHORT', riskUsd: '0.86', maxHoldHours: 168 } });
    expect(scan?.readings[1]).toMatchObject({ direction: null, ticket: null, signal: false });
    expect(scan?.openTrades).toEqual([{ sleeve: 'crypto-cfd', tradeId: 'E1', epic: 'BTCUSD', dueAt: '2026-11-16T00:05:00Z', overdue: true, beyondStop: false, atTarget: false, exitSignal: null }]);
    writeFileSync(file, JSON.stringify({ version: 2, generated_at: '2026-11-16T00:25:00Z', error: 'no daily bars in data/market/cfd/BTCUSD.json' }));
    expect(readScan(file)).toMatchObject({ error: 'no daily bars in data/market/cfd/BTCUSD.json', readings: [], openTrades: [], feedStale: false, title: null });
  });

  it('reads the practice trader and shows its sleeves running while its heartbeat is fresh (ADR-0056)', async () => {
    const { readTrader, TRADER_STALE_SECONDS } = await import('../server/sources/mission');
    const { practiceView, runningSetups } = await import('../shared/mission');
    const dir = mkdtempSync(path.join(os.tmpdir(), 'trader-'));
    const file = path.join(dir, 'trader.json');
    expect(readTrader(file)).toBeNull();
    const decision = { setup: 'crypto_hourly_breakout_v1', epic: 'BTCUSD', decided_at: '2026-11-12T10:00:00Z', direction: null, outcome: 'NO_SIGNAL', detail: 'no signal on this bar', trade_id: null };
    const setup = (key: string, sleeve: string, extra: Record<string, unknown> = {}) => ({ key, title: key, sleeve, status: 'forward_test', trial: 'T-0023', enabled: true, decides: 'hourly', open_trades: [], forward_trades: 0, forward_result_usd: '0.00', loss_cap_usd: '5', switched_off: null, last_decision: decision, ...extra });
    writeFileSync(file, JSON.stringify({ version: 1, live: true, updated_at: '2026-11-12T10:00:21Z', next_decision_at: '2026-11-12T11:00:20Z', hedging: false, entries_paused: null, errors: [], setups: [setup('crypto_hourly_breakout_v1', 'crypto-cfd'), setup('index_dip_v1', 'capital-demo', { enabled: false })] }));
    const now = Date.parse('2026-11-12T10:01:00Z');
    const trader = readTrader(file, now);
    expect(trader).toMatchObject({ live: true, fresh: true, hedging: false, nextDecisionAt: '2026-11-12T11:00:20Z' });
    expect(trader?.setups[0]).toMatchObject({ status: 'forward_test', lossCapUsd: '5', lastDecision: { epic: 'BTCUSD', outcome: 'NO_SIGNAL', direction: null } });
    expect(readTrader(file, now + TRADER_STALE_SECONDS * 1000)?.fresh).toBe(false);
    const base = { start: 100, realized: 100, openPnl: 0, markedAt: null, floor: 90, riskFraction: 0.01, maxOpenTrades: 4, curve: [], lastEventAt: null, chainOk: true, equity: 100, trades: [] };
    // the crypto sleeve has a running setup; the CFD sleeve's only setup is off
    expect(runningSetups('crypto', trader).map((x) => x.key)).toEqual(['crypto_hourly_breakout_v1']);
    expect(practiceView({ ...base, id: 'crypto' }, trader)).toMatchObject({ live: true, statusLine: 'Trader running: crypto_hourly_breakout_v1' });
    expect(practiceView({ ...base, id: 'cfd' }, trader)).toMatchObject({ live: false, statusLine: 'No trade yet' });
    // a stopped trader runs nothing
    expect(practiceView({ ...base, id: 'crypto' }, readTrader(file, now + 3_600_000))).toMatchObject({ live: false });
  });
});

// ------------------------------------------------------------------------------------- trades
describe('one trade shape for every sleeve', () => {
  const fixture = path.join(UI_ROOT, 'tests', 'fixtures', 'practice_journal.jsonl');

  it('tells a journalled trade from plan to lesson', async () => {
    const { readJournal, tradesOf } = await import('../server/sources/mission');
    const { practiceStory, rMultiple, tradeStats, tradeHref } = await import('../shared/trades');
    const [gold, silver] = tradesOf(readJournal(fixture).events).map((t) => practiceStory('cfd', t));
    expect(gold).toMatchObject({ sleeve: 'cfd', symbol: 'GOLD', marketId: 'cfd:GOLD', direction: 'long', state: 'closed' });
    expect(gold).toMatchObject({ entry: 4101.5, exit: 4233, pnl: 1.27, fees: 0.03, financing: 0.02, risk: 0.8, reason: 'Closed by hand' });
    expect(gold.plan).toMatchObject({ entry: 4100, stop: 4020, target: 4260, setup: 'breakout', invalidation: 'close back below 4050' });
    expect(gold.events.map((e) => e.kind)).toEqual(['plan', 'open', 'note', 'amend', 'close', 'review']);
    expect(gold.lesson).toEqual({ text: expect.stringContaining('closed early'), followedPlan: false });
    expect(rMultiple(gold)).toBeCloseTo(1.27 / 0.8, 10);
    expect(silver).toMatchObject({ state: 'open', pnl: null, lesson: null });
    expect(rMultiple(silver)).toBeNull();
    expect(tradeHref(gold)).toBe('/trades/cfd/G1');
    expect(tradeStats([gold, silver])).toMatchObject({ closed: 1, open: 1, wins: 1, winRate: 1, pnl: 1.27, fees: 0.03, financing: 0.02 });
    const { marketIdOf } = await import('../shared/trades');
    expect(marketIdOf('crypto', 'BTCUSD')).toBe('crypto:BTC');
    expect(marketIdOf('stocks', 'NVDA')).toBe('stocks:NVDA');
  });

  it("rebuilds the bot's round trips so that they add up to the session's own totals", async () => {
    const { stockStories } = await import('../shared/trades');
    const scenarios = listScenarios().filter((s) => s.path.includes(`${path.sep}demo${path.sep}`));
    let seen = 0;
    for (const s of scenarios) {
      const tables = loadReplayTables(s.path) as unknown as Tables;
      const stories = stockStories(tables);
      const totals = runTotals(tables);
      for (const t of stories) {
        expect(t.events.length).toBeGreaterThan(0);
        expect(t.plan.stop).not.toBeNull();
        expect(t.openedAt).not.toBeNull();
      }
      const allClosed = stories.length > 0 && stories.every((t) => t.state === 'closed') && tables.positions.every((p) => Number(p.quantity) === 0);
      if (allClosed && stories.length * 2 === totals.fills) {
        seen += 1;
        expect(stories.reduce((a, t) => a + (t.pnl as number), 0)).toBeCloseTo(Number(totals.net_trading_pnl), 6);
        expect(stories.reduce((a, t) => a + t.fees, 0)).toBeCloseTo(Number(totals.fees), 6);
      }
    }
    expect(seen).toBeGreaterThan(0);
  });
});
