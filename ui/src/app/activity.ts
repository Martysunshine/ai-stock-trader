/**
 * One chronological feed of what the bot did, derived from the rows on screen. Feeds the Activity
 * panel, the toasts and desktop notifications. Every item links back to where it can be inspected.
 */
import Decimal from 'decimal.js';
import { symbolOf } from '../../shared/derive';
import { decisionVerdict } from '../../shared/verdict';
import type { Tables } from '../../shared/schema';
import { price, qty, usd } from '../design/format';
import { ALERTS, DISPOSITIONS, ORDER_STATES, PAUSE_CAUSES, PURPOSES } from '../design/language';

export type ActivityTone = 'neutral' | 'accent' | 'gain' | 'loss' | 'warning' | 'critical' | 'good';
export type ActivityGroup = 'trading' | 'health';

export interface Activity {
  id: string;
  at: string;
  group: ActivityGroup;
  tone: ActivityTone;
  /** Short label for the icon/kind column. */
  kind: 'decision' | 'order' | 'broker' | 'fill' | 'pause' | 'resume' | 'alert' | 'recon' | 'loss' | 'flow';
  title: string;
  detail: string;
  link?: string;
  /** Worth a toast / notification when it arrives live. */
  notable: boolean;
}

export function deriveActivity(t: Tables): Activity[] {
  const out: Activity[] = [];
  const candidates = new Map(t.candidates.map((c) => [c.candidate_id, c]));

  for (const d of t.risk_decisions) {
    const c = candidates.get(d.candidate_id);
    const sym = symbolOf(c?.instrument_id ?? '?');
    const v = decisionVerdict(d);
    out.push({
      id: `d:${d.decision_id}`,
      at: d.decided_at,
      group: 'trading',
      tone: v.verdict === 'taken' ? 'accent' : v.verdict === 'blocked' ? 'warning' : 'neutral',
      kind: 'decision',
      title: `${sym} ${v.label.toLowerCase()}`,
      detail:
        v.verdict === 'taken'
          ? `${qty(d.payload.quantity ?? '0')} sh · ${usd(d.payload.notional_usd ?? '0')} notional · planned loss ${usd(d.payload.planned_loss_usd ?? '0', { precise: true })}`
          : v.why,
      link: `/decisions?c=${encodeURIComponent(d.candidate_id)}`,
      // A skipped signal is routine (the same signal repeats every bar): no toast, no notification.
      notable: v.verdict !== 'skipped',
    });
  }

  for (const i of t.order_intents) {
    const sym = symbolOf(i.instrument_id);
    out.push({
      id: `i:${i.intent_id}`,
      at: i.created_at,
      group: 'trading',
      tone: 'neutral',
      kind: 'order',
      title: `${PURPOSES[i.purpose] ?? i.purpose} order · ${sym}`,
      detail: `${i.side === 'BUY' ? 'Buy' : 'Sell'} ${qty(i.quantity)}${i.limit_price ? ` @ ${price(i.limit_price)}` : ''} · ${ORDER_STATES[i.state] ?? i.state}`,
      link: i.candidate_id ? `/decisions?c=${encodeURIComponent(i.candidate_id)}` : undefined,
      notable: false,
    });
  }

  for (const e of t.broker_order_events) {
    const intent = t.order_intents.find((i) => i.client_order_id === e.client_order_id);
    // Duplicates and stale updates are the bot's de-duplication working; say so instead of
    // presenting them as new fills.
    const ignored = e.disposition === 'FILL_DUPLICATE' || e.disposition === 'STALE' ? ` · ${DISPOSITIONS[e.disposition]}` : '';
    out.push({
      id: `b:${e.id}`,
      at: e.event_time,
      group: 'trading',
      tone: e.status === 'REJECTED' || e.disposition === 'UNSOLICITED' ? 'critical' : 'neutral',
      kind: 'broker',
      title: `Broker: ${e.status.toLowerCase().replaceAll('_', ' ')}`,
      detail: `${intent ? symbolOf(intent.instrument_id) + ' · ' : ''}filled ${qty(e.cum_filled_qty)} so far${ignored}${e.disposition === 'UNSOLICITED' ? ' · unsolicited' : ''}`,
      link: intent?.candidate_id ? `/decisions?c=${encodeURIComponent(intent.candidate_id)}` : undefined,
      notable: e.status === 'REJECTED',
    });
  }

  for (const f of t.fills) {
    const sym = symbolOf(f.instrument_id);
    const realized = f.realized_net !== null ? ` · realised ${usd(f.realized_net, { sign: true, precise: true })}` : '';
    const intent = t.order_intents.find((i) => i.intent_id === f.intent_id);
    const entry = intent?.parent_intent_id ? t.order_intents.find((i) => i.intent_id === intent.parent_intent_id) : intent;
    out.push({
      id: `f:${f.id}`,
      at: f.exec_time,
      group: 'trading',
      tone: f.side === 'BUY' ? 'accent' : f.realized_net !== null && new Decimal(f.realized_net).lt(0) ? 'loss' : 'gain',
      kind: 'fill',
      title: `${f.side === 'BUY' ? 'Bought' : 'Sold'} ${sym}`,
      detail: `${qty(f.quantity)} @ ${price(f.price)} · fee ${usd(f.fee, { precise: true })}${realized}`,
      link: entry?.candidate_id ? `/decisions?c=${encodeURIComponent(entry.candidate_id)}` : '/money',
      notable: true,
    });
  }

  for (const l of t.ledger_entries) {
    if (!l.kind.startsWith('FLOW_') && l.kind !== 'FEE_ADJUSTMENT') continue;
    out.push({
      id: `l:${l.id}`,
      at: l.entry_time,
      group: 'trading',
      tone: 'neutral',
      kind: 'flow',
      title: l.kind === 'FLOW_INITIAL_CAPITAL' ? 'Initial capital booked' : l.kind.replace('FLOW_', '').replaceAll('_', ' ').toLowerCase(),
      detail: usd(l.cash_delta, { sign: true, precise: true }),
      link: '/money',
      notable: false,
    });
  }

  for (const p of t.pause_state) {
    out.push({
      id: `p:${p.flag_id}`,
      at: p.raised_at,
      group: 'health',
      tone: p.category === 'LOSS_BREACH' ? 'critical' : 'warning',
      kind: 'pause',
      title: 'Entries paused',
      detail: PAUSE_CAUSES[p.cause] ?? p.cause,
      link: '/risk',
      notable: true,
    });
    if (p.cleared_at) {
      out.push({
        id: `pc:${p.flag_id}`,
        at: p.cleared_at,
        group: 'health',
        tone: 'good',
        kind: 'resume',
        title: 'Pause cleared',
        detail: `${PAUSE_CAUSES[p.cause] ?? p.cause} · by ${p.cleared_by ?? 'the bot'}`,
        link: '/risk',
        notable: true,
      });
    }
  }

  for (const a of t.alerts) {
    out.push({
      id: `a:${a.id}`,
      at: a.raised_at,
      group: 'health',
      tone: 'critical',
      kind: 'alert',
      title: ALERTS[a.code] ?? a.code,
      detail: a.message,
      link: '/risk',
      notable: true,
    });
  }

  for (const w of t.loss_windows) {
    if (!w.breached_at) continue;
    out.push({
      id: `w:${w.scope}:${w.kind}:${w.window_key}`,
      at: w.breached_at,
      group: 'health',
      tone: 'critical',
      kind: 'loss',
      title: 'Loss limit breached',
      detail: `${w.kind.replace('_', ' ').toLowerCase()} window ${w.window_key}`,
      link: '/risk',
      notable: true,
    });
  }

  // Reconciliation: every failure, and the first pass after one (recovery), not the 390 routine passes.
  let previousFailed = false;
  for (const r of t.reconciliation_runs) {
    const failed = r.status === 'FAILED';
    if (failed || previousFailed) {
      out.push({
        id: `r:${r.run_id}`,
        at: r.finished_at,
        group: 'health',
        tone: failed ? 'critical' : 'good',
        kind: 'recon',
        title: failed ? 'Reconciliation failed' : 'Reconciliation passed again',
        detail: failed ? (r.summary.problems?.join('; ') ?? 'see Risk & health') : 'Ledger agrees with the broker',
        link: '/risk',
        notable: true,
      });
    }
    previousFailed = failed;
  }

  return out.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : a.id < b.id ? 1 : -1));
}
