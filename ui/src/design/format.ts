import Decimal from 'decimal.js';
import type { Dec } from '../../shared/schema';

export type Tz = 'market' | 'local';
export const MARKET_TZ = 'America/New_York';

const nf = (opts: Intl.NumberFormatOptions) => new Intl.NumberFormat('en-US', opts);

/** USD with cents; sub-cent amounts keep up to 4 decimals so a $0.0049 P&L is not shown as $0.00. */
export function usd(v: Dec | number | null | undefined, opts: { sign?: boolean; precise?: boolean } = {}): string {
  if (v === null || v === undefined) return '—';
  const d = new Decimal(v);
  const abs = d.abs();
  const decimals = opts.precise ? (abs.gt(0) && abs.lt(1) ? 4 : 2) : abs.gt(0) && abs.lt(0.01) ? 4 : 2;
  const s = nf({ minimumFractionDigits: 2, maximumFractionDigits: decimals }).format(abs.toNumber());
  const sign = d.isNeg() && !d.isZero() ? '−' : opts.sign && d.gt(0) ? '+' : '';
  return `${sign}$${s}`;
}

/** A price: 2-4 decimals, or exactly `digits` (an instrument's display precision, 0-8). */
export function price(v: Dec | number | null | undefined, digits?: number): string {
  if (v === null || v === undefined) return '—';
  if (digits !== undefined) {
    const d = Math.max(0, Math.min(8, Math.round(digits)));
    return nf({ minimumFractionDigits: d, maximumFractionDigits: d }).format(Number(v));
  }
  return nf({ minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(Number(v));
}

/** Quantities: fractional shares keep their precision (up to 6 places shown). */
export function qty(v: Dec | null | undefined): string {
  if (v === null || v === undefined) return '—';
  const d = new Decimal(v);
  return nf({ maximumFractionDigits: d.abs().lt(1) ? 6 : 4 }).format(d.toNumber());
}

export function pct(fraction: number | Dec | null | undefined, digits = 1, sign = false): string {
  if (fraction === null || fraction === undefined) return '—';
  const n = Number(fraction) * 100;
  const s = nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Math.abs(n));
  const sg = n < 0 && Math.abs(n) >= 0.5 * 10 ** -digits ? '−' : sign && n > 0 ? '+' : '';
  return `${sg}${s}%`;
}

export const compact = (n: number) => nf({ notation: 'compact', maximumFractionDigits: 1 }).format(n);
export const int = (n: number) => nf({ maximumFractionDigits: 0 }).format(n);

const tzName = (tz: Tz) => (tz === 'market' ? MARKET_TZ : undefined);

export function clock(iso: string | number | Date | null | undefined, tz: Tz, seconds = false): string {
  if (iso === null || iso === undefined) return '—';
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: seconds ? '2-digit' : undefined,
    hour12: false,
    timeZone: tzName(tz),
  }).format(new Date(iso));
}

export function dayTime(iso: string | number | Date | null | undefined, tz: Tz): string {
  if (iso === null || iso === undefined) return '—';
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: tzName(tz),
  }).format(new Date(iso));
}

export function day(iso: string | number | Date | null | undefined, tz: Tz = 'local'): string {
  if (iso === null || iso === undefined) return '—';
  return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: tzName(tz) }).format(
    new Date(iso),
  );
}

export const tzLabel = (tz: Tz) =>
  tz === 'market'
    ? 'ET'
    : (new Intl.DateTimeFormat('en-GB', { timeZoneName: 'short' }).formatToParts(new Date()).find((p) => p.type === 'timeZoneName')
        ?.value ?? 'local');

export function ago(iso: string | number | null | undefined, now: number): string {
  if (iso === null || iso === undefined) return 'never';
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min`;
  return `${s}s`;
}

export const shortHash = (h: string | null | undefined, n = 10) => (h ? h.slice(0, n) : '—');

export const sign = (v: Dec | number | null | undefined): -1 | 0 | 1 => {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return n > 0 ? 1 : n < 0 ? -1 : 0;
};
