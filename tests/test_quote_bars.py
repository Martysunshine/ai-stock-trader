"""Two-sided Capital.com datasets (ADR-0052): ingest stores a bid file and an ask file per epic
with the same bars, the quality report says what the quotes are worth, and the loader joins them
back. No test touches the network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.data.history import DataQualityError, HistoryBar
from trader.data.ingest import IngestContext, ingest, verify_dataset, verify_or_restore
from trader.data.manifest import HistoryDataset
from trader.data.quote_bars import QuoteBar, fold_weekend_stub, load_quote_bars
from trader.data.sources.capital_prices import MAX_BARS, QuotePage

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)  # a Monday


def _bar(start: datetime, o: str, h: str, low: str, c: str, minutes: int = 60) -> HistoryBar:
    values = (Decimal(o), Decimal(h), Decimal(low), Decimal(c))
    return HistoryBar(start, minutes, *values, volume=Decimal(11))


class Quotes:
    """Hourly quotes on weekdays from ``START``: bid 100 + n, ask half a point above, except
    one bar with no spread and one whose bid side is not a valid bar."""

    def __init__(self, hours: int = 72) -> None:
        self.rows: list[tuple[HistoryBar, HistoryBar]] = []
        for n in range(hours):
            start = START + timedelta(hours=n)
            price = Decimal(100 + n)
            spread = Decimal(0) if n == 3 else Decimal("0.5")
            bid = _bar(start, f"{price}", f"{price + 1}", f"{price - 1}", f"{price}")
            ask = _bar(
                start,
                f"{price + spread}",
                f"{price + 1 + spread}",
                f"{price - 1 + spread}",
                f"{price + spread}",
            )
            if n == 5:  # a low above the open: the pair is excluded on both sides
                bid = _bar(start, f"{price}", f"{price + 1}", f"{price + 1}", f"{price}")
            self.rows.append((bid, ask))
        self.calls = 0

    def fetch_quote_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> QuotePage:
        self.calls += 1
        held = [r for r in self.rows if to is None or r[0].start <= to][-max_bars:]
        return QuotePage(bid=tuple(r[0] for r in held), ask=tuple(r[1] for r in held))


def _dataset(**request: Any) -> HistoryDataset:
    base = {
        "instruments": [
            {"instrument_id": "CAPITAL:GOLD.BID", "source_symbol": "GOLD"},
            {"instrument_id": "CAPITAL:GOLD.ASK", "source_symbol": "GOLD"},
        ],
        "bar_minutes": 60,
        "start": START,
        "end": START + timedelta(hours=72),
        "calendar": "BROKER_CFD",
    }
    return HistoryDataset.model_validate(
        {
            "name": "capital_demo_test_1h_v1",
            "kind": "HISTORICAL_BARS",
            "source": "capital_demo_prices",
            "venue": "CAPITAL_COM_DEMO",
            "feed": "CAPITAL_DEMO_PRICES",
            "entitlement": "test",
            "request": {**base, **request},
            "path": "data/history/capital_demo_test_1h_v1",
            "availability_rule": "test",
            "corrections": "test",
            "adjustment_basis": "RAW",
            "status": "REQUESTED",
        }
    )


def _ctx(tmp_path: Path, capital: Any) -> IngestContext:
    return IngestContext(repo_root=tmp_path, transport=None, now=NOW, capital=capital)  # type: ignore[arg-type]


def test_each_epic_is_stored_as_a_bid_file_and_an_ask_file_with_the_same_bars(
    tmp_path: Path,
) -> None:
    acquired = ingest(_dataset(), _ctx(tmp_path, Quotes()))
    assert acquired.status == "ACQUIRED" and verify_dataset(acquired, tmp_path) == []
    bid, ask = acquired.files
    assert (bid.instrument_id, ask.instrument_id) == ("CAPITAL:GOLD.BID", "CAPITAL:GOLD.ASK")
    assert bid.rows == ask.rows == 71  # 72 hours less the pair with an invalid bid bar
    assert bid.first_start == ask.first_start == START and bid.content_sha256 != ask.content_sha256
    quotes = bid.quality["quotes"]
    assert quotes == ask.quality["quotes"]
    assert (
        quotes["walk"]["stopped"] == "reached_start" and quotes["rows_excluded_invalid_ohlc"] == 1
    )
    year = quotes["spread_by_year"]["2026"]
    assert year["rows"] == 71 and year["rows_bid_equals_ask"] == 1 and year["median_bps"] > 0
    assert quotes["bars_per_weekday"] == {
        "mon": 23,
        "tue": 24,
        "wed": 24,
        "thu": 0,
        "fri": 0,
        "sat": 0,
        "sun": 0,
    }
    assert quotes["days_without_a_bar"] == 0 and "tick count" in quotes["volume"]
    assert bid.quality["expected_intervals"] == 0  # no grid for a broker's own hours

    bars = load_quote_bars(acquired, "CAPITAL:GOLD", tmp_path)
    assert len(bars) == 71 and bars[0].start == START and bars[0].end == START + timedelta(hours=1)
    assert bars[0].ask.close - bars[0].bid.close == Decimal("0.5")
    assert bars[0].mid_close == Decimal("100.25")
    assert round(bars[0].spread_bps_at_close, 2) == Decimal("49.88")
    assert bars[3].spread_bps_at_close == 0
    window = load_quote_bars(
        acquired,
        "CAPITAL:GOLD",
        tmp_path,
        start=START + timedelta(hours=10),
        end=START + timedelta(hours=12),
    )
    assert [b.start.hour for b in window] == [10, 11]


def test_a_fresh_checkout_restores_the_same_content_and_no_keys_means_blocked(
    tmp_path: Path,
) -> None:
    acquired = ingest(_dataset(), _ctx(tmp_path, Quotes()))
    for record in acquired.files:
        (tmp_path / acquired.path / record.file).unlink()
    assert verify_or_restore(acquired, _ctx(tmp_path, Quotes())).status == "RESTORED"
    changed = Quotes()
    changed.rows[10] = (
        changed.rows[10][0],
        _bar(changed.rows[10][1].start, "111", "112", "110", "111"),
    )
    for record in acquired.files:
        (tmp_path / acquired.path / record.file).unlink()
    assert verify_or_restore(acquired, _ctx(tmp_path, changed)).status == "MISMATCH"

    blocked = ingest(_dataset(), _ctx(tmp_path, None))
    assert blocked.status == "BLOCKED" and "CAP_API_KEY" in blocked.blocker
    assert "demo" in blocked.blocker.lower()


@pytest.mark.parametrize(
    ("instruments", "message"),
    [
        ([{"instrument_id": "CAPITAL:GOLD", "source_symbol": "GOLD"}], "must end in .BID or .ASK"),
        (
            [{"instrument_id": "CAPITAL:GOLD.BID", "source_symbol": "GOLD"}],
            "needs one .BID and one .ASK",
        ),
    ],
)
def test_a_request_names_both_sides_of_every_epic(
    tmp_path: Path, instruments: list[dict[str, str]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ingest(_dataset(instruments=instruments), _ctx(tmp_path, Quotes()))


def test_sides_that_do_not_line_up_are_refused(tmp_path: Path) -> None:
    with pytest.raises(DataQualityError, match="same bar"):
        QuoteBar(
            _bar(START, "1", "1", "1", "1"), _bar(START + timedelta(hours=1), "1", "1", "1", "1")
        )


def test_the_sunday_evening_bar_opens_mondays_session() -> None:
    def day(d: int, o: str, h: str, low: str, c: str) -> QuoteBar:
        start = datetime(2026, 11, d, tzinfo=UTC)
        ask = _bar(start, o, h, low, c, minutes=1440)
        bid = _bar(start, *(str(Decimal(v) - 1) for v in (o, h, low, c)), minutes=1440)
        return QuoteBar(bid, ask)

    # Fri 6, Sun 8 (the evening open), Mon 9, Tue 10, then a Sunday with nothing after it
    bars = [
        day(6, "7400", "7420", "7390", "7410"),
        day(8, "7430", "7435", "7425", "7432"),
        day(9, "7432", "7480", "7428", "7470"),
        day(10, "7470", "7500", "7460", "7490"),
        day(15, "7510", "7512", "7505", "7508"),
    ]
    folded = fold_weekend_stub(bars)
    assert [b.start.day for b in folded] == [6, 8, 10]
    monday = folded[1]
    assert monday.minutes == 2880 and monday.end == datetime(2026, 11, 10, tzinfo=UTC)
    assert (monday.ask.open, monday.ask.high, monday.ask.low, monday.ask.close) == (
        Decimal("7430"),
        Decimal("7480"),
        Decimal("7425"),
        Decimal("7470"),
    )
    assert monday.bid.open == Decimal("7429") and monday.bid.volume == 22
    assert fold_weekend_stub(bars[:1]) == bars[:1]


def test_daily_bars_are_built_from_hours_with_the_weekend_joined_to_monday() -> None:
    from trader.data.quote_bars import daily_from_hourly

    def hour(day: int, h: int, price: str) -> QuoteBar:
        start = datetime(2026, 11, day, h, tzinfo=UTC)
        return QuoteBar(
            _bar(start, price, price, price, price), _bar(start, price, price, price, price)
        )

    hours = [
        hour(6, 0, "10"),  # Friday
        hour(6, 21, "11"),
        hour(8, 23, "12"),  # Sunday evening: the start of Monday's session
        hour(9, 0, "13"),  # Monday
        hour(9, 23, "9"),
        hour(10, 5, "14"),  # Tuesday
    ]
    closed = daily_from_hourly(hours, weekend_closed=True)
    assert [b.start for b in closed] == [hours[0].start, hours[2].start, hours[5].start]
    monday = closed[1]
    assert monday.end == datetime(2026, 11, 10, tzinfo=UTC) and monday.minutes == 25 * 60
    assert (monday.bid.open, monday.bid.high, monday.bid.low, monday.bid.close) == (
        Decimal(12),
        Decimal(13),
        Decimal(9),
        Decimal(9),
    )
    assert monday.ask.volume == 33  # three hours of ticks
    every_day = daily_from_hourly(hours, weekend_closed=False)
    assert [b.start.day for b in every_day] == [6, 8, 9, 10]
