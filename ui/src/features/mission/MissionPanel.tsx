import { motion, useReducedMotion } from 'motion/react';
import { ArrowUpRight } from 'lucide-react';
import { Link } from 'react-router';
import { goalProgress } from '../../../shared/desk';
import { SLEEVES, sleeveShares, type EquityPoint, type PracticeScan, type PracticeTrade, type PracticeTraderState, type SleeveId, type SleeveView } from '../../../shared/mission';
import { useConsole } from '../../app/store';
import { dayTime, tzLabel, usd } from '../../design/format';
import { Card, Chip, Delta, Dot, Odometer } from '../../design/primitives';
import { useSleeves } from './useSleeves';
import s from './mission.module.css';

const shortUsd = (n: number) => (n >= 1000 ? `$${(n / 1000).toLocaleString('en-US', { maximumFractionDigits: 1 })}k` : `$${n}`);

/**
 * The mission at a glance: three sleeves compounding toward the target. Every number is
 * read from the bot's tables or a practice journal; nothing here is decorative.
 */
export function MissionPanel() {
  const { mission, views, total, start } = useSleeves();
  const reduce = useReducedMotion();
  if (!mission || !views.length) return null;

  const g = goalProgress(total, start, mission.target);
  const shares = sleeveShares(views);
  const next = g.ticks.find((t) => t.value > total) ?? g.ticks[g.ticks.length - 1];
  const trades = mission.practice.flatMap((p) => p.trades.filter((t) => t.state === 'open').map((t) => ({ sleeve: p.id, trade: t })));

  return (
    <Card>
      <div className={s.head}>
        <div>
          <div className={s.label}>
            Mission · {usd(start)} → {usd(mission.target)}
            <Chip title="Three sleeves: the stocks book and the crypto and CFD sleeves, each from its own start. No deposits are ever added: profits are reinvested.">no new money · profits compound</Chip>
            <Chip title="Paper and demo accounts use simulated money">paper · simulated</Chip>
          </div>
          <div className={`${s.total} num`}>
            <Odometer text={usd(total, { precise: true })} />
          </div>
          <div className={s.meta}>
            <Delta value={total - start} pill precise />
            <span>since the start</span>
            <span>
              {g.doublingsDone.toFixed(2)} of {g.doublingsNeeded.toFixed(1)} doublings
            </span>
            {total < mission.target && (
              <span>
                next step {shortUsd(next.value)} · {usd(next.value - total, { precise: true })} to go
              </span>
            )}
          </div>
        </div>
      </div>

      <div
        className={s.track}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(g.fraction * 100)}
        aria-label="Progress to the goal on a doubling scale, split by sleeve"
      >
        <motion.div
          className={s.fill}
          initial={reduce ? false : { width: '0%' }}
          animate={{ width: `${Math.max(g.fraction * 100, 1.5)}%` }}
          transition={reduce ? { duration: 0 } : { duration: 1.2, ease: [0.22, 1, 0.36, 1] }}
        >
          {views.map((v, i) => (
            <span key={v.id} className={s.segment} style={{ flexGrow: shares[i], background: SLEEVES[v.id].color }} title={`${SLEEVES[v.id].title}: ${usd(v.equity, { precise: true })}`} />
          ))}
        </motion.div>
        {g.ticks.map((t) => (
          <span key={t.value} className={s.tick} style={{ left: `${t.at * 100}%` }} />
        ))}
      </div>
      <div className={s.tickLabels} aria-hidden>
        <span style={{ left: '0%' }}>{shortUsd(start)}</span>
        {g.ticks.map((t) => (
          <span key={t.value} style={{ left: `${t.at * 100}%` }}>
            {shortUsd(t.value)}
          </span>
        ))}
      </div>

      <div className={s.sleeves}>
        {views.map((v, i) => (
          <SleeveCard key={v.id} view={v} share={shares[i]} />
        ))}
      </div>

      <OpenTrades trades={trades} />
      <Trader trader={mission.trader} />
      <Signals scan={mission.scan} />
    </Card>
  );
}

/** Words for the practice trader's decision outcomes (ADR-0056). */
const OUTCOME: Record<string, string> = {
  OPENED: 'opened',
  NO_SIGNAL: 'no signal',
  NO_BAR: "waiting for the broker's bar",
  BUSY: 'already holding',
  GROUP_BUSY: 'already holding the group',
  ENTRY_LIMIT: 'entries for the day used',
  SLEEVE_FULL: 'the sleeve already holds its open trades',
  SPREAD: 'spread too wide',
  CLOSED: 'market closed',
  HEDGING_OFF: 'hedging mode is off',
  STOP_PASSED: 'price already beyond the stop',
  TARGET_PASSED: 'price already beyond the target',
  NOT_SIZED: 'too small or too costly to size',
  TOO_SMALL: 'below the minimum size',
  SWITCHED_OFF: 'switched off by its loss cap',
  PAUSED: 'entries paused',
  LATE: 'too late in the day',
  DONE: 'already taken',
  DRY_RUN: 'dry run',
  REJECTED: 'rejected by the broker',
  ORDER_UNKNOWN: 'order answer lost',
  JOURNAL_REFUSED: 'refused by the sleeve rules',
};

/**
 * The practice trader (ADR-0056): the registered trial rules it runs on the demo account by
 * itself, whether it is running, and what each setup decided on its last bar.
 */
function Trader({ trader }: { trader: PracticeTraderState | null }) {
  const tz = useConsole((st) => st.tz);
  if (!trader) return null;
  const running = trader.fresh && trader.live;
  return (
    <div className={s.trades}>
      <div className={s.tradesHead}>
        Practice trader · {running ? 'running on the demo account' : trader.fresh ? 'dry run' : 'stopped'}
        {!trader.fresh && <Chip title="No heartbeat for over five minutes. Start it with npm run trader. Stops and targets stay at the broker meanwhile.">not running</Chip>}
        {trader.entriesPaused && <Chip title={trader.entriesPaused}>entries paused</Chip>}
        {!trader.hedging && <Chip title="Hedging mode is off on the demo account, so two setups cannot hold the same coin at once. Switch it on in the Capital.com platform settings.">hedging off</Chip>}
      </div>
      {trader.setups.map((x) => (
        <div key={x.key} className={s.trade}>
          <b>{x.title}</b>
          <span
            title={
              x.status === 'tested'
                ? `${x.trial} passed the base, stressed and walk-forward checks`
                : x.status === 'practice'
                  ? `Practice, like the stocks bot: a rule without a passing trial, kept trading on demo. Expect small losses to the spread. Off below -$${x.lossCapUsd ?? '?'}.`
                  : `${x.trial} failed one check; run on demo to see whether it holds. Off below -$${x.lossCapUsd ?? '?'}.`
            }
          >
            {x.status === 'tested' ? `tested · ${x.trial}` : x.status === 'practice' ? 'practice' : `forward test · ${x.trial}`}
          </span>
          {!x.enabled ? (
            <span>off</span>
          ) : x.switchedOff ? (
            <span title={x.switchedOff}>switched off</span>
          ) : x.lastDecision ? (
            <span title={x.lastDecision.detail}>
              {x.lastDecision.epic} {dayTime(x.lastDecision.decidedAt, tz)}: {OUTCOME[x.lastDecision.outcome] ?? x.lastDecision.outcome}
            </span>
          ) : (
            <span>{x.decides}</span>
          )}
          <span className="num">
            {x.forwardTrades} trade{x.forwardTrades === 1 ? '' : 's'} · {Number(x.forwardResultUsd) >= 0 ? '+' : ''}${x.forwardResultUsd}
          </span>
        </div>
      ))}
      <p className={s.none}>
        Heartbeat {dayTime(trader.updatedAt, tz)} {tzLabel(tz)}
        {trader.nextDecisionAt ? ` · next decision ${dayTime(trader.nextDecisionAt, tz)} ${tzLabel(tz)}` : ''} · rules of registered trials, sized by their own policy, stop and target held at the broker.
      </p>
    </div>
  );
}

/**
 * The practice watch's scan (ADR-0049, ADR-0052): what the one traded setup says on the last
 * daily close, and any open practice trade that needs a decision. Nothing here is an order.
 */
function Signals({ scan }: { scan: PracticeScan | null }) {
  const tz = useConsole((st) => st.tz);
  if (!scan) return null;
  const due = scan.openTrades.filter((o) => o.overdue || o.beyondStop || o.atTarget || o.exitSignal);
  const pct = (v: string) => `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`;
  return (
    <div className={s.trades}>
      <div className={s.tradesHead}>
        Signals · {scan.title ?? 'practice setup'}
        {scan.feedStale && <Chip title="The market feed has not refreshed, so these readings may be old. It runs with npm run dev.">prices are old</Chip>}
      </div>
      {scan.error ? (
        <p className={s.none}>The scan could not run: {scan.error}</p>
      ) : (
        scan.readings.map((r) => (
          <div key={r.epic} className={s.trade}>
            <b>{r.epic}</b>
            <span className="num">{r.close}</span>
            <span className="num" title="The last daily close against the close a week earlier. The week's direction is traded at Monday 00:00 UTC.">
              week {pct(r.change)}
            </span>
            {r.ticket ? (
              r.ticket.status === 'TRADE' ? (
                <span className="num">
                  <b>{r.ticket.direction === 'LONG' ? 'buy' : 'sell short'} {r.ticket.size}</b> · stop {r.ticket.stop} · target {r.ticket.target} · risk ${r.ticket.riskUsd}
                </span>
              ) : (
                <span title={r.ticket.note}>signal, not taken: {r.ticket.note}</span>
              )
            ) : (
              <span>{r.direction ? `points ${r.direction === 'LONG' ? 'up' : 'down'}, decided on Monday` : 'flat'}</span>
            )}
            {r.stale && <Chip title={`The newest finished daily bar is ${r.day}; a newer one was expected.`}>old bar</Chip>}
          </div>
        ))
      )}
      {due.map((o) => (
        <div key={`${o.sleeve}-${o.tradeId}`} className={s.trade}>
          <b>{o.epic}</b>
          <span>{o.tradeId}</span>
          {o.overdue && <Chip title="Past its time stop: close it before planning another trade.">time stop passed</Chip>}
          {o.beyondStop && <Chip title="The last price is beyond the stop. The broker holds the stop: check whether it filled, then journal the close.">price beyond the stop</Chip>}
          {o.atTarget && <Chip title="The last price reached the target. The broker holds the target: check whether it filled, then journal the close.">price at the target</Chip>}
          {o.exitSignal && <Chip title="The setup's exit signal: close at the next open.">exit signal</Chip>}
        </div>
      ))}
      <p className={s.none}>
        Checked {dayTime(scan.generatedAt, tz)} {tzLabel(tz)}
        {scan.nextDecisionAt ? ` · next decision ${dayTime(scan.nextDecisionAt, tz)} ${tzLabel(tz)}` : ''}
        {scan.riskLimitUsd ? ` · risk limit $${scan.riskLimitUsd} a trade` : ''} · the practice trader places these by itself.
      </p>
    </div>
  );
}

function SleeveCard({ view, share }: { view: SleeveView; share: number }) {
  const meta = SLEEVES[view.id];
  const tone = view.note ? 'paused' : view.live ? 'running' : 'idle';
  return (
    <Link to={meta.to} className={s.sleeve} style={{ ['--sleeve' as string]: meta.color }} aria-label={`${meta.title} sleeve: open ${meta.toLabel}`}>
      <div className={s.sleeveTop}>
        <img className={s.emblem} src={meta.emblem} alt="" width={52} height={52} />
        <div className={s.sleeveName}>
          <div className={s.sleeveTitle}>{meta.title}</div>
          <div className={s.sleeveSub}>
            {meta.venue} · traded by {meta.trader}
          </div>
        </div>
        <ArrowUpRight className={s.go} size={16} aria-hidden />
      </div>
      <div className={s.sleeveMid}>
        <div>
          <div className={`${s.sleeveEquity} num`}>{usd(view.equity, { precise: true })}</div>
          <div className={s.sleeveDelta}>
            <Delta value={view.equity - view.start} precise />
            <span>of {usd(view.start)} · {(share * 100).toFixed(0)}% of the total</span>
          </div>
        </div>
        <Sparkline points={view.curve} baseline={view.start} />
      </div>
      <div className={s.sleeveState}>
        <Dot tone={tone} live={view.live} />
        <span>{view.statusLine}</span>
      </div>
      <div className={s.sleeveFoot}>{view.note ?? view.tally}</div>
    </Link>
  );
}

/** Realized equity as a step line against the sleeve's start; a flat dashed line before any trade. */
function Sparkline({ points, baseline }: { points: EquityPoint[]; baseline: number }) {
  const W = 132;
  const H = 40;
  if (points.length < 2) {
    return (
      <svg className={s.spark} width={W} height={H} viewBox={`0 0 ${W} ${H}`} aria-hidden>
        <line x1={2} x2={W - 2} y1={H / 2} y2={H / 2} className={s.sparkBase} />
      </svg>
    );
  }
  const t0 = points[0].t;
  const t1 = Math.max(points[points.length - 1].t, Date.now() / 1000);
  const values = [...points.map((p) => p.v), baseline];
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const x = (t: number) => 2 + ((t - t0) / (t1 - t0 || 1)) * (W - 4);
  const y = (v: number) => H - 4 - ((v - lo) / span) * (H - 8);
  let d = `M ${x(points[0].t).toFixed(1)} ${y(points[0].v).toFixed(1)}`;
  for (let i = 1; i < points.length; i++) d += ` H ${x(points[i].t).toFixed(1)} V ${y(points[i].v).toFixed(1)}`;
  d += ` H ${x(t1).toFixed(1)}`;
  const last = points[points.length - 1];
  return (
    <svg className={s.spark} width={W} height={H} viewBox={`0 0 ${W} ${H}`} aria-hidden>
      <line x1={2} x2={W - 2} y1={y(baseline)} y2={y(baseline)} className={s.sparkBase} />
      <path d={d} className={s.sparkLine} pathLength={1} />
      <circle cx={x(t1)} cy={y(last.v)} r={2.6} className={s.sparkDot} />
    </svg>
  );
}

function OpenTrades({ trades }: { trades: { sleeve: SleeveId; trade: PracticeTrade }[] }) {
  const tz = useConsole((st) => st.tz);
  if (!trades.length) {
    return <p className={s.none}>No open crypto or CFD trade. Claude journals a plan with a stop before every order; open trades appear here.</p>;
  }
  return (
    <div className={s.trades}>
      <div className={s.tradesHead}>Open practice trades</div>
      {trades.map(({ sleeve, trade: t }) => (
        <Link key={`${sleeve}-${t.tradeId}`} to={`/trades/${sleeve}/${encodeURIComponent(t.tradeId)}`} className={s.trade} title={t.thesis}>
          <span className={s.tradeDot} style={{ background: SLEEVES[sleeve].color }} />
          <b>{t.epic}</b>
          <span>{t.direction === 'BUY' ? 'long' : 'short'} {t.size}</span>
          <span className="num">in {t.fill ?? t.entry}</span>
          <span className="num">stop {t.stop}</span>
          <span className="num">{t.target ? `target ${t.target}` : 'no target'}</span>
          <span className="num">{t.riskUsd ? `risk ${usd(t.riskUsd, { precise: true })}` : ''}</span>
          {t.mark ? (
            <span className="num" title={`If closed now at ${t.mark.price}: price move ${usd(t.mark.grossUsd, { precise: true })}, fees ${usd(t.mark.feesUsd, { precise: true })}`}>
              now {t.mark.price} <Delta value={t.mark.netUsd} precise />
            </span>
          ) : (
            <span className="num">no price yet</span>
          )}
          <span className={s.tradeWhen}>{t.openedAt ? `${dayTime(t.openedAt, tz)} ${tzLabel(tz)}` : ''}</span>
        </Link>
      ))}
    </div>
  );
}
