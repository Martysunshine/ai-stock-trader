"""Daily features ``daily-v1`` (docs/ml_research_guide.md §3; ADR-0023 extended to daily bars).

* SMA, Wilder RSI and Wilder ATR equal a direct batch recomputation with the documented
  quantisation, and match hand-computed values.
* Month ends come from the XNYS calendar.
* Weekends, holidays and early closes are not gaps; a missing session resets the state.
* A split rescales the state point-in-time and equals a batch over equally adjusted bars.
* Snapshots are exact decimals and hash deterministically.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.daily_fixtures import CALENDAR, daily_history, session_bar, sessions
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.features.daily import (
    FEATURE_VERSION,
    NEUTRAL_RSI,
    DailyFeatures,
    DailyWindows,
    divide,
    is_month_end,
    quantize,
    rescale_price,
)

WINDOWS = DailyWindows(sma_periods=(5, 200), rsi_period=2, atr_period=10, month_end_sma_months=10)


def bars_from_closes(closes: list[str], start: date = date(2017, 1, 3)) -> list[Bar]:
    out = []
    days = sessions(start, start + timedelta(days=3 * len(closes) + 10))
    for session, c in zip(days, closes, strict=False):
        price = Decimal(c)
        out.append(
            session_bar(
                "ARCX:SPY", session, price, price + Decimal("0.5"), price - Decimal("0.5"), price
            )
        )
    assert len(out) == len(closes)
    return out


# ----------------------------------------------------------------------------- batch reference


def batch_sma(closes: list[Decimal], n: int) -> Decimal:
    return divide(sum(closes[-n:], start=Decimal(0)), Decimal(n))


def batch_wilder(values: list[Decimal], n: int) -> Decimal:
    avg = divide(sum(values[:n], start=Decimal(0)), Decimal(n))
    for x in values[n:]:
        avg = divide(avg * (n - 1) + x, Decimal(n))
    return avg


def batch_rsi(closes: list[Decimal], n: int) -> Decimal:
    changes = [b - a for a, b in itertools.pairwise(closes)]
    gain = batch_wilder([max(c, Decimal(0)) for c in changes], n)
    loss = batch_wilder([max(-c, Decimal(0)) for c in changes], n)
    return NEUTRAL_RSI if gain + loss == 0 else divide(100 * gain, gain + loss)


def batch_atr(bars: list[Bar], n: int) -> Decimal:
    trs = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in itertools.pairwise(bars)
    ]
    return batch_wilder(trs, n)


def month_end_closes(bars: list[Bar]) -> list[Decimal]:
    return [b.close for b in bars if is_month_end(CALENDAR, CALENDAR.session_date_of(b.start))]


# ----------------------------------------------------------------------------- values


def test_hand_computed_values_follow_wilder_and_the_documented_quantisation() -> None:
    w = DailyWindows(sma_periods=(3,), rsi_period=2, atr_period=2)
    f = DailyFeatures(w, CALENDAR)
    for b in bars_from_closes(["10", "11", "10.5", "12"]):
        f.update(b)
    assert f.sma(3) == quantize(Decimal("33.5") / 3)  # (11 + 10.5 + 12) / 3
    # changes +1, -0.5, +1.5: seed gain (1 + 0)/2 = 0.5, loss (0 + 0.5)/2 = 0.25;
    # then gain (0.5 * 1 + 1.5)/2 = 1.0, loss (0.25 * 1 + 0)/2 = 0.125; RSI = 100 * 1 / 1.125
    assert f.rsi() == quantize(Decimal(100) / Decimal("1.125"))
    # TR (H-L = 1 each bar): max(1, |11.5-10|, |10.5-10|) = 1.5; max(1, |11-11|, |10-11|) = 1;
    # max(1, |12.5-10.5|, |11.5-10.5|) = 2. Seed (1.5 + 1)/2 = 1.25, then (1.25 + 2)/2 = 1.625
    assert f.atr() == Decimal("1.625")


def test_rsi_is_100_rising_0_falling_and_neutral_when_flat() -> None:
    w = DailyWindows(rsi_period=2)
    rising, falling, flat = (DailyFeatures(w, CALENDAR) for _ in range(3))
    for b in bars_from_closes(["10", "11", "12", "13"]):
        rising.update(b)
    for b in bars_from_closes(["13", "12", "11", "10"]):
        falling.update(b)
    for b in bars_from_closes(["10", "10", "10", "10"]):
        flat.update(b)
    assert rising.rsi() == 100
    assert falling.rsi() == 0
    assert flat.rsi() == NEUTRAL_RSI  # a stale flat series never looks oversold


def test_streaming_values_equal_a_batch_recomputation_over_the_fixture() -> None:
    fixture = daily_history(["ARCX:SPY", "XNAS:TLT"], missing=(), splits=())
    for iid, bars in fixture.bars.items():
        f = DailyFeatures(WINDOWS, CALENDAR)
        checked = 0
        for i, bar in enumerate(bars):
            f.update(bar)
            if not f.ready or i % 37:
                continue
            seen = bars[: i + 1]
            closes = [b.close for b in seen]
            assert f.sma(5) == batch_sma(closes, 5), (iid, i)
            assert f.sma(200) == batch_sma(closes, 200), (iid, i)
            assert f.rsi() == batch_rsi(closes, 2), (iid, i)
            assert f.atr() == batch_atr(seen, 10), (iid, i)
            assert f.month_end_sma() == batch_sma(month_end_closes(seen), 10), (iid, i)
            checked += 1
        assert checked > 20
        assert f.gaps == 0


def test_values_are_exact_decimals_and_the_snapshot_hashes_deterministically() -> None:
    fixture = daily_history(["ARCX:SPY"], missing=(), splits=())
    a, b = DailyFeatures(WINDOWS, CALENDAR), DailyFeatures(WINDOWS, CALENDAR)
    with pytest.raises(LookupError):
        a.snapshot()
    for bar in fixture.bars["ARCX:SPY"][:300]:
        a.update(bar)
        b.update(bar)
    snap = a.snapshot()
    assert set(snap) == {
        "close",
        "sma_5",
        "sma_200",
        "rsi_2",
        "atr_10",
        "month_end_sma_10",
        "month_end",
    }
    assert all(isinstance(v, Decimal) and v == quantize(v) for v in snap.values())
    assert content_hash({"version": FEATURE_VERSION, "features": snap}) == content_hash(
        {"version": FEATURE_VERSION, "features": b.snapshot()}
    )


def test_warmup_estimate_covers_every_configured_window() -> None:
    assert WINDOWS.warmup_sessions == 230  # 10 month ends need at most 23 x 10 sessions
    assert DailyWindows(sma_periods=(200,), rsi_period=2).warmup_sessions == 200
    fixture = daily_history(["ARCX:SPY"], missing=(), splits=())
    f = DailyFeatures(WINDOWS, CALENDAR)
    for i, bar in enumerate(fixture.bars["ARCX:SPY"]):
        f.update(bar)
        if f.ready:
            assert i + 1 <= WINDOWS.warmup_sessions
            break
    with pytest.raises(ValueError):
        DailyWindows(sma_periods=(5, 5))
    with pytest.raises(ValueError):
        DailyWindows(rsi_period=0)


# ----------------------------------------------------------------------------- calendar


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2016, 1, 29), True),  # Friday; 31 January is a Sunday
        (date(2016, 1, 28), False),
        (date(2016, 12, 30), True),  # last session of 2016
        (date(2018, 9, 28), True),  # 30 September 2018 is a Sunday
        (date(2018, 3, 29), True),  # Good Friday 30 March 2018 is a holiday
        (date(2018, 3, 28), False),
        (date(2017, 6, 30), True),
    ],
)
def test_month_end_is_the_last_session_of_the_month(day: date, expected: bool) -> None:
    assert is_month_end(CALENDAR, day) is expected


def test_weekends_holidays_and_early_closes_are_not_gaps_but_a_missing_session_resets() -> None:
    fixture = daily_history(["ARCX:XLB"])  # the default fixture misses one XLB session
    bars = fixture.bars["ARCX:XLB"]
    assert fixture.early_closes  # e.g. the day after Thanksgiving
    early = [b for b in bars if CALENDAR.session_date_of(b.start) in fixture.early_closes]
    assert early and all(b.minutes == 210 for b in early)
    f = DailyFeatures(WINDOWS, CALENDAR)
    missing_day = fixture.missing[0][1]
    for bar in bars:
        day = CALENDAR.session_date_of(bar.start)
        before = f.gaps
        f.update(bar)
        if day == CALENDAR.sessions_after(missing_day).session_date:
            assert f.gaps == before + 1 and f.count == 1 and not f.ready
        else:
            assert f.gaps == before
    assert f.gaps == 1


def test_bars_that_do_not_advance_the_session_are_refused() -> None:
    bars = bars_from_closes(["10", "11"])
    f = DailyFeatures(WINDOWS, CALENDAR)
    f.update(bars[1])
    with pytest.raises(ValueError, match="advance one session"):
        f.update(bars[0])
    with pytest.raises(ValueError, match="advance one session"):
        f.update(bars[1])


# ----------------------------------------------------------------------------- splits


def adjusted(bar: Bar, ratio: Decimal) -> Bar:
    return bar.model_copy(
        update={
            "open": rescale_price(bar.open, ratio),
            "high": rescale_price(bar.high, ratio),
            "low": rescale_price(bar.low, ratio),
            "close": rescale_price(bar.close, ratio),
        }
    )


@pytest.mark.parametrize("iid", ["ARCX:XLK", "ARCX:XLE"])  # 2-for-1 and 3-for-1
def test_a_split_rescales_the_state_and_equals_a_batch_over_adjusted_bars(iid: str) -> None:
    fixture = daily_history([iid], missing=())
    (split,) = fixture.splits
    bars = fixture.bars[iid]
    f = DailyFeatures(WINDOWS, CALENDAR)
    applied = False
    checked = 0
    for i, bar in enumerate(bars):
        day = CALENDAR.session_date_of(bar.start)
        if not applied and day >= split.ex_date:
            f.rescale(split.ratio)
            applied = True
        f.update(bar)
        if not applied or not f.ready or i % 11:
            continue
        seen = [
            adjusted(b, split.ratio) if CALENDAR.session_date_of(b.start) < split.ex_date else b
            for b in bars[: i + 1]
        ]
        closes = [b.close for b in seen]
        # sums of individually rescaled closes: exact
        assert f.sma(200) == batch_sma(closes, 200)
        assert f.sma(5) == batch_sma(closes, 5)
        assert f.month_end_sma() == batch_sma(month_end_closes(seen), 10)
        # Wilder averages were rounded on the old share basis: equal to the 12-dp quantum
        assert abs(f.atr() - batch_atr(seen, 10)) <= Decimal("1e-11")
        assert abs(f.rsi() - batch_rsi(closes, 2)) <= Decimal("1e-9")
        checked += 1
    assert applied and checked > 10
    assert f.gaps == 0  # a split is not a gap: no warm-up is lost


# ----------------------------------------------------------------------------- the fixture


def test_the_synthetic_fixture_has_dip_buy_setups_trend_changes_and_a_gap() -> None:
    """What the daily rule families need to exercise (SYNTHETIC data, never evidence)."""
    fixture = daily_history()
    windows = DailyWindows(sma_periods=(200,), rsi_period=2, month_end_sma_months=10)
    for iid in ("ARCX:SPY", "XNAS:QQQ", "ARCX:XLV"):
        f = DailyFeatures(windows, CALENDAR)
        dips = 0
        above: list[bool] = []
        for bar in fixture.bars[iid]:
            f.update(bar)
            if not f.ready:
                continue
            dips += f.close > f.sma(200) and f.rsi() < 10
            if f.is_month_end:
                above.append(f.close > f.month_end_sma())
        assert dips >= 10, iid  # RSI(2) < 10 inside an uptrend
        changes = [b for a, b in itertools.pairwise(above) if a != b]
        assert True in changes and False in changes, iid  # trend exits and re-entries
    gap_bars = [
        b
        for bars in fixture.bars.values()
        for prev, b in itertools.pairwise(bars)
        if CALENDAR.session_date_of(b.start) == fixture.gap_session
        and b.open < prev.close * Decimal("0.95")
    ]
    assert len(gap_bars) == 13  # every equity ETF gaps down; bonds and gold do not
