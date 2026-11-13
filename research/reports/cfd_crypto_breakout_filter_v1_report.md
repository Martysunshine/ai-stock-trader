# A trained veto on the hourly crypto breakout (T-0023) - Bitcoin and Ether CFDs

Generated 2026-11-11 17:57 UTC. Study `cfd_crypto_breakout_filter_v1` on base study `cfd_crypto_hourly_channel_v1` trial T-0023, results digest `66ffcb5a42acb132`. BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence.

## Verdict

- **T-0026** (CFD_HOURLY_CHANNEL+LOGIT_FILTER/LGBM_FILTER): the filter **does not work** by the rule fixed before the run (the filter works only if its net USD beats the rule alone, the mean of the random control's seeds and the equal-turnover control, in the base view and in the stressed view). Checks: base_beats_rule_only no, base_beats_random_control no, base_beats_turnover_control no, stressed_beats_rule_only no, stressed_beats_random_control no, stressed_beats_turnover_control no.
- Filtered rule: 212 trades, net +4.96 USD on USD 100 windows, stressed -10.94 USD; 3 of 3 folds trained. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive yes, stressed_net_positive no, walk_forward_net_positive yes.
- Walk forward: 212 trades over 273 sessions, net +4.96 USD. G2: **NOT_PASSED** (oos_trades_at_least_100 yes, sessions_at_least_120 yes, effective_sessions_at_least_120 yes, positive_lower_bound_session_net no, positive_lower_bound_per_trade_net no, selection_adjusted_lower_bound_positive no, best_reality_check_row_is_a_model_filter no, filter_beats_rule_only_lower_bound_positive no, filter_beats_random_control_lower_bound_positive no, filter_beats_turnover_control_lower_bound_positive no).

## The filter against its controls

Net USD over the same test windows, each a fresh deployment of the sleeve's capital:

### T-0026

| variant | base | stressed | mid, no financing |
|---|---|---|---|
| rule + filter | +4.96 | -10.94 | +18.95 |
| rule alone | +15.05 | -7.94 | +33.35 |
| random refusal (mean of seeds) | +9.49 | -6.18 | +24.49 |
| equal turnover, smallest break refused | +9.92 | -6.92 | +24.37 |

Random refusal over 20 seeds: base view from +2.09 to +18.83 USD, median +9.59; 17 seeds at least as good as the filter (p 0.857). Stressed view: 17 seeds at least as good (p 0.857).

Paired bootstrap, base view: filter minus rule_only: mean -0.0370 USD a session, one-sided lower bound -0.0988; filter minus random_control: mean -0.0166 USD a session, one-sided lower bound -0.0478; filter minus turnover_control: mean -0.0182 USD a session, one-sided lower bound -0.0535.

Base view:

| variant | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows |
|---|---|---|---|---|---|---|---|---|---|
| rule + filter | 212 | +4.96 | +4.96 | 10.27 | 0.00 | 42% | 1.06 | 9% | 1/3 |
| rule alone | 274 | +15.05 | +15.05 | 13.34 | 0.00 | 44% | 1.14 | 8% | 1/3 |
| equal turnover, smallest break refused | 212 | +9.92 | +9.92 | 10.31 | 0.00 | 42% | 1.12 | 8% | 1/3 |
| random refusal (mean of seeds) | 218.6 | +9.49 | - | - | - | 43% | - | - | - |

Stressed view:

| variant | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows |
|---|---|---|---|---|---|---|---|---|---|
| rule + filter | 177 | -10.94 | -10.94 | 15.90 | 0.00 | 41% | 0.85 | 12% | 2/3 |
| rule alone | 242 | -7.94 | -7.94 | 21.83 | 0.00 | 41% | 0.92 | 13% | 2/3 |
| equal turnover, smallest break refused | 176 | -6.92 | -6.92 | 15.84 | 0.00 | 40% | 0.90 | 11% | 2/3 |
| random refusal (mean of seeds) | 182.6 | -6.18 | - | - | - | 42% | - | - | - |

Diagnostic view:

| variant | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows |
|---|---|---|---|---|---|---|---|---|---|
| rule + filter | 213 | +18.95 | +18.95 | 0.02 | 0.00 | 47% | 1.24 | 6% | 0/3 |
| rule alone | 275 | +33.35 | +33.35 | 0.02 | 0.00 | 48% | 1.32 | 6% | 0/3 |
| equal turnover, smallest break refused | 213 | +24.37 | +24.37 | 0.02 | 0.00 | 47% | 1.30 | 6% | 0/3 |
| random refusal (mean of seeds) | 219.6 | +24.49 | - | - | - | 48% | - | - | - |

Long and short (filtered rule, base view):

| side | trades | net USD | win | avg win | avg loss |
|---|---|---|---|---|---|
| long | 98 | -6.41 | 39% | +0.89 | -0.67 |
| short | 114 | +11.36 | 46% | +1.05 | -0.70 |

## Folds

| fold | train | test | train labels | validation labels | model | threshold | validation USD/session | refused | filter net | rule net |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2025-01-01..2025-07-01 | 2025-10-01..2026-01-01 | 561 (237 won) | 288 (141 won) | TRAINED logistic | 0.54 | 0.197058206522 | 210/268 | +0.17 | +7.07 |
| 2 | 2025-04-01..2025-10-01 | 2026-01-01..2026-04-01 | 562 (239 won) | 267 (123 won) | TRAINED logistic | 0.36 | 0.081785043478 | 14/243 | +7.03 | +10.23 |
| 3 | 2025-07-01..2026-01-01 | 2026-04-01..2026-07-01 | 555 (264 won) | 242 (116 won) | TRAINED logistic | 0.3 | 0.124144766667 | 0/251 | -2.25 | -2.25 |

Calibration on the test windows' labels (each signal's own outcome; diagnostic only): 762 labels, base rate 42%, mean predicted 47%, Brier 0.2502, log-loss 0.6938, ECE 0.0770.

## Selection

- Reality check across the four variants: p 0.220, selection-adjusted lower bound -0.0435, best row `T-0026/rule_only`; deflated Sharpe 0.456 with 25 registered trials.

## Method

- Features `cfd-breakout-v1`: `break_atr` (mid close beyond the channel of the previous channel_hours bars) / mean true range of the last atr_hours bars, decision bar included; negative inside the channel; `return_24h` side x ln(mid close / mid close 24 bars earlier); `return_72h` side x ln(mid close / mid close 72 bars earlier); `vol_ratio_24_168` sd of the last 24 hourly log returns / sd of the last 168 (1 if zero); `spread_bps` max(quoted spread at the decision bar's open, at its close) / mid close x 1e4; `direction` +1 long, -1 short; `hour_sin` sin(2 pi h / 24), h = UTC hour of the decision time; `hour_cos` cos(2 pi h / 24), h = UTC hour of the decision time; `hours_to_flat` hours from the decision time to the trade's flat time.
- Labels: an unfiltered base-view run of the frozen rule over the window emits every signal with its cfd-breakout-v1 features at its decision time; a signal the run took is labelled by its realised trade, any other by the same signal traded alone from the sleeve's capital with the same costs; win = net USD after spread, slippage and financing > 0; net return = net USD / notional. A signal still open when the outcome bars end, refused even alone, or with less than a week of earlier bars has no label.
- Selection: Rolling walk-forward folds over the base study's development period, purged and embargoed by the rule's longest hold: training labels decided in the training window with outcomes before its end, purged within the embargo; validation labels decided in the validation window (less the embargo) with outcomes before the test start. Per fold the primary model is trained with training.train_fold (grid by validation log-loss, calibration on validation, threshold on validation by the net USD per session of a validation replay) and a challenger replaces it only with a strictly higher validation objective. A fold that cannot be trained refuses every test signal. The test window is never an input to training or selection. Each test window is replayed for the rule alone, the rule with the filter, random refusal at the filter's refusal rate (N salts) and equal turnover (smallest break refused first), in three cost views. Study walk forward: per fold the filtered rule when its validation objective is positive, else cash. Selection-aware checks: a reality check across the four variants and the deflated Sharpe ratio with N = all registered trials. The locked final test period is not used.
- Threshold: on validation only: among thresholds whose calibrated probability accepts at least min_accepts validation signals, the one with the highest net USD per session of a base-view replay of the validation window (fresh sleeve capital, the base study's policy and costs, the bundle as the filter; outcome bars up to the test start); ties go to the lower threshold
- Challenger: per fold, the challenger replaces the primary model only if its validation objective is strictly higher; a model that cannot be trained loses to one that can
- Controls: every test window, fresh sleeve capital, base, stressed and diagnostic views: the rule alone; random refusal at the filter's realised refusal rate in that window (hash draw, one salt per seed); equal turnover, refusing as many signals as the filter did, the smallest break beyond the channel (in mean true ranges) first, ties by signal id
- Splits: development 2025-01-01T00:00:00Z .. 2026-08-01T00:00:00Z, locked final test 2026-08-01T00:00:00Z .. 2026-11-01T00:00:00Z (unlocked: False), train 6m / validation 3m / test 3m, step 3m, embargo 1260 minutes.

## Limits

- The rule alone is the base trial's rule on the test windows only, each a fresh deployment; where a test window is one of the base study's own windows it is the same run and has the same result hash.
- In a fold whose filter could not be trained, the filter variant refuses every signal; the walk forward holds cash in every fold whose validation objective is not positive.
- The filter decides on the broker's raw quotes, so it refuses the same signals in every view; the stressed view then tests those trades at doubled costs.
- A signal not taken in the unfiltered run is labelled by trading it alone; in a filtered run it may still be blocked by another open trade.
- Demo quotes may be tighter than live fills. Backtest only: neither G2 nor G3 evidence on its own.
