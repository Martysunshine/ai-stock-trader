# Strategy research and the path to profitability

## 1. What would actually create profit?

A tradable edge exists when the expected value of the chosen entries and exits exceeds commissions, spread, slippage, FX, rounding, and other costs at executable prices. AI can help identify conditional patterns, reject weak candidates, or extract information. More data or a larger model does not by itself create that edge.

For a long position with quantity `q`:

```text
realized_net_pnl = q * (actual_exit_price - actual_entry_price)
                   - entry_fees - exit_fees - attributable_FX_costs

total_project_net_pnl = net_trading_pnl
                        - data_costs - model_API_costs - compute_costs
                        - funding/withdrawal_costs - other_operating_costs
```

Actual entry/exit prices already reflect the realized spread and slippage. Do not subtract those costs twice. Before execution, estimate them explicitly relative to the prediction's price basis.

If a model predicts mid-to-mid returns, its cost estimate includes both crossings of the spread. If its label uses executable ask-to-bid returns, those crossings are already in the label. Record this convention in every experiment.

The target quantity is not “probability of a green candle.” It is the expected net return of an executable trade with the implemented horizon and exits. Accuracy, win rate, and convincing explanations are secondary diagnostics.

## 2. USD 50 economics

The following are arithmetic illustrations, not expected returns:

| Scenario | Arithmetic | Consequence |
| --- | --- | --- |
| USD 10 position moves favorably by 0.1% | USD 0.01 gross | Even tiny costs can consume the entire move |
| USD 10 position has a 0.2% round-trip cost | USD 0.02 cost | A 0.1% expected move is uneconomic |
| USD 5 buy and approximately USD 5 sell each incur a 1% commission | Approximately USD 0.10 total | About 2% round-trip drag before spread; relevant to the researched IBKR fractional fee rule |
| USD 99/month data for three months | USD 297 | Operating cost is nearly six times initial trading capital |
| USD 5 monthly compute for three months | USD 15 | The project needs USD 15 trading profit just to cover that assumed expense |

IBKR's fractional footnote and Alpaca's USD 99 consolidated-data plan are described and sourced in `markets_and_brokers.md`. The compute line is an assumed budget illustration, not a quoted hosting price.

Prefer local compute on existing hardware, free paper access, and no paid LLM calls in the order loop. Free data can be incomplete; reduced cost never justifies pretending it has consolidated coverage. If suitable licensed data and a compatible broker cannot be obtained affordably, the correct first result is a paper research system rather than an uneconomic live bot.

## 3. Initial research universe

Start with a fixed, disclosed candidate universe such as AAPL, MSFT, GOOGL, AMZN, and NVDA, subject to current broker eligibility, fractionability, liquidity, and data availability. These are research examples, not buy recommendations. Evaluate sector concentration; five technology-related names do not provide broad diversification.

The agent may later study US-listed gold producers such as NEM and AEM after confirming the instrument and current broker listing. Gold-miner results form a separate strategy/universe evaluation; do not quietly mix them into a successful tech-stock test.

Exclude OTC equities, distressed microcaps, partly paid shares, leveraged products, and short sales in the pilot. Avoid scheduled earnings windows in v1 unless an earnings-specific strategy has separate evidence. Use a point-in-time earnings calendar; if it is unavailable, disclose that limitation and do not claim event-risk filtering works.

## 4. Candidate A — intraday trend continuation

**Hypothesis:** after an established intraday trend, certain liquid stocks continue beyond a recent range sufficiently often to cover costs. This must be tested; a breakout rule is not inherently profitable.

Initial frozen specification:

1. Use completed five-minute bars and a fifteen-minute trend context.
2. Require an upward context, for example positive trailing returns and price above a consistently defined session VWAP.
3. Generate a long candidate only after a completed bar closes above the previous six completed five-minute highs. Exclude the candidate bar from that historical range.
4. Require current spread/liquidity and data freshness to pass the canonical risk policy.
5. Exclude the first thirty minutes and the last thirty minutes of ordinary entry time. Exchange early closes use corresponding calendar-relative windows.
6. Enter only after the candidate becomes available, using a certified bounded limit order. Never assume a fill at the breakout bar's closing price.
7. Initially research a volatility-based stop, a target expressed as a multiple of planned stop distance, and a maximum sixty-minute hold. Calibrate these only in development windows and freeze them before final evaluation.
8. Close before the regular session ends; do not silently hold a failed intraday trade overnight.

Suggested bounded initial grid: ATR multiplier in `{1.0, 1.5}` and reward/risk target in `{1.5, 2.0}`. Register all four variations, including failures. Additional filters or horizons count as further trials. The research budget and evidence requirements are in `risk_and_validation.md`.

## 5. Candidate B — controlled mean reversion

**Hypothesis:** in a quiet, nontrending regime, a moderate price excursion below a session reference can revert with favorable net expectancy. It should not be applied indiscriminately to collapsing prices.

- Use a completed-bar deviation from a same-feed VWAP or moving reference, normalized by recent volatility.
- Require a quiet/nontrending regime defined without future data, no identified company event, and a completed-bar stabilization condition.
- Enter only after the observation is available. Define a volatility-based invalidation stop, an exit near the reference, and a bounded hold.
- Reject if the expected reversion distance does not cover round-trip costs and the validated uncertainty margin.
- Never average down or widen a stop after entry.

Keep this in a separate experiment family. Initially deploy at most one validated strategy; combining A and B adds model-selection and interaction risk and needs a fresh portfolio evaluation.

## 6. AI model design

Start with regularized logistic regression for a precisely defined profitable-after-costs outcome, and a simple regression model for gross executable return. Then compare a gradient-boosted tree challenger. Prefer a small model that improves economic results over a complex model that only improves fit.

Candidate features, all causal:

- Trailing 1/3/6/12-bar returns, preceding-session return, and overnight gap.
- ATR/realized volatility, range position, reference/VWAP deviation, and time of day.
- Recent dollar volume, relative volume compared with prior sessions at the same time, and observed spread.
- Lagged market/sector context using properly licensed observations.
- For a later gold model: lagged gold returns, AUD/USD, gold expressed in AUD, and company events available before the decision.

Choose the label **before** fitting: entry at the next feasible decision/quote after latency, then the implemented stop/target/time exit. With bars, ambiguous stop/target order must be treated conservatively or resolved from quote/tick data. Purge overlapping labels across data splits.

Calibrate probabilities on a chronological validation set. Measure Brier/log-loss as diagnostics, but select a threshold using net economics and risk, with all tuning logged. Use uncertainty estimates appropriate to the sample; a raw model probability is not a verified confidence interval.

The model is a **candidate filter** in v1. Position sizing is controlled by deterministic risk, not model confidence. Compare rule-only and AI-filtered results with matched capital, costs, and risk. Also compare equal-exposure and equal-turnover controls so a reduction in trades is not mistaken for predictive skill.

## 7. News and LLMs

Add news only after the price-only pipeline is working. Use entitled feeds and issuer/regulator sources with publication and first-seen timestamps. Deduplicate syndication, record revisions, and map events to verified instruments.

An LLM can extract categories such as earnings, guidance, financing, production disruption, or acquisition, plus source-supported facts. It must be allowed to return unknown and must not invent price forecasts or numerical facts absent from the source.

Historical LLM tests have an additional contamination problem: a current model may already know what happened after an old article. Do not treat such tests as clean historical prediction. Constrain extraction to supplied facts, evaluate against deterministic text baselines, and require forward evidence for any LLM feature before using it to make trading decisions. If historical contamination cannot be excluded, classify that evidence accordingly.

Cache annotations once per event, cap cost, and keep article content isolated from tools and broker credentials. Prompt injection in an article must not become a trading instruction. News-model failure cannot block already-required exits.

## 8. Australian gold research track

Study Northern Star (ASX: NST), Evolution (ASX: EVN), and Genesis (ASX: GMD), initially in offline/replay research. The issuer identities are verified in the sources document. Broker availability and the historical universe still require confirmation.

Two useful hypotheses:

1. **Post-open continuation:** gold and FX information available before ASX opening, combined with the first completed bars after opening, predicts the return from a feasible later entry to a specified exit.
2. **Residual reversion:** a miner moves unusually relative to an estimated gold/FX/sector model, and the residual subsequently reverts in a non-event regime.

Do not score a model on yesterday's close-to-today's-open gain if the bot could only enter after today's open. A prediction can be correct while the reported gain is inaccessible to the strategy.

Fit gold/FX betas using trailing windows only. Include production, costs, financing, acquisition, operational, currency, and company-specific risks; miners are not simple gold-price trackers. Announcements and opening auctions may rapidly incorporate overnight information. The claim that gold-equity prediction is “easy” is an untested hypothesis.

Keep USD and AUD ledgers separate, use an ASX calendar, and include actual FX and settlement constraints. Do not substitute a leveraged gold CFD to bypass the parcel requirement.

## 9. Trade admission and compounding

For each candidate, estimate expected gross USD PnL at the actual permitted size, conservative round-trip costs, and a prevalidated uncertainty margin. Enter only if:

```text
expected_gross_pnl - estimated_costs - uncertainty_margin > 0
AND broker/account permissions pass
AND data/price/exit capabilities pass
AND exact rounded size passes risk and minimums
```

The margin is calibrated using training/validation evidence and locked with the model; do not invent a universal confidence threshold. If there is insufficient evidence to estimate net edge, stay in research/paper mode.

Maintain a compounding capital base:

```text
capital_base = initial_trading_capital
               + realized_net_pnl + received_net_cash_distributions
               - withdrawals

effective_sizing_equity = min(capital_base, conservative_current_equity)
```

For the initial USD 50 experiment, external deposits are zero. Later deposits, if separately authorized, must be reported as contributions rather than gains. Losses and adverse marks reduce sizing immediately. Unrealized gains do not increase the compounding capital base. Available funds must also pass currency, reserve, and settlement tests; realized profit is not necessarily immediately spendable cash.

Recompute position limits from effective sizing equity. Keep risk percentages constant or lower them; rising profit does not justify rising risk percentages. Reserve operating expenses outside the initial trading balance wherever possible, but still deduct them in total project profitability.

## 10. Scaling and honest results

Promotion proceeds by evidence, not a balance deadline. USD 50 is a minimum-capital feasibility and execution pilot, not a likely source of meaningful income. Scale only after actual live costs agree with the validated model and a new affordability/capacity calculation passes.

Every report must show data coverage, all attempted variants, capital assumptions, out-of-sample dates, daily net returns, drawdown, turnover, exposure, actual/simulated fee assumptions, uncertainty, and cash-flow adjustments. If no strategy survives costs, reject it or change the tested horizon under a new registered experiment. Never publish a profit claim based solely on a repository chart or selectively chosen days.
