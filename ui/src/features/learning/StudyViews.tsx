import { useState } from 'react';
import { CheckCircle2, CircleSlash, XCircle } from 'lucide-react';
import type { StudyResults } from '../../app/api';
import { Columns, IntervalPlot } from '../../charts/svg';
import { usd } from '../../design/format';
import { Card, Chip, KV } from '../../design/primitives';
import p from '../pages.module.css';
import h from './learning.module.css';

const monthLabel = (iso: string) =>
  new Intl.DateTimeFormat('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' }).format(new Date(iso)).replace(' ', ' ’');

/**
 * Every trial against every walk-forward window: the one picture that shows whether any rule ever
 * made money consistently. Diverging colour around zero, the value printed in each cell.
 */
export function TrialHeatmap({ study, selected, onSelect }: { study: StudyResults; selected: string | null; onSelect: (id: string) => void }) {
  const trials = Object.entries(study.trials);
  const windows = trials[0]?.[1].views.base?.windows ?? [];
  const values = trials.flatMap(([, t]) => (t.views.base?.window_nets_usd ?? []).map(Number));
  const ext = Math.max(0.01, ...values.map((v) => Math.abs(v)));
  const [tip, setTip] = useState<string | null>(null);
  const colour = (v: number) => {
    if (Math.abs(v) < 1e-9) return 'var(--fill)';
    const a = Math.min(1, 0.18 + (Math.abs(v) / ext) * 0.82);
    return `color-mix(in srgb, ${v > 0 ? 'var(--gain-mark)' : 'var(--loss-mark)'} ${Math.round(a * 100)}%, var(--surface))`;
  };
  const textOn = (v: number) => (Math.abs(v) / ext > 0.55 ? '#fff' : 'var(--ink)');
  return (
    <div className={h.heatWrap}>
      <div
        className={h.heat}
        style={{ gridTemplateColumns: `minmax(110px, max-content) repeat(${windows.length}, minmax(${windows.length > 16 ? 32 : 44}px, 1fr))` }}
      >
        <span />
        {windows.map((w, i) => (
          <span key={w} className={h.heatCol}>
            {/* Dense studies label every other window; every cell still has its tooltip. */}
            {windows.length <= 16 || i % 2 === 0 ? monthLabel(w) : ''}
          </span>
        ))}
        {trials.map(([id, t]) => {
          const nets = (t.views.base?.window_nets_usd ?? []).map(Number);
          const trades = t.views.base?.window_trades ?? [];
          const ov = t.overrides as { atr_stop_multiplier?: string; reward_risk_multiple?: string } | undefined;
          return (
            <div key={id} className={`${h.heatRow} ${selected === id ? h.heatRowOn : ''}`} onClick={() => onSelect(id)} style={{ display: 'contents' }}>
              <button className={h.heatLabel} onClick={() => onSelect(id)}>
                <b>{id}</b> {t.family.replace('CANDIDATE_', '')}
                {ov?.atr_stop_multiplier ? ` · ${ov.atr_stop_multiplier}×${ov.reward_risk_multiple}` : ''}
              </button>
              {nets.map((v, i) => (
                <span
                  key={i}
                  className={`${h.cell} ${selected === id ? h.cellOn : ''}`}
                  style={{ background: colour(v), color: textOn(v) }}
                  onMouseEnter={() => setTip(`${id} · ${monthLabel(windows[i])}: ${usd(v, { sign: true, precise: true })} on ${trades[i] ?? 0} trades`)}
                  onMouseLeave={() => setTip(null)}
                  onClick={() => onSelect(id)}
                >
                  {Math.abs(v) < 1e-9 ? '·' : Math.abs(v) >= 10 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(1) : v.toFixed(2)}
                </span>
              ))}
            </div>
          );
        })}
      </div>
      <div className={h.heatFoot}>
        <span>{tip ?? 'Net USD per window (base costs). Hover a cell for trades; click a row to inspect the trial.'}</span>
        <span className={h.scale}>
          <i style={{ background: 'var(--loss-mark)' }} /> loss
          <i style={{ background: 'var(--fill)' }} /> no trades
          <i style={{ background: 'var(--gain-mark)' }} /> gain
        </span>
      </div>
    </div>
  );
}

const G2_LABELS: Record<string, string> = {
  sessions_at_least_120: 'At least 120 sessions',
  effective_sessions_at_least_120: 'At least 120 effective sessions',
  oos_trades_at_least_100: 'At least 100 out-of-sample trades',
  positive_lower_bound_per_trade_net: 'Per-trade net: lower bound above zero',
  positive_lower_bound_session_net: 'Per-session net: lower bound above zero',
  selection_adjusted_lower_bound_positive: 'Still positive after correcting for trying many trials',
  best_reality_check_row_is_a_model_filter: 'The best row in the reality check is a model filter',
  filter_beats_rule_only_lower_bound_positive: 'Filter beats the rule alone (lower bound above zero)',
  filter_beats_random_control_lower_bound_positive: 'Filter beats random vetoes (lower bound above zero)',
  filter_beats_turnover_control_lower_bound_positive: 'Filter beats turnover-matched vetoes (lower bound above zero)',
};

export function G2Checks({ g2, studyId }: { g2: { status: string; note?: string; checks?: Record<string, boolean> } | undefined; studyId: string }) {
  if (!g2) return null;
  return (
    <Card title="G2 checks" sub={`${studyId}: ${g2.status.replaceAll('_', ' ').toLowerCase()}`}>
      {Object.entries(g2.checks ?? {}).map(([k, ok]) => (
        <div key={k} className={p.check}>
          {ok ? <CheckCircle2 size={16} color="var(--good)" /> : <XCircle size={16} color="var(--critical)" />}
          {G2_LABELS[k] ?? k.replaceAll('_', ' ')}
        </div>
      ))}
      {g2.note && <p className={p.small + ' ' + p.muted} style={{ marginTop: 8 }}>{g2.note}</p>}
    </Card>
  );
}

// --- Model filter studies (M3, model_results.json) ------------------------------------------------

interface Ci {
  estimate: number;
  lower: number;
  upper: number;
}
interface Fold {
  fold: number;
  status: string;
  reason?: string;
  test: string[];
  labels?: { train_positives: number; train_rows: number; validation_positives: number; validation_rows: number; test_positives: number; test_rows: number };
  runs?: Record<string, { net_usd?: string; trades?: string; filter_accepts?: string; filter_decisions?: string }>;
}
export interface ModelStudy {
  study_id: string;
  title?: string;
  g2?: { status: string; note?: string; checks?: Record<string, boolean> };
  trials: Record<
    string,
    {
      family: string;
      model_type?: string;
      folds_trained?: number;
      folds: Fold[];
      variants: Record<string, { net_pnl_usd?: number | string; trades?: number; mean_net_pnl_usd?: number; mean_trades?: number; filter_vetoes?: number }>;
      paired_differences?: Record<string, Ci>;
    }
  >;
}

const VARIANT_LABEL: Record<string, string> = {
  rule_only: 'Rule alone',
  model_filter: 'With the model filter',
  random_control: 'Random vetoes (control)',
  turnover_control: 'Same-turnover vetoes (control)',
};

/** A plain-language reading of what a model-filter trial actually showed. */
function verdict(t: ModelStudy['trials'][string]): string {
  const rule = Number(t.variants.rule_only?.net_pnl_usd ?? 0);
  const model = Number(t.variants.model_filter?.net_pnl_usd ?? 0);
  const trained = t.folds_trained ?? t.folds.filter((f) => f.status === 'TRAINED').length;
  if (trained === 0) {
    return `The filter could not be trained in any of the ${t.folds.length} folds, so it vetoed every candidate and made no trades. It "beats" the rule (${usd(rule, { sign: true, precise: true })}) only by not trading, which is why it does not beat the random or turnover-matched controls. Profitable candidates are too rare in this data to learn from.`;
  }
  return `The filter was trained in ${trained} of ${t.folds.length} folds; with it the strategy made ${usd(model, { sign: true, precise: true })} against ${usd(rule, { sign: true, precise: true })} for the rule alone.`;
}

export function ModelTrialView({ model, id }: { model: ModelStudy; id: string }) {
  const t = model.trials[id];
  if (!t) return null;
  const net = (k: string) => {
    const v = t.variants[k];
    if (!v) return 0;
    return Number(v.net_pnl_usd ?? v.mean_net_pnl_usd ?? 0);
  };
  const trades = (k: string) => {
    const v = t.variants[k];
    return Number(v?.trades ?? v?.mean_trades ?? 0);
  };
  const pd = t.paired_differences ?? {};
  return (
    <div className={p.stack}>
      <div className={p.why} style={{ marginBottom: 0 }}>
        <b>What this showed.</b> {verdict(t)}
      </div>
      <div className={p.grid}>
        <section className={p.s6}>
          <div className={p.small + ' ' + p.muted} style={{ marginBottom: 10 }}>Net result over all test windows (USD)</div>
          <Columns
            data={['rule_only', 'model_filter', 'random_control', 'turnover_control'].map((k) => ({
              label: VARIANT_LABEL[k].replace(' (control)', '').replace('With the ', '').replace('Same-turnover', 'Turnover'),
              value: net(k),
              hint: `${VARIANT_LABEL[k]} · ${trades(k)} trades${k.endsWith('control') ? ' (mean of seeds)' : ''}`,
            }))}
            format={(n) => usd(n, { precise: true })}
            height={200}
          />
          <div className={p.small + ' ' + p.muted} style={{ margin: '18px 0 10px' }}>Per-session difference, model minus each comparison (95% interval)</div>
          <IntervalPlot
            rows={Object.entries(pd).map(([k, v]) => ({
              label: k.replace('model_minus_', 'vs ').replace('rule_only', 'rule').replace('random_control', 'random').replace('turnover_control', 'turnover'),
              lower: Number(v.lower),
              estimate: Number(v.estimate),
              upper: Number(v.upper),
            }))}
            format={(n) => `${n < 0 ? '−' : ''}$${Math.abs(n).toFixed(3)}`}
          />
        </section>
        <section className={p.s6}>
          <KV
            items={[
              ['Model', `${t.model_type ?? 'logistic'} filter on ${t.family.split('+')[0].replace('CANDIDATE_', 'Candidate ')}`],
              ['Folds trained', <span key="f" className={p.inline}>{t.folds_trained ?? 0} of {t.folds.length} {(t.folds_trained ?? 0) === 0 && <Chip style={{ color: 'var(--loss)' }}>none</Chip>}</span>],
              ['Candidates vetoed', (t.variants.model_filter?.filter_vetoes ?? 0).toLocaleString('en-US')],
            ]}
          />
          <div className={p.small + ' ' + p.muted} style={{ margin: '18px 0 8px' }}>Each fold: profitable candidates available to learn from</div>
          <div className={p.list}>
            {t.folds.map((f) => (
              <div key={f.fold} className={p.row} style={{ padding: '9px 0' }}>
                {f.status === 'TRAINED' ? <CheckCircle2 size={16} color="var(--good)" /> : <CircleSlash size={16} color="var(--ink-3)" />}
                <div className={p.rowMain}>
                  <div className={p.rowTitle} style={{ fontSize: 13 }}>
                    Fold {f.fold} · test {f.test[0].slice(0, 7)}
                  </div>
                  <div className={p.rowSub} style={{ whiteSpace: 'normal' }}>
                    {f.labels
                      ? `train ${f.labels.train_positives} of ${f.labels.train_rows.toLocaleString('en-US')} profitable · validation ${f.labels.validation_positives} of ${f.labels.validation_rows.toLocaleString('en-US')}`
                      : ''}
                    {f.reason ? ` — ${f.reason}` : ''}
                  </div>
                </div>
                <Chip>{f.status.toLowerCase()}</Chip>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
