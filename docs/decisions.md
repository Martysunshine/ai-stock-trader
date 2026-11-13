# Architecture decision records

instructions.md §2 requires recording deviations and interpretations "with their effects on costs, risk, and testing". Each record below is short: **Context, Decision, Effect.**

## ADR-0001 - SQLite allowed for replay only

**Context.** PostgreSQL is the runtime store. Replay runs are single-process and disposable.

**Decision.** `RuntimeConfig` rejects SQLite for any mode other than replay. Exact decimals are stored as canonical TEXT on SQLite and NUMERIC(38,18) on PostgreSQL. Both are quantised identically, so the two backends produce the same hashes (tested).

**Effect.** Replay is fast and needs no services. Paper and live require PostgreSQL. Advisory-lock and concurrency invariants are tested on PostgreSQL only.

## ADR-0002 - Track profiles instead of editing the global instrument flags

**Context.** The policy's `ordinary_equities_only` and `long_only` flags describe the stock track. The addenda allow "explicitly named profiles".

**Decision.** `configs/tracks/*.yaml` may only set product types, directions, calendar and cadence. The schema forbids numeric risk keys. The STOCKS profile is validated to stay long-only ordinary equities on 5m/15m bars. Crypto and commodities are disabled by default.

**Effect.** There is one numeric policy and no competing copies.

## ADR-0003 - Synthetic sessions and a declared fixture edge for the demonstrator

**Context.** No historical data or validated model exists, yet the demonstrator needs a cost-aware score.

**Decision.**
- The synthetic manifests declare an explicit edge assumption.
- `FixtureEdgeEstimator` works in replay only, and only against the matching synthetic manifest. Its scores carry `SYNTHETIC_FIXTURE` evidence, which the risk engine refuses in shadow, paper and live.
- With no estimator, every candidate is `NO_EDGE`.
- Demonstrator runs are not research trials and do not count toward the 10-trial budget.

**Effect.** Reports carry a "NOT evidence of profitability" watermark.

## ADR-0004 - Loss windows and session counting across tracks

**Decision.**
- Stock accounts use New York session and New York ISO-week windows. The portfolio additionally uses UTC day and Monday-00:00-UTC week windows. All must pass.
- `max_new_entries_per_session` uses the larger of the track's session count and the portfolio's UTC-day count.
- Drawdown and the USD 45 floor use cash-flow-adjusted equity, so a deposit can never hide a loss.
- Window start equity is recorded at the first observation in the window, even when flat. If the process was down across a boundary, the higher of the last snapshot and the first observation is used (conservative).

## ADR-0005 - Monitored synthetic exits in replay; native protection required for live

**Decision.** The replay broker has no native stops. The exit manager monitors stop, target, maximum hold, the session close procedure and loss breaches, and sends reducing market sells. Live admission requires `native_protective_stop` at `LIVE_CONFIRMED` (the policy default).

**Effect.** Synthetic exits cannot protect during a full outage. This is documented, not hidden.

## ADR-0006 - No resubmission of an unresolved intent

**Decision.**
- A timeout is an unknown outcome. The intent is resolved only by its persisted client order ID.
- Non-acceptance is established only after the adapter's certified lookup count **and** its certified visibility window (plus the maximum plan validity) have elapsed.
- Even then, the same candidate is never resubmitted. A later bar may produce a fresh decision.

**Effect.** Some opportunities are missed, but no duplicate exposure is possible.

## ADR-0007 - CFD leverage expressed through notional only

**Context.** `leverage_enabled: false`, yet CFDs are margined.

**Decision.** CFD sizing uses the full underlying notional `q·m·P·x` against the 20%/40% caps. Margin is used only for the cash check: margin plus planned loss must stay outside the reserve. CFDs remain replay/demo only.

## ADR-0008 - Calendars and settlement

**Decision.** Session times come from `exchange_calendars` XNYS. US T+1 settlement counts business days that are both NYSE sessions and not Federal Reserve holidays (Columbus Day, Veterans Day). Where the broker reports settled cash, that takes precedence.

## ADR-0009 - Deterministic IDs and canonical hashing

**Decision.** IDs are UUIDv5 values derived from logical inputs, and the replay clock drives every timestamp. Wall-clock reads and `random` are banned outside `ops/clock.py` and `data/synthetic.py` (ruff TID251 plus an AST test). Hashes exclude autoincrement IDs.

## ADR-0010 - One pending entry enforced by the database

**Decision.** In addition to the engine check, a partial unique index allows only one `ACTIVE` `ENTRY` reservation per portfolio. The policy loader therefore requires `max_pending_entry_orders == 1`.

**Effect.** Raising that limit later would need a migration and a new decision.

## ADR-0011 - Python 3.12 via uv on a 3.13 host

**Decision.** The project targets Python 3.12 (instructions §3) through `uv`'s managed interpreter. The host has 3.13.5; `requires-python` is `>=3.12,<3.14`.

## ADR-0012 - Calendar-relative entry window

**Decision.** Entries are allowed in `[open + 30 min, close - 30 min)`. The close procedure starts at `close - 15 min`. On a regular day that is 15:30 and 15:45 New York; on the 13:00 early close used by the demonstrator it is 12:30 and 12:45.

## ADR-0013 - Policy overlay limited to mode and live_enabled

**Decision.** A mode configuration may override only `mode` and `live_enabled`. The policy hash covers the merged result.

## ADR-0014 - Synchronous broker contract for M1

**Context.** architecture.md §4 proposes an asynchronous interface.

**Decision.** M1 uses a synchronous protocol, which keeps the deterministic replay loop single-threaded and avoids Windows async-driver issues. The M4 streaming adapter will feed WebSocket events into `drain_events` from a background thread.

## ADR-0015 - Concentration tags

**Decision.** `exposure_tags` mark shared underlying exposures (`GOLD`, `BTC`), and each tag is capped like a single symbol at 20%. A gold miner, a gold ETC and a gold CFD therefore never count as diversifying one another. Broad sector labels are not tags; sector concentration is disclosed instead.

## ADR-0016 - Submission authority rotates on restart

**Decision.** Each new `SubmissionGateway` binds a fresh authority object to its adapters. A stale worker's gateway can no longer submit, which complements the database fencing token.

## ADR-0017 - Bar-based research engine beside the database replay runner

**Context.** Research needs years of bars; the M1 replay runner processes quotes through the database and is built for correctness under faults, not speed.

**Decision.** `research/engine.py` replays completed bars in memory with the *same* policy, sizer, fee schedules, instrument specs and strategy code as the runtime, on a USD 50 ledger. Fills are deliberately conservative (next bar's open with spread and slippage, limit respected, stop before target, participation caps, missed entries). The stock Candidate A runs through `SessionWindowAdapter`, and a test proves the research path produces exactly the runtime's candidates.

**Consequence.** Bar-only research can reject a strategy but cannot certify execution realism (risk §6).

## ADR-0018 - Coinbase public candles as a price proxy for Kraken

**Context.** Kraken is the first crypto adapter candidate, but its public OHLC endpoint returns only 720 recent rows, and the deep archive needs a manual download.

**Decision.** The crypto baseline uses three years of Coinbase Exchange public candles for prices, Kraken's public pair metadata for increments and minimums, and the documented Kraken Pro Tier 1 fee table (0.40% / 0.80%) as the cost basis. The Coinbase-to-Kraken basis is measured on a common recent week and reported. Coinbase Advanced fees could not be verified (conflicting public reports) and are recorded as unknown.

## ADR-0019 - Research cost gate is not an edge estimate

**Decision.** Rule baselines have no validated edge. The research engine passes the planned target distance to the sizer as a *cost-coverage gate*: the target must cover round-trip costs at the exact size. This is never an `EdgeEvidence` the runtime accepts; paper and live still need a validated estimator.

## ADR-0020 - Out-of-sample windows are independent deployments

**Context.** The first baseline run simulated each trial continuously for 33 months. The research rule that a drawdown or floor pause stops new entries made every trial stop within months, so later walk-forward windows were empty.

**Decision.** Each 3-month window (the walk-forward step, matching every validation and test window) is an independent deployment from a fresh USD 50 ledger with two days of feature warm-up; positions still open at a window end are closed at the last bar's estimated bid. The continuous deployment is still reported, as the date the policy pause would have stopped the strategy for owner review. Rules, parameters, costs, data and splits were unchanged; the defect and the fix are recorded as a NOTE in the experiment registry.

## ADR-0021 - Trial budget allocation

**Decision.** The ten-configuration budget is project-wide (risk §5). Five trials were registered for the crypto study: Candidate A's 2 x 2 grid and one Candidate B. Five remain for stock baselines once data exists. Model filters (M3) need a recorded `BUDGET_DECISION` after the owner reviews the M2 evidence and data adequacy.

## ADR-0022 - Historical storage: DuckDB and Parquet with content hashes

**Decision.** Bars are stored as exact `DECIMAL(38,12)` Parquet through DuckDB (no pandas/pyarrow at runtime), with epoch-millisecond UTC start times. Experiments bind to a *content* hash of canonical row text, which is independent of the Parquet writer version. Data files stay out of git.

## ADR-0023 - Streaming features with exact running sums

**Decision.** Rolling features (reference price, ATR, efficiency ratio, breakout range, context return) update in O(1) from exact running sums of decimal products, so long runs cannot drift and a live process computes exactly the research values. A data gap resets the state (fail closed). Sources without a VWAP use a volume-weighted typical price ((H+L+C)/3), never a substituted VWAP.

## ADR-0024 - README orientation section above the protected brief

**Context.** The owner asked (2026-11-08) for the README to show the milestones, what is done and what is left. README.md is also the protected project brief.

**Decision.** README.md starts with an orientation section (status, roadmap M0-M7, gates G0-G5, tracks, checklist, next steps, repository map). It is followed by a marker comment and the original brief, **byte-identical**. The recorded hash covers exactly the bytes after the marker, so the brief cannot change unnoticed. The README is the single place for status; docs/PROJECT_GUIDE.md links to it instead of keeping its own copy.

## ADR-0025 - The official alpaca-py SDK for the paper adapter

**Context.** instructions.md §1 names the official `alpaca-py` SDK for the first paper adapter, and the README asked for a recorded decision before it enters the dependencies. The boundary test banned the substring `alpaca` (and `requests`, which the SDK pulls in) from runtime dependencies.

**Decision.** alpaca-py is the one allowed broker SDK, pinned exactly (`alpaca-py==0.44.0`, released 2026-08-11 and reviewed 2026-11-08; the newer 0.45.0 was three days old). The boundary test allows exactly that pin and still bans `requests`/`httpx` as direct dependencies. The SDK is imported only under `src/trader/brokers/alpaca/`. The adapter uses raw response dictionaries and converts every number to `Decimal` from its string form, so no price or quantity passes through a binary float into the domain. Its base URL is the paper endpoint, hard-coded; live stays impossible (`CERTIFIED_LIVE_ADAPTERS` is empty). Upgrading the pin is a reviewed change with a new certification run.

## ADR-0026 - Keys in the operating-system keyring

**Decision.** Broker and data keys are stored in the OS keyring under the configuration's `secret_ref` service (`ai-stock-trader/alpaca-paper`), as two entries with usernames `key_id` and `secret_key`. `trader secrets set|status|delete` handles them with hidden input; values are never printed, logged or written to disk by the project, and `status` reports presence only. A key ID that looks like a live Alpaca key (`AK…`) is refused for a paper service. Session environment variables (`APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`) keep working and take precedence, so the M2 ingestion route is unchanged. `keyring` is imported only by `config/secrets.py`.

## ADR-0027 - Training libraries stay out of the runtime

**Decision.** scikit-learn and LightGBM live in a separate dependency group `ml`, installed by default for development but never a `[project]` dependency. They are imported only under `research/`. A trained filter is exported as a JSON `ModelBundle` with a checksum; `models/` evaluates it with numpy alone and does not import `trader.research`, so the runtime never loads a training library. Boundary tests enforce both rules.

## ADR-0028 - Stock baseline cost basis and disclosures

**Context.** The stock baseline study (M2; strategy_and_profitability.md §3-5) could not be registered. Its only Alpaca fee schedule was unknown (`known: false`), so every candidate would be rejected `UNKNOWN_COSTS` and the buy-and-hold comparator raised `UnknownFeesError`. The report was written for crypto only, and `research baseline` wrote every study to `research/reports/baseline_report.md`, so a stock run would have overwritten the crypto report.

The fees Alpaca passes through changed several times over the backtest span:
- SEC Section 31: USD 5.10, 22.90, 8.00, 27.80, 0.00 and then 20.60 per million between 2022 and 2026.
- FINRA TAF: rose from 0.000130 to 0.000195 per share, with the per-trade cap rising from 6.49 to 9.79; paused from 2026-10-01 to 2026-12-31.
- FINRA CAT industry-member fee: charged from August 2024, at most 0.000048 per share.

Alpaca's current schedule (revised 2026-10-12) charges no commission on ordinary equity trades. It applies the SEC fee and TAF to sells and the CAT fee to buys and sells, computes them on the exact fractional quantity, and rounds each fee type's daily total up to the cent. Its support article describes rounding per trade instead.

**Decision.** One documented schedule covers the whole 2022-01..2026-11 backtest: `configs/fees/alpaca_us_equities_v2.yaml` (`alpaca-us-equities-conservative-2026-11-08`, DOCUMENTED, 15 dated official sources).
- It uses the highest rate of each fee in that span: zero commission; `regulatory_sell` with SEC 0.0000278 and TAF 0.000195 per share capped at 9.79; and a new `per_share` component for CAT at 0.000048 per share on both sides.
- Each fee type is rounded up to USD 0.01 on every order. Because the ceiling of a sum is never more than the sum of ceilings, this is an upper bound on Alpaca's daily rounding. A USD 10 round trip costs USD 0.04.
- Assumed, not verified: a retail account trading through the API, and Alpaca having passed through the official rates in past periods.
- The unverified file stays unchanged as the record of 2026-11-07.
- `FeeScheduleConfig` gains an optional `sources` list (url, what the page states, retrieved date).

A study whose fee schedule is unknown now fails before registration and before a run (`UnknownCostBasisError`). The CLI exits with code 2 and writes nothing.

`configs/research/stocks_baseline_v1.yaml` declares, before any run:
- trials T-0006..T-0009 (the Candidate A 2x2 grid) and T-0010 (Candidate B) on AAPL, MSFT, GOOGL, AMZN and NVDA;
- fills of 2.0/2.0 bps, NYSE sessions, a 12/3/3 walk-forward from 2022-01-01, and a locked final test from 2026-08-01 to 2026-11-01;
- registration text disclosing the fixed current mega-cap universe (survivorship and sector bias, no point-in-time membership), the missing earnings calendar (Candidate B's no-company-event condition and the avoid-earnings rule are not enforced), IEX single-venue volume and VWAP, and fractional limit orders accepted only as an unverified assumption.

The report's market wording is chosen by the result's track, and stock wording is filled from the study's registered disclosures. A result recorded before the track field existed (the crypto baseline) renders with the crypto wording and reproduces `research/reports/baseline_report.md` byte for byte. The G2 table follows the gate's check order instead of JSON key order. `research baseline` writes `research/reports/<study_id>_report.md` by default; `crypto_baseline_v1` keeps `baseline_report.md`. `build_registration` reads named fields only, so registration hashes are unchanged and the committed crypto trials still verify.

## ADR-0029 - Filter hook placement and the random and equal-turnover controls

**Context.** In M3 a model may only filter candidates that a frozen rule proposed (strategy_and_profitability.md §6-§7, docs/ml_research_guide.md §5). Research needs to compare rule-only with rule-plus-filter in the same USD 50 engine with the same costs. It also needs controls that reject candidates without skill, so that trading less is not mistaken for skill. The research engine had no hook. A filter must not be able to size, price or time an order, and runs without a filter must keep their existing result hashes.

**Decision.** `ResearchEngine` takes an optional `candidate_filter` and `feature_fn`.
- **Placement.** The filter is asked first in admission, right after the STALE_DATA check and before pauses, entry limits, the busy check, the spread check and sizing. So it sees every in-window, non-stale candidate whatever the portfolio state, and the set of candidates it sees never depends on its own or another filter's decisions.
- **Effect.** A veto becomes `REJECTED` with `NO_EDGE` and binding constraint `filter:<filter_id>`. An accepted candidate goes through exactly the same checks and sizing as without a filter. `FilterDecision` carries only accept, score and reason, so it has no channel to size.
- **Log.** Each decision is logged in `EngineResult.filter_log`; the score is a Decimal quantised to 6 dp because the log is hashed. The log has its own `filter_hash`. `result_hash` covers trades, outcomes and sessions only, so no filter or `AcceptAll` reproduces the pre-hook hashes. The tests pin those hashes.
- **Features.** They come from the rule's `features(instrument_id)` snapshot, which is checked against `candidate.snapshot_hash` before use (`candidate_feature_fn`).
- **Random control.** `HashRandomReject(rate, salt)` rejects a candidate when `int(sha256(salt + candidate_id)[:8]) / 2**64 < rate`, compared exactly. The draw is deterministic and does not depend on order.
- **Equal-turnover control.** `TurnoverMatchedReject` works over evaluation windows: non-overlapping half-open UTC intervals `[start, end)` in time order, normally the study's deployment, validation or test windows. A candidate belongs to the window that contains its decision time. In each window the control accepts as many candidates as the reference filter accepted there, choosing those with the smallest `sha256(salt + candidate_id)` (ties broken by ID) among the candidates the reference filter saw. It is built from the reference run's filter log, or from explicit target counts. It refuses any candidate outside that universe. Because of the placement rule, a control run sees exactly the same candidates, so the accepted counts match exactly.
- **What "equal turnover" means.** Matching is on accepted candidates, which is what a filter controls. Realised trades and USD turnover can still differ, because pauses, entry limits and busy instruments apply after the filter. Reports must show both.

## ADR-0030 - Model bundles: numpy-only inference, validation-only calibration and thresholds

**Context.** M3 adds candidate-filter models (strategy_and_profitability.md §6, docs/ml_research_guide.md §1 and §5). architecture.md §3 requires a `ModelBundle` with feature and strategy versions, training cutoff, calibration, thresholds, data hashes, an evaluation report and an artifact checksum. ADR-0027 keeps training libraries out of the runtime. A filter must not learn anything from its validation or test windows, and the trading process must score exactly what research evaluated.

**Decision.** A trained filter is exported as a `ModelBundle` (`models/bundle.py`). This is a frozen ConfigModel. Every number in it is stored as the exact decimal string of the float's shortest round-trip form (`models/codec.py`). This covers scaler means and scales, coefficients, the LightGBM `dump_model()` thresholds and leaf values, and calibration parameters. As a result `canonical_json` can hash it and inference matches the trained model exactly.

`checksum` is the SHA-256 of the canonical JSON of all other fields. It is checked when a bundle is constructed or loaded (`BundleIntegrityError`). `ModelBundle.seal(...)` computes it, `model_copy(update=...)` is refused, and `save()` checks again before writing deterministic, sorted-key JSON.

Inference uses numpy alone:
- Logistic: `sigmoid(((x - means) / scales) . coef + intercept)`.
- LightGBM: the dumped trees are evaluated directly, with numerical `<=` splits, `default_left`, and `missing_type` None/Zero/NaN handled as in LightGBM's NumericalDecision. Leaf values are summed in tree order and the binary sigmoid applied.
- Then the calibration map (`models/calibration.py`): Platt on the raw log-odds, isotonic piecewise-linear knots on the uncalibrated probability, or none.

Categorical splits, non-binary objectives, multiclass and averaged output are refused. Tests show equality with scikit-learn and LightGBM `predict_proba` within 1e-9, including missing values.

Training (`research/training.py`) works as follows:
- `purge` drops training labels whose decision or exit is at or after `train_end - embargo`.
- `train_fold` refuses training rows outside `[training_start, training_cutoff)`, labels that end at or after the cutoff, and validation rows before it.
- The scaler and the model are fitted on training rows only.
- The grid point is chosen by validation log-loss of the uncalibrated numpy-bundle predictions; ties go to the first point.
- Calibration is fitted on the selected model's validation scores only.
- The threshold is chosen on validation only. It uses the engine-replay objective callback or, without one, the mean net return of the accepted validation candidates. Only thresholds that accept at least `max(min_accepts, 1)` validation candidates are eligible. Ties go to the lower threshold, and `p >= threshold` is compared exactly against the Decimal.
- Every grid point, the calibration and every threshold candidate are logged as strings.
- LightGBM runs single-threaded and deterministic (`n_jobs=1, deterministic=True, force_col_wise=True`, seeded), so two runs produce the identical checksum.
- Library versions, digests of the training and validation rows, and the label set's source hashes (dataset, content and spec hashes) are recorded in `data_hashes`.
- The test window is never an input.

Validation diagnostics (Brier, log-loss, ECE, raw and calibrated) are returned in `TrainResult.val_metrics` and are not stored in the bundle. They are in-sample for the calibration and the threshold.

**Consequence.** The trading process can load and score a bundle without scikit-learn or LightGBM. Any change to its content is detected, and research and runtime compute the same probability. Validation metrics are diagnostics only. Out-of-sample evidence comes from the registered test-window replay. A bundle's checksum depends on the recorded library versions, so it reproduces exactly only in the same environment. Supporting a new LightGBM dump format or categorical features requires a reviewed change to the evaluator.

## ADR-0031 - Paper runtime: shared decision loop, polling bars, subledger bootstrap

**Context.** M4 runs the bot against the Alpaca paper account. It must use the risk, order-management and accounting code that replay has proven, and replay must not change: the demo stays 14/14 with pinned hashes. Candidate A decides on completed five-minute bars. The paper account holds about USD 100,000, while the policy trades a USD 50 subledger. Real clocks can disagree with the data source. A database can be reset while the broker still remembers the client order IDs it has seen.

**Decision.**
1. **One decision cycle.** `runtime/decision_loop.py` holds the cycle: broker events, loss windows, exits, entry time-outs, then completed bars -> strategy -> risk -> allocator -> outbox -> gateway, then unknown-outcome resolution. ReplayRunner keeps only the replay clock and the simulated venue. Both environments are wired by the same functions (`account_runtime`, `wire_execution`, `bootstrap`). The replay demo hashes are pinned in the tests, and a parity test feeds externally completed bars through the loop and reproduces the replay baseline.
2. **Paper polls instead of streaming market data.**
   - Completed IEX 5-minute bars come from `/v2/stocks/bars` about 5 s after each bar end. A missing bar is retried twice, then reported missing; it is never fabricated.
   - Latest quotes come from `/v2/stocks/quotes/latest` every 5 s.
   - A bar's availability time is its local receipt time. Only bars with a source VWAP are used.
   - Polling keeps the bar path identical to research, keeps the SDK confined to the adapter, and stays far below rate limits.
   - The account-event stream belongs to the adapter.
3. **Environment.**
   - PostgreSQL only; the schema is brought to the Alembic head programmatically.
   - Both the adapter and the capability profile must be paper.
   - On a fresh database the account must be flat. A non-flat account is accepted only when this database already recorded its baseline (a restart).
   - `bootstrap` creates the USD 50 subledger once, with the adapter's cash as the broker cash baseline. Reconciliation compares cash deltas, so the paper balance is never trading capital.
4. **ID namespace.** The allocator namespace is `paper/<account>/<session date>/<baseline time>`. Client order IDs therefore never repeat across sessions, and a database reset (which records a new baseline time) cannot re-issue an ID the broker has seen.
5. **Health.**
   - Reconciliation runs every 60 s and 2 minutes after the close. Expiry of DAY orders at the close is observed by client order ID, never assumed; an order still live raises an alert.
   - Source time ahead of the local clock beyond the policy limit (250 ms) raises a `CLOCK_SKEW` pause; the receipt time is clamped to the event time, so nothing crashes.
   - A failed data poll raises `FEED_GAP`.
   - Both are HEALTH pauses: they block entries until a later passed reconciliation. Exits are never blocked.
6. **Edge.** `NoEdgeEstimator` is the default, so every candidate is rejected `NO_EDGE` until a validated bundle exists.
7. **Stop.** Ctrl+C means no new entries, working entry orders are cancelled, and a final reconciliation runs. Positions still held are reported as pending liquidation; monitored exits do not run while the bot is stopped (ADR-0005).
8. **Adapter construction.** The CLI builds the adapter through `runtime/broker_factory.py`, which imports `trader.brokers.alpaca.adapter` lazily. A build without the adapter refuses with exit code 2 before the database is touched. `trader live` is unchanged and live stays impossible.

## ADR-0032 - Key profiles, a keys inbox folder, and the Alpaca MCP server without trading tools

**Context.** The owner switches Alpaca paper keys often (several paper accounts, resets) and asked for a folder to drop new keys into, instead of `.env`. The owner also asked to connect Claude Code to Alpaca's official MCP server. Keys must stay out of the repository and of any configuration file, and an assistant must not be able to trade around the risk engine: orders placed outside the bot also break its reconciliation (unknown orders and positions pause the account).

**Decision.**
- **Profiles.** The keyring keeps any number of named key pairs per service (`<service>#<profile>`, an index entry lists the names) and one **active** pair under the service itself, which is what every reader uses. `trader secrets set --profile`, `list`, `use <profile>` and `delete --profile` manage them; output shows names only.
- **Keys inbox.** `~/.ai-stock-trader/keys` (or `TRADER_KEYS_DIR`), outside the repository. The owner drops `<profile>.env` files there (`APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`, or the common `ALPACA_*` names). `trader secrets import` validates every file first (key and secret present, no live `AK…` key, no non-paper endpoint), then stores each as a profile and **deletes the file** (`--keep` leaves it). Errors never quote values. The keys that had been placed in `.env` on 2026-11-08 were moved this way into the profile `paper-main` and removed from `.env`.
- **Launching tools with keys.** `trader secrets exec alpaca-paper -- <command>` runs a local tool with the active keys in its environment only, forces `ALPACA_PAPER_TRADE=true` and the paper base URL, and passes the operating system's trust store as `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` (exported by `data/sources/http.py::write_os_trust_store`), so tools built on certifi work behind an antivirus HTTPS proxy. It prints nothing to stdout.
- **Alpaca MCP server.** `.mcp.json` starts `alpaca-mcp-server==2.4.0` (released 2026-10-12; 2.4.1 was four days old at review) through `trader secrets exec`; it holds no keys. It is a development tool run by `uv tool run`, not a project dependency. `.claude/settings.json` **denies** the twelve state-changing tools (placing, replacing or cancelling orders, closing positions, exercising options, changing account configuration, creating locates); read-only account, market-data, news and documentation tools stay available, and watchlist edits are allowed because they do not affect trading. A test pins both files.

**Consequence.** Switching keys is one command and keys never sit in `.env` or a config file. The assistant can read the paper account and market data but cannot trade on it; the bot remains the only thing that places orders, through the risk engine and the gateway. Lifting a deny rule is a deliberate owner change, best paired with a separate paper account that the bot does not use.

## ADR-0033 - Alpaca paper adapter: definite vs unknown outcomes, canonical execution ids, version ordering

**Context.** Milestone M4 runs the `BrokerAdapter` contract against Alpaca's paper Trading API through alpaca-py 0.44.0 (ADR-0025). The submission gateway treats `BrokerTimeout` as an unknown outcome (pause, then resolve by client order ID), so a definite refusal must come back as `SubmissionResult(accepted=False)`. Fills reach the OMS twice, from the `trade_updates` stream and from REST reconciliation, and must post exactly once. The OMS orders status events by an integer `broker_version` and ignores versions it has already seen. Alpaca does not document that the stream's `execution_id` equals the `FILL` account-activity `id` (checked 2026-11-08). Its activities `after` filter is defined on `created_at`, which is null for FILL activities. The SDK retries 504 by default, which could re-send a POST the broker already accepted.

**Decision.**
1. **Outcomes.**
   - HTTP 4xx (except 404, 408 and 429) is a `DefiniteRejection` carrying status, code and message, so the result is `accepted=False`. 404 is `NotFound`.
   - Timeouts, connection or TLS errors, unreadable bodies, 408, 5xx and 429 after the SDK's retries are `BrokerTimeout` (unknown outcome).
   - The SDK's automatic retry is limited to 429, and every request has a 10 s timeout.
   - A 422 "client_order_id must be unique" is an unknown outcome, because the order exists. So is a submit response that cannot be mapped exactly.
   - A cancel with an unknown outcome is recorded by the gateway as a `CANCEL_OUTCOME_UNKNOWN` alert plus an `UNKNOWN_ORDER` pause. The intent stays live; nothing escapes the run loop.
2. **Canonical execution ids.** A `Fill` is built only from a REST `FILL` activity, and the activity `id` is the execution id. A stream fill only triggers that REST fetch, which is retried until the activities cover the order's filled quantity. Stream and reconciler therefore post the same id, and the unique `(broker, account_id, execution_id)` constraint dedups it. Fees are posted as 0 with status `PENDING`, never guessed.
3. **Version ordering.** `broker_version` is the order's `updated_at` in integer epoch microseconds, with nanosecond digits truncated. REST and stream views of one state carry the same version and the same event id `{order_id}:v{version}`. The three version columns become BIGINT (migration 0002). Two states with the same microsecond would share a version; the second status is then ignored (fills are still posted) and reconciliation fails closed.
4. **Statuses.** An explicit table covers every documented and SDK-enum status.
   - Working statuses map by filled quantity and are never terminal unless fully filled, so exposure stays reserved.
   - `replaced`, `pending_replace`, `suspended` and any unknown value raise `UnmappedStatus`.
   - `drain_events` keeps the events it could map and raises. `OrderManager.pump` turns the error into a `BROKER_EVENTS_UNAVAILABLE` alert plus an `UNKNOWN_ORDER` pause.
5. **Paper only.**
   - The constructor refuses non-PAPER mode, a capability profile that is not paper or not alpaca, and `AK…` key IDs.
   - No parameter accepts a URL, and the SDK clients are built with `paper=True` (both checked by tests).
   - `CERTIFIED_LIVE_ADAPTERS` stays empty.
6. **Pacing and scope.**
   - Every REST call takes a token from a bucket on the injected clock: 200/min unless the profile states a limit, sized so that capacity + 60 × refill rate = limit.
   - History is scoped to `since`; open orders and positions are always reported in full.
   - FILL activities are read newest first and filtered by `transaction_time` on the client.
   - Working orders are re-read over REST after every stream reconnection, and every 5 s while no stream runs.

## ADR-0034 - Model study protocol: per-fold training, validation-only selection, controls

**Context.** M3 (implementation_roadmap.md §1-§2) needs a walk-forward comparison of candidate filters and a statistical model report. strategy_and_profitability.md §6 requires that "trading less" is never mistaken for skill. risk_and_validation.md §5 requires registration before running, chronological walk-forward and a locked final test. Labels, the engine filter hook, the controls (task C), bundles and per-fold training (task D) existed separately; how they combine had to be fixed before any trial ran.

**Decision.**
- A model study (`kind: model_study`, `research/model_study.py`) names a registered base study for data, universe, instrument specs, costs, fills, policy, splits and bootstrap settings, and lists filter trials.
- Each trial's registration is the base rule registration unchanged plus a frozen filter recipe: model type and training library versions, feature set and version (`candidate-v1`), label definition, grid, inner selection metric, calibration, threshold grid and rule, `min_accepts`, seeds and controls. The family is written `<RULE>+LOGIT_FILTER` (or `+LGBM_FILTER`). A run refuses any trial whose registration differs, before any data is read.
- Per fold:
  - Training labels use only bars before the training end, then are purged and embargoed by the maximum hold.
  - The scaler and model are fitted on training rows only.
  - The grid point is chosen by validation log-loss, and the calibration map is fitted on validation scores.
  - The threshold is chosen on validation by net PnL per session of an engine replay of the validation window, with the bundle as `ModelThresholdFilter` (fresh USD 50 ledger, same costs, outcome bars up to the test start, never a test-window bar, at least `min_accepts` accepts, ties to the lower threshold).
  - A fold that cannot be trained vetoes every candidate in its test window (fail closed: cash). It never falls back to the unfiltered rule.
- Every test window is a fresh USD 50 deployment, replayed for four variants: rule only; rule plus filter; random rejection at the filter's realised veto rate (`HashRandomReject`, N salts); and equal accepted-candidate counts (`TurnoverMatchedReject`, N salts).
- Statistics: `study.summarize` per variant; a bootstrap reality check across every variant of every trial; deflated Sharpe with N = all registered trials; paired bootstrap differences of the filter against each other variant.
- The study's walk-forward deploys, per fold, the trial with the highest positive validation objective, else cash. Gate G2 uses `study._g2` unchanged.
- The holdout is never loaded. `research labels` requires a registered trial and always refuses the holdout.
- The first study, `crypto_filter_v1`, has exactly two logistic trials:
  - T-0011: Candidate A crypto at spec defaults;
  - T-0012: Candidate B crypto;
  - shared recipe: C {0.1, 1.0} x class weight {none, balanced}, Platt, thresholds 0.30-0.70 step 0.05 (widened by the integrator before registration: a 2R target breaks even near p = 0.33 before costs, so a 0.50 floor would leave most folds untradeable), at least 20 accepts, 20 salts per control.
  Its report is the roadmap artifact `research/reports/model_report.md`.

**Addendum (2026-11-08, before any model trial was registered).** An adversarial review found that the study reused the baseline G2 gate, whose selection-adjusted bound can come from a rule-only or control row. The model study now uses `model_gate`: the baseline walk-forward checks, plus the reality check's best row must be a model-filter row, and that trial's filter must beat the rule alone, the random-rejection control and the equal-turnover control, each with a positive one-sided paired lower bound. A missing control fails the gate. The report says "challenger" only when this gate passes.

## ADR-0035 - Paper certification path and evidence

**Context.** configs/capabilities/alpaca_documented.yaml lists `fractional.limit.day`, `client_order_id_unique`, `order_visibility_lookups` and `order_visibility_window_seconds` as UNKNOWN.

UNKNOWN means disabled. So the paper bot cannot enter fractional stocks, and an order with an unknown outcome can never be resolved as "not accepted". Only orders on the owner's paper account can produce this evidence.

Those orders must still go through the risk engine, `risk/approval.py` and the submission gateway. They must be tightly bounded. No strategy, model or ordinary paper run may be able to use this route.

**Decision.**

1. **Grant.** `trader paper certify` creates a `CertificationGrant` (domain/certification.py) for one run.
   - Construction raises for any mode except PAPER.
   - The grant names its account and expires within 30 minutes.
   - It allows at most 6 broker submissions and at most USD 5 notional per order. These are hard ceilings: a grant can lower them but never raise them.
   - The capabilities under test are a subset of {fractional.limit.day, fractional.market.day}.
   - A shared `CertificationBudget` counts the run's submissions.
2. **Evidence.** `EdgeEvidence.CERTIFICATION` is in no `ACCEPTED_EVIDENCE` set, so `evaluate_entry` refuses it in every mode.
   - A separate `CERTIFICATION_EVIDENCE` table accepts it in paper only.
   - Paper accepts it only through `evaluate_certification_order`, which needs an active grant.
   - That function fails closed on: pauses; unknown orders; an existing position or pending entry; the market closed (broker clock); a stale quote; a capability that is neither usable nor under test; a limit price less than 10% below the bid; a market-buy spread above 50 bps; the budget used up; and a notional bound above the cap (limit: qty x limit; market: qty x ask x 1.05).
3. **Allocation.** `PortfolioAllocator.reserve_certification_order` writes, in one transaction: the decision (evidence CERTIFICATION, model hash `certification:<run id>`), the intent, the plan from `issue_plan`, an entry reservation and the outbox row.
   - The gateway submits.
   - The round trip's sell is the ordinary reducing exit.
4. **Adapter.** Only an `AlpacaPaperAdapter` built with the run's budget accepts the run's plans for a capability under test or for a MARKET DAY buy.
   - It re-checks the cap.
   - It counts every submission, exits included.
   - It refuses without sending once the budget is used or the grant has expired.
   - Ordinary paper runs never build the adapter with a budget.
5. **Duplicate probe.** `SubmissionGateway.probe_duplicate_submission` re-sends one resting certification LIMIT buy's still-valid plan, unchanged.
   - It sends it through a second adapter instance with no memory of the first submission, standing in for a restarted worker. The broker itself must refuse the ID.
   - The intent is not touched, and an alert records the outcome.
   - If Alpaca accepts the duplicate, it is cancelled at once, raises a CRITICAL alert and pauses entries.
   - Alpaca's duplicate refusal raises `DuplicateClientOrderId`, a `BrokerTimeout` subclass, so the ordinary submission path still treats it as an unknown outcome.
6. **Run and report.** The command prints the plan and sends nothing without `--yes`.
   - Preflight sends nothing. It requires the Alpaca clock open with at least 15 minutes to the close, and an asset that is active, tradable and fractionable. The asset record is applied to the instrument spec for this run only. It also requires a PASSED startup reconciliation and no position in the symbol.
   - The steps:
     1. A fractional LIMIT DAY buy of about USD 4 at 70% of the bid, counting lookups until it is visible by client ID.
     2. The duplicate probe.
     3. A timed cancel.
     4. A fractional MARKET DAY buy of USD 1.50 and its reducing MARKET sell. Fills, broker cash changes and observed fees are recorded, then reconciliation runs and the account must be flat.
   - Anything still working at the end is cancelled, then a final reconciliation runs.
   - The report `runs/paper/certification/<UTC>.json` is content-hashed, never overwritten and holds no secrets.
7. **Profile proposal.** `trader paper certify-profile` writes `configs/capabilities/alpaca_paper_certified.yaml` from a verified report. It sets `PAPER_DEMONSTRATED` only for what the report shows:
   - fractional limit buy accepted and resting in regular hours;
   - market round trip filled, every fill in the ledger, account flat;
   - duplicate refused, with exactly one unfilled order for the ID;
   - order visibility, only from at least 3 samples that were all visible: lookups = max(3, observed + 2), window = max(30 s, 10 x observed seconds), never less conservative than the base;
   - `X-RateLimit-Limit`, never above a limit the base already states.

   A demonstrated duplicate acceptance sets `supported: false`. Any other refusal becomes a reviewer note, because its cause cannot be attributed safely. Everything else is copied from the base, and the environment stays paper. The owner reviews the file, points `configs/paper.yaml` at it and runs `trader docs build`.
8. **Reconciliation guard (F + G1 integration).** `Reconciler.reconcile` turns an adapter `BrokerError`/`BrokerTimeout` into a FAILED run with a `RECONCILIATION_BROKER_UNAVAILABLE` alert and a `RECONCILIATION_FAILED` pause. Before, the error was raised out of the paper loop or the owner commands.

**Consequence.**
- Replay, shadow, live and ordinary paper runs behave as before; the demo hashes are unchanged and live stays impossible.
- No capability is enabled by code. A real paper run plus the owner's review of the proposed profile is required.
- Certification orders land in the USD 50 paper subledger: a few cents of spread, at most four submissions.
- Sell-side pass-through fees are not read into the ledger yet. If Alpaca debits them at once, the final reconciliation fails by more than a cent and pauses entries until the fees are handled; the report shows `observed_sell_fees_usd`.
- Paper evidence never certifies live availability.

## ADR-0036 - Research MCP servers for the assistant, and plain secrets for their keys

**Context.** WATER_PLAN (docs/integrations/WATER_PLAN.md) Phase 1: connect the research MCP servers the owner chose (Kraken, CoinGecko, Alpha Vantage, Capital.com demo, plus Twelve Data and Massive) next to the existing Alpaca server (ADR-0032). MCP servers are tools for the assistant, not for the bot. Two of them can trade as shipped: the Kraken CLI exposes trade, funding and a `feedback` tool that uploads text to Kraken, and capital-mcp 0.5.0 defaults to `CAP_ALLOW_TRADING=True` and `CAP_DRY_RUN=False` and reads `CAP_ENV=true` as **live**. Capital.com issues no read-only API keys. `trader secrets` only knew the Alpaca key pair.

**Decision.**
- **The bot never calls MCP.** The runtime keeps using direct APIs behind `BrokerAdapter` and `SubmissionGateway`; nothing in `src/trader` imports an MCP client, and no MCP package is a project dependency. MCP output is research input for the assistant and is treated as untrusted data.
- **Hosted servers carry no credentials in files.** CoinGecko (public, no key), Alpha Vantage, Twelve Data and Massive are `"type": "http"` entries with their official URL and nothing else. The three keyed providers authorize through their own OAuth page (`/mcp` in Claude Code), so the owner's key is entered on the provider's site and Claude Code stores the token; no query-string key (Alpha Vantage's legacy `?apikey=` form) is allowed.
- **Kraken: market data and the local paper simulator only.** `kraken-mcp -s market,paper` from the official kraken-cli v0.5.1, built from the tagged source with the toolchain the project pins (Rust 1.99.1), because no Windows release asset exists. No Kraken key is stored, so private groups cannot work even if selected. `.claude/settings.json` additionally denies every tool outside the market and paper groups (including `feedback`), in case the arguments ever change. Kraken "paper" is a local simulation; it never certifies exchange fills (WATER_PLAN §4.1).
- **Capital.com: demo only, trading off, three layers.** (1) `trader secrets exec capital-demo` drops every inherited `CAP_*` variable and `VIRTUAL_ENV`, adds the three demo credentials, then forces `CAP_ENV=demo`, `CAP_ALLOW_TRADING=false`, `CAP_DRY_RUN=true`, `CAP_REQUIRE_EXPLICIT_CONFIRM=true`, an empty instrument allowlist and zero order and position limits; nothing inherited can override them. (2) `.claude/settings.json` denies its eleven tools that trade or change the account or watchlists. (3) The server is pinned to a reviewed commit (3207be4, release v0.5.0; it talks only to Capital.com's API hosts and has no telemetry) and runs in its own uv tool environment. Its `.mcp.json` entry is added only after the owner stores demo keys. Its `install.ps1` is never run.
- **Plain secrets.** `trader secrets` gains a registry of `ServiceSpec`s. `alpaca-paper` keeps every existing behaviour (profiles, keys inbox, `--verify`). A *plain* service (first: `capital-demo`) stores one fixed set of named fields under `ai-stock-trader/<name>`, one keyring username per field, with no profiles; `set` prompts for each field with hidden input, `status` shows presence per field, `delete` removes exactly its fields, and profile, inbox and verify commands are refused for it.
- **Evidence.** Each server's status (`planned`, `configured`, `authenticated`, `read_verified`) and smoke test live in `docs/integrations/connection_report.md`. Not connected now, with reasons recorded there: FMP, IBKR, Coinbase, cTrader and IG.

**Consequence.** The assistant can research crypto breadth, fundamentals, earnings, macro, gold and silver context and venue data without any trading path: the only order path stays the bot's risk engine and gateway. A deny rule or forced setting is lifted only by a deliberate, reviewed owner change. `tests/test_mcp_config.py` pins the server set, the official URLs, Kraken's arguments and every deny rule; `tests/test_secrets.py` pins the forced Capital.com environment.

## ADR-0037 - Repository root tidied: specifications under docs/specs/, migrations under deploy/, caches under .cache/

**Context.** The owner found the repository root hard to navigate: 39 entries, among them 13 Markdown files, four tool caches, `alembic.ini` and `migrations/`. The ten specification files are hash-pinned (`docs/protected_spec_hashes.sha256`, ADR-0024), and the protected README brief links to seven of them by bare file name, so moving them would break those links unless the brief changed.

**Decision (owner, 2026-11-09).**
- The nine specification files move unchanged to `docs/specs/`; `CONTRIBUTING.md` moves to `docs/` (GitHub still finds it there). The manifest and `.gitattributes` list the new paths; the nine hashes are unchanged.
- **Exception to the brief's byte-identity, approved by the owner:** the seven link targets in the README brief now point to `docs/specs/<file>.md`. Nothing else in the brief changed (the rewrite was checked to differ only in those targets). The marker line says so, and the README hash is re-pinned.
- `alembic.ini` and `migrations/` move to `deploy/` (`script_location = %(here)s/migrations`; run `alembic -c deploy/alembic.ini ...`). `docker-compose.yml` stays at the root: moving it would change the compose project name (a new, empty `trader_pg_data` volume) and the `.env` lookup.
- pytest, mypy, ruff and Hypothesis caches live under one ignored `.cache/` folder.
- Prose citations such as "instructions.md §3" in code, configs and the research registry are left as they are: the file names are unique, and the registry is append-only.

**Consequence.** The visible root is `configs/ data/ deploy/ docs/ research/ runs/ src/ tests/ ui/` plus `README.md`, `docker-compose.yml`, `pyproject.toml` and `uv.lock`. Branches that predate this merge `sunshine` normally; git carries their edits across the renames. Any new test or tool that opens a specification must use `docs/specs/`.

## ADR-0038 - A light launcher for keyed MCP servers

**Context.** Every Claude Code session starts the Alpaca MCP server through `trader secrets exec`, and that wrapper process stays alive for the whole session. Going through the full CLI imported the ledger, database and market-data stack (pandas, numpy, sqlalchemy; `trader.config.secrets` itself reached pandas via `alpaca_history` and the calendars). Measured on the owner's laptop (2026-11-09): about 560 MB of committed memory and 5-9 s of imports per session, with six or seven sessions open at once. The slow start also made the MCP connection time out, and because the wrapper ran `.venv\Scripts\trader.exe`, uv could not reinstall the project after a `pyproject.toml` change while any session was open.

**Decision.**
- `KEY_ENV`, `SECRET_ENV` and `AlpacaCredentials` live in `trader/config/alpaca_keys.py` (standard library only). `trader.data.sources.alpaca_history` re-exports them, so existing callers are unchanged.
- The exec logic lives in `trader/config/secrets_exec.py`. `.mcp.json` launches it as `python -m trader.config.secrets_exec NAME -- CMD`, and `trader secrets exec` calls the same `run`, so behaviour (forced paper or demo settings, trust store, nothing on stdout) is identical.
- `tests/test_secrets.py` fails if importing the launcher loads pandas, numpy, sqlalchemy or typer.

**Consequence.** The launcher uses about 21 MB and starts in about 1 s. Keyed servers added later (for example capital-demo) should use the same launcher.

## ADR-0039 - Keys for research MCP servers may live in the gitignored .env

**Context.** On 2026-11-09 the owner connected Alpha Vantage and Twelve Data. The hidden-prompt and browser sign-in routes failed or confused: the antivirus HTTPS scanner broke Claude Code's OAuth discovery for the hosted servers, the hosted Alpha Vantage server then needed an interactive terminal, and an `mcp add` written into Claude Code's own config was lost to concurrent sessions. The owner decided that API keys he adds himself go into the repository's `.env`, one `NAME=value` per line, and asked Claude to wire each one. This departs from ADR-0026/0032 ("no keys in `.env`") for plain research services only.

**Decision.**
- A *plain* service (ADR-0036) reads each field from the repository `.env` (`trader.config.secrets.DOTENV_PATH`) under the field's variable name first, then from the OS keyring. An empty line counts as absent. `trader secrets status NAME` says where each value comes from (`from .env`, `from keyring`, `missing`), never the value.
- The Alpaca key pair is unchanged: keyring, profiles and the keys inbox only (`load_alpaca` never reads `.env`). Live keys stay impossible.
- Only a field's own variable is read from `.env`. Forced safety settings still win, so a `.env` line such as `CAP_ALLOW_TRADING=true` never reaches the Capital.com server.
- New plain service `alphavantage` (`ALPHA_VANTAGE_API_KEY`). `.mcp.json` runs Alpha Vantage's official local server instead of the hosted one: `python -m trader.config.secrets_exec alphavantage -- uvx --python 3.13 --from marketdata-mcp-server==0.3.1 --with "mcp>=1.12.3,<2" marketdata-mcp`. 0.3.1 uses the mcp 1.x server API and crashes on mcp 2.x, hence the pin. It is read-only market, fundamentals and macro data, 25 requests a day on the free plan.
- `.env` stays gitignored and is never committed; tests point `DOTENV_PATH` at a temporary file and never read a developer's real `.env`.

**Consequence.** Adding a key is one line in `.env` plus a registry entry and a `.mcp.json` launcher entry. Twelve Data's key also sits in `.env`, but its local server (twelvedata/mcp, commit d139a77) has not been run: it is unpublished third-party code and needs the owner's approval first; until then Twelve Data uses the hosted server's browser sign-in. A key in `.env` is readable by anything running as the owner, which the keyring would not change on this machine; a leaked key is revoked at the provider.

## ADR-0040 - Owner practice mode on the paper account

**Context.** On 2026-11-09 the paper account was certified and the paper bot ran its first session, rejecting every candidate `NO_EDGE` because no strategy has passed G2 (ADR-0019: a cost-coverage gate is not an edge). The owner wants to practise with real paper fills before any money is involved: see entries, exits, costs, accounting and restarts behave on the broker, and learn from them, while the research for a strategy with an edge continues. Paper money is not at risk, but practice results must never be confused with evidence.

**Decision.**
- `trader paper run --practice` injects `PracticeEdgeEstimator`. It *assumes* each candidate's planned target is reached: the expected gross return is the target distance from the reference price, on a mid-to-mid basis (both spread crossings charged) with no uncertainty margin. The risk engine's conservative net-edge check therefore becomes a cost-coverage gate: a practice trade is taken only when its target pays the spread and every fee at the exact size. Candidates without a target get no score.
- A new evidence kind, `EdgeEvidence.PRACTICE`, is accepted by `evaluate_entry` in **paper and replay only**. Shadow and live never accept it; the estimator refuses to be built for them, and the CLI refuses practice outside a paper configuration with live disabled.
- Every other control is unchanged: the USD 50 subledger, 0.25% risk per trade, two open positions, two entries per session, spread and staleness limits, pauses, reconciliation and the certified capability profile.
- Practice decisions carry evidence `PRACTICE` and model hash `practice:planned-target:v1` (risk decisions and `model_versions`), and the run log starts with a PRACTICE MODE banner. They are never research (G2) or forward-paper (G3) evidence and never enter a promotion manifest; the forward report must exclude them, and G3 sessions start from a fresh baseline after practice ends.
- Capital stays USD 50, the size real money would start at. Alpaca charges no commission, so practice costs are the spread plus regulatory sell fees (rounded up to USD 0.01 per component per order); a larger practice balance would dilute those fixed cents and paint a rosier picture than the USD 50 account will see.

**Consequence.** The owner sees genuine paper fills from Candidate A, whose backtests lost in every window, so practice results are expected to be negative; their value is operational (fills, slippage against the cost model, exits, restarts) and educational, not a signal. Turning practice off is simply running `paper run` without the flag.

## ADR-0041 - Research-only policy variant with 1% risk per trade for the sized ETF trend trial

**Context.** The daily ETF study (etf_daily_v1, 2026-11-09) found that Candidate D's trend timing made +3.08 USD before costs at USD 50 but -1.62 USD after them. The shared policy's 0.25% risk per trade, against a 4 x ATR(20) disaster stop, sized positions at USD 1.5-5, and Alpaca's pass-through fees, rounded up to a cent per fee type on every order, cost about USD 0.04 a round trip: 1-3% of such a position. The diagnostic USD 1000 view, with fees diluted, made +51.77 USD. The owner chose to test the same rule with larger positions while keeping USD 50 as the starting capital.

**Decision.**
- `configs/policy/trend_sized_v1.yaml` is a research-only copy of `configs/policy/base.yaml` with `risk_per_trade_fraction` 0.01 (base 0.0025) and `max_total_planned_risk_fraction` 0.02 (base 0.005). Every other field is identical: 40% gross exposure, the USD 30 / 60% cash reserve, the 20% (USD 10) symbol cap, two open positions, entry limits and all loss limits. No runtime configuration may point at it.
- Study `etf_trend_sized_v1` registers one trial, T-0015: the frozen Candidate D spec on SPY, QQQ, TLT and GLD under that policy, with etf_daily_v1's data, fills, costs, windows, locked holdout and bootstrap seed.
- The study file states that the trial was designed after etf_daily_v1's results were seen. It counts in the trial budget and in the selection-aware statistics, and its result is not independent of T-0014's.

**Consequence.** If T-0015 passes G2, adopting 1% risk per trade for paper or live is a separate owner decision with its own review of the loss limits: at 1% a full stop-out equals the 1% daily loss limit. If it fails, the result shows that position size alone does not rescue the rule at USD 50.

## ADR-0042 - Crypto daily bars on the UTC-day calendar

**Context.** Every intraday crypto trial lost to costs (T-0001..T-0005 on 15-minute bars, the T-0011/T-0012 filters): a Kraken Pro Tier 1 round trip of two taker fills costs about 1.6% (0.80% a side), more than BTC's median daily move of about 1.2%, while its median monthly move is about 7%. Rules that hold for weeks are untested on crypto. The daily-bar machinery (`daily-v1` features, Candidates C and D, next-open orders, overnight holds, exit signals) was built for XNYS sessions only; `Utc247Calendar` could not count sessions, and Candidate D refused it.

**Decision.**
- A crypto session is a UTC day `[00:00, 24:00)` (CRYPTO_TRADING_TRACK.md §3). `Utc247Calendar` gains the session arithmetic of `ExchangeCalendar` (`session_for_date`, `session_on`, `session_date_of`, `sessions_after`, `session_open_before`, `next_session`, `is_session_day`, `session_opening_at`); every date is a session, so the next session is the next day and a month's last session is its last calendar day.
- A daily dataset on a 24/7 venue is a fixed-interval request: `bar_minutes: 1440`, `calendar: UTC_24_7`, no `timeframe`. Every UTC day has the same length, so the epoch-aligned 1440-minute grid is the session grid, and the existing fixed-interval checks (alignment, duplicates, missing intervals reported and never filled) are the daily-bar checks; `"1Day"` stays the label of variable-length XNYS session bars and still requires XNYS. A UTC-day bar is usable at `end + 1 s` (00:00:01 UTC), like every bar; there is no after-hours finalisation delay. First dataset: `coinbase_btc_eth_usd_daily_v1`, Coinbase Exchange public candles at granularity 86400 s, 2016-01-01 .. 2026-11-01, acquired 2026-11-09: BTC-USD 3957 of 3957 days; ETH-USD 3817 days from its first candle on 2016-05-18 (138 days before its listing and 21-22 May 2016 missing). Coinbase prices stand in for Kraken (ADR-0018).
- `daily-v1` features accept either calendar with unchanged definitions. On UTC days the month-end average needs at most `31 x k` sessions (`DailyWindows.warmup_sessions_on`; the XNYS bound `23 x k` and `warmup_sessions` are unchanged), and a missing UTC day resets the state.
- Candidate D runs on the calendar of its track: a CRYPTO specification needs `Utc247Calendar`, any other the exchange calendar (each refuses the other). Crypto universe ids are `VENUE:BASE/QUOTE` (`KRAKEN:BTC/USD`). A candidate's `max_hold_minutes` on UTC days is exactly `N x 1440`. A registration's feature text comes from the specification (`RuleFamily.features_of`), so a CRYPTO trial says "last UTC day of the month" while the XNYS text bound into T-0014 is unchanged.
- `candidate_d_crypto_monthly_v1.yaml` keeps every Candidate D parameter except the time exit: 365 UTC days instead of 260 XNYS sessions, because the ETF specification states it as "about one year" in sessions. ATR(20) stays 20 bars: it counts observations of the daily range, and a weekend day is a full trading day on a 24/7 market.
- Reports of a crypto study with overnight trials use daily sentences (warm-up in UTC days, the daily data note, why a window can be idle, the trial budget, and section 11 pointing to the stock reports). The 15-minute crypto reports keep their wording byte for byte.
- Nothing changes on XNYS: no XNYS code path reads the new methods, the ETF specifications and registrations hash as before, and the daily, Candidate C and D test suites pass unchanged.

**Consequence.** `configs/research/crypto_daily_v1.yaml` (T-0016) is prepared and not registered; the owner registers it on `sunshine` (runbook §10.2). Its known limits, stated before any run: G2's 100 out-of-sample trades are very probably out of reach for a monthly rule on two pairs (at most 186 month-end decisions over the 31 test windows); at USD 50 Kraken's minimum orders exceed the USD 0.125 risk budget once BTC is above about USD 9,200-16,300 or ETH above about USD 275-490, so most entries after 2020 will be rejected, and the minimums are today's, not the historical ones; the Coinbase-Kraken basis is measured on one recent 15-minute week only. Candidate C on crypto is not prepared; it would need its own specification and trial. Before registration (2026-11-09, no crypto result of the rule seen) the study's policy was set to the research-only ADR-0041 variant (1% risk per trade), for feasibility only: under the base policy the minimum orders would block most entries, and the owner had chosen larger positions at USD 50 for the same reason.

## ADR-0043 - Stocks sleeve of USD 100 for paper practice, run like production; certification orders are not strategy entries

**Context.** On 2026-11-09 the owner set the plan: grow the account to USD 10,000 with no new money, starting from three sleeves of USD 100 (stocks, crypto, gold/CFD commodities), compounding profits, and practising exactly as production would run (trading in USD). An interim decision that evening (USD 1000 practice capital, four positions) was withdrawn the same night and never ran. That afternoon every practice candidate had been rejected ENTRY_LIMIT_REACHED, because the two `trader paper certify` buys had used up the policy's two entries per session: `entries_since` counted every ENTRY intent of the account. The owner found the rejections on the dashboard before Claude acted on them.

**Decision.**
- `configs/policy/paper_practice_v1.yaml` is the stocks sleeve policy: `base.yaml` scaled to USD 100 (cash reserve 60, capital floor 90) with twelve new entries per session (first four; raised by the owner on 2026-11-10 after a rehearsal of the 2026-11-09 session: of 23 real signals a limit of 4 took 4 trades, 8 took 8 and no limit took 9, the rest being repeats of a signal while both position slots or that stock were already in use). Two open positions, the 20% symbol cap (USD 20), the 40% gross cap and every risk and loss fraction are base's. `base.yaml` stays the canonical, pinned policy for research, replay and live.
- Profits compound without any mechanism of their own: sizing uses the ledger's equity (initial capital plus realized results), and no deposit is ever recorded.
- This amends ADR-0013: a paper configuration may name a `policy/paper_*` file. The runtime configuration refuses such a file outside paper mode, and a live configuration must use `policy/base.yaml`.
- It supersedes ADR-0040's USD 50 practice capital and two entries per session. Practice stays `PRACTICE` evidence: never G2/G3, and G3 starts from a fresh baseline.
- `configs/paper.yaml` uses a fresh baseline: portfolio `pf-practice`, account `acct-alpaca-paper-practice`, in a new database `trader_practice` read from its own variable `TRADER_PRACTICE_DATABASE_URL`, so the bot refuses to start until that is set rather than writing the practice account into the pilot database. The database `trader` keeps the USD 50 pilot history, including the certification run.
- The per-session entry count counts strategy entries only (intents with a candidate). Certification orders are bounded by their own grant and still count for pending-entry and open-position checks.
- `PaperRunner` flags operational rejections (`runtime/session_watch.py`): a `!!` log line per bar, at most two `ENTRIES_BLOCKED` alerts per session, and a session summary line with rejection counts.
- End-to-end proof (tests/test_paper_runtime.py): after a same-day certification run, practice entries are approved, sent through the Alpaca adapter and filled up to the limit, and an unfilled entry is cancelled and frees the next. A shadow check of the 20 real candidates of 2026-11-09 at USD 20 positions found all 20 covering their costs (about USD 0.05 a round trip) and 4 refused for a spread above 10 bps.

**Consequence.** USD 300 to USD 10,000 is a 33-fold gain; no trial so far supports a fast path, and results are reported as measured. Candidate A lost in every backtest window, so the stocks sleeve is expected to lose small amounts while it produces real fills to learn from. Rejections for market and risk reasons stay normal and visible; operational ones raise an alert the same session.

## ADR-0044 - Bounded Capital.com DEMO trading by the assistant, guarded and journalled

**Context.** ADR-0036 connected Capital.com's MCP server for research with trading forced off, because Capital.com issues no read-only API keys. On 2026-11-09 the owner created a demo account with USD 1000 of demo money and allowed Claude to place DEMO trades in gold, silver, oil, indices and EUR/USD, so that CFD practice builds a database of trades for later real money. Live trading stays impossible. capital-mcp at commit 3207be4 reports `CAP_MAX_OPEN_POSITIONS` and `CAP_MAX_WORKING_ORDER_SIZE` but does not enforce them, and its single `CAP_MAX_POSITION_SIZE` is in contract units across all epics.

**Decision.**
- The capital-demo launcher (`trader.config.secrets`) still drops every inherited `CAP_*` variable and forces `CAP_ENV=demo` last. It now sets `CAP_ALLOW_TRADING=true`, `CAP_DRY_RUN=false`, `CAP_REQUIRE_EXPLICIT_CONFIRM=true`, `CAP_ALLOWED_EPICS` to the seven practice epics (`CAPITAL_DEMO_EPICS`, never ALL), a 100-unit size ceiling, 2 open positions and 8 orders a day.
- `.claude/settings.json` no longer denies the five order tools (execute position, execute, update and cancel working order, close position). Account preferences, the demo top-up and every watchlist write stay denied, and every Alpaca MCP order tool stays denied: only the bot trades the Alpaca paper account.
- A PreToolUse hook runs `python -m trader.practice.capital_guard` before every order preview. Execution needs a preview ID, so every order passes it. It blocks unless the epic and size are inside `configs/practice/capital_demo.yaml`, the order has a stop, a journalled PLAN for that epic, direction and size is under 15 minutes old with the same stop, and fewer than 2 trades are open and fewer than 4 were opened that UTC day. An order update needs a recent AMEND. Errors block (fail closed).
- Every trade goes through the hash-chained practice journal `runs/practice/capital_demo_journal.jsonl` (`trader journal add|verify|open`): PLAN with thesis, stop, target and risk (at most 1% of the sleeve's equity, ADR-0046), OPEN, AMEND, CLOSE with P&L and financing, REVIEW with the lesson. The journal holds no credentials or account numbers and stays out of git.
- The live host `api-capital.backend-capital.com` may not appear in `src/` (test).

**Consequence.** CFD practice produces real demo fills, spreads and overnight charges, recorded with the reasoning behind each trade. Demo results are practice, never G2/G3 evidence. Removing the hook or widening the epics or limits is a deliberate owner change, reviewed like this one.

## ADR-0045 - Market feed for the dashboard

**Status.** Draft (2026-11-09), for the owner's review. ADR-0043 and ADR-0044 are taken on another branch.

**Context.** The owner wants the Trader Console's Markets page to show three labelled rows, Stocks, Crypto and CFDs, each with several instruments. Until now it showed one row of five stocks from the bot's own Postgres bars and the IEX research history. The console is an observer by design (docs/observability.md §1): it makes no outbound HTTP call, holds no keys and never loads the repository `.env`. Live prices therefore have to come from somewhere else.

**Decision.**
- **A data tool, not the trading runtime.** `trader.market` (`python -m trader.market refresh | watch --every N`, also `trader market ...`) fetches the watchlists in `configs/market/watchlists.yaml` and writes files. It cannot place, approve or route an order: `tests/test_boundaries.py` forbids it to import brokers, execution, risk, runtime or storage, and no strategy, research study or the paper runtime reads its output. It imports no pandas, numpy, DuckDB, SQLAlchemy or exchange calendar (an import-budget test), so it can run beside the console all day.
- **Sources.** Stocks: Alpaca IEX bars with the paper keys from the OS keyring (`require_alpaca`), regular session only (09:30-16:00 New York; without the exchange calendar an early-close afternoon is kept), hours anchored at 09:30 and the daily bar re-stamped to the open. Crypto: Kraken public OHLC, keyless, at most 720 rows per call, one call a second. CFDs: Capital.com **demo** prices as bid/ask mids without volume.
- **Capital.com keys.** The feed may read the `capital-demo` plain service (`require_capital_demo`: `.env` first, then the keyring; ADR-0039) for one purpose only: to log in to the demo host (`POST /api/v1/session`) and read `/api/v1/prices`. The host is hard-coded; the live host's name must not appear anywhere under `src/` (a test scans for it). The session tokens stay in memory; a 401 logs in again once, then the feed backs off for 30 minutes, also across restarts (`retry_after` in the index). `UrllibTransport.open_session` posts only to an `https` `/api/v1/session` URL. The capital-demo MCP settings (`forced_env`) are unchanged.
- **Files under `data/market/`, separate from research data.** `data/market/<group>/<SYMBOL>.json` and `data/market/index.json` (gitignored with the rest of `data/`), each replaced atomically, the index last. Nothing is written under `data/history/` or to `research/data_manifest.json`, whose checksums bind research to its data; feed files are display data and never research evidence. Retention: 5-minute bars 14 days, hourly 180 days, daily 730 days.
- **The console stays offline.** It reads `data/market/` only (`GET /api/market`, `GET /api/market/:id`), serves only ids the index lists and never builds a path from a request. `npm run dev` starts the feed as a separate child process (`--no-market` skips it); its keys never enter the console process.
- **Day rules.** Stocks aggregate on New York sessions as before; crypto and CFDs on UTC days (`shared/aggregate.ts` `DayRule`), so their daily candles cut at 00:00 UTC.

**Consequence.** The Markets page shows 23 instruments in three rows, each row with its source, last update and a stale chip after two missed refreshes. The feed adds a few Alpaca data requests every 5 minutes on the paper keys (well inside the free plan's rate limit) and a Capital.com demo session. Deeper 5-minute and hourly history for crypto and CFDs builds up while the feed runs (Kraken serves only the latest 720 rows; Capital.com is paged back one page per series and refresh). A feed that stops leaves the last files in place and the rows turn stale; nothing in the bot depends on it.

## ADR-0046 - Three sleeves of USD 100, compounding, no deposits

**Context.** On 2026-11-09 the owner fixed the plan: grow the account to USD 10,000 without any further money, from USD 100 in stocks, USD 100 in crypto and USD 100 in gold and CFD commodities, reinvesting every profit, and practise exactly as production would run. The bot trades only the stocks sleeve today (ADR-0043); there is no crypto or CFD broker adapter in `src/trader`.

**Decision.**
- **Stocks sleeve:** the paper bot on Alpaca paper under `configs/policy/paper_practice_v1.yaml` (ADR-0043).
- **CFD sleeve:** the assistant's guarded Capital.com DEMO trades (ADR-0044) under `configs/practice/capital_demo.yaml`. The demo account's USD 1000 balance is not the sleeve.
- **Crypto sleeve:** the assistant's trades on the Kraken CLI's local paper simulator (live prices, 0.80% taker fee, spot, long only) under `configs/practice/kraken_paper.yaml`. The simulator holds no stop orders: the journalled stop is checked at each review and closed by hand.
- **One rule set for both assistant sleeves** (`trader.practice.journal`): each sleeve's equity is USD 100 plus its journal's closed results; risk per trade is at most 1% of that equity, so position sizes grow and shrink with it; at or below 90% of the start no new trade is planned until the owner reviews; two open trades per sleeve. The PreToolUse guard covers Capital.com order previews and `kraken_paper_buy`/`kraken_paper_sell`.
- **No deposits, ever:** nothing records one. The goal is the sum of the three sleeves; `trader journal summary` writes `runs/practice/sleeves.json`, and the console's Desk shows the three sleeves and their sum against USD 10,000.
- Practice results of all three sleeves are practice, never G2/G3 evidence.

**Consequence.** USD 300 to USD 10,000 is a 33-fold gain. No registered trial supports a fast path: the best results so far are slow trend rules that made a few percent a year and less than buy-and-hold. The sleeves therefore measure honestly what this capital can do, with real fees, minimum sizes and financing; results are reported as measured. A crypto or CFD sleeve moves into the bot only with its own adapter, certification and gates.

## ADR-0047 - Intel feed: news, insider filings and pre-market activity

**Context.** The bot decides from prices only: completed 5-minute bars and polled quotes. It reads no news, no insider filings and nothing that happens before the open; pre-market bars are filtered out. On 2026-11-10 the owner asked for that information to flow into the pipeline, from news sources, insiders, pre-market trading and X. The strategy specification already sets the rules for news (strategy_and_profitability.md §7, architecture.md §3 `NewsAnnotation`): add it once the price-only pipeline works; use entitled feeds and issuer or regulator sources with publication and first-seen times; remove syndicated duplicates and keep revisions; keep article content away from tools and broker credentials; a model that reads text needs forward evidence before it may decide a trade; a news failure can never block an exit.

**Decision (owner, 2026-11-10).**
- **Collect, show, then test.** The feed gathers the information, the console shows it, every trade can be read against what was known at the time, and the assistant reads it before its crypto and CFD practice trades. Nothing in this decision changes what the stocks bot does. A rule that uses this information is added only after a registered trial passes its gate, with its own decision record.
- **A data tool, not the trading runtime.** `trader.intel` (`python -m trader.intel refresh | watch | brief`, also `trader intel ...`) reads the sources in `configs/intel/sources.yaml` and writes files. `tests/test_boundaries.py` forbids it to import brokers, execution, risk, runtime or storage, and forbids everything except the feed itself and the command line to import `trader.intel`: no strategy, feature, model, risk rule, runtime loop, research study or report reads it. It imports no pandas, numpy, DuckDB, SQLAlchemy or exchange calendar.
- **Sources.** News: Alpaca's news endpoint (Benzinga) on the paper keys; headline, summary and metadata only, the article body is never requested. Insiders: SEC EDGAR Form 4, straight from the regulator, for the eight companies on the watchlist. Pre-market: Alpaca SIP 5-minute bars, 04:00 to 09:30 New York; the free plan serves SIP data once it is 15 minutes old, so each request ends 16 minutes ago. CFDs have no news of their own here: where a US-listed fund tracks the same thing its news is shown as a labelled stand-in (GLD, SLV, USO, SPY, QQQ); DE40 and EUR/USD have none and the console says so.
- **X and Grok: not connected.** Both charge about USD 5 per 1,000 posts read (X pay-per-use; xAI's X search plus model fees). The owner declined the cost for this version. A later source is one more entry in the configuration and one class behind the same protocol.
- **Pre-market is read, not traded.** `extended_hours_enabled` stays false and the entry window is unchanged.
- **The SEC contact.** EDGAR asks automated readers to name a contact address in the `User-Agent`. It is the owner's line `SEC_EDGAR_CONTACT=...` in the gitignored `.env` (plain service `sec-edgar`; keyring as fallback), never in a committed file or in output. Until it exists the insider source reports `waiting` and the other two keep working.
- **Point-in-time store under `data/intel/`.** Append-only JSON lines, filed by kind and month: each record carries the source id and link, the instruments, `published_at`, `updated_at`, `first_seen_at` (when this machine first saw it), a category with its `extractor_version`, a cleaned headline and summary, `provenance` and a content hash. A changed item is appended as the next revision; the same content seen again adds nothing. One writer at a time (a lock file). Nothing is written under `data/history/` or to `research/data_manifest.json`: these files are not research inputs, and a later trial builds its own checksummed datasets.
- **Categories by fixed keyword rules** (`keywords-v1`): earnings, guidance, financing, mna, analyst, legal_regulatory, unknown. No language model reads the text. This is the deterministic baseline the specification asks for.
- **What was known.** `trader.intel.snapshot.snapshot` is a pure function of the stored records and a time: a record counts only if it had been seen by then, in the revision that was current then; the time windows use the publication time, so a backlog read after an outage is not counted as fresh. The feed appends a snapshot per instrument whenever it changes, and one run line per refresh. The console shows the newest snapshot at or before a trade's decision time, with its age and the last feed run. `DecisionRecord`, the database schema and the runtime are unchanged, so the pinned replay hashes hold.
- **Untrusted text.** Headlines and filings are someone else's words. They are reduced to one plain capped line before storage, rendered as plain text, and printed by `trader intel brief` as quoted data under a notice that they are not instructions. The control that matters is unchanged: the assistant's trade tools stay behind the plan-first guard and its limits (ADR-0044, ADR-0046). A Form 4 that declares a DTD or an entity, or an unexpected encoding, is refused before parsing; the byte fetch is https only, follows no redirect, accepts no encoded answer and has a size cap.
- **Journal evidence.** A practice PLAN may carry an optional `evidence` list of intel references. It is never required for an order, and a plan without it serialises exactly as before.

**Consequence.** The Markets page shows news, insider filings and pre-market activity beside each instrument's prices, and a trade's story shows what was known when it was decided. The feed adds a few Alpaca data requests every 5 minutes and a handful of SEC requests every 30 minutes, far inside both limits. A feed that stops leaves the last files in place and the run log shows the gap; nothing in the bot depends on it. Whether any of this information helps a rule is unknown until a trial is registered and run: backfilled history has no true first-seen time, the hypothesis must be fixed before outcomes are studied, and such a trial uses one of the remaining trial slots.

## ADR-0048 - Sized ETF dip-buy trial T-0017: negative at USD 50

**Context.** On 2026-11-10 the owner asked the assistant to use the four remaining trial slots to look for a rule that earns, while the stocks bot trades Candidate A as a live test. T-0013 (Connors and Alvarez RSI(2) dip-buy on 13 US equity ETFs) had lost 1.59 USD net on 55 trades at 0.25% risk but made 1.23 USD before fees, and T-0015 had shown that larger positions turned the trend rule from a loss into a gain.

**Decision.** Study `etf_dip_sized_v1` registers one trial, T-0017: the frozen Candidate C spec on the same 13 ETFs, data, windows, fills and fees as T-0013, under `configs/policy/trend_sized_v1.yaml` (1% risk per trade). The policy is the only change. Budget 17 of 20.

**Result (development period, 38 three-month windows from USD 50).** 239 trades, net -6.01 USD, win rate 53.1%, profit factor 0.68; stressed -6.63 USD; 26 of 38 windows lost. Before fees the rule made 4.13 USD (mid-price gross 5.20 USD); fees were 9.56 USD, about USD 0.04 per round trip, against a gross gain of about USD 0.02 per trade on USD 10 positions. Verdict NEGATIVE_NET; G2 not passed. The diagnostic view at USD 1,000 (positions near USD 200, same absolute fees) made 73.49 USD over the same period, 7.3% of capital in about nine and a half years.

**Consequence.** The dip-buy rule is not traded. The finding that matters for the stocks sleeve: the pass-through fees round to about 4 cents per round trip whatever the position size, which is 0.4% of a USD 10 position and 0.2% of the practice sleeve's USD 20 position. A stock rule at this size needs an average gross gain per trade well above that; the dip-buy rule's is about 0.2%. Three trial slots remain.

## ADR-0049 - Crypto and CFD sleeves: short-term trades on Capital.com, one tested setup

**Context.** The assistant's first trades in the crypto and CFD sleeves followed the monthly trend rules T-0015 and T-0016: BTC with a target 38% away, ETH 59%, US100 25%, each held until a month-end signal. On 2026-11-10 the owner asked for both sleeves to trade short term, day to day and a week at most, with nearer targets, so that the sleeves grow by trading and position sizes grow with them. He then left the choice of venue and method to the assistant: judge it as a professional trader would, decide, and build it.

**Decision (2026-11-10).**
- **Short-term rules in the journal** (`trader.practice.journal`, read from `configs/practice/*.yaml`): `max_reward_risk: 2` - every PLAN needs a target, at most twice its planned risk from the entry; `max_hold_hours: 168` - every open trade has a time stop one week after its OPEN, a PLAN or an AMEND may set a shorter `max_hold_hours`, and no new PLAN is accepted in a sleeve while one of its trades is overdue; an AMEND that changes the target or the hold is held to the same limits, measured from the entry and risk as planned. `trader journal open` prints each trade's stop and target in force and its time stop.
- **Crypto moves to Capital.com CFDs; the Kraken simulator is retired.** Kraken spot costs 1.6% a round trip (0.80% a side), which a trade of a few days cannot earn back. Capital.com quoted BTC/USD at a spread of 0.07% of the price and ETH/USD at 0.09% on 2026-11-10, with 0.062% a night on a long (a short is paid 0.014%) and 50% margin. `configs/practice/crypto_cfd.yaml` (sleeve name `crypto-cfd`) replaces `kraken_paper.yaml`: BTCUSD and ETHUSD, long or short, stop and target held at the broker. The sleeve keeps its journal file and chain, so its equity carries over. The two Kraken trades were closed the same day: -0.32 USD and -0.31 USD, of which 0.33 USD was fees; the sleeve stands at USD 99.36.
- **This widens ADR-0044** by two epics: `CAPITAL_DEMO_EPICS` now lists nine, and the launcher allows 4 open positions and 16 orders a day (two sleeves, each 2 open trades and 4 opens a day). The guard checks an order against the sleeve that owns its epic (`capital_guard.route`), each with its own journal, limits and open-trade count, and blocks every Kraken simulator order. The Capital.com connector reads the allowlist when it starts, so crypto orders work after it is reconnected.
- **One setup is traded: the index dip** (`configs/practice/setups.yaml`, `trader.practice.setups`, `trader journal scan`). It is the Connors and Alvarez (2008) RSI(2) rule with the parameters of the frozen Candidate C specification, checked by a test: on a completed daily bar the close is above its 200-day average and the 2-period RSI is below 10; buy; sell on the first close above the 5-day average; stop 3 x ATR(10) below the signal close; out after five days at the latest. Trials T-0013 and T-0017 ran this rule on US index and sector funds from 2017 to 2026: it earned before fees (4.13 USD on 239 trades of about USD 10, 0.17% of the position a trade) and lost after Alpaca's cent-rounded fees. On US500, US100 and DE40 the cost is a spread under 0.01% of the price plus about 0.02% a night. Not tested, and said so in the setup file: the CFD instead of the fund, a daily bar that ends at 00:00 UTC instead of the New York close, and DE40.
- **The CFD sleeve's risk limit is 2% at the stop (it was 1%); the crypto sleeve's stays 1%.** The limit is measured at the stop, and this setup's stop is a disaster stop: in T-0017's walk-forward trades 7 of 71 reached it, and the usual losing trade lost about 1.5% of the position, which on the smallest US100 contract (about USD 28) is about USD 0.40. At 1% the smallest contract was too large for most signals. On the feed's bars from August 2025 to November 2026 it fitted US500 on 0 of 24 signal days, US100 on 4 of 27 and DE40 on 15 of 21; at 2% it fits 6, 25 and 21. 2% is the most the journal's rules model accepts.
- **Size follows the limit, not the other way round.** The scan sizes a signal at the largest size inside the limit and the epic's range. When the smallest contract at the rule's stop risks more than the limit, the trade is not taken; the stop is not pulled in to make a contract fit.
- **Not traded.** The stocks rule's shape (a breakout on a short bar, stop 1.5 ATR, target twice the risk) on hourly bars: the intraday rules of trials T-0001 to T-0010 lost in every window. Gold, silver, oil, EUR/USD, Bitcoin and Ether have no short-term setup with a trial behind it, so those markets and the crypto sleeve stay flat until one has. A setup is added to `setups.yaml` only with a registered trial; three slots remain.
- Two open trades per sleeve, the 90% floor and the plan-first guard are unchanged. Sizes grow with a sleeve because the limit is a fraction of its equity; the per-epic maximum sizes are raised when a sleeve has grown by half.

**State on 2026-11-10.** No signal: US500 and DE40 are above their 200-day averages with RSI(2) near 70; US100 closed below its 200-day average, which switches the rule off there. Gold, silver and oil are below their 200-day averages. Both sleeves are flat. The resting US100 limit order of the monthly rule (27,400, target 34,336) was cancelled at the broker at 21:05 UTC, unfilled, by the session that had placed it, and the cancel is in the journal; nothing rests on the demo account. The session that wrote this record had its own cancel refused twice by its permission check, which is why the owner is asked to allow the demo order tools for the project.

**Added the same day** (the first step of the CFD and crypto plan). A sleeve holds one open trade per group of epics that move together (`groups:` in the rules files: the indices; Bitcoin with Ether): the journal refuses the plan or the open, the guard the order, and the scan marks the signal `GROUP_BUSY`. The scan is also a file, `runs/practice/scan.json`, with every open practice trade checked against its stop, target and time stop at the feed's last price; `python -m trader.practice watch`, started by `npm run dev`, replaces it every 15 minutes and the console's Overview shows it. The feed's CFD watchlist and the intel targets gain BTCUSD and ETHUSD so that the crypto sleeve's trades are valued, and briefed, at the broker's own instrument. The index dip's hold is the sleeve's week, 168 hours.

**Consequence.** The sleeves trade only where a rule has earned something in a test, at a cost that leaves it. That is few trades: on the feed's bars from late August 2025 to November 2026 the dip signalled on 21 to 27 days per index in 15 to 19 separate dips, so about two trades a month across US100 and DE40. A sleeve that risks at most USD 2 a trade on a USD 28 contract earns or loses cents to a few dollars a month; the practice record shows whether the rule pays on this broker, and size is a later decision made on that record. Practice results are never G2/G3 evidence. The scan reads the market feed's daily bars (ADR-0045): it is an aid for the assistant's plans, has no order authority, and nothing in the bot reads it.

## ADR-0050 - Modelled fees in the practice ledger, and a day report

**Context.** Alpaca's paper account charges no regulatory fees, and its fills arrive with fee 0 while the fee is pending. The ledger booked that zero and nothing ever replaced it (the known gap of ADR-0035), so the practice sleeve's results left out what production would take. On 2026-11-10 the stocks sleeve made three AMZN round trips: -0.09 USD in the ledger, where the account's fee schedule (ADR-0028) charges another 0.12 USD. The owner judges the sleeve by its daily result and has set "practise like production" (ADR-0046); a result that flatters by more than it shows is not that.

**Decision.**
- An account may set `accrue_modelled_fees: true` (paper configurations only; replay and live refuse it). For such an account the order manager posts every fill that the broker reports with fee 0 and status `PENDING` with the fee the account's schedule charges. A schedule charges per order, so the fill takes the order's fee on everything executed so far less what its earlier fills carry; an order filled in parts costs what one fill would. A fee the broker did report, pending or final, is never replaced, and an unknown schedule accrues nothing.
- The fee stays `PENDING` and is booked as `FEE_ACCRUAL` with a `fee_accruals` row: an estimate, never a broker charge. `configs/paper.yaml` sets the flag for the stocks sleeve.
- Reconciliation adds accruals still pending back to the ledger's cash before comparing it with the broker, for every account: the ledger takes the estimate at the fill, the broker charges later or, on a paper account, never. The one-cent tolerance is unchanged, so a difference nobody can explain still fails.
- No backfill. Fills recorded before the switch keep fee 0; marking them final would be false.
- `trader paper day [--date]` reports one session from the recorded fills: round trips with gross result by the ledger's average-cost method, fees booked, fees the schedule charges that are not booked (the fills before the switch), net result, the position at the close, and losing sessions in a row counted as the console does. It reads only: no broker, no keys.

**Consequence.** From the first session after the switch the practice ledger, the loss limits and the console show results after fees, about USD 0.04 a round trip on the conservative schedule. The sleeve's cash for sizing falls by the same cents. A certification run on this configuration also accrues the schedule's fees on its fills; its `observed_*_fees_usd` fields stay the broker's own figures. The estimate is the per-order upper bound of ADR-0028: Alpaca's schedule totals each fee type per day before rounding up, which is cheaper on a day with several sells, so a practice day with many round trips reads slightly worse than a live one would. A live account needs the broker's end-of-day fee activities read into `Ledger.finalize_fee` before its accruals can be trusted; that is not built, and live is not possible in this build.

## ADR-0051 - Catalyst labels: a forward-only reading of the intel feed

**Context.** The owner's research file (`AI_TRADING_REPOSITORY_RESEARCH.md`, section 8) recommends one narrowly defined language-model feature: classifying timestamped news and filings. Its one positive first-hand report uses a model exactly so, to read filings inside an otherwise ordinary strategy. A language model cannot be tested on history honestly: it has read about the past, and its own changelogs in that file record fixes for future information leaking into backtests. Only a judgement written down before the prices it might explain can count. The intel feed (ADR-0047) already stores what was known and when; what it lacks is the judgement. The keyword classifier in `trader.intel.classify` sorts headlines into categories and is not one. The operating-cost ceiling is USD 0, so the reader is the assistant during the daily session, not a paid API call per headline.

**Decision.**
- `trader intel label` (also `python -m trader.intel label`) stores a catalyst label for one instrument and one session: catalyst (`earnings`, `analyst`, `corporate`, `product`, `legal`, `sector`, `macro`, `none`, `unclear`), direction (`up`, `down`, `unclear`), confidence (`low`, `medium`, `high`), the references it rests on, one line of note, and who judged. `trader intel labels` lists them.
- The store, not the caller, sets `first_seen_at`. A label for a session that is past is refused; so is a session more than four days ahead, a weekend day, and an instrument the feed does not follow. Every reference must be one the store already holds: a stored record, or a snapshot of the instrument with exactly that time. A label without a catalyst (`none`) carries no direction.
- Labels are append-only in `data/intel/labels/<YYYY-MM>.jsonl` under their own lock. A change of mind is the next revision of the same id (`stocks:NVDA@2026-11-11`); every revision stays on file with its time.
- Each row carries the rubric it was written under (`catalyst-label:v1`, docs/runbook.md section 14): every stock of the watchlist is labelled every session before the open, `none` included, from what `trader intel brief` shows and nothing else. A changed rubric is a new method name.
- Nothing that decides a trade reads the labels. The import boundary of ADR-0047 covers the module, and a test refuses the files' path in research, strategy, feature, model, risk, runtime and execution code.

**Consequence.** From the first labelled session there is a record that a later study can trust for its timing: which instruments had a company-specific catalyst before the open, and which way it pointed. It answers nothing yet. Ten instruments give at most ten labels a session, so months pass before there are enough for a test, and a rule that uses them still needs a registered trial and its own decision record (ADR-0047). A day without a labelling session leaves a gap that cannot be filled afterwards. The console does not show labels yet.

## ADR-0052 - CFD studies on the broker's own quotes: two-sided data, a sibling engine, two trials fixed in advance

**Status.** Built, registered and run (2026-11-11); results below: the seams in `research/study.py`, the two-sided transport and datasets, the instrument specs, fee schedule and research policies (`configs/instruments/capital_demo_cfds_v1.yaml`, `configs/fees/capital_com_cfd_demo_v1.yaml`, `configs/policy/cfd_sleeve_v1.yaml`, `crypto_cfd_sleeve_v1.yaml`), the engine (`research/cfd_engine.py`), the rules and the broker calendar (`strategies/cfd_rules.py`, `BrokerCfdCalendar`), the study kind and report (`research/cfd_study.py`, `research/cfd_report.py`, `trader research cfd`) and the two study files (`configs/research/cfd_index_dip_v1.yaml`, `cfd_crypto_momentum_v1.yaml`). The results follow the data findings.

**As built, where the text below is more general.** A bar's spread is the wider of its quoted spreads at the open and at the close (the two simultaneous quotes), at least the registered floor. Daily bars are built from the hourly datasets (see the data findings). RTY trades once its 200-day average is warm on its hourly history, which starts on 2019-02-13 (about the end of 2019), not from a fixed date. The index dip's stop is a disaster stop and its 5-day average at the signal is only the sizer's cost gate, as in Candidate C. Warm-up before each window: 400 days for T-0018, 30 for T-0019. Annualisation: 260 weekday sessions for index CFDs, 365 days for crypto.

**Context.** ADR-0049 put both practice sleeves on Capital.com demo with one setup, the index dip, whose evidence is a trial on US funds at Alpaca's costs. Nothing traded on Capital.com has been tested on Capital.com's prices. The research engine cannot do it: it is long only, pays every position in full from cash, charges no overnight financing, assumes one constant spread, caps size by bar volume and knows only the XNYS and 24/7 calendars. Its result hashes are pinned for the 17 registered trials, and making it two-sided would move them. COMMODITIES_AND_CFD_TRACK.md section 6 asks for the intended broker's executable bid/ask for CFD execution analysis. On 2026-11-10 the Capital.com demo API was found to serve its own history: hourly bid/ask bars at least since 2018 (gold), 2019 (BTC) and 2020 (US100), daily bars since 2016, each with a tick count. The spread changes over time (BTC 0.39% of the price in 2019, 0.07% now) and the oldest daily bars carry no spread. The owner approved the plan on 2026-11-10; it also draws on his review of open-source trading systems, whose usable advice is method: separate validated tracks behind one risk interface, a trained filter before any language model, a transparent forward record.

**Decision.**
- **A sibling engine and study kind, not a second mode of the existing engine.** `kind: cfd_study`, run with `trader research cfd`, in `research/cfd_engine.py` and `research/cfd_study.py`. It reuses, unchanged, the registry and trial budget, `walk_forward`, `_g2`, the bootstrap and reality check in `research/stats.py`, and `Sizer` (`risk/sizing.py`), which already handles both directions, margin as a cash check and financing. `study.summarize` now delegates to `summarize_series` and `walk_forward` takes its starting capital, with no arithmetic change.
- **Data: the broker's two sides, each a checksummed file.** Source `capital_demo_prices`; `CapitalPrices.fetch_quote_bars` keeps bid, ask and the tick count; `fetch_capital_quote_history` pages backwards 1000 bars at a time and reports where and why it stopped. A row missing a side, with an ask below its bid, or with an invalid bar on either side is left out and counted. Each epic is stored as two ordinary history files, `CAPITAL:<EPIC>.BID` and `.ASK`, with the same bar starts, so the Parquet schema and every existing dataset are untouched; `data/quote_bars.py` loads them as one sequence and joins the Sunday-evening bar to Monday's. Calendar `BROKER_CFD` builds no expected grid: the bars that exist are the record of when the market was open. Each file's quality report adds the spread by year (median and 95th percentile, in basis points), the rows with no spread, bars per weekday and weekdays without a bar. Four datasets are requested to 2026-11-01: CFD daily from 2016 and hourly from 2018 (GOLD, SILVER, OIL_CRUDE, US500, US100, US30, RTY, DE40, EURUSD), crypto CFD daily and hourly (BTCUSD, ETHUSD).
- **Keys.** Research ingest is the second reader of the `capital-demo` keys, after the market feed (ADR-0045): it logs in to the demo host and reads `/prices`. The host stays hard-coded, the live host's name may not appear under `src/`, and no order, position or account call is added.
- **Sleeve economics in the simulation.** USD 100 at the start of each window; risk at the stop 2% of equity for index and commodity CFDs and 1% for crypto; total notional never above equity; each epic's minimum size and step; at most two open trades and one per group of epics that move together. A signal whose smallest contract risks more than the limit is recorded as not taken. Entry is a market order at the next bar's open, on the ask for a long and the bid for a short; a stop or target triggers on the side that would fill; a gap through a stop fills at the open. Financing is charged each night at the broker's charge time with the weekend multiple, at the long and short rates read on 2026-11-10 and stated with that date in the registration.
- **Cost views by transforming the quotes.** Base: quotes as stored, 1 bp of slippage a side, and a floor per epic at today's spread where old bars show less. Stressed: spread and slippage doubled, financing charges times 1.5, financing credits zero, 5% of entries missed. Diagnostic: mid prices, no financing, labelled unachievable. An entry is refused when the quoted spread is above 20 bps (CFD) or 60 bps (crypto).
- **Two trials, fixed here before any result exists.** They use two of the three remaining slots; nothing below is tuned.
  - **T-0018, the index dip on index CFDs.** Capital.com daily bars, long only, trading from 2019-01-01 (RTY from 2020-01-01) with 2018 as the indicators' run-in, development to 2026-08-01 and the last three months locked as in every study (see the data findings below for why not 2016), US500, US100, US30 and RTY (quoted in USD; the indices of the four funds T-0017 tested). The frozen Candidate C parameters: close above the 200-day average and Wilder RSI(2) below 10, buy the next open; sell at the open after the first close above the 5-day average; stop 3 x ATR(10); out after 168 hours. DE40 is left out: it was in no trial and its euro conversion cost has not been read. Known before registration: `trader journal scan` showed this rule's signal days for August 2025 to November 2026, and T-0013 and T-0017 showed it on funds. The daily bar ends at 00:00 UTC, not at the New York close, so this is a new hypothesis and not a replication.
  - **T-0019, weekly momentum on crypto CFDs, long and short.** Capital.com daily bars of BTCUSD and ETHUSD, trading from 2018-03-01 with the weeks before as run-in, development to 2026-08-01 and the last three months locked. At the bar that closes on Monday 00:00 UTC: long when the 7-day return is positive, short when it is negative; stop 3 x ATR(10); target twice the risk; out after one week; one crypto trade at a time, the larger absolute 7-day move first. Long and short results are reported separately.
  - **T-0020** is held back and chosen after the first two are read; its registration will say it was designed afterwards.
- **What a result does to the practice sleeves.** A setup is traded only if its trial is net positive in the base and stressed views and in the walk-forward summary; G2 is reported and not required for practice. If T-0018 is positive, the index dip trades its four indices (US30 and RTY join the demo allowlist); if it is negative, the index dip is removed from `configs/practice/setups.yaml`. If T-0019 is positive it becomes the crypto sleeve's setup; otherwise that sleeve stays flat.

**Data findings, 2026-11-10 (daily datasets acquired; no rule has been run on them).** The quality reports decide the periods above.
- **2016 and 2017 are not usable** for US500, US100, US30, gold, EUR/USD, BTC and ETH: the source's bid and ask disagree by tens to hundreds of basis points (gold: a median of 592 bps in 2016), and in about two thirds of the rows the ask is below the bid, so those rows are left out and the series has holes (US500: 186 of 260 weekdays missing in 2016, 3 of 261 in 2018).
- **2018 has prices and no spread** for the indices, oil and EUR/USD (bid equal to ask on most rows, 251 of them for US500): good for an indicator's run-in, not for a fill. From 2019 the quoted spread is plausible and falls over time: US500 2.4 bps in 2019 and 0.8 in 2026, US100 2.2 and 0.6, US30 1.9 and 0.4, RTY 2.5 and 1.8, gold 2.1 and 1.1.
- **RTY** has no spread until mid-2018 and 45 and 29 weekdays missing in 2018 and 2019; it is complete from 2020. **DE40** is complete but carries no spread before 2024. **Crypto** is complete from 2018 with wide early spreads: BTC 88 bps in 2018, 44 in 2019, 28 in 2021, 7 in 2026; ETH 119, 95, 34 and 8.
- From 2018 on, US500, US100, US30, gold and EUR/USD have at most five weekdays a year without a bar (holidays). Volume is a tick count.
- **The hourly datasets are better than the daily ones** (acquired the same day): about 52,000 bars an epic from 2018-01-02 to 2026-10-30 and 76,500 for each crypto CFD, with a real spread in every year, 2018 included (US500 2.2 bps, DE40 1.4, EUR/USD 0.7, gold 1.9, BTC 87) and for DE40 throughout. Between 0.5% and 2% of rows are left out for an ask below the bid or a missing side; 12 to 14 weekdays have no bar in almost nine years (29 for DE40), none for crypto. RTY's hourly history starts on 2019-02-13, where the source ends.
- **So a study builds its daily bars from the hourly dataset**, one bar per UTC day [00:00, 24:00) with the Sunday-evening hours joined to Monday, the same day as the broker's own daily candle and as `trader journal scan` reads. The daily datasets stay as a cross-check of that aggregation. T-0018's periods are unchanged; its 2018 run-in then also has a quoted spread.
- So T-0018 has about seven and a half years of trading on four indices that mostly dip together, with one index trade at a time. It may end near the 100 out-of-sample trades G2 asks for; that is reported as it comes out, and no trade is manufactured to reach it.

**Results, 2026-11-11** (`research/reports/cfd_index_dip_v1_report.md`, `research/reports/cfd_crypto_momentum_v1_report.md`; USD 100 at the start of each three-month window; net of the quoted spread, slippage and financing).

| | T-0018 index dip, index CFDs | T-0019 weekly momentum, crypto CFDs |
|---|---|---|
| Period, windows | 2019-01 to 2026-07, 31 | 2018-03 to 2026-07, 34 |
| Trades (base) | 122, all long | 372: 196 long, 176 short |
| Net, base view | +4.40 USD (gross +7.85, spread and slippage 1.63, financing 3.45) | +19.51 USD (gross +23.50, spread and slippage 6.43, financing 3.99) |
| Net, stressed view | -0.02 USD | +1.23 USD |
| Net at the mid, no financing | +10.01 USD | +32.80 USD |
| Win rate, profit factor (base) | 67%, 1.17 | 51%, 1.28 |
| Walk forward | 53 trades, +1.22 USD | 225 trades, +0.42 USD |
| G2 | not passed: 53 out-of-sample trades, no positive lower bound | not passed: no positive lower bound (reality check p 0.091) |
| Buy and hold over the same test sessions | +88.54 USD, 21% drawdown | +239.50 USD, 35% drawdown |
| Practice rule | **not traded** (stressed view negative) | **traded** (base, stressed and walk forward positive) |

Read plainly: the index dip earns before financing and spread and about nothing after them; overnight financing cost it nearly as much as its gross gain. Weekly momentum on the crypto CFDs earns a little in every view, mostly from its longs (+16.78 USD; shorts +2.73 USD with the financing credit), and far less than holding the coins. Neither is evidence of an edge at the project's standard. In 2018 most crypto entries were refused for a spread above 60 bps, so the first two windows have no trades.

**What changed in the practice sleeves.** `configs/practice/setups.yaml` lists one setup, `weekly_momentum_v1` (T-0019's rule, unchanged) for the crypto sleeve; the index dip is removed and the CFD sleeve has no setup and stays flat. `trader journal scan` and the practice watch read it on the market feed's daily bars of the Capital.com crypto CFDs (scan file version 2) and show each Monday 00:00 UTC the week's side, the size inside 1% of the sleeve and the stop and target; the console's Signals block shows the same. US30 and RTY do not join the demo allowlist. One trial slot remains (T-0020); a trained filter on T-0019's signals is not justified by a result this thin.

**Consequence.** A CFD rule can be tested on the quotes it would be dealt at, with the spread of each year and the cost of each night. The existing engine, its datasets and its 17 results are unchanged. Limits known now: financing rates are today's, applied to every year; the Friday multiple and the charge time are the broker's stated terms and are checked against the first journalled overnight trade; demo quotes may be better than live fills; a dataset the source later recomputes cannot be restored bit for bit. A trained filter on top of a rule (a rolling logistic or LightGBM veto, as `research/training.py` does for stocks and crypto) is considered only for a rule that passes here and yields a few hundred candidates, and needs a budget raise by the owner.

## ADR-0053 - One host: the mini PC runs everything; the laptop copy is frozen

**Context.** Until 2026-11-11 everything ran on the owner's laptop (16 GB, Wi-Fi only): VS Code and the Claude chats, Docker with the one PostgreSQL container, the console, the feeds and the paper bot. The laptop could not carry it. Memory ran out with a few chats and a full test run open, so Docker was capped at 2 GB and full runs were rationed. It sleeps when the lid closes. Its Wi-Fi adapter changed band about once an hour and each change paused entries for a minute. Its clock drifted past the 250 ms limit (0.47 s on 2026-11-10). A session timer set in a chat died with that chat. An idle mini PC (32 GB) is reachable from the laptop by remote desktop. Most of the project's state is not in git: the ledger sits in a Docker volume, the Alpaca paper keys in Windows Credential Manager, and `.env`, `ui/.env.local`, `.desk/`, `data/`, `runs/`, Claude's memory, plans and chat history in untracked folders. The bot's one-worker rule is a lock inside its own database (architecture.md section 5, `ops/leadership.py`), so it cannot see a second computer that has its own copy of that database.

**Decision (owner, 2026-11-11).**
- **One host.** The mini PC runs the database, the console, the feeds, the bot and Claude's work. The laptop is only the screen.
- **The switch is the database export.** Before the export only the laptop runs anything. After it the laptop copy is frozen: it never runs the bot or the console again, Docker stays off there, and pushing from it is disabled (`git remote set-url --push origin DISABLED`). Two computers can therefore never trade the same paper account or write two ledgers. Until the mini PC has passed its checks the laptop can be put back into service; once the mini PC has run a session it is the only truth. The laptop copy, its volume and the package are kept two weeks as a backup, then deleted.
- **One package carries the state that is not in git.** One archive with the repository and its `.git`, the untracked project files, the database files, Claude's memory, plans, rules and chats, the Kraken programs, and the Alpaca paper keys as a keys inbox file that is imported with `trader secrets import` and then deleted (ADR-0032). `.venv`, `node_modules`, caches and the worktree copies are not carried; they are rebuilt. The package holds secrets, so it is never uploaded or committed.
- **The database moves as an exact copy of its volume**, restored under the same image (saved and loaded, not pulled again) and the same volume name. The project folder keeps its name for that reason: compose derives the volume name from it (ADR-0037). A text dump travels as a safety net. A fingerprint taken on the laptop, the row count of every table in `trader` and `trader_practice` and the Alembic revision, must equal the mini PC's before the console or the bot is started.
- **Work continues in fresh chats.** The old chats are kept for reading. What each open chat still owed, and the prompts that continue it, are written down in the owner's private notes on the move.
- **The practice session of 2026-11-11** runs on the mini PC only if every check is green by 14:00 UTC; otherwise it is skipped. It does not run on the laptop.
- **The record** of the move is kept in the owner's private operations notes: what lived where, what the package holds, the mini PC's settings and why (clock service, never sleep, 6 GB for Docker, long paths, one Node install), the procedure that finishes the move, its checks, and the rule to leave the remote session by closing its window and never by signing out. It names machines and local paths, so it is not part of this repository.

**Consequence.** No code, configuration, policy or schema changes; the host role stays `DEV`. The limits that came from the laptop end: all data connections stay on and full test runs are allowed. The workarounds for the laptop's HTTPS scanning (`UV_SYSTEM_CERTS=1`, `truststore`, the `mirror.gcr.io` image) stay, because they do no harm without a scanner and the saved image carries that name. The project now depends on one computer. Signing out of it or restarting it stops the database, the console, the feeds and the bot, and nothing starts them again by itself; a start-up task is a suggestion, not built. After the two weeks the code has GitHub as its second copy, but the ledger, the journals and `data/` have none until a backup routine exists. A later move repeats this procedure from the mini PC.

## ADR-0054 - The paper broker does charge fees: reconciliation reads them

**Context.** ADR-0050 rests on a statement that is wrong: that Alpaca's paper account charges no regulatory fees. It does. It books them as `FEE` account activities during the night after the trade date, one activity per fee type for the whole day: for the six fills of 2026-11-10 it took USD 0.01 (`CAT`) at 01:07 UTC and USD 0.01 (`REG`) at 01:16 UTC on 2026-11-11, and the same two cents a night earlier for the certification fills of 2026-11-09. The second pair went unnoticed because the practice baseline was read after it. The first session after such a night, 2026-11-11, started with `startup recovery: reconciliation FAILED`: the broker's cash was 0.02 lower than the ledger could explain, and entries stayed paused. The cash check was right to fail; the ledger simply could not see the cause.

**Decision.**
- `BrokerAdapter.list_fee_charges()` returns the fees a broker took from cash on its own since the account's baseline, as `BrokerFeeCharge` (id, kind, amount, currency, booking time). The Alpaca adapter reads `GET /v2/account/activities/FEE` newest first and counts a fee from its booking time (`created_at`), not its trade date: a fee booked before the baseline is already in the baseline cash. Only `executed` activities count; a `canceled` one is left out; any other status fails the read, because what it means for the cash is not known. The replay broker reports fees on its fills and returns none.
- Reconciliation subtracts these charges when it compares cash: ledger cash change, plus the cent rounding of the fills, plus accruals still pending, minus the broker's own fee charges, must equal the broker's cash change within one cent. A passed run notes the amount and the number of charges. Cash that no fee activity explains still fails.
- **The ledger is not changed.** Accruals stay pending estimates and no charge is booked as a ledger entry. The check compares the same two books as before; it only knows one more thing about the broker's.

**Consequence.** A session after a night with fees starts with a passed reconciliation. The practice ledger still shows the schedule's estimate, about USD 0.04 a round trip, where the broker took USD 0.02 for a whole day with three round trips: the sleeve's result reads worse than its account by the difference, as ADR-0050 said it would, and the fills of 2026-11-10 still carry no fee at all although the broker charged two cents for them. Booking the broker's charges into the ledger, so that each day's accruals are replaced by what was taken (`Ledger.finalize_fee`), is the next step and needs a rule for sharing a day's charge among its fills; it is not built. The research fee basis (ADR-0028, the per-order upper bound) is unchanged; the two nights seen so far say a day costs two or three cents however many orders it has, which a study at this capital should weigh. Other cash movements a broker books on its own (dividends, interest, journals) are still unexplained differences and fail the run.

## ADR-0055 - Day trades on hourly bars: four trials on the 24-hour channel

**Status.** Written on 2026-11-11 before any of the four trials was registered or run. Results are added below once they exist.

**Context.** The owner wants the crypto sleeve and the gold and CFD sleeve traded intraday, every day, and found both idle on 2026-11-11: the crypto sleeve trades one weekly rule (T-0019, next decision on Monday) and the CFD sleeve has no setup since the index dip failed its trial (T-0018). ADR-0052 lets a setup into the practice sleeves only after a registered trial on the broker's own quotes is net positive in the base view, the stressed view and walk forward. No intraday rule has been tested on these instruments; the one intraday rule tested anywhere in the project, the stock breakout on 5-minute bars, lost before costs. Nineteen of twenty trials were used and the last is reserved for the stocks momentum plan. On 2026-11-11 the owner raised the budget ("raise the budget"); the registry records 24.

What the hourly datasets' quality reports say about cost, read before anything was fixed: the broker's median quoted spread in 2026 is 1.1 bps on gold, 0.8 on US 500 and 0.6 on US Tech 100, and has been between 1 and 3 bps since 2018. On Bitcoin it is 6.8 bps in 2026 and 4.8 in 2025, but 16.5 in 2024 and 25 to 87 before; Ether 8.1, 5.9, 19.4 and 29 to 118. Silver quotes about 12 bps and crude oil about 6. Overnight financing is charged at 22:00 UTC: a long pays 1.5 bps a night on gold, 2.2 on an index and 6.2 on Bitcoin or Ether.

**Decision.**
- **One question, asked from both sides, for each sleeve.** The channel is the highest high and the lowest low of the previous 24 hourly bars (mid prices). An hourly close beyond it is a break. One trial trades with the break, one against it: if hourly prices trend after a 24-hour extreme the first earns, if they return the second does, and if neither does within the day both lose their costs. Family `CFD_HOURLY_CHANNEL` (`strategies/cfd_rules.py`), `mode: breakout` or `fade`.
- **The rule, fixed here and not tuned.** Stop: twice the mean true range of the last 24 hourly bars, measured from the decision bar's close. Target: twice the stop's distance with the break, once the stop's distance against it (the practice journal allows at most two). Entry at the open of the next hourly bar. Every trade is closed on the day it was opened, before the financing charge: by 20:00 UTC on the CFD sleeve, with decisions on hours ending 07:00 to 17:00 UTC (London morning to the US afternoon); by 21:00 UTC on the crypto sleeve, with decisions on hours ending 00:00 to 17:00 UTC. Bars are counted, not clock hours, so after a weekend the channel reaches back to Friday.
- **The trials.** T-0021 (with the break) and T-0022 (against it) on GOLD, US500 and US100 in study `cfd_hourly_channel_v1`, 2019-01-01 to 2026-08-01, the period of T-0018. T-0023 and T-0024, the same two on BTCUSD and ETHUSD in study `cfd_crypto_hourly_channel_v1`, 2025-01-01 to 2026-08-01: a day trade pays the spread every day, and before 2025 the broker's crypto spread was three to thirteen times today's, so earlier years would test a cost that no longer exists. Nineteen months is one market regime, and the report says so. The last three months of the data stay locked in both studies. Gold and the two indices are the three CFDs of the sleeve's allowlist that are cheap enough and small enough; the two indices count as one group. T-0020 stays reserved for the stocks momentum plan.
- **Sleeve economics as in ADR-0052**, unchanged: USD 100 per three-month window, 2% of equity at the stop on the CFD sleeve and 1% on the crypto sleeve, total notional at most equity, at most two open trades and one per group, four entries a UTC day, 1 bp of slippage a side, a floor at today's spread, entries refused above 20 bps (CFD) or 60 bps (crypto), the stressed view with spread and slippage doubled and 5% of entries missed.
- **What the simulator gained.** A study may feed its rule the hourly bars themselves (`bars.decision_minutes: 60`); a daily study's registration reads as before, and T-0018 and T-0019 still match theirs. A signal may carry a fixed time by which it is closed; an entry that would open at or after it is not taken, and the sizer counts the nights the trade is actually held through, none for these.
- **Promotion is ADR-0052's rule.** A trial's rule is traded in its practice sleeve only if it is net positive in the base view and in the stressed view and the study's walk forward is net positive. The walk forward chooses between a study's two trials in each fold by the validation window before it, or stays in cash, so its result does not depend on knowing afterwards which side won. If both trials of a study pass, the one with the higher stressed result is traded. G2 is reported, not required for practice.

**Consequence.** Two sides tested on each sleeve double the chance that one looks good by luck; the stressed view and the walk forward are the guard, and practice on the demo account is the forward test. A pass here is not evidence for real money. If none passes, the two sleeves have no intraday setup and stay as they are: the crypto sleeve on its weekly rule, the CFD sleeve flat. Trading an hourly rule in practice needs more than a pass: the scan must read hourly bars and print a ticket each hour, and someone must place it within the hour; neither exists yet.

**Results (run on 2026-11-11, after the text above was committed as a8206d9).** None of the four passes; nothing changes in the practice sleeves.

| Trial | Sleeve, side | Trades | Base net | Stressed net | At mid prices, no costs | Walk forward of its study |
| --- | --- | --- | --- | --- | --- | --- |
| T-0021 | CFD, with the break | 2425 | -80.50 USD | -141.34 | -1.36 | -29.23 USD over 987 trades |
| T-0022 | CFD, against the break | 2972 | -103.45 | -150.97 | -12.53 | the same study |
| T-0023 | crypto, with the break | 584 | +15.13 | -27.55 | +54.19 | +15.05 USD over 274 trades |
| T-0024 | crypto, against the break | 408 | -58.87 | -66.51 | -49.72 | the same study |

Net results are sums over independent three-month windows of USD 100 (31 windows for the CFD study, 7 for the crypto study).

- **Gold and the two US indices: no edge on either side.** At mid prices, with no costs at all, trading with the break made -1.36 USD over 2516 trades and trading against it -12.53 over 3045. The break of the 24-hour range says nothing about the rest of the day on these three markets, and the spread and slippage (67 to 74 USD in the base view) are then the whole result. 24 and 25 of the 31 windows lost.
- **Bitcoin and Ether: the break is followed, but not by enough.** With the break made +54.19 USD at mid prices and +15.13 after the quoted spread and slippage (profit factor 1.07, 42% winners, 3 of 7 windows lost); with those costs doubled and 5% of entries missed it lost 27.55 and 6 of 7 windows. The shorts earned +28.24 and the longs lost 13.11, in nineteen months in which both coins fell. Against the break lost in every window. T-0023 passes two of the three checks and fails the stressed one, so it is not traded.
- G2 is not passed by either study.

Reports: `research/reports/cfd_hourly_channel_v1_report.md` and `research/reports/cfd_crypto_hourly_channel_v1_report.md`. Budget 23 of 24; the free slot is T-0020, reserved for the stocks momentum plan.

## ADR-0056 - The practice sleeves trade by themselves: a rule-based demo trader, forward tests, a place per setup

**Status.** Decided by the owner on 2026-11-11 (plan approved that evening), written before the code. What is built is noted at the end as it lands.

**Context.** On 2026-11-11 the owner again found the crypto sleeve and the gold and CFD sleeve idle and asked that they trade every day, by themselves, beside the stocks, with more places so that several rules trade side by side. Three things kept them idle. No program places their orders: a Claude chat journals a PLAN and places each order by hand through the Capital.com connector (ADR-0044), so without a chat nothing trades, including the weekly crypto trade due on a Monday at 00:00 UTC. The crypto sleeve has one setup, which decides once a week (T-0019). The CFD sleeve has none (ADR-0052), and the four day-trading trials of ADR-0055 failed the promotion rule. That rule did what it was written to do; its outcome is two sleeves that do nothing for days, which is not what the owner wants from demo money.

What the trials say about the candidates, read before this decision: T-0023 (crypto, with the break of the 24-hour channel) made +15.13 USD in the base view, -27.55 in the stressed view and +15.05 in walk forward, about one trade a day, 42% winners, +0.026 USD a trade at the quoted costs and -0.059 with them doubled. Its base figure is a sum over seven windows that each restart at USD 100: the same rule run as one account from 2025-01-01 was at -2.30 USD after 130 trades when the 10% drawdown pause stopped it (`runs/research/cfd_crypto_hourly_channel_v1/results.json`, view `continuous`). T-0018 (index dip) made +4.40 base, -0.02 stressed and +1.22 in walk forward, about 16 trades a year. T-0021 and T-0022 (gold and two indices, with and against the break) lost at mid prices with no costs at all.

**Decision.**
- **A forward test is a second, labelled class of setup; the promotion rule is not rewritten.** A setup is `tested` when its registered trial passed ADR-0052's rule (base, stressed and walk forward net positive). A setup is a `forward_test` when the owner lets a registered trial's rule, unchanged, run on demo money although it failed one of the three checks, to measure whether the backtest holds. Forward tests from 2026-11-11: T-0023's rule on the crypto sleeve and T-0018's rule on the CFD sleeve. T-0021, T-0022 and T-0024 are not run: a rule with no result before costs has nothing to measure. Gold has no setup until a trial supports one. A forward test is practice, never research evidence, and never a step toward real money.
- **Loss cap, fixed now.** A forward test is switched off when its own closed result is below -5 USD. At 60 trades it is reviewed against its trial's per-trade averages. Sixty trades cannot tell the base view from the stressed one; the cap limits what finding out costs. The sleeve's floor (90% of its start) stays the hard stop for everything in the sleeve.
- **A rule-based trader places the orders.** `python -m trader.practice trade` runs beside the stocks bot in its own window. No language model, no chat and no connector is in its loop. Each completed hour it reads the broker's hourly bid and ask bars, feeds them to the rule classes and spec files the trials ran (`strategies/cfd_rules.py`, unchanged), sizes a signal with the trial's sizer and policy at the live quote, refuses it above the trial's spread limit or while the market is closed, passes the sleeve's rules in the practice journal, journals the PLAN, sends one market order with stop and target held at the broker, and journals the OPEN and later the CLOSE at the broker's level. It writes every decision, taken or not, to `runs/practice/decisions.jsonl` and a heartbeat to `runs/practice/trader.json`.
- **Keys and order calls.** The trader is the third reader of the `capital-demo` keys, after the market feed (ADR-0045) and research ingest (ADR-0052), and the first component under `src/` that sends an order. The demo host stays hard-coded, the live host's name may not appear under `src/`, every address is built from the demo host, and an order is sent once and never retried. The instrument file `configs/instruments/capital_demo_cfds_v1.yaml`, written for research, is read by the trader for tick and step sizes; it checks them against the broker's market details when it starts.
- **A place per setup.** A setup holds one open trade per group of epics that move together, counted for that setup alone, so the crypto sleeve's week-long trade does not block its hourly rule. Each sleeve allows four open trades (it was two). Entries a UTC day and the limit on total notional are counted per setup, as each trial ran. Trades planned by hand in a chat are counted together as before. The trader's PLAN carries its setup's key and the decision bar's close (`reference`), from which the rule measured its stop and target; the journal's limit on the target's distance is measured from that reference when it is given. The connector guard ignores the trader's plans, so a chat order cannot use one.
- **Hedging.** Two setups may hold opposite sides of one coin, which needs the demo account's hedging mode. The assistant may not change account preferences (ADR-0044's deny list). Until the owner switches it on, the trader opens no position on an epic that already has one.
- **US30 and RTY** join the demo allowlist and the CFD sleeve's epics, so that the index dip runs on the four indices of its trial.
- **Trial budget 24 to 26** (owner, 2026-11-11). T-0025: weekly momentum, the family that passed on crypto, on gold, silver, crude oil, EUR/USD and the indices, fixed in a record before it runs. T-0026: a trained filter that may only refuse trades of the hourly crypto rule. T-0020 stays reserved for the stocks momentum plan.

**What comes from the owner's review of open-source systems** (`AI_TRADING_REPOSITORY_RESEARCH.md`): separate tracks behind one set of risk rules (every setup passes the same journal checks); a forward record compared with the frozen backtest (`python -m trader.practice forward`); a trained numerical filter before any language model (T-0026); no language model in the order loop. None of the reviewed systems is used: none has a verified result.

**As built (2026-11-11).** The journal takes a lock for every append and counts places per setup (e88f7e0). The order client `practice/capital_orders.py` sends through the transport's `send_json`, which accepts only `POST /api/v1/positions` and `DELETE /api/v1/positions/<dealId>`, once; it was certified at 16:48 UTC by a 0.0001 BTCUSD round trip (bought 74,183.9, sold 74,133.9, -0.005 USD; c7bc6cb). The trader is `practice/trader.py` (068b42a): a daily rule acts at the first tradeable open after its decision bar and its hold runs from the entry hour, as the engine timed them (bd97c5d); the index dip's target is its sizer's cost gate and is not sent to the broker. `npm run trader` starts it; the console's mission panel shows it (61e3ff5); `python -m trader.practice forward` is the forward ledger (caaebbf). The trader went live at 16:54 UTC; its first decision, on the hour ending 17:00 UTC, found no break on either coin. US30 and RTY are in the allowlist and the index dip is on (cfd4b27). Not done: the trial budget raise and T-0025 and T-0026.

**Consequence.** The sleeves trade when a rule fires and nobody has to be awake. The honest expectation for the hourly crypto rule is a result near zero with losing stretches. The demo account now depends on one more long-running process: if it is stopped, stops and targets stay at the broker, but the hourly rule's 21:00 UTC close does not happen until it runs again, and an overdue trade blocks new plans in its sleeve. A setup's forward result will differ from its trial for known reasons: one continuous account instead of windows restarted at USD 100, fills a few seconds after the hour instead of at the bar's open, and demo quotes.

## ADR-0057 - An aggressive paper exploration book for stocks, kept apart from the practice sleeves

**Context.** On 2026-11-11 the owner asked for the stock bot to trade far more: "at least 15-20 trades" a day, more stocks, more positions, higher leverage, and a target of 10 to 20% a week. His argument: the money is paper, so risk costs nothing, and many trades give data to validate strategies faster. His real capital is 300 EUR. Against it stand the evidence (Candidate A lost before costs in every window of T-0006 to T-0010, and every frequent rule tested since lost after costs) and the fees: about USD 0.04 a round trip on the conservative schedule, 0.2% of a USD 20 position. The USD 100 stocks sleeve that day made two round trips, +0.04 USD gross, 0.08 in fees.

**Decision.**
- **A separate exploration book on paper.** `configs/paper_explore.yaml`: portfolio `pf-explore`, account id `acct-alpaca-paper-explore`, database `trader_explore` (`TRADER_EXPLORE_DATABASE_URL`, derived from `TRADER_DATABASE_URL` when absent), Candidate A in practice mode. Its results are reported every day and never mixed with the three USD 100 practice sleeves, which stay the record that decides where real money goes.
- **The owner's numbers, within what the code can do safely.** `configs/policy/paper_explore_v1.yaml`: USD 300, 2% of equity at the stop (USD 6), 10% planned risk in total, the whole capital may be invested with at most 25% (USD 75) in one stock, five open positions, 20 entries a session, entries refused above 20 bps of quoted spread, and loss limits of 5% a day, 15% a week, 30% from the high, a floor at USD 210. At USD 75 a position, the fees are about 0.05% instead of 0.2%.
- **Universe of 20 stocks** instead of 5 (`configs/instruments/stocks_explore.yaml`, Alpaca asset metadata read the same day): AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA, AMD, AVGO, NFLX, JPM, V, MA, COST, WMT, XOM, LLY, UNH, ORCL, CRM.
- **No leverage and no shorting.** The v1 policy refuses both because the code has neither: buying power is read as non-marginable cash, and the stock execution path is long only. Building them is a project of its own and multiplies the result of a rule with no tested edge.
- **One account, one book at a time.** Both books trade the one Alpaca paper account, and each holds its account lock in its own database, so they cannot see each other. The launcher (`runs/paper/sessions/explore-run.ps1`, untracked) waits until no practice-sleeve bot runs before it starts. From 2026-11-12 the account belongs to the exploration book; the USD 100 stocks sleeve stops after the session of 2026-11-11 and waits for the weekly momentum rule, which will need its own paper account (Alpaca allows several).

**Consequence.** About five times the trades of the practice sleeve, which answers within weeks rather than months whether Candidate A earns before costs on 20 stocks with real paper fills. Expected answer, from every test so far: it does not, and the book loses mostly its fees. The fee estimate is the per-order upper bound; the broker's own charge is about two cents a day (ADR-0054). The console does not show this book yet (it reads the practice database); `trader paper day --config configs/paper_explore.yaml` reports a session. A target of 10 to 20% a week is not adopted: no rule in this project or in the owner's research file has shown it, and a book sized to chase it would breach its own loss limits first.

## ADR-0058 - Weekly momentum on the CFD sleeve's markets: trial T-0025, fixed before it runs

**Status.** Written on 2026-11-11 before T-0025 was registered or run. The result is added below once it exists.

**Context.** The gold and CFD sleeve has one setup, the index dip, and it runs as a forward test (ADR-0056). Gold, silver, crude oil and EUR/USD have no rule: the only one tested on them, the hourly channel (T-0021, T-0022), had no edge on gold before costs. The one rule in the project that passed ADR-0052's practice rule is weekly momentum on the crypto CFDs (T-0019). The owner approved raising the trial budget from 24 to 26 (ADR-0056): one slot for this trial and one for a trained filter on the hourly crypto rule.

**Decision.**
- **Budget 26.** Recorded with `trader research decide-budget`. T-0020 stays reserved for the stocks momentum plan; T-0026 is reserved for the filter.
- **T-0025, fixed here.** Study `configs/research/cfd_weekly_momentum_v1.yaml`, rule `src/trader/strategies/specs/cfd_weekly_momentum_v1.yaml` (family `CFD_WEEKLY_MOMENTUM`, unchanged code). T-0019's rule carried to markets closed at the weekend: on Friday's daily session, long when the close is above the close five sessions earlier, short when below; stop 3 x ATR(10); target twice the risk; out one week after the entry. Only the two calendar numbers differ from T-0019 (five sessions instead of seven days; Friday instead of Sunday). Universe: every USD-quoted epic of the CFD sleeve, GOLD, SILVER, OIL_CRUDE, EURUSD, US500, US100, US30 and RTY; DE40 is left out (euro conversion unread). Sleeve economics as in ADR-0052: USD 100 per three-month window, 2% at the stop, at most two open trades and one index trade at a time, the larger weekly move first. Costs: the broker's quoted spread with a floor at its 2026 median (gold 1.1 bps, silver 8.5, oil 4.4, EUR/USD 0.6, the indices 0.4 to 1.8), 1 bp of slippage a side, overnight financing; the stressed view as before. Trading 2019-01-01 to 2026-08-01, the last three months locked. Walk forward 12/3/3 months.
- **What the result does.** ADR-0052's rule, unchanged: the setup joins the CFD sleeve's practice trader as `tested` only if it is net positive in the base view, the stressed view and walk forward. Otherwise gold, silver, oil and EUR/USD stay without a setup.

**Consequence.** A pass would give the gold and CFD sleeve a weekly trade on its main markets. Eight markets that partly move together (the four indices) raise the chance of a lucky pass less than eight separate trials would, but more than one market; the stressed view and walk forward are the guard.

**Result (run on 2026-11-11, after the text above was committed as 6f1ac96).** T-0025 is not traded; gold, silver, crude oil and EUR/USD stay without a setup.

| View | Trades | Net | Gross | Spread and slippage | Financing |
| --- | --- | --- | --- | --- | --- |
| Base | 693 | -14.43 USD | -2.06 | 14.47 | 13.61 |
| Stressed | 664 | -41.55 | -18.07 | 21.32 | 24.19 |
| At mid prices, no financing | 699 | +21.48 | +20.39 | 0.16 | 0 |
| Walk forward | 293 | -7.34 | | | |

Sums over 31 three-month windows of USD 100; 18 of them lost in the base view. One continuous account from 2019 was at -7.58 USD after 92 trades when its drawdown pause stopped it. Longs made +10.99 USD net (405 trades, 52% winners) and shorts lost 26.65 (288 trades, 44%). 607 of 693 trades ended at the one-week time exit, 35 at the stop and one at the target; 2,292 signals were not taken for the risk limits (two open trades, one index at a time, the smallest contract). Buy and hold of the same markets made +93.81 USD over the same sessions.

Read plainly: the sign of the week says a little about the next week on these markets, enough to earn at mid prices, but less than a weekly trade pays in spread and in overnight financing on a position held seven nights. The CFD sleeve keeps one setup, the index dip as a forward test. Budget 24 of 26; T-0026 is reserved for the filter on the hourly crypto rule. Report: `research/reports/cfd_weekly_momentum_v1_report.md`.

## ADR-0059 - A trained veto on the hourly crypto breakout: trial T-0026, fixed before it runs

**Status.** Written on 2026-11-11 on branch `cfd/filter-v1` (merged into `sunshine` as b851c44), before T-0026 was registered or run and before any feature, label or model was computed on the broker's quotes. The result is added below once it exists.

**Context.** T-0023, trading with the break of the last 24 hourly bars on the Bitcoin and Ether CFDs (ADR-0055), is the one intraday rule with a result before costs: +54.19 USD at mid prices over 584 trades in nineteen months, +15.13 after the quoted spread and slippage, -27.55 with those costs doubled and 5% of entries missed, +15.05 in walk forward; 42% winners, profit factor 1.07, about +0.026 USD a trade at the quoted costs and -0.059 stressed. It failed ADR-0052's stressed check and runs unfiltered as a forward test on the demo crypto sleeve (ADR-0056). Its edge before costs is a few hundredths of a dollar a trade; refusing the trades that are least likely to clear the spread could move the stressed view above zero, or it could only trade less. ADR-0052 allowed a trained filter only on a rule with a few hundred candidates; ADR-0056 raised the budget to 26 and reserved T-0026 for it. The stock filter pipeline (`research/training.py`, `research/model_study.py`, ADR-0029, ADR-0030, ADR-0034) runs on the stock engine only; the CFD engine had no hook for a veto.

**Decision.**
- **What was built (branch `cfd/filter-v1`).** A veto hook in the CFD engine (`research/cfd_engine.py`): an optional signal filter asked once per in-window signal before the pause, the limits and sizing; it can only accept or refuse, and without it every output is as before (a fixture run reproduces the engine's result hash of 24af8ab; the CFD tests pass unchanged). The features `cfd-breakout-v1` (`features/cfd_breakout.py`). The filters, controls and labels (`research/cfd_filter.py`). A study kind `cfd_filter_study`, run with `trader research cfd-filter` (`research/cfd_filter_study.py`, `research/cfd_filter_report.py`). No registered file changes.
- **T-0026, fixed here.** Study `configs/research/cfd_crypto_breakout_filter_v1.yaml` on base study `cfd_crypto_hourly_channel_v1`, trial T-0023, whose registration must still match the registry when T-0026 runs. Everything the base trial binds is unchanged: hourly bid and ask of BTCUSD and ETHUSD, development 2025-01-01 to 2026-08-01, the last three months locked, USD 100 per window, 1% of equity at the stop, one crypto trade at a time, four entries a UTC day, 1 bp of slippage a side, spread floors 6.8 and 8.1 bps, entries refused above 60 bps, flat by 21:00 UTC, the stressed view with spread and slippage doubled, financing charges times 1.5, no credits and 5% of entries missed.
- **Features**, nine, from bars that ended at or before the decision only: the break beyond the 24-bar channel in mean true ranges of the last 24 bars (the rule's own priority); the 24- and 72-bar log returns of the mid close, signed by the trade's side; the ratio of the standard deviations of the last 24 and the last 168 hourly log returns; the quoted spread at the decision bar in bps (the wider of its open and close quotes, raw); the side (+1, -1); the UTC hour as sine and cosine; the hours to the flat time. A signal with less than 168 earlier bars has no features and no label.
- **Labels.** An unfiltered base-view run emits every signal with its features. A signal the run took is labelled by its realised trade; any other (another crypto trade open, the pause, the entry limit) by the same signal traded alone from USD 100 with the same costs. Win = net USD after spread, slippage and financing above zero. A trade not closed when the outcome bars end has no label.
- **Folds.** Rolling: six months of training, three of validation, three of test, step three. Three folds; test windows 2025-10 to 2025-12, 2026-01 to 2026-03 and 2026-04 to 2026-06, which are three of the base study's own windows (July 2026 is not tested). Embargo 1,260 minutes, the rule's longest hold (decision on the hour ending 00:00 UTC, flat at 21:00): a training label decided or closed within 21 hours of the training end is dropped, and validation decisions end 21 hours before the test window, with outcome bars up to its start. No bar of a test window is read by training or by the threshold choice.
- **Model.** Primary: standardised L2 logistic regression, grid C 0.01, 0.1, 1 with class weights none and balanced (six points), chosen by validation log-loss. Challenger: LightGBM, num_leaves 3 or 7 with min_child_samples 20 or 40 (n_estimators 50 or 100 as paired in the file), learning rate 0.05, deterministic, one thread; it replaces the logistic model in a fold only if its validation objective is strictly higher. Platt calibration on validation scores. Threshold from 0.30 to 0.54 in steps of 0.03, chosen on validation by the net USD per session of a base-view replay of the validation window with the filter, among thresholds that accept at least 30 validation signals; ties to the lower threshold. Seed 20261126. A fold that cannot be trained refuses every signal in its test window (cash). Each fold's model is sealed as a checksummed `ModelBundle`.
- **Controls, in the same test windows and the same three views.** The rule alone. Random refusal at the filter's own refusal rate in that window, 20 seeds (hash draws salted by study, trial, fold and seed). Equal turnover: refuse as many signals as the filter refused, the smallest break beyond the channel first, ties by signal id.
- **The filter works** only if its net USD beats the rule alone, the mean of the 20 random seeds and the equal-turnover control, in the base view and in the stressed view: six comparisons, all required. The report also gives the paired bootstrap of the filter minus each control and the share of random seeds at least as good.
- **Practice rule.** ADR-0052's, unchanged, applied to the filtered rule: net positive in the base view, in the stressed view, and in the study's walk forward, which deploys the filtered rule in a fold when its validation objective is positive and holds cash otherwise. G2 (with the filter's positive lower bound over each control) is reported, not required for practice.
- **Budget.** T-0026 is the 25th registration of 26; T-0020 stays reserved for the stocks momentum plan.

**What the result does.**
- **The filter works and the filtered rule passes the practice rule:** the last fold's bundle (trained on 2025-07 to 2025-12, validated on 2026-01 to 2026-03), frozen, becomes the candidate filter for T-0023's crypto setup, which moves from `forward_test` to `tested`. Wiring a bundle into the practice trader is a separate change with its own record; until it lands the setup stays as it is. Retraining on later data is a challenger and a new trial.
- **The filter works but the filtered rule fails the practice rule** (for example the stressed view stays negative): the features carry information but not enough to pay the costs. Nothing changes in the sleeves; T-0023 continues as an unfiltered forward test under its -5 USD cap.
- **The filter does not beat all three controls** (including folds that could not be trained): a documented rejection. Refusing trades by a model is then not distinguishable from refusing them at random or by the size of the break. Nothing changes; no further filter trial on this rule without a new budget decision.

**Known before registration (disclosures).** T-0023's results above, on the same quotes and period, including the rule-alone control's result on the three test windows. T-0023's forward test on demo since 2026-11-11. The features, grids, threshold grid and controls were chosen after reading those results but before any feature was computed, any label built or any model fitted on these quotes; the pipeline ran on synthetic random walks only. Nineteen months of two coins that move together are one market regime; the three test windows hold roughly half of T-0023's trades, so G2's 100 out-of-sample trades are reachable only if the filter keeps most of them. Selecting among 6 logistic and 4 LightGBM grid points, 9 thresholds and two model families per fold is selection on validation; the test windows are the evidence, and the reality check across the four variants and the deflated Sharpe ratio with all registered trials correct for what is chosen there.

**Consequence.** A trained model can now be tested as a veto on any registered hourly CFD rule without touching a registered result. A pass would be the first evidence in this project that a filter selects a profitable subset after costs, and still backtest evidence only. The likely result, given a before-cost edge of a few hundredths of a dollar a trade, is that no filter beats random refusal by a margin the stressed costs leave standing.

**Run.** On `sunshine`, after merging, in the environment that will run it (the registration binds the Python, numpy, scikit-learn and LightGBM versions):

```powershell
uv run trader research register --study configs/research/cfd_crypto_breakout_filter_v1.yaml
uv run trader research cfd-filter --study configs/research/cfd_crypto_breakout_filter_v1.yaml
```

The run writes `runs/research/cfd_crypto_breakout_filter_v1/results.json`, the bundles beside it, `research/reports/cfd_crypto_breakout_filter_v1_report.md` and one RESULT event. After registration, pin `configs/research/cfd_crypto_breakout_filter_v1.yaml` in `tests/test_registered_inputs.py`.

**Result (run on 2026-11-11, after the text above was committed as 5fd49ca).** The filter does not work: it lost to every control in both views, a documented rejection. Nothing changes in the sleeves; T-0023 continues unfiltered as a forward test under its -5 USD cap, and no further filter trial on this rule is run without a new budget decision.

| Net USD over the three test windows | Base | Stressed | Mid, no financing |
| --- | --- | --- | --- |
| Rule + filter (212 trades base) | +4.96 | -10.94 | +18.95 |
| Rule alone (274) | +15.05 | -7.94 | +33.35 |
| Random refusal, mean of 20 seeds | +9.49 | -6.18 | +24.49 |
| Equal turnover, smallest break refused | +9.92 | -6.92 | +24.37 |

All three folds trained. 17 of the 20 random seeds did at least as well as the filter in the base view (p 0.857). The paired bootstrap's lower bounds for the filter minus each control are negative. G2 not passed. Read plainly: the nine features did not tell the breakouts that pay from those that do not; the model refused good trades about as often as bad ones, and slightly more. Budget 25 of 26; the free slot is T-0020, reserved for the stocks momentum plan. Report: `research/reports/cfd_crypto_breakout_filter_v1_report.md`.

## ADR-0060 - Practice setups: the crypto and CFD sleeves trade actively, like the stocks bot's practice mode

**Context.** On 2026-11-11 evening, with the practice trader running (ADR-0056) and no signal from its three setups, the owner asked why crypto and the gold and CFD sleeve were not trading "right now", wanted them "trading just like the stocks, actively", and named more commodities: magnets, silver, gold, copper, rare earths. The stocks bot trades every day because the owner chose practice mode (ADR-0040): it runs Candidate A, which lost in every backtest, labelled practice and never evidence. The crypto and CFD sleeves had no such permission: a setup ran only with a registered trial behind it (ADR-0052, ADR-0056), and every intraday trial on them failed (ADR-0055, T-0025, T-0026). What the demo account offers, read the same evening: copper (2.7 bps quoted spread), Brent oil (4.3), heating oil (4.8), natural gas (14), silver (9.6), gold and the US indices (about 1); platinum (39), palladium (70), nickel (40), aluminium (34), lead and zinc far wider. Rare earths and magnet metals have no commodity CFD there, only shares (MP Materials, a lithium ETF).

**Decision.**
- **A third status, `practice`.** A practice setup runs a rule without a passing trial on the demo account, as `--practice` does for stocks: it keeps a sleeve trading and records what happens; it is never research evidence and never a step toward real money. It names its own rule, instrument, policy and fee files, epics and calendar (`trader_setups` in `configs/practice/setups.yaml`); a tested or forward-test setup still names its trial. Every practice setup has a loss cap and stops taking entries below it.
- **Two practice setups, from 2026-11-11.**
  - `practice_commodity_breakout_v1` on the CFD sleeve (split into three families by ADR-0063): the hourly channel break of T-0021, unchanged in channel, stop (2 x the mean true range), target (2R) and day-trade exit, decided on hours ending 01:00 to 20:00 UTC and flat at 21:00 UTC (`practice_cfd_hourly_breakout_v1.yaml`), on GOLD, SILVER, COPPER, OIL_CRUDE, OIL_BRENT, HEATINGOIL, NATURALGAS, EURUSD, US500 and US100. Sizing policy `cfd_sleeve_v1.yaml` (2% at the stop, two open trades, entries refused above 20 bps).
  - `practice_crypto_breakout_v1` on the crypto sleeve: T-0023's rule on Bitcoin and Ether with decisions on hours ending 00:00 to 20:00 UTC, flat at 21:00 UTC (`practice_crypto_hourly_breakout_v1.yaml`). Policy `crypto_cfd_sleeve_v1.yaml` (1%, 60 bps).
  - Loss cap -5 USD each.
- **More markets.** `configs/instruments/capital_demo_cfds_v2.yaml` is version 1 (pinned) plus COPPER, OIL_BRENT, HEATINGOIL and NATURALGAS, read from the demo API. They join the CFD sleeve's epics (Brent and heating oil with crude in one `oil` group) and the demo allowlist.
- **What is not traded.** Platinum, palladium, nickel, aluminium, lead and zinc: their spread is above the entry limit, so a short trade would start several tenths of a percent down. Rare earths and magnets: no commodity CFD exists; share CFDs are a different product and sleeve.
- **Correction, same day.** Zinc (16 bps) is inside the 20 bps entry limit; only platinum (39), palladium (70), nickel (40), aluminium (34) and lead (39) are over it. Zinc, cocoa, the two coffees and wheat are inside the limit but their markets close between 17:30 and 19:20 UTC, before the rules' 21:00 UTC flat time, so a day trade could not be closed on time; they stay out until a rule with earlier hours exists (docs/strategy_playbook.md section 6).

**Expectation, stated before any trade.** T-0021, the same rule on gold and two indices over 2019-2026, made -1.36 USD at mid prices over 2,516 trades and -80.50 after the quoted spread and slippage: about -2.60 USD per three months on a USD 100 sleeve. T-0023 on the crypto CFDs earned before costs but lost under doubled costs. So the practice setups are expected to lose slowly to the spread, with winning and losing days; they trade, they do not earn. The loss caps end them after about -5 USD each; the sleeve floors (90%) stay the hard stop.

**Consequence.** The sleeves trade most hours of most days. The console labels these setups "practice". The forward ledger shows their results beside nothing, as they have no trial.

## ADR-0061 - The entry spread is the market's, not the IEX book's

**Context.** On 2026-11-11 the stocks practice bot refused 10 of 19 signals as SPREAD_TOO_WIDE (10 bps limit), with spreads of 10 to 34 bps on AAPL and GOOGL. The bot reads Alpaca's free IEX feed, one exchange's book. At 16:25:06 UTC IEX showed GOOGL at 292.00 / 292.98 (33.5 bps) while the consolidated (SIP) book was 292.25 / 292.30 (1.7 bps). The paper broker fills at the consolidated price: the 15:45 GOOGL buy filled at 291.516 against a SIP ask of 291.52, and the 17:54 sell at 293.888 against a SIP bid of 293.88 while the IEX bid was as low as 292.94. At 18:30 UTC the delayed SIP median spread of ten large stocks was 0.6 to 5 bps while IEX showed up to 291 bps (COST) and 160 bps (META). The free plan does not serve real-time SIP quotes (HTTP 403 "subscription does not permit querying recent SIP data"); quotes at least 15 minutes old it does serve.

**Decision.**
- **A full-market spread estimate.** At each bar boundary the paper runtime asks for the last 200 consolidated quotes of each symbol that are 16 to 31 minutes old and takes their median spread (`DelayedSipSpreads`, refreshed at most every 5 minutes per symbol; fewer than 20 two-sided quotes or a failed request give no estimate).
- **The entry check and the spread cost use the smaller of the estimate and the IEX spread.** A one-venue book is never tighter than the consolidated one, so the smaller of the two is the better estimate. Without an estimate the IEX spread is used as before, so a missing estimate fails closed.
- **The IEX book still bounds the order.** The entry limit stays the IEX ask plus the frozen offset, and sizing still uses that limit, so the price paid is bounded by the venue book whatever the estimate says. An IEX spread above 500 bps is refused even with a tight estimate: the estimate is 16 minutes old and a broken venue book may be a real disruption.
- **The risk limits are unchanged.** The practice policy keeps 10 bps and the exploration policy 20 bps; only the measurement changes.

**Consequence.** Signals on liquid stocks are no longer refused for a spread the broker does not charge, and the spread cost in the cost gate is the market's (about 1-5 bps) instead of IEX's. The quote-age check (2 s) still reads the IEX book, which can sit unchanged for seconds on a quiet venue; two signals were refused that way on 2026-11-11. That is left for a separate decision.

## ADR-0062 - The strategy playbook and the league: forward results decide what stays, by rule

**Context.** On 2026-11-11 the owner said that finding a strategy that makes money is Claude's job and asked that it be noted, written down and reused. The state of the evidence that day: 26 registered trials, one rule that passes ADR-0052's practice rule (weekly momentum on Bitcoin and Ether, T-0019: +19.51 USD summed over 34 quarterly windows of USD 100, about 2% a year, +1.23 with costs doubled), buy and hold ahead of every rule in every rising market, no edge before costs for any intraday rule on stocks, gold or the indices, and a rule that did not carry from crypto to the other markets (T-0025). The knowledge sat in ADRs, reports and a chat's memory; no document said what is traded, why, at what cost, or how a rule enters the book and leaves it.

**Decision.**
- **`docs/strategy_playbook.md` is the written record.** It holds the table of every trial, what the numbers say, the setups in the book, what each market costs, the ranked next hypotheses, and the steps for adding a market to a rule or a rule to the book. It is updated when a trial finishes or a forward test is reviewed. The decision records stay the source for why; the playbook is the source for what and how much.
- **The league is by rule.** `python -m trader.practice forward` puts every setup in one state from its own closed demo trades: `OFF`, `STOPPED` (below its loss cap), `COLLECTING` (fewer than 60 closed trades, where a result says nothing), `CANDIDATE` (at 60 with a positive net and a positive average a trade) or `DROP` (at 60 with a net at or below zero). The count is 60 unless the setup states another.
- **A forward result never promotes a setup by itself.** `tested` remains a registered trial's verdict (ADR-0052). A `CANDIDATE` earns a registered trial, which needs a budget slot; a `DROP` is removed from the setups; a `STOPPED` setup goes to the owner. Nothing in the league moves toward real money (ADR-0056).
- **No budget change and no new trial here.** 25 of 26 are used and the last, T-0020, is the stocks momentum plan.

**What the evidence says about the next step.** The strongest untested prior is weekly cross-sectional momentum on liquid US stocks (T-0020): decades of published evidence and costs of cents. The rules the owner's mandate allows on crypto and CFDs (held a week at most, ADR-0049) have each earned either nothing before costs or less than the financing and spread they pay; the only slow rules with a gross edge (T-0013 to T-0019) earn little against holding the asset. Longer holds on spot or ETFs would change that picture and are the owner's call.

**Consequence.** The sleeves keep trading practice rules; the ledger says after about 60 trades each which of them deserves a trial and which to drop. Expect small losses to the spread in the meantime (ADR-0060). A reader who wants to know whether anything here makes money finds the answer, with its numbers and its limits, in one file.

## ADR-0063 - Slots by market family: the CFD practice break runs as three setups, and a fill is never left untracked

**Context.** The practice trader's first real signals went through the order path at 19:00 UTC on 2026-11-11. It opened a short of 0.02 gold at 3,986.32 (USD 80 of notional on a USD 100 sleeve, stop 4,011.68, target 3,938.42, held at the broker; broker and journal agree), and refused the same hour's US 500 and natural gas signals with `RISK_LIMIT (gross_notional)`: the policy holds one setup's notional to the sleeve's equity, and one contract of these markets is USD 40 to 85, so a setup holds about one position. The owner had asked twice for more slots so that several rules trade side by side. The policy type itself (`RiskPolicy`, shared with the stock bot and the risk engine) accepts fractions only within 0 to 1, so a cap above 1.0 equity is not a configuration change and is not made here.

**Decision.**
- **Slots are setups.** Limits are counted per setup (ADR-0056), so concurrency comes from more setups, not from a looser cap. The practice break of the 24-hour range (one rule file, `practice_cfd_hourly_breakout_v1.yaml`) now runs as three setups by market family: `practice_commodity_breakout_v1` for metals (gold, silver, copper; the key is kept because its first trade was open when the split was made), `practice_energy_breakout_v1` (crude oil, Brent, heating oil, natural gas) and `practice_macro_breakout_v1` (EUR/USD, US 500, US Tech 100, US 30). Each has its own ledger and its own -5 USD loss cap, so one family that bleeds does not stop the others, and the forward ledger shows which family pays. With the index dip the CFD sleeve can hold its four trades at once.
- **A fill is never left untracked.** The trader now checks the sleeve's limits before it sends an order (`SLEEVE_FULL` when the sleeve already holds its four open trades; the journal's daily entry count too). If the journal still refuses an OPEN after the broker filled the order, the position is closed at once and a CANCEL records why; if that close fails, entries pause and the console says so. Before this, a full sleeve would have sent the order and only then found the journal unable to record it.

**Consequence.** The sleeve may hold up to four positions of USD 40 to 85, three or four times its equity in notional, with each trade's risk still 2% of equity at a stop held at the broker and the sleeve's 90% floor as the hard stop. That is the per-setup design of ADR-0056 used as intended. More trades also mean more spread paid: T-0021 paid about 3 cents a trade on gold and the indices, so the expected drift of ADR-0060, slow losses to costs, now arrives faster; the three -5 USD caps and the floor bound it.

## ADR-0064 - Paper books have their own rules; the real-money rules are unchanged

**Context.** On 2026-11-11 the owner said the guidelines, guardrails and limits "are no longer applying to our cause", naming the loss limits, the rules and the decisions. The cause since ADR-0057: paper money only, the USD 300 exploration book, many trades a day, learning from real paper fills. The written rules had three layers that were never told apart: the protected specifications, written for a cautious path to real money (USD 50, two entries a session, a 1% daily loss limit, a recorded owner review after every breach, no trade without a validated model); the paper policies that later decisions loosened one number at a time (ADR-0040, 0043, 0057, 0060); and stale statements in the runbook, the README, the Desk and the assistant's notes that contradict those decisions ("2.5% a week is the hard stop", "from 16 Nov the stocks bot only watches", a trial budget of 15 or 20). What actually refused the 27 signals of 2026-11-11: the venue's spread 13 times (fixed by ADR-0061), a quote older than 2 seconds 3 times, a pause after a restart twice, and cost, the entry count and size once each. No loss limit has ever refused a trade. A dry run the same night also found that reading the market spread for 20 stocks one after another took 14 seconds, between the quote poll and the decision.

**Decision.**
- **Real money: nothing changes.** The nine protected specifications, `configs/policy/base.yaml`, the gates G0 to G5, the live guard and the denied broker tools stay as they are. Live trading stays impossible. A rule reaches real money only through a registered trial that passes and the gates after it.
- **Every book, paper or not.** Only paper and demo keys; one bot per broker account; exits are never blocked by a pause; unknown orders block entries; orders are never resubmitted blind; the trial registry is append-only and its budget is the owner's to raise; a practice or exploration result is never research or forward-paper evidence (ADR-0040).
- **Paper books set their own numbers.** Each book's limits live in its own `paper_*` policy file and are changed by a decision record, between sessions and never during one. A rule may trade in a paper book without a passing trial; the league (ADR-0062) and the day reports say what it earned. "Cash and no trade" stays a legitimate outcome: there is no quota, the entry count is a ceiling.
- **Loss limits on a paper book are fault catchers.** They are set far above a normal day's loss, so that only a malfunction or a very bad run reaches them. For the exploration book, `configs/policy/paper_explore_v2.yaml`: 10% a day (USD 30), 25% a week, 50% from the high, a floor at USD 150 (5%, 15%, 30% and USD 210 before). A normal day of Candidate A on this book loses USD 1 to 3. The policy loader still refuses automatic resume, and a breach still sells the book's positions.
- **Who reviews a breach on a paper book.** The assistant: it reads the cause (the trades, the fills against the plan, the ledger against the broker), records the review with `trader resume --ack-loss-breach --note`, resumes when the cause is not a malfunction, and tells the owner the same day. A malfunction is fixed first. On real money the review stays the owner's (runbook section 7).
- **Losing days are reported, not a stop.** Three losing sessions in a row is a line in the day report with the cause. The old statements that a 2.5% weekly loss is the hard stop for every book, and that the stocks bot only watches from 16 Nov, are withdrawn: the exploration book trades every session under its own policy.
- **The other numbers of the exploration book.** A quote may be 15 seconds old (2 before): the free IEX book often stands still for longer while the market trades. 30 entries a session (20 before). Unchanged: USD 300, 2% at the stop, USD 75 a stock, five positions, 20 bps of market spread, a 250 ms clock limit, one entry order waiting at a time (ADR-0010), long only, no borrowing.
- **The spread estimates are read side by side and before the quotes.** `DelayedSipSpreads` asks for up to eight symbols at once (4 seconds for 20 stocks instead of 14), and the runner reads them before the latest quotes, so the decision still sees the freshest quote.
- **The stocks book is one setting.** `TRADER_PAPER_CONFIG` names the paper configuration that `npm run bot` runs and the console shows; the tracked default is `configs/paper_explore.yaml`. `npm run bot` refuses to start while any paper bot runs (both books trade one Alpaca paper account and cannot see each other's lock) and appends its output to `runs/paper/sessions/<alias>.log`. `npm run day` brings up the database, the console, the stocks bot and the practice trader, each in its own window and only when it is missing; it never stops anything.

**Not decided here.** Borrowing and short selling on the stock book: the policy type refuses both because the sizing, the funds check and the ledger have neither, and those are shared with the registered trials' pinned inputs. The limits of the crypto and CFD practice trader (ADR-0056, 0060, 0063) are unchanged; the owner changes them in that work.

**Consequence.** The exploration book runs unattended: nothing short of a fault waits for a person. Today's bars gave 154 Candidate A signals on the 20 stocks (28 on the practice sleeve's five), so the 30 entries will usually be used. The honest expectation is unchanged and is the reason the limits can be wide: no intraday rule tested here earns before costs (T-0001 to T-0010, T-0021, T-0022), so more entries mostly mean more spread paid; at USD 75 a position without borrowing, a session in which all 30 trades reached their targets would make about 2 to 3% (a target is 0.3 to 0.45% of the position), and a normal one loses a little. The book buys data about real paper fills on 20 stocks, not income. A reader who finds a stricter number in an older record or in the specifications reads it as the real-money rule.
