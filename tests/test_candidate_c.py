"""Candidate C - dip-buy in an uptrend (Connors & Alvarez 2008, the 2-period RSI strategy) on
daily ETF bars (strategies/dip_buy.py; decision record "Candidate C: dip-buy in an uptrend").

Everything here runs on SYNTHETIC bars (tests/daily_fixtures.py or hand-made series): pipeline
checks, never evidence. No real market data is read and nothing is registered.

* The frozen specification carries the published parameters and resolves through the rule
  registry and the study path.
* Streaming decisions and feature snapshots equal an independent batch recomputation.
* No lookahead: truncating future bars changes no earlier candidate, exit signal or engine result.
* Signals appear exactly where a hand-computed RSI(2)/SMA(200) example says.
* The rule never shorts and trades only its 13 equity ETFs.
* End to end on the daily fixture: SIGNAL exits, stops (also through a gap) and time exits.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.daily_fixtures import (
    CALENDAR,
    EQUITY_ETFS,
    UNIVERSE,
    DailyFixture,
    daily_history,
    daily_specs,
    daily_study,
    session_bar,
    sessions,
    study_config,
)
from trader.accounting.fees import load_fee_schedule
from trader.config.policy import load_policy, scale_policy
from trader.data.calendars import Utc247Calendar
from trader.data.corporate_actions import SplitEvent, split_effective_time
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.domain.types import Direction, Track
from trader.features.daily import (
    FEATURE_VERSION,
    NEUTRAL_RSI,
    divide,
    is_month_end,
    quantize,
    rescale_price,
)
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
    _registered_features,
    build_strategy,
    deployment_windows,
    resolve_rule,
    run_trial,
    run_windows,
    summarize,
)
from trader.strategies import registry
from trader.strategies.dip_buy import FEATURES, SPEC_FILE, DipBuy, DipBuySpec, symbol_of

REPO = Path(__file__).resolve().parents[1]
SPEC_REL = "src/trader/strategies/specs/candidate_c_etf_daily_v1.yaml"
#: The frozen specification's content hash: an edit to the YAML is a new trial.
PINNED_SPEC_HASH = "a2c643bb34cb74ebc826c1d5071beef9e6128328f23040b6d2fcf2ef5bc5ff59"
SPEC: DipBuySpec = registry.load_spec(SPEC_FILE)
SPY = "ARCX:SPY"
CENT = Decimal("0.01")
SIGNAL = "SIGNAL:close_above_sma_5"
TRIAL = TrialCfg(
    trial_id="T-9950",
    spec_file=SPEC_REL,
    hypothesis="SYNTHETIC pipeline check of Candidate C; never registered",
)
EXIT_REASONS = {SIGNAL, "TIME_EXIT", "STOP", "STOP_GAP", "END_OF_DATA"}


@pytest.fixture(scope="module")
def fixture_data() -> DailyFixture:
    return daily_history()


def timeline(bars_by_instrument: Iterable[Sequence[Bar]]) -> list[Bar]:
    return sorted(
        (b for bars in bars_by_instrument for b in bars),
        key=lambda b: (b.available_time, b.instrument_id),
    )


def floor_tick(value: Decimal, tick: Decimal = CENT) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


# ----------------------------------------------------------------------------- specification


def test_the_frozen_spec_carries_the_published_parameters() -> None:
    assert isinstance(SPEC, DipBuySpec)
    assert SPEC.spec_hash == PINNED_SPEC_HASH
    assert (SPEC.family, SPEC.track, SPEC.feature_version) == (
        "CANDIDATE_C",
        Track.STOCKS,
        "daily-v1",
    )
    # Connors & Alvarez (2008): close > SMA(200), RSI(2) < 10, exit on a close > SMA(5)
    assert (SPEC.trend_sma_sessions, SPEC.rsi_period, SPEC.rsi_entry_below) == (200, 2, 10)
    assert SPEC.exit_sma_sessions == 5
    # the shared design contract: 10-session time exit, 3 x ATR(10) disaster stop
    assert (SPEC.max_hold_sessions, SPEC.atr_period, SPEC.stop_atr_multiplier) == (10, 10, 3)
    assert (SPEC.holding, SPEC.target_role) == ("OVERNIGHT", "COST_GATE")
    assert SPEC.universe_symbols == (
        "SPY", "QQQ", "IWM", "DIA", "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY",
    )  # fmt: skip
    assert tuple(symbol_of(i) for i in EQUITY_ETFS) == SPEC.universe_symbols
    assert SPEC.min_warmup_sessions == 200
    assert SPEC.exit_reason == "close_above_sma_5"


def test_the_family_resolves_through_the_registry_and_the_study_path(
    fixture_data: DailyFixture,
) -> None:
    entry = registry.rule_family("CANDIDATE_C")
    assert entry.label == "Candidate C (dip-buy in an uptrend)"
    assert "CANDIDATE_C" in registry.families()
    assert registry.family_label("CANDIDATE_C") == entry.label
    ctx, _ = daily_study(study_config([TRIAL], warmup_sessions=200), fixture_data)
    spec = resolve_rule(ctx, TRIAL)
    assert spec == SPEC and spec.spec_hash == PINNED_SPEC_HASH
    rule = build_strategy(ctx, spec, {})
    assert isinstance(rule, DipBuy)
    assert _registered_features(spec) == FEATURES
    timing = RuleTiming.of(rule)
    assert timing.overnight and timing.max_hold_sessions == 10
    assert timing.target_role == "COST_GATE" and timing.exit_signal is not None
    with pytest.raises(TypeError):
        entry.build(object(), CALENDAR, {})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"feature_version": "streaming-v1"}, "versions differ"),
        ({"direction": "SHORT"}, "Extra inputs"),  # long only: there is no direction to set
        ({"rsi_entry_below": "100"}, "below 100"),
        ({"exit_sma_sessions": 200}, "shorter than the trend"),
        ({"universe_symbols": ["SPY", "SPY"]}, "distinct"),
        ({"holding": "INTRADAY"}, "OVERNIGHT"),
        ({"target_role": "EXIT"}, "COST_GATE"),
    ],
)
def test_the_spec_refuses_other_versions_shorts_and_inconsistent_values(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    raw = yaml.safe_load(SPEC_FILE.read_text(encoding="utf-8"))
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({**raw, **change}), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        registry.load_spec(path)


def test_the_rule_needs_whole_session_bars_and_an_exchange_calendar() -> None:
    with pytest.raises(ValueError, match="exchange calendar"):
        DipBuy(SPEC, Utc247Calendar(), {})
    rule = DipBuy(SPEC, CALENDAR, {})
    session = sessions(date(2019, 1, 2), date(2019, 1, 4))[0]
    p = Decimal(100)
    five_minutes = Bar(
        instrument_id=SPY,
        feed="synthetic-daily",
        minutes=5,
        start=session.open,
        end=session.open + timedelta(minutes=5),
        available_time=session.open + timedelta(minutes=5, seconds=1),
        complete=True,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal(1000),
        trade_count=10,
        vwap=p,
    )
    with pytest.raises(ValueError, match="one bar per regular session"):
        rule.on_bar(five_minutes, five_minutes.available_time)
    daily = session_bar(SPY, session, p, p, p, p)
    with pytest.raises(ValueError, match="availability"):
        rule.on_bar(daily, daily.end)
    # bonds and gold are outside Candidate C's universe: ignored, no state is kept
    gold = session_bar("ARCX:GLD", session, p, p, p, p)
    assert rule.on_bar(gold, gold.available_time) is None
    assert rule.exit_signal("ARCX:GLD", gold) is None
    assert not rule.trades("ARCX:GLD") and not rule.trades("XNAS:TLT") and rule.trades("XNAS:QQQ")
    assert rule.gaps() == 0 and "ARCX:GLD" not in rule._state
    with pytest.raises(LookupError):
        rule.features(SPY)


# ----------------------------------------------------------------------------- hand-computed

#: Sessions 0..199 close at 100.0, 100.1, ..., 119.9: SMA(200) = 109.95, RSI(2) = 100.
RISE = [Decimal(1000 + i) / 10 for i in range(200)]


def hand_bars(closes: Sequence[Decimal], iid: str = SPY) -> list[Bar]:
    """Open = close, high = close + 0.5, low = close - 0.5 (so every true range is known)."""
    days = sessions(date(2017, 1, 3), date(2018, 3, 1))
    return [
        session_bar(iid, s, c, c + Decimal("0.5"), c - Decimal("0.5"), c)
        for s, c in zip(days, closes, strict=False)
    ]


def feed(rule: DipBuy, bars: Sequence[Bar]) -> list[tuple[TradeCandidate | None, str | None]]:
    out: list[tuple[TradeCandidate | None, str | None]] = []
    for bar in bars:
        candidate = rule.on_bar(bar, bar.available_time)
        out.append((candidate, rule.exit_signal(bar.instrument_id, bar)))
    return out


def test_signals_appear_exactly_where_the_hand_computed_example_says() -> None:
    # 200: 116.0 (-3.9)  RSI = 100 * 0.05 / (0.05 + 1.95) = 2.5 < 10; SMA(200) = 22006/200
    #      = 110.03 < 116 -> SIGNAL. ATR = (9 * 1.0 + TR 4.4) / 10 = 1.34; stop = 116 - 4.02;
    #      target = SMA(5) = (119.6 + 119.7 + 119.8 + 119.9 + 116) / 5 = 119.00
    # 201: 115.6 (-0.4)  RSI = 100 * 0.025 / (0.025 + 1.175) = 2.083..; SMA(200) = 110.1075 -> SIGNAL
    #      ATR = (9 * 1.34 + 1.0) / 10 = 1.306; stop = floor(115.6 - 3.918) = 111.68;
    #      target = (119.7 + 119.8 + 119.9 + 116 + 115.6) / 5 = 118.20
    # 202: 116.6 (+1.0)  RSI = 100 * 0.5125 / (0.5125 + 0.5875) = 46.59 -> none
    # 203: 105.0 (-11.6) RSI = 100 * 0.25625 / (0.25625 + 6.09375) = 4.04 < 10, but
    #      SMA(200) = 110.213 > 105 -> none (the trend filter)
    # 204: 118.0 (+13.0) RSI = 68.5 -> none
    closes = [
        *RISE,
        Decimal("116.0"),
        Decimal("115.6"),
        Decimal("116.6"),
        Decimal(105),
        Decimal(118),
    ]
    bars = hand_bars(closes)
    rule = DipBuy(SPEC, CALENDAR, {SPY: CENT})
    out = feed(rule, bars)
    signalled = [i for i, (c, _) in enumerate(out) if c is not None]
    assert signalled == [200, 201]
    first, second = out[200][0], out[201][0]
    assert first is not None and second is not None
    assert (first.reference_price, first.stop_price, first.target_price) == (
        Decimal("116.0"),
        Decimal("111.98"),
        Decimal("119.00"),
    )
    assert (second.stop_price, second.target_price) == (Decimal("111.68"), Decimal("118.20"))
    assert first.direction is Direction.LONG and second.direction is Direction.LONG
    # the snapshot behind the most recent candidate (day 201), hashed into it
    snap = rule.features(SPY)
    assert snap["sma_200"] == Decimal("110.1075") and snap["sma_5"] == Decimal("118.2")
    assert snap["rsi_2"] == quantize(Decimal("2.5") / Decimal("1.2"))
    assert snap["atr_10"] == Decimal("1.306") and snap["close"] == Decimal("115.6")
    assert second.snapshot_hash == content_hash({"version": FEATURE_VERSION, "features": snap})
    # next-open timing: the order lives from the next open; the hold is ten sessions from it
    day200 = CALENDAR.session_date_of(bars[200].start)
    entry = CALENDAR.sessions_after(day200)
    assert entry.open == bars[201].start
    assert first.expires_at == entry.open + timedelta(seconds=SPEC.entry_ttl_seconds)
    deadline = CALENDAR.sessions_after(entry.session_date, 10).open
    assert first.max_hold_minutes * 60 == (deadline - entry.open).total_seconds()
    # exit signal: close > SMA(5). Steady rise: every close is above SMA(5); the dip and the
    # crash are below it (119.00, 118.20, 117.58, 114.62); 118 > SMA(5) = 114.24 on day 204
    exits = [i for i, (_, reason) in enumerate(out) if reason is not None]
    assert exits == [*range(4, 200), 204]
    assert out[204][1] == "close_above_sma_5"
    with pytest.raises(ValueError, match="has not seen"):
        rule.exit_signal(SPY, bars[100])


@pytest.mark.parametrize(("drop", "signal"), [("0.90", False), ("0.91", True)])
def test_the_rsi_threshold_is_strict(drop: str, signal: bool) -> None:
    # gain 0.1 -> 0.05; loss 0 -> drop / 2. A drop of 0.90 gives RSI = 5 / 0.5 = 10 exactly (no
    # signal: RSI must be below 10); 0.91 gives 5 / 0.505 = 9.90 (signal).
    closes = [*RISE, RISE[-1] - Decimal(drop)]
    rule = DipBuy(SPEC, CALENDAR, {})
    out = feed(rule, hand_bars(closes))
    rsi = rule.state(SPY).rsi()
    assert (rsi == 10) is not signal
    assert (out[-1][0] is not None) is signal
    assert all(c is None for c, _ in out[:-1])  # RSI(2) = 100 throughout the rise


def run_engine(
    setups: dict[str, InstrumentSetup],
    rule: object,
    start: datetime,
    end: datetime,
) -> EngineResult:
    cfg = EngineConfig(
        policy=load_policy(REPO / "configs" / "policy" / "base.yaml").policy,
        fees=load_fee_schedule(REPO / "configs" / "fees" / "alpaca_us_equities_v2.yaml"),
        fills=FillAssumptions("base", Decimal("2"), Decimal("2"), Decimal("0.10")),
        track=Track.STOCKS,
        calendar=CALENDAR,
        start=start,
        end=end,
        loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
        session_basis="XNYS",
    )
    return ResearchEngine(cfg, rule, setups).run()  # type: ignore[arg-type]


def test_the_engine_enters_at_the_next_open_and_exits_at_the_open_after_the_signal() -> None:
    # Days 0..202 as above; 203 closes at 118.0 > SMA(5) = (119.9 + 116 + 115.6 + 116.6 + 118) / 5
    # = 117.22 -> sell at the open of 204. Day 202 trades up to 121 intraday, through the target
    # (119.00): with target_role COST_GATE that is not an exit (the book has no profit limit).
    closes = [*RISE, Decimal("116.0"), Decimal("115.6"), Decimal("116.6"), Decimal(118)]
    closes += [Decimal("118.5"), Decimal(119)]
    bars = hand_bars(closes)
    spike = bars[202]
    bars[202] = spike.model_copy(update={"high": Decimal(121)})
    assert bars[202].high * Decimal("0.9998") > Decimal("119.00")  # the bid trades through it
    setups = {SPY: InstrumentSetup(spec=daily_specs([SPY])[SPY], bars=bars)}
    result = run_engine(setups, DipBuy(SPEC, CALENDAR, {SPY: CENT}), bars[0].start, bars[-1].end)
    (trade,) = result.trades
    assert trade.entry.time == bars[201].start  # the next open, never the signal close
    # fill = ceil(115.6 * (1 + 4 bps)) = 115.65 <= limit floor(116 * 1.0002 * 1.0005) = 116.08
    assert trade.entry.price == Decimal("115.65")
    assert (trade.stop, trade.target, trade.target_exit) == (
        Decimal("111.98"),
        Decimal("119.00"),
        False,
    )
    (exit_fill,) = trade.exits
    assert exit_fill.reason == SIGNAL and exit_fill.time == bars[204].start
    assert exit_fill.price == floor_tick(Decimal("118.5") * Decimal("0.9996"))  # 118.45
    assert exit_fill.quantity == trade.quantity and trade.planned_loss_usd <= Decimal("0.125")
    # day 201's second signal is refused: one position per instrument
    assert [(o.status, o.reasons) for o in result.candidates] == [
        ("FILLED", ()),
        ("REJECTED", ("RISK_LIMIT",)),
    ]
    assert result.candidates[1].binding == "instrument_busy"
    assert result.diagnostics["exit_signals"] == 1
    # contrast: the same rule declaring the target an EXIT would have sold at it on day 202
    as_exit = DipBuy(SPEC, CALENDAR, {SPY: CENT})
    as_exit.target_role = "EXIT"  # type: ignore[attr-defined]
    (limit_trade,) = run_engine(setups, as_exit, bars[0].start, bars[-1].end).trades
    assert [(f.reason, f.price, f.time) for f in limit_trade.exits] == [
        ("TARGET", Decimal("119.00"), bars[202].end)
    ]


# ----------------------------------------------------------------------------- batch reference


def segments(bars: Sequence[Bar]) -> list[list[Bar]]:
    """Contiguous runs of sessions (a missing session starts a new run, as the state resets)."""
    out: list[list[Bar]] = []
    for bar in bars:
        day = CALENDAR.session_date_of(bar.start)
        if out:
            prev = CALENDAR.session_date_of(out[-1][-1].start)
            if CALENDAR.sessions_after(prev).session_date == day:
                out[-1].append(bar)
                continue
        out.append([bar])
    return out


def wilder(values: Sequence[Decimal | None], n: int) -> list[Decimal | None]:
    """Wilder's average aligned to ``values`` (``values[0]`` is None: no previous close)."""
    out: list[Decimal | None] = [None] * len(values)
    avg: Decimal | None = None
    for i in range(n, len(values)):
        x = values[i]
        assert x is not None
        if avg is None:
            seed = [v for v in values[1 : n + 1] if v is not None]
            avg = divide(sum(seed, start=Decimal(0)), Decimal(n))
        else:
            avg = divide(avg * (n - 1) + x, Decimal(n))
        out[i] = avg
    return out


def batch(bars: Sequence[Bar]) -> list[dict[str, Decimal] | None]:
    """An independent batch recomputation of the rule's snapshot over one contiguous run."""
    closes = [b.close for b in bars]
    changes: list[Decimal | None] = [None, *(b - a for a, b in itertools.pairwise(closes))]
    gains = wilder([None if c is None else max(c, Decimal(0)) for c in changes], 2)
    losses = wilder([None if c is None else max(-c, Decimal(0)) for c in changes], 2)
    ranges: list[Decimal | None] = [None]
    for a, b in itertools.pairwise(bars):
        ranges.append(max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)))
    atr = wilder(ranges, 10)
    out: list[dict[str, Decimal] | None] = []
    for i, bar in enumerate(bars):
        g, lo, a = gains[i], losses[i], atr[i]
        if i < 199 or g is None or lo is None or a is None:
            out.append(None)
            continue
        day = CALENDAR.session_date_of(bar.start)
        out.append(
            {
                "close": closes[i],
                "sma_5": divide(sum(closes[i - 4 : i + 1], start=Decimal(0)), Decimal(5)),
                "sma_200": divide(sum(closes[i - 199 : i + 1], start=Decimal(0)), Decimal(200)),
                "rsi_2": NEUTRAL_RSI if g + lo == 0 else divide(100 * g, g + lo),
                "atr_10": a,
                "month_end": Decimal(1) if is_month_end(CALENDAR, day) else Decimal(0),
            }
        )
    return out


def batch_signal(snap: dict[str, Decimal] | None) -> bool:
    return snap is not None and snap["close"] > snap["sma_200"] and snap["rsi_2"] < 10


def test_streaming_decisions_and_snapshots_equal_a_batch_recomputation() -> None:
    fixture = daily_history(EQUITY_ETFS, splits=())  # keeps the missing XLB session
    rule = DipBuy(SPEC, CALENDAR, {})
    emitted: dict[tuple[str, datetime], TradeCandidate] = {}
    snapshots: dict[tuple[str, datetime], dict[str, Decimal]] = {}
    for bar in timeline(fixture.bars.values()):
        candidate = rule.on_bar(bar, bar.available_time)
        if candidate is not None:
            emitted[(bar.instrument_id, bar.end)] = candidate
            snapshots[(bar.instrument_id, bar.end)] = dict(rule.features(bar.instrument_id))
    expected: set[tuple[str, datetime]] = set()
    for iid, bars in fixture.bars.items():
        for run in segments(bars):
            for bar, snap in zip(run, batch(run), strict=True):
                key = (iid, bar.end)
                if not batch_signal(snap):
                    continue
                assert snap is not None
                expected.add(key)
                assert snapshots[key] == snap, key
                candidate = emitted[key]
                assert candidate.stop_price == floor_tick(snap["close"] - 3 * snap["atr_10"])
                assert candidate.target_price == floor_tick(snap["sma_5"])
                assert candidate.snapshot_hash == content_hash(
                    {"version": FEATURE_VERSION, "features": snap}
                )
    assert set(emitted) == expected
    assert len(expected) >= 100  # the fixture's dips in uptrends
    assert {iid for iid, _ in expected} == set(EQUITY_ETFS)
    assert rule.gaps() == 1  # the missing XLB session reset that state (fail closed)


@pytest.mark.parametrize("iid", ["ARCX:XLK", "ARCX:XLE"])  # 2-for-1 and 3-for-1
def test_a_split_keeps_the_200_session_state_and_matches_adjusted_bars(iid: str) -> None:
    fixture = daily_history([iid], missing=())
    (split,) = fixture.splits
    effective = split_effective_time(split, CALENDAR)
    rule = DipBuy(SPEC, CALENDAR, {})
    applied = False
    after: list[tuple[int, dict[str, Decimal]]] = []
    bars = fixture.bars[iid]
    for i, bar in enumerate(bars):
        if not applied and bar.start >= effective:
            rule.on_split(iid, split.ratio)
            applied = True
        if rule.on_bar(bar, bar.available_time) is not None and applied:
            after.append((i, dict(rule.features(iid))))
    adjusted = [
        b
        if b.start >= effective
        else b.model_copy(
            update={
                k: rescale_price(getattr(b, k), split.ratio)
                for k in ("open", "high", "low", "close")
            }
        )
        for b in bars
    ]
    reference = batch(adjusted)
    assert after, "the fixture has dips after the split"
    assert rule.gaps() == 0  # a split is not a gap
    for i, snap in after:
        ref = reference[i]
        assert ref is not None
        assert snap["sma_200"] == ref["sma_200"] and snap["sma_5"] == ref["sma_5"]  # exact sums
        assert abs(snap["atr_10"] - ref["atr_10"]) <= Decimal("1e-11")
        assert abs(snap["rsi_2"] - ref["rsi_2"]) <= Decimal("1e-9")


# ----------------------------------------------------------------------------- no lookahead


def replay(
    bars: Sequence[Bar], splits: Sequence[SplitEvent] = ()
) -> list[tuple[str, date, Any, str | None]]:
    """Every candidate and exit-signal answer of a fresh rule, in availability order."""
    rule = DipBuy(SPEC, CALENDAR, {})
    queue = {s.instrument_id: split_effective_time(s, CALENDAR) for s in splits}
    ratios = {s.instrument_id: s.ratio for s in splits}
    out: list[tuple[str, date, Any, str | None]] = []
    for bar in bars:
        iid = bar.instrument_id
        if iid in queue and bar.start >= queue[iid]:
            rule.on_split(iid, ratios.pop(iid))
            del queue[iid]
        candidate = rule.on_bar(bar, bar.available_time)
        dumped = None if candidate is None else candidate.model_dump(mode="json")
        out.append((iid, CALENDAR.session_date_of(bar.start), dumped, rule.exit_signal(iid, bar)))
    return out


def test_truncating_future_bars_changes_no_earlier_candidate_or_exit_signal(
    fixture_data: DailyFixture,
) -> None:
    full_bars = timeline(fixture_data.bars.values())
    full = replay(full_bars, fixture_data.splits)
    assert sum(1 for r in full if r[2] is not None) >= 100
    for cut in (date(2017, 3, 1), date(2018, 6, 4), date(2019, 9, 9), date(2020, 3, 2)):
        part = [b for b in full_bars if CALENDAR.session_date_of(b.start) < cut]
        assert replay(part, fixture_data.splits) == full[: len(part)], cut


def _entry_open(decision_time: datetime) -> datetime:
    return CALENDAR.sessions_after(CALENDAR.session_date_of(decision_time)).open


def test_truncating_future_bars_changes_no_earlier_engine_result(
    fixture_data: DailyFixture,
) -> None:
    cfg = study_config([TRIAL], warmup_sessions=200)
    ctx, data = daily_study(cfg, fixture_data)
    policy = scale_policy(ctx.policy, Decimal(1000))  # more trades to compare (diagnostic scale)
    start = datetime(2017, 1, 1, tzinfo=UTC)
    full = run_trial(ctx, TRIAL, data, "base", start=start, policy=policy)
    assert len(full.trades) >= 50
    full_trades = {t.trade_id: t.as_dict() for t in full.trades}
    for cut in (datetime(2018, 6, 15, tzinfo=UTC), datetime(2019, 12, 2, tzinfo=UTC)):
        part = run_trial(ctx, TRIAL, data, "base", start=start, end=cut, policy=policy)
        compared = 0
        for trade in part.trades:
            if trade.exits[-1].reason != "END_OF_DATA":
                assert trade.as_dict() == full_trades[trade.trade_id]
                compared += 1
        assert compared >= 10
        early = [c.as_dict() for c in full.candidates if _entry_open(c.decision_time) < cut]
        assert [c.as_dict() for c in part.candidates if c.reasons != ("END_OF_DATA",)] == early
        last = part.sessions[-1].session
        assert [s for s in part.sessions if s.session < last] == [
            s for s in full.sessions if s.session < last
        ]


# ----------------------------------------------------------------------------- end to end


def check_trades(trades: Sequence[dict[str, Any]], *, capital: Decimal) -> None:
    """Long only, reducing exits only, entries at session opens, the policy's risk per trade.

    The risk budget is 0.25% of the sizing equity: the window's starting ``capital`` plus the
    gains realised before the entry (less than 2% in these synthetic windows).
    """
    for t in trades:
        assert t["instrument_id"] in EQUITY_ETFS  # never bonds or gold
        entry = t["entry"]
        assert entry["side"] == "BUY"
        assert all(f["side"] == "SELL" for f in t["exits"])
        sold = sum((Decimal(str(f["quantity"])) for f in t["exits"]), start=Decimal(0))
        assert sold + Decimal(str(t["dust"])) == Decimal(str(t["quantity"]))  # never oversold
        opened = datetime.fromisoformat(str(entry["time"]).replace("Z", "+00:00"))
        session = CALENDAR.session_on(CALENDAR.session_date_of(opened))
        assert session is not None and opened == session.open  # the next open, not a close
        assert Decimal(str(t["planned_loss_usd"])) <= Decimal("0.0025") * capital * Decimal("1.02")
        assert t["exit_reason"] in EXIT_REASONS


def test_end_to_end_on_the_synthetic_daily_fixture(fixture_data: DailyFixture) -> None:
    cfg = study_config([TRIAL], warmup_sessions=SPEC.min_warmup_sessions)
    ctx, data = daily_study(cfg, fixture_data)
    windows = deployment_windows(cfg.splits)
    run = run_windows(ctx, TRIAL, data, "base", windows)
    assert run.trades, "the fixture's dips must produce trades at USD 50"
    check_trades(run.trades, capital=Decimal(50))
    reasons = {t["exit_reason"] for t in run.trades}
    assert {SIGNAL, "STOP"} <= reasons
    outcomes = {r for c in run.candidates for r in c["reasons"]}
    assert "TOO_COSTLY" in outcomes  # the cost gate at the exact USD 50 size is active
    summary = summarize(ctx, run.series, run.trades, run.candidates)
    assert summary["trades"] == len(run.trades)
    assert _g2(summary, None)["status"] == "NOT_PASSED"  # synthetic data is never a pass
    start, end = windows[0]
    first = run_trial(ctx, TRIAL, data, "base", start=start, end=end)
    assert run_trial(ctx, TRIAL, data, "base", start=start, end=end).result_hash == (
        first.result_hash
    )
    # a diagnostic capital view (policy USD amounts scaled to USD 1000) over the windows around
    # the market-wide 6% gap (fixture.gap_session): it opens through held stops, sold at the open
    gap = fixture_data.gap_session
    k = next(i for i, w in enumerate(windows) if w[0].date() <= gap < w[1].date())
    around_gap = windows[k - 1 : k + 1]
    wide = run_windows(
        ctx, TRIAL, data, "base", around_gap, policy=scale_policy(ctx.policy, Decimal(1000))
    )
    check_trades(wide.trades, capital=Decimal(1000))
    assert {SIGNAL, "STOP", "STOP_GAP"} <= {t["exit_reason"] for t in wide.trades}


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def with_stall(fixture: DailyFixture, at: date) -> DailyFixture:
    """The fixture with a market-wide stall in the equity ETFs from session ``at`` (SYNTHETIC).

    30 sessions rising 0.5% (an uptrend), 3 falling 1.5% (RSI(2) far below 10), then 14
    falling 0.1%: every close stays below SMA(5) (no exit signal) and far above a 3-ATR stop,
    so only the time exit can close the positions. Later bars are rescaled to continue from the
    new price (no jump). Opens equal the previous close, so next-open limits are marketable.
    """
    pattern = [Decimal("0.005")] * 30 + [Decimal("-0.015")] * 3 + [Decimal("-0.001")] * 14
    out = dict(fixture.bars)
    for iid in EQUITY_ETFS:
        old = fixture.bars[iid]
        i0 = next(i for i, b in enumerate(old) if CALENDAR.session_date_of(b.start) >= at)
        new = list(old[:i0])
        price = old[i0 - 1].close
        for k, r in enumerate(pattern):
            b = old[i0 + k]
            session = CALENDAR.session_on(CALENDAR.session_date_of(b.start))
            assert session is not None
            c = _q(price * (1 + r))
            hi, lo = _q(max(price, c) * Decimal("1.001")), _q(min(price, c) * Decimal("0.999"))
            new.append(session_bar(iid, session, price, hi, lo, c, b.volume))
            price = c
        factor = price / old[i0 + len(pattern) - 1].close
        for b in old[i0 + len(pattern) :]:
            session = CALENDAR.session_on(CALENDAR.session_date_of(b.start))
            assert session is not None
            o, h, lo, c = (_q(v * factor) for v in (b.open, b.high, b.low, b.close))
            new.append(session_bar(iid, session, o, h, lo, c, b.volume))
        out[iid] = new
    return dataclasses.replace(fixture, bars=out)


def test_a_stall_without_an_exit_signal_ends_in_the_ten_session_time_exit(
    fixture_data: DailyFixture,
) -> None:
    stalled = with_stall(fixture_data, date(2019, 4, 1))
    cfg = study_config([TRIAL], warmup_sessions=200)
    ctx, data = daily_study(cfg, stalled)
    window = (datetime(2019, 4, 1, tzinfo=UTC), datetime(2019, 7, 1, tzinfo=UTC))
    run = run_windows(ctx, TRIAL, data, "base", [window])
    check_trades(run.trades, capital=Decimal(50))
    timed = [t for t in run.trades if t["exit_reason"] == "TIME_EXIT"]
    assert timed, "the stall must end in time exits"
    for t in timed:
        entry = datetime.fromisoformat(str(t["entry"]["time"]).replace("Z", "+00:00"))
        exit_time = datetime.fromisoformat(str(t["exits"][-1]["time"]).replace("Z", "+00:00"))
        deadline = CALENDAR.sessions_after(CALENDAR.session_date_of(entry), 10).open
        assert exit_time == deadline  # held through ten sessions, sold at the next open


def test_the_rule_never_proposes_a_short(fixture_data: DailyFixture) -> None:
    rule = DipBuy(SPEC, CALENDAR, {})
    seen = 0
    for bar in timeline(fixture_data.bars.values()):
        candidate = rule.on_bar(bar, bar.available_time)
        if candidate is None:
            continue
        seen += 1
        assert candidate.direction is Direction.LONG
        assert candidate.stop_price < candidate.reference_price
        assert candidate.instrument_id in EQUITY_ETFS
    assert seen >= 100
    assert set(UNIVERSE) - set(EQUITY_ETFS) == {"XNAS:TLT", "XNAS:IEF", "ARCX:GLD"}
