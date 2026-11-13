/**
 * Trader Console server: one process, one address (http://127.0.0.1:4317) for the console API, the
 * Sunshine Desk (/desk, its API under /api/desk) and the page itself (Vite middleware with hot
 * reload in development, the built dist/ in production).
 *
 * A read-only observer of the bot (docs/observability.md): no route writes to the database, the
 * research log, configs or data. The Desk writes only its own notes under <repo>/.desk/.
 * Binds 127.0.0.1 only and refuses foreign Host headers.
 */
import { missionState } from './sources/mission';
import { existsSync, readFileSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { gzipSync } from 'node:zlib';
import Fastify, { type FastifyInstance, type FastifyReply } from 'fastify';
import fastifyMiddie from '@fastify/middie';
import fastifyStatic from '@fastify/static';
import type { Timeframe } from '../shared/aggregate';
import type { ReplayControl, SourceId, StreamMessage } from '../shared/protocol';
import { HOST, PORT, UI_ROOT } from './config';
import { LEGACY_DESK_PORT } from './desk/guard';
import { registerDesk } from './desk/index';
import { assertLoopbackBind, isAllowedHost, isAllowedOrigin } from './guard';
import { readReport, readStudy, researchSummary, rulesSummary, watchFiles } from './sources/files';
import { DEFAULT_DATASET, HISTORY_DATASETS, historyBars, historySymbols, TIMEFRAMES, warmHistory } from './sources/history';
import { LiveSource } from './sources/live';
import { intelFor, intelIndexReply, intelKnown } from './sources/intel';
import { MARKET_TIMEFRAMES, marketBars, marketIndexReply } from './sources/market';
import { listScenarios, ReplaySource } from './sources/replay';
import type { Source } from './store';

/** Routes (API + Desk). The page is attached separately: attachDev or attachDist. */
export function buildServer(): FastifyInstance {
  const app = Fastify({ logger: { level: process.env.UI_LOG_LEVEL ?? 'warn' } });

  app.addHook('onRequest', async (req, reply) => {
    if (!isAllowedHost(req.headers.host)) return reply.code(403).send({ error: 'localhost only' });
    // One origin serves page and API, so no CORS headers are ever sent. Desk writes have their
    // own, stricter guard (server/desk/guard.ts); the replay controls need a loopback Origin.
    if (req.url.startsWith('/api/desk/')) return;
    if (req.method !== 'GET' && req.method !== 'HEAD' && !isAllowedOrigin(req.headers.origin)) {
      return reply.code(403).send({ error: 'cross-origin request refused' });
    }
  });

  // gzip JSON bodies (history and study files run 100-300 KB). A root hook applies to every route;
  // the SSE streams hijack their replies and never pass through onSend.
  app.addHook('onSend', async (req, reply, payload) => {
    if (typeof payload !== 'string' || payload.length < 2048) return payload;
    if (!/\bgzip\b/.test(String(req.headers['accept-encoding'] ?? ''))) return payload;
    reply.header('content-encoding', 'gzip');
    reply.header('vary', 'Accept-Encoding');
    reply.removeHeader('content-length');
    return gzipSync(payload, { level: 6 });
  });

  const live = new LiveSource();
  live.start();
  const warm = setTimeout(warmHistory, 1500);
  const replays = new Map<string, { source: ReplaySource; idleSince: number | null }>();
  const sseClients = new Set<(m: StreamMessage) => void>();

  const getSource = (id: string): Source | null => {
    if (id === 'live') return live;
    if (!id.startsWith('replay:')) return null;
    const scenarioId = id.slice('replay:'.length);
    const existing = replays.get(scenarioId);
    if (existing) return existing.source;
    const scenario = listScenarios().find((s) => s.id === scenarioId);
    if (!scenario) return null;
    const source = new ReplaySource(scenario);
    source.start();
    replays.set(scenarioId, { source, idleSince: null });
    return source;
  };

  // Dispose replay sessions nobody has watched for 10 minutes.
  const sweeper = setInterval(() => {
    const now = Date.now();
    for (const [id, r] of replays) {
      if (r.source.subscriberCount > 0) r.idleSince = null;
      else if (r.idleSince === null) r.idleSince = now;
      else if (now - r.idleSince > 600_000) {
        r.source.dispose();
        replays.delete(id);
      }
    }
  }, 30_000);

  const stopWatching = watchFiles((scope) => {
    for (const send of sseClients) send({ type: 'files', scopes: [scope] });
  });

  app.addHook('onClose', async () => {
    clearTimeout(warm);
    clearInterval(sweeper);
    stopWatching();
    live.dispose();
    for (const r of replays.values()) r.source.dispose();
  });

  app.get('/api/health', async () => ({ ok: true, app: 'trader-console', live: live.status().state }));

  app.get('/api/sources', async () => ({ live: live.status(), scenarios: listScenarios() }));

  app.get<{ Querystring: { source?: string } }>('/api/stream', (req, reply: FastifyReply) => {
    const id = (req.query.source ?? 'live') as SourceId;
    const source = getSource(id);
    if (!source) {
      void reply.code(404).send({ error: `unknown source ${id}` });
      return;
    }
    reply.hijack();
    const raw = reply.raw;
    raw.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    const send = (m: StreamMessage) => {
      raw.write(`event: ${m.type}\ndata: ${JSON.stringify(m)}\n\n`);
    };
    const unsubscribe = source.subscribe(send);
    sseClients.add(send);
    const keepAlive = setInterval(() => raw.write(': keep-alive\n\n'), 15_000);
    req.raw.on('close', () => {
      clearInterval(keepAlive);
      unsubscribe();
      sseClients.delete(send);
    });
  });

  app.post<{ Params: { scenario: string }; Body: ReplayControl }>('/api/replay/:scenario/control', async (req, reply) => {
    const r = replays.get(req.params.scenario);
    if (!r) return reply.code(404).send({ error: 'replay not running' });
    const body = req.body ?? ({} as ReplayControl);
    if (!['play', 'pause', 'speed', 'seek', 'restart', 'next'].includes(body.action)) {
      return reply.code(400).send({ error: 'unknown action' });
    }
    r.source.control(body);
    return r.source.status();
  });

  app.get('/api/research', async () => researchSummary());
  app.get<{ Params: { id: string } }>('/api/research/study/:id', async (req, reply) => {
    const s = readStudy(req.params.id);
    return s ?? reply.code(404).send({ error: 'no such study' });
  });
  app.get<{ Params: { name: string } }>('/api/research/report/:name', async (req, reply) => {
    const md = readReport(req.params.name);
    if (md === null) return reply.code(404).send({ error: 'no such report' });
    return reply.type('text/markdown; charset=utf-8').send(md);
  });

  app.get('/api/rules', async () => rulesSummary());

  // The mission's sleeves: practice journals and the stocks sleeve's start (ADR-0046).
  app.get('/api/mission', async (_req, reply) => reply.header('Cache-Control', 'no-store').send(missionState()));

  app.get('/api/history', async () => historySymbols().map(({ symbol, dataset }) => ({ symbol, dataset })));
  app.get<{ Params: { symbol: string }; Querystring: { before?: string; limit?: string; tf?: string; dataset?: string } }>(
    '/api/history/:symbol',
    async (req, reply) => {
      const tf = (req.query.tf ?? '5m') as Timeframe;
      if (!TIMEFRAMES.includes(tf)) return reply.code(400).send({ error: 'tf must be 5m, 15m, 1h or 1d' });
      const dataset = req.query.dataset ?? DEFAULT_DATASET;
      if (!HISTORY_DATASETS.includes(dataset)) return reply.code(400).send({ error: 'unknown dataset' });
      const before = req.query.before ? Number(req.query.before) : null;
      const limit = Math.min(20000, Math.max(1, Number(req.query.limit ?? 2400)));
      return historyBars(req.params.symbol, Number.isFinite(before) ? before : null, limit, tf, dataset);
    },
  );

  registerMarketRoutes(app);
  registerIntelRoutes(app);
  registerDesk(app);

  return app;
}

/**
 * The dashboard's market feed: the index and one instrument's bars, read from data/market (written
 * by `python -m trader.market watch`). Only ids the index lists are served. `root` is for tests.
 */
export function registerMarketRoutes(app: FastifyInstance, root?: string): void {
  app.get('/api/market', async () => marketIndexReply(root));
  app.get<{ Params: { id: string }; Querystring: { tf?: string; limit?: string } }>('/api/market/:id', async (req, reply) => {
    const tf = (req.query.tf ?? '5m') as Timeframe;
    if (!MARKET_TIMEFRAMES.includes(tf)) return reply.code(400).send({ error: 'tf must be 5m, 15m, 1h or 1d' });
    const limit = Number(req.query.limit ?? 600);
    if (!Number.isFinite(limit)) return reply.code(400).send({ error: 'limit must be a number' });
    const bars = marketBars(req.params.id, tf, limit, root);
    return bars ?? reply.code(404).send({ error: 'unknown market id' });
  });
}

/**
 * The intel feed (ADR-0047): the index, one instrument's newest items, and what was known about
 * an instrument at a past moment, read from data/intel (written by `python -m trader.intel
 * watch`). Only ids the index lists are served. `root` is for tests.
 */
export function registerIntelRoutes(app: FastifyInstance, root?: string): void {
  app.get('/api/intel', async () => intelIndexReply(root));
  app.get<{ Params: { id: string } }>('/api/intel/:id', async (req, reply) => {
    return intelFor(req.params.id, root) ?? reply.code(404).send({ error: 'unknown intel id' });
  });
  app.get<{ Params: { id: string }; Querystring: { at?: string } }>('/api/intel/:id/known', async (req, reply) => {
    const at = req.query.at ?? '';
    if (!/^\d{4}-\d{2}-\d{2}T[0-9:.]{5,15}(Z|[+-]\d{2}:\d{2})$/.test(at) || !Number.isFinite(Date.parse(at))) {
      return reply.code(400).send({ error: 'at must be an ISO time with a zone, e.g. 2026-11-10T15:01:07Z' });
    }
    return intelKnown(req.params.id, at, root) ?? reply.code(404).send({ error: 'unknown intel id' });
  });
}

/** A page path (not the API, not a file): answered with the SPA's index.html. */
const isPagePath = (url: string) => {
  const pathOnly = url.split('?')[0];
  return !pathOnly.startsWith('/api/') && !/\.[a-z0-9]+$/i.test(pathOnly);
};

/** Development: Vite runs inside this server (middleware mode), hot reload on the same port. */
export async function attachDev(app: FastifyInstance): Promise<void> {
  const { createServer } = await import('vite');
  await app.register(fastifyMiddie);
  const vite = await createServer({
    root: UI_ROOT,
    configFile: path.join(UI_ROOT, 'vite.config.ts'),
    appType: 'custom',
    server: { middlewareMode: true, hmr: { server: app.server } },
  });
  app.use(vite.middlewares);
  app.addHook('onClose', async () => vite.close());
  app.setNotFoundHandler(async (req, reply) => {
    if (req.method !== 'GET' || !isPagePath(req.url)) return reply.code(404).send({ error: 'not found' });
    const html = await vite.transformIndexHtml(req.url, readFileSync(path.join(UI_ROOT, 'index.html'), 'utf8'));
    return reply.type('text/html; charset=utf-8').header('Cache-Control', 'no-store').send(html);
  });
}

/** Production: the built page from dist/ (run `pnpm run build` first). */
export function attachDist(app: FastifyInstance): boolean {
  const dist = path.join(UI_ROOT, 'dist');
  if (!existsSync(path.join(dist, 'index.html'))) return false;
  // wildcard: files are looked up per request, so a rebuild needs no restart.
  void app.register(fastifyStatic, { root: dist, wildcard: true });
  app.setNotFoundHandler((req, reply) => {
    if (req.method !== 'GET' || !isPagePath(req.url)) return reply.code(404).send({ error: 'not found' });
    return reply.sendFile('index.html');
  });
  return true;
}

/** The Desk used to live on its own port; old bookmarks land on /desk. Loopback only. */
function legacyDeskRedirect(): http.Server {
  const target = `http://${HOST}:${PORT}/desk`;
  const server = http.createServer((_req, res) => {
    res.writeHead(301, { Location: target });
    res.end();
  });
  server.on('error', () => undefined); // the port is taken (e.g. an old Desk): skip quietly
  server.listen(LEGACY_DESK_PORT, HOST);
  return server;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]).replace(/\.ts$/, '') === path.resolve(UI_ROOT, 'server', 'index');
if (isMain) {
  const dev = process.argv.includes('--dev');
  const serveDist = process.argv.includes('--serve-dist');
  assertLoopbackBind(HOST);
  const app = buildServer();
  const page = dev ? attachDev(app) : Promise.resolve(serveDist ? attachDist(app) : false);
  page
    .then((attached) => {
      if (serveDist && attached === false) {
        console.error('dist/ is missing: run `pnpm run build` first (or use `npm run dev`).');
        process.exit(1);
      }
      return app.listen({ host: HOST, port: PORT });
    })
    .then(() => {
      legacyDeskRedirect(); // closes with the process
      console.log(`Trader Console on http://${HOST}:${PORT}${dev ? ' (development, hot reload)' : serveDist ? ' (production build)' : ' (API only)'}`);
    })
    .catch((err) => {
      console.error(err);
      process.exit(1);
    });
}
