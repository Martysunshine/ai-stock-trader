# Risk policy, validation, and release gates

This file owns the initial numeric policy. These are conservative **project choices**, not broker requirements, optimal trading parameters, or guarantees against loss. Production code must load one validated policy and reference its hash in every order decision.

## 1. Canonical starter configuration

The following YAML is a specification for the coding agent to implement and schema-validate; it is not executable software supplied with this pack.

```yaml
policy_version: 1
mode: replay
base_currency: USD
initial_trading_capital: "50.00"
live_enabled: false

instruments:
  ordinary_equities_only: true
  long_only: true
  fractional_only_if_certified: true
  leverage_enabled: false
  shorting_enabled: false
  extended_hours_enabled: false
  minimum_notional_from_broker: true

exposure:
  risk_per_trade_fraction: "0.0025"
  max_total_planned_risk_fraction: "0.0050"
  max_gross_exposure_fraction: "0.40"
  max_symbol_exposure_fraction: "0.20"
  minimum_cash_reserve_fraction: "0.60"
  minimum_cash_reserve_absolute: "30.00"
  max_open_positions: 2
  live_pilot_max_open_positions: 1
  max_pending_entry_orders: 1
  max_new_entries_per_session: 2

loss_limits:
  daily_loss_fraction: "0.01"
  weekly_loss_fraction: "0.025"
  max_high_water_drawdown_fraction: "0.10"
  initial_capital_pause_floor: "45.00"
  automatic_resume_after_loss_breach: false

quality:
  max_quote_age_seconds: 2
  max_account_snapshot_age_seconds: 5
  max_clock_skew_milliseconds: 250
  max_entry_spread_bps: 10
  gap_allowance_floor_bps: 10
  require_feed_parity: true
  require_positive_conservative_net_edge: true

execution:
  native_protection_required_for_live: true
  allow_blind_submission_retries: false
  unknown_order_blocks_new_entries: true
  preserve_risk_reducing_exits_when_paused: true
  funds_check_uses_actual_account_rule_profile: true

expenses:
  paid_data_enabled: false
  live_loop_llm_api_budget_usd_per_day: "0.00"
  automatic_funding_enabled: false
  automatic_withdrawals_enabled: false
```

Risk, exposure, reserve percentages, and symbol limits use `effective_sizing_equity` defined in `strategy_and_profitability.md`, with broker funds constraints applied separately. Pending entries count as exposure and planned risk. A limit of two entries is a ceiling, not a trading goal; broker rules can lower it further.

Daily/weekly loss uses conservative marked equity plus all known/accrued trading fees, adjusted for external cash flows. Record beginning-of-session and beginning-of-week equity; do not reset the loss counter by restarting the process. Intraday equity marks do not include optimistic prices for illiquid positions.

High-water drawdown uses cash-flow-adjusted conservative marked equity. Trigger a pause at 10% drawdown or at/below USD 45, whichever condition occurs first. Default loss-breach pauses require a recorded owner review before resume. Health-only pauses may clear after complete reconciliation, but not while a loss-breach flag remains.

## 2. Dollar interpretation at USD 50

| Control | Initial amount |
| --- | --- |
| Planned risk per trade, 0.25% | USD 0.125 before conservative rounding |
| Total planned risk, 0.50% | USD 0.25 |
| Maximum symbol notional, 20% | USD 10 |
| Maximum gross notional, 40% | USD 20 |
| Cash reserve, greater of 60% and USD 30 | USD 30 |
| Daily loss trigger, 1% | USD 0.50 |
| Weekly loss trigger, 2.5% | USD 1.25 |
| Initial-capital pause floor | USD 45 |

Costs, quote precision, and minimum trade sizes may make these budgets infeasible. The agent must calculate that and return no trade; it may not raise risk automatically.

The cash reserve remains unavailable for new entries and known fees must be reserved too. At higher equity, the percentage reserve generally dominates the USD 30 floor. As losses reduce funds, these controls can stop further trading altogether.

USD 45 is a **pause trigger**, not a guaranteed account floor. A gap, halt, failed order, or broker outage can cause larger loss before an exit completes. Cash reserves and no borrowing limit market exposure but cannot eliminate operational or counterparty risk. Guaranteeing that all original USD 50 stays intact would require keeping that USD 50 outside trading risk, with a separate risk budget.

## 3. Exact sizing

Let `E` be effective sizing equity, `P` the bounded entry price, `S` the planned stop, `g` a conservative adverse-fill/gap allowance fraction, and `F(q)` all estimated round-trip fees at quantity `q`.

```text
planned_loss(q) = q * max(P - S, 0) + q * P * g + F(q)

planned_loss(q) <= 0.0025 * E
```

Use at least the configured gap floor, increasing it when observed tail behavior warrants. It is an allowance, not a bound on possible gaps. Stop width comes from the frozen strategy and observable volatility; do not tighten it merely to buy more shares.

Also require, after quantity rounding:

- Symbol and gross exposure limits, including pending orders.
- Aggregate planned stop risk under its ceiling.
- Free cash after entry reservations and reserved fees at least `max(0.60 * E, USD 30)`.
- Correct currency, actual account funds availability, and settlement rules.
- Broker minimum notional/quantity and supported precision.
- Expected net value positive at this exact size after rounding and fees.

Fee functions may be nonlinear and have minimums. Solve using a conservative monotonic quantity search or an exact calculation with boundary checks; do not just divide a risk budget by stop distance and ignore charges. Round quantity down, never up to reach a broker minimum. Skip if the resulting order is too small.

Examples must state their fee/gap assumptions. For instance, a USD 5 position with a 2.5% planned stop already uses USD 0.125 before fees or gaps, so it exceeds this starter risk budget once those costs are added. A smaller feasible position or no trade is required.

## 4. Runtime circuit breakers

Pause new exposure for a loss breach, unknown order, lost account leadership, stale/restricted account, feed mismatch/gap, clock skew, failed reconciliation, unexpectedly changed instrument capability, missing protection, or an abnormal rejection/fee/slippage pattern.

State the reason and affected account in a durable record. On a loss breach, request cancellation of entries, reconcile fills in flight, and reduce positions through certified exits. Do not cancel protective exits without immediately replacing their protection or accounting for their outstanding quantities.

A pause does not prove liquidation. Report remaining positions, queued exits, halts, and broker status until fills confirm closure. Protective orders may also be unavailable during halts or session closures.

The default requires certified broker-held protection for live positions. Synthetic-only exits are not automatically accepted because they worked in paper. A deliberate alternate policy requires a documented outage analysis, watchdog test, smaller exposure assessment, and owner selection before live use.

## 5. Research protocol

Register each experiment before running it: hypothesis, symbol/universe selection method, horizon, candidate rule, feature list, entry/exit timing, cost basis, parameters, split dates, metrics, and trial ID.

Initial exploration budget: **at most ten registered strategy/model configurations** before a review of evidence and data adequacy. This is a project limit, not a statistical claim that ten trials are always safe. Log rejected/failed configurations and informal strategy changes too. Expanding the budget requires a new recorded decision and increased multiple-testing scrutiny.

Use chronological train/validation/test separation and walk-forward evaluation. For intraday development, a starting schedule is twelve months train, three months validation, three months test, rolling forward. If data does not support this, reduce the scope and explicitly withhold unsupported conclusions. Fit preprocessing only on training data. Embargo/purge boundaries by the maximum label/holding horizon; cross-symbol records at the same time must not accidentally place the same market event in different splits.

Keep the final test period locked. If it is used to select a threshold or redesign a strategy, it becomes development data; obtain a new untouched test or rely on a forward test. Do not repeatedly inspect the same holdout until it looks good.

Count selection across strategy variants, features, symbols, thresholds, and models. Use a multiple-testing assessment appropriate to the trial history, such as a deflated performance statistic, a bootstrap maximum-statistic procedure, or probability-of-backtest-overfitting analysis. Do not stack unrelated tests until one passes. The [backtest-overfitting paper](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) motivates reporting this selection risk.

## 6. Realistic replay requirements

- Match USD 50, the same risk controls, broker permissions, min size, fractional precision, and funds rules.
- Use execution prices available after signal availability plus measured/assumed latency, not the signal bar's historical close.
- Marketable buys cross an observable ask and sells cross a bid, with additional adverse slippage where justified. A limit must respect its limit price.
- A historical bar touching a limit does not prove a fill. Model missed fills and conservative participation; passive execution needs quote/tick validation.
- If stop and target are both touched in one bar, resolve with finer data or pessimistic sequencing, never automatic target-first fills.
- Simulate partial fills, cancellation races, rejected orders, trading halts, early closes, expired DAY orders, and corporate actions.
- Include fees at their actual charging/rounding granularity. Model FX and cash-lot settlement by the correct calendar.
- Preserve raw execution prices separately from adjusted research series. Splits change quantities/basis and may invalidate outstanding orders.
- Run both normal and stressed friction assumptions. Never grant paper/replay infinite liquidity or unlimited capital.

Document cases that bar-only data cannot resolve. A bar-only replay can support preliminary strategy research; it cannot certify execution realism for a spread-sensitive strategy.

## 7. Required comparisons and statistical report

Compare cash/no trade, same-universe buy-and-hold, the unchanged deterministic strategy, and AI-filtered strategy under the same initial capital and cost model. Match exposure or explain the differences. Use untraded zero-return sessions in portfolio statistics.

Report net USD PnL, total project net PnL, daily return series, drawdown/time underwater, exposure, turnover, win/loss distribution, payoff ratio, profit factor, average net expectancy, slippage/fees, rejected/unfilled candidates, and sample size. Risk-adjusted metrics such as Sharpe require disclosed sampling/annualization and uncertainty; a few profitable days cannot establish an annual Sharpe.

Use block resampling by session and, where needed, multi-session blocks to preserve autocorrelation and correlated trades. Show a confidence interval for aggregate net expectancy/portfolio return, not just raw trade count. Perform prospective power/sample-size analysis from observed variance and the economically relevant effect. One hundred correlated trades may carry far less than one hundred independent observations.

Stress spreads/slippage to at least twice the base assumption, introduce realistic outages/missed trades, and evaluate trending, choppy, high-volatility, and adverse periods. Fixed recurring expenses are charged in every applicable period. Add bootstrap/path-resampling scenarios for the probability of reaching loss triggers. Label these estimates as model-dependent, not guarantees.

## 8. Promotion gates

| Gate | Minimum deliverable and pass criterion |
| --- | --- |
| G0 — broker/economic feasibility | Eligible account route, certified required capabilities, priced data/funding/fees, and at least plausible net economics at USD 50; unknown critical items block live |
| G1 — software integrity | All applicable accounting, permission, duplicate-order, protection, restart, and failure tests pass; deterministic replay is reproducible |
| G2 — research evidence | Untouched/walk-forward results after full trading costs; at least 100 completed out-of-sample trades across 120 sessions as a starter floor, sufficient effective sample/power, and a selection-aware positive lower confidence bound on net expectancy |
| G3 — forward paper/shadow | At least 30 completed trading sessions and 20 actual paper round trips, USD 50 constraints, conservative cost overlay, no unresolved incidents, and no contradictory evidence; extend if trade opportunities are sparse |
| G4 — live pilot readiness | Owner-configured dedicated account, account-rule profile, protection/cost validation, secrets and recovery setup, signed/recorded promotion manifest; live default remains disabled until deliberate startup enablement |
| G5 — scaling | Actual live fees/slippage/reliability and outcomes remain consistent with the model; reviewed capacity/affordability report before any capital increase |

These counts are initial floors, not sufficient proof. Do not manufacture trades to reach them or relax them to fit a three-month calendar. A weak or negative result fails the profitability gate even if the software works.

For G2, the default interval target is 95%, with the selection correction declared in advance. If evidence supports positive trading expectancy but total project profit is negative after fixed expenses, report it precisely and withhold the claim that the project is economically profitable. The total-cost feasibility report must support any paid live deployment.

G3 validates forward behavior and operational consistency; thirty sessions alone do not establish a durable financial edge. A short live pilot likewise cannot verify long-term profit. Describe the strength and limits of the evidence at each step.

## 9. Essential adversarial tests

| Scenario | Required invariant |
| --- | --- |
| Timeout after broker accepts entry | One persisted intent, reconciliation before any resubmission, no duplicated exposure |
| Duplicate/out-of-order order events | Ledger/fill quantities do not double count or regress |
| Partial fill followed by cancel | Actual filled position remains protected; only confirmed remainder reservation releases |
| Fill arrives after cancel request | New position is recognized and managed |
| Two workers or worker restart | Only current account leader can submit; recovered orders reconcile first |
| Stop triggered while entry system paused | Valid reducing exit remains actionable; sell quantity never exceeds ownership |
| Broker quote stale or missing | No entries; exit recovery does not deadlock behind entry-only rules |
| Commission minimum/rounding exceeds budget | Order rejected before submission |
| Unsettled cash reused in true cash mode | Entry blocked under starter policy |
| Legacy PDT, new margin, unknown regime | Each profile behaves explicitly; unknown never silently assumes permission |
| Fractional minimum/precision boundary | Round down or reject; no accidental whole-share order |
| Split, symbol change, early close, DST | Instruments, orders, quantities, and schedules stay consistent |
| Native protection unavailable/expired | Entry blocked or position incident handled under certified policy |
| Market halt/gap through stop | Loss at possible executable price, honest trigger breach reporting |
| News prompt injection or model output error | No credential/tool access and no risk-policy bypass |
| Broker/account manually altered | Reconciliation discrepancy pauses entries |
| Restart after loss threshold | Pause state and daily/weekly counters remain in force |
| Paper account shows USD 100,000 | Internal strategy budget still permits only the USD 50 subportfolio |

Run targeted tests when relevant behavior changes. Do not add superficial tests that merely repeat implementation constants; verify meaningful invariants and real failure modes.

## 10. Release evidence and rollback

A promotion manifest identifies git commit, dependencies, data/feature hashes, strategy/model/policy versions, broker capabilities, account ID alias, evaluated date ranges, costs, statistical results, outstanding limitations, and owner enablement record. Never put raw secrets in it.

Deploy a challenger to shadow/paper alongside a frozen champion. A new model does not replace the champion automatically. On a material anomaly, stop new exposure, reconcile, retain or execute valid protective exits, and preserve the incident evidence. Rolling code back must not replay already-submitted intents. A prior model may resume only after account reconciliation and resolution of persisted loss/incident pauses.
