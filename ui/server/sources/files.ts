/**
 * Research and configuration files, read-only (docs/observability.md sections 2 and 5).
 * Watched for changes so a finished study or an edited policy shows up without a reload.
 */
import { existsSync, readdirSync, readFileSync, realpathSync, statSync, watch, type FSWatcher } from 'node:fs';
import path from 'node:path';
import { parse as parseYaml } from 'yaml';
import { PAPER_CONFIG, repoPath } from '../config';

export interface RegistryEvent {
  seq: number;
  event: string;
  at: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  record_hash: string;
}

/**
 * Python's json module writes bare Infinity / -Infinity / NaN (e.g. a profit factor with no losing
 * trade). Strict JSON rejects them, so they become the strings "Infinity" / "-Infinity" / "NaN".
 */
export function parsePythonJson(text: string): unknown {
  return JSON.parse(text.replace(/([:\[,]\s*)(-?Infinity|NaN)(?=\s*[,\]}])/g, '$1"$2"'));
}

function readJson<T>(rel: string): T | null {
  const p = repoPath(rel);
  if (!existsSync(p)) return null;
  try {
    return parsePythonJson(readFileSync(p, 'utf8')) as T;
  } catch {
    return null;
  }
}

function readYaml(rel: string): { path: string; text: string; data: unknown; modifiedAt: string } | null {
  const p = repoPath(rel);
  if (!existsSync(p)) return null;
  const text = readFileSync(p, 'utf8');
  return { path: rel.replaceAll('\\', '/'), text, data: parseYaml(text), modifiedAt: statSync(p).mtime.toISOString() };
}

export function readRegistry(): { events: RegistryEvent[]; chain: { ok: boolean; breaks: number[] } } {
  const p = repoPath('research', 'experiment_registry.jsonl');
  if (!existsSync(p)) return { events: [], chain: { ok: true, breaks: [] } };
  const events = readFileSync(p, 'utf8')
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as RegistryEvent);
  // Linkage check only (each prev_hash equals the previous record_hash). Full verification
  // recomputes the hashes: `uv run trader research status`.
  const breaks: number[] = [];
  for (let i = 1; i < events.length; i++) {
    if (events[i].prev_hash !== events[i - 1].record_hash) breaks.push(events[i].seq);
  }
  return { events, chain: { ok: breaks.length === 0, breaks } };
}

export interface StudySummary {
  id: string;
  files: string[];
  kind: 'baseline' | 'model' | 'unknown';
  title: string | null;
  track: string | null;
  g2: string | null;
  trials: string[];
  modifiedAt: string;
}

export function listStudies(): StudySummary[] {
  const dir = repoPath('runs', 'research');
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => {
      const files = readdirSync(path.join(dir, d.name)).filter((f) => f.endsWith('.json'));
      const kind: StudySummary['kind'] = files.includes('model_results.json') ? 'model' : files.includes('results.json') ? 'baseline' : 'unknown';
      const file = kind === 'model' ? 'model_results.json' : 'results.json';
      const doc = kind === 'unknown' ? null : readJson<{ title?: string; track?: string; g2?: { status?: string }; trials?: Record<string, unknown> }>(`runs/research/${d.name}/${file}`);
      return {
        id: d.name,
        files,
        kind,
        title: doc?.title ?? null,
        track: doc?.track ?? null,
        g2: doc?.g2?.status ?? null,
        trials: Object.keys(doc?.trials ?? {}),
        modifiedAt: statSync(path.join(dir, d.name, kind === 'unknown' ? '.' : file)).mtime.toISOString(),
      };
    })
    .sort((a, b) => a.modifiedAt.localeCompare(b.modifiedAt));
}

const SAFE_NAME = /^[A-Za-z0-9_.-]+$/;

export function readStudy(id: string): { results: unknown; model: unknown } | null {
  if (!SAFE_NAME.test(id)) return null;
  const results = readJson(`runs/research/${id}/results.json`);
  const model = readJson(`runs/research/${id}/model_results.json`);
  if (results === null && model === null) return null;
  return { results, model };
}

export function listReports(): { name: string; modifiedAt: string; bytes: number }[] {
  const dir = repoPath('research', 'reports');
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith('.md'))
    .map((f) => {
      const s = statSync(path.join(dir, f));
      return { name: f, modifiedAt: s.mtime.toISOString(), bytes: s.size };
    });
}

export function readReport(name: string): string | null {
  if (!SAFE_NAME.test(name) || !name.endsWith('.md')) return null;
  const p = repoPath('research', 'reports', name);
  return existsSync(p) ? readFileSync(p, 'utf8') : null;
}

export function researchSummary() {
  const registry = readRegistry();
  const budgets = registry.events.filter((e) => e.event === 'BUDGET_DECISION');
  const budget = budgets.length ? Number(budgets[budgets.length - 1].payload.new_budget) : null;
  const registered = registry.events.filter((e) => e.event === 'REGISTER').length;
  const promotion = readJson<Record<string, unknown>>('research/promotion_manifest.json');
  const data = readJson<{ history?: Record<string, unknown>[]; datasets?: Record<string, unknown>[] }>(
    'research/data_manifest.json',
  );
  return {
    registry,
    budget: { total: budget, used: registered },
    studies: listStudies(),
    reports: listReports(),
    promotion,
    datasets: (data?.history ?? []).map((h) => ({
      name: h.name,
      kind: h.kind,
      source: h.source,
      venue: h.venue,
      feed: h.feed,
      status: h.status,
      blocker: h.blocker ?? null,
      start: (h.request as Record<string, unknown> | undefined)?.start ?? null,
      end: (h.request as Record<string, unknown> | undefined)?.end ?? null,
      barMinutes: (h.request as Record<string, unknown> | undefined)?.bar_minutes ?? null,
      instruments: ((h.files as Record<string, unknown>[] | undefined) ?? []).map((f) => ({
        instrument_id: f.instrument_id,
        rows: f.rows,
        first: f.first_start,
        last: f.last_start,
      })),
      corporateActions: h.corporate_actions ?? [],
    })),
  };
}

/** A configs-relative path named in the stocks book's configuration, when it is a plain file of that folder. */
export function paperRef(value: unknown, folder: string, fallback: string): string {
  const ref = typeof value === 'string' ? value.split('#')[0].trim() : '';
  return new RegExp(`^${folder}/[a-z0-9_]+\.yaml$`).test(ref) ? `configs/${ref}` : fallback;
}

export function rulesSummary() {
  const paper = readYaml(PAPER_CONFIG);
  const cfg = (paper?.data ?? {}) as Record<string, unknown>;
  const account = ((cfg.accounts as Record<string, unknown>[] | undefined) ?? [])[0] ?? {};
  const files = {
    // The policy, fees and instruments the paper bot actually runs with (ADR-0043).
    policy: readYaml(paperRef(cfg.policy_file, 'policy', 'configs/policy/base.yaml')),
    paper,
    instruments: readYaml(paperRef(account.instruments, 'instruments', 'configs/instruments/stocks_research.yaml')),
    strategy: readYaml('src/trader/strategies/specs/candidate_a_v1.yaml'),
    fees: readYaml(paperRef(account.fee_schedule, 'fees', 'configs/fees/alpaca_us_equities.yaml')),
  };
  return files;
}

export function corporateActions(): { instrument_id: string; ex_date: string; ratio: number }[] {
  const data = readJson<{ history?: { corporate_actions?: Record<string, unknown>[] }[] }>('research/data_manifest.json');
  const out: { instrument_id: string; ex_date: string; ratio: number }[] = [];
  for (const h of data?.history ?? []) {
    for (const a of h.corporate_actions ?? []) {
      if (a.type !== 'SPLIT') continue;
      out.push({
        instrument_id: String(a.instrument_id),
        ex_date: String(a.ex_date),
        ratio: Number(a.new_rate) / Number(a.old_rate),
      });
    }
  }
  return out;
}

/** Watch research outputs and configs; calls back with the scope that changed (debounced). */
export function watchFiles(onChange: (scope: 'research' | 'rules') => void): () => void {
  const watchers: FSWatcher[] = [];
  const pending = new Set<'research' | 'rules'>();
  let timer: NodeJS.Timeout | null = null;
  const fire = (scope: 'research' | 'rules') => {
    pending.add(scope);
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      for (const s of pending) onChange(s);
      pending.clear();
    }, 400);
  };
  const add = (rel: string, scope: 'research' | 'rules') => {
    const p = repoPath(rel);
    if (!existsSync(p)) return;
    try {
      // Real long path: an 8.3 short path (C:\Users\SOMEUS~1\...) aborts Node's recursive watch on Windows.
      watchers.push(watch(realpathSync.native(p), { recursive: true }, () => fire(scope)));
    } catch {
      /* recursive watch unsupported: changes show on next reload */
    }
  };
  add('research', 'research');
  add(path.join('runs', 'research'), 'research');
  add('configs', 'rules');
  return () => watchers.forEach((w) => w.close());
}
