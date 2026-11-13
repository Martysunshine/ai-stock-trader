import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { aggregate, joinCandles, sessionsBackStart, TF_SECONDS, type DayRule, type Ohlcv, type Timeframe } from '../../../shared/aggregate';
import { latestBy, symbolOf } from '../../../shared/derive';
import { useHistory, useHistorySymbols, useMarketBars, useMarketIndex, type MarketGroup, type MarketInstrument } from '../../app/api';
import { useNow } from '../../app/hooks';
import { useConsole } from '../../app/store';
import type { ChartLine, ChartMarker } from '../../charts/CandleChart';
import { MarketChart } from '../../charts/MarketChart';
import { ago, day, dayTime, pct, price, qty, tzLabel, usd } from '../../design/format';
import { reasonTitle } from '../../design/language';
import { Card, Chip, CopyCommand, Delta, Dot, Empty, KV, Segmented } from '../../design/primitives';
import { botCandles } from './candles';
import { IntelCards } from './IntelCards';
import p from '../pages.module.css';
import m from './markets.module.css';
import { decisionVerdict } from '../../../shared/verdict';
import { SLEEVE_OF_GROUP, SLEEVES, type SleeveId } from '../../../shared/mission';
import { useSleeves } from '../mission/useSleeves';

type RangeId = '1D' | '1W' | '1M' | '3M' | '1Y' | 'All';
/**
 * Stocks count New York sessions back (`sessions`); crypto and CFDs trade around the clock, so their
 * ranges are rolling UTC days (`days`). `feedDays` is how much wall time the market feed is asked
 * for, per day rule (a stock week of 5 sessions spans up to 9 calendar days).
 */
const RANGES: {
  id: RangeId;
  tf: Timeframe;
  sessions: number | null;
  days: number | null;
  limit: number;
  feedDays: Record<DayRule, number>;
  label: Record<DayRule, string>;
}[] = [
  { id: '1D', tf: '5m', sessions: 1, days: 1, limit: 2400, feedDays: { nyse: 4, utc: 1 }, label: { nyse: 'today', utc: 'past 24 hours' } },
  { id: '1W', tf: '15m', sessions: 5, days: 7, limit: 1600, feedDays: { nyse: 9, utc: 7 }, label: { nyse: 'past week', utc: 'past 7 days' } },
  { id: '1M', tf: '1h', sessions: 21, days: 30, limit: 1400, feedDays: { nyse: 31, utc: 30 }, label: { nyse: 'past month', utc: 'past 30 days' } },
  { id: '3M', tf: '1d', sessions: 63, days: 90, limit: 5000, feedDays: { nyse: 92, utc: 90 }, label: { nyse: 'past 3 months', utc: 'past 90 days' } },
  { id: '1Y', tf: '1d', sessions: 252, days: 365, limit: 5000, feedDays: { nyse: 366, utc: 365 }, label: { nyse: 'past year', utc: 'past 365 days' } },
  { id: 'All', tf: '1d', sessions: null, days: null, limit: 5000, feedDays: { nyse: 5000, utc: 5000 }, label: { nyse: 'all time', utc: 'all time' } },
];

const STOCKS = 'stocks';

/** One labelled row of the Markets page: a watchlist group of the feed (or the bot's stocks). */
interface Row {
  id: string;
  title: string;
  source: string | null;
  dayRule: DayRule;
  group: MarketGroup | null;
  items: { id: string; symbol: string; name: string | null; feed: MarketInstrument | null }[];
}

/** `/markets/cfd:GOLD` names an instrument by id; a bare `/markets/AAPL` is a stock. */
const marketId = (param: string | undefined): string | null => (param ? (param.includes(':') ? param : `${STOCKS}:${param}`) : null);

/** Index of the candle whose bucket contains `t` (latest start <= t). */
function bucketOf(candles: Ohlcv[], t: number): number | null {
  let lo = 0;
  let hi = candles.length - 1;
  let ans: number | null = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (candles[mid].t <= t) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}

const utcDay = (t: number) => new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(t * 1000));

export default function Markets() {
  const tables = useConsole((s) => s.tables);
  const status = useConsole((s) => s.status);
  const tz = useConsole((s) => s.tz);
  const params = useParams();
  const navigate = useNavigate();
  const now = useNow(30_000);
  const historySymbols = useHistorySymbols();
  const marketIndex = useMarketIndex();
  const isReplay = status?.kind === 'replay';
  const [rangeId, setRangeId] = useState<RangeId>('1D');
  const [mode, setMode] = useState<'area' | 'candles'>('area');
  const [hovered, setHovered] = useState<Ohlcv | null>(null);
  const range = RANGES.find((r) => r.id === rangeId)!;
  const index = marketIndex.data?.available ? marketIndex.data : null;
  const sleeves = useSleeves();
  const sleeveOf = (rowId: string): SleeveId | null => SLEEVE_OF_GROUP[rowId] ?? null;
  const sleeveView = (rowId: string) => sleeves.views.find((v) => v.id === sleeveOf(rowId)) ?? null;

  const rows = useMemo<Row[]>(() => {
    const groups = index?.groups ?? [];
    const stockGroup = groups.find((g) => g.id === STOCKS) ?? null;
    // Stocks: the feed's watchlist, then anything the bot holds bars or specs for, then research history.
    const feedStocks = isReplay ? [] : (stockGroup?.instruments ?? []);
    const fromBot = [...tables.instruments.map((i) => i.instrument_id), ...tables.bars.map((b) => b.instrument_id)].map(symbolOf);
    const fromHistory = isReplay ? [] : (historySymbols.data ?? []).filter((h) => h.dataset.startsWith('alpaca')).map((h) => h.symbol);
    const stockSymbols = [...new Set([...feedStocks.map((i) => i.symbol), ...fromBot, ...fromHistory])];
    const stocks: Row = {
      id: STOCKS,
      title: stockGroup?.title ?? 'Stocks',
      source: isReplay ? 'Replay' : (stockGroup?.source ?? null),
      dayRule: 'nyse',
      group: isReplay ? null : stockGroup,
      items: stockSymbols.map((symbol) => {
        const feed = feedStocks.find((i) => i.symbol === symbol) ?? null;
        return { id: `${STOCKS}:${symbol}`, symbol, name: feed?.name ?? null, feed };
      }),
    };
    const others: Row[] = groups
      .filter((g) => g.id !== STOCKS)
      .map((g) => ({
        id: g.id,
        title: g.title,
        source: g.source,
        dayRule: g.day_rule === 'utc' ? 'utc' : 'nyse',
        group: g,
        items: g.instruments.map((i) => ({ id: i.id, symbol: i.symbol, name: i.name, feed: i })),
      }));
    return [stocks, ...others].filter((r) => r.items.length > 0);
  }, [index, isReplay, tables.instruments, tables.bars, historySymbols.data]);

  const items = useMemo(() => rows.flatMap((r) => r.items.map((i) => ({ ...i, row: r }))), [rows]);
  const wanted = marketId(params.symbol);
  const selected = items.find((i) => i.id === wanted) ?? items[0] ?? null;
  const row = selected?.row ?? null;
  const feedInst = selected?.feed ?? null;
  const dayRule: DayRule = row?.dayRule ?? 'nyse';
  const isStock = row?.id === STOCKS;
  const sym = selected?.symbol ?? null;
  const digits = feedInst?.precision ?? 2;
  const hasVolume = feedInst ? feedInst.has_volume : true;

  // The bot's own view of a stock (instrument spec, 5-minute bars, proposals, fills, positions).
  const instrument = isStock ? (tables.instruments.find((i) => symbolOf(i.instrument_id) === sym) ?? null) : null;
  const instrumentId = isStock
    ? (instrument?.instrument_id ?? tables.bars.find((b) => symbolOf(b.instrument_id) === sym)?.instrument_id ?? (sym ? `XNAS:${sym}` : ''))
    : '';
  const hasHistory = isStock && !isReplay && (historySymbols.data ?? []).some((h) => h.symbol === sym && h.dataset.startsWith('alpaca'));
  const history = useHistory(hasHistory ? sym : null, range.limit, range.tf);
  const feedLimit = Math.ceil((range.feedDays[dayRule] * 86400) / TF_SECONDS[range.tf]);
  const feed = useMarketBars(feedInst ? feedInst.id : null, range.tf, feedLimit);
  const barTf: Timeframe = isStock ? range.tf : (feed.data?.barTf ?? range.tf);
  const loading = history.isLoading || feed.isLoading;

  const candles = useMemo<Ohlcv[]>(() => {
    const live =
      isStock && instrumentId
        ? aggregate(
            botCandles(tables, instrumentId).map((b) => ({ t: b.time, o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume })),
            range.tf,
            'nyse',
          )
        : [];
    // Research history, then the feed, then the bot's bars: each later source wins from its first bar.
    return joinCandles([isStock ? (history.data ?? []) : [], feed.data?.bars ?? [], live]);
  }, [tables, instrumentId, isStock, history.data, feed.data, range.tf]);

  const lastCandle = candles.at(-1);
  const visibleFrom =
    dayRule === 'nyse'
      ? range.sessions === null
        ? null
        : sessionsBackStart(candles, range.sessions, 'nyse')
      : range.days === null || !lastCandle
        ? null
        : Math.max(candles[0].t, lastCandle.t + TF_SECONDS[barTf] - range.days * 86400);
  const inRange = useMemo(() => (visibleFrom === null ? candles : candles.filter((c) => c.t >= visibleFrom)), [candles, visibleFrom]);
  const first = inRange[0];
  const last = lastCandle;
  const shown = hovered ?? last;
  const change = shown && first ? (shown.c - first.o) / first.o : null;
  const up = change === null || change >= 0;
  // Daily bars carry no meaningful time of day; UTC-day candles are dated in UTC.
  const when = (t: number) =>
    barTf === '1d' ? (dayRule === 'utc' ? `${utcDay(t)} UTC` : day(t * 1000, 'market')) : `${dayTime(t * 1000, tz)} ${tzLabel(tz)}`;

  const symCandidates = useMemo(() => (isStock ? tables.candidates.filter((c) => c.instrument_id === instrumentId) : []), [tables.candidates, instrumentId, isStock]);
  const decisionsByCand = useMemo(() => new Map(tables.risk_decisions.map((d) => [d.candidate_id, d])), [tables.risk_decisions]);

  const markers = useMemo<ChartMarker[]>(() => {
    const out: ChartMarker[] = [];
    if (!isStock) return out;
    const snap = (t: number) => {
      const i = bucketOf(candles, t);
      return i === null ? null : candles[i].t;
    };
    for (const c of symCandidates) {
      const d = decisionsByCand.get(c.candidate_id);
      const t = snap(Date.parse(c.payload.bar_end ?? c.decision_time) / 1000 - 1);
      if (t !== null) out.push({ time: t, kind: d?.approved ? 'approved' : 'rejected', text: d?.approved ? 'Approved' : '' });
    }
    for (const f of tables.fills.filter((x) => x.instrument_id === instrumentId)) {
      const t = snap(Date.parse(f.exec_time) / 1000);
      if (t !== null) out.push({ time: t, kind: f.side === 'BUY' ? 'buy' : 'sell', text: f.side === 'BUY' ? 'Buy' : 'Sell' });
    }
    return out;
  }, [isStock, symCandidates, decisionsByCand, tables.fills, instrumentId, candles]);

  const lines = useMemo<ChartLine[]>(() => {
    if (!isStock) return [];
    const plan = latestBy(tables.exit_plans.filter((e) => e.instrument_id === instrumentId && e.active), (e) => e.created_at);
    if (!plan) return [];
    const l: ChartLine[] = [{ price: Number(plan.stop_price), kind: 'stop', title: 'Stop' }];
    if (plan.target_price) l.push({ price: Number(plan.target_price), kind: 'target', title: 'Target' });
    return l;
  }, [isStock, tables.exit_plans, instrumentId]);

  const lastBar = isStock ? latestBy(tables.bars.filter((b) => b.instrument_id === instrumentId), (b) => b.start) : undefined;
  const position = isStock ? (tables.positions.find((x) => x.instrument_id === instrumentId && Number(x.quantity) > 0) ?? null) : null;
  const lastCandidate = latestBy(symCandidates, (c) => c.decision_time);
  const lastDecision = lastCandidate ? decisionsByCand.get(lastCandidate.candidate_id) : undefined;
  const rangeHigh = inRange.length ? Math.max(...inRange.map((c) => c.h)) : null;
  const rangeLow = inRange.length ? Math.min(...inRange.map((c) => c.l)) : null;
  const px = (v: number | string | null | undefined) => price(v, digits);

  const isStale = (g: MarketGroup | null) => {
    if (!g || !index) return false;
    const updated = g.updated_at ? Date.parse(g.updated_at) : null;
    return updated === null || now - updated > 2 * index.every_minutes * 60_000;
  };

  return (
    <div className={p.stack}>
      <div className={p.head}>
        <div>
          <h1 className={p.title}>Markets</h1>
          <p className={p.subtitle}>
            {isReplay
              ? 'Prices as the bot sees them, with every proposal and fill marked. Replay builds the forming bar from recorded trades.'
              : 'Stocks, crypto and CFDs from the market feed (refreshed every few minutes). Stock history is IEX 5-minute data (split-adjusted) with the bot’s own bars appended live, and every proposal and fill marked.'}
          </p>
        </div>
      </div>

      {!isReplay && marketIndex.data && !marketIndex.data.available && (
        <Card>
          <div className={p.small}>
            <b>The market feed has not written data/market/ yet.</b>{' '}
            <span className={p.muted}>`npm run dev` starts it beside the console (unless `--no-market`); to fill it once by hand:</span>
          </div>
          <div style={{ marginTop: 10 }}>
            <CopyCommand command="python -m uv run python -m trader.market refresh" />
          </div>
        </Card>
      )}

      {rows.length > 0 && (
        <Card flush className={m.rows}>
          {rows.map((r) => {
            const g = r.group;
            const stale = isStale(g);
            return (
              <section key={r.id} className={m.row} aria-label={r.title}>
                <div className={m.rowHead}>
                  {sleeveOf(r.id) && <img className={m.rowEmblem} src={SLEEVES[sleeveOf(r.id)!].emblem} alt="" width={26} height={26} />}
                  <h2 className={m.rowTitle}>{r.title}</h2>
                  {sleeveView(r.id) && (
                    <Chip title={`The ${SLEEVES[sleeveOf(r.id)!].title} sleeve: ${sleeveView(r.id)!.statusLine}`}>
                      sleeve {usd(sleeveView(r.id)!.equity, { precise: true })}
                    </Chip>
                  )}
                  <span className={m.rowMeta}>
                    {r.source ?? 'History and the bot’s bars'}
                    {g?.updated_at ? ` · updated ${ago(g.updated_at, now)}` : g ? ' · not updated yet' : ''}
                  </span>
                  {g && stale && (
                    <Chip title={`No successful refresh for more than ${2 * (index?.every_minutes ?? 5)} minutes. Is the feed running?`}>
                      <Dot tone="paused" />
                      stale
                    </Chip>
                  )}
                  {g && g.status !== 'ok' && (
                    <Chip title={g.error ?? undefined}>
                      <Dot tone="alert" />
                      {g.status === 'partial' ? 'some failed' : 'feed error'}
                    </Chip>
                  )}
                </div>
                <div className={m.strip} role="tablist" aria-label={`${r.title} instruments`}>
                  {r.items.map((it) => {
                    const active = it.id === selected?.id;
                    return (
                      <button
                        key={it.id}
                        role="tab"
                        aria-selected={active}
                        title={it.name ?? it.symbol}
                        className={`${m.inst} ${active ? m.instActive : ''}`}
                        onClick={() => navigate(`/markets/${it.id}`)}
                      >
                        <span className={m.instSymbol}>{it.symbol}</span>
                        {it.feed?.last_price && <span className={`${m.instPrice} num`}>{price(it.feed.last_price, it.feed.precision)}</span>}
                      </button>
                    );
                  })}
                </div>
                {g && g.status !== 'ok' && g.error && <p className={m.rowError}>{g.error}</p>}
              </section>
            );
          })}
        </Card>
      )}

      {!selected || !sym ? (
        <Card>
          <Empty title="No instruments yet" body="Symbols appear once the market feed has run, the bot loads its instruments, or history is available." />
        </Card>
      ) : (
        <div className={p.marketLayout}>
          <div className={p.stack}>
          <Card flush>
            <div className={m.quote}>
              <div className={m.symbol}>
                {sym}
                {selected.name && <span className={m.name}> · {selected.name}</span>}
              </div>
              <div className={`${m.price} num`}>{shown ? px(shown.c) : '—'}</div>
              <div className={m.change}>
                {change !== null && <Delta value={change} kind="pct" pill />}
                <span className={m.when}>
                  {hovered
                    ? when(hovered.t)
                    : last
                      ? `${range.label[dayRule]} · last bar ${when(last.t)}`
                      : loading
                        ? 'loading prices…'
                        : 'no bars'}
                </span>
              </div>
              {hovered && (
                <div className={m.ohlc}>
                  <span>O <b>{px(hovered.o)}</b></span>
                  <span>H <b>{px(hovered.h)}</b></span>
                  <span>L <b>{px(hovered.l)}</b></span>
                  <span>C <b>{px(hovered.c)}</b></span>
                  {hasVolume && <span>Vol <b>{Intl.NumberFormat('en-US', { notation: 'compact' }).format(hovered.v)}</b></span>}
                </div>
              )}
            </div>
            <div style={{ padding: '0 8px' }}>
              {candles.length ? (
                <MarketChart
                  data={candles}
                  mode={mode}
                  up={up}
                  markers={markers}
                  lines={lines}
                  visibleFrom={visibleFrom}
                  rangeKey={`${status?.id}-${selected.id}-${rangeId}-${mode}`}
                  onHover={setHovered}
                  precision={digits}
                  showVolume={hasVolume}
                />
              ) : (
                <Empty
                  title={loading ? 'Loading…' : 'No bars yet'}
                  body={loading ? undefined : isStock ? 'Bars appear here as soon as the feed or the bot stores them.' : 'Bars appear here after the market feed’s next refresh.'}
                />
              )}
            </div>
            <div className={m.controls}>
              <Segmented label="Range" value={rangeId} onChange={setRangeId} options={RANGES.map((r) => ({ value: r.id, label: r.id }))} />
              <span className={m.tf}>
                {barTf} bars{dayRule === 'utc' ? ' · days cut at 00:00 UTC' : ''}
              </span>
              <Segmented
                label="Chart type"
                value={mode}
                onChange={setMode}
                options={[
                  { value: 'area', label: 'Line' },
                  { value: 'candles', label: 'Candles' },
                ]}
              />
            </div>
            {isStock && (
              <div className={p.legendRow} style={{ padding: '0 24px 18px', marginTop: 0 }}>
                <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--accent)', borderRadius: '50%' }} />Approved proposal</span>
                <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--ink-3)', borderRadius: '50%' }} />Signal not taken</span>
                <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--gain-mark)' }} />Buy</span>
                <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--loss-mark)' }} />Sell</span>
              </div>
            )}
          </Card>
          {!isReplay && <IntelCards id={selected.id} tz={tz} now={now} digits={digits} />}
          </div>
          <div className={p.stack}>
            <Card title="Range">
              <KV
                items={[
                  ['High', rangeHigh !== null ? px(rangeHigh) : '—'],
                  ['Low', rangeLow !== null ? px(rangeLow) : '—'],
                  ['Change', change !== null && !hovered ? pct(change, 2, true) : first && last ? pct((last.c - first.o) / first.o, 2, true) : '—'],
                  ...(isStock ? [['Session VWAP', lastBar?.vwap ? price(lastBar.vwap) : '—'] as [string, string]] : []),
                ]}
              />
            </Card>
            {isStock ? (
              <>
                <Card title="Instrument">
                  <KV
                    items={[
                      ['ID', <code key="i">{instrumentId}</code>],
                      ['Tradable', instrument ? instrument.spec.tradable ? <Chip key="t" strong>certified</Chip> : <Chip key="t">not certified</Chip> : '—'],
                      ['Evidence', instrument?.spec.evidence?.toLowerCase().replaceAll('_', ' ') ?? '—'],
                      ['Min notional', instrument?.spec.min_notional ? `$${instrument.spec.min_notional}` : '—'],
                      ['Prices', [hasHistory && 'IEX history', feedInst && `${row?.source ?? 'market'} feed`, 'the bot’s bars'].filter(Boolean).join(', ')],
                    ]}
                  />
                  {instrument && !instrument.spec.tradable && (
                    <p className={p.small + ' ' + p.muted} style={{ marginTop: 12 }}>
                      Uncertified instruments are rejected with “{reasonTitle('CAPABILITY')}” before any order is sized.
                    </p>
                  )}
                </Card>
                <Card title="Latest proposal">
                  {lastCandidate ? (
                    <KV
                      items={[
                        ['When', `${dayTime(lastCandidate.decision_time, tz)} ${tzLabel(tz)}`],
                        ['Outcome', lastDecision ? (lastDecision.approved ? 'Taken' : `${decisionVerdict(lastDecision).label} · ${decisionVerdict(lastDecision).why}`) : 'Pending'],
                        ['Ref / stop / target', `${price(lastCandidate.payload.reference_price)} / ${price(lastCandidate.payload.stop_price)} / ${price(lastCandidate.payload.target_price)}`],
                      ]}
                    />
                  ) : (
                    <Empty title="No proposals for this symbol" />
                  )}
                </Card>
                <Card title="Position">
                  {position ? (
                    <KV
                      items={[
                        ['Quantity', qty(position.quantity)],
                        ['Average cost', price(Number(position.cost_basis) / Number(position.quantity))],
                        ['Opened', position.opened_at ? `${dayTime(position.opened_at, tz)} ${tzLabel(tz)}` : '—'],
                      ]}
                    />
                  ) : (
                    <Empty title="Not held" />
                  )}
                </Card>
              </>
            ) : (
              feedInst && (
                <Card title="Instrument">
                  <KV
                    items={[
                      ['ID', <code key="i">{feedInst.id}</code>],
                      ['Source', `${row?.source ?? '—'}${feedInst.source_symbol && feedInst.source_symbol !== feedInst.symbol ? ` · ${feedInst.source_symbol}` : ''}`],
                      ['Basis', feedInst.basis === 'mid' ? 'Bid/ask mid' : 'Traded prices'],
                      ['Volume', feedInst.has_volume ? 'Traded volume' : 'None (mid prices)'],
                      ...(feedInst.min_deal ? [['Min deal', feedInst.min_deal] as [string, string]] : []),
                      ['Day', dayRule === 'utc' ? '00:00-24:00 UTC' : 'New York session'],
                      ['Updated', row?.group?.updated_at ? ago(row.group.updated_at, now) : 'never'],
                    ]}
                  />
                  <p className={p.small + ' ' + p.muted} style={{ marginTop: 12 }}>
                    Display only: the bot does not trade this instrument, and nothing here can place an order.
                  </p>
                </Card>
              )
            )}
          </div>
        </div>
      )}
    </div>
  );
}
