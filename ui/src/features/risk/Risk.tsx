import { useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, OctagonAlert, PauseCircle } from 'lucide-react';
import { D, lossWindowUsage, moneySummary, symbolOf } from '../../../shared/derive';
import { useNow } from '../../app/hooks';
import { usePolicyLimits } from '../../app/policy';
import { sessionAt } from '../../app/session';
import { useConsole, viewNow } from '../../app/store';
import { ago, clock, dayTime, pct, tzLabel, usd } from '../../design/format';
import { ALERTS, LOSS_WINDOWS, ORDER_STATES, PAUSE_CAUSES } from '../../design/language';
import { Card, Chip, CopyCommand, Empty, KV, Meter } from '../../design/primitives';
import { SourceNotice } from '../SourceNotice';
import p from '../pages.module.css';

const LIVE_STATES = ['PENDING_SUBMIT', 'SUBMITTING', 'UNKNOWN_OUTCOME', 'UNRESOLVED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED'];
const DB = '$env:TRADER_DATABASE_URL';

export default function Risk() {
  const tables = useConsole((s) => s.tables);
  const status = useConsole((s) => s.status);
  const tz = useConsole((s) => s.tz);
  const wall = useNow(1000);
  const now = viewNow(status, wall);
  const limits = usePolicyLimits();
  const m = useMemo(() => moneySummary(tables, limits), [tables, limits]);
  const windows = useMemo(() => lossWindowUsage(tables, limits), [tables, limits]);
  const active = tables.pause_state.filter((x) => x.active);
  const isReplay = status?.kind === 'replay';
  const [hoverRecon, setHoverRecon] = useState<string | null>(null);

  const equity = Number(m.equity ?? m.cash) || Number(m.initialCapital);
  const gross = (Number(m.invested) + Number(m.reserved)) / equity;
  const bySymbol = useMemo(() => {
    const map = new Map<string, number>();
    for (const pos of tables.positions) map.set(pos.instrument_id, (map.get(pos.instrument_id) ?? 0) + Number(pos.cost_basis));
    for (const r of tables.risk_reservations.filter((x) => x.state === 'ACTIVE')) map.set(r.instrument_id, (map.get(r.instrument_id) ?? 0) + Number(r.notional_usd));
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }, [tables.positions, tables.risk_reservations]);
  const topSymbol = bySymbol[0];
  const session = sessionAt(now);
  const entriesThisSession = session.open
    ? tables.order_intents.filter((i) => i.purpose === 'ENTRY' && Date.parse(i.created_at) >= session.open!).length
    : 0;
  const liveIntents = tables.order_intents.filter((i) => LIVE_STATES.includes(i.state));
  const recons = tables.reconciliation_runs.slice(-120);
  const lastRecon = tables.reconciliation_runs.at(-1);
  const failedRecons = tables.reconciliation_runs.filter((r) => r.status === 'FAILED').length;
  const hovered = recons.find((r) => r.run_id === hoverRecon) ?? lastRecon;

  return (
    <div className={p.stack}>
      <SourceNotice />
      <div className={p.head}>
        <div>
          <h1 className={p.title}>Risk &amp; health</h1>
          <p className={p.subtitle}>The limits that stop the bot, how close it is to each, and whether it agrees with the broker.</p>
        </div>
      </div>

      {active.map((x) => {
        const loss = x.category === 'LOSS_BREACH';
        return (
          <div key={x.flag_id} className={`${p.banner} ${loss ? p.bannerCritical : ''}`} style={{ marginBottom: 0 }}>
            {loss ? <OctagonAlert className={p.bannerIcon} size={22} color="var(--critical)" /> : <PauseCircle className={p.bannerIcon} size={22} color="var(--warning-ink)" />}
            <div className={p.bannerBody}>
              <div className={p.bannerTitle}>New entries paused: {PAUSE_CAUSES[x.cause] ?? x.cause}</div>
              <div className={p.small}>
                {x.category.replace('_', ' ').toLowerCase()} · {x.scope.toLowerCase()} {x.scope_id} · since {dayTime(x.raised_at, tz)} {tzLabel(tz)}
                {x.details ? ` · ${x.details}` : ''}. Exits keep working.
              </div>
              {!isReplay && (
                <>
                  <div className={p.small + ' ' + p.muted}>Review the account first, then resume from a terminal (the console never changes bot state):</div>
                  <CopyCommand command={`uv run trader status --db ${DB}`} />
                  {loss ? (
                    <CopyCommand command={`uv run trader resume --db ${DB} --config configs/paper.yaml --ack-loss-breach --note "what I reviewed"`} />
                  ) : x.category === 'OWNER' ? (
                    <CopyCommand command={`uv run trader resume --db ${DB} --config configs/paper.yaml`} />
                  ) : (
                    <div className={p.small + ' ' + p.muted}>Health pauses clear themselves after a later passed reconciliation.</div>
                  )}
                </>
              )}
            </div>
          </div>
        );
      })}

      <Card title="Loss limits" sub="Daily and weekly windows in both New York and UTC time, the drawdown limit and the capital floor.">
        <div className={p.meters}>
          {windows.length === 0 && <Empty title="No loss windows yet" />}
          {windows.map((w) => (
            <Meter
              key={w.kind}
              label={LOSS_WINDOWS[w.kind] ?? w.kind}
              used={w.usedFraction}
              valueText={pct(w.usedFraction, 0)}
              foot={`${usd(D(w.lossUsd).gt(0) ? w.lossUsd : '0', { precise: true })} lost of ${usd(w.limitUsd)} · window ${w.windowKey}${w.breachedAt ? ' · breached' : ''}`}
            />
          ))}
          <Meter
            label="Drawdown from high-water"
            used={m.drawdownFraction ? Number(m.drawdownFraction) / Number(limits.maxDrawdownFraction) : 0}
            valueText={m.drawdownFraction ? pct(m.drawdownFraction, 2) : '—'}
            foot={`Pause at ${pct(limits.maxDrawdownFraction, 0)}`}
          />
          <Meter
            label="Capital floor"
            used={m.floorDistance !== null ? 1 - Math.max(0, Math.min(1, Number(m.floorDistance) / (Number(m.initialCapital) - Number(limits.capitalFloor)))) : 0}
            valueText={m.adjustedEquity ? usd(m.adjustedEquity, { precise: true }) : '—'}
            foot={`Pause at USD ${Number(limits.capitalFloor).toFixed(0)} (cash-flow adjusted)`}
          />
        </div>
      </Card>

      <Card title="Exposure" sub="What is invested or reserved for working orders, against the policy caps.">
        <div className={p.meters}>
          <Meter label="Gross exposure" used={gross / Number(limits.maxGrossExposureFraction)} valueText={pct(gross, 1)} foot={`Cap ${pct(limits.maxGrossExposureFraction, 0)} of equity`} />
          <Meter
            label={topSymbol ? `Largest symbol (${symbolOf(topSymbol[0])})` : 'Largest symbol'}
            used={topSymbol ? topSymbol[1] / equity / Number(limits.maxSymbolExposureFraction) : 0}
            valueText={topSymbol ? pct(topSymbol[1] / equity, 1) : '0%'}
            foot={`Cap ${pct(limits.maxSymbolExposureFraction, 0)} per symbol`}
          />
          <Meter label="Open positions" used={m.openPositions / limits.maxOpenPositions} valueText={`${m.openPositions} of ${limits.maxOpenPositions}`} foot="Live pilot allows 1" />
          <Meter label="Entries this session" used={entriesThisSession / limits.maxNewEntriesPerSession} valueText={`${entriesThisSession} of ${limits.maxNewEntriesPerSession}`} foot={session.open ? `Since ${clock(session.open, tz)} ${tzLabel(tz)}` : 'Market closed'} />
        </div>
      </Card>

      <div className={p.grid}>
        <section className={p.s7}>
          <Card
            title="Reconciliation"
            sub={
              lastRecon
                ? `Every 60 s while the worker runs · last ${ago(lastRecon.finished_at, now)} · ${failedRecons} failed of ${tables.reconciliation_runs.length}`
                : 'The worker compares its ledger with the broker every 60 seconds.'
            }
          >
            {recons.length === 0 ? (
              <Empty title="No reconciliations yet" />
            ) : (
              <>
                <div className={p.reconStrip} onMouseLeave={() => setHoverRecon(null)}>
                  {recons.map((r) => (
                    <span
                      key={r.run_id}
                      className={p.reconDot}
                      onMouseEnter={() => setHoverRecon(r.run_id)}
                      style={{ background: r.status === 'PASSED' ? 'var(--good)' : 'var(--critical)', opacity: r.run_id === hovered?.run_id ? 1 : 0.55 }}
                      title={`${r.status} · ${clock(r.finished_at, tz, true)}`}
                    />
                  ))}
                </div>
                {hovered && (
                  <div style={{ marginTop: 14 }}>
                    <KV
                      items={[
                        ['Result', <span key="r" className={p.inline}>{hovered.status === 'PASSED' ? <CheckCircle2 size={15} color="var(--good)" /> : <AlertTriangle size={15} color="var(--critical)" />}{hovered.status === 'PASSED' ? 'Passed' : 'Failed'}</span>],
                        ['Finished', `${dayTime(hovered.finished_at, tz)} ${tzLabel(tz)}`],
                        ['Problems', hovered.summary.problems?.length ? hovered.summary.problems.join('; ') : 'none'],
                        ['Notes', hovered.summary.notes?.length ? hovered.summary.notes.join('; ') : 'none'],
                      ]}
                    />
                  </div>
                )}
              </>
            )}
          </Card>
        </section>
        <section className={p.s5}>
          <Card title="Orders in flight" sub={`${liveIntents.length} live · ${tables.submission_outbox.filter((o) => o.status === 'PENDING' || o.status === 'CLAIMED').length} waiting in the outbox`}>
            {liveIntents.length === 0 ? (
              <Empty title="No working orders" />
            ) : (
              <div className={p.list}>
                {liveIntents.map((i) => (
                  <div key={i.intent_id} className={p.row}>
                    <div className={p.rowMain}>
                      <div className={p.rowTitle}>{symbolOf(i.instrument_id)} {i.side.toLowerCase()} {i.purpose === 'ENTRY' ? 'entry' : 'exit'}</div>
                      <div className={p.rowSub}>updated {ago(i.updated_at, now)}</div>
                    </div>
                    <Chip strong style={['UNKNOWN_OUTCOME', 'UNRESOLVED'].includes(i.state) ? { color: 'var(--critical)' } : undefined}>{ORDER_STATES[i.state] ?? i.state}</Chip>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </section>
        <section className={p.s6}>
          <Card title="Alerts">
            {tables.alerts.length === 0 ? (
              <Empty art="/assets/empty-clear.png" title="All clear" body="No alerts have been raised." />
            ) : (
              <div className={p.list}>
                {[...tables.alerts].reverse().map((a) => (
                  <div key={a.id} className={p.row}>
                    <AlertTriangle size={18} color="var(--critical)" />
                    <div className={p.rowMain}>
                      <div className={p.rowTitle}>{ALERTS[a.code] ?? a.code}</div>
                      <div className={p.rowSub}>{a.message}</div>
                    </div>
                    <div className={p.rowRightSub}>{dayTime(a.raised_at, tz)}</div>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </section>
        <section className={p.s6}>
          <Card title="Pause history">
            {tables.pause_state.length === 0 ? (
              <Empty title="Never paused" />
            ) : (
              <div className={p.list}>
                {[...tables.pause_state].sort((a, b) => b.raised_at.localeCompare(a.raised_at)).map((x) => {
                  const review = tables.owner_reviews.find((r) => r.flag_id === x.flag_id);
                  return (
                    <div key={x.flag_id} className={p.row}>
                      <div className={p.rowMain}>
                        <div className={p.rowTitle}>
                          {PAUSE_CAUSES[x.cause] ?? x.cause}
                          {x.active ? <Chip strong style={{ color: 'var(--warning-ink)' }}>active</Chip> : <Chip>cleared</Chip>}
                        </div>
                        <div className={p.rowSub}>
                          raised {dayTime(x.raised_at, tz)}
                          {x.cleared_at ? ` · cleared ${dayTime(x.cleared_at, tz)} by ${x.cleared_by}` : ''}
                          {review ? ` · review: “${review.note}”` : ''}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </section>
      </div>
    </div>
  );
}
