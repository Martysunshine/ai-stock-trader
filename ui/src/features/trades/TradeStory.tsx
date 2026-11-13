import { useMemo } from 'react';
import { ArrowLeft } from 'lucide-react';
import { Link, useParams } from 'react-router';
import { joinCandles, type Ohlcv, type Timeframe } from '../../../shared/aggregate';
import { SLEEVES, type SleeveId, type TradeEvent } from '../../../shared/mission';
import { rMultiple, type TradeStory as Story } from '../../../shared/trades';
import { useMarketBars, useMarketIndex } from '../../app/api';
import { useNow } from '../../app/hooks';
import { useConsole } from '../../app/store';
import type { ChartLine, ChartMarker } from '../../charts/CandleChart';
import { MarketChart } from '../../charts/MarketChart';
import { dayTime, price, tzLabel, usd } from '../../design/format';
import { Card, Chip, Delta, Dot, Empty, KV, type Tone } from '../../design/primitives';
import { botCandles } from '../markets/candles';
import { WhatWasKnown } from '../markets/IntelCards';
import { heldFor, useTrades } from './useTrades';
import p from '../pages.module.css';
import s from './trades.module.css';

const STATE: Record<Story['state'], { label: string; tone: Tone }> = {
  planned: { label: 'Planned, not opened', tone: 'idle' },
  open: { label: 'Open', tone: 'running' },
  closed: { label: 'Closed', tone: 'neutral' },
  cancelled: { label: 'Cancelled', tone: 'paused' },
};

const EVENT_TONE: Record<TradeEvent['kind'], Tone> = {
  plan: 'idle',
  open: 'replay',
  amend: 'paused',
  note: 'neutral',
  close: 'good',
  cancel: 'paused',
  review: 'replay',
};

/** Bars and the left edge of the view sized to the trade: minutes for a day trade, days for a swing. */
function chartWindow(t: Story, now: number): { tf: Timeframe; limit: number; from: number; to: number | null; step: number } {
  const start = Date.parse(t.openedAt ?? t.plannedAt);
  const end = t.closedAt ? Date.parse(t.closedAt) : now;
  const span = Math.max(end - start, 30 * 60_000);
  const pad = Math.max(span * 0.6, 2 * 3_600_000);
  const from = (start - pad) / 1000;
  // A closed trade is framed by its own life; an open one runs to the newest bar.
  const to = t.closedAt ? (end + pad) / 1000 : null;
  const total = (now - (start - pad)) / 1000;
  if (span < 36 * 3_600_000) return { tf: '5m', limit: Math.min(4000, Math.ceil(total / 300) + 20), from, to, step: 300 };
  if (span < 45 * 86_400_000) return { tf: '1h', limit: Math.min(4000, Math.ceil(total / 3600) + 20), from, to, step: 3600 };
  return { tf: '1d', limit: Math.min(1500, Math.ceil(total / 86_400) + 20), from, to, step: 86_400 };
}

export default function TradeStory() {
  const { sleeve, id } = useParams();
  const trades = useTrades();
  const story = trades.find(sleeve, id);
  if (!story) {
    return (
      <div className={p.stack}>
        <BackLink />
        <Card>
          <Empty
            title={trades.loading ? 'Loading the trade' : 'This trade is not here'}
            body={trades.loading ? undefined : 'It is not in the journal or the bot database the console is reading. The Money page lists every trade it can see.'}
          />
        </Card>
      </div>
    );
  }
  return <StoryView story={story} />;
}

function BackLink() {
  return (
    <Link to="/money" className={s.back}>
      <ArrowLeft size={15} aria-hidden /> All trades
    </Link>
  );
}

function StoryView({ story: t }: { story: Story }) {
  const tz = useConsole((st) => st.tz);
  const tables = useConsole((st) => st.tables);
  const now = useNow(30_000);
  const meta = SLEEVES[t.sleeve as SleeveId];
  const win = useMemo(() => chartWindow(t, now), [t, Math.floor(now / 300_000)]); // eslint-disable-line react-hooks/exhaustive-deps
  const index = useMarketIndex();
  const inst = index.data?.groups.flatMap((g) => g.instruments).find((i) => i.id === t.marketId);
  // Only instruments the market feed lists have feed bars (a replay's synthetic stock has none).
  const feed = useMarketBars(inst ? t.marketId : null, win.tf, win.limit);

  const data = useMemo<Ohlcv[]>(() => {
    const fromFeed = feed.data?.bars ?? [];
    if (!t.instrumentId || win.tf !== '5m') return fromFeed;
    const bot = botCandles(tables, t.instrumentId).map((c) => ({ t: c.time, o: c.open, h: c.high, l: c.low, c: c.close, v: c.volume ?? 0 }));
    return joinCandles<Ohlcv>([fromFeed, bot]);
  }, [feed.data, tables, t.instrumentId, win.tf]);

  const lines = useMemo<ChartLine[]>(() => {
    const out: ChartLine[] = [];
    if (t.entry !== null) out.push({ price: t.entry, kind: 'reference', title: t.openedAt ? 'entry' : 'planned entry' });
    const stop = t.plan.stop;
    if (stop !== null) out.push({ price: stop, kind: 'stop', title: 'stop' });
    if (t.plan.target !== null) out.push({ price: t.plan.target, kind: 'target', title: 'target' });
    return out;
  }, [t]);

  const markers = useMemo<ChartMarker[]>(() => {
    const out: ChartMarker[] = [];
    const long = t.direction === 'long';
    // A marker sits on the bar that contains its moment (the newest bar starting at or before it).
    const onBar = (iso: string) => {
      const at = Date.parse(iso) / 1000;
      let best: number | null = null;
      for (const b of data) {
        if (b.t > at) break;
        best = b.t;
      }
      return best ?? Math.floor(at / win.step) * win.step;
    };
    if (t.openedAt) out.push({ time: onBar(t.openedAt), kind: long ? 'buy' : 'sell', text: long ? 'in' : 'short' });
    if (t.closedAt) out.push({ time: onBar(t.closedAt), kind: long ? 'sell' : 'buy', text: 'out' });
    return out;
  }, [t, data, win.step]);

  const digits = inst?.precision ?? (t.sleeve === 'stocks' ? 2 : undefined);
  const px = (v: number | null) => (v === null ? '—' : price(v, digits));
  const r = rMultiple(t);
  const state = STATE[t.state];
  const plannedR = t.plan.entry !== null && t.plan.stop !== null && t.plan.target !== null && t.plan.entry !== t.plan.stop
    ? Math.abs(t.plan.target - t.plan.entry) / Math.abs(t.plan.entry - t.plan.stop)
    : null;
  const slip = t.openedAt && t.entry !== null && t.plan.entry !== null ? t.entry - t.plan.entry : null;

  return (
    <div className={p.stack}>
      <BackLink />
      <div className={s.head} style={{ ['--sleeve' as string]: meta.color }}>
        <img className={s.emblem} src={meta.emblem} alt="" width={56} height={56} />
        <div className={s.headMain}>
          <h1 className={p.title}>
            {t.symbol} · {t.direction} {t.size}
          </h1>
          <p className={p.subtitle}>
            {meta.title} sleeve · {meta.venue} · traded by {t.by} · {dayTime(t.openedAt ?? t.plannedAt, tz)} {tzLabel(tz)}
          </p>
        </div>
        <div className={s.headResult}>
          <Chip>
            <Dot tone={state.tone} live={t.state === 'open'} /> {state.label}
          </Chip>
          {t.pnl !== null && (
            <div className={`${s.result} num`}>
              <Delta value={t.pnl} precise />
              {r !== null && <span className={s.rMultiple}>{r >= 0 ? '+' : ''}{r.toFixed(2)}R</span>}
            </div>
          )}
        </div>
      </div>

      <div className={p.grid}>
        <section className={p.s8}>
          <Card title="On the chart" sub={`Entry, stop and target as planned; where it went in and came out. ${win.tf === '5m' ? '5-minute' : win.tf === '1h' ? 'Hourly' : 'Daily'} bars.`}>
            {data.length ? (
              <MarketChart
                data={data}
                mode="candles"
                up={(t.pnl ?? 0) >= 0}
                markers={markers}
                lines={lines}
                visibleFrom={win.from}
                visibleTo={win.to}
                fitLines
                rangeKey={`${t.sleeve}:${t.id}:${win.tf}`}
                onHover={() => undefined}
                height={380}
                precision={digits}
                showVolume={inst?.has_volume ?? t.sleeve === 'stocks'}
              />
            ) : (
              <Empty
                title={feed.isLoading ? 'Loading prices' : 'No prices for this period'}
                body={feed.isLoading ? undefined : 'The market feed keeps 5-minute bars for 14 days and hourly bars for 180. Start it with npm run dev to fill recent prices.'}
              />
            )}
          </Card>
        </section>
        <section className={p.s4}>
          <div className={p.stack}>
            <Card title="Result">
              <KV
                items={[
                  ['Entry', px(t.entry)],
                  ['Exit', px(t.exit)],
                  ['Net result', t.pnl !== null ? <Delta value={t.pnl} precise /> : 'still open'],
                  ['Fees', usd(t.fees, { precise: true })],
                  ['Financing', usd(t.financing, { precise: true })],
                  ['Planned risk', t.risk !== null ? usd(t.risk, { precise: true }) : '—'],
                  ['Result in risk units', r !== null ? `${r >= 0 ? '+' : ''}${r.toFixed(2)}R` : '—'],
                  ['Held', heldFor(t.openedAt, t.closedAt, now)],
                  ['Ended by', t.reason ?? (t.state === 'open' ? 'still open' : '—')],
                ]}
              />
            </Card>
          </div>
        </section>
      </div>

      <div className={p.grid}>
        <section className={p.s5}>
          <Card title="The plan" sub="Written before the order.">
            <p className={s.thesis}>{t.plan.thesis}</p>
            <KV
              items={[
                ['Setup', t.plan.setup || '—'],
                ['Timeframe', t.plan.timeframe || '—'],
                ['Planned entry', px(t.plan.entry)],
                ['Stop', px(t.plan.stop)],
                ['Target', px(t.plan.target)],
                ['Reward to risk', plannedR !== null ? `${plannedR.toFixed(1)} to 1` : '—'],
                ['Wrong if', t.plan.invalidation || '—'],
                ...(slip !== null ? ([['Fill vs plan', `${slip >= 0 ? '+' : ''}${price(slip, digits)}`]] as [string, string][]) : []),
              ]}
            />
            {t.candidateId && (
              <Link className={s.more} to={`/decisions?c=${encodeURIComponent(t.candidateId)}`}>
                Full decision trail: every check, order and broker event
              </Link>
            )}
          </Card>
          <div style={{ marginTop: 'var(--s-4)' }}>
            <WhatWasKnown id={t.marketId} at={t.plannedAt} tz={tz} evidence={t.plan.evidence} byBot={t.sleeve === 'stocks'} />
          </div>
        </section>
        <section className={p.s7}>
          <div className={p.stack}>
            <Card title="What happened" sub={`In order. Times in ${tzLabel(tz)}.`}>
              <ol className={s.timeline}>
                {t.events.map((e, i) => (
                  <li key={i} className={s.event}>
                    <span className={s.eventDot}>
                      <Dot tone={EVENT_TONE[e.kind]} />
                    </span>
                    <div>
                      <div className={s.eventHead}>
                        <b>{e.title}</b>
                        <span className={s.eventWhen}>{dayTime(e.at, tz)}</span>
                      </div>
                      <div className={s.eventDetail}>{e.detail}</div>
                    </div>
                  </li>
                ))}
              </ol>
            </Card>
            <Card title="The lesson" sub={t.sleeve === 'stocks' ? 'The bot follows fixed rules and writes no lessons; its numbers are the lesson.' : 'Written after the trade closed.'}>
              {t.lesson ? (
                <>
                  <p className={s.lesson}>{t.lesson.text}</p>
                  <Chip>
                    <Dot tone={t.lesson.followedPlan ? 'good' : 'paused'} /> {t.lesson.followedPlan ? 'Plan followed' : 'Plan not followed'}
                  </Chip>
                </>
              ) : t.sleeve === 'stocks' ? (
                <p className={s.muted}>
                  {t.pnl === null
                    ? 'Open: the stop, the target or the session close will end it.'
                    : `Ended by: ${t.reason ?? 'exit'}. Result ${r !== null ? `${r >= 0 ? '+' : ''}${r.toFixed(2)}R` : usd(t.pnl, { precise: true, sign: true })} after ${usd(t.fees, { precise: true })} of fees.`}
                </p>
              ) : (
                <p className={s.muted}>{t.state === 'closed' ? 'Not reviewed yet.' : 'The review is written when the trade closes.'}</p>
              )}
            </Card>
          </div>
        </section>
      </div>
    </div>
  );
}
