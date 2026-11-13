/**
 * The mission (ADR-0046): three sleeves of USD 100 - stocks, crypto, gold & CFDs - compounding
 * toward USD 10,000 with no new money. Shapes shared by the server (which reads the practice
 * journals) and the pages (which add the stocks sleeve from the bot's own tables).
 */
export type SleeveId = 'stocks' | 'crypto' | 'cfd';

export interface SleeveMeta {
  id: SleeveId;
  title: string;
  venue: string;
  /** Who places the trades. */
  trader: string;
  emblem: string;
  /** CSS colour used for this sleeve everywhere: bar segment, sparkline, row marker. */
  color: string;
  /** Where to look for this sleeve's detail. */
  to: string;
  toLabel: string;
}

export const SLEEVES: Record<SleeveId, SleeveMeta> = {
  stocks: {
    id: 'stocks',
    title: 'Stocks',
    venue: 'Alpaca paper',
    trader: 'the bot',
    emblem: '/assets/sleeve-stocks.webp',
    color: 'var(--sleeve-stocks)',
    to: '/decisions',
    toLabel: 'Decisions',
  },
  crypto: {
    id: 'crypto',
    title: 'Crypto',
    venue: 'Capital.com demo',
    trader: 'the practice trader',
    emblem: '/assets/sleeve-crypto.webp',
    color: 'var(--sleeve-crypto)',
    to: '/markets/crypto:BTC',
    toLabel: 'Crypto markets',
  },
  cfd: {
    id: 'cfd',
    title: 'Gold & CFDs',
    venue: 'Capital.com demo',
    trader: 'the practice trader',
    emblem: '/assets/sleeve-cfd.webp',
    color: 'var(--sleeve-cfd)',
    to: '/markets/cfd:GOLD',
    toLabel: 'CFD markets',
  },
};

export const SLEEVE_ORDER: SleeveId[] = ['stocks', 'crypto', 'cfd'];

/** The sleeve colours as hex, for WebGL (the same values as --sleeve-* in tokens.css). */
export const SLEEVE_HEX: Record<SleeveId, { light: string; dark: string }> = {
  stocks: { light: '#0071e3', dark: '#0a84ff' },
  crypto: { light: '#7a5af8', dark: '#9d8cff' },
  cfd: { light: '#b7850f', dark: '#e0b23c' },
};

/** The market-feed group that belongs to each sleeve (the Markets page rows). */
export const SLEEVE_OF_GROUP: Record<string, SleeveId> = { stocks: 'stocks', crypto: 'crypto', cfd: 'cfd' };

export interface EquityPoint {
  /** Unix seconds. */
  t: number;
  v: number;
}

/** One thing that happened to a trade, in order: the timeline of its story. */
export interface TradeEvent {
  at: string;
  kind: 'plan' | 'open' | 'amend' | 'note' | 'close' | 'cancel' | 'review';
  title: string;
  detail: string;
}

/** An open trade valued at the market feed's latest price, as if it were closed now. */
export interface TradeMark {
  /** The feed's latest price for the instrument. */
  price: number;
  /** Unix seconds of that price. */
  at: number;
  /** The price move since the fill, in USD, before fees. */
  grossUsd: number;
  /** The fee paid on entry plus the fee to close now (zero where the venue charges none). */
  feesUsd: number;
  /** The result if closed now: grossUsd minus feesUsd. */
  netUsd: number;
  /** Distance from the price to the stop as a share of the price (negative: beyond the stop). */
  toStop: number | null;
}

export interface PracticeTrade {
  tradeId: string;
  epic: string;
  direction: 'BUY' | 'SELL';
  size: string;
  entry: string;
  /** The stop and target in force now (after any AMEND). */
  stop: string;
  target: string | null;
  /** The stop and target as first planned. */
  plannedStop: string;
  plannedTarget: string | null;
  riskUsd: string | null;
  /** USD per unit of the quote currency (1 for USD instruments). */
  fxToUsd: string;
  /** Open trades only, when the market feed has a price for the instrument. */
  mark: TradeMark | null;
  thesis: string;
  setup: string;
  timeframe: string;
  invalidation: string;
  /** Intel references the plan cited (ADR-0047): record ids and snapshot references. */
  evidence: string[];
  plannedAt: string;
  openedAt: string | null;
  fill: string | null;
  closedAt: string | null;
  closeLevel: string | null;
  closeReason: string | null;
  /** Net result after fees and financing. */
  pnlUsd: string | null;
  feesUsd: string | null;
  financingUsd: string | null;
  lesson: string | null;
  followedPlan: boolean | null;
  events: TradeEvent[];
  state: 'planned' | 'open' | 'closed' | 'cancelled';
}

/** A sleeve the assistant trades through a practice journal (crypto, CFDs). */
export interface PracticeSleeve {
  id: Exclude<SleeveId, 'stocks'>;
  start: number;
  /** What the sleeve is worth now: realized plus the open trades valued at the latest price. */
  equity: number;
  /** The start plus every closed trade's net result. */
  realized: number;
  /** The open trades' result if closed now, after entry and exit fees. */
  openPnl: number;
  /** Unix seconds of the latest price used for openPnl; null when no open trade is valued. */
  markedAt: number | null;
  floor: number;
  riskFraction: number;
  maxOpenTrades: number;
  /** Realized equity after each closed trade, starting at the first journal event. */
  curve: EquityPoint[];
  trades: PracticeTrade[];
  lastEventAt: string | null;
  /** False when the journal's hash chain does not verify: numbers are then not trusted. */
  chainOk: boolean;
}

/** What a signal becomes under the sleeve's limits (ADR-0049). */
export interface ScanTicket {
  /** TRADE: take it. TOO_LARGE: the smallest contract risks more than the limit. GROUP_BUSY: a correlated trade is open. */
  status: 'TRADE' | 'TOO_LARGE' | 'GROUP_BUSY';
  direction: 'LONG' | 'SHORT';
  size: string;
  stop: string;
  target: string;
  riskUsd: string;
  maxHoldHours: number;
  note: string;
}

/** One epic of the setup on its last completed daily bar (weekly momentum, ADR-0052). */
export interface ScanReading {
  epic: string;
  /** The UTC day of the bar that was read. */
  day: string;
  /** True when that bar is older than yesterday: the reading is not today's. */
  stale: boolean;
  close: string;
  /** The close against the close a week earlier, minus one (a fraction, as text). */
  change: string;
  /** The side the week points to; null when it did not move. */
  direction: 'LONG' | 'SHORT' | null;
  /** The bar is the week's decision bar (Sunday's, closing Monday 00:00 UTC). */
  decisionBar: boolean;
  signal: boolean;
  ticket: ScanTicket | null;
}

/** An open practice trade checked against its stop, target and time stop at the feed's last price. */
export interface ScanOpenTrade {
  sleeve: string;
  tradeId: string;
  epic: string;
  dueAt: string | null;
  overdue: boolean;
  beyondStop: boolean;
  atTarget: boolean;
  exitSignal: boolean | null;
}

/** The practice watch's scan (runs/practice/scan.json), read as the watcher wrote it. */
export interface PracticeScan {
  generatedAt: string;
  /** Why the scan could not run; the other fields are then empty. */
  error: string | null;
  feedStale: boolean;
  feedUpdatedAt: string | null;
  riskLimitUsd: string | null;
  /** The setup's name, e.g. 'Weekly momentum on the Bitcoin and Ether CFDs'. */
  title: string | null;
  /** When the setup decides next (UTC). */
  nextDecisionAt: string | null;
  readings: ScanReading[];
  openTrades: ScanOpenTrade[];
}

/** One decision of the practice trader on a completed bar (ADR-0056). */
export interface TraderDecision {
  setup: string;
  epic: string;
  decidedAt: string;
  direction: 'BUY' | 'SELL' | null;
  /** OPENED, NO_SIGNAL, NO_BAR, BUSY, SPREAD, SWITCHED_OFF, ... */
  outcome: string;
  detail: string;
}

/** A setup the practice trader runs: a registered trial's rule (ADR-0056). */
export interface TraderSetupState {
  key: string;
  title: string;
  /** The practice journal's sleeve name: 'crypto-cfd' or 'capital-demo'. */
  sleeve: string;
  /** tested: its trial passed every check. forward_test: run on demo although it failed one.
   *  practice: a rule with no passing trial, run to keep the sleeve trading (ADR-0060). */
  status: 'tested' | 'forward_test' | 'practice';
  trial: string;
  enabled: boolean;
  decides: string;
  openTrades: string[];
  forwardTrades: number;
  forwardResultUsd: string;
  lossCapUsd: string | null;
  switchedOff: string | null;
  lastDecision: TraderDecision | null;
}

/** runs/practice/trader.json: the practice trader's heartbeat and setups. */
export interface PracticeTraderState {
  live: boolean;
  updatedAt: string;
  /** The heartbeat is recent: the trader is running. */
  fresh: boolean;
  nextDecisionAt: string | null;
  hedging: boolean;
  entriesPaused: string | null;
  errors: string[];
  setups: TraderSetupState[];
}

/** The practice journal's sleeve name for each console sleeve. */
export const JOURNAL_SLEEVE: Record<Exclude<SleeveId, 'stocks'>, string> = { crypto: 'crypto-cfd', cfd: 'capital-demo' };

export interface MissionState {
  target: number;
  stocks: {
    start: number;
    /** portfolio_id in configs/paper.yaml: the stocks sleeve's tables belong to this portfolio. */
    portfolioId: string | null;
    maxOpenPositions: number;
    maxEntriesPerSession: number;
  };
  practice: PracticeSleeve[];
  /** Null until the practice watch has written its first scan. */
  scan: PracticeScan | null;
  /** Null until the practice trader has run. */
  trader: PracticeTraderState | null;
}

/** One sleeve as a page draws it. */
export interface SleeveView {
  id: SleeveId;
  start: number;
  equity: number;
  curve: EquityPoint[];
  openCount: number;
  closedCount: number;
  wins: number;
  /** A short count line: closed trades and wins, or entries taken. */
  tally: string;
  /** A short, true sentence about what is happening in the sleeve right now. */
  statusLine: string;
  /** Something is live right now: the bot is running or a trade is open. */
  live: boolean;
  /** Not usable yet (for example the console is on another database). */
  note: string | null;
}

/** The setups the running practice trader trades in a sleeve (none when it is not running). */
export function runningSetups(id: Exclude<SleeveId, 'stocks'>, trader: PracticeTraderState | null | undefined): TraderSetupState[] {
  if (!trader || !trader.fresh || !trader.live) return [];
  return trader.setups.filter((x) => x.sleeve === JOURNAL_SLEEVE[id] && x.enabled && !x.switchedOff);
}

export function practiceView(p: PracticeSleeve, trader?: PracticeTraderState | null): SleeveView {
  const running = runningSetups(p.id, trader);
  const open = p.trades.filter((t) => t.state === 'open');
  const closed = p.trades.filter((t) => t.state === 'closed');
  const waiting = p.trades.filter((t) => t.state === 'planned');
  const side = (t: PracticeTrade) => `${t.direction === 'BUY' ? 'long' : 'short'} ${t.epic}`;
  const wins = closed.filter((t) => Number(t.pnlUsd) > 0).length;
  const atFloor = p.equity <= p.floor;
  const statusLine = !p.chainOk
    ? 'Journal failed its integrity check'
    : atFloor
      ? 'At its loss floor: no new trades until you review'
      : open.length
        ? `${open.length} open: ${open.map(side).join(', ')}`
        : waiting.length
          ? `${waiting.length} waiting to fill: ${waiting.map((t) => `${side(t)} at ${t.entry}`).join(', ')}`
          : running.length
            ? `Trader running: ${running.map((x) => x.title).join('; ')}`
            : closed.length
              ? 'No open trade'
              : 'No trade yet';
  return {
    id: p.id,
    start: p.start,
    equity: p.equity,
    // the open trades valued now are the curve's last point, so the line moves with the market
    curve: p.markedAt !== null && p.curve.length ? [...p.curve, { t: Math.max(p.markedAt, p.curve[p.curve.length - 1].t), v: p.equity }] : p.curve,
    openCount: open.length,
    closedCount: closed.length,
    wins,
    tally: closed.length ? `${closed.length} closed · ${wins} won` : 'no closed trade yet',
    statusLine,
    live: open.length > 0 || waiting.length > 0 || running.length > 0,
    note: null,
  };
}

/** Share of each sleeve in the total, for the stacked goal bar (falls back to equal thirds). */
export function sleeveShares(views: SleeveView[]): number[] {
  const total = views.reduce((a, v) => a + Math.max(v.equity, 0), 0);
  return views.map((v) => (total > 0 ? Math.max(v.equity, 0) / total : 1 / views.length));
}
