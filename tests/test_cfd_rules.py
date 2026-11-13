"""The CFD rules and the broker's trading calendar (ADR-0052): the index dip is the frozen
Candidate C rule, the weekly momentum rule goes long and short on a fixed weekday, and a holiday
does not reset a 200-day average."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.data.calendars import BrokerCfdCalendar, Utc247Calendar, closed_weekdays
from trader.domain.types import Direction
from trader.features.daily import DailyFeatures
from trader.strategies.cfd_rules import (
    CfdIndexDip,
    CfdIndexDipSpec,
    CfdWeeklyMomentum,
    CfdWeeklyMomentumSpec,
    build_cfd_rule,
    load_cfd_spec,
    mid_bar,
)
from trader.strategies.registry import load_spec

from .cfd_fixtures import qbar

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "src/trader/strategies/specs"
DIP = load_cfd_spec(SPECS / "cfd_index_dip_v1.yaml")
MOMENTUM = load_cfd_spec(SPECS / "cfd_crypto_weekly_momentum_v1.yaml")
US100, BTC = "CAPITAL:US100", "CAPITAL:BTCUSD"
TICKS = {US100: Decimal("0.1"), BTC: Decimal("0.01")}


def test_the_index_dip_is_candidate_c_with_the_sleeves_week() -> None:
    tested = load_spec(SPECS / "candidate_c_etf_daily_v1.yaml")
    assert isinstance(DIP, CfdIndexDipSpec)
    assert DIP.trend_sma_days == tested.trend_sma_sessions
    assert DIP.rsi_period == tested.rsi_period
    assert DIP.rsi_entry_below == tested.rsi_entry_below
    assert DIP.exit_sma_days == tested.exit_sma_sessions
    assert DIP.atr_period == tested.atr_period
    assert DIP.stop_atr_multiplier == tested.stop_atr_multiplier
    assert DIP.feature_version == tested.feature_version
    assert DIP.max_hold_hours == 168
    assert isinstance(MOMENTUM, CfdWeeklyMomentumSpec) and MOMENTUM.decision_weekday == 6
    with pytest.raises(ValueError, match="not a CFD rule"):
        load_cfd_spec(SPECS / "candidate_c_etf_daily_v1.yaml")


def test_the_broker_calendar_skips_weekends_and_short_closures_only() -> None:
    cal = BrokerCfdCalendar(frozenset({date(2026, 1, 1)}))
    sunday_open = datetime(2026, 1, 4, 23, tzinfo=UTC)
    assert cal.session_date_of(sunday_open) == date(2026, 1, 5)
    assert cal.sessions_after(date(2025, 12, 31)).session_date == date(2026, 1, 2)
    assert cal.sessions_after(date(2026, 1, 2)).session_date == date(2026, 1, 5)
    assert cal.sessions_after(date(2026, 1, 2), 3).session_date == date(2026, 1, 7)
    # a one-day and a two-day closure are holidays; five missing weekdays are a hole
    days = [date(2026, 1, 5) + timedelta(days=n) for n in range(40)]
    present = [
        d
        for d in days
        if d.weekday() < 5
        and d not in {date(2026, 1, 7), date(2026, 1, 12), date(2026, 1, 13)}
        and not date(2026, 1, 19) <= d <= date(2026, 1, 23)
    ]
    assert closed_weekdays(present) == {date(2026, 1, 7), date(2026, 1, 12), date(2026, 1, 13)}


def _daily(n: int, close: str, *, spread: str = "1") -> object:
    start = datetime(2025, 1, 6, tzinfo=UTC) + timedelta(days=n)
    c = Decimal(close)
    return qbar(start, close, str(c + 20), str(c - 20), close, half=spread)


def _weekday_bars(closes: list[str]) -> list[object]:
    out, n = [], 0
    for close in closes:
        while (date(2025, 1, 6) + timedelta(days=n)).weekday() >= 5:
            n += 1
        out.append(_daily(n, close))
        n += 1
    return out


def test_the_index_dip_buys_a_sharp_fall_in_an_uptrend_and_sells_the_bounce() -> None:
    closes = [str(20000 + 10 * k) for k in range(210)] + ["21500", "21300"]
    bars = _weekday_bars(closes)
    rule = build_cfd_rule(DIP, {US100: BrokerCfdCalendar()}, TICKS)
    assert isinstance(rule, CfdIndexDip)
    signals = [rule.on_bar(US100, b) for b in bars]  # type: ignore[arg-type]
    # the uptrend never signals; the two-day fall does, on both days
    assert all(s is None for s in signals[:210])
    last = signals[-1]
    assert signals[210] is not None and last is not None
    assert last.direction is Direction.LONG and last.target_role == "COST_GATE"
    assert last.stop < last.reference and last.max_hold_minutes == 168 * 60
    assert last.signal_id.startswith("cfd_index_dip:CAPITAL:US100:")
    assert rule.exit_signal(US100, bars[-1], Direction.LONG) is None  # type: ignore[arg-type]
    bounce = _weekday_bars([*closes, "22300"])[-1]
    rule.on_bar(US100, bounce)  # type: ignore[arg-type]
    assert rule.exit_signal(US100, bounce, Direction.LONG) == "close_above_sma_5"  # type: ignore[arg-type]
    assert rule.exit_signal(US100, bounce, Direction.SHORT) is None  # type: ignore[arg-type]


def test_the_index_dip_reads_the_same_features_as_candidate_c() -> None:
    closes = [str(20000 + 10 * k + (k % 7) * 15) for k in range(230)]
    bars = _weekday_bars(closes)
    rule = CfdIndexDip(DIP, {US100: BrokerCfdCalendar()}, TICKS)
    for b in bars:
        rule.on_bar(US100, b)  # type: ignore[arg-type]
    direct = DailyFeatures(DIP.windows(), BrokerCfdCalendar())
    for b in bars:
        direct.update(mid_bar(US100, b))  # type: ignore[arg-type]
    state = rule._state[US100]
    assert (state.rsi(), state.atr(), state.sma(200)) == (
        direct.rsi(),
        direct.atr(),
        direct.sma(200),
    )


def _crypto_days(closes: list[str], first: date = date(2025, 3, 3)) -> list[object]:
    out = []
    for n, close in enumerate(closes):
        start = datetime.combine(first + timedelta(days=n), datetime.min.time(), UTC)
        c = Decimal(close)
        out.append(qbar(start, close, str(c + 300), str(c - 300), close, half="5"))
    return out


def test_weekly_momentum_decides_on_sunday_long_or_short_by_the_week() -> None:
    # 2025-03-03 is a Monday: day 6 is the first Sunday, day 13 the second
    rising = [str(40000 + 100 * k) for k in range(14)]
    falling = [str(50000 - 100 * k) for k in range(14)]
    for closes, direction in ((rising, Direction.LONG), (falling, Direction.SHORT)):
        rule = build_cfd_rule(MOMENTUM, {BTC: Utc247Calendar()}, TICKS)
        assert isinstance(rule, CfdWeeklyMomentum)
        signals = [rule.on_bar(BTC, b) for b in _crypto_days(closes)]  # type: ignore[arg-type]
        decided = [(n, s) for n, s in enumerate(signals) if s is not None]
        assert [n for n, _ in decided] == [13]  # day 6 is not warm yet (ATR(10) needs 11 days)
        signal = decided[0][1]
        assert signal.direction is direction and signal.target_role == "EXIT"
        distance = abs(signal.reference - signal.stop)
        assert abs(signal.target - signal.reference) == pytest.approx(float(2 * distance), abs=0.02)
        assert signal.priority == abs(Decimal(closes[13]) / Decimal(closes[6]) - 1)
        assert signal.decided_at == datetime(2025, 3, 17, tzinfo=UTC)  # Monday 00:00 UTC


def test_a_missing_day_restarts_the_weekly_lookback() -> None:
    closes = [str(40000 + 100 * k) for k in range(30)]
    bars = _crypto_days(closes)
    del bars[15]  # a whole UTC day without a bar
    rule = CfdWeeklyMomentum(MOMENTUM, {BTC: Utc247Calendar()}, TICKS)  # type: ignore[arg-type]
    signals = [rule.on_bar(BTC, b) for b in bars]  # type: ignore[arg-type]
    decided_days = [b.start.date() for b, s in zip(bars, signals, strict=True) if s]  # type: ignore[attr-defined]
    # the first Sunday after the hole is not warm again; the next one, twelve days on, is
    assert decided_days == [date(2025, 3, 16), date(2025, 3, 30)]
    assert rule.gaps() == 1
