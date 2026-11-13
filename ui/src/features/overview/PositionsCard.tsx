import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import type { PositionView } from '../../../shared/derive';
import { SLEEVE_ORDER, SLEEVES, type SleeveId } from '../../../shared/mission';
import { lastActivity, positionsBoard, RECENT_TRADE_HOURS, tradeHref } from '../../../shared/trades';
import { useConsole } from '../../app/store';
import { clock, dayTime, price, qty, tzLabel, usd } from '../../design/format';
import { Card, Delta, Empty, Monogram, RangeBar, Segmented } from '../../design/primitives';
import { heldFor, useTrades } from '../trades/useTrades';
import p from '../pages.module.css';

type Filter = SleeveId | 'all';

const FILTERS: { value: Filter; label: string }[] = [
  { value: 'all', label: 'All' },
  ...SLEEVE_ORDER.map((id) => ({ value: id as Filter, label: SLEEVES[id].title })),
];


/** At most this many recent trades are listed; the Money page has all of them. */
const RECENT_ROWS = 8;

/** The sleeve a row belongs to: its colour and its name. */
function SleeveTag({ sleeve }: { sleeve: SleeveId }) {
  return (
    <span className={p.sleeveTag}>
      <span className={p.sleeveTagDot} style={{ background: SLEEVES[sleeve].color }} />
      {SLEEVES[sleeve].title}
    </span>
  );
}

/**
 * What is open now in all three sleeves, what waits to fill, and every trade of the last 48 hours. Stock positions
 * come from the bot's tables, crypto and CFD trades from the practice journals; one filter picks
 * a sleeve. Every row opens the trade's story.
 */
export function PositionsCard({ positions, now }: { positions: PositionView[]; now: number }) {
  const [filter, setFilter] = useState<Filter>('all');
  const trades = useTrades();
  const tz = useConsole((s) => s.tz);
  const navigate = useNavigate();
  const { open, waiting, recent } = useMemo(() => positionsBoard(trades.all, now, filter), [trades.all, now, filter]);
  // a stock position the bot holds without a matching round trip yet (its entry is still being booked)
  const unmatched = filter === 'all' || filter === 'stocks' ? positions.filter((pos) => !open.some((t) => t.instrumentId === pos.instrumentId)) : [];
  const openCount = open.length + unmatched.length;
  const shown = recent.slice(0, RECENT_ROWS);
  const where = filter === 'all' ? 'in any sleeve' : `in ${SLEEVES[filter].title}`;

  return (
    <Card
      title="Positions and trades"
      sub={`Open now in all three sleeves, orders waiting to fill, and every trade of the last ${RECENT_TRADE_HOURS} hours`}
      action={<Segmented label="Sleeve" options={FILTERS} value={filter} onChange={setFilter} />}
      style={{ height: '100%' }}
    >
      {openCount === 0 && waiting.length === 0 && recent.length === 0 ? (
        <Empty
          art="/assets/empty-positions.png"
          title={`Nothing open ${where}`}
          body={`No trade ${where} in the last ${RECENT_TRADE_HOURS} hours either. Positions appear here the moment an entry fills.`}
        />
      ) : (
        <div className={p.list}>
          <div className={p.subhead}>
            Open now <span className={p.subheadNote}>{openCount || 'nothing open'}</span>
          </div>
          {open.map((t) => {
            const pos = t.sleeve === 'stocks' ? (positions.find((x) => x.instrumentId === t.instrumentId) ?? null) : null;
            const last = pos?.lastPrice != null ? Number(pos.lastPrice) : (t.mark?.price ?? null);
            const result = pos ? pos.unrealized : t.mark ? t.mark.netUsd : null;
            const stop = pos?.stop != null ? Number(pos.stop) : t.plan.stop;
            const target = pos?.target != null ? Number(pos.target) : t.plan.target;
            return (
              <button key={`${t.sleeve}:${t.id}`} type="button" className={`${p.row} ${p.rowButton}`} onClick={() => navigate(tradeHref(t))} title={t.plan.thesis || undefined}>
                <Monogram symbol={t.sleeve === 'crypto' ? t.symbol.replace(/USD$/, '') : t.symbol} />
                <div className={p.rowMain}>
                  <div className={p.rowTitle}>
                    {t.symbol} <SleeveTag sleeve={t.sleeve} />
                  </div>
                  <div className={p.rowSub}>
                    {t.direction} {t.size} · in {price(t.entry)} · held {heldFor(t.openedAt, null, now)}
                    {pos?.maxHoldUntil && ` · closes by ${clock(pos.maxHoldUntil, tz)} ${tzLabel(tz)}`}
                  </div>
                  {stop !== null && target !== null && last !== null && (
                    <div style={{ marginTop: 10, maxWidth: 320 }}>
                      <RangeBar stop={stop} price={last} target={target} />
                    </div>
                  )}
                </div>
                <div className={p.rowRight}>
                  <div>{last !== null ? price(last) : '—'}</div>
                  <div className={p.rowRightSub} title={t.mark ? 'If closed now, after entry and exit fees' : undefined}>
                    {result !== null ? <Delta value={result} precise /> : 'no price yet'}
                  </div>
                </div>
              </button>
            );
          })}
          {unmatched.map((pos) => (
            <div key={pos.instrumentId} className={p.row}>
              <Monogram symbol={pos.symbol} />
              <div className={p.rowMain}>
                <div className={p.rowTitle}>
                  {pos.symbol} <SleeveTag sleeve="stocks" />
                </div>
                <div className={p.rowSub}>
                  {qty(pos.quantity)} sh · avg {price(pos.avgCost)}
                  {pos.maxHoldUntil && ` · closes by ${clock(pos.maxHoldUntil, tz)} ${tzLabel(tz)}`}
                </div>
                {pos.stop && pos.target && pos.lastPrice && (
                  <div style={{ marginTop: 10, maxWidth: 320 }}>
                    <RangeBar stop={Number(pos.stop)} price={Number(pos.lastPrice)} target={Number(pos.target)} />
                  </div>
                )}
              </div>
              <div className={p.rowRight}>
                <div>{pos.lastPrice ? price(pos.lastPrice) : '—'}</div>
                <div className={p.rowRightSub}>
                  <Delta value={pos.unrealized} precise />
                </div>
              </div>
            </div>
          ))}

          {waiting.length > 0 && (
            <div className={p.subhead}>
              Waiting to fill <span className={p.subheadNote}>{waiting.length} planned</span>
            </div>
          )}
          {waiting.map((t) => (
            <button key={`${t.sleeve}:${t.id}`} type="button" className={`${p.row} ${p.rowButton}`} onClick={() => navigate(tradeHref(t))} title={t.plan.thesis || undefined}>
              <Monogram symbol={t.sleeve === 'crypto' ? t.symbol.replace(/USD$/, '') : t.symbol} />
              <div className={p.rowMain}>
                <div className={p.rowTitle}>
                  {t.symbol} <SleeveTag sleeve={t.sleeve} />
                </div>
                <div className={p.rowSub}>
                  {t.direction} {t.size} · {t.direction === 'long' ? 'buys' : 'sells'} at {price(t.plan.entry)}
                  {t.plan.stop !== null && ` · stop ${price(t.plan.stop)}`}
                  {t.plan.target !== null && ` · target ${price(t.plan.target)}`}
                </div>
              </div>
              <div className={p.rowRight}>
                <div>{t.risk !== null ? `risk ${usd(t.risk, { precise: true })}` : '—'}</div>
                <div className={p.rowRightSub}>
                  planned {dayTime(t.plannedAt, tz)} {tzLabel(tz)}
                </div>
              </div>
            </button>
          ))}

          <div className={p.subhead}>
            Last {RECENT_TRADE_HOURS} hours <span className={p.subheadNote}>{recent.length ? `${recent.length} ended` : 'no trade ended'}</span>
          </div>
          {shown.map((t) => (
            <button key={`${t.sleeve}:${t.id}`} type="button" className={`${p.row} ${p.rowButton}`} onClick={() => navigate(tradeHref(t))} title={t.plan.thesis || undefined}>
              <Monogram symbol={t.sleeve === 'crypto' ? t.symbol.replace(/USD$/, '') : t.symbol} />
              <div className={p.rowMain}>
                <div className={p.rowTitle}>
                  {t.symbol} <SleeveTag sleeve={t.sleeve} />
                </div>
                <div className={p.rowSub}>
                  {t.direction} {t.size}
                  {t.state === 'closed'
                    ? ` · ${price(t.entry)} to ${price(t.exit)} · held ${heldFor(t.openedAt, t.closedAt, now)}`
                    : ` · planned at ${price(t.plan.entry)}`}
                  {` · ${dayTime(lastActivity(t), tz)} ${tzLabel(tz)}`}
                </div>
              </div>
              <div className={p.rowRight}>
                <div>{t.pnl !== null ? <Delta value={t.pnl} precise /> : '—'}</div>
                <div className={p.rowRightSub}>{t.state === 'closed' ? (t.reason ?? 'Closed') : 'Plan cancelled'}</div>
              </div>
            </button>
          ))}
          {recent.length > shown.length && (
            <button type="button" className={`${p.row} ${p.rowButton}`} onClick={() => navigate('/money')}>
              <span className={p.rowSub}>and {recent.length - shown.length} more on the Money page</span>
            </button>
          )}
        </div>
      )}
    </Card>
  );
}
