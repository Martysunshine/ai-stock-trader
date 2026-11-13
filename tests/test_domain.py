"""Domain contracts reject malformed data (architecture.md §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.domain.hashing import canonical_json, content_hash
from trader.domain.market import Bar, Quote
from trader.domain.orders import (
    BrokerOrderEvent,
    BrokerOrderStatus,
    OrderIntent,
    OrderPurpose,
    OrderType,
    TimeInForce,
)
from trader.domain.types import (
    Side,
    Track,
    canon_decimal,
    round_down_to_step,
    round_up_to_step,
    to_decimal,
)

T0 = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)


def test_binary_floats_nan_and_infinity_are_rejected() -> None:
    with pytest.raises(ValueError, match="binary floating point"):
        to_decimal(0.1)
    for bad in ("NaN", "Infinity", "-inf"):
        with pytest.raises(ValueError):
            to_decimal(bad)
    with pytest.raises(ValueError):
        Quote(
            instrument_id="XNYS:SYNA",
            feed="F",
            event_time=T0,
            receipt_time=T0,
            sequence=1,
            bid=100.0,
            ask=Decimal("100.1"),
            bid_size=Decimal(1),
            ask_size=Decimal(1),
        )  # type: ignore[arg-type]


def test_naive_datetimes_negative_prices_and_crossed_quotes_are_rejected() -> None:
    kwargs = dict(
        instrument_id="XNYS:SYNA", feed="F", sequence=1, bid_size=Decimal(1), ask_size=Decimal(1)
    )
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 - deliberately naive input
    with pytest.raises(ValueError, match="naive"):
        Quote(
            event_time=naive,
            receipt_time=T0,
            bid=Decimal(1),
            ask=Decimal(2),
            **kwargs,
        )
    with pytest.raises(ValueError):
        Quote(event_time=T0, receipt_time=T0, bid=Decimal(-1), ask=Decimal(2), **kwargs)
    with pytest.raises(ValueError, match="crossed"):
        Quote(event_time=T0, receipt_time=T0, bid=Decimal(3), ask=Decimal(2), **kwargs)
    with pytest.raises(ValueError, match="receipt_time"):
        Quote(
            event_time=T0,
            receipt_time=T0 - timedelta(seconds=1),
            bid=Decimal(1),
            ask=Decimal(2),
            **kwargs,
        )


def test_decimals_serialise_as_strings_and_hash_canonically() -> None:
    q = Quote(
        instrument_id="XNYS:SYNA",
        feed="F",
        event_time=T0,
        receipt_time=T0,
        sequence=1,
        bid=Decimal("100.10"),
        ask=Decimal("100.2"),
        bid_size=Decimal(5),
        ask_size=Decimal(5),
    )
    assert '"bid":"100.1"' in q.model_dump_json()
    assert canonical_json({"x": Decimal("10.000000")}) == canonical_json({"x": Decimal("1E+1")})
    assert content_hash({"a": Decimal("0.50")}) == content_hash({"a": Decimal("0.5")})
    assert canon_decimal(Decimal("-0.000")) == "0"


def test_rounding_helpers_round_quantity_down_and_fees_up() -> None:
    assert round_down_to_step(Decimal("0.0987264299"), Decimal("0.000000001")) == Decimal(
        "0.098726429"
    )
    assert round_up_to_step(Decimal("0.0000278"), Decimal("0.01")) == Decimal("0.01")
    assert round_down_to_step(Decimal("7"), Decimal("5")) == Decimal("5")


def test_contradictory_broker_statuses_are_rejected() -> None:
    base = dict(
        broker="replay",
        account_id="a",
        event_id="e1",
        broker_order_id="b",
        client_order_id="co-1234",
        order_quantity=Decimal(1),
        event_time=T0,
        broker_version=1,
    )
    with pytest.raises(ValueError, match="FILLED"):
        BrokerOrderEvent(
            status=BrokerOrderStatus.FILLED, cum_filled_quantity=Decimal("0.5"), **base
        )
    with pytest.raises(ValueError, match="exceeds"):
        BrokerOrderEvent(
            status=BrokerOrderStatus.PARTIALLY_FILLED, cum_filled_quantity=Decimal(2), **base
        )
    with pytest.raises(ValueError, match="REJECTED"):
        BrokerOrderEvent(
            status=BrokerOrderStatus.REJECTED, cum_filled_quantity=Decimal("0.1"), **base
        )


def test_v1_intents_are_long_only() -> None:
    common = dict(
        intent_id="i",
        client_order_id="co-1234",
        portfolio_id="p",
        account_id="a",
        instrument_id="XNYS:SYNA",
        track=Track.STOCKS,
        quantity=Decimal(1),
        reason="r",
        created_at=T0,
        time_in_force=TimeInForce.DAY,
    )
    with pytest.raises(ValueError, match="long-only"):
        OrderIntent(
            side=Side.SELL,
            purpose=OrderPurpose.ENTRY,
            order_type=OrderType.LIMIT,
            limit_price=Decimal(1),
            **common,
        )
    with pytest.raises(ValueError, match="exits are sells"):
        OrderIntent(
            side=Side.BUY,
            purpose=OrderPurpose.EXIT_STOP,
            order_type=OrderType.MARKET,
            limit_price=None,
            **common,
        )


def test_bar_is_unusable_before_its_availability_time() -> None:
    bar = Bar(
        instrument_id="XNYS:SYNA",
        feed="F",
        minutes=5,
        start=T0,
        end=T0 + timedelta(minutes=5),
        available_time=T0 + timedelta(minutes=5, seconds=1),
        complete=True,
        open=Decimal(1),
        high=Decimal(2),
        low=Decimal(1),
        close=Decimal(2),
        volume=Decimal(1),
        trade_count=1,
        vwap=Decimal("1.5"),
    )
    assert not bar.is_usable_at(T0 + timedelta(minutes=5))
    assert bar.is_usable_at(T0 + timedelta(minutes=5, seconds=1))
