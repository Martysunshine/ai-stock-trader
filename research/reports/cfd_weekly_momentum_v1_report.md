# Weekly momentum on commodity, currency and index CFDs - long or short by the sign of the week

Generated 2026-11-11 17:11 UTC. Study `cfd_weekly_momentum_v1`, results digest `304c1d2a977b1256`. BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence.

## Verdict

- **T-0025** (CFD_WEEKLY_MOMENTUM): 693 trades, net -14.43 USD on USD 100 windows, stressed -41.55 USD. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive no, stressed_net_positive no, walk_forward_net_positive no.
- Walk forward: 293 trades over 1628 sessions, net -7.34 USD. G2: **NOT_PASSED** (oos_trades_at_least_100 yes, sessions_at_least_120 yes, effective_sessions_at_least_120 yes, positive_lower_bound_session_net no, positive_lower_bound_per_trade_net no, selection_adjusted_lower_bound_positive no).

## Data

Dataset `capital_demo_cfd_1h_v1`: the broker's hourly bid and ask, built into one bar per UTC day on the `BROKER_CFD` calendar. Warm-up 60 days before each window. Splits: development 2019-01-01T00:00:00Z .. 2026-08-01T00:00:00Z, locked final test 2026-08-01T00:00:00Z .. 2026-11-01T00:00:00Z (unlocked: False).

Median quoted spread at the daily close, bps of the mid:

| instrument | daily bars | closed weekdays | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CAPITAL:GOLD | 2008 | 13 | 2.1 | 2.1 | 4.4 | 1.7 | 1.1 | 1.6 | 1.5 | 0.9 | 1.1 |
| CAPITAL:SILVER | 2008 | 13 | 14.0 | 13.5 | 12.9 | 12.1 | 9.5 | 8.6 | 10.0 | 7.9 | 10.1 |
| CAPITAL:OIL_CRUDE | 2009 | 12 | 18.9 | 10.2 | 21.5 | 6.3 | 3.5 | 3.9 | 4.3 | 5.1 | 5.0 |
| CAPITAL:EURUSD | 2017 | 4 | 3.5 | 0.7 | 0.5 | 0.7 | 0.6 | 0.6 | 0.6 | 0.5 | 0.6 |
| CAPITAL:US500 | 2010 | 11 | 1.9 | 2.4 | 1.9 | 1.6 | 1.8 | 1.8 | 1.5 | 0.8 | 0.8 |
| CAPITAL:US100 | 2010 | 11 | 2.4 | 2.2 | 2.0 | 1.3 | 1.3 | 1.3 | 1.0 | 0.8 | 0.7 |
| CAPITAL:US30 | 2010 | 11 | 3.1 | 1.9 | 0.9 | 1.4 | 0.9 | 0.6 | 0.6 | 0.5 | 0.4 |
| CAPITAL:RTY | 1939 | 9 | - | 2.5 | 3.4 | 2.4 | 1.8 | 2.8 | 2.4 | 2.2 | 1.8 |

## Costs

| view | slippage bps | spread x | fills | financing | credits | missed % |
|---|---|---|---|---|---|---|
| base | 1 | 1 | bid/ask | yes x1 | yes | 0 |
| stressed | 2 | 2 | bid/ask | yes x1.5 | no | 5 |
| diagnostic | 0 | 1 | mid | no | yes | 0 |

## T-0025 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 693 | -14.43 | -2.06 | 14.47 | 13.61 | 48% | 0.92 | 22% | 18/31 | 566 |
| stressed | 664 | -41.55 | -18.07 | 21.32 | 24.19 | 46% | 0.80 | 46% | 20/31 | 405 |
| diagnostic | 699 | +21.48 | +20.39 | 0.16 | 0.00 | 51% | 1.10 | 10% | 14/31 | 617 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 405 | +10.99 | 52% | +0.58 | -0.56 | 15.86 |
| short | 288 | -26.65 | 44% | +0.55 | -0.60 | -2.25 |

Signals (base view): {'FILLED': 693, 'REJECTED': 2389}. Not taken by reason: {'RISK_LIMIT': 2292, 'SPREAD_TOO_WIDE': 97}. Exit reasons: {'END_OF_DATA': 50, 'STOP': 35, 'TARGET': 1, 'TIME_EXIT': 607}.

Continuous run over the development period from USD 100: 92 trades, net -7.58 USD.

## Comparators and selection

- Buy and hold (equal notional, long, financing paid) over the walk-forward test sessions: net +93.81 USD, max drawdown 15%.
- Cash: 0.00 USD.
- Reality check p-value 0.622, selection-adjusted lower bound -0.0215; deflated Sharpe 0.410 with 24 registered trials.

## Limits

- Financing rates are those read on 2026-11-10, applied to every year.
- Demo quotes may be tighter than live fills; entries are at the daily bar's open (00:00 UTC, or the Sunday-evening open), where spreads are often wider than in the US session.
- A stop is assumed to fill at its level plus slippage unless the market opens through it; a stop and a target touched in one daily bar count as the stop.
- Backtest only. Practice results on the demo account are a separate record; neither is G2 or G3 evidence on its own.
