"""Daily SIP bars for the ETF universe (ADR "Daily SIP bars for ETFs"; data/daily_bars.py).

Every test uses a fake transport and ``tmp_path``: no network, and the real manifest is only
read. The fake rows follow the shape Alpaca returned to read-only probes on 2026-11-09: one row
per session stamped 00:00 New York (05:00Z in winter, 04:00Z in summer).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trader.data.calendars import ExchangeCalendar
from trader.data.daily_bars import (
    DAILY_FINALIZATION,
    classify_session_bar,
    expected_session_opens,
    session_of_source_day,
    session_ranges,
    session_slot,
    source_day_stamp,
)
from trader.data.history import (
    DataQualityError,
    HistoryBar,
    content_sha256,
    read_bars,
    to_domain_bars,
    write_bars,
)
from trader.data.ingest import (
    IngestContext,
    ingest,
    load_bars,
    split_events,
    verify_dataset,
    verify_or_restore,
)
from trader.data.manifest import HistoryDataset, HistoryRequest, load_manifest, save_manifest
from trader.data.quality import clean_and_check
from trader.data.sources.alpaca_history import (
    AlpacaCredentials,
    fetch_alpaca_bars,
    fetch_alpaca_daily_bars,
)
from trader.domain.types import iso_utc

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "research" / "data_manifest.json"
NAME = "alpaca_us_etfs_daily_sip_v1"
CAL = ExchangeCalendar("XNYS")
CREDS = AlpacaCredentials("key-id-123", "very-secret-456")
LATER = datetime(2026, 11, 9, tzinfo=UTC)

UNIVERSE = {
    "SPY": "ARCX",
    "QQQ": "XNAS",
    "IWM": "ARCX",
    "DIA": "ARCX",
    "XLB": "ARCX",
    "XLE": "ARCX",
    "XLF": "ARCX",
    "XLI": "ARCX",
    "XLK": "ARCX",
    "XLP": "ARCX",
    "XLU": "ARCX",
    "XLV": "ARCX",
    "XLY": "ARCX",
    "TLT": "XNAS",
    "IEF": "XNAS",
    "GLD": "ARCX",
}


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _row(stamp: str, close: str = "100.00", **kw: Any) -> dict[str, Any]:
    c = Decimal(close)
    row: dict[str, Any] = {
        "t": stamp,
        "o": c - Decimal("0.5"),
        "h": c + 1,
        "l": c - 1,
        "c": c,
        "v": 1000,
        "n": 10,
        "vw": c,
    }
    row.update(kw)
    return row


class FakeAlpaca:
    """Serves daily rows by symbol in two pages, and empty corporate actions."""

    def __init__(
        self,
        rows: Mapping[str, list[dict[str, Any]]],
        actions: dict[str, Any] | None = None,
    ) -> None:
        self.rows = rows
        self.actions = actions or {}
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        p = dict(params or {})
        self.calls.append((url, p, dict(headers or {})))
        if url.endswith("/v2/stocks/bars"):
            symbols = p["symbols"].split(",")
            if "page_token" not in p:  # first page: the first symbol only
                return {
                    "bars": {symbols[0]: self.rows.get(symbols[0], [])},
                    "next_page_token": "p2",
                }
            return {
                "bars": {s: self.rows.get(s, []) for s in symbols[1:]},
                "next_page_token": None,
            }
        if url.endswith("/v1/corporate-actions"):
            return {"corporate_actions": self.actions, "next_page_token": None}
        raise AssertionError(f"unexpected request {url}")


def _entry() -> HistoryDataset:
    return load_manifest(MANIFEST).dataset(NAME)


def _small(symbols: tuple[str, ...], start: datetime, end: datetime) -> HistoryDataset:
    """The registered entry narrowed to a few symbols and days, written under a test path."""
    ds = _entry()
    instruments = tuple(i for i in ds.request.instruments if i.source_symbol in symbols)
    return ds.model_copy(
        update={
            "path": "data/history/test_daily",
            "request": ds.request.model_copy(
                update={"instruments": instruments, "start": start, "end": end}
            ),
        }
    )


# ----------------------------------------------------------------------------- session mapping


def test_source_day_stamps_map_to_sessions_across_a_clock_change() -> None:
    # Friday 2025-03-07 in EST, Monday 2025-03-10 in EDT (clocks changed on Sunday 03-09).
    friday = _utc(2025, 3, 7, 5)
    monday = _utc(2025, 3, 10, 4)
    assert source_day_stamp(date(2025, 3, 7)) == friday
    assert source_day_stamp(date(2025, 3, 10)) == monday
    assert session_slot(CAL, friday) == (_utc(2025, 3, 7, 14, 30), 390)
    assert session_slot(CAL, monday) == (_utc(2025, 3, 10, 13, 30), 390)
    # 04:00Z on the Friday is 23:00 New York on Thursday: not a source-day stamp.
    assert session_of_source_day(CAL, _utc(2025, 3, 7, 4)) is None
    # Saturday midnight labels no session.
    assert session_of_source_day(CAL, _utc(2025, 3, 8, 5)) is None


def test_an_early_close_is_a_shorter_bar_ending_at_the_early_close() -> None:
    start, minutes = session_slot(CAL, _utc(2025, 11, 28, 5))  # day after Thanksgiving
    assert (start, minutes) == (_utc(2025, 11, 28, 14, 30), 210)
    bar = HistoryBar(
        start=start,
        minutes=minutes,
        open=Decimal(680),
        high=Decimal(684),
        low=Decimal(680),
        close=Decimal(683),
        volume=Decimal(1),
    )
    assert bar.end == _utc(2025, 11, 28, 18)  # 13:00 New York
    assert classify_session_bar(bar, CAL) == "OK"
    (domain,) = to_domain_bars(
        [bar],
        instrument_id="ARCX:SPY",
        feed="ALPACA_SIP",
        finalization_delay=timedelta(seconds=1),
        require_source_vwap=False,
    )
    assert (domain.start, domain.end) == (_utc(2025, 11, 28, 14, 30), _utc(2025, 11, 28, 18))
    assert domain.available_time == _utc(2025, 11, 28, 18, 0, 1)
    assert domain.minutes == 210


def test_expected_sessions_skip_weekends_and_holidays_and_respect_the_range() -> None:
    opens, by_day = expected_session_opens(CAL, _utc(2025, 11, 25), _utc(2025, 12, 2))
    days = [CAL.session_date_of(o).isoformat() for o in opens]
    assert days == ["2025-11-25", "2025-11-26", "2025-11-28", "2025-12-01"]  # 27th Thanksgiving
    assert by_day["2025-11-28"] == [_utc(2025, 11, 28, 14, 30)]
    # A start after the open drops that session; an end at an open excludes it.
    opens, _ = expected_session_opens(CAL, _utc(2025, 11, 25, 15), _utc(2025, 12, 1, 14, 30))
    assert [CAL.session_date_of(o).isoformat() for o in opens] == ["2025-11-26", "2025-11-28"]


def test_fetch_requests_daily_sip_raw_bars_and_maps_each_row_to_its_session() -> None:
    rows = {
        "SPY": [
            _row("2025-03-07T05:00:00Z"),
            _row("2025-03-10T04:00:00Z"),
            _row("2025-11-28T05:00:00Z"),
        ],
        "GLD": [_row("2025-03-07T05:00:00Z")],
    }
    transport = FakeAlpaca(rows)
    out = fetch_alpaca_daily_bars(
        transport,
        CREDS,
        ["SPY", "GLD"],
        start=_utc(2025, 3, 7),
        end=_utc(2025, 11, 29),
        feed="sip",
        calendar=CAL,
    )
    url, params, headers = transport.calls[0]
    assert url == "https://data.alpaca.markets/v2/stocks/bars"
    assert params["timeframe"] == "1Day"
    assert params["feed"] == "sip" and params["adjustment"] == "raw"
    # Alpaca's end is inclusive: the request spans the first to the last session's stamp.
    assert params["start"] == "2025-03-07T05:00:00Z"
    assert params["end"] == "2025-11-28T05:00:00Z"
    assert headers["APCA-API-SECRET-KEY"] == "very-secret-456"
    assert len(transport.calls) == 2 and transport.calls[1][1]["page_token"] == "p2"
    spy = out["SPY"]
    assert [(b.start, b.end, b.minutes) for b in spy] == [
        (_utc(2025, 3, 7, 14, 30), _utc(2025, 3, 7, 21), 390),
        (_utc(2025, 3, 10, 13, 30), _utc(2025, 3, 10, 20), 390),
        (_utc(2025, 11, 28, 14, 30), _utc(2025, 11, 28, 18), 210),
    ]
    assert spy[0].close == Decimal("100.00") and spy[0].vwap == Decimal("100.00")
    assert spy[0].trade_count == 10
    assert [b.start for b in out["GLD"]] == [_utc(2025, 3, 7, 14, 30)]


def test_unmappable_rows_keep_their_stamp_and_are_counted_not_redated() -> None:
    transport = FakeAlpaca(
        {"SPY": [_row("2025-03-07T04:00:00Z"), _row("2025-03-08T05:00:00Z")], "QQQ": []}
    )
    out = fetch_alpaca_daily_bars(
        transport,
        CREDS,
        ["SPY", "QQQ"],
        start=_utc(2025, 3, 3),
        end=_utc(2025, 3, 10),
        feed="sip",
        calendar=CAL,
    )
    opens, sessions = expected_session_opens(CAL, _utc(2025, 3, 3), _utc(2025, 3, 10))
    bars, report = clean_and_check(
        out["SPY"],
        minutes=None,
        retrieved_at=LATER,
        expected=opens,
        sessions=sessions,
        gap_semantics="test",
        finalization_delay=DAILY_FINALIZATION,
        session_calendar=CAL,
    )
    assert bars == []
    assert report.misaligned_rows_excluded == 1  # 23:00 New York, not a day stamp
    assert report.outside_session_excluded == 1  # a Saturday
    assert report.missing_intervals == 5
    assert report.missing_ranges == ("2025-03-03T14:30:00Z +5 sessions",)


def test_the_five_minute_request_is_unchanged() -> None:
    transport = FakeAlpaca({"AAPL": []})
    fetch_alpaca_bars(
        transport,
        CREDS,
        ["AAPL"],
        minutes=5,
        start=_utc(2026, 11, 2, 14, 30),
        end=_utc(2026, 11, 2, 15, 30),
        feed="iex",
    )
    assert transport.calls[0][1] == {
        "symbols": "AAPL",
        "timeframe": "5Min",
        "start": "2026-11-02T14:30:00Z",
        "end": "2026-11-02T15:30:00Z",
        "limit": "10000",
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    }


# ----------------------------------------------------------------------------- quality and storage


def test_quality_reports_missing_sessions_and_never_fills_them() -> None:
    opens, sessions = expected_session_opens(CAL, _utc(2025, 11, 24), _utc(2025, 12, 3))
    slots = [session_slot(CAL, source_day_stamp(CAL.session_date_of(o))) for o in opens]
    raw = [
        HistoryBar(
            start=s,
            minutes=m,
            open=Decimal(10),
            high=Decimal(11),
            low=Decimal(9),
            close=Decimal(10),
            volume=Decimal(5),
        )
        for s, m in slots
        if CAL.session_date_of(s) not in (date(2025, 11, 26), date(2025, 11, 28))
    ]
    bars, report = clean_and_check(
        raw,
        minutes=None,
        retrieved_at=LATER,
        expected=opens,
        sessions=sessions,
        gap_semantics="test",
        finalization_delay=DAILY_FINALIZATION,
        session_calendar=CAL,
    )
    assert len(bars) == len(opens) - 2
    assert report.expected_intervals == 6
    assert report.missing_sessions == ("2025-11-26", "2025-11-28")
    # 26th and 28th are consecutive sessions (Thanksgiving in between): one run of two.
    assert report.missing_ranges == ("2025-11-26T14:30:00Z +2 sessions",)
    assert report.as_dict()["coverage"] == "0.666667"


def test_a_daily_bar_is_unfinished_until_after_hours_trading_has_ended() -> None:
    opens, sessions = expected_session_opens(CAL, _utc(2025, 3, 10), _utc(2025, 3, 11))
    start, minutes = session_slot(CAL, _utc(2025, 3, 10, 4))
    raw = [
        HistoryBar(
            start=start,
            minutes=minutes,
            open=Decimal(10),
            high=Decimal(11),
            low=Decimal(9),
            close=Decimal(10),
            volume=Decimal(5),
        )
    ]
    close = _utc(2025, 3, 10, 20)

    def check(retrieved: datetime) -> int:
        bars, _ = clean_and_check(
            raw,
            minutes=None,
            retrieved_at=retrieved,
            expected=opens,
            sessions=sessions,
            gap_semantics="test",
            finalization_delay=DAILY_FINALIZATION,
            session_calendar=CAL,
        )
        return len(bars)

    assert check(close + timedelta(hours=2)) == 0  # after-hours volume still accruing
    assert check(close + timedelta(hours=4, seconds=1)) == 1


def test_clean_and_check_needs_exactly_one_interval_definition() -> None:
    with pytest.raises(ValueError, match="session_calendar"):
        clean_and_check([], minutes=None, retrieved_at=LATER, expected=[], gap_semantics="t")
    with pytest.raises(ValueError, match="session_calendar"):
        clean_and_check(
            [],
            minutes=5,
            retrieved_at=LATER,
            expected=[],
            gap_semantics="t",
            session_calendar=CAL,
        )


def test_daily_storage_round_trip_takes_lengths_from_the_calendar(tmp_path: Path) -> None:
    stamps = [_utc(2025, 11, 26, 5), _utc(2025, 11, 28, 5), _utc(2025, 12, 1, 5)]
    bars = []
    for i, stamp in enumerate(stamps):
        start, minutes = session_slot(CAL, stamp)
        c = Decimal("680.25") + i
        bars.append(
            HistoryBar(
                start=start,
                minutes=minutes,
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=Decimal(49212030),
                vwap=c,
                trade_count=568720,
            )
        )
    path = tmp_path / "ARCX__SPY.parquet"
    _, content = write_bars(path, bars, session_calendar=CAL)
    back = read_bars(path, minutes=None, session_calendar=CAL)
    assert back == bars
    assert [b.minutes for b in back] == [390, 210, 390]
    assert content_sha256(back) == content
    with pytest.raises(ValueError, match="session_calendar"):
        read_bars(path, minutes=None)
    with pytest.raises(ValueError, match="session_calendar"):
        read_bars(path, minutes=390, session_calendar=CAL)


def test_storage_refuses_bars_that_are_not_exactly_one_session(tmp_path: Path) -> None:
    start, _ = session_slot(CAL, _utc(2025, 11, 28, 5))
    wrong_length = HistoryBar(
        start=start,
        minutes=390,  # the 28th closed at 13:00
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
    )
    with pytest.raises(DataQualityError, match="not one regular session"):
        write_bars(tmp_path / "x.parquet", [wrong_length], session_calendar=CAL)
    # A file whose stored start is not a session open cannot be read as daily bars.
    intraday = HistoryBar(
        start=start + timedelta(minutes=5),
        minutes=5,
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
    )
    write_bars(tmp_path / "y.parquet", [intraday])
    with pytest.raises(DataQualityError, match="not a session open"):
        read_bars(tmp_path / "y.parquet", minutes=None, session_calendar=CAL)


def test_session_ranges_group_runs_of_the_expected_order() -> None:
    expected = [_utc(2025, 1, d, 14, 30) for d in (2, 3, 6, 7, 8)]
    missing = [expected[0], expected[1], expected[3]]
    assert session_ranges(missing, expected) == [
        "2025-01-02T14:30:00Z +2 sessions",
        "2025-01-07T14:30:00Z +1 sessions",
    ]
    assert session_ranges([], expected) == []


# ----------------------------------------------------------------------------- ingestion


def test_ingest_stores_one_bar_per_session_and_reports_the_missing_one(tmp_path: Path) -> None:
    ds = _small(("SPY", "TLT"), _utc(2025, 11, 24), _utc(2025, 12, 3))
    spy = [
        _row(iso_utc(source_day_stamp(d)), f"{670 + i}.5")
        for i, d in enumerate(
            date(2025, 11, x) if x > 3 else date(2025, 12, x) for x in (24, 25, 26, 28, 1, 2)
        )
    ]
    tlt = [r for r in spy if not r["t"].startswith("2025-11-28")]  # TLT misses the early close
    transport = FakeAlpaca(
        {"SPY": spy, "TLT": tlt},
        actions={
            "forward_splits": [
                {"symbol": "TLT", "new_rate": 2, "old_rate": 1, "ex_date": "2025-12-01"}
            ]
        },
    )
    out = ingest(
        ds, IngestContext(repo_root=tmp_path, transport=transport, now=LATER, alpaca=CREDS)
    )
    assert out.status == "ACQUIRED" and out.blocker == ""
    by_id = {f.instrument_id: f for f in out.files}
    assert set(by_id) == {"ARCX:SPY", "XNAS:TLT"}
    assert by_id["ARCX:SPY"].rows == 6 and by_id["ARCX:SPY"].quality["missing_intervals"] == 0
    tlt_q = by_id["XNAS:TLT"].quality
    assert tlt_q["missing_sessions"] == ["2025-11-28"]
    assert tlt_q["missing_ranges_first"] == ["2025-11-28T14:30:00Z +1 sessions"]
    assert "never filled" in tlt_q["gap_semantics"]
    assert by_id["XNAS:TLT"].file == "XNAS__TLT.parquet"
    # Corporate actions as for 5-minute data, keyed by the manifest instrument id.
    (split,) = split_events(out)
    assert split.instrument_id == "XNAS:TLT" and split.ex_date == date(2025, 12, 1)
    # Loading verifies the checksum and restores each session's own length.
    loaded = load_bars(out, "ARCX:SPY", tmp_path)
    assert [(b.start, b.end) for b in loaded][3] == (
        _utc(2025, 11, 28, 14, 30),
        _utc(2025, 11, 28, 18),
    )
    assert load_bars(out, "XNAS:TLT", tmp_path, start=_utc(2025, 11, 28))[0].start == _utc(
        2025, 12, 1, 14, 30
    )
    assert verify_dataset(out, tmp_path) == []
    manifest_text = out.model_dump_json()
    assert "very-secret-456" not in manifest_text and "key-id-123" not in manifest_text


def test_a_fresh_checkout_restores_the_daily_files_bit_for_bit(tmp_path: Path) -> None:
    ds = _small(("GLD",), _utc(2025, 3, 3), _utc(2025, 3, 15))
    opens, _ = expected_session_opens(CAL, ds.request.start, ds.request.end)
    rows = {"GLD": [_row(iso_utc(source_day_stamp(CAL.session_date_of(o)))) for o in opens]}
    ctx = IngestContext(repo_root=tmp_path, transport=FakeAlpaca(rows), now=LATER, alpaca=CREDS)
    acquired = ingest(ds, ctx)
    assert acquired.files[0].rows == 10  # two weeks across the March clock change
    (tmp_path / acquired.path / acquired.files[0].file).unlink()
    outcome = verify_or_restore(acquired, ctx)
    assert outcome.status == "RESTORED" and not outcome.failed
    assert verify_or_restore(acquired, ctx).status == "OK"


def test_without_keys_the_daily_dataset_is_blocked_with_the_owner_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    transport = FakeAlpaca({})
    out = ingest(_entry(), IngestContext(repo_root=tmp_path, transport=transport, now=LATER))
    assert out.status == "BLOCKED" and "owner action" in out.blocker
    assert transport.calls == []


# ----------------------------------------------------------------------------- manifest


def test_the_committed_manifest_round_trips_byte_for_byte(tmp_path: Path) -> None:
    out = tmp_path / "data_manifest.json"
    save_manifest(out, load_manifest(MANIFEST))
    assert out.read_bytes() == MANIFEST.read_bytes().replace(b"\r\n", b"\n")


def test_existing_entries_are_unchanged_and_carry_no_timeframe() -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    existing = [e for e in raw["history"] if e["name"] != NAME]
    # Six earlier datasets plus coinbase_btc_eth_usd_daily_v1 (ADR-0042), stored as 1440-minute
    # UTC_24_7 bars without a timeframe label, and the four two-sided Capital.com datasets
    # (ADR-0052), fixed-interval bars as well.
    assert len(existing) == 11
    assert sum(e["source"] == "capital_demo_prices" for e in existing) == 4
    for entry in existing:
        assert "timeframe" not in entry["request"], entry["name"]
        model = HistoryDataset.model_validate(entry)
        assert not model.request.is_daily
        assert model.model_dump(mode="json") == entry, entry["name"]


def test_the_daily_entry_matches_the_shared_contract() -> None:
    ds = _entry()
    req = ds.request
    assert (ds.source, ds.feed, ds.kind) == ("alpaca_stock_bars", "ALPACA_SIP", "HISTORICAL_BARS")
    assert req.timeframe == "1Day" and req.is_daily
    assert (req.feed_param, req.calendar, req.bar_minutes) == ("sip", "XNYS", 390)
    assert (req.start, req.end) == (_utc(2016, 1, 1), _utc(2026, 11, 1))
    assert ds.adjustment_basis == "RAW"
    assert [(i.source_symbol, i.instrument_id) for i in req.instruments] == [
        (sym, f"{mic}:{sym}") for sym, mic in UNIVERSE.items()
    ]
    # Acquired 2026-11-09 with the owner's paper keys: one file per fund, restorable from source.
    assert ds.status == "ACQUIRED" and not ds.blocker
    assert len(ds.files) == len(UNIVERSE) and ds.retrieved_at is not None and ds.restorable
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(e for e in raw["history"] if e["name"] == NAME)
    assert entry["request"]["timeframe"] == "1Day"


def test_a_daily_request_must_be_xnys_with_the_nominal_session_length() -> None:
    req = _entry().request
    data = req.model_dump(mode="json")
    assert data["timeframe"] == "1Day"
    assert HistoryRequest.model_validate(data) == req
    with pytest.raises(ValidationError, match="XNYS"):
        HistoryRequest.model_validate({**data, "calendar": "UTC_24_7"})
    with pytest.raises(ValidationError, match="390"):
        HistoryRequest.model_validate({**data, "bar_minutes": 1440})
    with pytest.raises(ValidationError):
        HistoryRequest.model_validate({**data, "timeframe": "1Hour"})
    intraday = {k: v for k, v in data.items() if k != "timeframe"}
    assert "timeframe" not in HistoryRequest.model_validate(intraday).model_dump(mode="json")
