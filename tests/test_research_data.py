"""Historical data: store, quality checks, sources and ingestion (instructions.md §6 task 4)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.data.history import (
    DataQualityError,
    HistoryBar,
    content_sha256,
    read_bars,
    to_domain_bars,
    write_bars,
)
from trader.data.ingest import IngestContext, ingest, load_bars, verify_dataset
from trader.data.manifest import HistoryDataset, load_manifest
from trader.data.quality import clean_and_check, expected_starts_24_7
from trader.data.sources.alpaca_history import AlpacaCredentials, credentials_from_env
from trader.data.sources.http import build_url, parse_json
from trader.data.sources.public_crypto import fetch_coinbase_candles, fetch_kraken_ohlc

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


def bar(i: int, close: str = "100.00", **kw: Any) -> HistoryBar:
    c = Decimal(close)
    fields: dict[str, Any] = {
        "start": T0 + timedelta(minutes=15 * i),
        "minutes": 15,
        "open": c,
        "high": c + 1,
        "low": c - 1,
        "close": c,
        "volume": Decimal("1.23456789"),
    }
    fields.update(kw)
    return HistoryBar(**fields)


class FakeTransport:
    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return self.responder(url, dict(params or {}))


# ----------------------------------------------------------------------------- store


def test_parquet_round_trip_is_exact_and_deterministic(tmp_path: Path) -> None:
    bars = [bar(i, f"{100 + i}.37", vwap=Decimal("100.123456789012")) for i in range(5)]
    first = write_bars(tmp_path / "a.parquet", bars)
    second = write_bars(tmp_path / "b.parquet", bars)
    assert first == second  # same file and content hashes on a rewrite
    back = read_bars(tmp_path / "a.parquet", minutes=15)
    assert [b.canonical_row() for b in back] == [b.canonical_row() for b in bars]
    assert back[0].vwap == Decimal("100.123456789012")
    assert content_sha256(back) == first[1]


def test_more_than_twelve_decimals_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DataQualityError):
        write_bars(tmp_path / "x.parquet", [bar(0, volume=Decimal("0.0000000000001"))])


def test_missing_vwap_is_never_substituted_for_a_strategy_that_needs_it() -> None:
    hb = [bar(0)]
    with pytest.raises(DataQualityError, match="requires it"):
        to_domain_bars(
            hb,
            instrument_id="X:Y",
            feed="F",
            finalization_delay=timedelta(seconds=1),
            require_source_vwap=True,
        )
    out = to_domain_bars(
        hb,
        instrument_id="X:Y",
        feed="F",
        finalization_delay=timedelta(seconds=1),
        require_source_vwap=False,
    )
    assert out[0].available_time == hb[0].end + timedelta(seconds=1)


# ----------------------------------------------------------------------------- quality


def test_quality_checks_exclude_unfinished_invalid_and_duplicate_rows() -> None:
    raw = [
        bar(0),
        bar(0),  # identical duplicate from an overlapping page
        bar(1, low=Decimal("200")),  # impossible OHLC -> excluded, leaves a hole
        bar(2),
        bar(3, start=T0 + timedelta(minutes=15 * 3 + 5)),  # misaligned
        bar(5),
        bar(6),  # still in progress at retrieval time
    ]
    retrieved = T0 + timedelta(minutes=15 * 6 + 10)
    expected = expected_starts_24_7(T0, T0 + timedelta(minutes=15 * 7), 15)
    bars, report = clean_and_check(
        raw, minutes=15, retrieved_at=retrieved, expected=expected, gap_semantics="test"
    )
    assert [b.start for b in bars] == [T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=75)]
    assert report.duplicates_dropped == 1
    assert report.invalid_rows_excluded == 1
    assert report.misaligned_rows_excluded == 1
    assert report.unfinished_excluded == 1
    assert report.missing_intervals == 4  # 1 (invalid), 3 and 4 (absent), 6 (unfinished)
    assert report.missing_ranges[0].startswith("2026-01-05T00:15:00Z +1")


def test_conflicting_duplicates_abort_ingestion() -> None:
    expected = expected_starts_24_7(T0, T0 + timedelta(hours=1), 15)
    with pytest.raises(DataQualityError, match="conflicting duplicate"):
        clean_and_check(
            [bar(0, "100"), bar(0, "101")],
            minutes=15,
            retrieved_at=T0 + timedelta(days=1),
            expected=expected,
            gap_semantics="test",
        )


# ----------------------------------------------------------------------------- sources


def test_json_numbers_are_parsed_as_exact_decimals() -> None:
    payload = parse_json(b"[[1698804000,34442.3,34515.53,34508.77,34503.64,83.89821583]]")
    assert payload[0][1] == Decimal("34442.3")
    assert isinstance(payload[0][5], Decimal)
    assert build_url("https://x/y", {"b": "2", "a": "1"}) == "https://x/y?a=1&b=2"


def test_coinbase_pages_cover_the_range_without_overlap_or_out_of_range_rows() -> None:
    def responder(url: str, params: dict[str, str]) -> Any:
        start = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        rows = []
        t = start - timedelta(minutes=15)  # one row before the page, which must be ignored
        while t <= end:
            rows.append(
                [
                    int(t.timestamp()),
                    Decimal("1"),
                    Decimal("3"),
                    Decimal("2"),
                    Decimal("2"),
                    Decimal("5"),
                ]
            )
            t += timedelta(minutes=15)
        return list(reversed(rows))  # newest first, like the API

    transport = FakeTransport(responder)
    end = T0 + timedelta(minutes=15 * 650)
    bars = fetch_coinbase_candles(transport, "BTC-USD", minutes=15, start=T0, end=end)
    starts = sorted(b.start for b in bars)
    assert len(transport.calls) == 3  # 300 buckets per request
    assert starts[0] == T0 and starts[-1] == end - timedelta(minutes=15)
    assert len(starts) == len(set(starts)) == 650


def test_kraken_ohlc_always_drops_the_uncommitted_last_row() -> None:
    rows = [
        [int((T0 + timedelta(minutes=15 * i)).timestamp()), "2", "3", "1", "2", "2.1", "4", 7]
        for i in range(4)
    ]
    transport = FakeTransport(lambda url, p: {"error": [], "result": {"XXBTZUSD": rows, "last": 1}})
    bars = fetch_kraken_ohlc(transport, "XBTUSD", minutes=15)
    assert len(bars) == 3
    assert bars[0].vwap == Decimal("2.1") and bars[0].trade_count == 7


def _alpaca_dataset() -> HistoryDataset:
    return load_manifest(REPO / "research" / "data_manifest.json").dataset(
        "alpaca_us_equities_5m_v1"
    )


def test_alpaca_without_keys_is_blocked_with_an_owner_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert credentials_from_env() is None
    ds = _alpaca_dataset()
    transport = FakeTransport(lambda url, p: pytest.fail("no request without credentials"))
    out = ingest(ds, IngestContext(repo_root=tmp_path, transport=transport, now=T0))
    assert out.status == "BLOCKED" and "APCA_API_KEY_ID" in out.blocker


def test_alpaca_bars_and_corporate_actions_are_parsed_without_leaking_keys(tmp_path: Path) -> None:
    creds = AlpacaCredentials("key-id-123", "very-secret-456")
    assert "secret" not in repr(creds)
    session_open = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)

    def responder(url: str, params: dict[str, str]) -> Any:
        if url.endswith("/v2/stocks/bars"):
            if "page_token" not in params:
                rows = [
                    {
                        "t": "2026-11-02T13:00:00Z",
                        "o": 10,
                        "h": 11,
                        "l": 9,
                        "c": 10,
                        "v": 5,
                        "n": 1,
                        "vw": Decimal("10"),
                    },  # pre-market
                    {
                        "t": "2026-11-02T14:30:00Z",
                        "o": Decimal("10.5"),
                        "h": 11,
                        "l": 10,
                        "c": Decimal("10.75"),
                        "v": 500,
                        "n": 12,
                        "vw": Decimal("10.6"),
                    },
                ]
                return {"bars": {"AAPL": rows}, "next_page_token": "p2"}
            rows = [
                {
                    "t": "2026-11-02T14:35:00Z",
                    "o": 11,
                    "h": 12,
                    "l": 10,
                    "c": 11,
                    "v": 300,
                    "n": 9,
                    "vw": Decimal("11.2"),
                }
            ]
            return {"bars": {"AAPL": rows}, "next_page_token": None}
        return {
            "corporate_actions": {
                "forward_splits": [
                    {"symbol": "AAPL", "new_rate": 4, "old_rate": 1, "ex_date": "2026-11-03"}
                ],
                "name_changes": [
                    {"old_symbol": "AAPL", "new_symbol": "APPL", "process_date": "2026-11-04"}
                ],
            },
            "next_page_token": None,
        }

    ds = _alpaca_dataset().model_copy(
        update={
            "path": "data/history/test_alpaca",
            "request": _alpaca_dataset().request.model_copy(
                update={
                    "instruments": _alpaca_dataset().request.instruments[:1],
                    "start": session_open,
                    "end": session_open + timedelta(hours=1),
                }
            ),
        }
    )
    transport = FakeTransport(responder)
    out = ingest(
        ds,
        IngestContext(
            repo_root=tmp_path, transport=transport, now=T0 + timedelta(days=400), alpaca=creds
        ),
    )
    assert out.status == "ACQUIRED"
    record = out.files[0]
    assert record.rows == 2  # the pre-market bar is excluded as outside the regular session
    assert record.quality["outside_session_excluded"] == 1
    assert record.quality["missing_intervals"] == 10
    kinds = sorted(a["type"] for a in out.corporate_actions)
    assert kinds == ["SPLIT", "SYMBOL_CHANGE"]
    split = next(a for a in out.corporate_actions if a["type"] == "SPLIT")
    assert split["instrument_id"] == "XNYS:AAPL" and split["new_rate"] == "4"
    manifest_text = out.model_dump_json()
    assert "very-secret-456" not in manifest_text and "key-id-123" not in manifest_text
    assert all(c[2].get("APCA-API-SECRET-KEY") == "very-secret-456" for c in transport.calls)


def test_tampered_dataset_fails_verification_and_loading(tmp_path: Path) -> None:
    ds = load_manifest(REPO / "research" / "data_manifest.json").dataset("synthetic_equities_5m_v1")
    out = ingest(ds, IngestContext(repo_root=tmp_path, transport=FakeTransport(None), now=T0))
    assert verify_dataset(out, tmp_path) == []
    record = out.files[0]
    path = tmp_path / out.path / record.file
    bars = read_bars(path, minutes=5)
    write_bars(path, bars[:-1])  # silently drop one bar
    assert any("checksum" in p for p in verify_dataset(out, tmp_path))
    with pytest.raises(DataQualityError, match="checksum"):
        load_bars(out, record.instrument_id, tmp_path)


def test_missing_owner_archive_is_blocked(tmp_path: Path) -> None:
    ds = load_manifest(REPO / "research" / "data_manifest.json").dataset(
        "kraken_ohlcvt_archive_btc_eth_usd_15m_v1"
    )
    out = ingest(ds, IngestContext(repo_root=tmp_path, transport=FakeTransport(None), now=T0))
    assert out.status == "BLOCKED" and "owner action" in out.blocker


# ----------------------------------------------------------------------------- fresh checkouts


def test_a_fresh_checkout_restores_missing_files_bit_for_bit(tmp_path: Path) -> None:
    from trader.data.ingest import verify_or_restore

    ds = load_manifest(REPO / "research" / "data_manifest.json").dataset("synthetic_equities_5m_v1")
    ctx = IngestContext(repo_root=tmp_path, transport=FakeTransport(None), now=T0)
    first = verify_or_restore(ds, ctx)  # no files yet, as on a teammate's new clone
    assert first.status == "RESTORED" and not first.failed
    assert verify_or_restore(ds, ctx).status == "OK"


def test_restoring_different_content_is_a_loud_failure(tmp_path: Path) -> None:
    from trader.data.ingest import verify_or_restore

    ds = load_manifest(REPO / "research" / "data_manifest.json").dataset("synthetic_equities_5m_v1")
    tampered = ds.model_copy(
        update={"files": tuple(f.model_copy(update={"content_sha256": "0" * 64}) for f in ds.files)}
    )
    outcome = verify_or_restore(
        tampered, IngestContext(repo_root=tmp_path, transport=FakeTransport(None), now=T0)
    )
    assert outcome.status == "MISMATCH" and outcome.failed


def test_a_one_time_capture_is_reported_not_refetched(tmp_path: Path) -> None:
    from trader.data.ingest import verify_or_restore

    ds = load_manifest(REPO / "research" / "data_manifest.json").dataset(
        "kraken_btc_eth_usd_15m_recent_v1"
    )
    assert ds.restorable is False
    transport = FakeTransport(
        lambda url, p: pytest.fail("a one-time capture must not be re-downloaded")
    )
    outcome = verify_or_restore(ds, IngestContext(repo_root=tmp_path, transport=transport, now=T0))
    assert outcome.status == "NOT_RESTORABLE" and not outcome.failed
