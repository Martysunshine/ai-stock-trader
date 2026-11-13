"""Deep two-sided Capital.com DEMO history for research datasets (ADR-0052): the backward walk
over 1000-bar pages, and how it ends. No test touches the network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.data.history import HistoryBar
from trader.data.sources.capital_history import fetch_capital_quote_history
from trader.data.sources.capital_prices import MAX_BARS, QuotePage, parse_quote_bars
from trader.data.sources.http import TransportError, parse_json

from .test_capital_prices import PRICES, price_row

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def _bar(start: datetime, price: str, minutes: int = 60) -> HistoryBar:
    p = Decimal(price)
    return HistoryBar(
        start=start, minutes=minutes, open=p, high=p, low=p, close=p, volume=Decimal(7)
    )


class FakeQuotes:
    """Holds hourly bars from ``T0`` and serves them as Capital.com does: the newest
    ``max_bars`` up to ``to``. ``fail_at`` raises that HTTP status on the n-th request."""

    def __init__(self, hours: int, *, fail_at: dict[int, int] | None = None) -> None:
        self.starts = [T0 + timedelta(hours=h) for h in range(hours)]
        self.calls: list[datetime | None] = []
        self.fail_at = fail_at or {}
        self.one_sided = 0

    def fetch_quote_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> QuotePage:
        self.calls.append(to)
        status = self.fail_at.get(len(self.calls))
        if status is not None:
            raise TransportError(f"HTTP {status}: error.prices.not-found", status=status)
        held = [s for s in self.starts if to is None or s <= to][-max_bars:]
        return QuotePage(
            bid=tuple(_bar(s, "100") for s in held),
            ask=tuple(_bar(s, "100.5") for s in held),
            one_sided=self.one_sided,
        )


def test_the_walk_pages_backwards_until_the_requested_start() -> None:
    source = FakeQuotes(2500)
    start, end = T0 + timedelta(hours=100), T0 + timedelta(hours=2400)
    got = fetch_capital_quote_history(source, "US100", minutes=60, start=start, end=end)
    assert got.stopped == "reached_start" and got.pages == 3
    assert len(got.bid) == len(got.ask) == 2300
    assert got.bid[0].start == start and got.bid[-1].start == end - timedelta(hours=1)
    assert all(
        b.start == a.start and a.close - b.close == Decimal("0.5")
        for b, a in zip(got.bid, got.ask, strict=True)
    )
    # each page ends one second before the oldest bar of the page above it
    assert source.calls[0] == end - timedelta(seconds=1)
    assert source.calls[1] == T0 + timedelta(hours=1400) - timedelta(seconds=1)
    assert got.report()["first_start"] == "2026-01-09T04:00:00Z"


@pytest.mark.parametrize(
    ("hours", "fail_at", "pages"),
    [
        (1500, None, 3),  # the third page is the second one again: nothing older exists
        (1000, {2: 404}, 1),  # the source's own answer for a range it does not hold
    ],
)
def test_the_walk_stops_where_the_source_ends(
    hours: int, fail_at: dict[int, int] | None, pages: int
) -> None:
    source = FakeQuotes(hours, fail_at=fail_at)
    got = fetch_capital_quote_history(
        source, "GOLD", minutes=60, start=T0 - timedelta(days=400), end=T0 + timedelta(hours=hours)
    )
    assert got.stopped == "source_exhausted" and got.pages == pages
    assert len(got.bid) == hours and got.bid[0].start == T0


def test_an_empty_first_page_and_the_page_cap_are_reported() -> None:
    empty = fetch_capital_quote_history(
        FakeQuotes(0), "GOLD", minutes=60, start=T0, end=T0 + timedelta(days=1)
    )
    assert empty.stopped == "source_exhausted" and empty.bid == () and empty.report()["rows"] == 0
    capped = fetch_capital_quote_history(
        FakeQuotes(5000),
        "GOLD",
        minutes=60,
        start=T0,
        end=T0 + timedelta(hours=5000),
        max_pages=2,
    )
    assert capped.stopped == "page_cap" and capped.pages == 2 and len(capped.bid) == 2000


def test_errors_other_than_the_end_of_history_are_not_swallowed() -> None:
    with pytest.raises(TransportError, match="404"):  # a 404 before any page: a wrong request
        fetch_capital_quote_history(
            FakeQuotes(10, fail_at={1: 404}),
            "NOPE",
            minutes=60,
            start=T0,
            end=T0 + timedelta(hours=10),
        )
    with pytest.raises(TransportError, match="500"):
        fetch_capital_quote_history(
            FakeQuotes(2500, fail_at={2: 500}),
            "GOLD",
            minutes=60,
            start=T0,
            end=T0 + timedelta(hours=2500),
        )
    with pytest.raises(ValueError, match="ends before it starts"):
        fetch_capital_quote_history(FakeQuotes(1), "GOLD", minutes=60, start=T0, end=T0)


def test_skipped_rows_are_counted_across_pages() -> None:
    source = FakeQuotes(1200)
    source.one_sided = 2
    got = fetch_capital_quote_history(
        source, "GOLD", minutes=60, start=T0, end=T0 + timedelta(hours=1200)
    )
    assert got.pages == 2 and got.one_sided == 4
    assert got.report()["rows_skipped_one_sided"] == 4


def test_quote_bars_keep_each_side_and_the_tick_count() -> None:
    page = parse_quote_bars(PRICES, minutes=5)
    assert len(page.bid) == len(page.ask) == 2 and page.one_sided == 1 and page.crossed == 0
    bid, ask = page.bid[0], page.ask[0]
    assert bid.start == ask.start == datetime(2026, 11, 9, 17, 50, tzinfo=UTC)
    assert (bid.open, bid.high, bid.low, bid.close) == (
        Decimal("4103.33"),
        Decimal("4103.56"),
        Decimal("4100.65"),
        Decimal("4101.81"),
    )
    assert (ask.open, ask.close) == (Decimal("4103.83"), Decimal("4102.31"))
    assert bid.volume == ask.volume == 923  # the source's tick count, on both sides
    assert not bid.ohlc_problems() and not ask.ohlc_problems()
    crossed = parse_json(
        '{"prices": ['
        + price_row(
            "2026-11-09T17:50:00",
            ("4103.33", "4103.83"),
            ("4103.56", "4104.06"),
            ("4100.65", "4100.15"),  # the low's ask is below its bid
            ("4101.81", "4102.31"),
        )
        + "]}"
    )
    assert parse_quote_bars(crossed, minutes=5) == QuotePage(bid=(), ask=(), crossed=1)
