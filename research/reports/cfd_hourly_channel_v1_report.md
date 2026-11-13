# Day trades on the 24-hour channel - gold and two US index CFDs, with the break and against it

Generated 2026-11-11 16:08 UTC. Study `cfd_hourly_channel_v1`, results digest `5da73906a900afac`. BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence.

## Verdict

- **T-0021** (CFD_HOURLY_CHANNEL): 2425 trades, net -80.50 USD on USD 100 windows, stressed -141.34 USD. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive no, stressed_net_positive no, walk_forward_net_positive no.
- **T-0022** (CFD_HOURLY_CHANNEL): 2972 trades, net -103.45 USD on USD 100 windows, stressed -150.97 USD. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive no, stressed_net_positive no, walk_forward_net_positive no.
- Walk forward: 987 trades over 1945 sessions, net -29.23 USD. G2: **NOT_PASSED** (oos_trades_at_least_100 yes, sessions_at_least_120 yes, effective_sessions_at_least_120 yes, positive_lower_bound_session_net no, positive_lower_bound_per_trade_net no, selection_adjusted_lower_bound_positive no).

## Data

Dataset `capital_demo_cfd_1h_v1`: the broker's hourly bid and ask, used as hourly bars (sessions are UTC days) on the `BROKER_CFD` calendar. Warm-up 30 days before each window. Splits: development 2019-01-01T00:00:00Z .. 2026-08-01T00:00:00Z, locked final test 2026-08-01T00:00:00Z .. 2026-11-01T00:00:00Z (unlocked: False).

Median quoted spread at the hourly close, bps of the mid:

| instrument | hourly bars | closed weekdays | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CAPITAL:GOLD | 45145 | 13 | 2.2 | 2.0 | 2.9 | 1.7 | 1.1 | 1.5 | 1.3 | 0.9 | 1.1 |
| CAPITAL:US500 | 45546 | 11 | 1.9 | 1.8 | 1.8 | 1.6 | 1.8 | 1.7 | 1.3 | 0.8 | 0.8 |
| CAPITAL:US100 | 45601 | 11 | 2.4 | 2.1 | 1.7 | 1.3 | 1.3 | 1.2 | 1.0 | 0.8 | 0.7 |

## Costs

| view | slippage bps | spread x | fills | financing | credits | missed % |
|---|---|---|---|---|---|---|
| base | 1 | 1 | bid/ask | yes x1 | yes | 0 |
| stressed | 2 | 2 | bid/ask | yes x1.5 | no | 5 |
| diagnostic | 0 | 1 | mid | no | yes | 0 |

## T-0021 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 2425 | -80.50 | -80.15 | 66.89 | 0.36 | 43% | 0.85 | 82% | 24/31 | 1 |
| stressed | 2269 | -141.34 | -140.74 | 119.00 | 0.61 | 40% | 0.73 | 141% | 27/31 | 1 |
| diagnostic | 2516 | -1.36 | -1.36 | 0.56 | 0.00 | 46% | 1.00 | 20% | 19/31 | 1 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 1401 | -33.16 | 46% | +0.38 | -0.37 | 0.40 |
| short | 1024 | -47.35 | 39% | +0.51 | -0.40 | -0.04 |

Signals (base view): {'FILLED': 2425, 'REJECTED': 5380}. Not taken by reason: {'ENTRY_LIMIT_REACHED': 1, 'PAUSED': 394, 'RISK_LIMIT': 4975, 'SPREAD_TOO_WIDE': 10}. Exit reasons: {'STOP': 958, 'STOP_GAP': 2, 'TARGET': 281, 'TIME_EXIT': 1184}.

Continuous run over the development period from USD 100: 323 trades, net -9.03 USD.

## T-0022 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 2972 | -103.45 | -103.24 | 74.18 | 0.21 | 45% | 0.83 | 109% | 25/31 | 0 |
| stressed | 2857 | -150.97 | -150.60 | 134.09 | 0.37 | 43% | 0.76 | 154% | 27/31 | 0 |
| diagnostic | 3045 | -12.53 | -12.53 | 0.63 | 0.00 | 47% | 0.98 | 30% | 18/31 | 0 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 1264 | -42.55 | 47% | +0.41 | -0.43 | 0.26 |
| short | 1708 | -60.90 | 44% | +0.36 | -0.34 | -0.04 |

Signals (base view): {'FILLED': 2972, 'REJECTED': 4833}. Not taken by reason: {'ENTRY_LIMIT_REACHED': 52, 'NO_EXIT_PLAN': 1, 'PAUSED': 243, 'RISK_LIMIT': 4526, 'SPREAD_TOO_WIDE': 11}. Exit reasons: {'STOP': 1066, 'TARGET': 998, 'TIME_EXIT': 908}.

Continuous run over the development period from USD 100: 142 trades, net -7.23 USD.

## Comparators and selection

- Buy and hold (equal notional, long, financing paid) over the walk-forward test sessions: net +106.78 USD, max drawdown 18%.
- Cash: 0.00 USD.
- Reality check p-value 1.000, selection-adjusted lower bound -0.0559; deflated Sharpe 0.000 with 23 registered trials.

## Limits

- Financing rates are those read on 2026-11-10, applied to every year.
- Demo quotes may be tighter than live fills; entries are at the open of the hour after the decision.
- A stop is assumed to fill at its level plus slippage unless the market opens through it; a stop and a target touched in one hourly bar count as the stop.
- Backtest only. Practice results on the demo account are a separate record; neither is G2 or G3 evidence on its own.
