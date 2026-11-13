import type { Tables } from '../../../shared/schema';
import type { Candle } from '../../charts/CandleChart';

const BAR = 300; // seconds

/**
 * Completed bars from the bot's `bars` table, plus the bar currently forming built from ticks
 * (replay journals quotes and trades; live paper does not store quotes, so live shows completed
 * bars only and never invents an in-progress candle).
 */
export function botCandles(t: Tables, instrumentId: string): Candle[] {
  const bars = t.bars
    .filter((b) => b.instrument_id === instrumentId && b.minutes === 5)
    .map((b) => ({
      time: Date.parse(b.start) / 1000,
      open: Number(b.open),
      high: Number(b.high),
      low: Number(b.low),
      close: Number(b.close),
      volume: Number(b.volume),
    }));
  const lastEnd = bars.length ? bars[bars.length - 1].time + BAR : 0;
  const trades = t.ticks.filter((k) => k.instrument_id === instrumentId && k.payload.price && Date.parse(k.event_time) / 1000 >= lastEnd);
  if (trades.length) {
    const forming = new Map<number, Candle>();
    for (const k of trades) {
      const ts = Date.parse(k.event_time) / 1000;
      const start = ts - (ts % BAR);
      const px = Number(k.payload.price);
      const size = Number(k.payload.size ?? 0);
      const c = forming.get(start);
      if (!c) forming.set(start, { time: start, open: px, high: px, low: px, close: px, volume: size });
      else {
        c.high = Math.max(c.high, px);
        c.low = Math.min(c.low, px);
        c.close = px;
        c.volume += size;
      }
    }
    bars.push(...[...forming.values()].sort((a, b) => a.time - b.time));
  }
  return bars;
}

export function candlesAround(t: Tables, instrumentId: string, from: number, to: number): Candle[] {
  return botCandles(t, instrumentId).filter((c) => c.time >= from && c.time <= to);
}
