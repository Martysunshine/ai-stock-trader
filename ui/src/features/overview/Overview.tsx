import { lazy, memo, Suspense, useCallback, useMemo, useState } from 'react';
import { SessionRibbon } from './SessionRibbon';
import { useNavigate } from 'react-router';
import { AnimatePresence, motion } from 'motion/react';
import Decimal from 'decimal.js';
import {
  D,
  latestBy,
  lossWindowUsage,
  moneySummary,
  positionViews,
  symbolOf,
} from '../../../shared/derive';

import { Boundary } from '../../app/Boundary';
import { botStatus } from '../../app/botStatus';
import { useIsDark, useNow } from '../../app/hooks';
import { usePolicyLimits } from '../../app/policy';
import { MissionPanel } from '../mission/MissionPanel';
import { PositionsCard } from './PositionsCard';
import { decisionVerdict } from '../../../shared/verdict';
import { VerdictChip } from '../decisions/VerdictChip';
import { useSleeves } from '../mission/useSleeves';
import { SLEEVE_HEX, SLEEVES } from '../../../shared/mission';
import { sessionAt } from '../../app/session';
import { useConsole, viewNow } from '../../app/store';
import { EquityChart } from '../../charts/EquityChart';
import { ago, clock, dayTime, pct, qty, tzLabel, usd } from '../../design/format';
import { LOSS_WINDOWS } from '../../design/language';
import { Card, Chip, Delta, Empty, Meter, Monogram, Odometer, Segmented, SPRING, Stat } from '../../design/primitives';
import { SourceNotice } from '../SourceNotice';
import p from '../pages.module.css';

// Memoised: the page re-renders every second for the clock; the 3D scene only on real changes.
const Core = memo(lazy(() => import('../../three/Core')));

type Range = 'session' | 'week' | 'month' | 'all';
const RANGE_MS: Record<Range, number> = { session: 8 * 3600_000, week: 7 * 86400_000, month: 31 * 86400_000, all: Infinity };

export function Overview() {
  const tables = useConsole((s) => s.tables);
  const status = useConsole((s) => s.status);
  const pulse = useConsole((s) => s.pulse);
  const tz = useConsole((s) => s.tz);
  const wall = useNow(1000);
  const now = viewNow(status, wall);
  const session = useMemo(() => sessionAt(now), [now]);
  const limits = usePolicyLimits();
  const dark = useIsDark();
  const [range, setRange] = useState<Range>('all');
  const [coreReady, setCoreReady] = useState(false);
  const onCoreReady = useCallback(() => setCoreReady(true), []);
  const navigate = useNavigate();

  const m = useMemo(() => moneySummary(tables, limits), [tables, limits]);
  const windows = useMemo(() => lossWindowUsage(tables, limits), [tables, limits]);
  const positions = useMemo(() => positionViews(tables), [tables]);
  const bot = botStatus(status, tables.pause_state, now, session);
  const sleeves = useSleeves();
  const rings = useMemo(
    () => sleeves.views.map((v) => ({ color: SLEEVE_HEX[v.id][dark ? 'dark' : 'light'], live: v.live, warn: v.equity <= v.start * 0.9 })),
    [sleeves.views, dark],
  );

  const equity = m.equity ?? m.cash;
  // No rows yet means unknown, not zero: tiles show a dash rather than an invented $0.00.
  const hasMoney = tables.ledger_entries.length > 0 || tables.equity_snapshots.length > 0;
  const money = (v: string | null) => (hasMoney && v !== null ? usd(v, { precise: true }) : '—');
  const today = windows.find((w) => w.kind === 'NY_SESSION') ?? windows.find((w) => w.kind === 'UTC_DAY');
  const todayDelta = today ? new Decimal(today.lossUsd).neg().toFixed() : null;

  const points = useMemo(() => {
    const snaps = tables.equity_snapshots.filter((e) => e.scope === 'PORTFOLIO');
    const rows = snaps.length ? snaps : tables.equity_snapshots;
    const from = now - RANGE_MS[range];
    const pts = rows
      .filter((e) => Date.parse(e.ts) >= from)
      .map((e) => ({ time: Date.parse(e.ts) / 1000, value: Number(e.equity) }));
    // Snapshots are written only when equity changes: the last value holds until now.
    const last = latestBy(rows, (r) => r.ts);
    if (last && Date.parse(last.ts) < now) pts.push({ time: now / 1000, value: Number(last.equity) });
    return pts;
  }, [tables.equity_snapshots, range, now]);

  const recent = useMemo(() => [...tables.risk_decisions].reverse().slice(0, 7), [tables.risk_decisions]);
  const candidateById = useMemo(() => new Map(tables.candidates.map((c) => [c.candidate_id, c])), [tables.candidates]);

  return (
    <div className={p.stack}>
      <SourceNotice />
      <MissionPanel />
      <div className={p.grid}>
        <section className={`${p.s12}`}>
          <Card>
            <div className={p.hero}>
              <div style={{ minWidth: 0 }}>
                <div className={p.heroLabel}>
                  Stocks sleeve · the bot's USD {Number(m.initialCapital).toLocaleString('en-US')} subledger
                  <Chip title="Conservative: cash plus positions marked at the bid, less estimated exit fees (accounting/equity.py)">conservative</Chip>
                </div>
                <div className={`${p.heroValue} num`}>
                  {m.equity !== null || tables.ledger_entries.length ? <Odometer text={usd(equity, { precise: true })} /> : <span style={{ color: 'var(--ink-3)' }}>$—</span>}
                </div>
                <div className={p.heroMeta}>
                  <Delta value={m.totalPnl} pill precise />
                  <span>since start (USD {Number(m.initialCapital).toFixed(0)})</span>
                  {todayDelta !== null && (
                    <>
                      <Delta value={todayDelta} precise />
                      <span>today ({today?.kind === 'NY_SESSION' ? 'New York session' : 'UTC day'})</span>
                    </>
                  )}
                  {m.highWaterMark && <span>High-water mark {usd(m.highWaterMark, { precise: true })}</span>}
                  {m.equityAt && <span>as of {dayTime(m.equityAt, tz)} {tzLabel(tz)}</span>}
                </div>
                <div className={p.heroChartHead}>
                  <span className={p.small + ' ' + p.muted}>Equity snapshots · step line (the bot writes a row only when equity changes)</span>
                  <Segmented
                    label="Chart range"
                    value={range}
                    onChange={setRange}
                    options={[
                      { value: 'session', label: 'Session' },
                      { value: 'week', label: '1W' },
                      { value: 'month', label: '1M' },
                      { value: 'all', label: 'All' },
                    ]}
                  />
                </div>
                {points.length ? (
                  <EquityChart points={points} baseline={Number(m.initialCapital)} />
                ) : (
                  <Empty title="No equity snapshots yet" body="They appear once the paper bot starts (and whenever equity changes)." />
                )}
              </div>
              <div>
                <div className={p.coreBox}>
                  {/* The still render holds the space and crossfades out once the live 3D has drawn. */}
                  <img
                    src="/assets/core.png"
                    alt=""
                    className={p.coreStill}
                    style={{ opacity: coreReady ? 0 : 0.92 }}
                  />
                  <div className={p.coreLive} style={{ opacity: coreReady ? 1 : 0 }}>
                    <Boundary fallback={null}>
                      <Suspense fallback={null}>
                        <Core
                          tone={bot.tone}
                          pulse={pulse.decisions}
                          pulseApproved={pulse.lastDecisionApproved}
                          dim={session.phase === 'closed' && status?.kind !== 'replay'}
                          dark={dark}
                          sleeves={rings}
                          onReady={onCoreReady}
                        />
                      </Suspense>
                    </Boundary>
                  </div>
                </div>
                <div className={p.coreCaption}>
                  <div className={p.coreTitle}>{bot.label}</div>
                  <div className={p.coreDetail}>{bot.detail}</div>
                  {sleeves.views.length > 0 && (
                    <div className={p.coreRings} title="Each ring is a sleeve. It glows and turns while that sleeve has something live, and rests dim when it has not.">
                      {sleeves.views.map((v) => (
                        <span key={v.id} className={p.coreRing} data-live={v.live || undefined}>
                          <span className={p.coreRingDot} style={{ background: SLEEVES[v.id].color }} />
                          {SLEEVES[v.id].title}
                          <span className={p.muted}>{v.live ? 'live' : v.note ? 'not started' : 'idle'}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </Card>
        </section>

        <section className={p.s12}>
          <Card>
            <SessionRibbon now={now} />
          </Card>
        </section>

        <section className={p.s12}>
          <div className={p.tiles}>
            <div className={p.tile}>
              <Stat label="Cash" value={<span className="num">{money(m.cash)}</span>} foot={hasMoney ? `Reserve required ${usd(m.requiredReserve)}` : 'Waiting for the ledger'} />
            </div>
            <div className={p.tile}>
              <Stat label="Invested" value={<span className="num">{money(m.invested)}</span>} foot={hasMoney ? `${m.openPositions} open position${m.openPositions === 1 ? '' : 's'}` : ' '} />
            </div>
            <div className={p.tile}>
              <Stat label="Realised net" value={hasMoney ? <Delta value={m.realizedNet} precise /> : '—'} foot={hasMoney ? `Gross ${usd(m.realizedGross, { sign: true, precise: true })}` : ' '} />
            </div>
            <div className={p.tile}>
              <Stat label="Fees paid" value={<span className="num">{money(m.fees)}</span>} foot={hasMoney ? `${tables.fills.length} fill${tables.fills.length === 1 ? '' : 's'}` : ' '} />
            </div>
            <div className={p.tile}>
              <Meter
                label="Drawdown"
                used={m.drawdownFraction ? Number(m.drawdownFraction) / Number(limits.maxDrawdownFraction) : 0}
                valueText={m.drawdownFraction ? pct(m.drawdownFraction, 2) : '—'}
                foot={`Pause at ${pct(limits.maxDrawdownFraction, 0)} from the high-water mark`}
              />
            </div>
            <div className={p.tile}>
              <Meter
                label="Above the floor"
                used={
                  m.floorDistance !== null
                    ? 1 - Math.max(0, Math.min(1, Number(m.floorDistance) / (Number(m.initialCapital) - Number(limits.capitalFloor))))
                    : 0
                }
                valueText={m.floorDistance !== null ? usd(m.floorDistance) : '—'}
                foot={`Pause at USD ${Number(limits.capitalFloor).toFixed(0)}`}
              />
            </div>
          </div>
        </section>

        <section className={`${p.s7} ${p.stretch}`}>
          <PositionsCard positions={positions} now={now} />
        </section>

        <section className={`${p.s5} ${p.stretch}`}>
          <Card
            title="Latest decisions"
            sub={tables.risk_decisions.length ? `${tables.risk_decisions.length} so far` : undefined}
            action={
              <button className="textButton" onClick={() => navigate('/decisions')} style={{ border: 0, background: 'none', color: 'var(--accent-ink)', cursor: 'pointer', fontSize: 13, fontWeight: 500 }}>
                All decisions
              </button>
            }
            style={{ height: '100%' }}
          >
            {recent.length === 0 ? (
              <Empty
                title="No decisions yet"
                body={
                  session.phase === 'entry'
                    ? 'The bot decides about 5 seconds after each 5-minute bar closes.'
                    : 'Candidates are only generated inside the entry window (open + 30 min to close − 30 min).'
                }
              />
            ) : (
              <div className={p.list}>
                <AnimatePresence initial={false}>
                  {recent.map((d) => {
                    const c = candidateById.get(d.candidate_id);
                    return (
                      <motion.button
                        key={d.decision_id}
                        layout
                        initial={{ opacity: 0, y: -10, scale: 0.98 }}
                        animate={{ opacity: 1, y: 0, scale: 1 }}
                        transition={SPRING}
                        className={`${p.row} ${p.rowButton}`}
                        onClick={() => navigate(`/decisions?c=${encodeURIComponent(d.candidate_id)}`)}
                      >
                        <Monogram symbol={symbolOf(c?.instrument_id ?? '')} />
                        <div className={p.rowMain}>
                          <div className={p.rowTitle}>
                            {symbolOf(c?.instrument_id ?? '?')}
                            {d.approved ? <Chip strong style={{ color: 'var(--accent-ink)', background: 'var(--accent-wash)' }}>Taken</Chip> : <VerdictChip decision={d} />}
                          </div>
                          <div className={p.rowSub}>
                            {d.approved
                              ? `Size ${qty(d.payload.quantity ?? '0')} sh · notional ${usd(d.payload.notional_usd ?? '0')}`
                              : decisionVerdict(d).why}
                          </div>
                        </div>
                        <div className={p.rowRight}>
                          <div className={p.rowRightSub}>{clock(d.decided_at, tz)} {tzLabel(tz)}</div>
                          <div className={p.rowRightSub}>{ago(d.decided_at, now)}</div>
                        </div>
                      </motion.button>
                    );
                  })}
                </AnimatePresence>
              </div>
            )}
          </Card>
        </section>

        <section className={p.s12}>
          <Card title="Loss limits" sub="Usage of each loss window. Breaching one pauses new entries until the owner reviews it.">
            {windows.length === 0 ? (
              <Empty title="No loss windows open" body="A window opens when the bot first evaluates losses in a session or week." />
            ) : (
              <div className={p.meters}>
                {windows.map((w) => (
                  <Meter
                    key={w.kind}
                    label={LOSS_WINDOWS[w.kind] ?? w.kind}
                    used={w.usedFraction}
                    valueText={`${usd(D(w.lossUsd).gt(0) ? w.lossUsd : '0', { precise: true })} of ${usd(w.limitUsd)}`}
                    foot={w.breachedAt ? `Breached ${dayTime(w.breachedAt, tz)}` : `Window ${w.windowKey} · start ${usd(w.startEquity, { precise: true })}`}
                  />
                ))}
              </div>
            )}
          </Card>
        </section>
      </div>
    </div>
  );
}

