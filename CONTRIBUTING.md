# Contributing to ai-stock-trader

For anyone who wants to run, study or change this project. It explains how to set up a machine, keep secrets private, propose a change through git, and why the research log has one writer.

Read these first:

- [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md): what every specification file is for, where it lives in code, and milestone status.
- [research/reports/baseline_report.md](research/reports/baseline_report.md): what the first research study found.
- [docs/ml_research_guide.md](docs/ml_research_guide.md): how machine learning and LLM work fits in, and where to start.

The ten specification files (docs/specs/instructions.md, architecture.md, ..., plus the brief in README.md) are **protected**: never edit them. A test checks their SHA-256 hashes. In README.md only the part above the marker line may be changed. The original brief below its marker line is protected like the other specifications.

## 1. Set up a machine (once)

You need Git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker Desktop. uv installs Python 3.12 by itself. The console also needs Node 22.13 or newer.

```bash
git clone https://github.com/Martysunshine/ai-stock-trader.git
cd ai-stock-trader
uv sync --frozen                 # exact locked dependencies
cp .env.example .env             # Windows PowerShell: copy .env.example .env
```

Edit `.env` and choose your own local database password. It appears in three places (`TRADER_PG_PASSWORD` and both database URLs), and they must match. Then:

```bash
docker compose up -d             # PostgreSQL 16 on 127.0.0.1:5433 (local only)
uv run trader ingest-history     # downloads public market data (~5 min) and verifies checksums
uv run pytest                    # full suite, 1,277 tests, several minutes
uv run trader demo               # replay demonstrator: 14 acceptance checks
```

What `ingest-history` should print on a new machine:

- `restored; content identical to the recorded checksums` for the Coinbase and synthetic datasets. Market data is never stored in git; it is re-downloaded and must match the recorded checksums exactly.
- `one-time capture ...` for the Kraken recent dataset. Kraken only serves the last ~7.5 days, so the recorded capture cannot be re-downloaded. Only the optional venue-basis check uses it.
- `BLOCKED` for the Alpaca and Kraken-archive datasets until you supply your own keys or the archive.

Notes:

- Without Docker, run `uv run pytest -m "not pg"`. The PostgreSQL tests fail loudly when the database is missing; they are never silently skipped.
- `ingest-history` is not needed for the demo or the tests.
- On Windows with antivirus HTTPS scanning, set `UV_SYSTEM_CERTS=1` (`$env:UV_SYSTEM_CERTS = "1"` in PowerShell) before `uv` commands.
- After a reboot, start Docker Desktop before running tests.
- The Node launchers (`npm run dev` and the others) start the Python feeds with `python -m uv`. On macOS or Linux, make sure `python` is on the PATH and uv is installed as a module (`python -m pip install uv`).

## 2. Secrets and API keys: everyone uses their own

- **Each person uses their own accounts and keys.** Use paper or data keys only, never live keys. Never share keys over chat or email.
- **Keys never go into git, config files, notebooks, issue or pull-request text, or an AI assistant chat.**
- `.env` stays on your machine (it is gitignored). It holds your local database settings and, since ADR-0039, keys for research MCP servers (`ALPHA_VANTAGE_API_KEY=...`). Broker keys (Alpaca) never go there.
- Keys live in the operating system's keyring (Windows Credential Manager or macOS Keychain; Linux needs a Secret Service keyring). Store your own **paper** keys with a hidden prompt: `uv run trader secrets set alpaca-paper`, check them with `uv run trader secrets status alpaca-paper --verify` (presence only, never the values). The ingestion and the paper adapter read them from there (ADR-0026).
- Environment variables still work for one shell session and take precedence over the keyring:

  | Variable | Used for |
  | --- | --- |
  | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Alpaca stock history (`trader ingest-history --dataset alpaca_us_equities_5m_v1`) |

  Set them in the terminal, run the command, then close the terminal.
- The service name is the `secret_ref` in `configs/paper.yaml` (`ai-stock-trader/alpaca-paper`). Each person connects their own paper account. A live-looking key (`AK…`) is refused.
- To switch keys often, use profiles and the keys inbox outside the repository (`~/.ai-stock-trader/keys`): drop `<profile>.env` there, run `uv run trader secrets import` (the file is deleted after it is stored), then `uv run trader secrets use alpaca-paper <profile>`. Never put Alpaca keys in `.env`.
- Claude Code can use research MCP servers (`.mcp.json`): Alpaca read-only with your own keyring keys, Kraken market data and its local paper simulator, CoinGecko, Alpha Vantage (its official local server, key from `.env`), and Twelve Data and Massive through each provider's own sign-in (`/mcp`). Every tool that could trade, move money or upload data is denied in `.claude/settings.json`, and the bot never uses MCP (ADR-0032, ADR-0036). See docs/owner_setup.md §7 and docs/integrations/connection_report.md.
- Keys for an MCP server that needs them locally go into `.env` under the names in `src/trader/config/secrets.py` (`ALPHA_VANTAGE_API_KEY`; Capital.com demo: `CAP_API_KEY`, `CAP_IDENTIFIER`, `CAP_API_PASSWORD`), or into the keyring with `uv run trader secrets set NAME` (hidden prompt). `.env` wins. `uv run trader secrets status NAME` shows where each value comes from. The launcher passes them to the server and, for Capital.com, forces demo-only, trading-off settings (ADR-0036, ADR-0039).
- If a key is ever committed or pasted somewhere public: **revoke it at the provider immediately**. Removing it from git history is not enough. If this repository is affected, report it privately ([SECURITY.md](SECURITY.md)).
- Manifests, the research log and reports never contain secrets; ingestion code keeps credentials in request headers only, and a test checks that they never reach the manifest.

## 3. Git workflow

- `main` must always pass every check. Fork the repository, work on a branch and open a pull request:
  ```bash
  git switch main && git pull
  git switch -c yourname/label-builder     # <name>/<topic>
  # ... work, commit ...
  git push -u origin yourname/label-builder   # then open a pull request on GitHub
  ```
  The maintainer reviews it. Never force-push `main` or rewrite shared history.
- Keep your branch current with `git pull --rebase origin main`.
- Before every push, run the same checks the project relies on:
  ```bash
  uv run ruff check . && uv run ruff format --check . && uv run mypy
  uv run pytest
  uv run trader docs build --check
  ```
- **Commit messages.** A summary line of at most 72 characters in the imperative ("Add candidate label builder"), a blank line, then a body that says what changed and why. Reference spec sections where relevant, and say how you verified it. Keep one logical change per commit.
- **Never commit:** `data/`, `runs/`, `.env`, keys, account exports, or notebooks with output cells (clear outputs first; notebooks belong under `research/notebooks/`).
- **No GitHub Actions, CI workflows, scheduled jobs or `.github/` configuration** without the owner's explicit agreement: they consume paid minutes. Git LFS also needs agreement.
- New dependencies: add them with `uv add <package>`, commit `pyproject.toml` and `uv.lock` together, and record version, licence and purpose in [docs/dependency_evaluation.md](docs/dependency_evaluation.md).

## 4. The research log: append-only, one writer

`research/experiment_registry.jsonl` records every registered trial, result, note and owner decision. Each line contains the hash of the previous line, so **git cannot merge two people's additions**: a merged file fails verification, and every research command then refuses to run.

**The research log has one writer, the owner.** In a fork or a pull request, do not run `trader research register`, `baseline`, `models`, `note`, `decide-budget` or `unlock-holdout`; build and test on synthetic data, and propose real-data trial configurations for the owner to register.

Rules:

1. Commands that write to the log run **only on an up-to-date `main`, by the owner**, and the log and any regenerated report are committed immediately afterwards.
2. Never edit the file by hand, and never hand-resolve a merge conflict in it. If it conflicts, take `main`'s exact version (`git fetch && git checkout origin/main -- research/experiment_registry.jsonl`), then finish the merge or rebase.
3. **The trial budget is project-wide:** ten configurations before a review of the evidence (risk_and_validation.md §5); the owner raised it step by step to 26 by 2026-11-11, and 25 are used (the table of every trial is in [docs/strategy_playbook.md](docs/strategy_playbook.md) section 2; the free slot, T-0020, is reserved for weekly stock momentum). Every evaluated variant counts: model, feature set, threshold, symbol choice. The owner records any budget change with `trader research decide-budget`.
4. **Register before you run anything on real data.** Exploration on synthetic data, or in a notebook, is fine but is not evidence. If it influenced a design, say so in the pull request.
5. Each study's final test period is locked. Do not look at it; opening it is an owner decision that is recorded permanently.
6. **Market datasets are versioned by name.** If you acquire a new version, add a new manifest entry with a new name; never overwrite a dataset that trials are registered against.

## 5. Current focus

The roadmap, the release gates and the open steps are in [docs/project_status.md](docs/project_status.md). What is traded on paper today, and the evidence for each rule, is in [docs/strategy_playbook.md](docs/strategy_playbook.md).

## 6. Things nobody does without an explicit, recorded owner decision

- Edit a protected specification file.
- Enable live trading, connect a live account, or move money.
- Loosen a risk limit or the policy file to make a backtest look better.
- Let a model size orders, change limits, or reach a broker. Tests enforce this boundary.
- Put LLM, HTTP or broker SDKs into the trading runtime's dependencies. Tests enforce this too; research-only tools belong in a separate dependency group.
