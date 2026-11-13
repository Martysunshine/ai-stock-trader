"""Candidate D - trend timing on month-end closes (Faber, 2007).

**Published rule.** M. T. Faber, "A Quantitative Approach to Tactical Asset Allocation", *The
Journal of Wealth Management* 9(4), Spring 2007, pp. 69-79 (SSRN 962461): at each month end,
hold an asset while its monthly close is above its 10-month simple moving average and move to
cash when the monthly close is below it. The signal is looked at once a month; between month
ends nothing changes.

**How this module implements it** (frozen specifications
``specs/candidate_d_etf_monthly_v1.yaml`` and ``specs/candidate_d_crypto_monthly_v1.yaml``;
strategy_and_profitability.md §1 and §9, risk_and_validation.md §3, §5 and §6):

* *Bars and month ends.* One completed bar per XNYS regular session, consumed through
  ``features/daily.py`` (``daily-v1``). A *month-end session* is the last XNYS session of a
  calendar month (its next session is in a later month), read from the exchange calendar: a
  month whose last weekday is a holiday ends on the session before it (Good Friday, 30 March
  2018 -> 29 March), and an early close can be a month end (Friday 29 November 2019, 13:00 New
  York). The calendar's published schedule is known in advance; an unscheduled closure on a
  month's last scheduled session would make the previous session a month end only in
  hindsight, and none occurred in 2016-2026 (the ad hoc closures 2018-12-05 and 2025-01-09 are
  mid-month; ``tests/test_trend_timing.py`` checks this).
* *The 24/7 calendar (track CRYPTO).* A CRYPTO specification runs on the UTC-day calendar
  (``Utc247Calendar``): one completed bar per UTC day ``[00:00, 24:00)`` UTC, and the month-end
  session is the month's last calendar day, whose bar closes at 00:00 UTC on the 1st of the next
  month. There are no holidays, so month ends are known years ahead. Every other mechanism below
  is the same; only the session counts differ (``max_hold_sessions`` counts UTC days, and the
  warm-up bound is ``31 x month_end_sma_months`` days, :meth:`TrendTimingSpec.warmup_sessions_on`).
  The calendar must match the track: a CRYPTO specification refuses the exchange calendar and
  any other refuses the UTC-day calendar.
* *Monthly SMA.* The mean of the last ``month_end_sma_months`` month-end closes, the current
  month end included. Only completed month-end bars ever enter it, so a decision at a month end
  uses that session's completed close and earlier month-end closes, never a later price.
* *Entry.* At a month-end close strictly above the SMA the rule emits a long candidate. The
  specification declares ``holding: OVERNIGHT``, so the research engine fills it at the next
  session's open (a next-open order), never at the signal close. A candidate is emitted at
  every such month end, also while the instrument is held; the engine then rejects it
  (``RISK_LIMIT`` / ``instrument_busy``), which for this family means "keep holding". A
  position closed by its stop, target or time exit is therefore re-entered at the next month
  end whose close is still above the SMA, as the published rule would be invested again.
* *Exit signal.* At a month-end close strictly below the SMA, :meth:`TrendTiming.exit_signal`
  returns ``CLOSE_BELOW_MONTHLY_SMA`` and the engine sells at the next open
  (``SIGNAL:CLOSE_BELOW_MONTHLY_SMA``). A close equal to the SMA neither enters nor exits (the
  publication's strict "above" and "below").
* *Risk plumbing that the USD 50 policy needs* (not in the publication; declared, never tuned):
  a protective disaster stop ``signal close - stop_atr_multiplier x Wilder ATR(atr_period)`` of
  daily true ranges, which sizes the position (risk_and_validation.md §3) and rests below it for
  the whole hold; a target ``signal close + target_risk_multiple x (signal close - stop)``,
  declared as a resting exit (``target_role: EXIT``) that truncates rare outsized runs and
  feeds the sizer's cost-coverage gate (ADR-0019); and a time exit after
  ``max_hold_sessions`` exchange sessions (``max_hold_sessions`` is read by the engine).
* *Missing data (fail closed).* A missing session resets the instrument's ``daily-v1`` state,
  so the SMA is unavailable until enough new month ends have passed. No entry is possible
  meanwhile, and a held position is sold after the next month end with the reason
  ``MONTHLY_SMA_UNAVAILABLE``: the rule cannot confirm the trend, so it does not keep the risk.
  Weekends, holidays and early closes are not missing sessions.
* *Splits* rescale the stored state point-in-time (:meth:`TrendTiming.on_split`), so a split
  never looks like a trend break and no warm-up is lost.
* *Universe.* Bars of instruments outside the specification's ``universe`` are ignored: they
  build no state and never produce a candidate or an exit signal.

``TradeCandidate`` requires ``max_hold_minutes``; the engine ignores it when
``max_hold_sessions`` is declared. The candidate carries a wall-clock *lower* bound of that
many sessions (``N`` XNYS sessions span at least ``N // 5`` whole weeks; ``N`` UTC days are
exactly ``N x 1440`` minutes), so any consumer that counts minutes exits no later than the
session rule. The candidate expires ``entry_ttl_seconds`` after the next session's open (the
next-open order's lifetime).

``features(instrument_id)`` returns the ``daily-v1`` snapshot hashed into the instrument's most
recent candidate (``snapshot_hash``), like the streaming families.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator

from trader.data.calendars import ExchangeCalendar, SessionCalendar, Utc247Calendar
from trader.domain.base import ConfigModel
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.ids import deterministic_id
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, NonNegDec, PosDec, Track
from trader.features.daily import FEATURE_VERSION, DailyFeatures, DailyWindows
from trader.strategies.registry import RuleFamily

FAMILY = "CANDIDATE_D"
DEFAULT_TICK = Decimal("0.01")
#: Exit-signal reasons (reported by the engine as ``SIGNAL:<reason>``).
EXIT_BELOW_SMA = "CLOSE_BELOW_MONTHLY_SMA"
EXIT_SMA_UNAVAILABLE = "MONTHLY_SMA_UNAVAILABLE"
#: ``<MIC>:<symbol>``, the instrument-id form of the daily-bar design (e.g. ``ARCX:SPY``).
_INSTRUMENT_ID = re.compile(r"^[A-Z]{4}:[A-Z][A-Z0-9.]*$")
#: ``<VENUE>:<BASE>/<QUOTE>``, the crypto spot instrument id (e.g. ``KRAKEN:BTC/USD``).
_CRYPTO_INSTRUMENT_ID = re.compile(r"^[A-Z]+:[A-Z0-9]+/[A-Z0-9]+$")
_MINUTES_PER_WEEK = 7 * 24 * 60
_MINUTES_PER_DAY = 24 * 60


class TrendTimingSpec(ConfigModel):
    """Frozen Candidate D specification (every field is bound into a trial's registration)."""

    strategy_id: str
    version: int = Field(ge=1)
    family: Literal["CANDIDATE_D"] = "CANDIDATE_D"
    track: Track
    feature_version: str
    #: The publication the rule follows (citation text).
    source: str = Field(min_length=1)
    universe: tuple[str, ...] = Field(min_length=1)
    #: Engine declarations (research/engine.py): multi-day holds with next-open entries, the
    #: time exit in exchange sessions, and the role of the target.
    holding: Literal["OVERNIGHT"]
    max_hold_sessions: int = Field(ge=5)
    target_role: Literal["EXIT", "COST_GATE"]
    use_source_vwap: Literal[False]
    #: Faber (2007): the SMA of the last 10 month-end closes.
    month_end_sma_months: int = Field(ge=1)
    #: Disaster stop: signal close - stop_atr_multiplier x Wilder ATR(atr_period_sessions).
    atr_period_sessions: int = Field(ge=1)
    stop_atr_multiplier: PosDec
    #: Target: signal close + target_risk_multiple x (signal close - stop).
    target_risk_multiple: PosDec
    #: The next-open order is a marketable limit this far above the estimated ask (a collar).
    entry_limit_offset_bps: NonNegDec
    entry_ttl_seconds: int = Field(gt=0)
    gap_allowance_bps: NonNegDec
    research_status: str

    @field_validator("feature_version")
    @classmethod
    def _daily_features(cls, value: str) -> str:
        if value != FEATURE_VERSION:
            raise ValueError(
                f"rule specification and feature implementation versions differ "
                f"({value!r} is not {FEATURE_VERSION!r})"
            )
        return value

    @field_validator("universe")
    @classmethod
    def _distinct_instrument_ids(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("universe instrument ids must be distinct")
        if info.data.get("track") is Track.CRYPTO:
            odd = [iid for iid in value if not _CRYPTO_INSTRUMENT_ID.match(iid)]
            if odd:
                raise ValueError(f"crypto universe entries must look like 'KRAKEN:BTC/USD': {odd}")
            return value
        bad = [iid for iid in value if not _INSTRUMENT_ID.match(iid)]
        if bad:
            raise ValueError(f"universe entries must look like 'ARCX:SPY', not {bad}")
        return value

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="python"))

    def windows(self) -> DailyWindows:
        return DailyWindows(
            atr_period=self.atr_period_sessions,
            month_end_sma_months=self.month_end_sma_months,
        )

    @property
    def warmup_sessions(self) -> int:
        """Sessions of contiguous bars that certainly make every indicator ready (a study's
        ``warmup_sessions`` should be at least this; 230 for ten month ends)."""
        return self.windows().warmup_sessions

    def warmup_sessions_on(self, calendar: SessionCalendar) -> int:
        """:attr:`warmup_sessions` on ``calendar`` (310 UTC days for ten month ends)."""
        return self.windows().warmup_sessions_on(calendar)

    @property
    def candidate_max_hold_minutes(self) -> int:
        """Wall-clock lower bound of ``max_hold_sessions`` (``N`` sessions >= ``N // 5`` weeks)."""
        return (self.max_hold_sessions // 5) * _MINUTES_PER_WEEK

    def max_hold_minutes_on(self, calendar: SessionCalendar) -> int:
        """:attr:`candidate_max_hold_minutes` on ``calendar``; ``N`` UTC days are exactly
        ``N x 1440`` minutes."""
        if isinstance(calendar, ExchangeCalendar):
            return self.candidate_max_hold_minutes
        return self.max_hold_sessions * _MINUTES_PER_DAY


def parse_spec(raw: dict[str, Any]) -> TrendTimingSpec:
    """Validate a raw specification mapping (registry ``parse``)."""
    return TrendTimingSpec.model_validate(raw)


def _down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


class TrendTiming:
    """Candidate D research rule: one ``daily-v1`` state per universe instrument."""

    spec: TrendTimingSpec

    def __init__(
        self,
        spec: TrendTimingSpec,
        calendar: ExchangeCalendar | Utc247Calendar,
        ticks: Mapping[str, Decimal] | None = None,
    ) -> None:
        if spec.track is Track.CRYPTO:
            if not isinstance(calendar, Utc247Calendar):
                raise ValueError(
                    "Candidate D on track CRYPTO needs the UTC-day calendar (a session is a UTC "
                    "day; the month end is the month's last UTC day)"
                )
        elif not isinstance(calendar, ExchangeCalendar):
            raise ValueError("Candidate D needs an exchange calendar (month-end sessions)")
        self.spec = spec
        self.calendar: SessionCalendar = calendar
        self._max_hold_minutes = spec.max_hold_minutes_on(calendar)
        self.ticks = dict(ticks or {})
        self.universe = frozenset(spec.universe)
        self._windows = spec.windows()
        self._state: dict[str, DailyFeatures] = {}
        self._features: dict[str, dict[str, Decimal]] = {}

    # ------------------------------------------------------------------ identity
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

    # ------------------------------------------------------------------ state
    def state(self, instrument_id: str) -> DailyFeatures:
        if instrument_id not in self.universe:
            raise KeyError(f"{instrument_id} is not in the {self.spec.strategy_id} universe")
        state = self._state.get(instrument_id)
        if state is None:
            state = DailyFeatures(self._windows, self.calendar)
            self._state[instrument_id] = state
        return state

    def features(self, instrument_id: str) -> Mapping[str, Decimal]:
        """The ``daily-v1`` snapshot hashed into the instrument's most recent candidate."""
        snapshot = self._features.get(instrument_id)
        if snapshot is None:
            raise LookupError(f"no candidate has been emitted for {instrument_id}")
        return dict(snapshot)

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        """Apply a split point-in-time (``divisor`` = shares after per share before)."""
        if instrument_id in self.universe:
            self.state(instrument_id).rescale(divisor)

    def gaps(self) -> int:
        """Missing sessions seen (each one reset an instrument's state)."""
        return sum(s.gaps for s in self._state.values())

    def data_ok(self, instrument_id: str) -> bool:
        """Always true: a missing session resets the state, so every value is contiguous."""
        del instrument_id
        return True

    # ------------------------------------------------------------------ decisions
    def _consume(self, bar: Bar) -> DailyFeatures:
        state = self.state(bar.instrument_id)
        state.update(bar)
        return state

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        """Update the instrument's state; at a month-end close above the SMA, a candidate."""
        if not bar.is_usable_at(now):
            raise ValueError("bar used before its availability time")
        if bar.instrument_id not in self.universe:
            return None
        state = self._consume(bar)
        if not state.is_month_end or not state.ready:
            return None
        s = self.spec
        close = state.close
        if close <= state.month_end_sma():
            return None
        atr = state.atr()
        if atr <= 0:
            return None
        tick = self.ticks.get(bar.instrument_id, DEFAULT_TICK)
        stop = _down(close - s.stop_atr_multiplier * atr, tick)
        if stop <= 0 or stop >= close:
            return None
        target = _up(close + s.target_risk_multiple * (close - stop), tick)
        return self._candidate(bar, state, stop, target)

    def exit_signal(self, instrument_id: str, bar: Bar) -> str | None:
        """At a month-end close: ``CLOSE_BELOW_MONTHLY_SMA`` below the SMA, or
        ``MONTHLY_SMA_UNAVAILABLE`` when a missing session left the SMA cold (fail closed)."""
        if instrument_id not in self.universe:
            return None
        if bar.instrument_id != instrument_id:
            raise ValueError("exit_signal got a bar of another instrument")
        state = self._state.get(instrument_id)
        if state is None or state.session != self.calendar.session_date_of(bar.start):
            raise ValueError(f"{instrument_id}: exit_signal before on_bar consumed this bar")
        if not state.is_month_end:
            return None
        if not state.ready:
            return EXIT_SMA_UNAVAILABLE
        if state.close < state.month_end_sma():
            return EXIT_BELOW_SMA
        return None

    def _candidate(
        self, bar: Bar, state: DailyFeatures, stop: Decimal, target: Decimal
    ) -> TradeCandidate:
        s = self.spec
        snapshot = state.snapshot()
        session = state.session
        if session is None:  # pragma: no cover - a candidate always follows an update
            raise LookupError("no session")
        next_open = self.calendar.sessions_after(session).open
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
            max_hold_minutes=self._max_hold_minutes,
            entry_limit_offset_bps=s.entry_limit_offset_bps,
            entry_ttl_seconds=s.entry_ttl_seconds,
            gap_allowance_fraction=s.gap_allowance_bps * BPS,
            snapshot_hash=content_hash({"version": FEATURE_VERSION, "features": snapshot}),
            expires_at=next_open + timedelta(seconds=s.entry_ttl_seconds),
        )
        self._features[bar.instrument_id] = snapshot
        return candidate


def build_rule(
    spec: TrendTimingSpec,
    calendar: ExchangeCalendar | Utc247Calendar,
    ticks: Mapping[str, Decimal] | None = None,
) -> TrendTiming:
    return TrendTiming(spec, calendar, ticks)


def _build(spec: Any, calendar: ExchangeCalendar | Utc247Calendar, ticks: Any) -> TrendTiming:
    if not isinstance(spec, TrendTimingSpec):
        raise TypeError(f"{FAMILY} needs a TrendTimingSpec, not {type(spec).__name__}")
    return build_rule(spec, calendar, ticks)


#: The registration's feature description. The XNYS text is bound into registered trials
#: (T-0014) and never changes; a CRYPTO specification registers the UTC-day text.
FEATURES: tuple[str, ...] = (
    "month-end session (last XNYS session of the month, from the exchange calendar)",
    "SMA of the last k month-end closes (the current month end included)",
    "Wilder ATR of daily true ranges (disaster stop)",
)
CRYPTO_FEATURES: tuple[str, ...] = (
    "month-end session (last UTC day of the month; one bar per UTC day)",
    "SMA of the last k month-end closes (the current month end included)",
    "Wilder ATR of daily (UTC-day) true ranges (disaster stop)",
)


def _features_of(spec: Any) -> tuple[str, ...]:
    return CRYPTO_FEATURES if getattr(spec, "track", None) is Track.CRYPTO else FEATURES


#: Registry entry (``strategies/registry.py`` imports this module on first use).
RULE_FAMILY = RuleFamily(
    family=FAMILY,
    label="Candidate D (trend timing)",
    parse=parse_spec,
    build=_build,
    features=FEATURES,
    features_of=_features_of,
)
