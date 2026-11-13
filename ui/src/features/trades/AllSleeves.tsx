import { useState } from 'react';
import { useNavigate } from 'react-router';
import { SLEEVE_ORDER, SLEEVES, type SleeveId } from '../../../shared/mission';
import { RED_STREAK_ALERT, rMultiple, tradeHref, tradeStats, type DailyResults, type TradeStory } from '../../../shared/trades';
import { useNow } from '../../app/hooks';
import { useConsole } from '../../app/store';
import { dayTime, price, tzLabel, usd } from '../../design/format';
import { Card, Chip, Delta, Dot, Empty, Segmented } from '../../design/primitives';
import ps from '../../design/primitives.module.css';
import { useSleeves } from '../mission/useSleeves';
import { heldFor, useTrades } from './useTrades';
import p from '../pages.module.css';
import s from './trades.module.css';

const pct = (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(0)}%`);
const rText = (v: number | null) => (v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`);

/** Today's result, or a dash before any trade has closed today. */
const today = (d: DailyResults) => (d.today ? <Delta value={d.today.pnl} precise /> : '—');

/** Losing days in a row; amber from the third, which calls for an explanation. */
function RedDays({ d }: { d: DailyResults }) {
  if (!d.days.length) return <>—</>;
  if (d.redStreak >= RED_STREAK_ALERT) {
    return (
      <Chip strong title={`${d.redStreak} losing days in a row: this needs an explanation`} style={{ color: 'var(--warning-ink)' }}>
        <Dot tone="paused" /> {d.redStreak}
      </Chip>
    );
  }
  return <>{d.redStreak}</>;
}

/** Balance, result, costs and hit rate of each sleeve, and of the three together. */
export function SleeveSummary() {
  const { views, total, start } = useSleeves();
  const trades = useTrades();
  if (!views.length) return null;
  const all = tradeStats(trades.all);
  return (
    <Card
      title="All three sleeves"
      sub="Balance and closed-trade results per sleeve. Results are after fees and financing; fees and financing are shown so you see what the trading itself cost. Today and the last 7 days count trades by the day they closed; losing days in a row skip days without a closed trade."
      flush
    >
      <div className={p.scroll}>
        <table className={ps.table}>
          <thead>
            <tr>
              <th>Sleeve</th>
              <th className={ps.r}>Balance</th>
              <th className={ps.r}>Since start</th>
              <th className={ps.r}>Today</th>
              <th className={ps.r}>Last 7 days</th>
              <th className={ps.r}>Losing days in a row</th>
              <th className={ps.r}>Closed</th>
              <th className={ps.r}>Open</th>
              <th className={ps.r}>Won</th>
              <th className={ps.r}>Net result</th>
              <th className={ps.r}>Fees</th>
              <th className={ps.r}>Financing</th>
              <th className={ps.r}>Avg per trade</th>
            </tr>
          </thead>
          <tbody>
            {views.map((v) => {
              const st = trades.stats[v.id];
              return (
                <tr key={v.id} title={v.note ?? v.statusLine}>
                  <td>
                    <span className={s.sleeveCell}>
                      <img src={SLEEVES[v.id].emblem} alt="" />
                      {SLEEVES[v.id].title}
                    </span>
                  </td>
                  <td className={ps.r}>{usd(v.equity, { precise: true })}</td>
                  <td className={ps.r}>
                    <Delta value={v.equity - v.start} precise />
                  </td>
                  <td className={ps.r}>{today(trades.days[v.id])}</td>
                  <td className={ps.r}>{trades.days[v.id].days.length ? <Delta value={trades.days[v.id].week} precise /> : '—'}</td>
                  <td className={ps.r}>
                    <RedDays d={trades.days[v.id]} />
                  </td>
                  <td className={ps.r}>{st.closed}</td>
                  <td className={ps.r}>{st.open}</td>
                  <td className={ps.r}>{pct(st.winRate)}</td>
                  <td className={ps.r}>{st.closed ? <Delta value={st.pnl} precise /> : '—'}</td>
                  <td className={ps.r}>{usd(st.fees, { precise: true })}</td>
                  <td className={ps.r}>{usd(st.financing, { precise: true })}</td>
                  <td className={ps.r}>{rText(st.avgR)}</td>
                </tr>
              );
            })}
            <tr className={s.totalRow}>
              <td>Total</td>
              <td className={ps.r}>{usd(total, { precise: true })}</td>
              <td className={ps.r}>
                <Delta value={total - start} precise />
              </td>
              <td className={ps.r}>{today(trades.allDays)}</td>
              <td className={ps.r}>{trades.allDays.days.length ? <Delta value={trades.allDays.week} precise /> : '—'}</td>
              <td className={ps.r}>
                <RedDays d={trades.allDays} />
              </td>
              <td className={ps.r}>{all.closed}</td>
              <td className={ps.r}>{all.open}</td>
              <td className={ps.r}>{pct(all.winRate)}</td>
              <td className={ps.r}>{all.closed ? <Delta value={all.pnl} precise /> : '—'}</td>
              <td className={ps.r}>{usd(all.fees, { precise: true })}</td>
              <td className={ps.r}>{usd(all.financing, { precise: true })}</td>
              <td className={ps.r}>{rText(all.avgR)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </Card>
  );
}

type Filter = 'all' | SleeveId;

/** Every trade of every sleeve, newest first; a row opens the trade's story. */
export function TradesTable() {
  const trades = useTrades();
  const tz = useConsole((st) => st.tz);
  const now = useNow(30_000);
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>('all');
  const rows = (filter === 'all' ? trades.all : trades.bySleeve[filter])
    .filter((t) => t.state !== 'cancelled')
    .slice()
    .sort((a, b) => (b.openedAt ?? b.plannedAt).localeCompare(a.openedAt ?? a.plannedAt));
  const open = (t: TradeStory) => navigate(tradeHref(t));

  return (
    <Card
      title="Trades"
      sub="Every trade of every sleeve, newest first. Click one for its story: the plan, the chart, what happened and the lesson. An open trade shows the latest price and its result if closed now, after entry and exit fees."
      action={
        <Segmented<Filter>
          label="Sleeve"
          value={filter}
          onChange={setFilter}
          options={[{ value: 'all', label: 'All' }, ...SLEEVE_ORDER.map((id) => ({ value: id as Filter, label: SLEEVES[id].title }))]}
        />
      }
      flush
    >
      {rows.length === 0 ? (
        <Empty
          title="No trade yet"
          body="Stock trades appear when the bot's practice session takes its first entry; crypto and CFD trades appear as soon as Claude journals a plan."
        />
      ) : (
        <div className={p.scroll}>
          <table className={ps.table}>
            <thead>
              <tr>
                <th>Trade</th>
                <th>Opened ({tzLabel(tz)})</th>
                <th>Held</th>
                <th className={ps.r}>Entry</th>
                <th className={ps.r}>Exit</th>
                <th className={ps.r}>Net result</th>
                <th className={ps.r}>Fees</th>
                <th className={ps.r}>Financing</th>
                <th className={ps.r}>In risk units</th>
                <th>Ended by</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr
                  key={`${t.sleeve}:${t.id}`}
                  className={s.rowLink}
                  tabIndex={0}
                  role="link"
                  onClick={() => open(t)}
                  onKeyDown={(e) => e.key === 'Enter' && open(t)}
                  title={t.plan.thesis}
                >
                  <td>
                    <span className={s.sleeveCell}>
                      <span className={s.sleeveDot} style={{ background: SLEEVES[t.sleeve].color }} />
                      {t.symbol}
                      <span style={{ fontWeight: 400, color: 'var(--ink-2)' }}>
                        {t.direction} {t.size}
                      </span>
                    </span>
                  </td>
                  <td>{t.openedAt ? dayTime(t.openedAt, tz) : 'planned'}</td>
                  <td>{heldFor(t.openedAt, t.closedAt, now)}</td>
                  <td className={ps.r}>{t.entry !== null ? price(t.entry) : '—'}</td>
                  <td className={ps.r}>{t.exit !== null ? price(t.exit) : t.mark ? <span style={{ color: 'var(--ink-2)' }}>now {price(t.mark.price)}</span> : '—'}</td>
                  <td className={ps.r} title={t.mark ? 'If closed now, after entry and exit fees' : undefined}>
                    {t.pnl !== null ? <Delta value={t.pnl} precise /> : t.mark ? <Delta value={t.mark.netUsd} precise /> : '—'}
                  </td>
                  <td className={ps.r}>{usd(t.mark ? t.mark.feesUsd : t.fees, { precise: true })}</td>
                  <td className={ps.r}>{usd(t.financing, { precise: true })}</td>
                  <td className={ps.r}>{rText(rMultiple(t))}</td>
                  <td>
                    {t.state === 'open' ? (
                      <Chip>
                        <Dot tone="running" live /> open
                      </Chip>
                    ) : t.state === 'planned' ? (
                      <Chip>planned</Chip>
                    ) : (
                      (t.reason ?? '—')
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
