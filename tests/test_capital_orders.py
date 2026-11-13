"""The practice trader's demo order client (ADR-0056): the broker's recorded answers parse, the
two writes reach only the positions path, an order is sent once, and only the order client
sends one."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.capital_fakes import (
    CLOSE_CONFIRM,
    MARKET,
    OPEN_CONFIRM,
    POSITION,
    Clock,
    FakeBroker,
    orders_for,
)
from trader.data.sources.capital_prices import CAPITAL_DEMO_HOST
from trader.data.sources.http import POSITIONS_PATH, TransportError, UrllibTransport
from trader.practice.capital_orders import (
    OrderError,
    parse_confirmation,
    parse_positions,
    parse_quote,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "trader"
NOW = datetime(2026, 11, 11, 16, 48, tzinfo=UTC)


def test_the_recorded_answers_parse() -> None:
    opened = parse_confirmation(OPEN_CONFIRM)
    assert opened.accepted and opened.deal_id == "00000000-7281-eda8-049d-be3a0055311e"
    assert (opened.level, opened.size) == (Decimal("74183.9"), Decimal("0.00010"))
    closed = parse_confirmation(CLOSE_CONFIRM)
    assert closed.accepted and closed.level == Decimal("74133.9") and closed.deal_id is None
    [position] = parse_positions({"positions": [POSITION]})
    assert position.deal_id == opened.deal_id and position.epic == "BTCUSD"
    assert (position.stop, position.target) == (Decimal("71958.14"), Decimal("75667.33"))
    quote = parse_quote("BTCUSD", MARKET)
    assert quote.tradeable and quote.min_size == Decimal("0.0001")
    assert round(quote.spread_bps, 1) == Decimal("6.7")
    rejected = parse_confirmation(
        {"dealReference": "o_x", "dealStatus": "REJECTED", "reason": "RISK_CHECK"}
    )
    assert not rejected.accepted and rejected.deal_id is None and rejected.reason == "RISK_CHECK"
    with pytest.raises(OrderError):
        parse_positions({"positions": [{"position": {"direction": "UP"}}]})


def test_open_confirm_list_and_close_on_the_fake_broker() -> None:
    broker = FakeBroker(Clock(NOW))
    orders = orders_for(broker)
    assert orders.hedging() is False
    ref = orders.open_position(
        "BTCUSD", "BUY", Decimal("0.0001"), stop=Decimal("72000"), target=Decimal("76000")
    )
    method, url, body = broker.sent[0]
    assert (method, url) == ("POST", CAPITAL_DEMO_HOST + POSITIONS_PATH)
    assert body == {
        "epic": "BTCUSD",
        "direction": "BUY",
        "size": "0.0001",
        "guaranteedStop": False,
        "trailingStop": False,
        "stopLevel": "72000",
        "profitLevel": "76000",
    }
    confirmed = orders.confirm(ref)
    assert confirmed.accepted and confirmed.deal_id is not None
    [held] = orders.positions()
    assert held.deal_id == confirmed.deal_id and held.stop == 72000
    closing = orders.close_position(held.deal_id)
    assert orders.confirm(closing).level == Decimal("74133.65")
    assert orders.positions() == []


def test_an_order_is_sent_once_even_when_its_answer_is_lost() -> None:
    broker = FakeBroker(Clock(NOW))
    orders = orders_for(broker)
    broker.fail_send = True
    with pytest.raises(TransportError, match="network error"):
        orders.open_position(
            "BTCUSD", "SELL", Decimal("0.0001"), stop=Decimal("76000"), target=None
        )
    assert len(broker.sent) == 1


def test_the_transport_writes_only_to_the_positions_path() -> None:
    transport = UrllibTransport()
    for method, url in (
        ("POST", f"{CAPITAL_DEMO_HOST}/api/v1/workingorders"),
        ("PUT", f"{CAPITAL_DEMO_HOST}/api/v1/accounts/preferences"),
        ("POST", f"{CAPITAL_DEMO_HOST}/api/v1/positions/abc"),
        ("DELETE", f"{CAPITAL_DEMO_HOST}/api/v1/positions"),
        ("DELETE", f"{CAPITAL_DEMO_HOST}/api/v1/positions/a/b"),
        ("POST", f"{CAPITAL_DEMO_HOST}/api/v1/positions?x=1"),
        ("POST", "http://demo-api-capital.backend-capital.com/api/v1/positions"),
        ("GET", f"{CAPITAL_DEMO_HOST}/api/v1/positions"),
    ):
        with pytest.raises(ValueError, match="send_json only"):
            transport.send_json(method, url, {})
    assert transport.requests == 0


def test_only_the_order_client_sends_an_order() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ("practice/capital_orders.py", "data/sources/http.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_json"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []
