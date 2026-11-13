# Weekly momentum on crypto CFDs - long or short Bitcoin and Ether by the sign of the week

Generated 2026-11-10 23:18 UTC. Study `cfd_crypto_momentum_v1`, results digest `39dc946fb3c5535e`. BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence.

## Verdict

- **T-0019** (CFD_WEEKLY_MOMENTUM): 372 trades, net +19.51 USD on USD 100 windows, stressed +1.23 USD. Practice rule of ADR-0052: **traded**. Checks: base_net_positive yes, stressed_net_positive yes, walk_forward_net_positive yes.
- Walk forward: 225 trades over 2557 sessions, net +0.42 USD. G2: **NOT_PASSED** (oos_trades_at_least_100 yes, sessions_at_least_120 yes, effective_sessions_at_least_120 yes, positive_lower_bound_session_net no, positive_lower_bound_per_trade_net no, selection_adjusted_lower_bound_positive no).

## Data

Dataset `capital_demo_crypto_cfd_1h_v1`: the broker's hourly bid and ask, built into one bar per UTC day on the `UTC_24_7` calendar. Warm-up 30 days before each window. Splits: development 2018-03-01T00:00:00Z .. 2026-08-01T00:00:00Z, locked final test 2026-08-01T00:00:00Z .. 2026-11-01T00:00:00Z (unlocked: False).

Median quoted spread at the daily close, bps of the mid:

| instrument | daily bars | closed weekdays | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CAPITAL:BTCUSD | 3105 | 0 | 86.5 | 43.7 | 35.0 | 27.7 | 30.1 | 25.5 | 16.5 | 4.8 | 7.1 |
| CAPITAL:ETHUSD | 3105 | 0 | 118.2 | 95.0 | 59.2 | 34.4 | 32.9 | 29.5 | 19.4 | 5.9 | 8.4 |

## Costs

| view | slippage bps | spread x | fills | financing | credits | missed % |
|---|---|---|---|---|---|---|
| base | 1 | 1 | bid/ask | yes x1 | yes | 0 |
| stressed | 2 | 2 | bid/ask | yes x1.5 | no | 5 |
| diagnostic | 0 | 1 | mid | no | yes | 0 |

## T-0019 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 372 | +19.51 | +23.50 | 6.43 | 3.99 | 51% | 1.28 | 5% | 11/34 | 26 |
| stressed | 243 | +1.23 | +6.15 | 5.81 | 4.92 | 49% | 1.03 | 9% | 14/34 | 17 |
| diagnostic | 433 | +32.80 | +32.80 | 0.00 | 0.00 | 52% | 1.44 | 4% | 10/34 | 25 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 196 | +16.78 | 49% | +0.55 | -0.37 | 4.95 |
| short | 176 | +2.73 | 53% | +0.38 | -0.39 | -0.96 |

Signals (base view): {'FILLED': 372, 'REJECTED': 503}. Not taken by reason: {'RISK_LIMIT': 339, 'SPREAD_TOO_WIDE': 164}. Exit reasons: {'END_OF_DATA': 26, 'STOP': 37, 'TARGET': 13, 'TIME_EXIT': 296}.

Continuous run over the development period from USD 100: 376 trades, net +21.69 USD.

## Comparators and selection

- Buy and hold (equal notional, long, financing paid) over the walk-forward test sessions: net +239.50 USD, max drawdown 35%.
- Cash: 0.00 USD.
- Reality check p-value 0.091, selection-adjusted lower bound -0.0011; deflated Sharpe 0.909 with 19 registered trials.

## Limits

- Financing rates are those read on 2026-11-10, applied to every year.
- Demo quotes may be tighter than live fills; entries are at the daily bar's open (00:00 UTC, or the Sunday-evening open), where spreads are often wider than in the US session.
- A stop is assumed to fill at its level plus slippage unless the market opens through it; a stop and a target touched in one daily bar count as the stop.
- Backtest only. Practice results on the demo account are a separate record; neither is G2 or G3 evidence on its own.
