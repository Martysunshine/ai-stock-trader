# Dependency evaluation record

This record follows repositories_and_sources.md §2. Exact versions are locked in `uv.lock` (resolved 2026-11-07 with uv 0.13.7 for Python 3.12.16; broker SDK, keyring and the `ml` group added 2026-11-08). Every package below supports Python 3.12.

## Runtime dependencies (installed)

| Package | Version | License | Purpose | Notes / risk |
| --- | --- | --- | --- | --- |
| pydantic | 2.14.3 | MIT | Validation at external boundaries, frozen domain models | Floats are rejected by our own validators |
| PyYAML | 6.0.3 | MIT | Configuration files | `safe_load` only; unquoted decimals rejected |
| SQLAlchemy | 2.0.57 | MIT | Database access (Core) on PostgreSQL and SQLite | Custom `ExactDecimal` and `UtcDateTime` types |
| Alembic | 1.20.1 | MIT | Schema migrations | A drift test runs against PostgreSQL |
| psycopg[binary] | 3.3.8 | LGPL-3.0 | PostgreSQL driver | Used as an unmodified library, so LGPL obligations are met by dynamic use. Synchronous engine (Windows-friendly). |
| typer | 0.28.0 | MIT | CLI | - |
| exchange-calendars | 4.13.2 | Apache-2.0 | NYSE sessions, holidays and early closes | Pulls in pandas and numpy. The default horizon is about one year ahead, so keep it updated. |
| tzdata | 2026.6 | Apache-2.0 | IANA zones on Windows | Needed for `zoneinfo("America/New_York")` |
| pandas / numpy | 3.1.1 / 2.5.4 | BSD | Transitive (exchange-calendars); numpy is also a direct dependency for research statistics | Floats appear only in research statistics, never in the ledger or sizing |
| duckdb | 1.5.6 | MIT | Exact-decimal Parquet storage for historical bars (M2) | Embedded, no server; no pandas/pyarrow needed for read or write. Timestamps are stored as epoch milliseconds to avoid an optional `pytz` dependency |
| alpaca-py | 0.44.0 (exact pin) | Apache-2.0 | Official Alpaca SDK for the M4 **paper** adapter (instructions.md §1, ADR-0025) | Pinned to the release of 2026-08-11 (0.45.0 was 3 days old at review). Imported only under `brokers/alpaca/` (boundary test). Responses are used as raw dicts and every number becomes a `Decimal` from its string form. Pulls in requests, websockets, msgpack, sseclient-py and pandas transitively; none is imported by our runtime code outside the adapter. |
| keyring | 25.7.0 | MIT | Broker and data keys from the OS keyring (Windows Credential Manager, macOS Keychain), ADR-0026 | Imported only by `config/secrets.py`. Values are never printed, logged or written to disk by the project. |
| truststore | 0.10.5 | MIT | Lets the SDK's `requests`/`websockets` trust the operating-system certificate store | Needed on hosts whose antivirus intercepts HTTPS (owner_setup.md §1). Imported only under `brokers/alpaca/`. |

## Development dependencies

| Package | Version | License | Purpose |
| --- | --- | --- | --- |
| pytest | 9.2.0 | MIT | Tests |
| hypothesis | 6.170.15 | MPL-2.0 | Property tests (sizing maximality, fee monotonicity) |
| ruff | 0.17.4 | MIT | Lint and format (bans wall-clock and random outside approved modules) |
| mypy | 1.20.2 | MIT | Strict static typing |
| types-PyYAML | 6.0.12.20261103 | Apache-2.0 | Type stubs |

## Research-only group `ml` (ADR-0027)

Installed by default for development (`[tool.uv] default-groups`), never a runtime `[project]` dependency. Imported only under `research/` (boundary test); trained models are exported as JSON bundles that `models/` evaluates with numpy alone.

| Package | Version | License | Purpose | Notes |
| --- | --- | --- | --- | --- |
| scikit-learn | 1.9.1 | BSD-3-Clause | Regularised logistic-regression candidate filter, calibration, preprocessing (M3) | Pulls in scipy 1.18.1, joblib 1.6.0, threadpoolctl 3.8.0 |
| lightgbm | 4.7.0 | MIT | Gradient-boosted challenger filter, used only if it beats the logistic filter economically | Trained single-threaded with fixed seeds for reproducibility |

## Developer tools run outside the project environment

| Tool | Version | License | Purpose | Notes |
| --- | --- | --- | --- | --- |
| alpaca-mcp-server | 2.4.0 (exact pin in `.mcp.json`) | MIT | Alpaca's official MCP server, so Claude Code can read the paper account, market data and Alpaca docs (ADR-0032) | Run with `uv tool run` in its own environment (fastmcp 3.4.7, httpx); never imported by the project. Started through `trader secrets exec`; its trading tools are denied in `.claude/settings.json`. 2.4.1 (2026-11-04) was too new at review. |
| kraken-cli (`kraken-mcp`) | v0.5.1, tag commit 671cdd7 (2026-10-29), built with `cargo +1.99.1 install --locked` | MIT | Kraken's official CLI in MCP mode: Kraken public market data and its local paper simulator for the assistant (ADR-0036) | No Windows release asset exists, so it is built from the tagged source with its own `Cargo.lock` and the toolchain the upstream repository pins. Started with `-s market,paper` only; no Kraken key is stored; every tool outside those groups (including the `feedback` upload) is denied in `.claude/settings.json`. Never imported by the project. |
| capital-mcp | 0.5.0, commit 3207be4 (2026-10-02), pinned by commit | MIT | Capital.com's official MCP server for gold/silver CFD **demo** research (ADR-0036) | Reviewed at the pinned commit: it calls only Capital.com's API hosts (httpx, websockets) and sends no telemetry. Defaults to trading on, so it is started only through `trader secrets exec capital-demo`, which forces demo and trading off; its trading, account-setting and watchlist-writing tools are also denied. Runs in its own uv tool environment. Its `install.ps1` is not used. Added to `.mcp.json` only after the owner stores demo keys. |

## Infrastructure

| Component | Version | Source | Notes |
| --- | --- | --- | --- |
| PostgreSQL | 16.15 (Alpine) | `mirror.gcr.io/library/postgres:16-alpine` (the official image through Google's Docker Hub mirror) | Bound to 127.0.0.1:5433 only. Local development credentials live in `.env`. |
| Docker | 29.5 | Docker Desktop | Development only |

## Shortlisted upstream projects: not installed

From repositories_and_sources.md §1 and the track addenda, at their reference commits. None is part of the execution process.

| Project | Reference commit | Decision now |
| --- | --- | --- |
| qlib, FinRL-Trading, FinRL, RD-Agent | see source document | Optional offline research references. Never imported by the account worker. |
| LEAN, NautilusTrader (LGPL-3.0) | see source document | Candidate independent reference engines for M2 replay comparison. Their fractional semantics are not assumed. |
| CCXT (MIT) | `a31f1689…` | Candidate for crypto normalisation (crypto track). Builder fees must be disabled. |
| Freqtrade (GPL-3.0) | `0789f5d5…` | Reference only. Never run against the same account. GPL obligations must be reviewed before copying any code. |
| capital-api-postman (MIT) | `f88a4b6d…` | Examples only. Never run with live credentials. |

Excluded from the execution process: LLM SDKs, HTTP scraping clients and any deployment scripts from upstream projects. A boundary test checks that the runtime dependency list contains no LLM or HTTP client packages as direct dependencies; the one exception is the exactly pinned alpaca-py (ADR-0025), whose transitive `requests` is used only inside the paper adapter.
