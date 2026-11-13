"""The intel feed's refresh (ADR-0047): what one run writes, which sources are due, failure
isolation, cursors, the write boundary, the import budget, the watcher and the command line.

Scripted sources and synthetic content only; no test touches the network or the real data/intel.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trader.intel import feed
from trader.intel.__main__ import main as intel_main
from trader.intel.config import IntelConfig, Target
from trader.intel.feed import FeedError, read_index, refresh, run_watch, summary_line
from trader.intel.records import IntelRecord, Kind
from trader.intel.sources import SourceResult
from trader.intel.store import LOCK_NAME, StoreBusy, read_jsonl
from trader.market.watchlists import Watchlists

from .test_intel_feed import insider, news, premarket

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)  # a Tuesday, 09:00 New York
MINUTE = timedelta(minutes=1)
WATCHLISTS = Watchlists.model_validate(
    {
        "retention_days": {"5m": 2, "1h": 10, "1d": 30},
        "groups": [
            {
                "id": "stocks",
                "title": "Stocks",
                "source": "alpaca_iex",
                "source_label": "Alpaca IEX",
                "day_rule": "nyse",
                "instruments": [
                    {"symbol": "NVDA", "name": "NVIDIA"},
                    {"symbol": "SPY", "name": "SPDR S&P 500 ETF"},
                ],
            },
            {
                "id": "cfd",
                "title": "CFDs",
                "source": "capital_demo",
                "source_label": "Capital.com demo",
                "day_rule": "utc",
                "instruments": [
                    {"symbol": "GOLD", "name": "Gold"},
                    {"symbol": "DE40", "name": "Germany 40"},
                ],
            },
        ],
    }
)
CONFIG = IntelConfig.model_validate(
    {
        "sources": {
            "alpaca_news": {"label": "Alpaca news (Benzinga)", "every_minutes": 5},
            "alpaca_premarket": {"label": "Alpaca SIP pre-market", "every_minutes": 5},
            "edgar_form4": {"label": "SEC EDGAR Form 4", "every_minutes": 30},
        },
        "targets": [
            {"id": "stocks:NVDA", "news": ["NVDA"], "cik": 1045810, "premarket": True},
            {"id": "stocks:SPY", "news": ["SPY"], "premarket": True},
            {"id": "cfd:GOLD", "news": ["GLD"], "news_proxy": "GLD, a gold fund"},
            {"id": "cfd:DE40"},
        ],
    }
)
CONFIG.check_against(WATCHLISTS)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.t = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


class Scripted:
    """A source that returns prepared results in order (the last one repeats), or raises."""

    def __init__(self, source_id: str, kind: Kind, *results: SourceResult | Exception) -> None:
        self.id = source_id
        self.kind: Kind = kind
        self.results = list(results) or [SourceResult()]
        self.cursors: list[dict[str, str]] = []

    def fetch(
        self, targets: Sequence[Target], cursor: Mapping[str, str], *, now: datetime
    ) -> SourceResult:
        self.cursors.append(dict(cursor))
        answer = self.results[min(len(self.cursors), len(self.results)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def result(*records: IntelRecord, **over: Any) -> SourceResult:
    return SourceResult(records=list(records), **over)


def sources(
    news_source: Scripted | None = None,
    pre: Scripted | None = None,
    edgar: Scripted | None = None,
) -> dict[str, Scripted]:
    return {
        "alpaca_news": news_source or Scripted("alpaca_news", "news"),
        "alpaca_premarket": pre or Scripted("alpaca_premarket", "premarket"),
        "edgar_form4": edgar or Scripted("edgar_form4", "insider"),
    }


def root_of(tmp_path: Path) -> Path:
    return tmp_path / "data" / "intel"


def load(root: Path, rel: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / rel).read_text(encoding="utf-8"))
    return data


def by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in rows}


FIRST = sources(
    Scripted(
        "alpaca_news",
        "news",
        result(
            news(1, "Acme Q3 Earnings Beat Estimates", NOW - 20 * MINUTE),
            news(2, "ACME Q3 earnings beat estimates!", NOW - 19 * MINUTE),  # a syndicated copy
            news(3, "Gold Fund Sees Inflows", NOW - 10 * MINUTE, targets=("cfd:GOLD",)),
            cursor={"updated_after": "2026-11-10T13:50:00Z"},
        ),
    ),
    Scripted(
        "alpaca_premarket",
        "premarket",
        result(premarket("2026-11-10", NOW - 16 * MINUTE, last="177.5", gap_pct="1.36")),
    ),
    Scripted(
        "edgar_form4",
        "insider",
        result(
            insider(
                "0001197647-26-000013",
                NOW - timedelta(days=4),
                buy_value="176520",
                buy_shares="1000",
                sell_value="0",
                sell_shares="0",
            ),
            cursor={"stocks:NVDA": "2026-11-06T21:12:02Z|0001197647-26-000013"},
        ),
    ),
)


# ----------------------------------------------------------------------------- one refresh
def test_a_refresh_stores_records_snapshots_latest_files_the_index_and_a_run_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = root_of(tmp_path)
    written: list[str] = []
    real_write = feed.write_json_atomic

    def spy(path: Path, payload: Any, *, indent: int | None = None) -> None:
        written.append(path.relative_to(root).as_posix())
        real_write(path, payload, indent=indent)

    monkeypatch.setattr(feed, "write_json_atomic", spy)
    index = refresh(CONFIG, WATCHLISTS, root, FIRST, clock=Clock())

    assert written == [
        "latest/stocks/NVDA.json",
        "latest/stocks/SPY.json",
        "latest/cfd/GOLD.json",
        "latest/cfd/DE40.json",
        "index.json",
    ]
    assert index == read_index(root) and index["version"] == 1
    assert index["generated_at"] == "2026-11-10T14:00:00Z" and index["every_minutes"] == 5
    assert not (root / LOCK_NAME).exists()

    by_source = by_id(index["sources"])
    assert list(by_source) == ["alpaca_news", "alpaca_premarket", "edgar_form4"]
    assert [(s["kind"], s["status"], s["new"]) for s in index["sources"]] == [
        ("news", "ok", 3),
        ("premarket", "ok", 1),
        ("insider", "ok", 1),
    ]
    assert by_source["alpaca_news"]["cursor"] == {"updated_after": "2026-11-10T13:50:00Z"}
    assert by_source["alpaca_news"]["label"] == "Alpaca news (Benzinga)"
    assert by_source["edgar_form4"]["updated_at"] == "2026-11-10T14:00:00Z"

    targets = by_id(index["targets"])
    nvda = targets["stocks:NVDA"]
    assert nvda["name"] == "NVIDIA" and nvda["file"] == "latest/stocks/NVDA.json"
    assert nvda["has"] == {"news": True, "insiders": True, "premarket": True}
    assert nvda["news_count_24h"] == 1  # the syndicated copy counts once
    assert nvda["last_news_at"] == "2026-11-10T13:40:00Z"
    assert nvda["insider_net_value"] == "176520"
    assert (nvda["premarket_session"], nvda["premarket_gap_pct"]) == ("2026-11-10", "1.36")
    assert nvda["snapshot_at"] == "2026-11-10T14:00:00Z"
    assert targets["cfd:GOLD"]["news_proxy"] == "GLD, a gold fund"
    assert targets["cfd:GOLD"]["has"] == {"news": True, "insiders": False, "premarket": False}
    assert targets["cfd:DE40"]["has"] == {"news": False, "insiders": False, "premarket": False}
    assert targets["stocks:SPY"]["news_count_24h"] == 0

    latest = load(root, "latest/stocks/NVDA.json")
    assert latest["id"] == "stocks:NVDA" and latest["snapshot"]["hash"] == nvda["snapshot_hash"]
    assert [n["id"] for n in latest["news"]] == ["alpaca_news:1"]
    item = latest["news"][0]
    assert item["first_seen_at"] == "2026-11-10T14:00:00Z" and item["at"] == "2026-11-10T13:40:00Z"
    assert item["category"] == "earnings" and item["feed"] == "benzinga" and item["proxy"] is None
    assert latest["insiders"][0]["facts"]["buy_shares"] == "1000"
    assert load(root, "latest/cfd/GOLD.json")["news"][0]["proxy"] == "GLD, a gold fund"
    assert load(root, "latest/cfd/DE40.json")["news"] == []

    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.jsonl"))
    assert files == [
        "records/insider/2026-11.jsonl",
        "records/news/2026-11.jsonl",
        "records/premarket/2026-11.jsonl",
        "runs/2026-11.jsonl",
        "snapshots/cfd/DE40/2026-11.jsonl",
        "snapshots/cfd/GOLD/2026-11.jsonl",
        "snapshots/stocks/NVDA/2026-11.jsonl",
        "snapshots/stocks/SPY/2026-11.jsonl",
    ]
    assert read_jsonl(root / "runs" / "2026-11.jsonl") == [
        {
            "at": "2026-11-10T14:00:00Z",
            "new": 5,
            "sources": {"alpaca_news": "ok", "alpaca_premarket": "ok", "edgar_form4": "ok"},
        }
    ]
    assert summary_line(index) == (
        "2026-11-10T14:00:00Z intel feed: alpaca_news ok (+3) · alpaca_premarket ok (+1) · "
        "edgar_form4 ok (+1)"
    )


def test_nothing_is_written_outside_data_intel(tmp_path: Path) -> None:
    refresh(CONFIG, WATCHLISTS, root_of(tmp_path), FIRST, clock=Clock())
    outside = [
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        if p.is_file() and not p.relative_to(tmp_path).as_posix().startswith("data/intel/")
    ]
    assert outside == []
    for wrong in (tmp_path / "data" / "market", tmp_path / "data" / "history" / "intel"):
        with pytest.raises(RuntimeError, match="refusing"):
            refresh(CONFIG, WATCHLISTS, wrong, FIRST, clock=Clock())
        assert not wrong.exists()


# ----------------------------------------------------------------------------- cadence
def test_each_source_runs_at_its_own_cadence_and_unchanged_knowledge_adds_no_snapshot(
    tmp_path: Path,
) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    src = sources()
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    calls = lambda: [len(s.cursors) for s in src.values()]  # noqa: E731
    assert calls() == [1, 1, 1]
    snapshots = root / "snapshots" / "stocks" / "NVDA" / "2026-11.jsonl"
    assert len(read_jsonl(snapshots)) == 1

    clock.t = NOW + 2 * MINUTE  # nothing is due yet
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert calls() == [1, 1, 1] and [s["new"] for s in index["sources"]] == [0, 0, 0]
    assert by_id(index["sources"])["alpaca_news"]["checked_at"] == "2026-11-10T14:00:00Z"

    clock.t = NOW + 5 * MINUTE - timedelta(seconds=1)  # still the same five-minute slot
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert calls() == [1, 1, 1]

    clock.t = NOW + 5 * MINUTE + timedelta(seconds=15)  # the next slot, where the watcher ticks
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert calls() == [2, 2, 1]  # the 30-minute source waits

    clock.t = NOW + 30 * MINUTE
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert calls() == [3, 3, 2]
    assert len(read_jsonl(snapshots)) == 1  # five runs, nothing new known: one snapshot
    assert by_id(index["targets"])["stocks:NVDA"]["snapshot_at"] == "2026-11-10T14:00:00Z"
    assert len(read_jsonl(root / "runs" / "2026-11.jsonl")) == 5  # every run leaves its line


def test_a_named_source_runs_now_and_the_others_are_left_alone(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    src = sources()
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=Clock(), only=["edgar_form4"])
    assert [len(s.cursors) for s in src.values()] == [0, 0, 1]
    assert [(s["status"], s["note"]) for s in index["sources"]] == [
        ("idle", "not read yet"),
        ("idle", "not read yet"),
        ("ok", None),
    ]
    refresh(CONFIG, WATCHLISTS, root, src, clock=Clock(), only=["edgar_form4"])
    assert len(src["edgar_form4"].cursors) == 2  # asked for by name: run again although not due
    with pytest.raises(FeedError, match="unknown source"):
        refresh(CONFIG, WATCHLISTS, root, src, clock=Clock(), only=["x_posts"])


# ----------------------------------------------------------------------------- failures
def test_one_failing_source_does_not_stop_the_others_and_keeps_its_cursor(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    flaky = Scripted(
        "alpaca_news",
        "news",
        result(
            news(1, "Acme Q3 Earnings Beat Estimates", NOW - MINUTE),
            cursor={"updated_after": "2026-11-10T13:59:00Z"},
        ),
        RuntimeError("HTTP 500 from https://data.alpaca.markets/v1beta1/news"),
        result(cursor={"updated_after": "2026-11-10T14:09:00Z"}),
    )
    src = sources(news_source=flaky)
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)

    clock.t = NOW + 5 * MINUTE
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    failed = by_id(index["sources"])["alpaca_news"]
    assert failed["status"] == "error" and "HTTP 500" in failed["error"]
    assert failed["cursor"] == {"updated_after": "2026-11-10T13:59:00Z"}  # not lost
    assert failed["updated_at"] == "2026-11-10T14:00:00Z"  # the last good read
    assert failed["checked_at"] == "2026-11-10T14:05:00Z"
    assert by_id(index["sources"])["alpaca_premarket"]["status"] == "ok"
    snap = load(root, "latest/stocks/NVDA.json")["snapshot"]
    assert snap["sources"]["alpaca_news"] == "error" and snap["news"]["count_24h"] == 1
    assert "alpaca_news error: HTTP 500" in summary_line(index)

    clock.t = NOW + 10 * MINUTE
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert flaky.cursors[-1] == {"updated_after": "2026-11-10T13:59:00Z"}  # resumed from there
    healed = by_id(index["sources"])["alpaca_news"]
    assert (healed["status"], healed["error"]) == ("ok", None)
    assert healed["cursor"] == {"updated_after": "2026-11-10T14:09:00Z"}
    # The status went ok -> error -> ok: three different states of knowledge, three snapshots.
    assert len(read_jsonl(root / "snapshots" / "stocks" / "NVDA" / "2026-11.jsonl")) == 3


def test_a_waiting_or_partial_source_is_shown_as_such(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    waiting = Scripted(
        "edgar_form4",
        "insider",
        result(status="waiting", note="waiting for SEC_EDGAR_CONTACT in .env"),
    )
    partial = Scripted(
        "alpaca_news",
        "news",
        result(
            news(1, "Acme Q3 Earnings Beat Estimates", NOW - MINUTE),
            status="partial",
            errors={"stocks:SPY": "HTTP 404", "cfd:GOLD": "HTTP 429"},
        ),
    )
    index = refresh(
        CONFIG, WATCHLISTS, root, sources(news_source=partial, edgar=waiting), clock=Clock()
    )
    got = by_id(index["sources"])
    assert got["edgar_form4"]["status"] == "waiting" and got["edgar_form4"]["updated_at"] is None
    assert got["edgar_form4"]["note"] == "waiting for SEC_EDGAR_CONTACT in .env"
    assert got["alpaca_news"]["status"] == "partial" and got["alpaca_news"]["new"] == 1
    assert got["alpaca_news"]["error"] == "cfd:GOLD: HTTP 429; stocks:SPY: HTTP 404"
    assert got["alpaca_news"]["updated_at"] == "2026-11-10T14:00:00Z"
    line = summary_line(index)
    assert "edgar_form4 waiting: waiting for SEC_EDGAR_CONTACT in .env" in line
    snap = load(root, "latest/stocks/NVDA.json")["snapshot"]
    assert snap["sources"]["edgar_form4"] == "waiting" and snap["insiders"]["filings"] == 0


def test_a_source_that_is_not_wired_and_a_source_that_backs_off(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    later = NOW + 20 * MINUTE
    backing_off = Scripted("alpaca_news", "news", result(status="error", retry_after=later))
    src: dict[str, Scripted] = {"alpaca_news": backing_off}
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    got = by_id(index["sources"])
    assert got["alpaca_premarket"]["status"] == "error"
    assert got["alpaca_premarket"]["error"] == "no source configured for alpaca_premarket"
    assert got["alpaca_news"]["retry_after"] == "2026-11-10T14:20:00Z"

    clock.t = NOW + 10 * MINUTE  # due by cadence, but told to wait
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert len(backing_off.cursors) == 1
    clock.t = later
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert len(backing_off.cursors) == 2


def test_a_second_writer_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    refresh(CONFIG, WATCHLISTS, root, FIRST, clock=Clock())
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / LOCK_NAME).write_text('{"pid": 1, "at": "2026-11-10T14:03:00Z"}\n', encoding="utf-8")
    with pytest.raises(StoreBusy):
        refresh(CONFIG, WATCHLISTS, root, sources(), clock=Clock(NOW + 5 * MINUTE))
    (root / LOCK_NAME).unlink()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


# ----------------------------------------------------------------------------- revisions
def test_an_edited_headline_is_a_new_revision_and_the_console_file_shows_the_latest(
    tmp_path: Path,
) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    at = NOW - 10 * MINUTE
    edits = Scripted(
        "alpaca_news",
        "news",
        result(news(1, "Acme Opens A New Office", at)),
        result(news(1, "Acme Sued Over New Office", at)),
    )
    src = sources(news_source=edits)
    refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    clock.t = NOW + 5 * MINUTE
    index = refresh(CONFIG, WATCHLISTS, root, src, clock=clock)
    assert by_id(index["sources"])["alpaca_news"]["new"] == 1
    rows = read_jsonl(root / "records" / "news" / "2026-11.jsonl")
    assert [(r["id"], r["rev"], r["first_seen_at"]) for r in rows] == [
        ("alpaca_news:1", 1, "2026-11-10T14:00:00Z"),
        ("alpaca_news:1", 2, "2026-11-10T14:05:00Z"),
    ]
    item = load(root, "latest/stocks/NVDA.json")["news"][0]
    assert (item["title"], item["rev"], item["category"]) == (
        "Acme Sued Over New Office",
        2,
        "legal_regulatory",
    )
    snapshots = read_jsonl(root / "snapshots" / "stocks" / "NVDA" / "2026-11.jsonl")
    assert [s["news"]["categories_24h"] for s in snapshots] == [
        {"unknown": 1},
        {"legal_regulatory": 1},
    ]


# ----------------------------------------------------------------------------- process
def test_the_feed_imports_neither_the_heavy_stack_nor_any_order_authority() -> None:
    probe = (
        "import sys, trader.intel.feed, trader.intel.__main__; "
        "heavy = ('pandas', 'duckdb', 'exchange_calendars', 'sqlalchemy', 'numpy', 'typer'); "
        "authority = ('trader.brokers', 'trader.execution', 'trader.risk', 'trader.runtime', "
        "'trader.storage'); "
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in heavy "
        "or m.startswith(authority))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"the intel feed imported: {out}"


def test_watch_refreshes_shortly_after_each_cadence_boundary(tmp_path: Path) -> None:
    clock = Clock(NOW + MINUTE)
    lines: list[str] = []
    src = sources()
    code = run_watch(
        root=root_of(tmp_path), echo=lines.append, clock=clock, sources=src, iterations=2
    )
    assert code == 0 and [len(s.cursors) for s in src.values()] == [2, 2, 1]
    assert clock.slept == [255.0]  # 14:01:00 -> 14:05:15
    assert "no order authority" in lines[0] and "37 instruments" in lines[0]
    assert lines[-1].startswith("2026-11-10T14:05:15Z intel feed: alpaca_news ok")


def test_a_failed_refresh_does_not_stop_the_watcher(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    root.mkdir(parents=True)
    (root / LOCK_NAME).write_text('{"pid": 1, "at": "2026-11-10T14:00:00Z"}\n', encoding="utf-8")
    lines: list[str] = []
    code = run_watch(root=root, echo=lines.append, clock=Clock(), sources=sources(), iterations=1)
    assert code == 0 and lines[-1].startswith("intel feed: refresh failed: another intel refresh")


def test_the_command_line_refuses_an_unknown_source_before_fetching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        intel_main(["refresh", "--source", "x_posts"])
    assert caught.value.code == 2 and "invalid choice" in capsys.readouterr().err
