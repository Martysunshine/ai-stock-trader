import { VerdictChip } from './VerdictChip';
import { useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { useVirtualizer } from '@tanstack/react-virtual';
import { ChevronRight } from 'lucide-react';
import { funnel, rejectionCounts, reasonsOf, symbolOf } from '../../../shared/derive';
import { useConsole } from '../../app/store';
import { HBars } from '../../charts/svg';
import { ChartOrTable } from '../../design/ChartOrTable';
import { clock, day, price, tzLabel, usd } from '../../design/format';
import { REASONS, reasonTitle } from '../../design/language';
import { Card, Chip, Empty, Monogram, Odometer, Segmented } from '../../design/primitives';
import { SourceNotice } from '../SourceNotice';
import { DecisionTrailSheet } from './DecisionTrail';
import p from '../pages.module.css';

type Outcome = 'all' | 'approved' | 'rejected';

export default function Decisions() {
  const tables = useConsole((s) => s.tables);
  const tz = useConsole((s) => s.tz);
  const [params, setParams] = useSearchParams();
  const selected = params.get('c');
  const [outcome, setOutcome] = useState<Outcome>('all');
  const [symbol, setSymbol] = useState('all');
  const [reason, setReason] = useState('all');

  const f = useMemo(() => funnel(tables), [tables]);
  const reasons = useMemo(() => rejectionCounts(tables.risk_decisions), [tables.risk_decisions]);
  const candidates = useMemo(() => new Map(tables.candidates.map((c) => [c.candidate_id, c])), [tables.candidates]);
  const symbols = useMemo(() => [...new Set(tables.candidates.map((c) => symbolOf(c.instrument_id)))].sort(), [tables.candidates]);

  const rows = useMemo(() => {
    return [...tables.risk_decisions]
      .reverse()
      .filter((d) => outcome === 'all' || (outcome === 'approved' ? d.approved : !d.approved))
      .filter((d) => symbol === 'all' || symbolOf(candidates.get(d.candidate_id)?.instrument_id ?? '') === symbol)
      .filter((d) => reason === 'all' || reasonsOf(d).includes(reason));
  }, [tables.risk_decisions, outcome, symbol, reason, candidates]);

  const steps: [string, number, string][] = [
    ['Candidates', f.candidates, 'Setups the strategy proposed'],
    ['Approved', f.approved, 'Passed every risk check'],
    ['Sent', f.submitted, 'Orders handed to the broker'],
    ['Filled', f.filled, 'Entries that bought shares'],
    ['Exited', f.exited, 'Round trips completed'],
  ];

  const scroller = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count: rows.length, getScrollElement: () => scroller.current, estimateSize: () => 66, overscan: 8 });

  return (
    <div className={p.stack}>
      <SourceNotice />
      <div className={p.head}>
        <div>
          <h1 className={p.title}>Decisions</h1>
          <p className={p.subtitle}>
            Every setup the strategy proposed, what the risk engine decided and why, and what happened after. Rejections are the bot
            working as designed: it only trades when every check passes.
          </p>
        </div>
      </div>

      <Card>
        <div className={p.funnel}>
          {steps.map(([label, n, hint], i) => (
            <div key={label} className={p.funnelStep} title={hint}>
              <div className={p.inline} style={{ fontSize: 13, color: 'var(--ink-2)', fontWeight: 500 }}>
                {label}
                {i < steps.length - 1 && <ChevronRight size={14} color="var(--ink-3)" />}
              </div>
              <div className={`${p.funnelValue} num`}>
                <Odometer text={String(n)} />
              </div>
              <div className={p.funnelConv}>
                {i === 0 ? hint : steps[i - 1][1] > 0 ? `${Math.round((n / steps[i - 1][1]) * 100)}% of previous` : '—'}
              </div>
            </div>
          ))}
        </div>
      </Card>

      <div className={p.grid}>
        <section className={p.s5}>
          <Card title="Why signals were not taken" sub="A signal that is not taken can carry several reason codes; each is counted. Most are routine: the same signal repeats every bar.">
            {reasons.length ? (
              <ChartOrTable
                label="rejection reasons"
                chart={
                  <HBars
                    data={reasons.map(([code, n]) => ({ label: reasonTitle(code), value: n, hint: REASONS[code]?.why }))}
                    format={(n) => String(n)}
                  />
                }
                columns={[
                  { key: 'reason', label: 'Reason' },
                  { key: 'code', label: 'Code' },
                  { key: 'n', label: 'Count', align: 'right' },
                ]}
                rows={reasons.map(([code, n]) => ({ reason: reasonTitle(code), code: <code>{code}</code>, n }))}
              />
            ) : (
              <Empty title="Nothing skipped" body="No signal has been skipped or blocked in the data on screen." />
            )}
            {reasons.length > 0 && (
              <div className={p.stack} style={{ marginTop: 16, gap: 10 }}>
                {reasons.slice(0, 3).map(([code]) => (
                  <div key={code} className={p.why}>
                    <b>{reasonTitle(code)}.</b> {REASONS[code]?.why ?? code}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </section>

        <section className={p.s7}>
          <Card
            title="Decision stream"
            sub={`${rows.length} shown · newest first`}
            action={
              <div className={p.toolbar}>
                <Segmented
                  label="Outcome"
                  value={outcome}
                  onChange={setOutcome}
                  options={[
                    { value: 'all', label: 'All' },
                    { value: 'approved', label: 'Approved' },
                    { value: 'rejected', label: 'Rejected' },
                  ]}
                />
                <select className={p.select} value={symbol} onChange={(e) => setSymbol(e.target.value)} aria-label="Symbol">
                  <option value="all">All symbols</option>
                  {symbols.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
                <select className={p.select} value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Reason">
                  <option value="all">All reasons</option>
                  {reasons.map(([code]) => (
                    <option key={code} value={code}>
                      {reasonTitle(code)}
                    </option>
                  ))}
                </select>
              </div>
            }
          >
            {rows.length === 0 ? (
              <Empty title="No decisions match" body="Decisions appear about 5 seconds after each 5-minute bar inside the entry window." />
            ) : (
              <div ref={scroller} style={{ height: 520, overflowY: 'auto', margin: '0 -8px', padding: '0 8px' }}>
                <div style={{ height: virtual.getTotalSize(), position: 'relative' }}>
                  {virtual.getVirtualItems().map((v) => {
                    const d = rows[v.index];
                    const c = candidates.get(d.candidate_id);
                    const sym = symbolOf(c?.instrument_id ?? '?');
                    const rs = reasonsOf(d);
                    return (
                      <button
                        key={d.decision_id}
                        className={`${p.row} ${p.rowButton}`}
                        style={{ position: 'absolute', top: 0, left: 8, right: 8, width: 'auto', transform: `translateY(${v.start}px)`, height: v.size, margin: 0 }}
                        onClick={() => setParams({ c: d.candidate_id })}
                      >
                        <Monogram symbol={sym} />
                        <div className={p.rowMain}>
                          <div className={p.rowTitle}>
                            {sym}
                            {d.approved ? (
                              <Chip strong style={{ color: 'var(--accent-ink)', background: 'var(--accent-wash)' }}>
                                Taken
                              </Chip>
                            ) : (
                              <VerdictChip decision={d} />
                            )}
                            {rs.slice(0, 3).map((r) => (
                              <Chip key={r} title={REASONS[r]?.why}>
                                {reasonTitle(r)}
                              </Chip>
                            ))}
                          </div>
                          <div className={p.rowSub}>
                            {c?.payload.reference_price && `Ref ${price(c.payload.reference_price)} · stop ${price(c.payload.stop_price)} · target ${price(c.payload.target_price)}`}
                            {d.payload.details?.[0] ? ` · ${d.payload.details[0]}` : ''}
                            {d.approved ? ` · ${usd(d.payload.notional_usd ?? '0')} notional` : ''}
                          </div>
                        </div>
                        <div className={p.rowRight}>
                          <div className={p.rowRightSub}>
                            {clock(d.decided_at, tz, true)} {tzLabel(tz)}
                          </div>
                          <div className={p.rowRightSub}>{day(d.decided_at, tz)}</div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </Card>
        </section>
      </div>

      <DecisionTrailSheet candidateId={selected} onClose={() => setParams({})} />
    </div>
  );
}
