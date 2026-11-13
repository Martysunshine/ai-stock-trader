"""The PreToolUse guard in front of the CFD and crypto practice orders (ADR-0044, ADR-0046,
ADR-0049)."""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trader.practice import capital_guard
from trader.practice import journal as journal_mod
from trader.practice.capital_guard import Blocked, check, check_kraken, route
from trader.practice.journal import PracticeJournal

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
PREVIEW = "mcp__capital__cap_trade_preview_position"
BUY, SELL = "mcp__kraken__kraken_paper_buy", "mcp__kraken__kraken_paper_sell"
PLAN = {
    "trade_id": "G1",
    "epic": "GOLD",
    "direction": "BUY",
    "size": "0.01",
    "entry": "4100",
    "stop": "4020",
    "target": "4260",
    "thesis": "daily close above the 20-day high after a pullback",
    "setup": "breakout",
    "timeframe": "1d",
    "invalidation": "close back below 4050",
}
BTC_PLAN = {
    **PLAN,
    "trade_id": "B1",
    "epic": "BTCUSD",
    "size": "0.0005",
    "entry": "74000",
    "stop": "72500",
    "target": "77000",
}


def _order(**kw: Any) -> dict[str, Any]:
    return {"epic": "GOLD", "direction": "BUY", "size": 0.01, "stop_level": 4020, **kw}


@pytest.fixture
def journal(tmp_path: Path) -> PracticeJournal:
    j = PracticeJournal(tmp_path / "journal.jsonl")
    j.append("PLAN", PLAN, NOW - timedelta(minutes=5))
    return j


@pytest.fixture
def crypto(tmp_path: Path) -> PracticeJournal:
    j = PracticeJournal(tmp_path / "crypto.jsonl", venue="crypto-cfd")
    j.append("PLAN", BTC_PLAN, NOW - timedelta(minutes=5))
    return j


def test_a_planned_order_with_its_stop_passes(journal: PracticeJournal) -> None:
    check(PREVIEW, _order(), journal, NOW)
    check(PREVIEW, _order(stop_level=None, stop_distance=80), journal, NOW)
    working = "mcp__capital__cap_trade_preview_working_order"
    check(working, _order(type="LIMIT", level=4090), journal, NOW)


@pytest.mark.parametrize(
    ("order", "message"),
    [
        (_order(epic="BTCUSD"), "not a practice epic"),
        (_order(size=0.5), "outside the practice range"),
        (_order(stop_level=None), "needs a stop"),
        (_order(direction="SELL"), "direction differs"),
        (_order(size=0.02), "size 0.02 differs"),
        (_order(stop_level=4000), "differs from PLAN G1 stop"),
        (_order(epic="SILVER", size=1), "no PLAN for SILVER"),
    ],
)
def test_orders_outside_the_plan_are_blocked(
    journal: PracticeJournal, order: dict[str, Any], message: str
) -> None:
    with pytest.raises(Blocked, match=message):
        check(PREVIEW, order, journal, NOW)


def test_a_stale_or_used_plan_does_not_cover_an_order(journal: PracticeJournal) -> None:
    with pytest.raises(Blocked, match="no PLAN for GOLD"):
        check(PREVIEW, _order(), journal, NOW + timedelta(minutes=30))
    opened = {"trade_id": "G1", "deal_id": "D1", "fill_level": "4101", "size": "0.01"}
    journal.append("OPEN", opened, NOW)
    with pytest.raises(Blocked, match="no PLAN for GOLD"):
        check(PREVIEW, _order(), journal, NOW)


def test_an_order_update_needs_a_recent_amend(journal: PracticeJournal) -> None:
    update = "mcp__capital__cap_trade_preview_working_order_update"
    with pytest.raises(Blocked, match="AMEND"):
        check(update, {"deal_id": "D1", "stop_level": 4030}, journal, NOW)
    opened = {"trade_id": "G1", "deal_id": "D1", "fill_level": "4101", "size": "0.01"}
    journal.append("OPEN", opened, NOW)
    amend = {
        "trade_id": "G1",
        "deal_id": "D1",
        "changes": {"stop": "4060"},
        "reason": "trail the stop",
    }
    journal.append("AMEND", amend, NOW)
    check(update, {"deal_id": "D1", "stop_level": 4060}, journal, NOW)


def test_other_tools_pass(journal: PracticeJournal, crypto: PracticeJournal) -> None:
    check("mcp__capital__cap_market_prices", {"epic": "GOLD"}, journal, NOW)
    route("mcp__capital__cap_market_get", {"epic": "BTCUSD"}, [journal, crypto], NOW)
    check_kraken("mcp__kraken__kraken_ticker")


def test_an_order_is_checked_against_the_sleeve_that_owns_its_epic(
    journal: PracticeJournal, crypto: PracticeJournal
) -> None:
    """ADR-0049: Bitcoin and Ether are Capital.com demo CFDs of the crypto sleeve, with its own
    journal and limits."""
    both = [journal, crypto]
    btc = {"epic": "BTCUSD", "direction": "BUY", "size": 0.0005, "stop_level": 72500}
    route(PREVIEW, btc, both, NOW)
    route(PREVIEW, _order(), both, NOW)
    for order, message in (
        ({**btc, "size": 0.001}, "differs from PLAN B1"),
        ({**btc, "size": 0.01}, "outside the practice range"),
        ({**btc, "epic": "ETHUSD", "size": 0.01}, "no PLAN for ETHUSD"),
        ({**btc, "epic": "DOGEUSD"}, "DOGEUSD is not a practice epic"),
    ):
        with pytest.raises(Blocked, match=message):
            route(PREVIEW, order, both, NOW)
    # each sleeve counts its own open trades: a full CFD sleeve does not block a crypto order
    for n in (2, 3):
        journal.append("PLAN", {**PLAN, "trade_id": f"G{n}"}, NOW)
        opened = {"trade_id": f"G{n}", "deal_id": f"D{n}", "fill_level": "4100", "size": "0.01"}
        journal.append("OPEN", opened, NOW)
    with pytest.raises(Blocked, match="2 practice trades are already open"):
        route(PREVIEW, _order(), both, NOW)
    route(PREVIEW, btc, both, NOW)
    update = "mcp__capital__cap_trade_preview_working_order_update"
    with pytest.raises(Blocked, match="AMEND"):
        route(update, {"deal_id": "D2", "stop_level": 4050}, both, NOW)
    amend = {"trade_id": "G2", "deal_id": "D2", "changes": {"stop": "4050"}, "reason": "trail it"}
    journal.append("AMEND", amend, NOW)
    route(update, {"deal_id": "D2", "stop_level": 4050}, both, NOW)


def test_a_second_trade_in_a_group_is_blocked(journal: PracticeJournal) -> None:
    """Both index plans were made while neither was open; once one is, the other's order stops."""
    us100 = {
        **PLAN,
        "trade_id": "N1",
        "epic": "US100",
        "size": "0.001",
        "entry": "27000",
        "stop": "26500",
        "target": "27800",
    }
    de40 = {**us100, "trade_id": "D1", "epic": "DE40", "fx_to_usd": "1.12"}
    journal.append("PLAN", us100, NOW - timedelta(minutes=5))
    journal.append("PLAN", de40, NOW - timedelta(minutes=5))
    order = {"epic": "DE40", "direction": "BUY", "size": 0.001, "stop_level": 26500}
    check(PREVIEW, order, journal, NOW)
    opened = {"trade_id": "N1", "deal_id": "D-N1", "fill_level": "27000", "size": "0.001"}
    journal.append("OPEN", opened, NOW)
    with pytest.raises(Blocked, match="group indices with the open trade N1"):
        check(PREVIEW, order, journal, NOW)


def test_the_kraken_simulator_takes_no_more_orders() -> None:
    for tool in (BUY, SELL):
        with pytest.raises(Blocked, match="Kraken simulator is retired"):
            check_kraken(tool)


def test_the_hook_entry_blocks_with_exit_2_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_mod, "REPO_ROOT", tmp_path)  # empty journals: nothing planned
    err = io.StringIO()
    event = {"tool_name": PREVIEW, "tool_input": _order()}
    assert capital_guard.main(io.StringIO(json.dumps(event)), err) == 2
    assert "practice guard blocked the order: no PLAN for GOLD" in err.getvalue()
    kraken = io.StringIO()
    buy = {"tool_name": BUY, "tool_input": {"pair": "BTCUSD", "volume": "0.0001"}}
    assert capital_guard.main(io.StringIO(json.dumps(buy)), kraken) == 2
    assert "Kraken simulator is retired" in kraken.getvalue()
    crypto = io.StringIO()
    btc = {"epic": "BTCUSD", "direction": "SELL", "size": 0.0005, "stop_level": 75500}
    preview = {"tool_name": PREVIEW, "tool_input": btc}
    assert capital_guard.main(io.StringIO(json.dumps(preview)), crypto) == 2
    assert "no PLAN for BTCUSD" in crypto.getvalue()
    ok = io.StringIO(
        json.dumps({"tool_name": "mcp__capital__cap_session_status", "tool_input": {}})
    )
    assert capital_guard.main(ok, io.StringIO()) == 0
    broken = io.StringIO()
    assert capital_guard.main(io.StringIO("not json"), broken) == 2
    assert "failed closed" in broken.getvalue()


def test_no_live_capital_host_in_the_source_tree() -> None:
    live = re.compile(r"(?<!demo-)api-capital\.backend-capital\.com")
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
        if live.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
