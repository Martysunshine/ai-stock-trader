"""The entry spread is the market's, not one venue's (ADR-0061).

On 2026-11-11 the IEX book of GOOGL was 292.00 / 292.98 (33.5 bps) while the consolidated one
was 292.25 / 292.30 (1.7 bps), and the paper broker fills at the consolidated price; ten of
nineteen practice signals were refused as SPREAD_TOO_WIDE on the IEX spread alone.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.test_paper_runtime import DAY, FAST, FakeClock, FakeMarketData, _env
from tests.test_risk_engine import ctx, quote
from trader.config.alpaca_keys import AlpacaCredentials
from trader.config.policy import LoadedPolicy
from trader.data.history import DataQualityError
from trader.data.sources.alpaca_history import fetch_alpaca_recent_spreads_bps
from trader.data.sources.http import TransportError
from trader.domain.candidates import RejectReason
from trader.risk.engine import VENUE_SPREAD_CEILING_BPS, entry_spread_bps, evaluate_entry
from trader.runtime.paper import PaperRunner
from trader.runtime.paper_data import SIP_DELAY, SIP_WINDOW, DelayedSipSpreads, median_decimal
from trader.storage.db import Database

NOW = datetime(2026, 11, 11, 16, 25, 7, tzinfo=UTC)
CREDS = AlpacaCredentials("key", "secret")
WIDE_VENUE = {"bid": "101.00", "ask": "101.24"}  # 23.7 bps, over base.yaml's 10 bps


def _rows(n: int, bid: str = "292.25", ask: str = "292.30") -> list[dict[str, Any]]:
    return [{"bp": bid, "ap": ask, "bs": 40, "as": 120, "t": "2026-11-11T16:09:06Z"}] * n


class FakeTransport:
    def __init__(self, rows: Mapping[str, list[dict[str, Any]]], *, fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def get_json(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> Any:
        self.calls.append(dict(params))
        if self.fail:
            raise TransportError("HTTP 403")
        symbol = params["symbols"]
        return {"quotes": {symbol: list(self.rows.get(symbol, []))}, "next_page_token": None}


# ---------------------------------------------------------------- the source
def test_fetch_asks_for_delayed_consolidated_quotes_newest_first() -> None:
    t = FakeTransport({"GOOGL": [*_rows(2), {"bp": 0, "ap": "1"}, {"bp": "2", "ap": "1"}]})
    end = NOW - SIP_DELAY
    spreads = fetch_alpaca_recent_spreads_bps(
        t, CREDS, "GOOGL", start=end - SIP_WINDOW, end=end, feed="sip", limit=200
    )
    assert len(spreads) == 2  # the one-sided and the crossed quote are left out
    assert spreads[0] == pytest.approx(Decimal("1.7108"), abs=Decimal("0.0001"))
    assert t.calls == [
        {
            "symbols": "GOOGL",
            "start": "2026-11-11T15:54:07Z",
            "end": "2026-11-11T16:09:07Z",
            "feed": "sip",
            "limit": "200",
            "sort": "desc",
        }
    ]


def test_fetch_refuses_an_unexpected_payload() -> None:
    class Bad(FakeTransport):
        def get_json(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> Any:
            return {"quotes": {"GOOGL": "nope"}}

    with pytest.raises(DataQualityError):
        fetch_alpaca_recent_spreads_bps(
            Bad({}), CREDS, "GOOGL", start=NOW, end=NOW, feed="sip", limit=10
        )


def test_median_of_an_even_and_an_odd_count() -> None:
    assert median_decimal([Decimal(3), Decimal(1), Decimal(2)]) == 2
    assert median_decimal([Decimal(4), Decimal(1), Decimal(2), Decimal(3)]) == Decimal("2.5")


def test_estimates_are_cached_for_the_refresh_interval_then_fetched_again() -> None:
    t = FakeTransport({"GOOGL": _rows(30)})
    ref = DelayedSipSpreads(t, CREDS, refresh=timedelta(minutes=5))
    first = ref.spreads_bps(["GOOGL"], now=NOW)
    assert first["GOOGL"] == pytest.approx(Decimal("1.7108"), abs=Decimal("0.0001"))
    ref.spreads_bps(["GOOGL"], now=NOW + timedelta(minutes=4))
    assert len(t.calls) == 1
    ref.spreads_bps(["GOOGL"], now=NOW + timedelta(minutes=5))
    assert len(t.calls) == 2


def test_too_few_quotes_or_a_failed_request_give_no_estimate() -> None:
    assert (
        DelayedSipSpreads(FakeTransport({"GOOGL": _rows(19)}), CREDS).spreads_bps(
            ["GOOGL"], now=NOW
        )
        == {}
    )
    t = FakeTransport({"GOOGL": _rows(30)})
    ref = DelayedSipSpreads(t, CREDS, refresh=timedelta(minutes=5))
    assert "GOOGL" in ref.spreads_bps(["GOOGL"], now=NOW)
    t.fail = True
    # a failed refresh drops the old estimate: the check falls back to the venue quote
    assert ref.spreads_bps(["GOOGL"], now=NOW + timedelta(minutes=5)) == {}


def test_many_symbols_are_fetched_side_by_side_once_each() -> None:
    """Twenty stocks one after another took 14 s on 2026-11-11, longer than a quote may age."""
    symbols = [f"S{i:02d}" for i in range(20)]
    rows = {s: _rows(30) for s in symbols if s != "S07"}  # S07 has no quotes: no estimate
    started = threading.Barrier(4, timeout=5)

    class Slow(FakeTransport):
        def get_json(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> Any:
            if params["symbols"] in ("S00", "S01", "S02", "S03"):
                started.wait()  # passes only when four requests are in flight together
            return super().get_json(url, params, headers)

    t = Slow(rows)
    ref = DelayedSipSpreads(t, CREDS, workers=4)
    out = ref.spreads_bps([*symbols, "S00"], now=NOW)
    assert list(out) == [s for s in symbols if s != "S07"]  # the caller's order, no duplicate
    assert sorted(c["symbols"] for c in t.calls) == symbols  # one request per symbol
    ref.spreads_bps(symbols, now=NOW + timedelta(minutes=1))
    assert len(t.calls) == 21  # only the symbol without an estimate is asked again
    with pytest.raises(ValueError, match="workers"):
        DelayedSipSpreads(t, CREDS, workers=0)


# ---------------------------------------------------------------- the risk check
def test_a_wide_venue_quote_is_refused_without_a_market_estimate(policy: LoadedPolicy) -> None:
    d = evaluate_entry(ctx(policy, quote=quote(**WIDE_VENUE)))
    assert RejectReason.SPREAD_TOO_WIDE in d.reasons


def test_a_tight_market_admits_a_wide_venue_quote_and_costs_the_market_spread(
    policy: LoadedPolicy,
) -> None:
    c = ctx(policy, quote=quote(**WIDE_VENUE), market_spread_bps=Decimal("1.5"))
    assert entry_spread_bps(c) == Decimal("1.5")
    d = evaluate_entry(c)
    assert d.approved, d.details
    assert d.limit_price == Decimal("101.29")  # still bounded by the venue's ask
    tight = evaluate_entry(ctx(policy, quote=quote(bid="101.2347", ask="101.25")))
    assert d.sizing is not None and tight.sizing is not None
    assert d.sizing.spread_cost_usd < tight.sizing.spread_cost_usd * 2


def test_a_wide_market_is_still_refused(policy: LoadedPolicy) -> None:
    d = evaluate_entry(ctx(policy, quote=quote(**WIDE_VENUE), market_spread_bps=Decimal("12")))
    assert RejectReason.SPREAD_TOO_WIDE in d.reasons
    assert any("market spread 12.00 bps" in x for x in d.details)


def test_the_venue_quote_is_used_when_it_is_tighter_than_the_estimate(
    policy: LoadedPolicy,
) -> None:
    c = ctx(policy, market_spread_bps=Decimal("30"))
    assert entry_spread_bps(c) == c.quote.spread_bps  # type: ignore[union-attr]
    assert evaluate_entry(c).approved


@pytest.fixture
def lite(tmp_path: Path) -> Iterator[Database]:
    db = Database(f"sqlite:///{(tmp_path / 'paper.db').as_posix()}")
    db.create_schema()
    yield db
    db.dispose()


class FakeReference:
    source = "fake"

    def __init__(self, values: dict[str, Decimal]) -> None:
        self.values = values
        self.calls: list[tuple[tuple[str, ...], datetime]] = []

    def spreads_bps(self, symbols: Sequence[str], *, now: datetime) -> dict[str, Decimal]:
        self.calls.append((tuple(symbols), now))
        return {s: v for s, v in self.values.items() if s in symbols}


def test_the_runner_refreshes_estimates_at_each_boundary_and_drops_missing_ones(
    lite: Database,
) -> None:
    clock = FakeClock(DAY.replace(hour=20))
    env = _env(lite, clock)
    ref = FakeReference({"AAPL": Decimal("0.6")})
    runner = PaperRunner(
        env, market_data=FakeMarketData(clock), schedule=FAST, spread_reference=ref
    )
    env.market.market_spreads["XNAS:MSFT"] = Decimal("9")  # an old estimate
    runner.refresh_market_spreads(["AAPL", "MSFT"])
    assert env.market.market_spreads == {"XNAS:AAPL": Decimal("0.6")}
    runner.run(sessions=1)
    env.close()
    assert len(ref.calls) >= 12  # every bar boundary of the last hour asked for estimates


def test_a_broken_venue_book_is_refused_even_with_a_tight_market(policy: LoadedPolicy) -> None:
    broken = quote(bid="96.00", ask="101.24")  # ~531 bps
    assert broken.spread_bps > VENUE_SPREAD_CEILING_BPS
    d = evaluate_entry(ctx(policy, quote=broken, market_spread_bps=Decimal("1.5")))
    assert RejectReason.SPREAD_TOO_WIDE in d.reasons
    assert any("ceiling" in x for x in d.details)
