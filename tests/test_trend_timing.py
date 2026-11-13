"""Candidate D - trend timing on month-end closes (Faber, 2007; strategies/trend_timing.py).

SYNTHETIC data only (tests/daily_fixtures.py and hand-made session bars): nothing here is
evidence about any real instrument, and no real market data is read.

* The frozen specification follows the publication and the shared daily-bar contract, and the
  family resolves through the rule registry.
* Month ends come from the XNYS calendar: a month whose last weekday is a holiday, an early
  close that is a month end, and no unscheduled closure that would move a month end in
  hindsight.
* The 10-month SMA uses only completed month-end closes; decisions never depend on later bars.
* Streaming decisions equal a batch recomputation; a split is not a trend break; a missing
  session fails closed.
* In the research engine: next-open entries after month ends only, ``SIGNAL`` exits after a
  month-end close below the SMA, the 260-session time exit, and the whole study path.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import exchange_calendars as xcals
import pytest
from pydantic import ValidationError

from tests.daily_fixtures import (
    CALENDAR,
    UNIVERSE,
    daily_history,
    daily_specs,
    daily_study,
    session_bar,
    sessions,
    study_config,
)
from trader.accounting.fees import load_fee_schedule
from trader.config.policy import load_policy
from trader.data.calendars import Utc247Calendar
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.domain.types import Track
from trader.features.daily import FEATURE_VERSION, divide, is_month_end, rescale_price
from trader.research.engine import (
    EngineConfig,
    EngineResult,
    FillAssumptions,
    InstrumentSetup,
    ResearchEngine,
    RuleTiming,
)
from trader.research.study import (
    TrialCfg,
    _g2,
    build_strategy,
    deployment_windows,
    resolve_rule,
    run_trial,
    run_windows,
    spec_family,
    summarize,
    walk_forward,
)
from trader.strategies import registry
from trader.strategies.trend_timing import (
    EXIT_BELOW_SMA,
    EXIT_SMA_UNAVAILABLE,
    FAMILY,
    TrendTiming,
    TrendTimingSpec,
)

REPO = Path(__file__).resolve().parents[1]
SPEC_FILE = "src/trader/strategies/specs/candidate_d_etf_monthly_v1.yaml"
SPEC_PATH = REPO / SPEC_FILE
#: Pinned: the frozen specification may only change as a new, separately registered trial.
SPEC_HASH = "8856a95f0529ce111f1462fbc046f91b6d1378a19c7102597aaf5e2677a69aba"
SPY, QQQ, XLB, XLK = "ARCX:SPY", "XNAS:QQQ", "ARCX:XLB", "ARCX:XLK"
CENT = Decimal("0.01")
TRIAL = TrialCfg(
    trial_id="T-9904",
    spec_file=SPEC_FILE,
    hypothesis="SYNTHETIC pipeline check of Candidate D; never a registered trial",
)
ALLOWED_EXITS = {
    f"SIGNAL:{EXIT_BELOW_SMA}",
    f"SIGNAL:{EXIT_SMA_UNAVAILABLE}",
    "STOP",
    "STOP_GAP",
    "TARGET",
    "TIME_EXIT",
    "END_OF_DATA",
}


def spec(**overrides: Any) -> TrendTimingSpec:
    loaded = registry.load_spec(SPEC_PATH, overrides or None)
    assert isinstance(loaded, TrendTimingSpec)
    return loaded


def rule(**overrides: Any) -> TrendTiming:
    built = registry.build(spec(**overrides), CALENDAR, {})
    assert isinstance(built, TrendTiming)
    return built


def day_of(bar: Bar) -> date:
    return CALENDAR.session_date_of(bar.start)


def month_end(day: date) -> bool:
    return is_month_end(CALENDAR, day)


def after_month_end(day: date) -> bool:
    """``day`` is the session right after a month-end session."""
    previous = CALENDAR.session_open_before(CALENDAR.session_on(day).open, 1)  # type: ignore[union-attr]
    return month_end(CALENDAR.session_date_of(previous))


def parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def path_bars(
    iid: str,
    start: date,
    end: date,
    step: Callable[[date, int], Decimal] = lambda _d, _k: Decimal("0.0002"),
    *,
    first: str = "100",
) -> list[Bar]:
    """Hand-made session bars: each close moves by ``step(day, k)`` (a fraction); the open is
    the previous close and the session trades 0.4% beyond the open and close (an ATR near
    0.8%, so a 4-ATR stop fits the USD 0.125 risk budget at the USD 1 minimum)."""
    out: list[Bar] = []
    close = Decimal(first)
    for k, session in enumerate(sessions(start, end)):
        o = close
        close = _q(close * (1 + step(session.session_date, k)))
        h = _q(max(o, close) * Decimal("1.004"))
        low = _q(min(o, close) * Decimal("0.996"))
        out.append(session_bar(iid, session, o, h, low, close))
    return out


def stream(
    rule_: TrendTiming, bars: Sequence[Bar], splits: Sequence[tuple[date, Decimal]] = ()
) -> dict[date, tuple[Any, str | None]]:
    """Feed bars as the engine does (split first, then ``on_bar``, then ``exit_signal`` as if
    held) and record each session's (candidate, exit reason)."""
    pending = sorted(splits)
    out: dict[date, tuple[Any, str | None]] = {}
    for bar in bars:
        day = day_of(bar)
        while pending and pending[0][0] <= day:
            rule_.on_split(bar.instrument_id, pending.pop(0)[1])
        candidate = rule_.on_bar(bar, bar.available_time)
        out[day] = (candidate, rule_.exit_signal(bar.instrument_id, bar))
    return out


# ----------------------------------------------------------------------------- batch reference


def batch_atr(bars: Sequence[Bar], n: int) -> Decimal:
    """Wilder ATR with the ``daily-v1`` quantisation (seed = mean of the first ``n`` TRs)."""
    trs = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in itertools.pairwise(bars)
    ]
    avg = divide(sum(trs[:n], start=Decimal(0)), Decimal(n))
    for x in trs[n:]:
        avg = divide(avg * (n - 1) + x, Decimal(n))
    return avg


def batch_decision(
    seen: Sequence[Bar], s: TrendTimingSpec
) -> tuple[tuple[Decimal, Decimal] | None, str | None]:
    """The rule recomputed from scratch at the last bar of ``seen``: ((stop, target) or None,
    exit reason or None)."""
    last = seen[-1]
    if not month_end(day_of(last)):
        return None, None
    closes = [b.close for b in seen if month_end(day_of(b))]
    k = s.month_end_sma_months
    if len(closes) < k or len(seen) < s.atr_period_sessions + 1:
        return None, EXIT_SMA_UNAVAILABLE
    sma = divide(sum(closes[-k:], start=Decimal(0)), Decimal(k))
    close = last.close
    if close < sma:
        return None, EXIT_BELOW_SMA
    if close == sma:
        return None, None
    atr = batch_atr(seen, s.atr_period_sessions)
    stop = ((close - s.stop_atr_multiplier * atr) / CENT).to_integral_value(ROUND_FLOOR) * CENT
    target = ((close + s.target_risk_multiple * (close - stop)) / CENT).to_integral_value(
        ROUND_CEILING
    ) * CENT
    return (stop, target), None


# ----------------------------------------------------------------------------- engine helpers

FEES = load_fee_schedule(REPO / "configs" / "fees" / "alpaca_us_equities_v2.yaml")
POLICY = load_policy(REPO / "configs" / "policy" / "base.yaml").policy
FILLS = FillAssumptions("base", Decimal("2"), Decimal("2"), Decimal("0.10"))


def engine_run(bars: list[Bar], rule_: TrendTiming, start: datetime, end: datetime) -> EngineResult:
    ids = sorted({b.instrument_id for b in bars})
    specs = daily_specs(ids)
    setups = {
        iid: InstrumentSetup(spec=specs[iid], bars=[b for b in bars if b.instrument_id == iid])
        for iid in ids
    }
    cfg = EngineConfig(
        policy=POLICY,
        fees=FEES,
        fills=FILLS,
        track=Track.STOCKS,
        calendar=CALENDAR,
        start=start,
        end=end,
        loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
        session_basis="XNYS",
    )
    return ResearchEngine(cfg, rule_, setups).run()


# ----------------------------------------------------------------------------- specification


def test_the_spec_follows_faber_and_the_shared_daily_contract() -> None:
    s = spec()
    assert s.family == FAMILY == "CANDIDATE_D"
    assert s.spec_hash == SPEC_HASH == content_hash(s.model_dump(mode="python"))
    assert "Faber" in s.source and "2007" in s.source and "10-month" in s.source
    assert s.feature_version == FEATURE_VERSION == "daily-v1"
    assert s.track is Track.STOCKS
    assert s.universe == UNIVERSE and len(s.universe) == 16  # all 16 ETFs of the design
    assert s.month_end_sma_months == 10  # the publication's parameter
    assert (s.atr_period_sessions, s.stop_atr_multiplier) == (20, Decimal(4))
    assert s.target_risk_multiple == Decimal(3)
    assert (s.holding, s.target_role, s.max_hold_sessions) == ("OVERNIGHT", "EXIT", 260)
    assert s.use_source_vwap is False
    assert s.warmup_sessions == 230  # ten month ends need at most 23 x 10 sessions
    assert s.candidate_max_hold_minutes == 52 * 7 * 24 * 60  # 260 sessions >= 52 weeks


def test_the_family_resolves_through_the_registry_and_declares_its_timing() -> None:
    assert registry.FAMILY_MODULES[FAMILY] == "trader.strategies.trend_timing"
    assert FAMILY in registry.families()
    assert registry.family_label(FAMILY) == "Candidate D (trend timing)"
    features = registry.rule_family(FAMILY).features
    assert any("month-end" in f for f in features) and any("ATR" in f for f in features)
    r = rule()
    timing = RuleTiming.of(r)
    assert timing.overnight and timing.max_hold_sessions == 260
    assert timing.target_role == "EXIT" and timing.exit_signal == r.exit_signal
    assert r.uses_source_vwap is False and r.snapshot_version == FEATURE_VERSION
    ctx, _ = daily_study(study_config([TRIAL], [SPY], warmup_sessions=230), daily_history([SPY]))
    resolved = resolve_rule(ctx, TRIAL)
    assert isinstance(resolved, TrendTimingSpec) and resolved.spec_hash == SPEC_HASH
    assert spec_family(resolved) == FAMILY
    assert isinstance(build_strategy(ctx, resolved, {}), TrendTiming)


@pytest.mark.parametrize(
    "overrides",
    [
        {"feature_version": "streaming-v1"},
        {"holding": "INTRADAY"},
        {"use_source_vwap": True},
        {"max_hold_sessions": "0"},
        {"stop_atr_multiplier": "0"},
        {"universe": ["ARCX:SPY", "ARCX:SPY"]},
        {"universe": ["SPY"]},
        {"universe": []},
        {"unexpected": "1"},
    ],
)
def test_the_spec_refuses_invalid_variants(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec(**overrides)


def test_a_parameter_override_is_a_different_specification() -> None:
    variant = spec(month_end_sma_months="12")  # trial overrides are strings
    assert variant.month_end_sma_months == 12
    assert variant.spec_hash != SPEC_HASH  # a variant is a new trial with its own hash


def test_the_rule_needs_an_exchange_calendar() -> None:
    with pytest.raises(ValueError, match="exchange calendar"):
        TrendTiming(spec(), Utc247Calendar())


# ----------------------------------------------------------------------------- month ends


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2018, 3, 29), True),  # Good Friday, 30 March 2018, is a holiday
        (date(2018, 3, 28), False),
        (date(2021, 5, 28), True),  # Memorial Day, 31 May 2021, is a holiday
        (date(2019, 11, 29), True),  # early close (13:00 New York) and the month's last session
        (date(2019, 11, 27), False),  # 28 November is Thanksgiving; the 29th is still November
        (date(2017, 7, 3), False),  # an early close that is not a month end
        (date(2017, 6, 30), True),
        (date(2016, 12, 30), True),  # 31 December 2016 is a Saturday
    ],
)
def test_month_ends_come_from_the_xnys_calendar(day: date, expected: bool) -> None:
    assert month_end(day) is expected


def test_candidates_come_only_at_month_end_closes_including_holiday_and_early_close_months() -> (
    None
):
    """A steadily rising series is above its 10-month SMA at every warm month end, so the rule
    must emit a candidate at exactly every month-end session and nowhere else."""
    bars = path_bars(SPY, date(2017, 1, 1), date(2020, 1, 1))
    decisions = stream(rule(), bars)
    month_ends = [d for d in decisions if month_end(d)]
    warm = month_ends[9:]  # the tenth month end is the first with ten month-end closes
    emitted = [d for d, (c, _) in decisions.items() if c is not None]
    assert emitted == warm
    assert date(2018, 3, 29) in emitted and date(2018, 3, 30) not in decisions
    assert all(decisions[d][1] is None for d in warm)  # never below its SMA
    assert all(decisions[d][1] == EXIT_SMA_UNAVAILABLE for d in month_ends[:9])  # still cold
    assert all(reason is None for d, (_, reason) in decisions.items() if not month_end(d))
    early = next(b for b in bars if day_of(b) == date(2019, 11, 29))
    assert early.minutes == 210
    candidate = decisions[date(2019, 11, 29)][0]
    assert candidate.bar_end == early.end == datetime(2019, 11, 29, 18, 0, tzinfo=UTC)
    assert candidate.decision_time == early.end + timedelta(seconds=1)
    next_open = CALENDAR.session_on(date(2019, 12, 2)).open  # type: ignore[union-attr]
    assert candidate.expires_at == next_open + timedelta(seconds=120)
    assert candidate.max_hold_minutes == spec().candidate_max_hold_minutes


def test_no_unscheduled_closure_moves_a_month_end_in_the_data_span() -> None:
    """Month ends use the published calendar. An ad hoc closure on a month's last scheduled
    session would make the previous session a month end only in hindsight; none of the XNYS
    ad hoc closures from 2016 to 2026 is the last session of its month."""
    adhoc = [
        ts.date() for ts in xcals.get_calendar("XNYS").adhoc_holidays if 2016 <= ts.year <= 2026
    ]
    assert adhoc == [date(2018, 12, 5), date(2025, 1, 9)]
    for day in adhoc:
        following = CALENDAR.sessions_after(day).session_date
        assert (following.year, following.month) == (day.year, day.month)


# ----------------------------------------------------------------------------- no lookahead


def test_the_monthly_sma_uses_only_completed_month_end_closes() -> None:
    bars = daily_history([SPY], missing=(), splits=()).bars[SPY]
    r = rule(universe=[SPY])
    previous: Decimal | None = None
    checked = 0
    for i, bar in enumerate(bars):
        r.on_bar(bar, bar.available_time)
        state = r.state(SPY)
        if not state.ready:
            continue
        sma = state.month_end_sma()
        if month_end(day_of(bar)):
            closes = [b.close for b in bars[: i + 1] if month_end(day_of(b))][-10:]
            assert sma == divide(sum(closes, start=Decimal(0)), Decimal(10))
            assert closes[-1] == bar.close  # the completed month-end close itself
            checked += 1
        else:
            assert sma == previous  # intra-month closes never enter the average
        previous = sma
    assert checked >= 45


def test_decisions_never_depend_on_later_bars() -> None:
    bars = daily_history([SPY], missing=(), splits=()).bars[SPY]
    cutoff = date(2018, 6, 1)
    full = stream(rule(), bars)
    truncated = stream(rule(), [b for b in bars if day_of(b) < cutoff])

    def crashed(b: Bar) -> Bar:  # every later price halved: a different future
        if day_of(b) < cutoff:
            return b
        half = Decimal("0.5")
        return b.model_copy(
            update={k: _q(getattr(b, k) * half) for k in ("open", "high", "low", "close")}
        )

    altered = stream(rule(), [crashed(b) for b in bars])

    def key(entry: tuple[Any, str | None]) -> tuple[Any, ...]:
        c, reason = entry
        return (None if c is None else (c.stop_price, c.target_price, c.snapshot_hash), reason)

    before = [d for d in full if d < cutoff]
    assert [key(full[d]) for d in before] == [key(truncated[d]) for d in before]
    assert [key(full[d]) for d in before] == [key(altered[d]) for d in before]
    assert any(full[d][0] is not None for d in before)
    later = [d for d in full if d >= cutoff and month_end(d)]
    assert [key(full[d]) for d in later] != [key(altered[d]) for d in later]


# ----------------------------------------------------------------------------- streaming = batch


@pytest.mark.parametrize("iid", [SPY, QQQ, "XNAS:TLT", "ARCX:GLD"])
def test_streaming_decisions_equal_a_batch_recomputation(iid: str) -> None:
    bars = daily_history([iid], missing=(), splits=()).bars[iid]
    s = spec()
    decisions = stream(rule(), bars)
    entries = 0
    for i, bar in enumerate(bars):
        candidate, reason = decisions[day_of(bar)]
        expected, expected_reason = batch_decision(bars[: i + 1], s)
        assert reason == expected_reason, (iid, day_of(bar))
        if expected is None:
            assert candidate is None, (iid, day_of(bar))
            continue
        assert candidate is not None, (iid, day_of(bar))
        assert (candidate.stop_price, candidate.target_price) == expected
        assert candidate.reference_price == bar.close
        entries += 1
    exits = sum(1 for _, r in decisions.values() if r == EXIT_BELOW_SMA)
    assert entries >= 10 and exits >= 3, (iid, entries, exits)


def test_a_split_is_not_a_trend_break() -> None:
    """XLK's synthetic 2-for-1 split: with ``on_split`` the decisions equal a batch over
    split-adjusted bars; without it the halved raw close would read as a fall below the SMA."""
    fixture = daily_history([XLK], missing=())
    (split,) = fixture.splits
    bars = fixture.bars[XLK]
    s = spec()
    decisions = stream(rule(), bars, [(split.ex_date, split.ratio)])
    adjusted = [
        b.model_copy(
            update={
                k: rescale_price(getattr(b, k), split.ratio)
                for k in ("open", "high", "low", "close")
            }
        )
        if day_of(b) < split.ex_date
        else b
        for b in bars
    ]
    compared = 0
    for i, bar in enumerate(bars):
        if day_of(bar) < split.ex_date or not month_end(day_of(bar)):
            continue
        candidate, reason = decisions[day_of(bar)]
        expected, expected_reason = batch_decision(adjusted[: i + 1], s)
        assert reason == expected_reason and (candidate is None) == (expected is None)
        if candidate is not None and expected is not None:
            # Wilder ATR was rounded on the old share basis: equal to within one cent
            assert abs(candidate.stop_price - expected[0]) <= CENT
        compared += 1
    assert compared >= 15
    first = next(d for d in decisions if d >= split.ex_date and month_end(d))
    assert decisions[first][1] is None
    naive = stream(rule(), bars)  # the split ignored: a false trend break
    assert naive[first][1] == EXIT_BELOW_SMA


# ----------------------------------------------------------------------------- missing data


def test_a_missing_session_resets_and_a_held_position_exits_at_the_next_month_end() -> None:
    fixture = daily_history([XLB])  # one XLB session is missing (2018-01-18)
    (_, missing), *_ = fixture.missing
    r = rule()
    decisions = stream(r, fixture.bars[XLB])
    assert r.gaps() == 1
    month_ends = [d for d in decisions if month_end(d)]
    after = [d for d in month_ends if d > missing]
    assert after[0] == date(2018, 1, 31)
    assert decisions[after[0]] == (None, EXIT_SMA_UNAVAILABLE)  # fail closed while held
    cold = after[:9]  # ten new month ends are needed before the SMA exists again
    assert all(decisions[d] == (None, EXIT_SMA_UNAVAILABLE) for d in cold)
    assert all(decisions[d][1] != EXIT_SMA_UNAVAILABLE for d in after[9:])
    assert not any(c is not None for d, (c, _) in decisions.items() if missing < d < after[9])
    before = [d for d in month_ends if d < missing]
    assert all(decisions[d][1] != EXIT_SMA_UNAVAILABLE for d in before[9:])


def test_instruments_outside_the_universe_are_ignored() -> None:
    r = rule(universe=[SPY])
    bars = path_bars(QQQ, date(2017, 1, 1), date(2018, 6, 1))
    for bar in bars:
        assert r.on_bar(bar, bar.available_time) is None
        assert r.exit_signal(QQQ, bar) is None
    r.on_split(QQQ, Decimal(2))
    with pytest.raises(KeyError):
        r.state(QQQ)
    with pytest.raises(LookupError):
        r.features(QQQ)


def test_exit_signal_refuses_a_bar_the_rule_has_not_consumed() -> None:
    r = rule()
    first, second = path_bars(SPY, date(2017, 1, 1), date(2017, 1, 10))[:2]
    r.on_bar(first, first.available_time)
    with pytest.raises(ValueError, match="before on_bar"):
        r.exit_signal(SPY, second)
    with pytest.raises(ValueError, match="before its availability"):
        r.on_bar(second, second.end)


# ----------------------------------------------------------------------------- engine


def _trade_days(result: EngineResult) -> list[tuple[date, str, date]]:
    return [
        (
            CALENDAR.session_date_of(t.entry.time),
            t.exits[-1].reason,
            CALENDAR.session_date_of(t.exits[-1].time),
        )
        for t in result.trades
    ]


def test_a_month_end_close_below_the_sma_sells_at_the_next_open_and_mid_month_closes_do_not() -> (
    None
):
    fall_start = date(2017, 6, 26)  # five sessions of -1% to the June 2017 month end

    def step(day: date, _k: int) -> Decimal:
        return Decimal("-0.01") if fall_start <= day <= date(2017, 6, 30) else Decimal("0.0002")

    bars = path_bars(SPY, date(2016, 1, 1), date(2017, 9, 1), step)
    probe = rule(universe=[SPY])
    below_mid_month: list[date] = []
    for bar in bars:
        probe.on_bar(bar, bar.available_time)
        reason = probe.exit_signal(SPY, bar)
        state = probe.state(SPY)
        if state.ready and state.close < state.month_end_sma() and not month_end(day_of(bar)):
            assert reason is None  # below the SMA mid-month: no signal
            below_mid_month.append(day_of(bar))
    assert date(2017, 6, 29) in below_mid_month  # and the stop is never reached
    start, end = datetime(2016, 1, 1, tzinfo=UTC), datetime(2017, 9, 1, tzinfo=UTC)
    result = engine_run(bars, rule(universe=[SPY]), start, end)
    assert len(result.trades) == 1
    (trade,) = result.trades
    assert trade.entry.time == CALENDAR.session_on(date(2016, 11, 1)).open  # type: ignore[union-attr]
    assert trade.exits[-1].reason == f"SIGNAL:{EXIT_BELOW_SMA}"
    # sold at the open of 3 July 2017 (an early-close session), after the 30 June month end
    assert trade.exits[-1].time == CALENDAR.session_on(date(2017, 7, 3)).open  # type: ignore[union-attr]
    held = [
        c
        for c in result.candidates
        if c.status == "REJECTED" and CALENDAR.session_date_of(c.decision_time) < date(2017, 6, 30)
    ]
    assert held and {c.binding for c in held} == {"instrument_busy"}
    assert result.diagnostics["exit_signals"] == 1


def test_the_time_exit_sells_after_260_sessions_and_a_later_month_end_re_enters() -> None:
    """A slow, steady rise never reaches the 4-ATR stop or the 3R target and never falls below
    its SMA, so only the 260-session time exit ends a position."""
    bars = path_bars(SPY, date(2016, 1, 1), date(2019, 2, 1))
    start, end = datetime(2016, 1, 1, tzinfo=UTC), datetime(2019, 2, 1, tzinfo=UTC)
    result = engine_run(bars, rule(universe=[SPY]), start, end)
    trades = result.trades
    assert [t.exits[-1].reason for t in trades] == ["TIME_EXIT", "TIME_EXIT", "END_OF_DATA"]
    # the first warm month end is 31 October 2016 (the tenth month-end close)
    assert trades[0].entry.time == CALENDAR.session_on(date(2016, 11, 1)).open  # type: ignore[union-attr]
    days = [day_of(b) for b in bars]
    for trade, following in itertools.pairwise(trades):
        entry_day = CALENDAR.session_date_of(trade.entry.time)
        deadline = CALENDAR.sessions_after(entry_day, 260)  # held through 260 sessions
        assert trade.exits[-1].time == deadline.open
        signal = next(d for d in days if d >= deadline.session_date and month_end(d))
        assert following.entry.time == CALENDAR.sessions_after(signal).open
    rejected = {c.binding for c in result.candidates if c.status == "REJECTED"}
    assert rejected == {"instrument_busy"}  # month ends while held: "keep holding"


def test_end_to_end_on_the_synthetic_fixture() -> None:
    """The whole study path on the SYNTHETIC 16-ETF fixture: independent three-month windows
    with 230 warm-up sessions, the continuous deployment, walk-forward and the gate."""
    fixture = daily_history()
    cfg = study_config([TRIAL], warmup_sessions=230)
    ctx, data = daily_study(cfg, fixture)
    windows = deployment_windows(cfg.splits)
    run = run_windows(ctx, TRIAL, data, "base", windows)
    assert run.trades and run.candidates
    for c in run.candidates:  # every candidate is a month-end decision
        assert month_end(CALENDAR.session_date_of(parse_time(c["decision_time"])))
    entries: Counter[date] = Counter()
    risk = POLICY.exposure.risk_per_trade_fraction
    for start, end in windows:  # sizing compounds realised gains inside each deployment
        inside = [t for t in run.trades if start <= parse_time(t["entry"]["time"]) < end]
        equity = Decimal(50) + sum(
            (max(Decimal(str(t["net_pnl"])), Decimal(0)) for t in inside), start=Decimal(0)
        )
        assert all(Decimal(str(t["planned_loss_usd"])) <= risk * equity for t in inside)
    for t in run.trades:
        entry = parse_time(t["entry"]["time"])
        day = CALENDAR.session_date_of(entry)
        assert entry == CALENDAR.session_on(day).open  # type: ignore[union-attr]
        assert after_month_end(day)  # entries only at the open after a month end
        entries[day] += 1
        assert t["exit_reason"] in ALLOWED_EXITS
        if str(t["exit_reason"]).startswith("SIGNAL:"):
            exit_time = parse_time(t["exits"][-1]["time"])
            exit_day = CALENDAR.session_date_of(exit_time)
            assert exit_time == CALENDAR.session_on(exit_day).open  # type: ignore[union-attr]
            assert after_month_end(exit_day)
    assert max(entries.values()) == 1  # one pending entry at a time (ADR-0010)
    reasons = Counter(str(t["exit_reason"]) for t in run.trades)
    assert reasons[f"SIGNAL:{EXIT_BELOW_SMA}"] >= 1
    bindings = Counter(str(c["binding"]) for c in run.candidates if c["status"] == "REJECTED")
    assert bindings["instrument_busy"] > 0 and bindings["pending_entry"] > 0
    assert run_windows(ctx, TRIAL, data, "base", windows).result_hash == run.result_hash

    continuous = run_trial(ctx, TRIAL, data, "base")
    signals = [t for t in continuous.trades if t.exits[-1].reason.startswith("SIGNAL:")]
    assert len(signals) >= 3
    assert all(after_month_end(CALENDAR.session_date_of(t.entry.time)) for t in continuous.trades)

    summary = summarize(ctx, run.series, run.trades, run.candidates)
    assert summary["trades"] == len(run.trades)
    wf, selected, log = walk_forward(
        cfg.splits, [TRIAL.trial_id], {TRIAL.trial_id: run.series}, {TRIAL.trial_id: run.trades}
    )
    assert len(log) == 9 and all(f["selected"] in (TRIAL.trial_id, "CASH") for f in log)
    gate = _g2(summarize(ctx, wf, selected, seed_offset=1000), None)
    assert gate["status"] == "NOT_PASSED"  # synthetic data and no reality check: never a pass
