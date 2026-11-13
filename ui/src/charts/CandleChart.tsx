import { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useConsole } from '../app/store';
import { alpha, baseOptions, useChartColors } from './chartTheme';

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ChartMarker {
  time: number;
  kind: 'approved' | 'rejected' | 'buy' | 'sell';
  text?: string;
}

export interface ChartLine {
  price: number;
  kind: 'stop' | 'target' | 'reference' | 'limit';
  title: string;
}

/**
 * Candles + a separate volume pane (never a second axis on the same pane). Markers show what the
 * bot proposed and did; dashed lines show live stops and targets. Live ticks update the last bar.
 */
export function CandleChart({
  candles,
  markers = [],
  lines = [],
  tickLine,
  height = 420,
  fitKey,
}: {
  candles: Candle[];
  markers?: ChartMarker[];
  lines?: ChartLine[];
  /** Optional tick-level price path (replay quotes/trades) drawn over the candles. */
  tickLine?: { time: number; value: number }[];
  height?: number;
  /** Changing this refits the visible range (e.g. on symbol change). */
  fitKey?: string;
}) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volSeries = useRef<ISeriesApi<'Histogram'> | null>(null);
  const tickSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const markerApi = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const lastFit = useRef<string | undefined>(undefined);
  const c = useChartColors();
  const tz = useConsole((s) => s.tz);

  useEffect(() => {
    if (!el.current) return;
    const ch = createChart(el.current, baseOptions(c, tz));
    const cs = ch.addSeries(CandlestickSeries, {
      upColor: c['gain-mark'],
      downColor: c['loss-mark'],
      borderVisible: false,
      wickUpColor: alpha(c['gain-mark'], 0.8),
      wickDownColor: alpha(c['loss-mark'], 0.8),
      priceLineVisible: false,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    const ts = ch.addSeries(LineSeries, {
      color: c['ink-2'],
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const vs = ch.addSeries(
      HistogramSeries,
      {
        priceFormat: { type: 'custom', formatter: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}K` : v.toFixed(0)), minMove: 1 },
        priceLineVisible: false,
        lastValueVisible: false,
      },
      1,
    );
    ch.panes()[1]?.setHeight(Math.round(height * 0.18));
    chart.current = ch;
    candleSeries.current = cs;
    volSeries.current = vs;
    tickSeries.current = ts;
    markerApi.current = createSeriesMarkers(cs, []);
    priceLines.current = [];
    lastFit.current = undefined;
    return () => {
      ch.remove();
      chart.current = null;
    };
  }, [c, tz, height]);

  useEffect(() => {
    const cs = candleSeries.current;
    const vs = volSeries.current;
    if (!cs || !vs) return;
    const data = candles
      .slice()
      .sort((a, b) => a.time - b.time)
      .filter((d, i, arr) => i === 0 || d.time !== arr[i - 1].time);
    cs.setData(data.map((d) => ({ time: d.time as UTCTimestamp, open: d.open, high: d.high, low: d.low, close: d.close })));
    vs.setData(
      data.map((d) => ({
        time: d.time as UTCTimestamp,
        value: d.volume,
        color: alpha(c['ink-3'].startsWith('#') ? c['ink-3'] : '#8e8e93', 0.28),
      })),
    );
    if (fitKey !== lastFit.current && data.length) {
      lastFit.current = fitKey;
      const ts = chart.current?.timeScale();
      const n = data.length;
      ts?.setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 4 });
    }
  }, [candles, c, fitKey]);

  useEffect(() => {
    tickSeries.current?.setData(
      (tickLine ?? [])
        .filter((p, i, arr) => i === 0 || Math.floor(p.time) !== Math.floor(arr[i - 1].time))
        .map((p) => ({ time: Math.floor(p.time) as UTCTimestamp, value: p.value })),
    );
  }, [tickLine]);

  useEffect(() => {
    const m: SeriesMarker<Time>[] = markers
      .slice()
      .sort((a, b) => a.time - b.time)
      .map((mk) => {
        const time = mk.time as UTCTimestamp;
        switch (mk.kind) {
          case 'approved':
            return { time, position: 'belowBar', shape: 'circle', color: c.accent, text: mk.text ?? 'Approved', size: 1 };
          case 'rejected':
            return { time, position: 'aboveBar', shape: 'circle', color: c['ink-3'], text: mk.text ?? '', size: 0.6 };
          case 'buy':
            return { time, position: 'belowBar', shape: 'arrowUp', color: c['gain-mark'], text: mk.text ?? 'Buy', size: 1 };
          default:
            return { time, position: 'aboveBar', shape: 'arrowDown', color: c['loss-mark'], text: mk.text ?? 'Sell', size: 1 };
        }
      });
    markerApi.current?.setMarkers(m);
  }, [markers, c]);

  useEffect(() => {
    const cs = candleSeries.current;
    if (!cs) return;
    for (const pl of priceLines.current) cs.removePriceLine(pl);
    priceLines.current = lines.map((l) =>
      cs.createPriceLine({
        price: l.price,
        color: l.kind === 'stop' ? c['loss-mark'] : l.kind === 'target' ? c['gain-mark'] : c['ink-3'],
        lineWidth: 1,
        lineStyle: l.kind === 'reference' ? LineStyle.Dotted : LineStyle.Dashed,
        axisLabelVisible: true,
        title: l.title,
      }),
    );
  }, [lines, c]);

  return <div ref={el} style={{ height, width: '100%' }} />;
}
