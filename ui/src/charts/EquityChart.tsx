import { useEffect, useRef } from 'react';
import {
  BaselineSeries,
  createChart,
  LineType,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useConsole } from '../app/store';
import { alpha, baseOptions, useChartColors } from './chartTheme';

export interface Point {
  time: number;
  value: number;
}

/**
 * Equity over time as a step line: snapshots are written only when equity changes, so the value
 * holds until the next row (an honest step, never a smoothed guess). Green above the baseline
 * (starting capital), red below; the hero figure and its signed delta carry the same message.
 */
export function EquityChart({ points, baseline, height = 260 }: { points: Point[]; baseline: number; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<'Baseline'> | null>(null);
  const lastCount = useRef(0);
  const c = useChartColors();
  const tz = useConsole((s) => s.tz);

  useEffect(() => {
    if (!el.current) return;
    const ch = createChart(el.current, baseOptions(c, tz));
    ch.applyOptions({ rightPriceScale: { scaleMargins: { top: 0.18, bottom: 0.12 } } });
    const s = ch.addSeries(BaselineSeries, {
      baseValue: { type: 'price', price: baseline },
      lineWidth: 2,
      lineType: LineType.WithSteps,
      topLineColor: c['gain-mark'],
      topFillColor1: alpha(c['gain-mark'], 0.16),
      topFillColor2: alpha(c['gain-mark'], 0.02),
      bottomLineColor: c['loss-mark'],
      bottomFillColor1: alpha(c['loss-mark'], 0.02),
      bottomFillColor2: alpha(c['loss-mark'], 0.16),
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerRadius: 4,
      crosshairMarkerBorderColor: c.surface,
      crosshairMarkerBorderWidth: 2,
    });
    s.createPriceLine({
      price: baseline,
      color: c['chart-axis'],
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      axisLabelVisible: false,
      title: '',
    });
    chart.current = ch;
    series.current = s;
    lastCount.current = 0;
    return () => {
      ch.remove();
      chart.current = null;
      series.current = null;
    };
  }, [c, tz, baseline]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    const data = dedupe(points);
    // New points at the end animate in through update(); anything else is a full reset.
    if (lastCount.current > 0 && data.length === lastCount.current + 1) {
      s.update(data[data.length - 1]);
    } else {
      s.setData(data);
      chart.current?.timeScale().fitContent();
    }
    lastCount.current = data.length;
  }, [points]);

  return <div ref={el} style={{ height, width: '100%' }} />;
}

function dedupe(points: Point[]) {
  const m = new Map<number, number>();
  for (const p of points) m.set(Math.floor(p.time), p.value);
  return [...m.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ time: t as UTCTimestamp, value: v }));
}
