# Trader Console

A read-only localhost dashboard for the bot. It shows every decision and why it was made, the money in the USD 50 subledger, positions, risk and pause state, and the research results, updated live.

It only observes the bot, following the rules in [docs/observability.md](../docs/observability.md):

- It connects through a SELECT-only database role, and that connection is also forced read-only.
- It never writes the research log, the manifests, configs or `data/`.
- It never calls Alpaca and never reads keys. It never loads the repository's `.env`, which holds the bot's read-write password.
- Market prices for the Markets page come from files: the market feed (`python -m trader.market`, a separate Python process with no order authority, ADR-0045) writes `data/market/`, and the console only reads them.
- News, insider filings and pre-market activity come from files in the same way: the intel feed (`python -m trader.intel`, ADR-0047) writes `data/intel/`. Headlines are someone else's text: they are rendered as plain text and linked only to the feed's own hosts.
- It binds to 127.0.0.1 only and refuses requests whose Host header is not localhost.
- Nothing here can pause, resume, trade or change a limit. Wherever something is adjustable, the console shows the exact `trader …` command or config key to use yourself.

## Run it

Requirements: Node 22.13 or newer, Docker Desktop, and pnpm (or Corepack, which ships with Node). From the repository root:

```powershell
npm run dev
```

That one command:

1. starts Docker Desktop if it is not running;
2. starts the bot's PostgreSQL (`docker compose up -d postgres`) and waits until it is healthy;
3. installs the UI dependencies when `ui/node_modules` is missing or older than the lockfile;
4. on the first run only, creates the read-only database role (see below);
5. starts the console with hot reload and opens **http://127.0.0.1:4317**;
6. starts the market feed beside it (`python -m uv run --frozen --quiet python -m trader.market watch --every 5`, output prefixed `market │`), which refreshes `data/market/` every 5 minutes;
7. starts the intel feed beside it (`python -m trader.intel watch`, output prefixed `intel │`), which reads news, insider filings and pre-market activity into `data/intel/`.

Everything lives at that one address: the console pages, the Desk at `/desk` and the API under `/api`. In development Vite runs inside the same server as middleware, so there is no second port and no proxy.

Ctrl+C stops the console and everything it started, including the market feed and the intel feed. PostgreSQL keeps running, because the bot uses it too.

| Command (repository root) | What it does |
| --- | --- |
| `npm run dev` | Docker, PostgreSQL, the console with hot reload, then opens the browser |
| `npm run desk` | The same, opening the Desk |
| `npm start` | The same with a production build instead of hot reload |
| `npm run bot` | The paper bot (`trader paper run --config <the stocks book>`), in its own terminal; the book is `TRADER_PAPER_CONFIG`, by default `configs/paper_explore.yaml`, and the console shows the same one. It refuses to start while another paper bot runs. `npm run dev` never starts it. |
| `npm run day` | Everything for a trading day, each in its own window and only when missing: the database, the console, the paper bot (`npm run bot -- --practice`) and the practice trader (`npm run trader`). It never stops anything. `-- --dry-run` only says what is missing. |
| `npm run setup` | Steps 1-4 only |
| `npm run db:up` / `npm run db:stop` | Start or stop only PostgreSQL |
| `npm run test:ui` / `npm run typecheck:ui` | The UI checks |

Flags go after `--`, for example `npm run dev -- --no-docker`:
- `--no-docker` runs the console only; replays need no database;
- `--no-open` skips opening the browser;
- `--no-market` does not start the market feed (the Markets page shows the last files in `data/market/`);
- `--no-intel` does not start the intel feed (news, insider filings and pre-market show the last files in `data/intel/`);
- `--no-practice` does not start the practice watch (the Signals block shows the last `runs/practice/scan.json`);
- `--open=/markets` opens another page.

If 4317 is already serving the console, `npm run dev` says so and opens it. If another program holds the port, it stops and shows the command that finds that program.

Inside `ui/`, `pnpm run dev` does the same thing. Use `pnpm run …`, not `pnpm dev`: the `pnpm-workspace.yaml` file (which allows esbuild's install script) makes pnpm treat a bare `pnpm dev` as a binary name.

### Live data: the read-only role

On the first `npm run dev`, when `ui/.env.local` has no `UI_DATABASE_URL`, the script creates the SELECT-only role `trader_ro`. It generates a random password, sends the SQL to psql on stdin (never on a command line), and writes the URL only to `ui/.env.local`, which git ignores. Any other lines already in that file are kept. To do it by hand instead:

The console reads **the database the paper bot writes**. `configs/paper.yaml` names the variable that holds the bot's database URL (`database.url_env`, today `TRADER_PRACTICE_DATABASE_URL`); `npm run dev` takes the database name from that line in `.env`, or `trader_practice` when the line is absent. When `ui/.env.local` points at another database, the script grants the role on the bot's database, keeps the role's password, rewrites only the database name in `UI_DATABASE_URL`, and asks you to restart a console that is already running. It creates the bot's database empty when it does not exist yet. `npm run bot` hands the bot the `TRADER_*` lines of `.env` and, without a practice line, the practice database on the same server; it never falls back to the pilot database. The logic is in `scripts/console-db.mjs` (tests: `tests/console-db.test.ts`).

```powershell
powershell -ExecutionPolicy Bypass -File ui\scripts\create-readonly-role.ps1
```

The grants include `ALTER DEFAULT PRIVILEGES`, so the role can be created before the first `trader paper run`. Tables the bot creates later are readable automatically. Until then, the console says "Connected. The paper bot has not run yet."

## Sunshine Desk: notes that open a Claude Code chat

The **Desk** page of the console, http://127.0.0.1:4317/desk (sidebar, or `G` then `K`). It is the local main copy of the owner setup sheet: the goal bar, what needs you next, the tracker and decisions, and a notes box. Each note opens a **new Claude Code tab in VS Code with the note already typed in**; you press Enter there. Nothing runs on its own.

It used to be a separate app on port 4320. The old address now redirects to `/desk` (when nothing else holds 4320), and `pnpm run desk` / `npm run desk` start the console and open the Desk.

- **First note:** the browser asks to open Visual Studio Code (tick "always allow" for 127.0.0.1), and VS Code may ask to let Claude Code open the link. VS Code sends the link to the most recently focused VS Code window, so keep the repository's window last.
- **Where things live:** `<repo>/.desk/` (gitignored). `sheet.json` is maintained by Claude. `notes/<id>.md` is written once by the Desk and then belongs to Claude, who sets `status: done` and writes the answer under `## Reply`. `state.json` records when a note was opened. The page updates live when any of them changes.
- **Long notes:** Chromium drops `vscode://` links longer than about 2,000 characters, so a long note opens with a pointer to its file instead of the full text.
- **Goal bar:** the latest PORTFOLIO paper equity snapshot through the same SELECT-only role, on a log scale from USD 50 to USD 10,000 (each tick is one doubling). Without data it shows the USD 50 start.
- **Safety:** the Desk API is under `/api/desk/*`, with its own guard on top of the console's localhost guard. A write needs the console's exact Origin (`http://127.0.0.1:4317` or `http://localhost:4317`), a JSON body of at most 64 KB and a per-process token that other sites cannot read. It writes nothing outside `.desk/`, never writes the database and never starts a process. `tests/desk.test.ts` checks all of this; `node e2e/desk.mjs` sends real notes through the page and cleans up.

## What each page answers

| Page | Answers |
| --- | --- |
| Overview | How much is there (conservative equity, step chart), what is the bot doing (3D Core status), what is held, the latest decisions, how close each loss limit is |
| Decisions | Candidate → approved → sent → filled → exited funnel, rejection reasons in plain English, and a full trail per decision: the bot's own reason text, sizing, order, broker events, fills and exit plan |
| Markets | Three labelled rows, Stocks (Alpaca IEX), Crypto (Kraken) and CFDs (Capital.com demo), each with its source, last update and a stale chip; instruments by id in the URL (`/markets/crypto:BTC`; `/markets/AAPL` is a stock). Trading 212-style chart: range pills 1D/1W/1M/3M/1Y/All pick the resolution (5m, 15m, 1h, 1d), on New York sessions for stocks and rolling UTC days for crypto and CFDs; Line or Candles; the headline price follows the crosshair. Stocks join IEX history (split-adjusted), the feed and the bot's own bars, with every proposal and fill marked, live stop and target lines and instrument certification; CFDs show mid prices without volume |
| Money | A Sankey of where each dollar sits (it balances exactly), the policy's capacity, the ledger with running cash, settlement and fee accruals |
| Risk & health | Loss windows, drawdown, the floor, exposure caps, the reconciliation heartbeat, orders in flight, alerts and pause history (with the exact resume command) |
| Learning | Trial budget, research log integrity, release gates G0-G4, study cards (rule baselines and model-filter studies), a trials x windows heatmap, each trial's windows and confidence intervals, model-filter variants against random and turnover controls with a plain-language verdict, the G2 checks, data coverage and the reports |
| Rules | Every policy limit in dollars today and its YAML key, drift between the file and the running policy, and the owner commands |

### Following the bot live

- **Activity** (button top right, or `A`): a docked, chronological log of every decision, order, broker event, fill, pause, alert and failed reconciliation, newest first, each linking to its detail. Duplicate and stale broker events are labelled as ignored, because that is the bot's de-duplication working.
- **Toasts** appear for notable events as they happen. A burst (a fast replay) collapses into one summary toast.
- **Desktop notifications** are opt-in (bell in the Activity panel). They fire for live data only, never for replays, and only while the console tab is in the background.
- **Session ribbon** (Overview): today's NYSE session on one line, with the first-30-minute and last-30-minute no-entry zones, the entry window and the close procedure, a moving "now" needle, and every decision and fill placed on it.
- **Keyboard:** `Ctrl K` to jump anywhere; `A` Activity; `G` then `O`/`D`/`M`/`W`/`R`/`L`/`U`/`K` for Overview, Decisions, Markets, Money, Risk, Learning, Rules and the Desk. In replay, `Space` plays or pauses and `N` jumps to the next event.

### Live paper vs replay

- **Live paper** polls PostgreSQL every 2 s using `id`/`seq` cursors.
  - The bot stores 5-minute bars, not quotes, so live prices move once per bar.
  - "Running" means a worker holds the account's leadership lock (`pg_locks`). The 60-second reconciliations act as its heartbeat.
- **Replay** streams a recorded demonstrator session (`runs/demo/<scenario>/replay.db`) at 1×, 10×, 60× or 300×, with play, pause and scrubbing. It is clearly labelled SYNTHETIC REPLAY.
  - Scenarios: `baseline`, `costly`, `crash`, `loss` (a loss-breach pause) and `timeout` (an unknown order outcome reconciled).
  - A test checks that each replay's final state matches the demonstrator's own `report.json`.

## Configuration (ui/.env.local, gitignored)

| Variable | Meaning |
| --- | --- |
| `UI_DATABASE_URL` | Read-only PostgreSQL URL of the bot's database (written by `npm run dev` or `scripts/create-readonly-role.ps1`). |
| `UI_REPO_ROOT` | The repository to observe. Defaults to the parent of `ui/`. Set it when the console runs from a second checkout, such as a git worktree, so that it still reads the real `runs/`, `data/` and `research/`. |
| `UI_PORT`, `UI_POLL_MS` | Console port (4317) and poll interval in ms (2000, clamped to 2000-5000). |
| `UI_DESK_PORT` | The old Desk port that redirects to `/desk` (4320). |

`npm run dev` also uses `UI_REPO_ROOT` to find `docker-compose.yml`, so PostgreSQL is started from the main checkout even when the console runs from a worktree.

One process serves the page and the API on the same origin, in development (Vite as middleware) and in production (the built `dist/`). There is no CORS and no dev proxy. Parquet decoding runs in a worker thread, so loading price history never pauses the live stream.

## Design

- Apple-style restraint with Trading 212's money-first layout: one big number, a calm chart, clean rows.
- Light and dark themes follow the OS, and there is a toggle in the sidebar. Times can be shown in ET or local time.
- The colour tokens are in `src/design/tokens.css`.
  - Chart series use the validated dataviz reference palette.
  - The gain/loss mark colours were checked with the dataviz validator; every check passes in both modes.
  - Gains and losses always carry a sign and an arrow, so colour is never the only signal.
- **The 3D Core** (`src/three/Core.tsx`) is the bot's presence on screen.
  - It is a brushed-metal gimbal built with Higgsfield: a GPT Image render, turned into a mesh with Tripo H3.1, then textures stripped and meshopt-compressed to 180 KB by `scripts/prepare-core.mjs`.
  - Its inner light shows the bot's state: green running, blue idle or replay, amber paused, red alert. It pulses on each decision and dims when the market is closed.
  - It respects `prefers-reduced-motion` and falls back to a static render without WebGL.
- **Other generated assets:**
  - The mark comes from Recraft V4.1, as a vector converted to a mask.
  - The empty-state renders were made with GPT Image, with backgrounds removed.
  - The three sleeve emblems (`public/assets/sleeve-*.webp`, 5-7 KB each) were made with GPT Image through Higgsfield, backgrounds removed, cropped to 192 px WebP. One emblem and one colour (`--sleeve-stocks`, `--sleeve-crypto`, `--sleeve-cfd`) mark a sleeve wherever it appears.
- **The mission panel** (`src/features/mission/`, first on the Overview) shows the three USD 100 sleeves and their sum against the target (ADR-0046).
  - Data: `GET /api/mission` reads `configs/practice/*.yaml` and the hash-chained practice journals under `runs/practice/` (chain verified; a failed check is shown), plus the stocks sleeve's start and portfolio id from `configs/paper.yaml`. The stocks sleeve's equity and curve come from the bot's own tables, and only when they belong to that portfolio.
  - Motion carries state, nothing else: the bar fill moves when the total changes, a sleeve's light pulses only while the bot runs or a trade is open, and the sparkline draws the sleeve's realized equity. All of it stops under `prefers-reduced-motion`.
  - The sidebar is grouped by what a page is about: Mission (Overview, Markets, Desk), Stock bot (Decisions, Money, Risk & health, Rules), Research (Learning).
- **One trade shape for every sleeve** (`shared/trades.ts`): the bot's stock round trips are rebuilt from its own tables (entry fills, exit fills, fees, net result; they add up to the session's totals, which a test checks) and the crypto and CFD trades come from the practice journals.
  - **Money** starts with all three sleeves (balance, closed and open trades, share won, net result, fees, financing, average result in risk units) and one table of every trade; the stock bot's ledger follows.
  - **Trade story** (`/trades/:sleeve/:id`, `src/features/trades/TradeStory.tsx`): the plan as written before the order, the price chart framed on the trade with entry, stop and target lines and in/out markers, the result (net, fees, financing, R multiple, time held), the timeline of what happened, and the lesson from the journal's REVIEW. A stock trade links to its full decision trail.
  - **The Core's three rings** are the sleeves: a ring glows in its sleeve's colour and turns while that sleeve has something live, rests dim when it has not, and turns amber at the sleeve's loss floor. `MarketChart` gained `visibleTo` and `fitLines` for the story page.

## Checks

```powershell
pnpm run test        # SQL guard, lock-key parity with the bot, localhost guard, replay totals == report.json
pnpm run typecheck
node e2e/shoot.mjs test-results/shots replay:baseline   # screenshots of every page, light and dark; fails on console errors
node e2e/desk.mjs                                       # sends real Desk notes, checks the VS Code links, cleans up
```

Both e2e scripts need the console running (`npm run dev`).

The screenshot script uses the installed Microsoft Edge, with software WebGL so the 3D Core renders.

## Calendar

`shared/nyse-sessions.json` is exported from the bot's own `ExchangeCalendar`, so the console's session phases match the bot exactly. It runs to 2027-11-08, which is as far ahead as `exchange_calendars` goes. After that date the header says "Calendar ends; re-export". To re-export, from the repository root:

```powershell
uv run python ui/scripts/export_calendar.py --start 2026-01-01 --end 2027-11-08
```

## Not here yet (bot-side changes, not UI work)

The console shows only what the bot records. These would make it richer. Each is a change to `src/trader/`, so each needs its own review:

- A per-cycle heartbeat row, and the quote and `SignalScore` used for each decision. That would give a real per-decision confidence; today paper decisions carry `model_hash: none`.
- Saved trade lists and equity curves from research studies. Today the Learning page has per-window figures only.
- Persisted live quotes, for price movement finer than 5 minutes.
