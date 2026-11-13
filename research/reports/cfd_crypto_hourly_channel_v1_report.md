# Day trades on the 24-hour channel - Bitcoin and Ether CFDs, with the break and against it

Generated 2026-11-11 16:08 UTC. Study `cfd_crypto_hourly_channel_v1`, results digest `f51522733acf0831`. BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence.

## Verdict

- **T-0023** (CFD_HOURLY_CHANNEL): 584 trades, net +15.13 USD on USD 100 windows, stressed -27.55 USD. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive yes, stressed_net_positive no, walk_forward_net_positive yes.
- **T-0024** (CFD_HOURLY_CHANNEL): 408 trades, net -58.87 USD on USD 100 windows, stressed -66.51 USD. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive no, stressed_net_positive no, walk_forward_net_positive yes.
- Walk forward: 274 trades over 365 sessions, net +15.05 USD. G2: **NOT_PASSED** (oos_trades_at_least_100 yes, sessions_at_least_120 yes, effective_sessions_at_least_120 yes, positive_lower_bound_session_net no, positive_lower_bound_per_trade_net no, selection_adjusted_lower_bound_positive no).

## Data

Dataset `capital_demo_crypto_cfd_1h_v1`: the broker's hourly bid and ask, used as hourly bars (sessions are UTC days) on the `UTC_24_7` calendar. Warm-up 30 days before each window. Splits: development 2025-01-01T00:00:00Z .. 2026-08-01T00:00:00Z, locked final test 2026-08-01T00:00:00Z .. 2026-11-01T00:00:00Z (unlocked: False).

Median quoted spread at the hourly close, bps of the mid:

| instrument | hourly bars | closed weekdays | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|
| CAPITAL:BTCUSD | 14394 | 0 | 5.1 | 4.8 | 7.1 |
| CAPITAL:ETHUSD | 14395 | 0 | 4.8 | 5.9 | 8.4 |

## Costs

| view | slippage bps | spread x | fills | financing | credits | missed % |
|---|---|---|---|---|---|---|
| base | 1 | 1 | bid/ask | yes x1 | yes | 0 |
| stressed | 2 | 2 | bid/ask | yes x1.5 | no | 5 |
| diagnostic | 0 | 1 | mid | no | yes | 0 |

## T-0023 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 584 | +15.13 | +15.13 | 28.73 | 0.00 | 42% | 1.07 | 14% | 3/7 | 0 |
| stressed | 467 | -27.55 | -27.55 | 43.86 | 0.00 | 39% | 0.86 | 33% | 6/7 | 0 |
| diagnostic | 594 | +54.19 | +54.19 | 0.04 | 0.00 | 46% | 1.24 | 9% | 1/7 | 0 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 315 | -13.11 | 40% | +0.96 | -0.70 | 0.00 |
| short | 269 | +28.24 | 45% | +1.05 | -0.67 | 0.00 |

Signals (base view): {'FILLED': 584, 'REJECTED': 1128}. Not taken by reason: {'PAUSED': 102, 'RISK_LIMIT': 1026}. Exit reasons: {'STOP': 226, 'TARGET': 110, 'TIME_EXIT': 248}.

Continuous run over the development period from USD 100: 130 trades, net -2.30 USD.

## T-0024 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 408 | -58.87 | -58.87 | 17.31 | 0.00 | 43% | 0.64 | 59% | 7/7 | 0 |
| stressed | 376 | -66.51 | -66.51 | 30.32 | 0.00 | 41% | 0.57 | 67% | 7/7 | 0 |
| diagnostic | 459 | -49.72 | -49.72 | 0.03 | 0.00 | 46% | 0.73 | 52% | 6/7 | 0 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 173 | -18.92 | 44% | +0.64 | -0.70 | 0.00 |
| short | 235 | -39.95 | 42% | +0.57 | -0.71 | 0.00 |

Signals (base view): {'FILLED': 408, 'REJECTED': 1304}. Not taken by reason: {'ENTRY_LIMIT_REACHED': 9, 'PAUSED': 837, 'RISK_LIMIT': 458}. Exit reasons: {'STOP': 193, 'TARGET': 128, 'TIME_EXIT': 87}.

Continuous run over the development period from USD 100: 28 trades, net -10.05 USD.

## Comparators and selection

- Buy and hold (equal notional, long, financing paid) over the walk-forward test sessions: net -61.26 USD, max drawdown 72%.
- Cash: 0.00 USD.
- Reality check p-value 0.136, selection-adjusted lower bound -0.0242; deflated Sharpe 0.000 with 23 registered trials.

## Limits

- Financing rates are those read on 2026-11-10, applied to every year.
- Demo quotes may be tighter than live fills; entries are at the open of the hour after the decision.
- A stop is assumed to fill at its level plus slippage unless the market opens through it; a stop and a target touched in one hourly bar count as the stop.
- Backtest only. Practice results on the demo account are a separate record; neither is G2 or G3 evidence on its own.
