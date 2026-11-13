# Project guide: ai-stock-trader

This is the entry point for the codebase. It covers:

- what each specification file is for;
- how the specifications map to code;
- which milestones are done and what comes next;
- how to run everything.

Working on the project with others: [CONTRIBUTING.md](../CONTRIBUTING.md) (setup, keys, git workflow, the shared research log). Machine-learning and LLM work: [ml_research_guide.md](ml_research_guide.md). Building a monitoring dashboard: [observability.md](observability.md) (the read-only data contract).

The ten specification files (nine in [specs/](specs/), plus the brief in README.md) are **protected**. They are never edited. Their SHA-256 hashes are pinned in [protected_spec_hashes.sha256](protected_spec_hashes.sha256), checked by `tests/test_protected_specs.py`, and excluded from line-ending conversion in `.gitattributes`. The one exception is the orientation section at the top of [README.md](../README.md) (status and roadmap, ADR-0024); the original project brief below it stays byte-identical and is checked the same way.

> **Honest status:** nothing in this repository has demonstrated a profitable strategy. The software can decide, size, execute in replay, account and pause safely. The first registered research study (M2, BTC/ETH on real data) found **no edge**: all five trials lose money after costs and are not positive even before commissions, and gate G2 is not passed ([research/reports/baseline_report.md](../research/reports/baseline_report.md)). The stock baseline (five trials on IEX 5-minute bars, registered 2026-11-08) lost in every window, and by 2026-11-11 25 of 26 budgeted trials are run with none passing G2 ([strategy_playbook.md](strategy_playbook.md) has every result). The M4 Alpaca paper bot was certified on the real paper account on 2026-11-09; without a validated model it rejects every candidate `NO_EDGE`, and it trades only in the owner's practice mode on paper books, whose looser limits are set per book and are never evidence ([decisions.md](decisions.md) ADR-0040, ADR-0057, ADR-0064; [runbook.md](runbook.md) section 0). The USD 100,000 ambition is not an input to any code path.

## 1. The specification files

| File | What it is for | Authority |
| --- | --- | --- |
| [README.md](../README.md) | Project brief. The USD 50 start, why the USD 100k goal is not a target (it needs about 8.8% per day), and the definition of success. | Context |
| [instructions.md](specs/instructions.md) | The contract for the coding agent: priority order, stack, directory layout, mandatory behaviour, the ten ordered tasks and the first demonstrator. | **Primary** |
| [architecture.md](specs/architecture.md) | Runtime shape, authority boundaries, domain objects, broker interface and evidence levels, storage and accounting, decision cycle, order lifecycle and timeout recovery, exits, recovery and security. | **Primary** |
| [risk_and_validation.md](specs/risk_and_validation.md) | **The only source of numeric risk values** (the policy YAML). Also: the dollar table, exact sizing, circuit breakers, research protocol, realistic replay rules, statistics, gates G0-G5 and the adversarial tests. | **Numeric authority** |
| [strategy_and_profitability.md](specs/strategy_and_profitability.md) | Edge and cost maths, USD 50 economics, Candidate A (trend continuation), Candidate B (mean reversion), the model's role as a filter only, news/LLM limits, the ASX gold track, compounding. | Strategy |
| [markets_and_brokers.md](specs/markets_and_brokers.md) | Market choice, Alpaca (paper first), IBKR, the account-rule profiles after FINRA 26-10, ASX parcels, data entitlements (IEX vs SIP). | Broker facts (dated) |
| [implementation_roadmap.md](specs/implementation_roadmap.md) | Milestones M0-M7, required generated artifacts, the CLI contract, the first integration demonstration, and when owner decisions are needed. | Plan |
| [repositories_and_sources.md](specs/repositories_and_sources.md) | Upstream repositories and their pinned commits, the procedure for reusing code, primary sources. | Provenance |
| [CRYPTO_TRADING_TRACK.md](specs/CRYPTO_TRADING_TRACK.md) | Addendum: BTC/ETH spot via Kraken first. One shared USD 50 portfolio, 15m/1h cadence, UTC-day loss windows, maker/taker cost maths, venue safety. | Track addendum |
| [COMMODITIES_AND_CFD_TRACK.md](specs/COMMODITIES_AND_CFD_TRACK.md) | Addendum: gold then silver via an ETC or a CFD. Contract-unit sizing, margin is not exposure, financing, Capital.com demo, CFD failure handling. | Track addendum |

Precedence when documents interact:

- numeric limits always come from risk_and_validation.md §1;
- the track addenda add profiles but never loosen the stock rules;
- decisions where the documents are ambiguous are recorded in [decisions.md](decisions.md).

## 2. Where each part of the specification lives in code

| Concern | Spec | Code |
| --- | --- | --- |
| Policy, mode configs, host role | risk §1; instructions §6.1 | `configs/policy/base.yaml`, `configs/*.yaml`, `src/trader/config/` |
| Secrets: OS keyring, key profiles, keys inbox, `secrets exec`; the assistant's Alpaca MCP server without trading tools | architecture §9 | `config/secrets.py` (ADR-0026, ADR-0032), `.mcp.json`, `.claude/settings.json` |
| Track profiles (stocks/crypto/commodities) | CRYPTO §1, COMMODITIES §1 | `configs/tracks/`, `config/track_profiles.py` |
| Domain contracts (Decimal, UTC, schema versions) | architecture §3 | `src/trader/domain/` |
| Instrument specs for all three tracks | architecture §3; CRYPTO §4; COMMODITIES §5 | `domain/instruments.py`, `configs/instruments/` |
| Broker capabilities and evidence levels | architecture §4 | `domain/capabilities.py`, `configs/capabilities/` |
| Fees (exact granularity; unknown means no trade) | risk §3; CRYPTO §6 | `accounting/fees.py`, `configs/fees/` |
| Exact sizing (monotone search, round down) | risk §3; COMMODITIES §5 | `risk/sizing.py` |
| Risk engine, the only approver (fail closed) | instructions §5; architecture §2, §6 | `risk/engine.py`, `risk/approval.py` |
| Shared cross-track allocator and reservations | architecture §5; CRYPTO §3; COMMODITIES §4 | `risk/allocator.py`, `risk/portfolio.py` |
| Account-rule profiles | markets §4 | `risk/account_rules.py` |
| Loss windows, drawdown, USD 45 floor | risk §1; CRYPTO §3 | `risk/loss_limits.py` |
| Persisted pauses, owner review | risk §1, §4; architecture §9 | `ops/pause.py`, `ops/owner_actions.py` |
| Leadership: advisory lock plus fencing | architecture §5 | `ops/leadership.py` |
| Ledger, P&L, fees, settlement, equity | architecture §5; strategy §1, §9 | `accounting/` |
| Order state machine, outbox, gateway | architecture §7 | `execution/states.py`, `execution/oms.py`, `execution/_gateway.py` |
| Unknown-outcome resolution, reconciliation | architecture §7, §9 | `execution/reconciliation.py`, `ops/recovery.py` |
| Exits (stop/target/time/session/loss) | architecture §8 | `execution/exits.py` |
| Replay broker with fault injection | instructions §3; risk §6 | `brokers/replay/` ([replay_broker.md](replay_broker.md)) |
| Alpaca paper adapter (pinned `alpaca-py`, paper endpoint only, same contract cases as replay) | architecture §4, §7; markets §2 | `brokers/alpaca/` ([alpaca_paper_adapter.md](alpaca_paper_adapter.md), ADR-0025, ADR-0033) |
| Calendars and causal bars | architecture §6, §8 | `data/calendars.py`, `data/bars.py` |
| Historical datasets: Parquet store, checksums, quality checks, sources, manifest | instructions §3-§4; roadmap §2; CRYPTO §5 | `data/history.py`, `data/quality.py`, `data/sources/`, `data/ingest.py`, `data/manifest.py`, `research/data_manifest.json` |
| Corporate actions (point-in-time splits, symbol changes) | risk §6, §9 | `data/corporate_actions.py` |
| Deterministic test dataset (DST, early close, split, rename, gaps) | instructions §6.4 | `data/synthetic_history.py` |
| Streaming causal features (research and live identical by construction) | instructions §4-§5 | `features/streaming.py` |
| Candidate features for filter models (`candidate-v1`) | strategy §6 | `features/candidate_features.py` |
| Candidate A (frozen spec) | strategy §4 | `strategies/candidate_a.py`, `strategies/specs/candidate_a_v1.yaml` |
| Candidate B (mean reversion) and crypto Candidate A | strategy §4-§5; CRYPTO §5 | `strategies/research_rules.py`, `strategies/specs/candidate_*_v1.yaml` |
| Edge estimators (none / fixture; no bundle scorer yet) | strategy §6, §9 | `strategies/edge.py` |
| Research: bar-fill engine, experiment registry, walk-forward splits, statistics, studies, report | risk §5-§8; roadmap §2 | `research/` (package), `configs/research/`, `research/experiment_registry.jsonl`, `research/reports/` |
| Model research: labelled candidates, filter hook and controls, per-fold training, model study and report | strategy §6; risk §5, §7; roadmap §1-§2 | `research/labels.py`, `research/filters.py`, `research/training.py`, `research/model_study.py`, `research/model_report.py`, `configs/research/crypto_filter_v1.yaml` ([ml_research_guide.md](ml_research_guide.md), ADR-0029, ADR-0034) |
| Model bundles (checksummed JSON, scored with numpy alone) | architecture §2-§3 | `models/` (ADR-0027, ADR-0030) |
| Decision cycle shared by replay and paper | architecture §6 | `runtime/decision_loop.py` |
| Replay loop and demonstrator | instructions §7; roadmap §4 | `runtime/replay_runner.py`, `runtime/scenarios.py`, `runtime/demo.py` |
| Paper runtime: polled IEX bars, USD 50 subledger, reconciliation, graceful stop | instructions §6.8; architecture §9 | `runtime/paper.py`, `runtime/paper_data.py`, `runtime/broker_factory.py`, `configs/paper.yaml` (ADR-0031; [runbook.md](runbook.md) §9) |
| Guarded live entry point | roadmap §3; risk §8 | `runtime/live_guard.py`, `research/promotion_manifest.json` |
| Reports and feasibility tables | instructions §5; roadmap §2 | `reporting/` |
| Read-only data contract for a local dashboard | architecture §9 | [observability.md](observability.md) |
| Database schema and migrations | architecture §5 | `storage/`, `deploy/migrations/` |

Requirement-by-requirement traceability, including tests: [traceability.md](traceability.md).

## 3. Milestone status

Status, the remaining work for every milestone (M0-M7), the release gates (G0-G5), the parallel tracks and the next steps are kept in one place, so they cannot drift: the [roadmap](project_status.md#roadmap-planned-milestones-and-where-they-stand) and its [detailed checklist](project_status.md#detailed-checklist-what-was-planned-what-is-left) in [project_status.md](project_status.md).

## 4. Implementation steps taken

### M0 + M1 + first demonstrator

1. Bootstrapped the repository: `uv`, Python 3.12, locked dependencies, Ruff, mypy (strict), pytest and Hypothesis, PostgreSQL 16 through Docker (localhost only), and protection for the spec files.
2. Policy and configuration: the policy mirrors risk §1 exactly (a test parses the spec's YAML and compares). Modes are replay (default), shadow, paper and live. A host-role guard blocks wrong-mode hosts, and track profiles carry no risk numbers.
3. Domain contracts: Decimal-only money (floats and NaN rejected), UTC-only timestamps, canonical hashing, deterministic IDs, instrument specs for equities, crypto spot and commodity ETCs/CFDs.
4. Storage: 32 tables, including the uniqueness constraints and the single-pending-entry partial index. Alembic migration `0001` is checked against the code for drift. Exact decimal storage gives identical hashes on SQLite and PostgreSQL.
5. Accounting: a fill-driven ledger, realised net P&L per strategy §1, fee accrual and finalisation, settlement lots (US T+1 on settlement days), external flows kept separate, and conservative equity.
6. Risk: exact sizing (binary search shown equal to brute force by property tests), a fail-closed engine, a shared cross-track allocator with an atomic reservation and outbox transaction, loss windows (New York session and UTC, both enforced), and persisted pauses with owner review.
7. Execution: authority-gated gateway, unknown-outcome reconciliation (never a blind retry; non-acceptance needs certified lookups **and** a visibility window), idempotent event and fill posting, cancel races, reducing-only exits, startup recovery.
8. Replay: a deterministic replay broker with 11 fault types, synthetic sessions with checksums, Candidate A, the fixture edge (replay-only), and reports.
9. M0 documents: generated feasibility and capability tables, owner setup, runbook, decisions.

### M2 - research baseline (2026-11-07)

10. Historical data layer: exact-decimal Parquet files through DuckDB, file and content SHA-256 per instrument, integrity checks (unfinished bars, conflicting duplicates, impossible OHLC, misaligned rows, missing intervals against an explicit calendar expectation), and `trader ingest-history` driven by `research/data_manifest.json`. Datasets that cannot be acquired are recorded as BLOCKED with the owner action.
11. Sources: Coinbase Exchange and Kraken public endpoints (no account or key), Kraken pair metadata turned into instrument specs (`trader data kraken-specs`), Alpaca stock bars and corporate actions (fixture-tested; needs the owner's keys), and the Kraken OHLCVT archive importer (needs an owner download).
12. A deterministic synthetic history covering the DST change, a holiday and early close, a 2-for-1 split, a symbol change, a feed gap and a missing session. Point-in-time split adjustment never uses a future split; raw prices are kept for execution.
13. Streaming features with exact running sums, checked against batch recomputation; the research path reproduces the runtime replay's Candidate A candidates exactly.
14. Candidate B (mean reversion) for stocks and crypto, and a crypto version of Candidate A, each a frozen YAML specification.
15. A bar-based research engine with conservative fills (next open, limit respected, stop before target, participation caps, missed entries), the same sizer, fee schedules and USD 50 policy as the runtime, loss pauses, and split handling.
16. Research controls: a hash-chained experiment registry with the ten-trial budget, walk-forward splits with a locked final test, stationary block bootstrap, a reality-check bound, the deflated Sharpe ratio, effective sample size, power analysis and loss-trigger probabilities.
17. The registered crypto study and its report: five trials on 3 years of data, cash and buy-and-hold comparators, stressed and zero-commission diagnostics, and a G2 assessment (not passed).
18. Repository fix: an unanchored `data/` ignore rule had also excluded the source package `src/trader/data/` from the first commit, so a fresh clone could not import it. The rule is now anchored to the repository root and `tests/test_repository_hygiene.py` asks git that no source, config or document file is ignored.

### Stock baseline preparation, M3 tooling and the M4 paper runtime (2026-11-08)

19. Keys: the OS keyring with named profiles, a keys inbox outside the repository, and `trader secrets exec` for local tools; the assistant's Alpaca MCP server runs with its trading tools denied (ADR-0026, ADR-0032).
20. Stock baseline: a documented Alpaca cost basis (`configs/fees/alpaca_us_equities_v2.yaml`), a track-aware report, and `configs/research/stocks_baseline_v1.yaml` (T-0006..T-0010) registered after the owner raised the budget to 15 (ADR-0028).
21. M3 tooling: `candidate-v1` features, labelled candidates, the research filter hook with random and equal-turnover controls, per-fold training, numpy-only model bundles, and the model study with its report and CLI (`trader research labels`, `trader research models`). The first study, `configs/research/crypto_filter_v1.yaml` (T-0011, T-0012), has no results yet (ADR-0029, ADR-0030, ADR-0034).
22. M4 Alpaca paper adapter on the pinned SDK: definite versus unknown outcomes, one canonical execution ID, broker versions in BIGINT columns (migration `0002`), pacing and the trade stream; the same contract cases pass against replay and the adapter (ADR-0033).
23. M4 paper runtime: one decision cycle shared with replay (demo hashes unchanged), polled completed IEX bars, the USD 50 subledger, one leader per account, and `trader paper run`/`check` (ADR-0031).
24. [observability.md](observability.md): what a local dashboard may read, and the rule that it never writes.

## 5. Quickstart

```powershell
# one-time setup (see owner_setup.md for details)
python -m pip install --user uv
$env:UV_SYSTEM_CERTS = "1"        # only needed where HTTPS is intercepted by antivirus
python -m uv sync --frozen
copy .env.example .env            # then choose a local database password
docker compose up -d              # PostgreSQL 16 on 127.0.0.1:5433

# everyday commands
python -m uv run trader config-check --config configs/replay.yaml
python -m uv run trader demo                 # first demonstrator, SQLite
python -m uv run trader demo --postgres      # plus PostgreSQL reproducibility
python -m uv run trader replay --scenario loss
python -m uv run trader docs build --check

# research (M2)
python -m uv run trader ingest-history                 # verify or acquire every dataset in the manifest
python -m uv run trader research status                # registered trials, results, budget
python -m uv run trader research baseline              # rerun the registered crypto study and its report

# model studies (M3; registered trials only, on the branch that writes the research log)
python -m uv run trader research models --study configs/research/crypto_filter_v1.yaml

# keys and paper (M4; runbook.md §9)
python -m uv run trader secrets status                 # presence only, never values
python -m uv run trader paper check                    # every missing prerequisite, without connecting
python -m uv run trader paper run --config configs/paper.yaml --sessions 1

# checks
python -m uv run pytest                      # full suite (PostgreSQL must be running)
python -m uv run ruff check . ; python -m uv run mypy
```

Every run writes its outputs under `runs/`, which is gitignored. Reports carry the evidence class and the synthetic watermark.
