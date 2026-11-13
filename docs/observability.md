# Observability: read-only data contract for a dashboard

For anyone building a local monitoring UI (for example a localhost dashboard) that shows what the bot decided, did and why, in real time. The UI is an **observer**. It reads the sources below and never changes them.

## 1. Rules for a dashboard

| Rule | Why |
| --- | --- |
| **Read only.** Use the read-only PostgreSQL role in section 4; never `INSERT`/`UPDATE`/`DELETE`, never run migrations. | The ledger, outbox and pause flags are the bot's safety state. A stray write can hide a loss pause or duplicate an order. |
| **Never write** `research/experiment_registry.jsonl`, `research/data_manifest.json`, `research/promotion_manifest.json`, `configs/`, `data/`. | The research log is hash-chained (one byte breaks it); manifests bind datasets by checksum. |
| **Never call Alpaca's trading endpoints and never read keys** (keyring, `~/.ai-stock-trader/keys`). Show broker state from the database, which the bot reconciles. | Orders from outside the bot bypass the risk engine and break reconciliation. |
| **Bind to `127.0.0.1` only.** No authentication exists; the data shows account state. | Local use only. |
| **Live in its own folder and branch** (for example `ui/` on a `ui/...` branch), not inside `src/trader/`. Do not add dependencies to the project's `pyproject.toml`; give the UI its own environment (`ui/package.json` or `ui/pyproject.toml`). | Runtime dependency rules are enforced by tests (`tests/test_boundaries.py`). |
| Poll every 2-5 seconds; use the `seq`/`id` columns in section 3 as cursors. | Tables are append-mostly; no change feed exists. |
| **One exception: the Desk** (`ui/server/desk/`, the console's `/desk` page at `http://127.0.0.1:4317/desk`, API under `/api/desk/*` with its own Origin, JSON and token guard). It writes only under `<repo>/.desk/` (the owner's notes and setup sheet, gitignored), reads the database through the same SELECT-only role, and starts no process: a note opens a prefilled Claude Code tab that the owner sends. | Notes for Claude have to be stored somewhere; one ignored folder keeps the bot's state out of reach. The console itself stays read-only. |

## 2. Where everything lives

| Source | What it holds | Updated when |
| --- | --- | --- |
| PostgreSQL `trader` on `127.0.0.1:5433` (URL in `.env` as `TRADER_DATABASE_URL`; never the `trader_test` database) | Paper runtime: every candidate, risk decision, order, broker event, fill, position, ledger entry, pause, alert, reconciliation and equity snapshot | Continuously while `trader paper run` runs |
| `runs/demo/<scenario>/replay.db` (SQLite; scenarios `baseline`, `baseline-rerun`, `costly`, `crash`, `loss`, `timeout`) and `runs/dev-*/replay.db` | Replay demonstrator runs in the same schema as PostgreSQL (decimals stored as TEXT, naive UTC timestamps); each scenario has `report.json`, and `runs/demo/summary.md` has the 14 acceptance checks and result hashes | `trader demo` / `trader replay` |
| `research/experiment_registry.jsonl` | Every registered trial, result, note, budget decision and holdout unlock; one JSON object per line, hash-chained | `trader research ...` commands |
| `research/reports/*.md` | Human-readable study reports (`baseline_report.md` = crypto baseline, `stocks_baseline_v1_report.md`, `model_report.md` = crypto filters) | After each study run |
| `runs/research/<study_id>/results.json`, `model_results.json` | Machine-readable study results behind each report (per trial, per window, G2 checks) | After each study run |
| `research/promotion_manifest.json` | Release gates G0-G4 and their status (G5 is defined in risk_and_validation.md but not yet in the manifest) | Owner/gate changes |
| `research/data_manifest.json` | Datasets, coverage, gaps, corporate actions, checksums | `trader ingest-history` |
| `runs/paper/certification/*.json` | Paper certification evidence (orders tried, visibility timings, results); content-hashed, never overwritten, no secrets (ADR-0035) | `trader paper certify --yes` (owner-confirmed; none run yet) |
| `configs/policy/base.yaml`, `configs/paper.yaml` | Risk limits (USD 50 budget, loss windows, spread caps) and paper wiring | Rarely; changes are reviewed |
| `data/market/index.json`, `data/market/<group>/<SYMBOL>.json` | Display prices for the Markets page: stocks (Alpaca IEX), crypto (Kraken public OHLC) and CFDs (Capital.com demo mids), section 6 | Every 5 minutes while `python -m trader.market watch` runs (started by `npm run dev`) |
| `data/intel/index.json`, `data/intel/latest/<group>/<SYMBOL>.json`, `data/intel/records/`, `snapshots/`, `runs/` | News (Alpaca, Benzinga), insider filings (SEC EDGAR Form 4) and pre-market activity (Alpaca SIP), with what was known per instrument over time, section 7. Information only: no strategy or risk rule reads it | News and pre-market every 5 minutes, insider filings every 30, while `python -m trader.intel watch` runs (started by `npm run dev`) |

## 3. The decision trail in PostgreSQL

All times are UTC (`timestamptz`). Money, prices and quantities are exact `NUMERIC(38,18)`; render them as decimals, never via binary floats if you compute sums. `*_json` columns hold canonical JSON text.

```
candidates ──candidate_id──> risk_decisions ──decision_id/candidate_id──> order_intents
                                                                  │ client_order_id
                     submission_outbox <──intent_id───────────────┤
                     broker_orders / broker_order_events <────────┤
                     fills (intent_id, execution_id) <────────────┘
                       └─> positions, ledger_entries, cash_lots, fee_accruals
exit_plans (entry_intent_id) · risk_reservations · pause_state · alerts
reconciliation_runs · equity_snapshots · loss_windows · bars · events (replay only)
```

| Question the UI answers | Table(s) | Key columns |
| --- | --- | --- |
| What did the strategy propose? | `candidates` | `candidate_id`, `strategy_id`, `instrument_id`, `decision_time`, `payload_json` (stop, target, reference price, feature snapshot hash) |
| Was it approved, and why or why not? | `risk_decisions` | `seq` (cursor), `candidate_id`, `approved`, `reasons` (e.g. `NO_EDGE`, `UNKNOWN_COSTS`, `STALE_DATA`, `PAUSED`), `decided_at`, `model_hash`, `payload_json` (sizing and limits used) |
| What order was created? | `order_intents` | `seq` (cursor), `client_order_id`, `side`, `purpose` (entry/exit), `order_type`, `quantity`, `limit_price`, `state`, `decision_id`, `candidate_id`, `created_at`, `updated_at` |
| Was it sent? | `submission_outbox` | `intent_id`, `status` (PENDING/CLAIMED/DONE/ABANDONED), `attempts` |
| What did the broker say? | `broker_order_events` | `id` (cursor), `status`, `cum_filled_qty`, `event_time`, `disposition` (`APPLIED`, `FILL_POSTED`, `FILL_DUPLICATE`, `STALE`, `UNSOLICITED`; `PENDING` briefly while processing; a duplicate event is not stored). `broker_orders` exists in the schema but nothing writes it: broker state lives in `order_intents` (`state`, `broker_order_id`, `cum_filled_qty`) and these events |
| What filled, at what price and cost? | `fills` | `id` (cursor), `execution_id`, `side`, `quantity`, `price`, `fee`, `fee_status`, `exec_time`, `realized_gross`, `realized_net` |
| What is held now? | `positions`, `exit_plans` (`active = true`) | quantity and `cost_basis` (a total: average cost = `cost_basis / quantity`); planned stop/target and `max_hold_until` |
| How much money (the USD 50 subledger)? | `equity_snapshots`, `ledger_entries`, `cash_lots` | `equity`, `high_water_mark`, `ts`; ledger is append-only. Snapshots are written only when equity or the high-water mark changes (at start, after a fill, at a new loss window, or on each 5 s mark while a position is open), so a flat bot has about one row: draw equity as a step line |
| Is the bot paused? | `pause_state` (`active = true`) | `scope` (`PORTFOLIO`/`ACCOUNT`), `category` (`LOSS_BREACH`, `HEALTH`, `OWNER`), `cause` (e.g. `DAILY_NY`, `WEEKLY_UTC`, `DRAWDOWN`, `CAPITAL_FLOOR`, `UNKNOWN_ORDER`, `CLOCK_SKEW`, `FEED_GAP`, `RECONCILIATION_FAILED`, `LEADERSHIP_LOST`), `raised_at`, `details` |
| Anything wrong? | `alerts` | `id` (cursor), `severity`, `code`, `message`, `raised_at` |
| Does the bot agree with the broker? | `reconciliation_runs` | `status` (PASSED/FAILED), `finished_at`, `summary_json` |
| Loss-window usage | `loss_windows` | `kind` (NY_SESSION, NY_WEEK, UTC_DAY, UTC_WEEK), `start_equity`, `breached_at` |
| Market data the bot used | `bars`, `events` | completed 5-minute bars per instrument (paper writes these); `events` holds raw journaled quotes/trades from the **replay** runner only. Paper keeps quotes in memory and does not store them |
| Is a worker running? | `reconciliation_runs`, `pg_locks` | No heartbeat table exists. A running paper worker reconciles every 60 s during market hours and holds the session advisory lock `advisory_lock_key("account:<account_id>")` (`trader.domain.ids`), visible in `pg_locks` as `classid` = high 32 bits, `objid` = low 32 bits, `objsubid = 1` |

`runs` is also in the schema but never written. Note: with no validated model, every paper candidate is rejected with `NO_EDGE`; with the current instrument file (all `tradable: false` until broker metadata is fetched) most rejections read `CAPABILITY,NO_EDGE`. That is the intended safe behaviour (README, M4), so a dashboard that shows many such rejections is working correctly.

The console in `ui/` (see [ui/README.md](../ui/README.md)) implements this contract.

## 4. Read-only PostgreSQL role

Run once (choose your own password; keep it in the UI's own local, gitignored config):

```powershell
docker compose exec -T postgres psql -U trader -d trader -c "CREATE ROLE trader_ro LOGIN PASSWORD 'choose-a-local-password';"
docker compose exec -T postgres psql -U trader -d trader -c "GRANT CONNECT ON DATABASE trader TO trader_ro; GRANT USAGE ON SCHEMA public TO trader_ro; GRANT SELECT ON ALL TABLES IN SCHEMA public TO trader_ro; ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA public GRANT SELECT ON TABLES TO trader_ro;"
```

The service name and user follow `docker-compose.yml` and `.env` (`TRADER_PG_USER`); adjust if yours differ. The UI then connects as `postgresql://trader_ro:<password>@127.0.0.1:5433/trader`.

`ui/scripts/create-readonly-role.ps1` does the same in one step: it also sets `default_transaction_read_only = on` for the role, generates a random password, passes it to psql on stdin and writes it only to `ui/.env.local` (gitignored). It is safe to re-run; that rotates the password.

## 5. Research log format

Each line of `research/experiment_registry.jsonl` is `{seq, event, at, payload, prev_hash, record_hash}`. `event` is one of `REGISTER` (a trial's full definition), `RESULT` (verdict and summary numbers as strings), `NOTE`, `BUDGET_DECISION`, `HOLDOUT_UNLOCK`. To verify the chain, run `uv run trader research status`; a UI can show `seq`, `event`, `at`, the trial id and verdict. Numbers in results are decimal strings.

## 6. Market feed files (data/market)

Written by the market feed (`trader.market`, ADR-0045), a separate process with no order authority; the console only reads them. They are display data, never research inputs: nothing here is listed in `research/data_manifest.json`.

**`data/market/index.json`** (written last, after every instrument file):

| Field | Meaning |
| --- | --- |
| `version` | `1` |
| `generated_at` | UTC time of the refresh (ISO, `Z`) |
| `every_minutes` | Refresh interval; a group is stale when `updated_at` is older than twice this |
| `retention_days` | `{"5m": 14, "1h": 180, "1d": 730}` |
| `groups[]` | `id` (`stocks`, `crypto`, `cfd`), `title`, `source` (label), `source_kind`, `day_rule` (`nyse` or `utc`), `basis`, `status` (`ok`, `partial`, `error`), `error` (text or null), `updated_at` (last success or null), `checked_at` (last attempt), `retry_after` (no request before this time, after a refused Capital.com session) and `instruments[]` |
| `instruments[]` | `id` (`stocks:AAPL`, `crypto:BTC`, `cfd:GOLD`), `symbol`, `name`, `source_symbol` (Kraken pair or Capital.com epic), `file` (relative, `<group>/<SYMBOL>.json`), `precision` (display decimals, the source's decimals clamped to 2-6), `has_volume` (false for CFDs), `basis` (`trade` or `mid`), optional `min_deal`, `last_price`/`last_at` (newest close and its bar start) and `series` with `first`, `last` (bar starts, epoch seconds) and `count` for `5m`, `1h` and `1d` |

**`data/market/<group>/<SYMBOL>.json`**: `{"id": "crypto:BTC", "series": {"5m": [[t, "o", "h", "l", "c", "v"], ...], "1h": [...], "1d": [...]}}`.

- `t` is the bar start in epoch seconds (UTC); rows are oldest first, one per start. Prices and volume are decimal strings exactly as the source gave them. Render them, never sum them as floats.
- Stocks: regular-session bars only (09:30-16:00 New York); hourly bars start at 09:30, 10:30, ... (the last one 30 minutes); a daily bar starts at the session open. Crypto and CFDs: UTC clock grid; a daily bar starts at 00:00 UTC.
- CFD prices are bid/ask mids and their volume is `"0"`; crypto and stock volume is traded volume (IEX is a single venue).
- The last bar of a series may still be forming; the next refresh replaces it.
- Each file is replaced atomically (temporary file, then rename), so a reader never sees a partial file. Read the index first and only open files it lists.

The console serves them as `GET /api/market` (the index, or `{"available": false}` before the first refresh) and `GET /api/market/<id>?tf=5m|15m|1h|1d&limit=N` (bars from the finest stored series that covers the range, rolled up on the group's day rule; `barTf` says when a coarser series had to be used).

## 7. Intel feed files (data/intel)

Written by the intel feed (`trader.intel`, ADR-0047), a separate process with no order authority; the console only reads them. Nothing that decides a trade reads them, and they are never research inputs: nothing here is listed in `research/data_manifest.json`.

Every title and summary was written by an outside publisher or filer. It is stored as one plain line of bounded length (no markup, no control or invisible characters). Render it as text, never as HTML or Markdown, and link only to the feed's own hosts (`www.benzinga.com`, `www.sec.gov`).

**`data/intel/labels/<YYYY-MM>.jsonl`** (append-only, one JSON object per line, filed by the month a row was first seen; ADR-0051): `schema_version` (`1`), `id` (`<target>@<session date>`, e.g. `stocks:NVDA@2026-11-11`), `rev` (1, 2, ...: a later row with the same `id` replaces the earlier one for display, and both stay on file), `first_seen_at` (UTC, set by the store), `content_hash`, and `label`: `target`, `session_date`, `catalyst` (`earnings`, `analyst`, `corporate`, `product`, `legal`, `sector`, `macro`, `none`, `unclear`), `direction` (`up`, `down`, `unclear`), `confidence` (`low`, `medium`, `high`), `evidence[]` (references to stored records and snapshots), `note` (one plain line, at most 200 characters, written by the labeller: render as text), `labeller`, `method`. A label seen after its session's open says nothing about what was known before it; compare `first_seen_at` with the open. The console does not read this file yet.

**`data/intel/index.json`** (replaced atomically, written after the `latest` files):

| Field | Meaning |
| --- | --- |
| `version` | `1` |
| `generated_at` | UTC time of the refresh (ISO, `Z`) |
| `every_minutes` | The shortest source cadence; the feed is stale when `generated_at` is older than three times this |
| `sources[]` | `id` (`alpaca_news`, `alpaca_premarket`, `edgar_form4`), `kind` (`news`, `premarket`, `insider`), `label`, `every_minutes`, `status`, `note`, `error`, `updated_at` (last successful read or null), `checked_at` (last attempt), `retry_after`, `new` (records stored by the last run) and `cursor` (the feed's own read position; the console does not serve it) |
| `sources[].status` | `ok`; `partial` (some instruments failed, see `error`); `error`; `waiting` (the insider source without the owner's `SEC_EDGAR_CONTACT` line: no request is made); `idle` (nothing to do now: outside the pre-market window, or not read yet) |
| `targets[]` | `id` (the watchlist id, `stocks:NVDA`), `group`, `symbol`, `name`, `file` (`latest/<group>/<SYMBOL>.json`), `has` (`news`, `insiders`, `premarket`: which parts apply), `news_proxy` (set when the news is about a stand-in fund, not the instrument), `news_count_24h`, `last_news_at`, `insider_net_value`, `premarket_session`, `premarket_gap_pct`, `snapshot_hash` and `snapshot_at` (when the current snapshot was stored) |

**`data/intel/latest/<group>/<SYMBOL>.json`** (replaced atomically on every refresh): `id`, `name`, `generated_at`, `snapshot` (below), `news[]` (newest first, at most 30, a syndicated headline once: `id`, `rev`, `title`, `summary`, `category`, `tags`, `at`, `first_seen_at`, `url`, `feed`, `author`, `proxy`) and `insiders[]` (newest first, at most 20: `id`, `rev`, `title`, `category`, `at`, `first_seen_at`, `url`, `facts`).

**Append-only logs** (JSON lines, one object per line, LF; a reader skips an unfinished last line):

- `records/<kind>/<YYYY-MM>.jsonl`, filed by the month a record was first seen: `schema_version` (1), `id` (`<source>:<source id>`), `rev`, `first_seen_at`, `content_hash`, `headline_key` and `record`. The record holds `kind`, `source`, `source_id`, `url`, `targets`, `published_at`, `updated_at`, `category`, `tags`, `extractor_version`, `title`, `summary`, `facts` and `provenance`. A line is never rewritten: a changed item is appended as the next `rev` of the same `id`.
- `snapshots/<group>/<SYMBOL>/<YYYY-MM>.jsonl`: what was known about one instrument at `as_of`, appended only when it differs from the previous one.
- `runs/<YYYY-MM>.jsonl`: one line per refresh, `{"at", "sources": {id: status}, "new"}`. A gap between lines is a time the feed was not running.

**A snapshot** (`trader.intel.snapshot`, `version` `intel-snapshot-v1`): `id`, `as_of`, `hash` (of everything but `as_of`), `sources` (each source's status then) and three parts, each null when it does not apply to the instrument:

- `news`: `count_2h`, `count_24h`, `categories_24h`, `last_at`, `latest[]` (three items: `id`, `title`, `category`, `at`, `url`) and `proxy`.
- `insiders`: `window_days`, `filings`, `buys`, `sells` (filings with open-market purchases or sales), `buy_value`, `sell_value`, `net_value` (USD, decimal strings), `last_at` and `latest[]`. Only Form 4 codes P and S of non-derivative securities are counted; grants, option exercises, gifts and tax withholding are listed in the record's `facts` and never counted.
- `premarket`: `session` (New York date, or null when nothing was read yet), `prior_close`, `last`, `gap_pct` (percent against the prior close), `volume`, `high`, `low`, `bars`, `through`. Bars from 04:00 to 09:30 New York, sixteen minutes late (the free plan's delay).

A record counts in a snapshot only if it had been seen by then (`first_seen_at <= as_of`), in the revision current then; the time windows use the publication time, never a time later than first seen. Numbers are decimal strings: render them, never sum them as floats.

**Categories** (`extractor_version` `keywords-v1`, fixed keyword rules, no model): `earnings`, `analyst`, `guidance`, `financing`, `mna`, `legal_regulatory`, `unknown`. Insider records use `insider_buy`, `insider_sell`, `insider_mixed`, `insider_other`; pre-market records `premarket`.

The console serves them as `GET /api/intel` (the index without cursors, or `{"available": false}` before the first refresh), `GET /api/intel/<id>` (the `latest` file) and `GET /api/intel/<id>/known?at=<ISO time with zone>`: the newest stored snapshot at or before that time (`snapshot`, `snapshotAgeSeconds`), the feed's last run at or before it (`lastRunAt`, `lastRunAgeSeconds`) and `everyMinutes`. Only ids the index lists are served.

## 8. Practice files (runs/practice)

Written by the assistant's practice journal and the practice watch (`trader.practice`, ADR-0044, ADR-0046, ADR-0049); the console only reads them. All are gitignored with the rest of `runs/`. Nothing here is research evidence, and nothing in the bot reads them.

- `runs/practice/capital_demo_journal.jsonl` and `runs/practice/kraken_paper_journal.jsonl` (the crypto sleeve's chain; its name dates from the sleeve's first venue): one canonical JSON line per event (`seq`, `event` PLAN, OPEN, AMEND, CLOSE, CANCEL, NOTE or REVIEW, `at`, `actor`, `venue`, `account_currency`, `payload`, `prev_hash`, `record_hash`). `record_hash` is the SHA-256 of the line without that member; `prev_hash` chains the lines. A sleeve's realized equity is its start plus every CLOSE `pnl_usd`. Which journal belongs to which sleeve is the `journal:` line of `configs/practice/capital_demo.yaml` and `configs/practice/crypto_cfd.yaml`.
- `runs/practice/sleeves.json`: `updated_at` and per sleeve `venue`, `start_usd`, `equity_usd`, `closed_trades`, `winning_trades`, `open_trades`, `last_event_at`. Written by `trader journal summary` and the watch.
- `runs/practice/scan.json`, replaced atomically every 15 minutes by `python -m trader.practice watch` and by `trader journal scan`:
  - `version` (2 since ADR-0052), `generated_at`, `setup` (`weekly_momentum_v1`), `title`, `sleeve`, `equity_usd`, `risk_limit_usd`, `expected_bar_day` (yesterday: the day the newest finished daily bar should have), `next_decision_at` (the next Monday 00:00 UTC), `params`.
  - `feed`: the market feed's CFD group as the scan found it: `status`, `updated_at`, `age_minutes`, `stale` (true when the status is not `ok`, the index is missing, or the last refresh is more than three of the feed's intervals old).
  - `readings[]`, one per epic of the setup: `epic`, `day` (the bar read), `stale` (that bar is older than `expected_bar_day`), `close`, `change` (the close against the close a week earlier, minus one), `atr`, `direction` (`LONG`, `SHORT` or null), `decision_bar` (the bar is Sunday's), `signal` (a decision bar with a direction), and `ticket` (null without a signal): `status` `TRADE`, `TOO_LARGE` (the smallest contract risks more than the limit) or `GROUP_BUSY` (a crypto trade is open, or the larger move was taken first), `direction`, `size`, `stop`, `target`, `risk_usd`, `max_hold_hours`, `note`.
  - `open_trades[]`, every open trade of both sleeves: `sleeve`, `trade_id`, `epic`, `direction`, `size`, `entry`, `stop` and `target` in force, `due_at` (the time stop), `overdue`, `last_price` (the feed's), `beyond_stop`, `at_target`, `exit_signal` (null: the weekly setup exits by stop, target or time).
  - A scan that could not run has only `version`, `generated_at` and `error`.
  - Prices are decimal strings. `beyond_stop` and `at_target` compare the feed's last mid price with the level: the broker holds the stop and target and decides the fill, so they mean "check the broker", not "closed".
- `runs/practice/trader.json`, replaced every minute by the practice trader (ADR-0056): `version` 1, `pid`, `live`, `started_at`, `updated_at` (the heartbeat; the console treats more than five minutes as stopped), `next_decision_at`, `hedging`, `entries_paused` (why no entry is taken, or null), `errors` (the last five), and `setups[]`: `key`, `title`, `sleeve`, `status` (`tested`, `forward_test` or `practice`), `trial`, `enabled`, `decides`, `open_trades`, `forward_trades`, `forward_result_usd`, `loss_cap_usd`, `switched_off`, `last_decision`.
- `runs/practice/decisions.jsonl`: one line per decision of the trader, taken or not: `setup`, `epic`, `decided_at` (the decision bar's end), `direction`, `outcome` (`OPENED`, `NO_SIGNAL`, `NO_BAR`, `BUSY`, `GROUP_BUSY`, `ENTRY_LIMIT`, `CLOSED`, `SPREAD`, `HEDGING_OFF`, `STOP_PASSED`, `TARGET_PASSED`, `NOT_SIZED`, `TOO_SMALL`, `SWITCHED_OFF`, `PAUSED`, `LATE`, `DONE`, `DRY_RUN`, `REJECTED`, `ORDER_UNKNOWN`, `JOURNAL_REFUSED`, `SLEEVE_FULL`, `ERROR`), `detail`, `trade_id`, `live`, `at`.
- `runs/practice/trader-<date>.log`: the trader's window output, one line per event, UTC.
- `runs/practice/forward.json`, written by `python -m trader.practice forward`: per setup the closed trades, wins, net and per-trade result, close reasons, loss-cap and review state, its league state (`league`: `OFF`, `STOPPED`, `COLLECTING`, `CANDIDATE` or `DROP`, with `league_note`; ADR-0062), and the trial's base and stressed per-trade averages (none for a practice setup).
- `runs/practice/certification/<time>.json`: the broker's answers to the order-path certification, without account identifiers.
