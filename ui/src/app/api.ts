import { useQuery } from '@tanstack/react-query';
import type { IntelIndexReply, IntelKnownReply, IntelLatest } from '../../shared/intel';
import type { ReplayControl, ReplayScenario, SourceStatus } from '../../shared/protocol';

/** Page and API share one origin (the console server), in development and in production. */
export const API = '';

export async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(API + url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return (await r.json()) as T;
}

export const useSources = () =>
  useQuery({
    queryKey: ['sources'],
    queryFn: () => getJson<{ live: SourceStatus; scenarios: ReplayScenario[] }>('/api/sources'),
    staleTime: 30_000,
  });

export interface RegistryEvent {
  seq: number;
  event: string;
  at: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  record_hash: string;
}

export interface DatasetSummary {
  name: string;
  kind: string;
  source: string;
  venue: string;
  feed: string;
  status: string;
  blocker: string | null;
  start: string | null;
  end: string | null;
  barMinutes: number | null;
  instruments: { instrument_id: string; rows: number; first: string; last: string }[];
  corporateActions: Record<string, unknown>[];
}

export interface ResearchSummary {
  registry: { events: RegistryEvent[]; chain: { ok: boolean; breaks: number[] } };
  budget: { total: number | null; used: number };
  studies: {
    id: string;
    files: string[];
    kind: 'baseline' | 'model' | 'unknown';
    title: string | null;
    track: string | null;
    g2: string | null;
    trials: string[];
    modifiedAt: string;
  }[];
  reports: { name: string; modifiedAt: string; bytes: number }[];
  promotion: {
    gates?: Record<string, { status: string; notes?: string; evidence_refs?: string[]; evaluated_at?: string }>;
    outstanding_limitations?: string[];
    owner_enablement?: { enabled: boolean };
  } | null;
  datasets: DatasetSummary[];
}

export const useResearch = () =>
  useQuery({ queryKey: ['research'], queryFn: () => getJson<ResearchSummary>('/api/research'), staleTime: 60_000 });

export const useStudy = (id: string | null) =>
  useQuery({
    queryKey: ['research', 'study', id],
    queryFn: () => getJson<{ results: StudyResults | null; model: unknown }>(`/api/research/study/${id}`),
    enabled: id !== null,
    staleTime: 60_000,
  });

export const useReport = (name: string | null) =>
  useQuery({
    queryKey: ['research', 'report', name],
    queryFn: async () => {
      const r = await fetch(`${API}/api/research/report/${name}`);
      if (!r.ok) throw new Error('report');
      return r.text();
    },
    enabled: name !== null,
  });

export interface YamlFile {
  path: string;
  text: string;
  data: unknown;
  modifiedAt: string;
}
export interface RulesSummary {
  policy: YamlFile | null;
  paper: YamlFile | null;
  instruments: YamlFile | null;
  strategy: YamlFile | null;
  fees: YamlFile | null;
}
export const useRules = () =>
  useQuery({ queryKey: ['rules'], queryFn: () => getJson<RulesSummary>('/api/rules'), staleTime: 60_000 });

export interface HistoryBar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}
export const useHistory = (symbol: string | null, limit = 2400, tf: '5m' | '15m' | '1h' | '1d' = '5m') =>
  useQuery({
    queryKey: ['history', symbol, limit, tf],
    queryFn: () => getJson<HistoryBar[]>(`/api/history/${symbol}?limit=${limit}&tf=${tf}`),
    enabled: symbol !== null,
    staleTime: Infinity,
  });
// --- market feed (data/market, ADR-0045) ----------------------------------------------------------
export type DayRule = 'nyse' | 'utc';
export type SeriesName = '5m' | '1h' | '1d';
export type Timeframe = '5m' | '15m' | '1h' | '1d';
export interface MarketInstrument {
  id: string;
  symbol: string;
  name: string;
  source_symbol?: string;
  file: string;
  precision: number;
  has_volume: boolean;
  basis: 'trade' | 'mid';
  min_deal?: string;
  last_price?: string | null;
  last_at?: number | null;
  series: Record<SeriesName, { first: number | null; last: number | null; count: number }>;
}
export interface MarketGroup {
  id: string;
  title: string;
  source: string;
  day_rule: DayRule;
  status: 'ok' | 'partial' | 'error';
  error: string | null;
  updated_at: string | null;
  checked_at?: string | null;
  retry_after?: string | null;
  instruments: MarketInstrument[];
}
export type MarketIndex =
  | { available: true; version: number; generated_at: string; every_minutes: number; groups: MarketGroup[] }
  | { available: false; reason: string; groups: [] };
export interface MarketBars {
  id: string;
  tf: Timeframe;
  barTf: Timeframe;
  series: SeriesName;
  dayRule: DayRule;
  bars: HistoryBar[];
}

/** The feed's index; polled, because the feed rewrites it every few minutes. */
export const useMarketIndex = () =>
  useQuery({
    queryKey: ['market-index'],
    queryFn: () => getJson<MarketIndex>('/api/market'),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

export const useMarketBars = (id: string | null, tf: Timeframe, limit: number) =>
  useQuery({
    queryKey: ['market-bars', id, tf, limit],
    queryFn: () => getJson<MarketBars>(`/api/market/${encodeURIComponent(id ?? '')}?tf=${tf}&limit=${limit}`),
    enabled: id !== null,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

/** The intel feed's index (ADR-0047): sources and one summary row per instrument; polled. */
export const useIntelIndex = () =>
  useQuery({
    queryKey: ['intel-index'],
    queryFn: () => getJson<IntelIndexReply>('/api/intel'),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

/** One instrument's newest news, insider filings and pre-market snapshot. */
export const useIntel = (id: string | null) =>
  useQuery({
    queryKey: ['intel', id],
    queryFn: () => getJson<IntelLatest>(`/api/intel/${encodeURIComponent(id ?? '')}`),
    enabled: id !== null,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: false,
  });

/** What was known about an instrument at a past moment (a stored snapshot; it never changes). */
export const useIntelKnown = (id: string | null, at: string | null) =>
  useQuery({
    queryKey: ['intel-known', id, at],
    queryFn: () => getJson<IntelKnownReply>(`/api/intel/${encodeURIComponent(id ?? '')}/known?at=${encodeURIComponent(at ?? '')}`),
    enabled: id !== null && at !== null,
    staleTime: 5 * 60_000,
    retry: false,
  });

export const useHistorySymbols = () =>
  useQuery({
    queryKey: ['history-symbols'],
    queryFn: () => getJson<{ symbol: string; dataset: string }[]>('/api/history'),
    staleTime: Infinity,
  });

export async function replayControl(scenario: string, c: ReplayControl): Promise<void> {
  await fetch(`${API}/api/replay/${encodeURIComponent(scenario)}/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(c),
  });
}

// --- results.json (runs/research/<study>/results.json) -------------------------------------------
export interface Ci {
  estimate: number | string;
  lower: number | string;
  upper: number | string;
  one_sided_lower?: number | string;
  level?: number | string;
}
export interface TrialView {
  net_pnl_usd: string;
  gross_pnl_usd: string;
  fees_usd: string;
  execution_cost_usd?: string;
  trades: number | string;
  win_rate: number | string | null;
  avg_win_usd?: string;
  avg_loss_usd?: string;
  profit_factor: number | string | null;
  payoff_ratio?: number | string | null;
  sharpe_annualised?: number | string | null;
  max_drawdown_usd: string;
  max_drawdown_fraction?: number | string;
  sessions: number;
  active_sessions?: number;
  exposure_fraction?: number | string;
  mean_session_ci?: Ci;
  per_trade_ci?: Ci;
  exit_reasons?: Record<string, number>;
  candidates?: { total: number; by_status?: Record<string, number>; by_reason?: Record<string, number> };
  windows?: string[];
  window_nets_usd?: (string | number)[];
  window_trades?: number[];
  negative_windows?: number;
}
export interface StudyResults {
  study_id: string;
  title?: string;
  trials: Record<
    string,
    { family: string; overrides?: Record<string, unknown>; views: Record<string, TrialView> }
  >;
  g2?: { status: string; note?: string; checks?: Record<string, boolean> };
  walk_forward?: {
    folds: { fold: number; train: string[]; validation: string[]; test: string[]; selected: string }[];
    selection_rule?: string;
  };
  comparators?: Record<string, unknown>;
  holdout_unlocked?: boolean;
  evidence_class?: string;
}
