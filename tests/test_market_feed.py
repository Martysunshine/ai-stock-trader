"""The dashboard's market feed (ADR-0045): files and index, merge and trim, failure isolation,
the Capital.com back-off, display flags, the write boundary and the import budget.

Every source runs against a fake transport; no test touches the network or the real data/market.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.config.alpaca_keys import AlpacaCredentials
from trader.config.secrets import CapitalDemoCredentials, SecretsError
from trader.data.history import HistoryBar
from trader.data.sources.capital_prices import AUTH_BACKOFF, CapitalPrices
from trader.data.sources.http import TransportError
from trader.domain.types import iso_utc
from trader.market import feed
from trader.market.__main__ import main as market_main
from trader.market.feed import (
    FeedError,
    check_root,
    read_index,
    refresh,
    run_watch,
    seconds_to_next_run,
)
from trader.market.sources import (
    AlpacaIexSource,
    CapitalDemoSource,
    FetchResult,
    GroupSource,
    KrakenPublicSource,
    StoredSeries,
)
from trader.market.watchlists import GroupConfig, SeriesName, Watchlists, load_watchlists

NOW = datetime(2026, 11, 6, 21, 30, tzinfo=UTC)  # a Friday, after the New York close (EST)
CONFIG = Watchlists.model_validate(
    {
        "every_minutes": 5,
        "retention_days": {"5m": 2, "1h": 10, "1d": 30},
        "groups": [
            {
                "id": "stocks",
                "title": "Stocks",
                "source": "alpaca_iex",
                "source_label": "Alpaca IEX",
                "day_rule": "nyse",
                "instruments": [
                    {"symbol": "AAPL", "name": "Apple"},
                    {"symbol": "SPY", "name": "SPDR S&P 500 ETF"},
                ],
            },
            {
                "id": "crypto",
                "title": "Crypto",
                "source": "kraken_public",
                "source_label": "Kraken public OHLC",
                "day_rule": "utc",
                "instruments": [
                    {"symbol": "BTC", "name": "Bitcoin", "source_id": "XBTUSD", "decimals": 1},
                    {"symbol": "DOGE", "name": "Dogecoin", "source_id": "XDGUSD", "decimals": 7},
                ],
            },
            {
                "id": "cfd",
                "title": "CFDs",
                "source": "capital_demo",
                "source_label": "Capital.com demo",
                "day_rule": "utc",
                "instruments": [
                    {"symbol": "GOLD", "name": "Gold", "decimals": 2, "min_deal": "0.01"},
                    {"symbol": "EURUSD", "name": "EUR/USD", "decimals": 5, "min_deal": "100"},
                ],
            },
        ],
    }
)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.t = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ----------------------------------------------------------------------------- fake sources
def alpaca_row(t: str, base: int) -> dict[str, Any]:
    return {
        "t": t,
        "o": Decimal(base),
        "h": Decimal(base + 2),
        "l": Decimal(base - 1),
        "c": Decimal(base + 1),
        "v": 100,
        "vw": Decimal(base) + Decimal("0.5"),
        "n": 7,
    }


#: Friday 2026-11-06, EST: 09:30 New York = 14:30 UTC, 16:00 = 21:00 UTC.
ALPACA_ROWS = {
    "5Min": ["14:25", "14:30", "14:35", "20:55", "21:00"],  # pre-market and after-hours at the ends
    "30Min": ["14:00", "14:30", "15:00", "15:30", "20:30", "21:00"],
    "1Day": ["05:00"],  # Alpaca stamps a daily bar 00:00 New York
}


class FakeAlpaca:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        assert url == "https://data.alpaca.markets/v2/stocks/bars"
        p = dict(params or {})
        self.calls.append((p, dict(headers or {})))
        times = ALPACA_ROWS[p["timeframe"]]
        return {
            "bars": {
                s: [alpaca_row(f"2026-11-06T{t}:00Z", 100 + i) for i, t in enumerate(times)]
                for s in p["symbols"].split(",")
            },
            "next_page_token": None,
        }


class FakeKraken:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.calls: list[dict[str, str]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        assert url == "https://api.kraken.com/0/public/OHLC" and not headers
        p = dict(params or {})
        self.calls.append(p)
        if p["pair"] in self.failing:
            return {"error": ["EQuery:Unknown asset pair"], "result": {}}
        step = int(p["interval"]) * 60
        last = int(NOW.timestamp()) // step * step  # the forming frame, dropped by the fetcher
        rows = [
            [last - k * step, "0.1234567", "0.2", "0.1", "0.15", "0.14", "12.5", 3]
            for k in (3, 2, 1, 0)
        ]
        return {"error": [], "result": {f"X{p['pair']}": rows, "last": last}}


def capital_payload(minutes: int, count: int) -> dict[str, Any]:
    step = minutes * 60
    last = int(NOW.timestamp()) // step * step
    prices = []
    for k in range(count - 1, -1, -1):
        t = datetime.fromtimestamp(last - k * step, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
        side = {"bid": Decimal("1.10001"), "ask": Decimal("1.10004")}
        prices.append(
            {
                "snapshotTime": t,
                "snapshotTimeUTC": t,
                "openPrice": side,
                "highPrice": side,
                "lowPrice": side,
                "closePrice": side,
                "lastTradedVolume": 99,
            }
        )
    return {"prices": prices, "instrumentType": "CURRENCIES"}


class FakeCapital:
    def __init__(self, deny: bool = False) -> None:
        self.deny = deny
        self.sessions = 0
        self.gets: list[dict[str, str]] = []

    def open_session(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> tuple[Any, dict[str, str]]:
        self.sessions += 1
        return {}, {"cst": "c", "x-security-token": "x"}

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        p = dict(params or {})
        self.gets.append(p)
        if self.deny:
            raise TransportError(f"HTTP 401 from {url}", status=401)
        minutes = {"MINUTE_5": 5, "HOUR": 60, "DAY": 1440}[p["resolution"]]
        return capital_payload(minutes, 3 if "to" not in p else 0)


def real_like_sources(
    clock: Clock,
    *,
    kraken: FakeKraken | None = None,
    capital: FakeCapital | None = None,
    alpaca: FakeAlpaca | None = None,
) -> dict[str, GroupSource]:
    creds = CapitalDemoCredentials("k", "id", "pw")
    return {
        "alpaca_iex": AlpacaIexSource(
            alpaca or FakeAlpaca(), lambda: AlpacaCredentials("PKFAKE", "secret")
        ),
        "kraken_public": KrakenPublicSource(kraken or FakeKraken()),
        "capital_demo": CapitalDemoSource(
            CapitalPrices(
                capital or FakeCapital(), lambda: creds, clock=clock, sleep=lambda _s: None
            )
        ),
    }


class Scripted:
    """A group source that returns prepared bars, or raises."""

    def __init__(
        self, bars: dict[str, dict[SeriesName, list[HistoryBar]]] | None = None, error: str = ""
    ) -> None:
        self.bars = bars or {}
        self.error = error
        self.calls = 0

    def fetch(
        self,
        group: GroupConfig,
        stored: Mapping[str, StoredSeries],
        *,
        now: datetime,
        retention: Mapping[SeriesName, timedelta],
    ) -> FetchResult:
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return FetchResult(bars=self.bars)


def bar(start: datetime, close: str, minutes: int = 5) -> HistoryBar:
    c = Decimal(close)
    return HistoryBar(start=start, minutes=minutes, open=c, high=c, low=c, close=c, volume=c)


def root_of(tmp_path: Path) -> Path:
    return tmp_path / "data" / "market"


def load(root: Path, rel: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / rel).read_text(encoding="utf-8"))
    return data


# ----------------------------------------------------------------------------- tests
def test_refresh_writes_one_file_per_instrument_and_the_index_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    written: list[str] = []
    real_write = feed.write_json_atomic

    def spy(path: Path, payload: Any, *, indent: int | None = None) -> None:
        written.append(path.relative_to(root).as_posix())
        real_write(path, payload, indent=indent)

    monkeypatch.setattr(feed, "write_json_atomic", spy)
    index = refresh(CONFIG, root, real_like_sources(clock), clock=clock)

    assert written[-1] == "index.json" and len(written) == 7
    assert index == read_index(root)
    assert index["version"] == 1
    assert index["generated_at"] == "2026-11-06T21:30:00Z"
    assert index["every_minutes"] == 5
    groups = {g["id"]: g for g in index["groups"]}
    assert list(groups) == ["stocks", "crypto", "cfd"]
    for g in groups.values():
        assert g["status"] == "ok" and g["error"] is None
        assert g["updated_at"] == g["checked_at"] == "2026-11-06T21:30:00Z"
    assert (groups["stocks"]["source"], groups["stocks"]["day_rule"]) == ("Alpaca IEX", "nyse")
    assert (groups["cfd"]["source"], groups["cfd"]["day_rule"]) == ("Capital.com demo", "utc")
    ids = [i["id"] for g in index["groups"] for i in g["instruments"]]
    assert ids == [
        "stocks:AAPL",
        "stocks:SPY",
        "crypto:BTC",
        "crypto:DOGE",
        "cfd:GOLD",
        "cfd:EURUSD",
    ]
    btc = groups["crypto"]["instruments"][0]
    assert btc["file"] == "crypto/BTC.json" and btc["source_symbol"] == "XBTUSD"
    data = load(root, "crypto/BTC.json")
    assert data["id"] == "crypto:BTC" and set(data["series"]) == {"5m", "1h", "1d"}
    first = data["series"]["5m"][0]
    assert isinstance(first[0], int) and all(isinstance(v, str) for v in first[1:])
    assert first[1:] == ["0.1234567", "0.2", "0.1", "0.15", "12.5"]  # decimals kept as text
    assert btc["series"]["5m"] == {
        "first": first[0],
        "last": data["series"]["5m"][-1][0],
        "count": 3,  # Kraken's forming frame is dropped
    }
    assert (btc["last_price"], btc["last_at"]) == ("0.15", data["series"]["5m"][-1][0])
    assert (root / "index.json").read_bytes().count(b"\r") == 0  # LF only


def test_stock_bars_keep_the_regular_session_and_hours_start_at_the_open(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    alpaca = FakeAlpaca()
    refresh(CONFIG, root, real_like_sources(clock, alpaca=alpaca), clock=clock)
    series = load(root, "stocks/AAPL.json")["series"]

    def t(hm: str) -> int:
        return int(utc(f"2026-11-06T{hm}").timestamp())

    assert [r[0] for r in series["5m"]] == [t("14:30"), t("14:35"), t("20:55")]
    # 30-minute bars 14:30+15:00 and 15:30 become the 09:30 and 10:30 New York hours.
    assert [r[0] for r in series["1h"]] == [t("14:30"), t("15:30"), t("20:30")]
    assert series["1h"][0][1:] == ["101", "104", "100", "103", "200"]
    assert [r[0] for r in series["1d"]] == [t("14:30")]  # re-stamped to the session open
    timeframes = [p["timeframe"] for p, _ in alpaca.calls]
    assert timeframes == ["5Min", "30Min", "1Day"]
    for params, headers in alpaca.calls:
        assert params["symbols"] == "AAPL,SPY" and params["feed"] == "iex"
        assert headers == {"APCA-API-KEY-ID": "PKFAKE", "APCA-API-SECRET-KEY": "secret"}


def test_incremental_refresh_merges_by_bar_start_and_trims_to_retention(tmp_path: Path) -> None:
    config = Watchlists.model_validate(
        {**CONFIG.model_dump(), "groups": [CONFIG.group("crypto").model_dump()]}
    )
    root = root_of(tmp_path)
    t0 = NOW
    first = Scripted(
        {
            "BTC": {
                "5m": [bar(t0 - timedelta(minutes=10), "1"), bar(t0 - timedelta(minutes=5), "2")],
                "1d": [
                    bar(t0 - timedelta(days=40), "9", 1440),
                    bar(t0 - timedelta(days=1), "8", 1440),
                ],
            }
        }
    )
    clock = Clock(t0)
    index = refresh(config, root, {"kraken_public": first}, clock=clock)
    assert [r[4] for r in load(root, "crypto/BTC.json")["series"]["1d"]] == ["8"]  # 40 d > 30 d
    assert index["groups"][0]["status"] == "ok"

    clock.t = t0 + timedelta(days=1)
    second = Scripted(
        {"BTC": {"5m": [bar(t0 - timedelta(minutes=5), "2.5"), bar(t0 + timedelta(days=1), "3")]}}
    )
    refresh(config, root, {"kraken_public": second}, clock=clock)
    rows = load(root, "crypto/BTC.json")["series"]["5m"]
    assert [r[4] for r in rows] == ["1", "2.5", "3"]  # the re-sent bar is replaced, not doubled

    clock.t = t0 + timedelta(days=2, minutes=7)  # past 2 days of 5m retention for the first two
    refresh(config, root, {"kraken_public": Scripted({"BTC": {}})}, clock=clock)
    rows = load(root, "crypto/BTC.json")["series"]["5m"]
    assert [r[4] for r in rows] == ["3"]
    summary = read_index(root)["groups"][0]["instruments"][0]["series"]["5m"]
    assert summary == {"first": rows[0][0], "last": rows[0][0], "count": 1}


def test_one_failing_group_does_not_stop_the_others(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    refresh(CONFIG, root, real_like_sources(clock), clock=clock)
    before = (root / "crypto" / "BTC.json").read_bytes()

    clock.t = NOW + timedelta(minutes=5)
    sources = real_like_sources(clock)
    sources["kraken_public"] = Scripted(error="network error for https://api.kraken.com")
    index = refresh(CONFIG, root, sources, clock=clock)
    groups = {g["id"]: g for g in index["groups"]}
    assert groups["crypto"]["status"] == "error"
    assert "network error" in groups["crypto"]["error"]
    assert groups["crypto"]["updated_at"] == "2026-11-06T21:30:00Z"  # the last good refresh
    assert groups["crypto"]["checked_at"] == "2026-11-06T21:35:00Z"
    assert groups["crypto"]["instruments"][0]["series"]["5m"]["count"] == 3  # kept files
    assert (root / "crypto" / "BTC.json").read_bytes() == before
    assert groups["stocks"]["status"] == groups["cfd"]["status"] == "ok"
    assert groups["stocks"]["updated_at"] == "2026-11-06T21:35:00Z"

    # One pair failing inside a group: the group is partial and the other pair still updates.
    clock.t = NOW + timedelta(minutes=10)
    index = refresh(
        CONFIG, root, real_like_sources(clock, kraken=FakeKraken({"XDGUSD"})), clock=clock
    )
    crypto = index["groups"][1]
    assert crypto["status"] == "partial" and crypto["error"].startswith("DOGE: Kraken error")
    assert crypto["updated_at"] == "2026-11-06T21:40:00Z"


def test_a_refused_capital_session_backs_off_for_30_minutes_across_restarts(
    tmp_path: Path,
) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    capital = FakeCapital(deny=True)
    index = refresh(CONFIG, root, real_like_sources(clock, capital=capital), clock=clock)
    cfd = index["groups"][2]
    assert cfd["status"] == "error" and "HTTP 401" in cfd["error"]
    assert cfd["retry_after"] == "2026-11-06T22:00:00Z" == iso_utc(NOW + AUTH_BACKOFF)
    assert capital.sessions == 2 and len(capital.gets) == 2  # one re-login, then stop
    assert index["groups"][0]["status"] == index["groups"][1]["status"] == "ok"

    # A restarted feed (new source objects) still honours the back-off recorded in the index.
    clock.t = NOW + timedelta(minutes=10)
    restarted = FakeCapital()
    index = refresh(CONFIG, root, real_like_sources(clock, capital=restarted), clock=clock)
    assert restarted.sessions == 0 and restarted.gets == []
    assert index["groups"][2]["status"] == "error"
    assert index["groups"][2]["retry_after"] == "2026-11-06T22:00:00Z"

    clock.t = NOW + timedelta(minutes=31)
    index = refresh(CONFIG, root, real_like_sources(clock, capital=restarted), clock=clock)
    assert restarted.sessions == 1 and index["groups"][2]["status"] == "ok"
    assert index["groups"][2]["retry_after"] is None


def test_missing_capital_keys_fail_the_cfd_group_once_and_nothing_else(tmp_path: Path) -> None:
    clock = Clock()
    capital = FakeCapital()

    def no_keys() -> CapitalDemoCredentials:
        raise SecretsError("owner action: capital-demo is incomplete (api_key missing)")

    sources = real_like_sources(clock, capital=capital)
    sources["capital_demo"] = CapitalDemoSource(
        CapitalPrices(capital, no_keys, clock=clock, sleep=lambda _s: None)
    )
    index = refresh(CONFIG, root_of(tmp_path), sources, clock=clock)
    stocks, crypto, cfd = index["groups"]
    assert stocks["status"] == crypto["status"] == "ok"
    assert cfd["status"] == "error" and cfd["retry_after"] is None
    assert cfd["error"] == "owner action: capital-demo is incomplete (api_key missing)"
    assert capital.sessions == 0 and capital.gets == []


def test_precision_volume_and_basis_flags(tmp_path: Path) -> None:
    root = root_of(tmp_path)
    clock = Clock()
    index = refresh(CONFIG, root, real_like_sources(clock), clock=clock)
    flags = {
        i["id"]: (i["precision"], i["has_volume"], i["basis"], i.get("min_deal"))
        for g in index["groups"]
        for i in g["instruments"]
    }
    assert flags == {
        "stocks:AAPL": (2, True, "trade", None),
        "stocks:SPY": (2, True, "trade", None),
        "crypto:BTC": (2, True, "trade", None),  # 1 decimal, shown with at least 2
        "crypto:DOGE": (6, True, "trade", None),  # 7 decimals, shown with at most 6
        "cfd:GOLD": (2, False, "mid", "0.01"),
        "cfd:EURUSD": (5, False, "mid", "100"),
    }
    gold = load(root, "cfd/GOLD.json")["series"]["5m"]
    assert gold and all(r[1:] == ["1.100025"] * 4 + ["0"] for r in gold)  # exact mid, no volume


def test_nothing_is_written_outside_data_market(tmp_path: Path) -> None:
    history = tmp_path / "data" / "history" / "alpaca_us_equities_5m_v1" / "XNYS__AAPL.parquet"
    manifest = tmp_path / "research" / "data_manifest.json"
    for path, text in ((history, "parquet bytes"), (manifest, '{"datasets": []}')):
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")

    def snapshot() -> dict[str, bytes]:
        return {
            p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    clock = Clock()
    for minutes in (0, 5):
        clock.t = NOW + timedelta(minutes=minutes)
        refresh(CONFIG, root_of(tmp_path), real_like_sources(clock), clock=clock)
    after = snapshot()
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed and all(k.startswith("data/market/") for k in changed)
    assert not [k for k in after if k.endswith(".tmp")]  # temporary files are always renamed
    for bad in (tmp_path / "data" / "history", tmp_path / "data" / "history" / "market"):
        with pytest.raises(FeedError):
            check_root(bad)
    with pytest.raises(FeedError):
        refresh(CONFIG, tmp_path / "research", real_like_sources(clock), clock=clock)
    with pytest.raises(FeedError, match="unknown group"):
        refresh(CONFIG, root_of(tmp_path), {}, clock=clock, groups=["bonds"])


def test_a_group_limited_refresh_leaves_the_other_groups_alone(tmp_path: Path) -> None:
    clock = Clock()
    kraken = FakeKraken()
    index = refresh(
        CONFIG,
        root_of(tmp_path),
        real_like_sources(clock, kraken=kraken),
        clock=clock,
        groups=["stocks"],
    )
    assert [g["status"] for g in index["groups"]] == ["ok", "error", "error"]
    assert "not refreshed yet" in index["groups"][1]["error"] and kraken.calls == []
    first_stocks = index["groups"][0]
    clock.t = NOW + timedelta(minutes=5)
    index = refresh(
        CONFIG, root_of(tmp_path), real_like_sources(clock), clock=clock, groups=["crypto"]
    )
    assert index["groups"][0] == first_stocks  # carried over untouched
    assert index["groups"][1]["status"] == "ok"


def test_the_feed_imports_neither_the_heavy_stack_nor_any_order_authority() -> None:
    probe = (
        "import sys, trader.market.feed, trader.market.__main__; "
        "heavy = ('pandas', 'duckdb', 'exchange_calendars', 'sqlalchemy', 'numpy'); "
        "authority = ('trader.brokers', 'trader.execution', 'trader.risk', 'trader.runtime', "
        "'trader.storage'); "
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in heavy "
        "or m.startswith(authority))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"the market feed imported: {out}"


def test_watch_refreshes_shortly_after_each_cadence_boundary(tmp_path: Path) -> None:
    assert seconds_to_next_run(utc("2026-11-06T21:30:05"), 5) == 10  # this boundary's settle
    assert seconds_to_next_run(utc("2026-11-06T21:30:16"), 5) == 299
    assert seconds_to_next_run(utc("2026-11-06T21:33:00"), 5) == 135
    clock = Clock(utc("2026-11-06T21:31:00"))
    lines: list[str] = []
    source = Scripted({"BTC": {"5m": [bar(NOW, "1")]}})
    code = run_watch(
        every_minutes=5,
        root=root_of(tmp_path),
        echo=lines.append,
        clock=clock,
        sources={"alpaca_iex": source, "kraken_public": source, "capital_demo": source},
        iterations=2,
    )
    assert code == 0 and source.calls == 6
    assert clock.slept == [255.0]  # 21:31:00 -> 21:35:15
    assert "no order authority" in lines[0]
    assert lines[-1].startswith("2026-11-06T21:35:15Z market feed: stocks ok")
    with pytest.raises(FeedError):
        run_watch(every_minutes=0, root=root_of(tmp_path), clock=clock, sources={})


def test_the_owner_watchlists_have_three_groups_and_37_instruments() -> None:
    config = load_watchlists()
    assert [(g.id, g.source, g.day_rule, len(g.instruments)) for g in config.groups] == [
        ("stocks", "alpaca_iex", "nyse", 22),  # the exploration book's 20 stocks, SPY and QQQ
        ("crypto", "kraken_public", "utc", 6),
        ("cfd", "capital_demo", "utc", 9),
    ]
    assert config.retention_days == {"5m": 14, "1h": 180, "1d": 730}
    kraken = {i.symbol: i.source_symbol for i in config.group("crypto").instruments}
    assert kraken == {
        "BTC": "XBTUSD",
        "ETH": "ETHUSD",
        "SOL": "SOLUSD",
        "XRP": "XRPUSD",
        "ADA": "ADAUSD",
        "DOGE": "XDGUSD",
    }
    cfds = {i.symbol: (i.decimals, str(i.min_deal)) for i in config.group("cfd").instruments}
    assert cfds == {
        "GOLD": (2, "0.01"),
        "SILVER": (3, "1"),
        "OIL_CRUDE": (3, "1"),
        "US500": (1, "0.01"),
        "US100": (1, "0.001"),
        "DE40": (1, "0.001"),
        "EURUSD": (5, "100"),
        "BTCUSD": (2, "0.0001"),
        "ETHUSD": (2, "0.001"),
    }
    assert [g.basis for g in config.groups] == ["trade", "trade", "mid"]
    assert [g.has_volume for g in config.groups] == [True, True, False]


def test_the_command_line_refuses_an_unknown_group_before_fetching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert market_main(["refresh", "--group", "bonds"]) == 2
    assert "unknown group(s): bonds" in capsys.readouterr().err
