# Index dip on index CFDs - the frozen Candidate C rule on Capital.com's own US index quotes

Generated 2026-11-10 23:17 UTC. Study `cfd_index_dip_v1`, results digest `14ded957fded7207`. BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence.

## Verdict

- **T-0018** (CFD_INDEX_DIP): 122 trades, net +4.40 USD on USD 100 windows, stressed -0.02 USD. Practice rule of ADR-0052: **not traded**. Checks: base_net_positive yes, stressed_net_positive no, walk_forward_net_positive yes.
- Walk forward: 53 trades over 1623 sessions, net +1.22 USD. G2: **NOT_PASSED** (oos_trades_at_least_100 no, sessions_at_least_120 yes, effective_sessions_at_least_120 yes, positive_lower_bound_session_net no, positive_lower_bound_per_trade_net no, selection_adjusted_lower_bound_positive no).

## Data

Dataset `capital_demo_cfd_1h_v1`: the broker's hourly bid and ask, built into one bar per UTC day on the `BROKER_CFD` calendar. Warm-up 400 days before each window. Splits: development 2019-01-01T00:00:00Z .. 2026-08-01T00:00:00Z, locked final test 2026-08-01T00:00:00Z .. 2026-11-01T00:00:00Z (unlocked: False).

Median quoted spread at the daily close, bps of the mid:

| instrument | daily bars | closed weekdays | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CAPITAL:US500 | 2227 | 12 | 2.5 | 2.4 | 1.9 | 1.6 | 1.8 | 1.8 | 1.5 | 0.8 | 0.8 |
| CAPITAL:US100 | 2227 | 12 | 2.0 | 2.2 | 2.0 | 1.3 | 1.3 | 1.3 | 1.0 | 0.8 | 0.7 |
| CAPITAL:US30 | 2227 | 12 | 2.1 | 1.9 | 0.9 | 1.4 | 0.9 | 0.6 | 0.6 | 0.5 | 0.4 |
| CAPITAL:RTY | 1939 | 9 | - | 2.5 | 3.4 | 2.4 | 1.8 | 2.8 | 2.4 | 2.2 | 1.8 |

## Costs

| view | slippage bps | spread x | fills | financing | credits | missed % |
|---|---|---|---|---|---|---|
| base | 1 | 1 | bid/ask | yes x1 | yes | 0 |
| stressed | 2 | 2 | bid/ask | yes x1.5 | no | 5 |
| diagnostic | 0 | 1 | mid | no | yes | 0 |

## T-0018 by view

| view | trades | net USD | gross USD | spread+slip USD | financing USD | win | PF | max DD | losing windows | too large |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 122 | +4.40 | +7.85 | 1.63 | 3.45 | 67% | 1.17 | 5% | 12/31 | 9 |
| stressed | 118 | -0.02 | +5.04 | 3.10 | 5.05 | 64% | 1.00 | 6% | 14/31 | 11 |
| diagnostic | 123 | +10.01 | +10.01 | 0.05 | 0.00 | 70% | 1.41 | 5% | 10/31 | 8 |

Long and short (base view):

| side | trades | net USD | win | avg win | avg loss | financing |
|---|---|---|---|---|---|---|
| long | 122 | +4.40 | 67% | +0.37 | -0.66 | 3.45 |
| short | 0 | +0.00 | - | - | - | 0.00 |

Signals (base view): {'FILLED': 122, 'REJECTED': 347}. Not taken by reason: {'RISK_LIMIT': 347}. Exit reasons: {'END_OF_DATA': 5, 'SIGNAL:close_above_sma_5': 95, 'STOP': 7, 'TIME_EXIT': 15}.

Continuous run over the development period from USD 100: 118 trades, net +3.99 USD.

## Comparators and selection

- Buy and hold (equal notional, long, financing paid) over the walk-forward test sessions: net +88.54 USD, max drawdown 21%.
- Cash: 0.00 USD.
- Reality check p-value 0.065, selection-adjusted lower bound -0.0004; deflated Sharpe 0.909 with 19 registered trials.

## Limits

- Financing rates are those read on 2026-11-10, applied to every year.
- Demo quotes may be tighter than live fills; entries are at the daily bar's open (00:00 UTC, or the Sunday-evening open), where spreads are often wider than in the US session.
- A stop is assumed to fill at its level plus slippage unless the market opens through it; a stop and a target touched in one daily bar count as the stop.
- Backtest only. Practice results on the demo account are a separate record; neither is G2 or G3 evidence on its own.
