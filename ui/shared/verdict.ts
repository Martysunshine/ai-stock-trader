/**
 * What a risk decision means to a person. The bot records "approved" or "rejected" with reason
 * codes; three different things hide behind "rejected":
 *
 * - taken: an entry was approved and sent.
 * - skipped: a normal outcome. The signal repeated while the stock is already held, both position
 *   slots are in use, today's trades are used, the spread is too wide, costs exceed the gain.
 * - blocked: the system, not the market, stopped it (pause, stale or mismatched data, a missing
 *   capability, mode, product, costs unknown, account rule, funds). This one needs attention; the
 *   bot also raises an ENTRIES_BLOCKED alert (runtime/session_watch.py uses the same list).
 */
import { reasonsOf } from './derive';
import type { DecisionRow } from './schema';

export type Verdict = 'taken' | 'skipped' | 'blocked';

export interface DecisionVerdict {
  verdict: Verdict;
  /** Taken, Skipped or Blocked. */
  label: string;
  /** One plain sentence fragment: why. Empty for a taken entry. */
  why: string;
}

/** Reasons that mean the system could not trade, whatever the market did. */
export const OPERATIONAL_REASONS: ReadonlySet<string> = new Set([
  'PAUSED',
  'STALE_DATA',
  'FEED_MISMATCH',
  'CAPABILITY',
  'LEADERSHIP',
  'MODE_NOT_PERMITTED',
  'FORBIDDEN_PRODUCT',
  'UNKNOWN_COSTS',
  'ACCOUNT_RULE',
  'INSUFFICIENT_FUNDS',
]);

const BLOCKED_WHY: Record<string, string> = {
  PAUSED: 'new entries are paused',
  STALE_DATA: 'the price data was stale',
  FEED_MISMATCH: 'the quote and the signal came from different feeds',
  CAPABILITY: 'this order type is not certified at the broker',
  LEADERSHIP: 'this worker does not hold the account lock',
  MODE_NOT_PERMITTED: 'not allowed in this mode',
  FORBIDDEN_PRODUCT: 'this product is not allowed',
  UNKNOWN_COSTS: 'the costs are unknown',
  ACCOUNT_RULE: 'an account rule forbids it',
  INSUFFICIENT_FUNDS: 'not enough free cash',
};

/** Detail text the risk engine writes, most specific first, and what it means. */
const SKIPPED_BY_DETAIL: [RegExp, string][] = [
  [/already exists in this instrument/, 'already in this stock'],
  [/max open positions reached/, 'every position slot is in use'],
  [/a pending entry already exists/, 'another order is still being placed'],
  [/max new entries per session reached/, "today's trades are used"],
];

const SKIPPED_BY_REASON: [string, string][] = [
  ['SPREAD_TOO_WIDE', 'the spread is too wide right now'],
  ['TOO_COSTLY', 'costs would be more than the expected gain'],
  ['NO_EDGE', 'no proven edge for this signal'],
  ['OUTSIDE_ENTRY_WINDOW', 'outside the hours for new entries'],
  ['SIZE_BELOW_MINIMUM', "the size would be below the broker's minimum"],
  ['NO_EXIT_PLAN', 'no valid exit plan'],
  ['ENTRY_LIMIT_REACHED', 'an entry limit is used'],
  ['RISK_LIMIT', 'a risk limit is used'],
];

export function decisionVerdict(d: DecisionRow): DecisionVerdict {
  if (d.approved) return { verdict: 'taken', label: 'Taken', why: '' };
  const reasons = reasonsOf(d);
  const blocked = reasons.filter((r) => OPERATIONAL_REASONS.has(r));
  if (blocked.length) {
    return { verdict: 'blocked', label: 'Blocked', why: blocked.map((r) => BLOCKED_WHY[r] ?? r.toLowerCase().replaceAll('_', ' ')).join('; ') };
  }
  const details = (d.payload?.details ?? []).join(' | ');
  for (const [pattern, why] of SKIPPED_BY_DETAIL) if (pattern.test(details)) return { verdict: 'skipped', label: 'Skipped', why };
  for (const [reason, why] of SKIPPED_BY_REASON) if (reasons.includes(reason)) return { verdict: 'skipped', label: 'Skipped', why };
  return { verdict: 'skipped', label: 'Skipped', why: reasons.map((r) => r.toLowerCase().replaceAll('_', ' ')).join(', ') || 'no reason recorded' };
}

export interface VerdictCounts {
  taken: number;
  skipped: number;
  blocked: number;
  /** Skipped decisions by plain reason, most frequent first. */
  skippedWhy: [string, number][];
}

export function verdictCounts(decisions: DecisionRow[]): VerdictCounts {
  const out: VerdictCounts = { taken: 0, skipped: 0, blocked: 0, skippedWhy: [] };
  const why = new Map<string, number>();
  for (const d of decisions) {
    const v = decisionVerdict(d);
    out[v.verdict] += 1;
    if (v.verdict === 'skipped') why.set(v.why, (why.get(v.why) ?? 0) + 1);
  }
  out.skippedWhy = [...why.entries()].sort((a, b) => b[1] - a[1]);
  return out;
}
