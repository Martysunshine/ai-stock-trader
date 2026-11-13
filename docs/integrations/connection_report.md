# MCP connection report

Research MCP servers for Claude Code, as of 2026-11-09 (WATER_PLAN §6.5, ADR-0032, ADR-0036). The bot never calls any of them; they are assistant tools. Six servers answer reads (Alpaca, CoinGecko, Kraken, Alpha Vantage, Twelve Data, Massive). Capital.com is configured (demo, trading forced off), but Capital.com refuses the login with the values in `.env` (401) until the owner corrects them. All six start in every new Claude Code tab (the owner's choice on 2026-11-09, despite the RAM cost of the two stdio chains).

Status labels: `planned` (chosen, not configured), `configured` (in `.mcp.json`), `authenticated` (sign-in done), `read_verified` (one harmless read returned valid data). These are reporting labels, not capability evidence for the bot.

## Servers

| Server | Status | Source and version | Transport | Auth | Environment | Tools (count, sha256/16 of sorted names) | Limits |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `alpaca` | `read_verified` | `alpaca-mcp-server==2.4.0` (ADR-0032) | stdio via `python -m trader.config.secrets_exec alpaca-paper` (the light form of `trader secrets exec`, ADR-0038) | paper keys from the keyring | Alpaca paper | 12 state-changing tools denied | Alpaca API limits |
| `coingecko` | `read_verified` | Hosted, server `coingecko_coingecko_typescript_api` 8.3.0 | HTTP `https://mcp.api.coingecko.com/mcp` | none (public beta) | Public research | 2, `e5f272218c841d5b` (`execute`, `search_docs`; both marked read-only) | Shared fair-use limits, not published |
| `kraken` | `read_verified` | kraken-cli v0.5.1 (tag commit 671cdd7), built with Rust 1.99.1 | stdio `kraken-mcp -s market,paper` | none; no Kraken key stored | Public market data + **local** paper simulator | 28, `71ea914f534f43c1`; the 124 others from `-s all` are denied | Kraken public API limits |
| `alphavantage` | `read_verified` | marketdata-mcp-server 0.3.1 (official, local) with `mcp<2` | stdio via `python -m trader.config.secrets_exec alphavantage` | `ALPHA_VANTAGE_API_KEY` from `.env` (ADR-0039) | Read-only data | 3 meta-tools: `TOOL_LIST`, `TOOL_GET`, `TOOL_CALL` | 25 requests/day (free) |
| `twelvedata` | `read_verified` | Hosted (official) | HTTP `https://mcp.twelvedata.com/mcp` | OAuth (Twelve Data account); the API key in a header is refused | Read-only data | includes `get_quote` | 800 credits/day, 8/minute (free) |
| `massive` | `read_verified` | Hosted (official) | HTTP `https://mcp.massive.com` | OAuth (Massive account) | Read-only data | after sign-in | Free: end of day, 5 requests/minute, 2 years |
| `capital` | `configured` | capital-mcp 0.5.0, commit 3207be4 (reviewed; `uvx --from git+https://github.com/capital-com-sv/capital-mcp@3207be41…6e50`, Python 3.13) | stdio via `python -m trader.config.secrets_exec capital-demo` | `CAP_API_KEY`, `CAP_IDENTIFIER`, `CAP_API_PASSWORD` from `.env` (ADR-0039) | Capital.com **demo** (`demo-api-capital.backend-capital.com`), trading forced off | 38; the 11 that trade or change the account or watchlists are denied | Demo account |

## Evidence log

Newest first. Times are UTC. Every check was a read; no order was placed anywhere.

| Time | Server | Check | Result |
| --- | --- | --- | --- |
| 2026-11-09 14:37 | capital | `cap_session_status`, then `cap_session_login` (3 attempts in total, then stopped) | Status: `env` demo, demo base URL, logged out. Login: Capital.com answered 401 (`SessionError`), so one of the three `.env` values is wrong; the next call got 429. The owner was asked to check the key's own password, the login email and that the key is enabled |
| 2026-11-09 14:36 | capital | `initialize` + `tools/list` through the wrapper, after the owner approved the server | Capital.com MCP Server 0.5.0, 38 tools, first start 16.4 s; all 11 write tools are in the deny list |
| 2026-11-09 13:58 | twelvedata | `get_quote` AAPL from a headless Claude Code run limited to this server | 342.37 (close 2026-11-06, market closed): the same as Alpha Vantage and Massive |
| 2026-11-09 13:57 | twelvedata | Browser sign-in started by `claude mcp login twelvedata` in a visible terminal (it needs a TTY); `claude mcp get` reports Connected | Authenticated. Before that, `initialize` with `Authorization: apikey <key>` or `Bearer <key>` got 401, and Python's default User-Agent got Cloudflare error 1010 |
| 2026-11-09 10:30 | massive | `call_api` `/v2/aggs/ticker/AAPL/prev` (after the owner's browser sign-in) | AAPL close 342.37, volume 44,703,806: the same as Alpha Vantage's quote |
| 2026-11-09 01:45 | alphavantage | local server, `TOOL_CALL` `GLOBAL_QUOTE` AAPL | price 342.37, latest day 2026-11-06; 2.0 s call. 0.3.1 crashes on mcp 2.x (`Server.list_tools` removed), so mcp is pinned below 2 |
| 2026-11-09 01:25 | alphavantage, twelvedata, massive | OAuth discovery from Claude Code | Failed with `InvalidHTTPResponse` until the owner excluded the providers' hosts from the antivirus HTTPS scanner, which re-sent the metadata compressed without a `Content-Encoding` header |
| 2026-11-09 00:25 | coingecko | `execute`: `client.simple.price.get({ids: 'bitcoin', vs_currencies: 'usd'})` | BTC USD 74,954; 1.0 s call, 2.9 s session |
| 2026-11-09 00:25 | kraken | `kraken_ticker` XBTUSD | last 74,913.1, bid 74,913.0, ask 74,913.1; 16.5 s (first public call through the host's HTTPS scanner) |
| 2026-11-09 00:25 | kraken | `kraken_paper_status` | "Workspace 'global' not found": the local simulator has no paper workspace yet (expected; `kraken_paper_init` creates one locally) |
| 2026-11-09 00:20 | kraken | `tools/list` with `-s market,paper` and with `-s all` | 28 and 152 tools; the 124-tool difference is denied in `.claude/settings.json` |
| 2026-11-09 00:10 | coingecko | `initialize` + `tools/list` | protocol 2025-06-18, 2 tools |
| 2026-11-09 00:06 | alpaca | `get_clock` | market closed; next open 2026-11-09 09:30 ET |
| 2026-11-08 23:59 | massive | `initialize` without a token | 401 with OAuth resource metadata at `/` (`/mcp` is 404); the metadata names the resource `https://mcp.massive.com` (no trailing slash), which `.mcp.json` uses |
| 2026-11-08 23:58 | alphavantage | `initialize` without a token | 401 `Bearer`, resource metadata `/.well-known/oauth-protected-resource/mcp` |
| 2026-11-08 23:58 | twelvedata | `initialize` without a token | No answer within 15 s. The URL is the one in Twelve Data's official README; re-check after sign-in |

The 2026-11-08 Alpaca connection evidence (paper account ACTIVE) is in the owner setup sheet history; `trader secrets status alpaca-paper --verify` repeats it at any time.

## Not connected, by decision

| Provider | Why not now | Revisit when |
| --- | --- | --- |
| FMP | Free tier lacks the earnings calendars and transcripts that would justify it; its key goes in a header, which would need a third-party proxy to stay out of files | A concrete fundamentals gap and a data budget exist |
| IBKR | Account eligibility unverified; autonomous MCP needs ten USD 100+ trades in review mode, far above the USD 50 experiment | Broader market access is a requirement |
| Coinbase | OAuth into a trading account; Kraken covers crypto research | Kraken cannot serve a needed pair or fee level |
| cTrader, IG | Only needed if Capital.com falls short; IG's read-only MCP is documented for Australia only | Capital.com demo research fails a requirement |

## Revoke or disable

- **One server:** remove it from `enabledMcpjsonServers` in `.claude/settings.local.json`, or add `mcp__<name>` to `permissions.deny`.
- **Hosted sign-ins:** revoke the token on the provider's site, then `/mcp` shows the server as unauthenticated.
- **Capital.com:** delete the API key in the Capital.com web app and run `uv run trader secrets delete capital-demo`.
- **Kraken:** `cargo uninstall kraken-cli` removes the binaries; nothing else is stored except a local paper workspace if one was created.

Owner steps and re-authorization: docs/owner_setup.md §7.1.
