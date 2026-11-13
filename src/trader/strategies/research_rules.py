"""Rule families evaluated in research (strategy_and_profitability.md §4-5; CRYPTO §5).

* :class:`TrendBreakout` - Candidate A for a continuous (24/7) market: completed 15-minute bars
  with a one-hour context. A long candidate needs a close above the previous ``N`` completed
  highs (the candidate bar excluded), a close above the rolling volume-weighted reference, and a
  positive return over the last completed context bars.
* :class:`MeanReversion` - Candidate B: a completed-bar close at least ``k`` ATRs below the
  reference in a quiet regime (low efficiency ratio and no volatility expansion), confirmed by a
  stabilising bar (close above its open and above the previous close). The stop is an ATR-based
  invalidation level, the target is the reference itself, the hold is bounded. Whether the
  reference distance covers round-trip costs is decided later by the sizer at the exact size.

Both consume bars through :class:`~trader.features.streaming.RollingBarFeatures` (one state per
instrument) and emit :class:`~trader.domain.candidates.TradeCandidate` objects; neither ever
assumes a fill at the signal bar's close. Parameters come from frozen YAML specifications; any
change is a new research trial.

In ``SESSION`` mode (stocks) the state is reset at each regular-session open, entries are
restricted to the calendar-relative entry window, and the reference is the session VWAP.

``features(instrument_id)`` returns the ``streaming-v1`` snapshot hashed into the instrument's
most recent candidate, so labels and research filters use exactly the decision-time values.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field

from trader.config.policy import load_yaml_strict
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.domain.base import ConfigModel
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.ids import deterministic_id
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, NonNegDec, PosDec, Track
from trader.features.streaming import FEATURE_VERSION, FeatureWindows, RollingBarFeatures

SPEC_DIR = Path(__file__).parent / "specs"
DEFAULT_TICK = Decimal("0.01")


class _RuleSpecBase(ConfigModel):
    strategy_id: str
    version: int = Field(ge=1)
    family: Literal["CANDIDATE_A", "CANDIDATE_B"]
    track: Track
    feature_version: str
    window_mode: Literal["ROLLING", "SESSION"]
    decision_bar_minutes: int = Field(gt=0)
    context_bar_minutes: int = Field(gt=0)
    context_lookback_bars: int = Field(ge=1)
    reference_window_bars: int | None = Field(default=None, ge=2)
    use_source_vwap: bool
    atr_period_bars: int = Field(ge=2)
    long_atr_period_bars: int = Field(ge=2)
    efficiency_window_bars: int = Field(ge=2)
    breakout_lookback_bars: int = Field(ge=2)
    max_hold_minutes: int = Field(gt=0)
    entry_limit_offset_bps: NonNegDec
    entry_ttl_seconds: int = Field(gt=0)
    candidate_ttl_seconds: int = Field(gt=0)
    gap_allowance_bps: NonNegDec
    research_status: str

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="python"))

    def windows(self) -> FeatureWindows:
        return FeatureWindows(
            minutes=self.decision_bar_minutes,
            reference_window_bars=self.reference_window_bars,
            use_source_vwap=self.use_source_vwap,
            atr_period=self.atr_period_bars,
            long_atr_period=self.long_atr_period_bars,
            efficiency_window=self.efficiency_window_bars,
            breakout_lookback=self.breakout_lookback_bars,
            context_minutes=self.context_bar_minutes,
            context_lookback=self.context_lookback_bars,
        )


class TrendBreakoutSpec(_RuleSpecBase):
    family: Literal["CANDIDATE_A"] = "CANDIDATE_A"
    atr_stop_multiplier: PosDec
    reward_risk_multiple: PosDec


class MeanReversionSpec(_RuleSpecBase):
    family: Literal["CANDIDATE_B"] = "CANDIDATE_B"
    entry_deviation_atr: PosDec
    max_efficiency_ratio: PosDec
    max_volatility_ratio: PosDec
    stop_atr_multiplier: PosDec


AnyRuleSpec = TrendBreakoutSpec | MeanReversionSpec


def load_rule_spec(path: Path, overrides: Mapping[str, object] | None = None) -> AnyRuleSpec:
    raw = load_yaml_strict(path)
    if overrides:
        raw = {**raw, **overrides}
    family = raw.get("family")
    spec: AnyRuleSpec
    if family == "CANDIDATE_A":
        spec = TrendBreakoutSpec.model_validate(raw)
    elif family == "CANDIDATE_B":
        spec = MeanReversionSpec.model_validate(raw)
    else:
        raise ValueError(f"unknown rule family {family!r} in {path}")
    if spec.feature_version != FEATURE_VERSION:
        raise ValueError("rule specification and feature implementation versions differ")
    return spec


def _down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


class _RuleBase:
    spec: AnyRuleSpec

    def __init__(
        self,
        spec: AnyRuleSpec,
        calendar: ExchangeCalendar | Utc247Calendar,
        ticks: Mapping[str, Decimal] | None = None,
    ) -> None:
        if spec.window_mode == "SESSION" and not isinstance(calendar, ExchangeCalendar):
            raise ValueError("SESSION mode needs an exchange calendar")
        self.spec = spec
        self.calendar = calendar
        self.ticks = dict(ticks or {})
        self._state: dict[str, RollingBarFeatures] = {}
        self._session: dict[str, object] = {}
        self._features: dict[str, dict[str, Decimal]] = {}

    @property
    def strategy_id(self) -> str:
        return self.spec.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    @property
    def uses_source_vwap(self) -> bool:
        return self.spec.use_source_vwap

    @property
    def snapshot_version(self) -> str:
        """Schema of the snapshots behind this rule's ``snapshot_hash`` values."""
        return FEATURE_VERSION

    def features(self, instrument_id: str) -> Mapping[str, Decimal]:
        """The feature snapshot behind the instrument's most recent candidate.

        These are exactly the values hashed into that candidate's ``snapshot_hash``; research
        labels and filters read them instead of recomputing (docs/ml_research_guide.md §4).
        """
        snapshot = self._features.get(instrument_id)
        if snapshot is None:
            raise LookupError(f"no candidate has been emitted for {instrument_id}")
        return dict(snapshot)

    def state(self, instrument_id: str) -> RollingBarFeatures:
        state = self._state.get(instrument_id)
        if state is None:
            state = RollingBarFeatures(self.spec.windows())
            self._state[instrument_id] = state
        return state

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        self.state(instrument_id).rescale(divisor)

    def gaps(self) -> int:
        return sum(s.gaps for s in self._state.values())

    def data_ok(self, instrument_id: str) -> bool:
        """Always true: a gap resets the streaming state, so every window used is contiguous."""
        del instrument_id
        return True

    def _admit_time(self, bar: Bar, now: datetime) -> tuple[bool, bool]:
        """Session bookkeeping: return (bar belongs to a tradable period, entry window open)."""
        if self.spec.window_mode == "ROLLING":
            return True, True
        if not isinstance(self.calendar, ExchangeCalendar):  # pragma: no cover - checked in init
            raise TypeError("SESSION mode needs an exchange calendar")
        session = self.calendar.session_at(bar.start)
        if session is None:
            return False, False
        if self._session.get(bar.instrument_id) != session.session_date:
            self._session[bar.instrument_id] = session.session_date
            self.state(bar.instrument_id).reset()
        start, end = self.calendar.entry_window(session)
        return True, start <= now < end

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        if not bar.is_usable_at(now):
            raise ValueError("bar used before its availability time")
        usable, in_window = self._admit_time(bar, now)
        if not usable:
            return None
        state = self.state(bar.instrument_id)
        state.update(bar)
        if not in_window or not state.ready:
            return None
        return self._evaluate(bar, state)

    def _evaluate(self, bar: Bar, state: RollingBarFeatures) -> TradeCandidate | None:
        raise NotImplementedError

    def _candidate(
        self,
        bar: Bar,
        state: RollingBarFeatures,
        stop: Decimal,
        target: Decimal,
    ) -> TradeCandidate:
        s = self.spec
        snapshot = state.snapshot()
        candidate = TradeCandidate(
            candidate_id=deterministic_id(
                "cand",
                s.strategy_id,
                s.version,
                s.spec_hash[:16],
                bar.instrument_id,
                bar.end.isoformat(),
            ),
            strategy_id=s.strategy_id,
            strategy_version=s.version,
            spec_hash=s.spec_hash,
            instrument_id=bar.instrument_id,
            track=s.track,
            direction=Direction.LONG,
            feed=bar.feed,
            bar_end=bar.end,
            decision_time=bar.available_time,
            reference_price=bar.close,
            stop_price=stop,
            target_price=target,
            max_hold_minutes=s.max_hold_minutes,
            entry_limit_offset_bps=s.entry_limit_offset_bps,
            entry_ttl_seconds=s.entry_ttl_seconds,
            gap_allowance_fraction=s.gap_allowance_bps * BPS,
            snapshot_hash=content_hash({"version": FEATURE_VERSION, "features": snapshot}),
            expires_at=bar.available_time + timedelta(seconds=s.candidate_ttl_seconds),
        )
        self._features[bar.instrument_id] = snapshot
        return candidate


class TrendBreakout(_RuleBase):
    spec: TrendBreakoutSpec

    def _evaluate(self, bar: Bar, state: RollingBarFeatures) -> TradeCandidate | None:
        s = self.spec
        if bar.close <= state.prior_high():
            return None
        if bar.close <= state.reference_price():
            return None
        if state.context_return() <= 0:
            return None
        volatility = state.atr()
        if volatility <= 0:
            return None
        tick = self.ticks.get(bar.instrument_id, DEFAULT_TICK)
        stop = _down(bar.close - s.atr_stop_multiplier * volatility, tick)
        if stop <= 0 or stop >= bar.close:
            return None
        target = _up(bar.close + s.reward_risk_multiple * (bar.close - stop), tick)
        return self._candidate(bar, state, stop, target)


class MeanReversion(_RuleBase):
    spec: MeanReversionSpec

    def _evaluate(self, bar: Bar, state: RollingBarFeatures) -> TradeCandidate | None:
        s = self.spec
        volatility = state.atr()
        if volatility <= 0:
            return None
        reference = state.reference_price()
        if (bar.close - reference) / volatility > -s.entry_deviation_atr:
            return None
        if state.efficiency_ratio() > s.max_efficiency_ratio:
            return None
        if state.long_atr() <= 0 or volatility / state.long_atr() > s.max_volatility_ratio:
            return None
        previous = list(state.recent)[-2]
        if not (bar.close > bar.open and bar.close > previous.close):
            return None
        tick = self.ticks.get(bar.instrument_id, DEFAULT_TICK)
        stop = _down(bar.close - s.stop_atr_multiplier * volatility, tick)
        target = _down(reference, tick)
        if stop <= 0 or stop >= bar.close or target <= bar.close:
            return None
        return self._candidate(bar, state, stop, target)


def build_rule(
    spec: AnyRuleSpec,
    calendar: ExchangeCalendar | Utc247Calendar,
    ticks: Mapping[str, Decimal] | None = None,
) -> TrendBreakout | MeanReversion:
    if isinstance(spec, TrendBreakoutSpec):
        return TrendBreakout(spec, calendar, ticks)
    return MeanReversion(spec, calendar, ticks)
