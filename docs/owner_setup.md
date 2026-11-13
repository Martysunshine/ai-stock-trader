# Owner setup

Who this is for: the account owner. Nothing in this project opens accounts, buys subscriptions, moves money or changes broker settings. Those are owner actions, taken only when a milestone needs them.

## 1. Local development environment

The steps below are the same on any machine.

| Step | Command / note |
| --- | --- |
| Python tooling | `python -m pip install --user uv`, then `python -m uv python install 3.12`. The project targets Python 3.12 (`.python-version`). |
| HTTPS interception | Antivirus software that scans HTTPS breaks certificate checks. Where HTTPS is scanned, use `$env:UV_SYSTEM_CERTS = "1"` for `uv`; it does no harm elsewhere. Docker Hub's CDN downloads failed on such a host, so `docker-compose.yml` pulls the official `postgres` image through `mirror.gcr.io`, Google's public Docker Hub mirror. |
| Dependencies | `python -m uv sync --frozen` (uses the committed `uv.lock`). |
| Database | `copy .env.example .env`, choose a local password, then `docker compose up -d`. PostgreSQL 16 listens on **127.0.0.1:5433** only. The test database `trader_test` is created on first start. |
| Verify | `python -m uv run pytest` (PostgreSQL must be running), then `python -m uv run trader demo`. |

`.env` holds **local database credentials only**. It is gitignored. Broker keys never go in `.env`, in config files, or into chat.

## 2. Paper trading credentials (needed for M4, not yet)

Every collaborator uses their **own** accounts and keys; keys are never shared or committed (see [CONTRIBUTING.md](../CONTRIBUTING.md) §2).

When the paper milestone starts:

1. In the Alpaca dashboard, create or open a **paper** account. Paper accounts are free and separate from any live account. Verify the current UI path yourself, because broker UIs change.
2. Generate a paper API key pair. Do **not** reset an existing paper account for this project without deciding to.
3. Store them in the OS keyring (Windows Credential Manager) with a hidden prompt, then check them:
   ```powershell
   python -m uv run trader secrets set alpaca-paper
   python -m uv run trader secrets status alpaca-paper --verify
   ```
   Or use the **keys inbox** (ADR-0032): run `python -m uv run trader secrets folder` once, copy `TEMPLATE.env.example` there to `<profile>.env` (for example `paper-2.env`), fill it in, then run `python -m uv run trader secrets import`. Each file is stored as a profile and deleted. Switch keys with `trader secrets use alpaca-paper <profile>`; list them with `trader secrets list alpaca-paper`.
   The active pair lands under the service name in `configs/paper.yaml`, `ai-stock-trader/alpaca-paper`, as two entries (usernames `key_id` and `secret_key`). `status` prints presence only; `--verify` calls Alpaca's paper account and IEX data endpoints. A key ID that looks like a live key (`AK…`) is refused. The project never writes the keys to disk and never logs them (ADR-0026). `trader secrets delete alpaca-paper` removes them.
4. Set `TRADER_DATABASE_URL` (see `.env.example`). Paper mode refuses SQLite.
5. Check everything without connecting: `python -m uv run trader paper check`. Then start the bot with `python -m uv run trader paper run` (docs/runbook.md section 9). The paper account must be flat the first time.
6. During market hours, with the bot stopped, run the paper certification once: `python -m uv run trader paper certify` prints the plan; add `--yes` to place at most 6 tiny paper orders (each at most USD 5). Then `python -m uv run trader paper certify-profile --report <report>` proposes `configs/capabilities/alpaca_paper_certified.yaml` for your review (docs/runbook.md section 9.1).

The paper account will probably show about USD 100,000. The bot still trades an internal **USD 50 subledger** and ignores the rest. This is tested: the paper balance is never treated as evidence for a USD 50 strategy.

## 3. Host roles

Set `TRADER_HOST_ROLE` per machine:

| Role | Allowed modes | Use |
| --- | --- | --- |
| `DEV` (default) | replay, shadow, paper | Development machine. Can never run live. |
| `PAPER` | replay, shadow, paper | Dedicated paper host. |
| `LIVE` | live only | A future dedicated live host. It refuses paper and replay configurations. |

## 4. Decisions only the owner can make (needed before G0/G4)

| Decision | Why it matters | When |
| --- | --- | --- |
| Actual residence, citizenship and contracting entity | **Decided 2026-11-09: an EU retail client, trading in USD.** Consequences to confirm per broker: US-listed ETFs are usually not offered to EU retail clients (PRIIPs/KID; markets_and_brokers.md section 7), so ETF research results need a UCITS, stock, CFD or crypto route for real money; Capital.com CFDs fall under ESMA retail leverage limits; Kraken under MiCA; Alpaca live onboarding for the owner's country is unconfirmed. The specs' Germany assumption is superseded. | Before choosing a live broker |
| Starting capital for real money | **Decided 2026-11-09: larger than USD 50.** Superseded the same night: three sleeves of USD 100 (stocks, crypto, gold/CFD commodities), no further deposits, profits compound toward USD 10,000; practice runs exactly these sleeves (ADR-0043, ADR-0044, ADR-0046). | Before G4 |
| Account legal type and the broker's rule regime | Selects `LEGACY_PDT`, `NEW_INTRADAY_MARGIN` or `TRUE_CASH`. `UNVERIFIED` blocks live entries. | Before G0 |
| Funding route and its cost | A USD 50 international wire would cost 100% of capital. | Before any live funding |
| Operating-cost ceiling | The default is zero paid subscriptions (`paid_data_enabled: false`). Consolidated data costs USD 99/month. | Before M4 if IEX-only data proves inadequate |
| Protection policy | The default requires broker-held protection for live use. Synthetic exits need a deliberate, documented alternative. | Before G4 |
| Alert channel | Needed only for unattended live operation. | Before G4 |
| Live enablement | Recorded in `research/promotion_manifest.json` (owner enablement) **and** given as the explicit `--enable-live` startup flag on a `LIVE` host. | Only after G0-G4 pass |

## 5. Market data access (M2; optional, free)

Two datasets in `research/data_manifest.json` are BLOCKED until you act. Neither costs money.

**Stock history (Alpaca).** A free Alpaca account (a paper account is enough) gives API keys that can read historical bars.

1. In the Alpaca dashboard, generate a **paper** API key pair. Never paste the keys into chat, commit them, or put them in `.env`.
2. Preferred: store them once in the OS keyring (section 2, step 3), then run `python -m uv run trader ingest-history --dataset alpaca_us_equities_5m_v1`. The ingestion reads the keyring when no session variables are set.
3. Alternative: in the PowerShell window where you run the project, set them for that session only:
   ```powershell
   $env:APCA_API_KEY_ID = "<your key id>"
   $env:APCA_API_SECRET_KEY = "<your secret>"
   python -m uv run trader ingest-history --dataset alpaca_us_equities_5m_v1
   ```
   Close the window afterwards, or remove the variables: `Remove-Item Env:APCA_API_KEY_ID, Env:APCA_API_SECRET_KEY`.

The ingestion uses the free IEX feed (`feed_param: iex`). Its volume is single-venue; a later change to SIP data would be a new dataset and a new experiment, never a silent switch.

**Venue-matched Kraken history.** Kraken publishes downloadable OHLCVT archives (see CRYPTO_TRADING_TRACK.md, source C4). Download the archive, extract `XBTUSD_15.csv` and `ETHUSD_15.csv` into `data/raw/kraken_ohlcvt/`, then run `python -m uv run trader ingest-history --dataset kraken_ohlcvt_archive_btc_eth_usd_15m_v1`.

**Research budget review.** Decided 2026-11-08: the budget is 15 (recorded on branch `sunshine`). Five trials went to the crypto baseline, five are for the stock baseline, two for crypto model filters (logistic filter on Candidates A and B), and three are kept for stock model filters chosen after the stock results. Any further change is again an owner decision: `python -m uv run trader research decide-budget --new-budget <n> --reason "..."`.

## 6. What the project will never do without you

- Open or reset accounts, accept agreements, or change account settings.
- Deposit, withdraw or transfer money. No adapter has funding operations.
- Buy data or services.
- Enable live trading, or loosen a risk limit after a loss.

## 7. Alpaca MCP server for Claude Code (read-only)

`.mcp.json` registers Alpaca's official MCP server (`alpaca-mcp-server==2.4.0`). It starts through `python -m trader.config.secrets_exec alpaca-paper` (the light form of `trader secrets exec`, ADR-0038), so it uses the active paper profile from the keyring and no key is written into any config file. Claude Code asks once to approve the project server (or add `"enabledMcpjsonServers": ["alpaca"]` to `.claude/settings.local.json`); it is available in sessions started after that.

`.claude/settings.json` denies every tool that places, replaces or cancels orders, closes positions, exercises options, changes account settings or creates locates. Claude can read the account, positions, orders, market data, news and Alpaca's documentation, but cannot trade: the bot is the only thing that places orders, through the risk engine, and outside orders would also break its reconciliation. If you ever want an assistant to trade on paper, use a second paper account that the bot does not use, and change the deny list deliberately.


### 7.1 Research MCP servers (WATER_PLAN Phase 1, ADR-0036)

`.mcp.json` also registers these research servers for Claude Code. They are tools for the assistant; the bot never calls them. Their status and smoke tests are in [integrations/connection_report.md](integrations/connection_report.md).

| Server | What it gives the assistant | Sign-in | Limits |
| --- | --- | --- | --- |
| `coingecko` | Crypto prices, rankings, volume, exchanges (public server) | none | shared fair-use limits |
| `alphavantage` | Earnings, fundamentals, news sentiment, macro and commodities | `ALPHA_VANTAGE_API_KEY=...` in `.env` (Alpha Vantage's official local server, ADR-0039) | 25 requests/day (free) |
| `twelvedata` | Gold, silver and FX context; cross-asset prices | `/mcp` → Twelve Data account | 800 credits/day, 8/minute (free) |
| `massive` | US stock end-of-day data and corporate actions (cross-check for the IEX dataset) | `/mcp` → Massive account | free: end of day, 5 requests/minute |
| `kraken` | Kraken market data and Kraken's **local** paper simulator (no exchange orders) | none; no Kraken key is stored | public API limits |

**Approve and sign in once.** Open a new Claude Code session in the repository and approve the project servers (or list them in `enabledMcpjsonServers` in `.claude/settings.local.json`). Then run `/mcp`, pick `twelvedata` and `massive`, and finish the sign-in page in the browser; the provider keeps your login and Claude Code keeps the token. Alpha Vantage needs no sign-in: put `ALPHA_VANTAGE_API_KEY=...` in the repository's `.env` (gitignored) and `uv run trader secrets status alphavantage` confirms it is found. If the browser page never opens, check that the antivirus does not scan the provider's HTTPS (docs/integrations/connection_report.md).

**Kraken.** Kraken publishes no Windows build, so it is built from the tagged source once: `rustup toolchain install 1.99.1 --profile minimal`, then `cargo +1.99.1 install --locked --git https://github.com/krakenfx/kraken-cli --tag v0.5.1 kraken-cli`. This puts `kraken-mcp.exe` into `%USERPROFILE%\.cargo\bin`. It is started with `-s market,paper` only: no account, trading, funding or `feedback` (upload) tools.

**Capital.com demo (gold and silver CFD research).** Capital.com has no read-only keys, so the server is only ever started through the keyring wrapper, which forces the demo environment and switches trading off whatever else is set. Steps:

1. Create a Capital.com **demo** account and turn on two-factor authentication.
2. In the web app: Settings → API integrations → Generate new key (demo), and set the key's own custom password (not your login password).
3. Put `CAP_API_KEY=...`, `CAP_IDENTIFIER=...` (your login email) and `CAP_API_PASSWORD=...` (the key's own password) in the repository's `.env`, or run `python -m uv run trader secrets set capital-demo` to enter them with hidden input.
4. Tell Claude "capital keys stored". Claude then adds the reviewed, commit-pinned server to `.mcp.json` and smoke-tests it (account must report demo; no order is ever placed).

**Re-authorize or revoke.** If a hosted server stops answering after a while, run `/mcp` and sign in again. To disconnect one: remove it from `enabledMcpjsonServers` (or deny `mcp__<name>` in settings), then revoke the token or key on the provider's site. Capital.com: delete the key in the web app and run `uv run trader secrets delete capital-demo`.
