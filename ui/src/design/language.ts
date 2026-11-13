/**
 * Plain-English meaning of the bot's codes. Sources: RejectReason (domain/candidates.py),
 * PauseCause (ops/pause.py), OrderState/Purpose (domain/orders.py), alert codes (execution/*).
 */

export const REASONS: Record<string, { title: string; why: string }> = {
  NO_EDGE: {
    title: 'No proven edge',
    why: 'No validated model scored this candidate, so expected profit after costs is unknown. The bot never trades on hope.',
  },
  TOO_COSTLY: { title: 'Costs exceed edge', why: 'Fees and spread would eat more than the expected gain.' },
  UNKNOWN_COSTS: { title: 'Costs unknown', why: 'The fee schedule for this order is not known, so it cannot be priced.' },
  ACCOUNT_RULE: { title: 'Account rule', why: 'The account type or its rules do not allow this order.' },
  STALE_DATA: { title: 'Stale quote', why: 'The latest quote is older than the 2-second limit.' },
  RISK_LIMIT: { title: 'Risk limit', why: 'A position, exposure or per-session entry limit is already used up.' },
  SPREAD_TOO_WIDE: { title: 'Spread too wide', why: 'The bid-ask spread is wider than the 10 bps entry limit.' },
  INSUFFICIENT_FUNDS: { title: 'Not enough cash', why: 'After the required cash reserve there is not enough to buy.' },
  SIZE_BELOW_MINIMUM: { title: 'Below minimum size', why: "The sized order is smaller than the broker's minimum." },
  PAUSED: { title: 'Paused', why: 'New entries are paused; exits still work.' },
  CAPABILITY: {
    title: 'Not certified',
    why: 'This instrument or order type is not certified for the broker yet (configs/instruments: tradable false).',
  },
  FORBIDDEN_PRODUCT: { title: 'Forbidden product', why: 'Only ordinary long equities are allowed.' },
  OUTSIDE_ENTRY_WINDOW: {
    title: 'Outside entry window',
    why: 'Entries are only allowed from 30 minutes after the open to 30 minutes before the close.',
  },
  ENTRY_LIMIT_REACHED: { title: 'Entry limit reached', why: 'The maximum number of new entries for this session is used.' },
  NO_EXIT_PLAN: { title: 'No exit plan', why: 'No certified way to exit the position exists.' },
  FEED_MISMATCH: { title: 'Feed mismatch', why: 'The quote and the signal came from different data feeds.' },
  MODE_NOT_PERMITTED: { title: 'Mode not permitted', why: 'This action is not allowed in the current mode.' },
  LEADERSHIP: { title: 'Not the leader', why: 'This worker does not hold the account lock, so it cannot submit.' },
};

export const reasonTitle = (code: string) => REASONS[code]?.title ?? code.replaceAll('_', ' ').toLowerCase();

export const PAUSE_CAUSES: Record<string, string> = {
  DAILY_NY: 'Daily loss limit (New York session) reached',
  WEEKLY_NY: 'Weekly loss limit (New York week) reached',
  DAILY_UTC: 'Daily loss limit (UTC day) reached',
  WEEKLY_UTC: 'Weekly loss limit (UTC week) reached',
  DRAWDOWN: 'Drawdown from the high-water mark reached 10%',
  CAPITAL_FLOOR: 'Equity fell to the USD 45 capital floor',
  OWNER_REQUESTED: 'Paused by the owner',
  UNKNOWN_ORDER: 'An order the bot did not create was seen',
  UNRESOLVED_ORDER: 'An order outcome could not be resolved',
  LEADERSHIP_LOST: 'The worker lost the account lock',
  STALE_ACCOUNT: 'Account data is stale',
  FEED_GAP: 'A market-data poll failed',
  FEED_MISMATCH: 'Quote and signal feeds disagree',
  CLOCK_SKEW: 'Data clock is more than 250 ms ahead of the local clock',
  RECONCILIATION_FAILED: 'Reconciliation with the broker failed',
  CAPABILITY_CHANGED: 'Broker capabilities changed',
  MISSING_PROTECTION: 'A position lacks its protective exit',
  ABNORMAL_PATTERN: 'Abnormal trading pattern detected',
  UNSOLICITED_EXECUTION: 'An execution with no matching intent arrived',
  RECOVERY_PENDING: 'Startup recovery: entries wait for a passed reconciliation',
};

export const ORDER_STATES: Record<string, string> = {
  PENDING_SUBMIT: 'Waiting to send',
  SUBMITTING: 'Sending',
  NOT_SENT: 'Not sent',
  UNKNOWN_OUTCOME: 'Outcome unknown',
  UNRESOLVED: 'Unresolved',
  NOT_ACCEPTED: 'Not accepted',
  ACKNOWLEDGED: 'Working',
  PARTIALLY_FILLED: 'Partly filled',
  CANCEL_REQUESTED: 'Cancel requested',
  FILLED: 'Filled',
  CANCELED: 'Cancelled',
  EXPIRED: 'Expired',
  REJECTED: 'Rejected',
};

export const PURPOSES: Record<string, string> = {
  ENTRY: 'Entry',
  EXIT_STOP: 'Stop exit',
  EXIT_TARGET: 'Target exit',
  EXIT_TIME: 'Time exit',
  EXIT_SESSION: 'Session-close exit',
  EXIT_RISK_REDUCTION: 'Risk-reduction exit',
  EXIT_LIQUIDATION: 'Liquidation',
};

export const ALERTS: Record<string, string> = {
  EXIT_PENDING: 'An exit is queued while the stock is halted',
  UNSOLICITED: 'An order or fill did not match any intent',
  LEDGER_INVARIANT: 'Posting a fill broke a ledger invariant',
  DAY_ORDER_NOT_EXPIRED: 'An order was still live after the close',
  ENTRIES_BLOCKED: 'Trades were blocked for a system reason (pause, data, limit or setup), not the market',
};

export const LEDGER_KINDS: Record<string, string> = {
  FLOW_INITIAL_CAPITAL: 'Initial capital',
  FLOW_DEPOSIT: 'Deposit',
  FLOW_WITHDRAWAL: 'Withdrawal',
  FLOW_DISTRIBUTION: 'Distribution',
  TRADE_BUY: 'Buy',
  TRADE_SELL: 'Sell',
  FEE: 'Fee',
  FEE_ACCRUAL: 'Fee (estimated)',
  FEE_ADJUSTMENT: 'Fee adjustment',
};

export const LOSS_WINDOWS: Record<string, string> = {
  NY_SESSION: 'Today (New York)',
  NY_WEEK: 'This week (New York)',
  UTC_DAY: 'Today (UTC)',
  UTC_WEEK: 'This week (UTC)',
};

export const DISPOSITIONS: Record<string, string> = {
  APPLIED: 'applied',
  FILL_POSTED: 'fill posted',
  FILL_DUPLICATE: 'duplicate fill ignored',
  STALE: 'stale, ignored',
  UNSOLICITED: 'unsolicited',
  PENDING: 'pending',
};
