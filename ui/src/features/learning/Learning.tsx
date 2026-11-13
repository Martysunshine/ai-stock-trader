import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Marked } from 'marked';
import { CheckCircle2, CircleDashed, Link2, Lock, XCircle } from 'lucide-react';
import { useReport, useResearch, useStudy, type RegistryEvent, type TrialView } from '../../app/api';
import { useConsole } from '../../app/store';
import { Columns, HBars, IntervalPlot } from '../../charts/svg';
import { day, pct, usd } from '../../design/format';
import { Card, Chip, CopyCommand, Delta, Empty, KV, Meter, Segmented, Sheet, styles as ps } from '../../design/primitives';
import { G2Checks, ModelTrialView, TrialHeatmap, type ModelStudy } from './StudyViews';
import p from '../pages.module.css';
import h from './learning.module.css';

interface TrialSummary {
  id: string;
  study: string;
  family: string;
  track: string;
  registeredAt: string;
  params: string;
  hypothesis: string;
  universe: string[];
  result: RegistryEvent | null;
}


const GATE_NAMES: Record<string, string> = {
  G0: 'Broker & economics',
  G1: 'Software integrity',
  G2: 'Research evidence',
  G3: 'Forward paper',
  G4: 'Live readiness',
};

/** Older results files carry no track field; the study id names it. */
const trackOf = (studyId: string) => (studyId.startsWith('crypto') ? 'crypto' : studyId.startsWith('stocks') ? 'stocks' : '');

const num = (v: unknown) => (v === null || v === undefined || v === '' ? null : Number(v));

export default function Learning() {
  const research = useResearch();
  const tz = useConsole((s) => s.tz);
  const [params, setParams] = useSearchParams();
  const [view, setView] = useState<'base' | 'stressed'>('base');
  const [report, setReport] = useState<string | null>(null);
  const reportText = useReport(report);
  const data = research.data;

  const trials = useMemo<TrialSummary[]>(() => {
    const events = data?.registry.events ?? [];
    const results = new Map<string, RegistryEvent>();
    for (const e of events) if (e.event === 'RESULT') results.set(String(e.payload.trial_id), e);
    return events
      .filter((e) => e.event === 'REGISTER')
      .map((e) => {
        const t = (e.payload.trial ?? {}) as Record<string, unknown>;
        const rule = (t.rule ?? {}) as { overrides?: Record<string, string> };
        const o = rule.overrides ?? {};
        const id = String(t.trial_id);
        return {
          id,
          study: String(t.study_id),
          family: String(t.family ?? ''),
          track: String(t.track ?? ''),
          registeredAt: e.at,
          params: o.atr_stop_multiplier ? `ATR ${o.atr_stop_multiplier} × R ${o.reward_risk_multiple}` : '—',
          hypothesis: String(t.hypothesis ?? ''),
          universe: (t.universe as string[]) ?? [],
          result: results.get(id) ?? null,
        };
      });
  }, [data]);

  const selectedId = params.get('trial') ?? trials.find((t) => t.result)?.id ?? trials[0]?.id ?? null;
  const selected = trials.find((t) => t.id === selectedId) ?? null;
  const studies = data?.studies ?? [];
  const hasOutput = studies.some((s) => s.id === selected?.study);
  const study = useStudy(selected && hasOutput ? selected.study : null);
  const studyResults = study.data?.results ?? null;
  const modelStudy = (study.data?.model ?? null) as ModelStudy | null;
  const modelTrial = selected && modelStudy?.trials[selected.id] ? selected.id : null;
  const tv: TrialView | null = selected ? (studyResults?.trials[selected.id]?.views[view] ?? null) : null;

  const lastVerdict = [...(data?.registry.events ?? [])].reverse().find((e) => e.event === 'RESULT');
  const holdoutUnlocked = (data?.registry.events ?? []).some((e) => e.event === 'HOLDOUT_UNLOCK');

  if (research.isLoading) return null;

  return (
    <div className={p.stack}>
      <div className={p.head}>
        <div>
          <h1 className={p.title}>Learning</h1>
          <p className={p.subtitle}>
            What the research has tested, what it found, and how far the project is from the evidence a real-money pilot needs. Every
            trial is registered before it runs; results here are read from the hash-chained research log.
          </p>
        </div>
      </div>

      <div className={p.tiles}>
        <div className={p.tile}>
          <Meter
            label="Trial budget"
            used={data?.budget.total ? data.budget.used / data.budget.total : 0}
            valueText={`${data?.budget.used ?? 0} of ${data?.budget.total ?? '—'}`}
            foot="Registered trials (each costs one)"
          />
        </div>
        <div className={p.tile}>
          <div className={p.inline} style={{ fontSize: 13, color: 'var(--ink-2)', fontWeight: 500 }}>
            <Link2 size={14} /> Research log
          </div>
          <div style={{ fontSize: 20, fontWeight: 600, marginTop: 6 }} className={p.inline}>
            {data?.registry.chain.ok ? <CheckCircle2 size={18} color="var(--good)" /> : <XCircle size={18} color="var(--critical)" />}
            {data?.registry.chain.ok ? 'Linked' : 'Broken'}
          </div>
          <div className={p.small + ' ' + p.muted} style={{ marginTop: 4 }}>{data?.registry.events.length ?? 0} events, prev-hash links checked</div>
        </div>
        <div className={p.tile}>
          <div style={{ fontSize: 13, color: 'var(--ink-2)', fontWeight: 500 }}>Latest verdict</div>
          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 6 }}>{lastVerdict ? String(lastVerdict.payload.verdict).replaceAll('_', ' ').toLowerCase() : '—'}</div>
          <div className={p.small + ' ' + p.muted} style={{ marginTop: 4 }}>{lastVerdict ? `${lastVerdict.payload.trial_id} · ${day(lastVerdict.at, tz)}` : ''}</div>
        </div>
        <div className={p.tile}>
          <div style={{ fontSize: 13, color: 'var(--ink-2)', fontWeight: 500 }}>Gate G2</div>
          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 6 }}>{data?.promotion?.gates?.G2?.status?.replaceAll('_', ' ').toLowerCase() ?? '—'}</div>
          <div className={p.small + ' ' + p.muted} style={{ marginTop: 4 }}>Research evidence before paper</div>
        </div>
        <div className={p.tile}>
          <div className={p.inline} style={{ fontSize: 13, color: 'var(--ink-2)', fontWeight: 500 }}>
            <Lock size={14} /> Final test data
          </div>
          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 6 }}>{holdoutUnlocked ? 'Unlocked' : 'Locked'}</div>
          <div className={p.small + ' ' + p.muted} style={{ marginTop: 4 }}>2026-08 → 2026-11 holdout</div>
        </div>
        <div className={p.tile}>
          <div style={{ fontSize: 13, color: 'var(--ink-2)', fontWeight: 500 }}>Live trading</div>
          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 6 }}>{data?.promotion?.owner_enablement?.enabled ? 'Enabled' : 'Disabled'}</div>
          <div className={p.small + ' ' + p.muted} style={{ marginTop: 4 }}>Needs G0–G4 and owner sign-off</div>
        </div>
      </div>

      <Card title="Release gates" sub="research/promotion_manifest.json. Each gate must pass, in order, before real money.">
        <div className={p.gates}>
          {['G0', 'G1', 'G2', 'G3', 'G4'].map((g) => {
            const gate = data?.promotion?.gates?.[g];
            const st = gate?.status ?? 'NOT_RUN';
            const icon =
              st === 'PASSED' ? <CheckCircle2 size={16} color="var(--good)" /> : st === 'FAILED' ? <XCircle size={16} color="var(--critical)" /> : <CircleDashed size={16} color="var(--ink-3)" />;
            return (
              <div key={g} className={p.gate}>
                <div className={p.gateName}>
                  <span>
                    {g} · {GATE_NAMES[g]}
                  </span>
                  {icon}
                </div>
                <div className={p.small} style={{ marginTop: 6, fontWeight: 600 }}>{st.replaceAll('_', ' ').toLowerCase()}</div>
                <div className={p.gateNote}>{gate?.notes ?? ''}</div>
              </div>
            );
          })}
        </div>
      </Card>

      {studies.length > 0 && (
        <Card title="Studies" sub="Each study tests its registered trials on one track. Pick one to inspect.">
          <div className={h.studies}>
            {studies.map((st) => (
              <button
                key={st.id}
                className={`${h.study} ${st.id === selected?.study ? h.studyOn : ''}`}
                onClick={() => st.trials[0] && setParams({ trial: st.trials[0] })}
              >
                <span className={h.studyName}>{st.id}</span>
                <span className={h.studyMeta}>
                  {[st.kind === 'model' ? 'Model filters (M3)' : 'Rule baseline (M2)', (st.track ?? trackOf(st.id)).toLowerCase(), `${st.trials.length} trials`].filter(Boolean).join(' · ')}
                </span>
                <span className={p.inline} style={{ marginTop: 6 }}>
                  {st.g2 === 'PASSED' ? <CheckCircle2 size={14} color="var(--good)" /> : <XCircle size={14} color="var(--critical)" />}
                  <span className={p.small}>G2 {st.g2?.replaceAll('_', ' ').toLowerCase() ?? '—'}</span>
                  <span className={p.small + ' ' + p.muted}>· {day(st.modifiedAt, tz)}</span>
                </span>
              </button>
            ))}
          </div>
        </Card>
      )}

      <Card title="Trials" sub="Click a trial to see its evidence." flush>
        {trials.length === 0 ? (
          <Empty title="No trials registered" />
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className={ps.table}>
              <thead>
                <tr>
                  <th>Trial</th>
                  <th>Study</th>
                  <th>Strategy</th>
                  <th>Parameters</th>
                  <th>Verdict</th>
                  <th className={ps.r}>Trades</th>
                  <th className={ps.r}>Net (base)</th>
                  <th className={ps.r}>Net (stressed)</th>
                </tr>
              </thead>
              <tbody>
                {trials.map((t) => {
                  const s = (t.result?.payload.summary ?? {}) as Record<string, string>;
                  const active = t.id === selectedId;
                  return (
                    <tr key={t.id} onClick={() => setParams({ trial: t.id })} style={{ cursor: 'pointer', background: active ? 'var(--accent-wash)' : undefined }}>
                      <td style={{ fontWeight: 600 }}>{t.id}</td>
                      <td>{t.study}</td>
                      <td>{t.family.replace('CANDIDATE_', 'Candidate ').replace('+LOGIT_FILTER', ' + filter')}</td>
                      <td>{t.params}</td>
                      <td>{t.result ? <Chip strong style={String(t.result.payload.verdict).includes('NEGATIVE') ? { color: 'var(--loss)' } : undefined}>{String(t.result.payload.verdict).replaceAll('_', ' ').toLowerCase()}</Chip> : <Chip>registered, not run</Chip>}</td>
                      <td className={ps.r}>{s.trades ?? '—'}</td>
                      <td className={ps.r}>{s.net_pnl_usd ? <Delta value={s.net_pnl_usd} precise /> : '—'}</td>
                      <td className={ps.r}>{s.stressed_net_pnl_usd ? <Delta value={s.stressed_net_pnl_usd} precise /> : '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {studyResults && selected && (
        <Card title="Every trial, every window" sub={`${studyResults.study_id}${studyResults.title ? ` · ${studyResults.title}` : ''}`}>
          <TrialHeatmap study={studyResults} selected={selected.id} onSelect={(id) => setParams({ trial: id })} />
        </Card>
      )}

      {selected && (
        <Card
          title={[selected.id, selected.family.replace('CANDIDATE_', 'Candidate ').replace('+LOGIT_FILTER', ' + logistic filter'), selected.params !== '—' ? selected.params : ''].filter(Boolean).join(' · ')}
          sub={selected.hypothesis.length > 240 ? selected.hypothesis.slice(0, 240) + '…' : selected.hypothesis}
          action={
            tv && (
              <Segmented
                label="Cost view"
                value={view}
                onChange={setView}
                options={[
                  { value: 'base', label: 'Base costs' },
                  { value: 'stressed', label: 'Stressed costs' },
                ]}
              />
            )
          }
        >
          {modelStudy && modelTrial ? (
            <ModelTrialView model={modelStudy} id={modelTrial} />
          ) : !selected.result && !hasOutput ? (
            <Empty title="Registered, not run yet" body={`Universe ${selected.universe.map((u) => u.split(':').pop()).join(', ')}. Results appear here when the study writes them.`} />
          ) : study.isLoading ? (
            <Empty title="Loading…" />
          ) : !tv ? (
            <Empty title="Study output not found" body={`runs/research/${selected.study}/results.json is not on this machine.`} />
          ) : (
            <div className={p.grid}>
              <section className={p.s7}>
                <div className={p.small + ' ' + p.muted} style={{ marginBottom: 10 }}>Net result per walk-forward window (USD)</div>
                <Columns
                  data={(tv.windows ?? []).map((w, i) => ({
                    label: new Intl.DateTimeFormat('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' }).format(new Date(w)).replace(' ', ' ’'),
                    value: Number(tv.window_nets_usd?.[i] ?? 0),
                    hint: `${tv.window_trades?.[i] ?? 0} trades`,
                  }))}
                  format={(n) => usd(n, { precise: true })}
                />
                <div className={p.small + ' ' + p.muted} style={{ margin: '22px 0 10px' }}>
                  Is the edge real? 95% intervals; G2 needs the lower end above zero.
                </div>
                {/* Per-trade and per-session nets differ by ~100x: small multiples, each on its own scale. */}
                {tv.per_trade_ci && (
                  <IntervalPlot
                    rows={[{ label: 'Per trade', lower: Number(tv.per_trade_ci.lower), estimate: Number(tv.per_trade_ci.estimate), upper: Number(tv.per_trade_ci.upper) }]}
                    format={(n) => usd(n, { precise: true })}
                  />
                )}
                {tv.mean_session_ci && (
                  <div style={{ marginTop: 10 }}>
                    <IntervalPlot
                      rows={[{ label: 'Per session', lower: Number(tv.mean_session_ci.lower), estimate: Number(tv.mean_session_ci.estimate), upper: Number(tv.mean_session_ci.upper) }]}
                      format={(n) => `${n < 0 ? '−' : ''}$${Math.abs(n).toFixed(4)}`}
                    />
                  </div>
                )}
              </section>
              <section className={p.s5}>
                <KV
                  items={[
                    ['Net P&L', <Delta key="n" value={tv.net_pnl_usd} precise />],
                    ['Gross P&L', <Delta key="g" value={tv.gross_pnl_usd} precise />],
                    ['Fees', usd(tv.fees_usd, { precise: true })],
                    ['Trades', String(tv.trades)],
                    ['Win rate', num(tv.win_rate) !== null ? pct(num(tv.win_rate)!, 0) : '—'],
                    ['Profit factor', num(tv.profit_factor) !== null ? Number(tv.profit_factor).toFixed(2) : String(tv.profit_factor ?? '—')],
                    ['Max drawdown', usd(tv.max_drawdown_usd, { precise: true })],
                    ['Sessions', `${tv.sessions}${tv.active_sessions !== undefined ? ` (${tv.active_sessions} active)` : ''}`],
                  ]}
                />
                {tv.exit_reasons && (
                  <>
                    <div className={p.small + ' ' + p.muted} style={{ margin: '20px 0 8px' }}>How trades ended</div>
                    <HBars data={Object.entries(tv.exit_reasons).map(([k, v]) => ({ label: k.toLowerCase().replace('_', ' '), value: v }))} />
                  </>
                )}
                {tv.candidates?.by_reason && (
                  <>
                    <div className={p.small + ' ' + p.muted} style={{ margin: '20px 0 8px' }}>
                      Candidates not taken ({tv.candidates.total.toLocaleString('en-US')} proposed)
                    </div>
                    <HBars
                      color="var(--ink-3)"
                      data={Object.entries(tv.candidates.by_reason)
                        .sort((a, b) => b[1] - a[1])
                        .map(([k, v]) => ({ label: k.toLowerCase().replaceAll('_', ' '), value: v }))}
                      format={(n) => n.toLocaleString('en-US')}
                    />
                  </>
                )}
              </section>
            </div>
          )}
        </Card>
      )}

      {modelStudy && !studyResults && (
        <G2Checks g2={modelStudy.g2} studyId={modelStudy.study_id} />
      )}

      {studyResults && (
        <div className={p.grid}>
          <section className={p.s6}>
            <G2Checks g2={studyResults.g2} studyId={studyResults.study_id} />
          </section>
          <section className={p.s6}>
            <Card title="Walk-forward selection" sub="On each fold the best validation trial is chosen, or cash if none earns its keep.">
              <div className={p.list}>
                {(studyResults.walk_forward?.folds ?? []).map((f) => (
                  <div key={f.fold} className={p.row}>
                    <div className={p.rowMain}>
                      <div className={p.rowTitle}>Fold {f.fold}</div>
                      <div className={p.rowSub}>
                        test {f.test[0].slice(0, 7)} → {f.test[1].slice(0, 7)}
                      </div>
                    </div>
                    <Chip strong>{f.selected === 'CASH' ? 'Stay in cash' : f.selected}</Chip>
                  </div>
                ))}
              </div>
            </Card>
          </section>
        </div>
      )}

      <div className={p.grid}>
        <section className={p.s6}>
          <Card title="Research log" sub="Newest first. Verify the full hash chain from a terminal:">
            <CopyCommand command="uv run trader research status" />
            <div className={`${p.list} ${p.scroll}`} style={{ marginTop: 12 }}>
              {[...(data?.registry.events ?? [])].reverse().map((e) => (
                <div key={e.seq} className={p.row}>
                  <Chip>{e.event.replace('_', ' ').toLowerCase()}</Chip>
                  <div className={p.rowMain}>
                    <div className={p.rowSub} style={{ whiteSpace: 'normal' }}>
                      {e.event === 'REGISTER'
                        ? `${(e.payload.trial as Record<string, unknown>)?.trial_id} registered for ${(e.payload.trial as Record<string, unknown>)?.study_id}`
                        : e.event === 'RESULT'
                          ? `${e.payload.trial_id}: ${String(e.payload.verdict).toLowerCase().replaceAll('_', ' ')}`
                          : e.event === 'BUDGET_DECISION'
                            ? `Budget set to ${e.payload.new_budget} by ${e.payload.by}`
                            : String(e.payload.text ?? '').slice(0, 160)}
                    </div>
                  </div>
                  <div className={p.rowRightSub}>#{e.seq} · {day(e.at, tz)}</div>
                </div>
              ))}
            </div>
          </Card>
        </section>
        <section className={p.s6}>
          <div className={p.stack}>
            <Card title="Data coverage" sub="research/data_manifest.json">
              <Coverage datasets={data?.datasets ?? []} />
            </Card>
            <Card title="Reports">
              {(data?.reports ?? []).length === 0 ? (
                <Empty title="No reports yet" />
              ) : (
                <div className={p.list}>
                  {(data?.reports ?? []).map((r) => (
                    <button key={r.name} className={`${p.row} ${p.rowButton}`} onClick={() => setReport(r.name)}>
                      <div className={p.rowMain}>
                        <div className={p.rowTitle}>{r.name}</div>
                        <div className={p.rowSub}>updated {day(r.modifiedAt, tz)}</div>
                      </div>
                      <span className={p.small + ' ' + p.muted}>Read</span>
                    </button>
                  ))}
                </div>
              )}
            </Card>
          </div>
        </section>
      </div>

      <Sheet open={report !== null} onClose={() => setReport(null)} title={report ?? ''} sub="research/reports">
        {reportText.data ? <div className={p.markdown} dangerouslySetInnerHTML={{ __html: renderMarkdown(reportText.data) }} /> : <p className={p.muted}>Loading…</p>}
      </Sheet>
    </div>
  );
}

/** Reports are the repo's own files, but raw HTML in them is still shown as text, never executed. */
const escape = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const md = new Marked({ gfm: true, renderer: { html: ({ text }: { text: string }) => escape(text) } });
const renderMarkdown = (text: string) => md.parse(text, { async: false }) as string;

function Coverage({ datasets }: { datasets: { name: string; status: string; start: string | null; end: string | null; blocker: string | null }[] }) {
  const ranges = datasets.filter((d) => d.start && d.end);
  if (!ranges.length) return <Empty title="No datasets" />;
  const min = Math.min(...ranges.map((d) => Date.parse(d.start!)));
  const max = Math.max(...ranges.map((d) => Date.parse(d.end!)));
  return (
    <div className={p.gantt}>
      {ranges.map((d) => {
        const a = (Date.parse(d.start!) - min) / (max - min);
        const b = (Date.parse(d.end!) - min) / (max - min);
        return (
          <div key={d.name} className={p.ganttRow} title={d.blocker ?? undefined}>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {d.name} {d.status !== 'ACQUIRED' && <Chip>{d.status.toLowerCase()}</Chip>}
            </span>
            <div className={p.ganttTrack}>
              <div
                className={p.ganttBar}
                style={{
                  left: `${a * 100}%`,
                  width: `${Math.max(1.5, (b - a) * 100)}%`,
                  background: d.status === 'ACQUIRED' ? 'var(--series-1)' : 'var(--ink-3)',
                  opacity: d.status === 'ACQUIRED' ? 1 : 0.5,
                }}
              />
            </div>
          </div>
        );
      })}
      <div className={p.small + ' ' + p.muted} style={{ display: 'flex', justifyContent: 'space-between', marginLeft: 'calc(min(260px, 40%) + 12px)' }}>
        <span>{new Date(min).getUTCFullYear()}</span>
        <span>{new Date(max).getUTCFullYear()}</span>
      </div>
    </div>
  );
}
