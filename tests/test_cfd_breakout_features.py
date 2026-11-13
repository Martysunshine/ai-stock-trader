"""Features of an hourly channel-break candidate (``features/cfd_breakout.py``, T-0026): what each
one measures, and that none of them reads a bar that ended after the decision."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.data.history import HistoryBar
from trader.data.quote_bars import QuoteBar
from trader.domain.types import Direction
from trader.features.cfd_breakout import (
    FEATURE_DEFINITIONS,
    FEATURE_NAMES,
    BreakoutFeatureBook,
    InsufficientHistory,
)
from trader.research.cfd_engine import CfdSignal
from trader.strategies.cfd_rules import CfdHourlyChannel, CfdHourlyChannelSpec, load_cfd_spec

from .cfd_filter_fixtures import quote_bars

ROOT = Path(__file__).resolve().parents[1]
BTC = "CAPITAL:BTCUSD"
START = datetime(2025, 1, 1, tzinfo=UTC)
END = START + timedelta(days=30)


def rule_spec() -> CfdHourlyChannelSpec:
    spec = load_cfd_spec(ROOT / "src/trader/strategies/specs/cfd_crypto_hourly_breakout_v1.yaml")
    assert isinstance(spec, CfdHourlyChannelSpec)
    return spec


def signals(bars: list[QuoteBar]) -> list[CfdSignal]:
    rule = CfdHourlyChannel(rule_spec(), {BTC: Decimal("0.01")})
    return [s for b in bars if (s := rule.on_bar(BTC, b)) is not None]


def book(bars: list[QuoteBar]) -> BreakoutFeatureBook:
    return BreakoutFeatureBook({BTC: bars}, channel_hours=24, atr_hours=24)


def features(b: BreakoutFeatureBook, s: CfdSignal) -> dict[str, float]:
    assert s.deadline is not None
    return b.features(s.instrument_id, s.decided_at, s.direction, s.deadline)


def _scaled(bar: QuoteBar, factor: str, widen: str = "0") -> QuoteBar:
    f, w = Decimal(factor), Decimal(widen)

    def side(h: HistoryBar, off: Decimal) -> HistoryBar:
        return replace(
            h,
            open=h.open * f + off,
            high=h.high * f + off,
            low=h.low * f + off,
            close=h.close * f + off,
        )

    return QuoteBar(side(bar.bid, -w), side(bar.ask, w))


@pytest.fixture(scope="module")
def bars() -> list[QuoteBar]:
    return quote_bars("BTCUSD", START, END, seed=7)


def test_the_feature_set_is_fixed_and_documented() -> None:
    assert FEATURE_NAMES == (
        "break_atr",
        "return_24h",
        "return_72h",
        "vol_ratio_24_168",
        "spread_bps",
        "direction",
        "hour_sin",
        "hour_cos",
        "hours_to_flat",
    )
    assert set(FEATURE_DEFINITIONS) == set(FEATURE_NAMES)


def test_each_feature_measures_what_it_says(bars: list[QuoteBar]) -> None:
    found = [s for s in signals(bars) if s.decided_at >= START + timedelta(days=8)]
    assert len(found) > 20, "the synthetic walk should break its channel often"
    b = book(bars)
    for s in found:
        f = features(b, s)
        assert list(f) == list(FEATURE_NAMES)
        assert all(math.isfinite(v) for v in f.values())
        # the break beyond the channel in mean true ranges is the rule's own priority
        assert f["break_atr"] == pytest.approx(float(s.priority), rel=1e-9) and f["break_atr"] > 0
        assert f["direction"] == (1.0 if s.direction is Direction.LONG else -1.0)
        assert s.deadline is not None
        assert f["hours_to_flat"] == (s.deadline - s.decided_at).total_seconds() / 3600
        h = s.decided_at.hour
        assert f["hour_sin"] == pytest.approx(math.sin(2 * math.pi * h / 24))
        assert f["hour_cos"] == pytest.approx(math.cos(2 * math.pi * h / 24))
        i = b.decision_index(BTC, s.decided_at)
        bar = bars[i]
        assert bar.end == s.decided_at
        quoted = max(bar.ask.open - bar.bid.open, bar.ask.close - bar.bid.close)
        assert f["spread_bps"] == pytest.approx(float(quoted / bar.mid_close) * 1e4)
        then = bars[i - 24].mid_close
        side = 1 if s.direction is Direction.LONG else -1
        assert f["return_24h"] == pytest.approx(side * math.log(bar.mid_close / then))
        then = bars[i - 72].mid_close
        assert f["return_72h"] == pytest.approx(side * math.log(bar.mid_close / then))
        assert f["vol_ratio_24_168"] > 0


def test_no_feature_reads_a_bar_after_the_decision(bars: list[QuoteBar]) -> None:
    found = [s for s in signals(bars) if s.decided_at >= START + timedelta(days=8)][:15]
    original = book(bars)
    for s in found:
        before = features(original, s)
        cut = original.decision_index(BTC, s.decided_at)
        # every later bar moved far away and its spread widened; then all of them removed
        moved = bars[: cut + 1] + [_scaled(b, "1.7", "500") for b in bars[cut + 1 :]]
        assert features(book(moved), s) == before
        assert features(book(bars[: cut + 1]), s) == before
        # the decision bar itself is read: changing it changes the features
        touched = [*bars[:cut], _scaled(bars[cut], "1", "50"), *bars[cut + 1 :]]
        assert features(book(touched), s)["spread_bps"] != before["spread_bps"]


def test_a_candidate_without_a_week_of_history_is_refused(bars: list[QuoteBar]) -> None:
    early = [s for s in signals(bars) if s.decided_at < START + timedelta(hours=160)]
    assert early, "the rule decides after 24 bars, before a week of history exists"
    with pytest.raises(InsufficientHistory):
        features(book(bars), early[0])
    with pytest.raises(ValueError, match="no bar ends"):
        book(bars).features(BTC, START + timedelta(minutes=30), Direction.LONG, END)
