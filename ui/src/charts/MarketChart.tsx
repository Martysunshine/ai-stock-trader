/**
 * The Markets chart, Trading 212 style: an area line by default (candles on request), coloured by
 * whether the range is up or down, with the headline price following the crosshair. Volume lives in
 * its own pane (never a second axis on the price pane), and is left out where the source has none
 * (CFD mid prices). `precision` sets the price-axis decimals (2-6).
 */
import { useEffect, useRef } from 'react';
import {
  AreaSeries,
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineStyle,
  LineType,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import type { Ohlcv } from '../../shared/aggregate';
import { useConsole } from '../app/store';
import type { ChartLine, ChartMarker } from './CandleChart';
import { alpha, baseOptions, useChartColors } from './chartTheme';

export function MarketChart({
  data,
  mode,
  up,
  markers,
  lines,
  visibleFrom,
  visibleTo = null,
  fitLines = false,
  rangeKey,
  onHover,
  height = 440,
  precision = 2,
  showVolume = true,
}: {
  data: Ohlcv[];
  mode: 'area' | 'candles';
  /** Range direction: colours the area line. */
  up: boolean;
  markers: ChartMarker[];
  lines: ChartLine[];
  /** Left edge of the visible range (seconds); null fits everything. */
  visibleFrom: number | null;
  /** Right edge of the visible range (seconds); null runs to the newest bar. */
  visibleTo?: number | null;
  /** True widens the price scale so every line (stop, target, entry) stays on screen. */
  fitLines?: boolean;
  /** Changing this re-applies the visible range (symbol or range switch). */
  rangeKey: string;
  onHover: (bar: Ohlcv | null) => void;
  height?: number;
  /** Price decimals on the axis and crosshair, clamped to 2-6. */
  precision?: number;
  /** False hides the volume pane (sources without traded volume). */
  showVolume?: boolean;
}) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const main = useRef<ISeriesApi<'Area'> | ISeriesApi<'Candlestick'> | null>(null);
  const vol = useRef<ISeriesApi<'Histogram'> | null>(null);
  const markerApi = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const byTime = useRef(new Map<number, Ohlcv>());
  const lastRange = useRef<string>('');
  const hover = useRef(onHover);
  hover.current = onHover;
  const c = useChartColors();
  const tz = useConsole((s) => s.tz);
  const lineColor = up ? c['gain-mark'] : c['loss-mark'];
  const digits = Math.max(2, Math.min(6, Math.round(precision)));
  const priceFormat = { type: 'price' as const, precision: digits, minMove: 10 ** -digits };

  useEffect(() => {
    if (!el.current) return;
    const ch = createChart(el.current, baseOptions(c, tz));
    ch.applyOptions({ rightPriceScale: { scaleMargins: { top: 0.14, bottom: 0.06 } }, timeScale: { rightOffset: 6 } });
    const series =
      mode === 'area'
        ? ch.addSeries(AreaSeries, {
            lineColor,
            topColor: alpha(lineColor, 0.18),
            bottomColor: alpha(lineColor, 0.01),
            lineWidth: 2,
            lineType: LineType.Simple,
            priceLineVisible: true,
            priceLineStyle: LineStyle.Dotted,
            priceLineColor: c['chart-axis'],
            crosshairMarkerRadius: 5,
            crosshairMarkerBorderColor: c.surface,
            crosshairMarkerBorderWidth: 2,
            priceFormat,
          })
        : ch.addSeries(CandlestickSeries, {
            upColor: c['gain-mark'],
            downColor: c['loss-mark'],
            borderVisible: false,
            wickUpColor: alpha(c['gain-mark'], 0.8),
            wickDownColor: alpha(c['loss-mark'], 0.8),
            priceLineVisible: true,
            priceLineStyle: LineStyle.Dotted,
            priceFormat,
          });
    const vs = showVolume
      ? ch.addSeries(
          HistogramSeries,
          {
            priceFormat: { type: 'custom', formatter: (v: number) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1000 ? `${(v / 1000).toFixed(0)}K` : v.toFixed(0)), minMove: 1 },
            priceLineVisible: false,
            lastValueVisible: false,
          },
          1,
        )
      : null;
    if (vs) ch.panes()[1]?.setHeight(Math.round(height * 0.16));
    ch.subscribeCrosshairMove((p: MouseEventParams<Time>) => {
      if (!p.time || !p.point || p.point.x < 0) return hover.current(null);
      hover.current(byTime.current.get(p.time as number) ?? null);
    });
    chart.current = ch;
    main.current = series;
    vol.current = vs;
    markerApi.current = createSeriesMarkers(series as ISeriesApi<'Area'>, []);
    priceLines.current = [];
    lastRange.current = '';
    return () => {
      ch.remove();
      chart.current = null;
    };
    // lineColor is applied below without recreating the chart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [c, tz, mode, height, digits, showVolume]);

  useEffect(() => {
    if (mode === 'area') (main.current as ISeriesApi<'Area'> | null)?.applyOptions({ lineColor, topColor: alpha(lineColor, 0.18), bottomColor: alpha(lineColor, 0.01) });
  }, [lineColor, mode, digits, showVolume]);

  useEffect(() => {
    const s = main.current;
    if (!s) return;
    byTime.current = new Map(data.map((d) => [d.t, d]));
    if (mode === 'area') (s as ISeriesApi<'Area'>).setData(data.map((d) => ({ time: d.t as UTCTimestamp, value: d.c })));
    else (s as ISeriesApi<'Candlestick'>).setData(data.map((d) => ({ time: d.t as UTCTimestamp, open: d.o, high: d.h, low: d.l, close: d.c })));
    vol.current?.setData(data.map((d) => ({ time: d.t as UTCTimestamp, value: d.v, color: alpha(c['ink-3'].startsWith('#') ? c['ink-3'] : '#8e8e93', 0.3) })));
    if (rangeKey !== lastRange.current && data.length) {
      lastRange.current = rangeKey;
      const ts = chart.current?.timeScale();
      if (visibleFrom === null) ts?.fitContent();
      else ts?.setVisibleRange({ from: visibleFrom as UTCTimestamp, to: Math.min(visibleTo ?? Infinity, data[data.length - 1].t) as UTCTimestamp });
    }
  }, [data, mode, c, rangeKey, visibleFrom, visibleTo, digits, showVolume]);

  useEffect(() => {
    const m: SeriesMarker<Time>[] = [...markers]
      .sort((a, b) => a.time - b.time)
      .map((mk) => {
        const time = mk.time as UTCTimestamp;
        if (mk.kind === 'approved') return { time, position: 'belowBar', shape: 'circle', color: c.accent, text: mk.text ?? '', size: 1 };
        if (mk.kind === 'rejected') return { time, position: 'aboveBar', shape: 'circle', color: c['ink-3'], text: mk.text ?? '', size: 0.6 };
        if (mk.kind === 'buy') return { time, position: 'belowBar', shape: 'arrowUp', color: c['gain-mark'], text: mk.text ?? 'Buy', size: 1 };
        return { time, position: 'aboveBar', shape: 'arrowDown', color: c['loss-mark'], text: mk.text ?? 'Sell', size: 1 };
      });
    markerApi.current?.setMarkers(m);
  }, [markers, c, mode, digits, showVolume]);

  useEffect(() => {
    const s = main.current;
    if (!s) return;
    for (const pl of priceLines.current) s.removePriceLine(pl);
    priceLines.current = lines.map((l) =>
      s.createPriceLine({
        price: l.price,
        color: l.kind === 'stop' ? c['loss-mark'] : l.kind === 'target' ? c['gain-mark'] : c['ink-3'],
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: l.title,
      }),
    );
    // Lines outside the bars' own range (a target not reached, a distant stop) stay visible.
    const prices = fitLines ? lines.map((l) => l.price) : [];
    s.applyOptions({
      autoscaleInfoProvider: prices.length
        ? (original: () => { priceRange: { minValue: number; maxValue: number } | null } | null) => {
            const res = original();
            if (!res || !res.priceRange) return res;
            const lo = Math.min(res.priceRange.minValue, ...prices);
            const hi = Math.max(res.priceRange.maxValue, ...prices);
            const pad = (hi - lo) * 0.04;
            return { ...res, priceRange: { minValue: lo - pad, maxValue: hi + pad } };
          }
        : undefined,
    });
  }, [lines, fitLines, c, mode, digits, showVolume]);

  return <div ref={el} style={{ height, width: '100%' }} />;
}
