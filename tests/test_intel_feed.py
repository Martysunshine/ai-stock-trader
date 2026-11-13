"""The intel feed (ADR-0047): configuration, text cleaning, keyword categories, the append-only
store and the point-in-time snapshot.

Synthetic content only; no test touches the network or the real data/intel.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trader.intel.classify import CATEGORIES, EXTRACTOR_VERSION, classify
from trader.intel.config import DEFAULT_CONFIG, IntelConfig, Target, load_intel_config
from trader.intel.records import (
    IntelRecord,
    StoredRecord,
    clean_text,
    headline_key,
)
from trader.intel.snapshot import SNAPSHOT_VERSION, snapshot, visible
from trader.intel.store import (
    LOCK_NAME,
    IntelStore,
    StoreBusy,
    StoreError,
    append_lines,
    check_root,
    latest_by_id,
    read_jsonl,
)

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)  # a Tuesday, 09:00 New York
NVDA = Target(id="stocks:NVDA", news=("NVDA",), cik=1045810, premarket=True)
GOLD = Target(id="cfd:GOLD", news=("GLD",), news_proxy="GLD, a gold fund")
DE40 = Target(id="cfd:DE40")
OK = {"alpaca_news": "ok", "alpaca_premarket": "ok", "edgar_form4": "ok"}


def root_of(tmp_path: Path) -> Path:
    return tmp_path / "data" / "intel"


def news(
    item: int,
    title: str,
    at: datetime,
    *,
    targets: tuple[str, ...] = ("stocks:NVDA",),
    category: str | None = None,
) -> IntelRecord:
    found, tags = classify(title)
    return IntelRecord(
        kind="news",
        source="alpaca_news",
        source_id=str(item),
        targets=targets,
        title=title,
        category=category or found,
        tags=tags,
        extractor_version=EXTRACTOR_VERSION,
        url=f"https://www.benzinga.com/news/{item}",
        published_at=at,
        updated_at=at,
        summary="A synthetic summary.",
        provenance={"provider": "alpaca", "feed": "benzinga"},
    )


def insider(accession: str, at: datetime, **facts: Any) -> IntelRecord:
    return IntelRecord(
        kind="insider",
        source="edgar_form4",
        source_id=accession,
        targets=("stocks:NVDA",),
        title="EXAMPLE PERSON (director): bought 1,000 shares",
        category="insider_buy",
        extractor_version="form4-v1",
        published_at=at,
        facts=facts,
        provenance={"provider": "sec", "form": "4"},
    )


def premarket(session: str, at: datetime, **facts: Any) -> IntelRecord:
    return IntelRecord(
        kind="premarket",
        source="alpaca_premarket",
        source_id=f"NVDA:{session}",
        targets=("stocks:NVDA",),
        title=f"NVDA pre-market {session}",
        category="premarket",
        extractor_version="premarket-v1",
        published_at=at,
        facts={"session": session, **facts},
        provenance={"provider": "alpaca", "feed": "sip"},
    )


# ----------------------------------------------------------------------------- configuration
def test_the_shipped_configuration_loads_and_matches_the_watchlists() -> None:
    config, watchlists = load_intel_config()
    assert DEFAULT_CONFIG.as_posix().endswith("configs/intel/sources.yaml")
    assert set(config.sources) == {"alpaca_news", "alpaca_premarket", "edgar_form4"}
    ids = [t.id for t in config.targets]
    assert len(ids) == sum(len(g.instruments) for g in watchlists.groups) == 37
    with_cik = [t.id for t in config.targets if t.cik is not None]
    assert len(with_cik) == 20 and "stocks:SPY" not in with_cik
    assert config.target("stocks:NVDA").cik == 1045810
    assert config.target("crypto:BTC").news == ("BTCUSD",)
    # the crypto sleeve's CFD epics carry the coin's own news, not a stand-in
    assert config.target("cfd:BTCUSD").news == ("BTCUSD",)
    assert not config.target("cfd:ETHUSD").news_proxy
    assert config.target("cfd:GOLD").news_proxy and not config.target("cfd:DE40").news
    assert all(t.premarket == (t.group == "stocks") for t in config.targets)
    assert config.names(watchlists)["stocks:NVDA"] == "NVIDIA"


@pytest.mark.parametrize(
    "target",
    [
        {"id": "stocks:NVDA", "news": ["nvda"]},
        {"id": "stocks:NVDA", "news": ["NVDA", "NVDA"]},
        {"id": "stocks:NVDA", "news": ["NV-DA"]},
        {"id": "NVDA"},
        {"id": "stocks:NVDA", "news_proxy": "a fund"},
        {"id": "crypto:BTC", "cik": 1},
        {"id": "cfd:GOLD", "premarket": True},
        {"id": "stocks:NVDA", "cik": 0},
        {"id": "stocks:NVDA", "surprise": 1},
    ],
)
def test_a_target_that_makes_no_sense_is_refused(target: dict[str, Any]) -> None:
    source = {"label": "x", "every_minutes": 5}
    with pytest.raises(ValueError):
        IntelConfig.model_validate({"sources": {"alpaca_news": source}, "targets": [target]})


def test_duplicate_targets_and_targets_outside_the_watchlists_are_refused() -> None:
    _, watchlists = load_intel_config()
    source = {"label": "x", "every_minutes": 5}
    with pytest.raises(ValueError, match="unique"):
        IntelConfig.model_validate(
            {"sources": {"alpaca_news": source}, "targets": [{"id": "stocks:NVDA"}] * 2}
        )
    stray = IntelConfig.model_validate(
        {"sources": {"alpaca_news": source}, "targets": [{"id": "stocks:ZZZZ"}]}
    )
    with pytest.raises(ValueError, match="stocks:ZZZZ"):
        stray.check_against(watchlists)
    with pytest.raises(KeyError):
        stray.target("stocks:NVDA")


# ----------------------------------------------------------------------------- text
def test_text_is_reduced_to_one_plain_capped_line() -> None:
    hostile = "Nvidia&#39;s <b>big</b>\n\tday <script>alert(1)</script>‮evil​ \x07bell &amp; more"
    assert clean_text(hostile, 200) == "Nvidia's big day alert(1) evil bell & more"
    assert clean_text("x" * 500, 20) == "x" * 19 + "…"
    assert clean_text(None, 50) == "" and clean_text(["a"], 50) == ""
    assert clean_text("  spaced   out  ", 50) == "spaced out"


def test_the_same_headline_from_two_outlets_has_one_key() -> None:
    assert headline_key("Nvidia To $300? Here's Why") == headline_key("NVIDIA to $300 - here s why")
    assert headline_key("Nvidia To $300?") != headline_key("Nvidia To $200?")
    assert headline_key("...") == ""


# ----------------------------------------------------------------------------- categories
@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Acme Q3 Earnings Beat Estimates As Revenue Jumps", "earnings"),
        ("Acme Raises Full-Year Outlook", "guidance"),
        ("Acme Prices $2 Billion Convertible Notes Offering", "financing"),
        ("Acme To Buy Smaller Rival In All-Cash Merger", "mna"),
        ("Analyst Lifts Acme Price Target, Reiterates Overweight", "analyst"),
        ("Regulators Open Antitrust Probe Into Acme", "legal_regulatory"),
        ("Acme Opens A New Office", "unknown"),
        ("", "unknown"),
    ],
)
def test_headlines_are_named_by_fixed_keyword_rules(title: str, category: str) -> None:
    found, tags = classify(title)
    assert found == category and found in CATEGORIES
    assert (found in tags) == (found != "unknown")


def test_keywords_match_whole_words_and_the_first_category_wins() -> None:
    assert classify("Acme Fined Over Chip Sales")[0] == "legal_regulatory"
    assert classify("Refined Sugar Prices Banner Year")[0] == "unknown"  # not "fined", not "ban"
    category, tags = classify("Analyst Cuts Target After Acme Earnings Miss", "Guidance was cut.")
    assert category == "earnings" and tags == ("earnings", "analyst", "guidance")
    assert EXTRACTOR_VERSION == "keywords-v1"


@pytest.mark.parametrize(
    ("title", "summary", "category"),
    [
        # Each of these was misnamed by a looser rule on live headlines (2026-11-10).
        ("Acme To $300? Here Are 10 Top Analyst Forecasts", "RIOT upgraded to Buy.", "analyst"),
        ("Fund Cuts Share Stake In Acme By 9.1%", "", "unknown"),
        ("Acme Ships New Fine-Tuning Tools", "", "unknown"),
        ("Consumer Prices Rise 0.4%, Exceeding Forecasts", "", "unknown"),
    ],
)
def test_phrases_that_proved_ambiguous_on_live_headlines_do_not_name_a_category(
    title: str, summary: str, category: str
) -> None:
    assert classify(title, summary)[0] == category


# ----------------------------------------------------------------------------- records
def test_a_record_round_trips_through_its_stored_line() -> None:
    record = news(1, "Acme Q3 Earnings Beat Estimates", NOW - timedelta(minutes=5))
    row = StoredRecord(
        id=record.id,
        rev=1,
        first_seen_at=NOW,
        content_hash=record.content_hash(),
        headline_key=headline_key(record.title),
        record=record,
    )
    line = json.loads(json.dumps(row.to_json()))
    assert line["schema_version"] == 1 and line["id"] == "alpaca_news:1"
    assert line["record"]["published_at"] == "2026-11-10T13:55:00Z"
    assert set(line["record"]) >= {"provenance", "extractor_version", "category", "url", "targets"}
    assert "content" not in line["record"]
    back = StoredRecord.from_json(line)
    assert back == row and back.record.content_hash() == row.content_hash


def test_an_item_never_counts_as_published_after_it_was_first_seen() -> None:
    late = news(1, "Acme Opens A New Office", NOW + timedelta(hours=1))
    row = StoredRecord("alpaca_news:1", 1, NOW, late.content_hash(), "k", late)
    assert row.effective_at == NOW
    early = news(2, "Acme Opens A New Office", NOW - timedelta(hours=1))
    assert StoredRecord("alpaca_news:2", 1, NOW, "h", "k", early).effective_at == early.published_at


def test_a_stored_line_of_another_shape_is_refused() -> None:
    record = news(1, "Acme Opens A New Office", NOW)
    good = StoredRecord("alpaca_news:1", 1, NOW, "h", "k", record).to_json()
    for broken in (
        {**good, "schema_version": 2},
        {**good, "first_seen_at": "yesterday"},
        {**good, "record": {**good["record"], "kind": "rumour"}},
    ):
        with pytest.raises(ValueError):
            StoredRecord.from_json(broken)


# ----------------------------------------------------------------------------- store
def test_the_store_only_writes_into_an_intel_folder(tmp_path: Path) -> None:
    assert check_root(root_of(tmp_path)) == root_of(tmp_path)
    for wrong in (tmp_path / "data" / "market", tmp_path / "data" / "history" / "intel"):
        with pytest.raises(StoreError, match="refusing"):
            IntelStore(wrong)


def test_new_items_are_appended_unchanged_items_are_not_and_changes_become_revisions(
    tmp_path: Path,
) -> None:
    store = IntelStore(root_of(tmp_path))
    latest: dict[str, StoredRecord] = {}
    first = news(1, "Acme Opens A New Office", NOW - timedelta(minutes=30))
    other = news(2, "Acme Q3 Earnings Beat Estimates", NOW - timedelta(minutes=20))
    added = store.append([first, other], seen_at=NOW, latest=latest)
    assert [(r.id, r.rev) for r in added] == [("alpaca_news:1", 1), ("alpaca_news:2", 1)]
    assert added[0].first_seen_at == NOW and added[0].headline_key == headline_key(first.title)

    again = store.append([first, other], seen_at=NOW + timedelta(minutes=5), latest=latest)
    assert again == []

    edited = news(1, "Acme Opens A New Office In Prague", NOW - timedelta(minutes=30))
    later = NOW + timedelta(minutes=10)
    revised = store.append([edited], seen_at=later, latest=latest)
    assert [(r.id, r.rev, r.first_seen_at) for r in revised] == [("alpaca_news:1", 2, later)]

    path = store.records_file("news", "2026-11")
    lines = path.read_bytes().split(b"\n")
    assert len(lines) == 4 and lines[-1] == b"" and b"\r" not in path.read_bytes()
    loaded = store.load("news", since=NOW - timedelta(days=1), until=later)
    assert [(r.id, r.rev) for r in loaded] == [
        ("alpaca_news:1", 1),
        ("alpaca_news:2", 1),
        ("alpaca_news:1", 2),
    ]
    assert latest_by_id(loaded)["alpaca_news:1"].record.title.endswith("Prague")
    assert latest_by_id(store.load_all(later)) == latest


def test_records_are_filed_by_kind_and_by_the_month_they_were_first_seen(tmp_path: Path) -> None:
    store = IntelStore(root_of(tmp_path))
    latest: dict[str, StoredRecord] = {}
    october = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    store.append([news(1, "Acme Opens A New Office", october)], seen_at=october, latest=latest)
    store.append([insider("0001-26-000001", NOW, buy_value="100")], seen_at=NOW, latest=latest)
    store.append([news(2, "Acme Opens Another Office", NOW)], seen_at=NOW, latest=latest)
    files = sorted(p.relative_to(store.root).as_posix() for p in store.root.rglob("*.jsonl"))
    assert files == [
        "records/insider/2026-11.jsonl",
        "records/news/2026-10.jsonl",
        "records/news/2026-11.jsonl",
    ]
    both_months = store.load("news", since=october - timedelta(days=1), until=NOW)
    assert [r.id for r in both_months] == ["alpaca_news:1", "alpaca_news:2"]
    assert [r.id for r in store.load("news", since=NOW - timedelta(hours=1), until=NOW)] == [
        "alpaca_news:2"
    ]


def test_a_torn_last_line_is_skipped_and_the_next_append_starts_a_new_line(tmp_path: Path) -> None:
    store = IntelStore(root_of(tmp_path))
    latest: dict[str, StoredRecord] = {}
    store.append([news(1, "Acme Opens A New Office", NOW)], seen_at=NOW, latest=latest)
    path = store.records_file("news", "2026-11")
    with path.open("ab") as fh:
        fh.write(b'{"schema_version":1,"id":"alpaca_news:9","rev":1,"first_se')  # a crash mid-write
    assert [r.id for r in store.load("news", since=NOW - timedelta(days=1), until=NOW)] == [
        "alpaca_news:1"
    ]
    store.append([news(2, "Acme Opens Another Office", NOW)], seen_at=NOW, latest=latest)
    loaded = store.load("news", since=NOW - timedelta(days=1), until=NOW)
    assert [r.id for r in loaded] == ["alpaca_news:1", "alpaca_news:2"]
    assert read_jsonl(tmp_path / "missing.jsonl") == []


def test_exact_duplicates_from_two_writers_are_read_once(tmp_path: Path) -> None:
    store = IntelStore(root_of(tmp_path))
    record = news(1, "Acme Opens A New Office", NOW)
    row = StoredRecord(record.id, 1, NOW, record.content_hash(), "k", record).to_json()
    append_lines(store.records_file("news", "2026-11"), [row, row, ["not", "an", "object"]])  # type: ignore[list-item]
    assert len(store.load("news", since=NOW - timedelta(days=1), until=NOW)) == 1


def test_one_writer_at_a_time_and_a_dead_writers_lock_is_taken_over(tmp_path: Path) -> None:
    store = IntelStore(root_of(tmp_path))
    lock = store.root / LOCK_NAME
    with store.writer_lock(NOW):
        assert json.loads(lock.read_text(encoding="utf-8"))["at"] == "2026-11-10T14:00:00Z"
        second = store.writer_lock(NOW + timedelta(minutes=14))
        with pytest.raises(StoreBusy, match="another intel refresh is running"), second:
            pass
        assert lock.exists()  # the refused writer did not remove the holder's lock
    assert not lock.exists()

    lock.write_text('{"pid": 1, "at": "2026-11-10T13:00:00Z"}\n', encoding="utf-8")
    with store.writer_lock(NOW):  # an hour old: its writer died
        assert json.loads(lock.read_text(encoding="utf-8"))["at"] == "2026-11-10T14:00:00Z"
    assert not lock.exists()

    with pytest.raises(RuntimeError, match="boom"), store.writer_lock(NOW):
        raise RuntimeError("boom")
    assert not lock.exists()


def test_snapshots_and_runs_are_appended_under_their_own_folders(tmp_path: Path) -> None:
    store = IntelStore(root_of(tmp_path))
    snap = snapshot([], NVDA, NOW, statuses=OK)
    store.append_snapshot(snap)
    store.append_run({"at": "2026-11-10T14:00:00Z", "sources": OK, "new": 0})
    assert read_jsonl(store.snapshot_file("stocks:NVDA", "2026-11")) == [snap]
    assert store.snapshot_file("stocks:NVDA", "2026-11").relative_to(store.root).as_posix() == (
        "snapshots/stocks/NVDA/2026-11.jsonl"
    )
    assert read_jsonl(store.root / "runs" / "2026-11.jsonl")[0]["new"] == 0
    for broken in ({"id": "stocks:NVDA"}, {"id": "stocks:NVDA", "as_of": "soon"}):
        with pytest.raises(StoreError):
            store.append_snapshot(broken)
    with pytest.raises(StoreError):
        store.append_run({"sources": OK})


# ----------------------------------------------------------------------------- snapshot
def stored(tmp_path: Path, batches: list[tuple[datetime, list[IntelRecord]]]) -> list[StoredRecord]:
    store = IntelStore(root_of(tmp_path))
    latest: dict[str, StoredRecord] = {}
    rows: list[StoredRecord] = []
    for seen_at, records in batches:
        rows += store.append(records, seen_at=seen_at, latest=latest)
    return rows


def test_a_snapshot_counts_only_what_had_been_seen_by_then(tmp_path: Path) -> None:
    hour = timedelta(hours=1)
    rows = stored(
        tmp_path,
        [
            (
                NOW - 30 * hour,
                [news(1, "Acme Opens A New Office", NOW - 31 * hour)],  # older than a day
            ),
            (
                NOW - 5 * hour,
                [news(2, "Analyst Lifts Acme Price Target", NOW - 6 * hour)],
            ),
            (
                NOW - hour,
                [
                    news(3, "Acme Q3 Earnings Beat Estimates", NOW - hour),
                    news(4, "ACME Q3 earnings beat estimates!", NOW - hour),  # syndicated copy
                    news(5, "Gold Fund Sees Inflows", NOW - hour, targets=("cfd:GOLD",)),
                ],
            ),
            (
                NOW + hour,
                [news(6, "Acme Sued Over Patent", NOW - 2 * hour)],  # published before, seen after
            ),
        ],
    )
    snap = snapshot(rows, NVDA, NOW, statuses=OK)
    assert snap["version"] == SNAPSHOT_VERSION and snap["id"] == "stocks:NVDA"
    assert snap["as_of"] == "2026-11-10T14:00:00Z" and snap["sources"] == OK
    assert snap["news"]["count_2h"] == 1 and snap["news"]["count_24h"] == 2
    assert snap["news"]["categories_24h"] == {"analyst": 1, "earnings": 1}
    assert snap["news"]["last_at"] == "2026-11-10T13:00:00Z" and snap["news"]["proxy"] is None
    assert [i["id"] for i in snap["news"]["latest"]] == [
        "alpaca_news:3",
        "alpaca_news:2",
        "alpaca_news:1",
    ]
    # Asked again after the late item arrived, the past moment gives the same answer...
    assert snapshot(rows, NVDA, NOW, statuses=OK) == snap
    # ...and a later moment sees it. Being seen late does not make it fresh: the windows use
    # the publication time, so a backlog read after an outage is not counted as breaking news.
    later = snapshot(rows, NVDA, NOW + 2 * hour, statuses=OK)
    assert later["news"]["count_2h"] == 0 and later["news"]["count_24h"] == 3
    assert [i["id"] for i in later["news"]["latest"]] == [
        "alpaca_news:3",
        "alpaca_news:6",
        "alpaca_news:2",
    ]
    assert later["news"]["latest"][1]["at"] == "2026-11-10T12:00:00Z"
    assert later["hash"] != snap["hash"]


def test_a_snapshot_uses_the_revision_that_was_current_then(tmp_path: Path) -> None:
    before, after = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
    rows = stored(
        tmp_path,
        [
            (before, [news(1, "Acme Opens A New Office", before)]),
            (after, [news(1, "Acme Sued Over New Office", before)]),
        ],
    )
    then = snapshot(rows, NVDA, NOW - timedelta(hours=2), statuses=OK)
    now = snapshot(rows, NVDA, NOW, statuses=OK)
    assert then["news"]["latest"][0]["title"] == "Acme Opens A New Office"
    assert then["news"]["categories_24h"] == {"unknown": 1}
    assert now["news"]["latest"][0]["title"] == "Acme Sued Over New Office"
    assert now["news"]["count_24h"] == 1 and now["news"]["categories_24h"] == {
        "legal_regulatory": 1
    }
    assert [r.rev for r in visible(rows, NOW)] == [2]


def test_insider_values_are_summed_exactly_over_the_window(tmp_path: Path) -> None:
    day = timedelta(days=1)
    rows = stored(
        tmp_path,
        [
            (
                NOW - 100 * day,
                [insider("0001-26-000001", NOW - 100 * day, buy_value="999", buy_shares="9")],
            ),
            (
                NOW - 10 * day,
                [
                    insider(
                        "0001-26-000002",
                        NOW - 10 * day,
                        buy_value="176520.10",
                        buy_shares="1000",
                        sell_value="0",
                        sell_shares="0",
                    )
                ],
            ),
            (
                NOW - day,
                [
                    insider(
                        "0001-26-000003",
                        NOW - day,
                        buy_value="0",
                        buy_shares="0",
                        sell_value="50000.05",
                        sell_shares="300",
                    )
                ],
            ),
        ],
    )
    got = snapshot(rows, NVDA, NOW, statuses=OK)["insiders"]
    assert got["window_days"] == 90 and got["filings"] == 2
    assert (got["buys"], got["sells"]) == (1, 1)
    assert got["buy_value"] == "176520.1" and got["sell_value"] == "50000.05"
    assert got["net_value"] == "126520.05"
    assert got["last_at"] == "2026-11-09T14:00:00Z"
    assert [i["id"] for i in got["latest"]] == [
        "edgar_form4:0001-26-000003",
        "edgar_form4:0001-26-000002",
    ]
    wide = snapshot(rows, NVDA, NOW, statuses=OK, insider_days=120)["insiders"]
    assert wide["filings"] == 3 and wide["buy_value"] == "177519.1"


def test_the_newest_pre_market_session_is_reported(tmp_path: Path) -> None:
    rows = stored(
        tmp_path,
        [
            (
                NOW - timedelta(days=1),
                [premarket("2026-11-09", NOW - timedelta(days=1), last="170", gap_pct="-1.2")],
            ),
            (
                NOW - timedelta(minutes=30),
                [premarket("2026-11-10", NOW, last="176", gap_pct="0.5", prior_close="175.12")],
            ),
            (
                NOW - timedelta(minutes=5),
                [
                    premarket(
                        "2026-11-10",
                        NOW,
                        last="177.5",
                        gap_pct="1.36",
                        prior_close="175.12",
                        volume="1200000",
                        high="178",
                        low="175.5",
                        bars=58,
                        through="2026-11-10T13:40:00Z",
                    )
                ],
            ),
        ],
    )
    got = snapshot(rows, NVDA, NOW, statuses=OK)["premarket"]
    assert got == {
        "session": "2026-11-10",
        "prior_close": "175.12",
        "last": "177.5",
        "gap_pct": "1.36",
        "volume": "1200000",
        "high": "178",
        "low": "175.5",
        "bars": 58,
        "through": "2026-11-10T13:40:00Z",
    }
    earlier = snapshot(rows, NVDA, NOW - timedelta(minutes=10), statuses=OK)["premarket"]
    assert earlier["last"] == "176" and earlier["volume"] is None
    assert snapshot([], NVDA, NOW, statuses=OK)["premarket"] == {"session": None}


def test_parts_that_do_not_apply_are_absent_and_the_hash_ignores_the_time(tmp_path: Path) -> None:
    rows = stored(
        tmp_path,
        [
            (
                NOW - timedelta(hours=30),
                [news(5, "Gold Fund Sees Inflows", NOW, targets=("cfd:GOLD",))],
            )
        ],
    )
    gold = snapshot(rows, GOLD, NOW, statuses=OK)
    assert gold["insiders"] is None and gold["premarket"] is None
    assert gold["news"]["proxy"] == "GLD, a gold fund" and gold["news"]["count_24h"] == 0
    assert gold["news"]["latest"][0]["id"] == "alpaca_news:5"
    bare = snapshot(rows, DE40, NOW, statuses=OK)
    assert bare["news"] is None and bare["insiders"] is None and bare["premarket"] is None

    minute_later = snapshot(rows, GOLD, NOW + timedelta(minutes=1), statuses=OK)
    assert minute_later["hash"] == gold["hash"] and minute_later["as_of"] != gold["as_of"]
    waiting = snapshot(rows, GOLD, NOW, statuses={**OK, "edgar_form4": "waiting"})
    assert waiting["hash"] != gold["hash"]
