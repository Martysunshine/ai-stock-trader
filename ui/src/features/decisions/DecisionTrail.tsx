import { useMemo } from 'react';
import { decisionTrail, reasonsOf, symbolOf } from '../../../shared/derive';
import { useConsole } from '../../app/store';
import { CandleChart, type ChartLine, type ChartMarker } from '../../charts/CandleChart';
import { clock, dayTime, price, qty, shortHash, tzLabel, usd } from '../../design/format';
import { DISPOSITIONS, ORDER_STATES, PURPOSES, REASONS, reasonTitle } from '../../design/language';
import { Chip, KV, Sheet } from '../../design/primitives';
import { candlesAround } from '../markets/candles';
import p from '../pages.module.css';

function Step({ title, at, done, children }: { title: string; at?: string | null; done: boolean | null; children?: React.ReactNode }) {
  const tz = useConsole((s) => s.tz);
  return (
    <div className={p.step}>
      <span className={`${p.stepDot} ${done === true ? p.stepDotDone : done === false ? p.stepDotNo : ''}`} />
      <div className={p.stepTitle}>
        {title}
        {at && (
          <span className={p.stepTime}>
            {clock(at, tz, true)} {tzLabel(tz)}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

/** One candidate followed through every table it touched. */
export function DecisionTrailSheet({ candidateId, onClose }: { candidateId: string | null; onClose: () => void }) {
  const tables = useConsole((s) => s.tables);
  const tz = useConsole((s) => s.tz);
  const trail = useMemo(() => (candidateId ? decisionTrail(tables, candidateId) : null), [tables, candidateId]);
  const c = trail?.candidate ?? null;
  const d = trail?.decision ?? null;
  const sym = symbolOf(c?.instrument_id ?? '');

  const chart = useMemo(() => {
    if (!c) return null;
    const t = Date.parse(c.decision_time) / 1000;
    const candles = candlesAround(tables, c.instrument_id, t - 3 * 3600, t + 2 * 3600);
    if (candles.length < 2) return null;
    const markers: ChartMarker[] = [{ time: t - (t % 300) - 300, kind: d?.approved ? 'approved' : 'rejected', text: d?.approved ? 'Approved' : 'Rejected' }];
    for (const f of trail?.fills ?? []) {
      const ft = Date.parse(f.exec_time) / 1000;
      markers.push({ time: ft - (ft % 300), kind: f.side === 'BUY' ? 'buy' : 'sell', text: f.side === 'BUY' ? 'Buy' : 'Sell' });
    }
    const lines: ChartLine[] = [];
    if (c.payload.reference_price) lines.push({ price: Number(c.payload.reference_price), kind: 'reference', title: 'Ref' });
    if (c.payload.stop_price) lines.push({ price: Number(c.payload.stop_price), kind: 'stop', title: 'Stop' });
    if (c.payload.target_price) lines.push({ price: Number(c.payload.target_price), kind: 'target', title: 'Target' });
    return { candles, markers, lines };
  }, [tables, c, d, trail?.fills]);

  return (
    <Sheet
      open={Boolean(candidateId)}
      onClose={onClose}
      title={c ? `${sym} · ${d?.approved ? 'approved' : 'rejected'}` : 'Decision'}
      sub={c ? `${dayTime(c.decision_time, tz)} ${tzLabel(tz)} · ${c.strategy_id} v${c.payload.strategy_version ?? '?'}` : undefined}
    >
      {!trail || !c ? (
        <p className={p.muted}>This candidate is not in the data on screen.</p>
      ) : (
        <div className={p.stack} style={{ gap: 24 }}>
          {chart && (
            <div style={{ margin: '0 -8px' }}>
              <CandleChart candles={chart.candles} markers={chart.markers} lines={chart.lines} height={240} fitKey={c.candidate_id} />
            </div>
          )}
          <div className={p.timeline}>
            <Step title="Strategy proposed a long entry" at={c.decision_time} done>
              <KV
                items={[
                  ['Reference price', price(c.payload.reference_price)],
                  ['Stop', price(c.payload.stop_price)],
                  ['Target', price(c.payload.target_price)],
                  ['Max hold', `${c.payload.max_hold_minutes ?? '—'} min`],
                  ['Bar ended', c.payload.bar_end ? `${clock(c.payload.bar_end, tz)} ${tzLabel(tz)}` : '—'],
                  ['Feature snapshot', <code key="h">{shortHash(c.payload.snapshot_hash, 16)}</code>],
                ]}
              />
            </Step>

            <Step title={d ? (d.approved ? 'Risk engine approved' : 'Risk engine rejected') : 'Awaiting risk decision'} at={d?.decided_at} done={d ? d.approved : null}>
              {d && !d.approved && (
                <>
                  {reasonsOf(d).map((r) => (
                    <div key={r} className={p.why}>
                      <b>{reasonTitle(r)}.</b> {REASONS[r]?.why ?? r}
                    </div>
                  ))}
                  {d.payload.details && d.payload.details.length > 0 && (
                    <div className={p.why}>
                      <b>The bot's own words:</b>
                      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                        {d.payload.details.map((x, i) => (
                          <li key={i}>{x}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              )}
              {d && (
                <KV
                  items={[
                    ...(d.approved
                      ? ([
                          ['Size', `${qty(d.payload.quantity)} sh`],
                          ['Notional', usd(d.payload.notional_usd, { precise: true })],
                          ['Planned loss at stop', usd(d.payload.planned_loss_usd, { precise: true })],
                          ['Estimated costs', usd(d.payload.estimated_costs_usd, { precise: true })],
                          ['Expected net', usd(d.payload.expected_net_usd, { precise: true })],
                          ['Limit price', price(d.payload.entry_limit_price ?? null)],
                          ['Binding constraint', d.payload.binding_constraint || '—'],
                        ] as [string, React.ReactNode][])
                      : []),
                    ['Sizing equity', usd(d.payload.effective_sizing_equity_usd)],
                    ['Model', <span key="m">{d.payload.model_hash ?? d.model_hash} <Chip>{d.payload.evidence ?? '—'}</Chip></span>],
                    ['Policy', <code key="p">{shortHash(d.policy_hash, 16)}</code>],
                    ['Decision #', d.seq],
                  ]}
                />
              )}
            </Step>

            {d?.approved && (
              <>
                <Step title={trail.entry ? `Order: ${PURPOSES[trail.entry.purpose] ?? trail.entry.purpose}` : 'No order recorded'} at={trail.entry?.created_at} done={trail.entry ? true : null}>
                  {trail.entry && (
                    <KV
                      items={[
                        ['State', <Chip key="s" strong>{ORDER_STATES[trail.entry.state] ?? trail.entry.state}</Chip>],
                        ['Side · type', `${trail.entry.side} · ${trail.entry.order_type} · ${trail.entry.time_in_force}`],
                        ['Quantity', `${qty(trail.entry.quantity)} (filled ${qty(trail.entry.cum_filled_qty)})`],
                        ['Limit', price(trail.entry.limit_price)],
                        ['Reason', String(trail.entry.plan?.intent?.reason ?? '—')],
                        ['Valid until', `${clock(trail.entry.valid_until, tz, true)} ${tzLabel(tz)}`],
                        ['Client order ID', <code key="c">{trail.entry.client_order_id}</code>],
                        ['Outbox', trail.outbox.map((o) => `${o.status} (${o.attempts} attempt${o.attempts === 1 ? '' : 's'})`).join(', ') || '—'],
                      ]}
                    />
                  )}
                </Step>
                <Step title="Broker events" at={trail.brokerEvents[0]?.event_time} done={trail.brokerEvents.length ? true : null}>
                  {trail.brokerEvents.length === 0 ? (
                    <p className={p.small + ' ' + p.muted}>None yet.</p>
                  ) : (
                    <table className="table" style={{ width: '100%', fontSize: 12.5, borderCollapse: 'collapse' }}>
                      <tbody>
                        {trail.brokerEvents.map((e) => (
                          <tr key={e.id}>
                            <td style={{ padding: '4px 0', color: 'var(--ink-3)' }} className="num">{clock(e.event_time, tz, true)}</td>
                            <td style={{ padding: '4px 8px', fontWeight: 600 }}>{e.status}</td>
                            <td style={{ padding: '4px 8px' }} className="num">cum {qty(e.cum_filled_qty)}</td>
                            <td style={{ padding: '4px 0', color: 'var(--ink-2)' }}>{DISPOSITIONS[e.disposition] ?? e.disposition}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </Step>
                <Step title={`Fills (${trail.fills.length})`} at={trail.fills[0]?.exec_time} done={trail.fills.length ? true : null}>
                  {trail.fills.map((f) => (
                    <div key={f.id} className={p.why}>
                      <b>{f.side === 'BUY' ? 'Bought' : 'Sold'} {qty(f.quantity)} @ {price(f.price)}</b> · fee {usd(f.fee, { precise: true })} ({f.fee_status.toLowerCase()})
                      {f.realized_net !== null && <> · realised net {usd(f.realized_net, { sign: true, precise: true })}</>}
                    </div>
                  ))}
                </Step>
                {trail.exitPlan && (
                  <Step title={trail.exitPlan.active ? 'Protected by an exit plan' : 'Exit plan closed'} at={trail.exitPlan.created_at} done={!trail.exitPlan.active}>
                    <KV
                      items={[
                        ['Stop', price(trail.exitPlan.stop_price)],
                        ['Target', price(trail.exitPlan.target_price)],
                        ['Max hold until', `${clock(trail.exitPlan.max_hold_until, tz)} ${tzLabel(tz)}`],
                        ['Planned loss', usd(trail.exitPlan.planned_loss_usd, { precise: true })],
                      ]}
                    />
                  </Step>
                )}
                {trail.exits.map((x) => (
                  <Step key={x.intent_id} title={`${PURPOSES[x.purpose] ?? x.purpose}: ${ORDER_STATES[x.state] ?? x.state}`} at={x.created_at} done={x.state === 'FILLED'}>
                    <div className={p.why}>{String(x.plan?.intent?.reason ?? x.plan?.reason ?? 'Exit')}</div>
                  </Step>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </Sheet>
  );
}
