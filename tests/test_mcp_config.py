"""Research MCP servers for the assistant (ADR-0032, ADR-0036, ADR-0044): reviewed and pinned.

The bot never calls MCP. These servers are tools for Claude Code; anything that could move money,
upload data or reach a live account is either not exposed (Kraken runs with ``-s market,paper``) or
denied in ``.claude/settings.json``. The one order path is the owner-approved Capital.com DEMO
practice (ADR-0044): forced to demo by the launcher and guarded before every order preview.
Statuses and smoke tests: docs/integrations/connection_report.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT / ".mcp.json").read_text(encoding="utf-8")
SERVERS: dict[str, dict[str, object]] = json.loads(RAW)["mcpServers"]
DENY: list[str] = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))[
    "permissions"
]["deny"]

#: Official hosted endpoints, exactly. OAuth happens on the provider's page; no key in a URL.
HOSTED = {
    "coingecko": "https://mcp.api.coingecko.com/mcp",
    "twelvedata": "https://mcp.twelvedata.com/mcp",
    "massive": "https://mcp.massive.com",  # the resource id in its OAuth metadata (no trailing slash)
}
#: alphavantage runs Alpha Vantage's official LOCAL server with the owner's key from .env (ADR-0039).
LOCAL = {"alpaca", "kraken", "alphavantage"}
#: Added only after the owner stores Capital.com demo keys (ADR-0036).
LATER = {"capital"}

#: kraken-cli 0.5.1 with ``-s market,paper``: public market data, the LOCAL paper simulator and its
#: strategy lab. Probed from the built binary on 2026-11-09 (28 tools).
KRAKEN_ALLOWED = frozenset(
    (
        "kraken_assets",
        "kraken_explain_pnl",
        "kraken_lab_compare",
        "kraken_lab_curve",
        "kraken_lab_new",
        "kraken_lab_next",
        "kraken_lab_score",
        "kraken_lab_show",
        "kraken_ohlc",
        "kraken_orderbook",
        "kraken_orderbook_grouped",
        "kraken_pairs",
        "kraken_paper_balance",
        "kraken_paper_buy",
        "kraken_paper_cancel",
        "kraken_paper_cancel_all",
        "kraken_paper_history",
        "kraken_paper_init",
        "kraken_paper_orders",
        "kraken_paper_reset",
        "kraken_paper_sell",
        "kraken_paper_status",
        "kraken_server_time",
        "kraken_spreads",
        "kraken_status",
        "kraken_tape_list",
        "kraken_ticker",
        "kraken_trades",
    )
)
#: Tools that would trade, move money, change the account or upload data if Kraken keys or other
#: service groups were ever added. Each must stay denied.
KRAKEN_MUST_DENY = (
    "kraken_order_buy",
    "kraken_order_sell",
    "kraken_order_batch",
    "kraken_order_cancel_all",
    "kraken_withdraw",
    "kraken_wallet_transfer",
    "kraken_subaccount_transfer",
    "kraken_earn_allocate",
    "kraken_futures_order_buy",
    "kraken_futures_order_sell",
    "kraken_futures_batch_order",
    "kraken_futures_transfer",
    "kraken_futures_wallet_transfer",
    "kraken_futures_set_leverage",
    "kraken_auth_show",
    "kraken_export_delete",
    "kraken_feedback",
    "kraken_workspace_promote",
)
#: capital-mcp 0.5.0 (commit 3207be4) tools that change account settings, top up the demo balance
#: or write watchlists: still denied after ADR-0044.
CAPITAL_MUST_DENY = (
    "cap_account_preferences_set",
    "cap_account_demo_topup",
    "cap_watchlists_create",
    "cap_watchlists_add_market",
    "cap_watchlists_delete",
    "cap_watchlists_remove_market",
)


def test_only_reviewed_servers_are_configured() -> None:
    names = set(SERVERS)
    assert LOCAL | set(HOSTED) <= names
    assert names <= LOCAL | set(HOSTED) | LATER


def test_hosted_servers_are_the_official_endpoints_with_nothing_inline() -> None:
    for name, url in HOSTED.items():
        assert SERVERS[name] == {"type": "http", "url": url}, name  # no headers, no env
        parts = urlsplit(url)
        assert parts.scheme == "https" and not parts.query and not parts.username, name
    assert not re.search(r"api_?key\s*[=:]", RAW, re.IGNORECASE)
    assert "authorization" not in RAW.lower() and "bearer" not in RAW.lower()


def test_kraken_exposes_only_market_data_and_the_local_paper_simulator() -> None:
    server = SERVERS["kraken"]
    assert server["type"] == "stdio" and server["command"] == "kraken-mcp"
    assert server["args"] == ["-s", "market,paper"]  # no feedback upload, account, trade or funding
    assert "env" not in server  # no Kraken key anywhere


def test_alphavantage_runs_the_pinned_official_server_with_the_key_from_dotenv() -> None:
    """marketdata-mcp-server 0.3.1 uses the mcp 1.x server API, so mcp stays below 2."""
    server = SERVERS["alphavantage"]
    assert server["type"] == "stdio" and server["command"] == "python"
    args = [str(a) for a in server["args"]]  # type: ignore[union-attr]
    assert args[args.index("trader.config.secrets_exec") + 1] == "alphavantage"  # ADR-0038/0039
    tool = args[args.index("--") + 1 :]
    assert tool == [
        "uvx",
        "--quiet",
        "--python",
        "3.13",
        "--from",
        "marketdata-mcp-server==0.3.1",
        "--with",
        "mcp>=1.12.3,<2",
        "marketdata-mcp",
    ]
    env = server.get("env", {})
    assert isinstance(env, dict) and "ALPHA_VANTAGE_API_KEY" not in env  # the launcher adds it


def test_claude_cannot_reach_kraken_tools_outside_market_and_paper() -> None:
    kraken_denied = {d.removeprefix("mcp__kraken__") for d in DENY if d.startswith("mcp__kraken__")}
    assert [t for t in KRAKEN_MUST_DENY if t not in kraken_denied] == []
    assert kraken_denied.isdisjoint(KRAKEN_ALLOWED)  # the research tools stay usable
    assert len(kraken_denied) >= 124  # every tool `kraken-mcp -s all` adds (0.5.1)


#: The DEMO order tools the owner allowed for practice (ADR-0044); every new order goes through a
#: preview, which the PreToolUse guard checks against the practice rules and the journal.
CAPITAL_DEMO_TRADE_TOOLS = (
    "cap_trade_execute_position",
    "cap_trade_execute_working_order",
    "cap_trade_execute_working_order_update",
    "cap_trade_positions_close",
    "cap_trade_orders_cancel",
)
SETTINGS = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_claude_cannot_change_the_capital_account_or_watchlists() -> None:
    missing = [t for t in CAPITAL_MUST_DENY if f"mcp__capital__{t}" not in DENY]
    assert missing == []


def test_capital_demo_orders_are_allowed_only_behind_the_practice_guard() -> None:
    assert not [t for t in CAPITAL_DEMO_TRADE_TOOLS if f"mcp__capital__{t}" in DENY]
    hooks = SETTINGS["hooks"]["PreToolUse"]
    guard = [
        h for h in hooks if re.fullmatch(h["matcher"], "mcp__capital__cap_trade_preview_position")
    ]
    assert len(guard) == 1
    for tool in ("cap_trade_preview_working_order", "cap_trade_preview_working_order_update"):
        assert re.fullmatch(guard[0]["matcher"], f"mcp__capital__{tool}")
    for tool in ("kraken_paper_buy", "kraken_paper_sell"):  # the crypto sleeve (ADR-0046)
        assert re.fullmatch(guard[0]["matcher"], f"mcp__kraken__{tool}")
    assert not re.fullmatch(guard[0]["matcher"], "mcp__kraken__kraken_ticker")
    assert [c["command"] for c in guard[0]["hooks"]] == [
        "python -m uv run --no-sync --quiet python -m trader.practice.capital_guard"
    ]
    # the Alpaca paper account is the bot's: no MCP order path to it, ever
    alpaca_orders = (
        "place_stock_order",
        "place_crypto_order",
        "close_position",
        "cancel_order_by_id",
    )
    assert all(f"mcp__alpaca__{t}" in DENY for t in alpaca_orders)


def test_capital_when_configured_is_keyring_wrapped_and_commit_pinned() -> None:
    server = SERVERS.get("capital")
    if server is None:  # not added until the owner stores demo keys
        return
    args = [str(a) for a in server["args"]]  # type: ignore[union-attr]
    # `trader secrets exec NAME` or the light `python -m trader.config.secrets_exec NAME` (ADR-0038)
    launcher = "trader.config.secrets_exec" if "trader.config.secrets_exec" in args else "exec"
    assert args[args.index(launcher) + 1] == "capital-demo"  # forced demo, trading off (ADR-0036)
    assert any(re.search(r"capital-mcp@[0-9a-f]{40}$", a) for a in args), args
    env = server.get("env", {})
    assert isinstance(env, dict) and not any(k.upper().startswith("CAP_") for k in env)
