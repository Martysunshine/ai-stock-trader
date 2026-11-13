import { useEffect, useState } from 'react';
import { ColorType, CrosshairMode, type DeepPartial, type ChartOptions, type Time, TickMarkType } from 'lightweight-charts';
import { MARKET_TZ, type Tz } from '../design/format';

const VARS = [
  'ink',
  'ink-2',
  'ink-3',
  'surface',
  'chart-grid',
  'chart-axis',
  'accent',
  'gain-mark',
  'loss-mark',
  'gain-wash',
  'loss-wash',
  'series-1',
  'series-2',
  'series-3',
  'series-4',
  'warning',
  'critical',
  'hairline',
  'fill',
] as const;
export type ChartColors = Record<(typeof VARS)[number], string>;

function read(): ChartColors {
  const cs = getComputedStyle(document.documentElement);
  return Object.fromEntries(VARS.map((v) => [v, cs.getPropertyValue(`--${v}`).trim()])) as ChartColors;
}

/** Resolved design tokens; updates when the theme toggle or the OS appearance changes. */
export function useChartColors(): ChartColors {
  const [c, setC] = useState<ChartColors>(read);
  useEffect(() => {
    const update = () => setC(read());
    const mo = new MutationObserver(update);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const mq = matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', update);
    return () => {
      mo.disconnect();
      mq.removeEventListener('change', update);
    };
  }, []);
  return c;
}

const fmt = (tz: Tz, o: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat('en-GB', { ...o, hour12: false, timeZone: tz === 'market' ? MARKET_TZ : undefined });

export function baseOptions(c: ChartColors, tz: Tz): DeepPartial<ChartOptions> {
  const time = fmt(tz, { hour: '2-digit', minute: '2-digit' });
  const dayMonth = fmt(tz, { day: 'numeric', month: 'short' });
  const month = fmt(tz, { month: 'short' });
  const year = fmt(tz, { year: 'numeric' });
  const full = fmt(tz, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const toDate = (t: Time) => new Date((t as number) * 1000);
  return {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: c['ink-3'],
      fontFamily: getComputedStyle(document.body).fontFamily,
      fontSize: 11,
      attributionLogo: false,
      panes: { separatorColor: c['chart-grid'], separatorHoverColor: c['chart-axis'], enableResize: false },
    },
    grid: { vertLines: { visible: false }, horzLines: { color: c['chart-grid'] } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
    timeScale: {
      borderVisible: false,
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: false,
      rightOffset: 4,
      tickMarkFormatter: (t: Time, type: TickMarkType) => {
        const d = toDate(t);
        if (type === TickMarkType.Year) return year.format(d);
        if (type === TickMarkType.Month) return month.format(d);
        if (type === TickMarkType.DayOfMonth) return dayMonth.format(d);
        return time.format(d);
      },
    },
    // Price formats are set per series (candles 2 dp, volume compact), never chart-wide.
    localization: {
      timeFormatter: (t: Time) => full.format(toDate(t)),
    },
    crosshair: {
      mode: CrosshairMode.Magnet,
      vertLine: { color: c['chart-axis'], width: 1, style: 0, labelBackgroundColor: c['ink'] },
      horzLine: { color: c['chart-axis'], width: 1, style: 0, labelBackgroundColor: c['ink'] },
    },
    handleScale: { axisPressedMouseMove: { time: true, price: false } },
  };
}

/** "rgb"/"#hex" + alpha -> rgba(); lightweight-charts needs explicit colours, not CSS variables. */
export function alpha(color: string, a: number): string {
  if (color.startsWith('#')) {
    const h = color.length === 4 ? color.slice(1).split('').map((x) => x + x).join('') : color.slice(1, 7);
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  return color;
}
