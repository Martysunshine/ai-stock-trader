# Machine-learning and LLM research guide

For whoever does the modelling work (milestone M3 and later). It assumes you know Python and ML; it explains what this project allows a model to do, what has been learned already, how to get data, and what counts as evidence. Set up your machine first with [CONTRIBUTING.md](../CONTRIBUTING.md).

## 1. What a model is allowed to do

The specifications are explicit (strategy_and_profitability.md §6-§7, instructions.md §3 and §5, CRYPTO_TRADING_TRACK.md §5, risk_and_validation.md §5):

- **A model is a candidate filter.** A frozen rule (Candidate A trend continuation, Candidate B mean reversion) proposes a trade; the model estimates whether taking it is profitable *after all costs* and may veto it. It never sizes positions, changes risk limits, or reaches a broker. Deterministic risk code does the sizing, and `tests/test_boundaries.py` enforces the separation.
- **Start simple.** Begin with regularised logistic regression for "net PnL after costs > 0" and a simple regression for gross executable return. Try a gradient-boosted challenger (LightGBM) only if it improves *economic* results. scikit-learn comes first; no GPU is needed.
- **Choose the label before fitting:** entry at the next feasible price after the decision, then the rule's stop, target and time exit. Accuracy, win rate and Brier score are diagnostics; selection is by net economics.
- **Models are frozen during trading.** Retraining produces a challenger, which is a new registered trial and never an automatic replacement.
- **LLMs are offline research tools only.** An LLM may extract categories and facts (earnings, guidance, financing, acquisitions) from properly licensed, timestamped news, and must be allowed to answer "unknown". It must not invent price forecasts, it never runs inside the trading loop, and the trading loop's paid-LLM budget is zero. Historical LLM tests are contaminated, because the model may already know what happened later, so LLM-derived features need forward (paper) evidence. News work starts only after the price-only pipeline works. In short: an LLM can supply a *feature* that a filter model tests; it cannot be the thing that trades.

## 2. What we already know

From [research/reports/baseline_report.md](../research/reports/baseline_report.md) (BTC/ETH, 15-minute bars, Nov 2023 to Aug 2026):

- Neither rule family has a gross edge. With zero commission every trial is still negative; the best mid-price gross is about USD 0.0006 per trade.
- Kraken Tier 1 taker fees are 0.80% per side, so a selected trade must win about 1.6% *more than it loses* per round trip just to break even. A filter can only choose a subset of candidates; it cannot create an edge the candidates do not contain.
- At USD 50, the smallest legal Kraken order plus fees can exceed the USD 0.125 risk budget, which blocks trading entirely in some periods.

Implication: stocks are the more promising ground for modelling, because Alpaca charges no commission. Stock data is now in the manifest (`alpaca_us_equities_5m_v1`, IEX 5-minute bars), and the stock baseline study (trials T-0006..T-0010, `configs/research/stocks_baseline_v1.yaml`) was registered on 2026-11-08; its results are pending. Read its report before designing stock filters. "No commission" is not "no cost": the conservative Alpaca cost basis rounds each regulatory fee up to the cent per order, so a USD 10 round trip costs USD 0.04, or 0.4% (ADR-0028).

## 3. Getting data

Always load through the manifest; it verifies the content checksum before you use anything:

```python
from pathlib import Path
from trader.data.manifest import load_manifest
from trader.data.ingest import load_bars

manifest = load_manifest(Path("research/data_manifest.json"))
ds = manifest.dataset("coinbase_btc_eth_usd_15m_v1")
bars = load_bars(ds, "COINBASE:BTC/USD", Path("."))   # list[HistoryBar]: exact Decimal OHLCV
```

For quick exploration in a DataFrame (no checksum check; not for registered runs):

```python
import duckdb
df = duckdb.sql(
    "SELECT * FROM 'data/history/coinbase_btc_eth_usd_15m_v1/COINBASE__BTC_USD.parquet'"
).df()   # columns: start_ms (UTC epoch ms, bar start), open, high, low, close, volume, vwap, trade_count
```

Rules that keep research honest:

- A bar `[start, start + 15 min)` may only be used from `end + 1 s` (`Bar.available_time`). Never use a bar's close as a fill price.
- Compute features with `src/trader/features/streaming.py` (`RollingBarFeatures`), or a new module with its own version string, so research and a live process compute identical values. Floats are fine inside features; money, prices and quantities stay `Decimal`.
- Coinbase supplies no VWAP or trade count. Do not substitute one.
- `research/data_manifest.json` lists every dataset, its gaps and its limitations. `docs/data_coverage.md` summarises them.

**Daily bars** (`alpaca_us_etfs_daily_sip_v1`: 16 ETFs, consolidated SIP feed, 2016-2026; `"timeframe": "1Day"` in the manifest request) load the same way, `load_bars(ds, "ARCX:SPY", Path("."))`. They differ from intraday bars in four ways (`src/trader/data/daily_bars.py`):

- One bar per NYSE regular session: `start` is the session open, `end` the close (13:00 New York on early closes). A bar's `minutes` is its session's length, 390 or 210, so tell daily data by the dataset's `timeframe`, never by `minutes == 390`. Parquet stores only the start; the length comes from the exchange calendar on read.
- A decision on a completed day (usable from the close + 1 s) is filled at the **next session's open**.
- Open, high, low and close are regular-session prices. Volume, trade count and VWAP also count pre-market and after-hours trades, so they are final only 4 hours after the close. A feature that reads them at the close uses later trades: say so, or do not use them.
- Prices are raw: ex-dividend drops are price moves and dividends are not credited. Splits and symbol changes are applied point-in-time as for 5-minute data. A missing session is listed under the file's `quality.missing_sessions` and is never filled.

## 4. The candidate pipeline that exists

The M3 tooling is built and tested on synthetic data (ADR-0029, ADR-0030, ADR-0034). Read the code and its tests before adding to it.

| Step | Code | What it does |
| --- | --- | --- |
| Features | `features/candidate_features.py` | Version `candidate-v1`: 15 fixed columns for both rule families, computed from the snapshot the rule hashed into the candidate (the hash is checked first) and the candidate's own fields. Distances to the reference, prior high, stop and session VWAP in ATR units; volatility ratios; efficiency ratio; context and 3-bar returns; reward:risk; time of day and weekday (New York time for stocks, UTC for crypto). A missing input gets a default plus an indicator (`streaming_missing`, `session_missing`), so every value is finite. Another version is refused. |
| Labels | `research/labels.py` (`build_labels`, `LabelSet`) | Runs a fresh frozen rule over the bars and keeps **every** in-window candidate with its verified snapshot and features. Each one is simulated **alone** from a fresh USD 50 ledger through `ResearchEngine` (same fills and costs), giving status, net USD, net return and a win flag; rejected and unfilled candidates are kept. The window and every bar pass `guard_window`, so the locked final test is refused. Parquet through DuckDB, read back and compared on write. |
| Filter hook | `research/engine.py`, `research/filters.py` | `ResearchEngine(..., candidate_filter=, feature_fn=)` asks the filter right after the stale-data check, before pauses, limits and sizing. A veto is `NO_EDGE` with binding `filter:<id>`; a filter cannot size. `ModelThresholdFilter` accepts when the probability is at or above the threshold. Controls: `HashRandomReject` (random rejection at a given rate) and `TurnoverMatchedReject` (equal accepted-candidate counts per window). |
| Training | `research/training.py` (`purge`, `train_fold`) | Purges and embargoes by the maximum hold, fits the scaler and model on training rows only, picks the grid point by validation log-loss, fits Platt or isotonic calibration on validation scores, and picks the threshold on validation by net economics with a minimum number of accepts. Every step goes into `selection_log`. |
| Bundle | `models/bundle.py` | The trained filter as checksummed JSON (`ModelBundle`): features, family, training window, calibration, threshold, seed, library versions and data hashes. Scored with numpy alone, identical to scikit-learn and LightGBM; tampering is detected. |
| Study and report | `research/model_study.py`, `research/model_report.py` | A `kind: model_study` config names a registered base study (data, costs, splits) and lists filter trials. Per fold it trains, selects on validation, then replays each test window from a fresh USD 50 ledger four ways: rule only, rule plus filter, random rejection at the filter's veto rate, equal accepted counts. A fold that cannot be trained vetoes everything (cash), never the unfiltered rule. |

Commands (both refuse an unregistered or changed trial before reading data; `labels` always refuses the locked final test):

```powershell
uv run trader research labels --study configs/research/crypto_filter_v1.yaml --trial T-0011 --out data/labels/t0011.parquet
uv run trader research models --study configs/research/crypto_filter_v1.yaml   # results, bundles, report, RESULT events
```

On synthetic data, call the library directly. `tests/model_study_fixtures.py` builds a seeded random-walk workspace with its own manifest, base study and model study; `tests/test_model_study.py` shows a full run on it.

## 5. Evaluating a model: what counts as evidence

- **Splits.** Use chronological walk-forward folds from `research/splits.py` (12 months train, 3 validation, 3 test, rolling by 3), purged and embargoed by the maximum holding horizon (6 hours for crypto, 60 minutes for stocks). Fit scaling, feature selection, hyperparameters and calibration on train and validation only. Never use a random split. `research/training.py` enforces this; a new model type goes through `train_fold`, not around it.
- **Comparison.** Rule-only against rule-plus-filter in the same USD 50 engine with the same costs, plus random rejection at the same rate and equal accepted-candidate counts, so that trading less is not mistaken for skill (strategy §6). `research models` runs all four per test window. Equal accepted counts do not mean equal trades or USD turnover, because pauses and limits apply after the filter; the report shows each variant's trades.
- **Integration point.** Research uses the filter hook above. The runtime has no model scorer yet: the paper bot uses `NoEdgeEstimator`, so every candidate is rejected `NO_EDGE`. A challenger reaches the runtime only through an `EdgeEstimator` (`strategies/edge.py`) that scores a `ModelBundle` and returns `SignalScore` with evidence `BACKTEST_ONLY` (accepted in replay, shadow and paper) or `VALIDATED_BUNDLE` (the only evidence accepted live, after gates G2-G4).
- **Report.** `research models` writes the report: `research/reports/model_report.md` for `crypto_filter_v1` (roadmap §2), otherwise `research/reports/<study_id>_report.md`. It covers the verdict, chronological splits, the full tuning history, calibration (Brier, log-loss, reliability bins, ECE on test-window labels), the economic ablation and selection-aware uncertainty (a reality check across every variant, deflated Sharpe with N = all registered trials, paired bootstrap differences). Validation metrics are in-sample for the calibration and the threshold; only the test windows count as evidence.
- **Gate G2.** At least 100 out-of-sample trades over 120 sessions, adequate effective sample size, and a selection-aware positive lower confidence bound on net expectancy. A weak or negative result fails the gate even if the software works. "The filter cannot find a profitable subset" is a documented rejection, and a valid M3 outcome.

## 6. Registration, budget and who writes the research log

- Every evaluated configuration on real data is a registered trial: model type, feature set, hyperparameter grid, calibration, threshold rule, controls, universe. Register it *before* running. A model trial's registration also binds the training library versions (Python, numpy, scikit-learn, and LightGBM when used), so register and run in the same environment; after an upgrade, `research models` refuses the trial and a new one is needed.
- **Budget: 15 trials project-wide** (owner decision of 2026-11-08, after the crypto baseline):

  | Trials | Use | State |
  | --- | --- | --- |
  | 5 | Crypto baseline (T-0001..T-0005) | Run: no edge, G2 failed |
  | 5 | Stock baseline (T-0006..T-0010) | Registered 2026-11-08; results pending |
  | 2 | Crypto filters (T-0011, T-0012: logistic filters on crypto Candidates A and B, `configs/research/crypto_filter_v1.yaml`) | Declared; no results yet |
  | 3 | Stock filters | Reserved; chosen after the stock baseline results |

  Every evaluated variant counts. Only the owner changes the budget (`trader research decide-budget`).
- **The research log has one writer, the owner** ([CONTRIBUTING.md](../CONTRIBUTING.md) §4). In a fork or on your own branch, do not run `trader research register`, `baseline`, `models`, `note`, `decide-budget` or `unlock-holdout` (`models` appends RESULT events).
- **Contributors** work on their own branch (`yourname/<topic>`): labels, features, filters and evaluation, built and tested on synthetic data. Exploration is not evidence. To run a design on real data, send the owner the trial configuration (a `kind: model_study` YAML); the owner registers and runs it. Every real-data trial uses budget, so agree it first.

## 7. Dependencies and tools

- scikit-learn and LightGBM are already in the `ml` dependency group: installed for development, never a `[project]` dependency (ADR-0027). Import them only under `research/`; `models/` scores bundles with numpy alone, and `tests/test_boundaries.py` enforces both rules. Add another modelling library with `uv add --group ml <package>` and record it in `docs/dependency_evaluation.md`.
- **LLM and HTTP client libraries must never enter the runtime `[project]` dependencies.** A test fails if `openai`, `anthropic`, `langchain` or `transformers` appear there. Use a separate group (`uv add --group llm <package>`) and keep LLM code outside the trading runtime, for example under `research/llm/`.
- LLM API keys follow the same rules as broker keys: your own, in environment variables, never committed. Any paid API use needs the owner's agreement on budget first.
- Notebooks belong under `research/notebooks/`, with outputs cleared before committing.

## 8. Daily-bar rules: what the research engine supports

Daily bars are one bar per XNYS regular session (`start` = the open, `end` = the close or early close, usable from `end + 1 s`); a decision on a completed day's close is filled at the next session's open by the ordinary fill model. Everything below is opt-in, so the registered trials T-0001..T-0012 re-run identically (the pinned hashes and the byte-identical crypto report prove it). Decision record: "Daily bars, overnight holds, exit signals and capital views" in [decisions.md](decisions.md).

| Need | How a rule or study declares it | What the engine does |
| --- | --- | --- |
| Hold across sessions | `holding: OVERNIGHT` on the rule or its spec | No session-close exit. The entry is a next-open order: it fills only in the bar at the next session's open (missing bar = missed entry, `EXPIRED_NO_BAR_AT_NEXT_OPEN`), at that open; a candidate is admitted when that open is inside the deployment window. An entry bar that opens through the stop sells at that open (`STOP_GAP`). Same-time bars run in two phases (fills and exits first, then decisions), so a fill never blocks its own session's decisions |
| Exit on a signal | `exit_signal(instrument_id, bar) -> str \| None` on the rule | Called after `on_bar` for every completed bar of a held instrument; a reason sells at the next open as `SIGNAL:<reason>` |
| Time exit in sessions | `max_hold_sessions: N` | Sold at the first open at or after the `N`-th session after the entry session (holidays skipped); `max_hold_minutes` is then ignored |
| A target that is only a cost gate | `target_role: COST_GATE` | The target feeds the sizer's cost-coverage gate (ADR-0019) and is never a resting exit |
| Long lookbacks | `warmup_sessions: N` in the study | `N` sessions of bars before every window feed the features only (default: two calendar days, ADR-0020) |
| Capital sensitivity | `diagnostic_capitals: ["1000", "10000"]` in the study | Each trial's base view re-run with every USD amount of the policy scaled (`config.policy.scale_policy`); report section 14, never used by selection or G2 |
| A trial on part of the study universe | `instruments: [ARCX:SPY, ...]` on the trial (study execution ids) | The registration's universe and data hashes, and the trial's runs, cover only those instruments; absent, every study instrument (unchanged). The buy-and-hold comparator stays the whole study universe. Used by `etf_daily_v1` T-0013 (13 equity ETFs of 16) |

- **Features.** `features/daily.py` (`daily-v1`): exact running-sum SMA, Wilder RSI and ATR (12-dp half-even quantisation, documented), month ends from the calendar, `snapshot()` for `snapshot_hash`. A missing session resets the state; weekends, holidays and early closes do not. A split rescales the state instead of resetting it.
- **A new family.** Add a `RULE_FAMILY = RuleFamily(...)` to the family's module and one line to `FAMILY_MODULES` in `strategies/registry.py`; `resolve_rule` then loads its spec and `build_strategy` its rule. The registration's feature list comes from `RuleFamily.features`.
- **Synthetic data for tests.** `tests/daily_fixtures.py` builds five years of deterministic daily bars for the 16-ETF basket (dips in uptrends, a bear market, a gap, two splits, early closes, one missing session), fractional specs, an in-memory study context, and `ScriptedDailyRule`. Never evaluate a daily rule on real data before its trial is registered on `sunshine`.
- **Not yet supported for daily rules:** candidate labels and model studies (labels purge by `max_hold_minutes`), and the paper runtime (it has no next-open orders, overnight holding or exit signals).

### 8.1 Candidate C: dip-buy in an uptrend

`strategies/dip_buy.py` with `strategies/specs/candidate_c_etf_daily_v1.yaml` (family `CANDIDATE_C`) implements the 2-period RSI strategy of Connors and Alvarez, *Short Term Trading Strategies That Work* (2008), with the published parameters and nothing tuned. Decision record: "Candidate C: dip-buy in an uptrend".

- **Signal.** On a completed session: close above SMA(200) and Wilder RSI(2) below 10, both strict. Only the 13 equity ETFs (`universe_symbols`, matched on the symbol after `VENUE:`); bonds and gold in a shared study are ignored.
- **Orders.** The entry is at the next session's open, using the engine's marketable limit (estimated ask + 5 bps). A close above SMA(5) sells at the next open (`SIGNAL:close_above_sma_5`). The time exit comes after 10 sessions. A disaster stop sits 3 × ATR(10) below the signal close. The target, SMA(5) at the signal, is only a cost gate (`target_role: COST_GATE`), never a profit exit. The rule is long only, with one position per instrument.
- **Departures from the book**, declared before any run: it buys and sells at the close, has no stop and no time limit. Here a next open more than about 3 bps above the signal close means a missed entry (`LIMIT_NOT_MARKETABLE_AT_NEXT_OPEN`).
- **Studies** need `warmup_sessions: 200` (`DipBuySpec.min_warmup_sessions`). Without it, nothing can trade until 200 sessions into each window.
- **Tests** in `tests/test_candidate_c.py`, on synthetic data only: a hand-computed RSI(2)/SMA(200) example, a batch recomputation, no lookahead, and an end-to-end run that produces signal exits, stops, gap stops and time exits.

### 8.2 Candidate D: trend timing (Faber, 2007)

`strategies/trend_timing.py`, frozen spec `strategies/specs/candidate_d_etf_monthly_v1.yaml` (family `CANDIDATE_D`, resolved through `FAMILY_MODULES`; tests in `tests/test_trend_timing.py`, synthetic data only). Not registered and never run on real data.

- **Rule.** Evaluated only at a month-end session's completed close (the last XNYS session of the month, from the calendar). Close above the SMA of the last 10 month-end closes: a long candidate, filled at the next open when flat. Close below it: `exit_signal` sells at the next open (`SIGNAL:CLOSE_BELOW_MONTHLY_SMA`). Universe: the 16 ETFs.
- **Additions the USD 50 policy needs** (declared before any data, not tuned): a disaster stop at signal close minus 4 x ATR(20), a 3R target that stays a resting exit (`target_role: EXIT`), a 260-session time exit, and a 1% collar on the next-open order (`entry_limit_offset_bps: 100`).
- **Fail closed.** A missing session resets the features. There is then no entry for ten month ends, and a held position is sold after the next month end (`SIGNAL:MONTHLY_SMA_UNAVAILABLE`). A split rescales the state, so it never looks like a trend break.
- **Study settings.** Set `warmup_sessions: 230` or more. While a position is held, the month-end candidates are rejected `instrument_busy`; for this rule that means "keep holding", not a missed trade. The independent three-month windows (ADR-0020) close every position at each window end, so the 260-session time exit can bind only in the continuous deployment.
