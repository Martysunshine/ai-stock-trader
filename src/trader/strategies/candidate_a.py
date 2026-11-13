"""Candidate A - intraday trend continuation (strategy_and_profitability.md §4).

On each completed five-minute bar (used only after its availability time):

1. the decision must fall inside the calendar-relative entry window (first and last 30 minutes
   of the regular session excluded, early closes included);
2. the bar closes above the highest high of the previous six completed bars (the candidate bar
   itself is excluded from that range);
3. the close is above the same-feed session VWAP;
4. the fifteen-minute context is upward: the last *completed* 15-minute bar closed above the
   session's first trade and the trailing three-bar return is positive;
5. the stop is ``close - atr_stop_multiplier * ATR(atr_period)``, the target is
   ``close + reward_risk_multiple * (close - stop)``, the hold is at most ``max_hold_minutes``.

Entry happens later, through a bounded limit order at the then-current ask; the bar's close is
never assumed as a fill price.

The ``momentum-v1`` values behind each candidate's ``snapshot_hash`` come from
:func:`snapshot_features`; :meth:`CandidateA.features` returns those of an instrument's most
recent candidate so research can label and filter it without recomputing anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import Field

from trader.config.policy import load_yaml_strict
from trader.data.bars import aggregate
from trader.data.calendars import ExchangeCalendar
from trader.domain.base import ConfigModel
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.ids import deterministic_id
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, NonNegDec, PosDec, Track
from trader.features.momentum import FEATURE_VERSION, atr, prior_high, session_vwap, trailing_return

SPEC_PATH = Path(__file__).parent / "specs" / "candidate_a_v1.yaml"


class CandidateASpec(ConfigModel):
    strategy_id: str
    version: int = Field(ge=1)
    feature_version: str
    decision_bar_minutes: int
    context_bar_minutes: int
    breakout_lookback_bars: int = Field(ge=2)
    atr_period_bars: int = Field(ge=2)
    atr_stop_multiplier: PosDec
    reward_risk_multiple: PosDec
    max_hold_minutes: int = Field(gt=0)
    entry_limit_offset_bps: NonNegDec
    entry_ttl_seconds: int = Field(gt=0)
    candidate_ttl_seconds: int = Field(gt=0)
    gap_allowance_bps: NonNegDec
    require_above_session_vwap: bool
    require_positive_context: bool
    research_status: str

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="python"))


def load_candidate_a_spec(path: Path = SPEC_PATH) -> CandidateASpec:
    spec = CandidateASpec.model_validate(load_yaml_strict(path))
    if spec.feature_version != FEATURE_VERSION:
        raise ValueError("strategy spec and feature implementation versions differ")
    return spec


def snapshot_features(session_bars: list[Bar], spec: CandidateASpec) -> dict[str, Decimal]:
    """The ``momentum-v1`` snapshot of the last completed session bar (hashed into candidates)."""
    last = session_bars[-1]
    return {
        "close": last.close,
        "prior_high": prior_high(session_bars, spec.breakout_lookback_bars),
        "session_vwap": session_vwap(session_bars),
        "atr": atr(session_bars, spec.atr_period_bars),
        "trailing_return_3": trailing_return(session_bars, 3),
    }


class CandidateA:
    def __init__(self, spec: CandidateASpec, calendar: ExchangeCalendar) -> None:
        self.spec = spec
        self.calendar = calendar
        self._features: dict[str, dict[str, Decimal]] = {}

    def features(self, instrument_id: str) -> Mapping[str, Decimal]:
        """The snapshot behind the instrument's most recent candidate (its hashed values)."""
        snapshot = self._features.get(instrument_id)
        if snapshot is None:
            raise LookupError(f"no candidate has been emitted for {instrument_id}")
        return dict(snapshot)

    def on_bar(self, bars: list[Bar], now: datetime) -> TradeCandidate | None:
        """Evaluate the most recent completed bar of one instrument."""
        if not bars:
            return None
        last = bars[-1]
        if not last.is_usable_at(now):
            return None
        session = self.calendar.session_at(last.start)
        if session is None:
            return None
        start, end = self.calendar.entry_window(session)
        if not (start <= now < end):
            return None
        session_bars = [b for b in bars if b.start >= session.open and b.end <= last.end]
        s = self.spec
        need = max(s.breakout_lookback_bars, s.atr_period_bars) + 1
        if len(session_bars) < need:
            return None
        if last.close <= prior_high(session_bars, s.breakout_lookback_bars):
            return None
        vwap = session_vwap(session_bars)
        if s.require_above_session_vwap and last.close <= vwap:
            return None
        context_ok = self._context_ok(session_bars)
        if s.require_positive_context and not context_ok:
            return None
        volatility = atr(session_bars, s.atr_period_bars)
        stop = last.close - s.atr_stop_multiplier * volatility
        if stop <= 0 or volatility <= 0:
            return None
        target = last.close + s.reward_risk_multiple * (last.close - stop)
        features = snapshot_features(session_bars, s)
        candidate = TradeCandidate(
            candidate_id=deterministic_id(
                "cand", s.strategy_id, s.version, last.instrument_id, last.end.isoformat()
            ),
            strategy_id=s.strategy_id,
            strategy_version=s.version,
            spec_hash=s.spec_hash,
            instrument_id=last.instrument_id,
            track=Track.STOCKS,
            direction=Direction.LONG,
            feed=last.feed,
            bar_end=last.end,
            decision_time=last.available_time,
            reference_price=last.close,
            stop_price=_tick_down(stop),
            target_price=_tick_up(target),
            max_hold_minutes=s.max_hold_minutes,
            entry_limit_offset_bps=s.entry_limit_offset_bps,
            entry_ttl_seconds=s.entry_ttl_seconds,
            gap_allowance_fraction=s.gap_allowance_bps * BPS,
            snapshot_hash=content_hash({"version": FEATURE_VERSION, "features": features}),
            expires_at=last.available_time + timedelta(seconds=s.candidate_ttl_seconds),
        )
        self._features[last.instrument_id] = features
        return candidate

    def _context_ok(self, session_bars: list[Bar]) -> bool:
        s = self.spec
        per = s.context_bar_minutes // s.decision_bar_minutes
        complete = [session_bars[i : i + per] for i in range(0, len(session_bars) - per + 1, per)]
        complete = [grp for grp in complete if len(grp) == per]
        if not complete:
            return False
        context = aggregate(complete[-1], s.context_bar_minutes)
        session_first = session_bars[0].open
        return context.close > session_first and trailing_return(session_bars, 3) > 0


def _tick_down(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding="ROUND_FLOOR")


def _tick_up(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding="ROUND_CEILING")
