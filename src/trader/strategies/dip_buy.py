"""Candidate C - dip-buy in an uptrend on daily ETF bars (feature version ``daily-v1``).

Published rule (frozen; no parameter was chosen or tuned on our data): L. Connors and
C. Alvarez, *Short Term Trading Strategies That Work: A Quantified Guide to Trading Stocks and
ETFs* (TradingMarkets Publishing Group, 2008), the 2-period RSI strategy:

1. the close is above its 200-day simple moving average (the long-term trend is up);
2. the 2-period RSI closes below 10 (a short-term dip) -> buy;
3. sell when the close is above the 5-day simple moving average.

How this module implements it (strategy_and_profitability.md §5: a separate experiment family
with a volatility-based invalidation stop, an exit near a reference and a bounded hold;
risk_and_validation.md §5: a frozen specification registered before any run):

* **Data.** One completed bar per XNYS regular session (``start`` = the open, ``end`` = the
  close or early close). Anything else is refused (``ValueError``): the indicators are defined
  on daily closes. Indicators come from :class:`~trader.features.daily.DailyFeatures`
  (``daily-v1``): exact SMA(200) and SMA(5) of closes, Wilder RSI(2), Wilder ATR(10). A missing
  session resets an instrument's state, so no signal is taken until 200 contiguous sessions
  have been seen again (fail closed); a split rescales the state point-in-time.
* **Setup** on a completed session: ``close > SMA(200)`` and ``RSI(2) < 10`` (both strict).
* **Entry** at the next session's open (``holding: OVERNIGHT``: the research engine's next-open
  order with a marketable limit of the estimated ask plus ``entry_limit_offset_bps``). The book
  buys on the signal close; a close is never a fill price here (strategy_and_profitability.md
  §4.6; ADR-0017), so the next open is the first executable price after the decision. A next
  open above the limit is a missed entry, never a chase.
* **Exit signal**: a completed session whose close is above SMA(5) sells at the next open
  (``SIGNAL:close_above_sma_5``), the book's exit moved to the next executable price. Before
  SMA(5) is warm again after a reset no signal is given; the time exit and the stop still apply.
* **Time exit** after ``max_hold_sessions`` = 10 exchange sessions (the engine sells at the
  first open at or after the 10th session after the entry session). Not in the book: a bounded
  hold that strategy_and_profitability.md §5 requires.
* **Disaster stop** = signal close - 3 x ATR(10), rounded down to the tick. The book reports that
  stops hurt this rule and uses none; the policy needs an exit plan to size a trade
  (``NO_EXIT_PLAN`` otherwise), so the stop is wide and exists for sizing and for crashes. It
  is checked during every bar; a gap through it sells at the open (``STOP_GAP``).
* **Target** = SMA(5) at the signal, rounded down to the tick, with ``target_role: COST_GATE``:
  it feeds only the sizer's cost-coverage gate (ADR-0019: the planned reversion to the 5-day
  mean must cover the round-trip costs at the exact size) and is never a resting profit exit,
  because the book's profit exit is the close above the moving SMA(5), not a limit. A signal
  whose target does not exceed the entry limit is still emitted and the engine rejects it as
  ``TOO_COSTLY`` (``target_below_entry_bound``), so it is visible in the candidate outcomes.
* **Long only**, one position per instrument (the engine refuses a candidate for an instrument
  it holds or has a pending entry for). Only the spec's ``universe_symbols`` (the 13 equity ETFs)
  are traded; other instruments of a shared study are ignored.

``features(instrument_id)`` returns the ``daily-v1`` snapshot hashed into the instrument's most
recent candidate (``snapshot_hash = content_hash({"version": "daily-v1", "features": ...})``),
as ``research_rules.py`` does for ``streaming-v1``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.domain.base import ConfigModel
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.ids import deterministic_id
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, NonNegDec, PosDec, Track
from trader.features.daily import FEATURE_VERSION, DailyFeatures, DailyWindows
from trader.strategies.registry import RuleFamily

FAMILY = "CANDIDATE_C"
SPEC_FILE = Path(__file__).parent / "specs" / "candidate_c_etf_daily_v1.yaml"
DEFAULT_TICK = Decimal("0.01")
HUNDRED = Decimal(100)

#: The registration's description of the features the rule uses.
FEATURES: tuple[str, ...] = (
    "daily close (completed XNYS session bar)",
    "SMA(200) of daily closes (trend filter)",
    "Wilder RSI(2) of daily closes (entry)",
    "SMA(5) of daily closes (exit signal; cost-gate target at the signal)",
    "Wilder ATR(10) (disaster stop)",
)


class DipBuySpec(ConfigModel):
    """Frozen Candidate C specification (``specs/candidate_c_etf_daily_v1.yaml``)."""

    strategy_id: str
    version: int = Field(ge=1)
    family: Literal["CANDIDATE_C"] = "CANDIDATE_C"
    track: Track
    feature_version: str
    #: Engine declarations (research/engine.py): carried across sessions, next-open entries.
    holding: Literal["OVERNIGHT"]
    target_role: Literal["COST_GATE"]
    max_hold_sessions: int = Field(ge=1)
    #: Symbols traded (the part of an instrument id after ``VENUE:``).
    universe_symbols: tuple[str, ...] = Field(min_length=1)
    trend_sma_sessions: int = Field(ge=2)
    rsi_period: int = Field(ge=1)
    rsi_entry_below: PosDec
    exit_sma_sessions: int = Field(ge=1)
    atr_period: int = Field(ge=1)
    stop_atr_multiplier: PosDec
    entry_limit_offset_bps: NonNegDec
    entry_ttl_seconds: int = Field(gt=0)
    gap_allowance_bps: NonNegDec
    research_status: str

    @model_validator(mode="after")
    def _consistent(self) -> DipBuySpec:
        if self.feature_version != FEATURE_VERSION:
            raise ValueError("rule specification and feature implementation versions differ")
        if self.rsi_entry_below >= HUNDRED:
            raise ValueError("rsi_entry_below must be below 100")
        if self.exit_sma_sessions >= self.trend_sma_sessions:
            raise ValueError("the exit SMA must be shorter than the trend SMA")
        if len(set(self.universe_symbols)) != len(self.universe_symbols):
            raise ValueError("universe_symbols must be distinct")
        return self

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="python"))

    def windows(self) -> DailyWindows:
        return DailyWindows(
            sma_periods=(self.exit_sma_sessions, self.trend_sma_sessions),
            rsi_period=self.rsi_period,
            atr_period=self.atr_period,
        )

    @property
    def min_warmup_sessions(self) -> int:
        """Contiguous sessions before every value is ready: a study's ``warmup_sessions``
        should be at least this, so its first window starts warm."""
        return self.windows().warmup_sessions

    @property
    def exit_reason(self) -> str:
        """The exit-signal reason (reported by the engine as ``SIGNAL:<reason>``)."""
        return f"close_above_sma_{self.exit_sma_sessions}"


def symbol_of(instrument_id: str) -> str:
    """``"ARCX:SPY"`` -> ``"SPY"`` (an id without a venue is its own symbol)."""
    return instrument_id.rsplit(":", 1)[-1]


def _down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


class DipBuy:
    """Candidate C research rule: one :class:`DailyFeatures` state per traded instrument."""

    uses_source_vwap = False

    def __init__(
        self,
        spec: DipBuySpec,
        calendar: ExchangeCalendar | Utc247Calendar,
        ticks: Mapping[str, Decimal] | None = None,
    ) -> None:
        if not isinstance(calendar, ExchangeCalendar):
            raise ValueError("Candidate C needs an exchange calendar (one bar per session)")
        self.spec = spec
        self.calendar = calendar
        self.ticks = dict(ticks or {})
        self._windows = spec.windows()
        self._universe = frozenset(spec.universe_symbols)
        self._state: dict[str, DailyFeatures] = {}
        self._features: dict[str, dict[str, Decimal]] = {}

    @property
    def strategy_id(self) -> str:
        return self.spec.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    @property
    def snapshot_version(self) -> str:
        """Schema of the snapshots behind this rule's ``snapshot_hash`` values."""
        return FEATURE_VERSION

    def trades(self, instrument_id: str) -> bool:
        """Whether the instrument is in the specification's universe."""
        return symbol_of(instrument_id) in self._universe

    def state(self, instrument_id: str) -> DailyFeatures:
        state = self._state.get(instrument_id)
        if state is None:
            state = DailyFeatures(self._windows, self.calendar)
            self._state[instrument_id] = state
        return state

    def features(self, instrument_id: str) -> Mapping[str, Decimal]:
        """The ``daily-v1`` snapshot behind the instrument's most recent candidate.

        These are exactly the values hashed into that candidate's ``snapshot_hash``.
        """
        snapshot = self._features.get(instrument_id)
        if snapshot is None:
            raise LookupError(f"no candidate has been emitted for {instrument_id}")
        return dict(snapshot)

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        state = self._state.get(instrument_id)
        if state is not None:
            state.rescale(divisor)

    def gaps(self) -> int:
        return sum(s.gaps for s in self._state.values())

    def data_ok(self, instrument_id: str) -> bool:
        """Always true: a missing session resets the state, so every window used is contiguous."""
        del instrument_id
        return True

    # ------------------------------------------------------------------ decisions
    def _session_day(self, bar: Bar) -> date:
        """The bar's session date; refuses a bar that is not one whole regular session."""
        day = self.calendar.session_date_of(bar.start)
        session = self.calendar.session_on(day)
        if session is None or bar.start != session.open or bar.end != session.close:
            raise ValueError(
                f"{bar.instrument_id}: Candidate C needs one bar per regular session "
                f"(got {bar.start.isoformat()} .. {bar.end.isoformat()})"
            )
        return day

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        if not bar.is_usable_at(now):
            raise ValueError("bar used before its availability time")
        if not self.trades(bar.instrument_id):
            return None
        day = self._session_day(bar)
        state = self.state(bar.instrument_id)
        state.update(bar)
        if not state.ready:
            return None
        s = self.spec
        close = state.close
        if close <= state.sma(s.trend_sma_sessions):
            return None
        if state.rsi() >= s.rsi_entry_below:
            return None
        volatility = state.atr()
        if volatility <= 0:
            return None
        tick = self.ticks.get(bar.instrument_id, DEFAULT_TICK)
        stop = _down(close - s.stop_atr_multiplier * volatility, tick)
        target = _down(state.sma(s.exit_sma_sessions), tick)
        if stop <= 0 or stop >= close or target <= 0:
            return None
        return self._candidate(bar, day, state, stop, target)

    def exit_signal(self, instrument_id: str, bar: Bar) -> str | None:
        """After the engine fed ``bar`` to :meth:`on_bar`: the reason to sell a held position
        at the next open when the completed close is above SMA(5), else ``None``."""
        state = self._state.get(instrument_id)
        if state is None or not self.trades(instrument_id):
            return None
        if state.session != self.calendar.session_date_of(bar.start):
            raise ValueError(f"{instrument_id}: exit_signal for a bar the rule has not seen")
        try:
            mean = state.sma(self.spec.exit_sma_sessions)
        except LookupError:  # not warm again after a missing session
            return None
        return self.spec.exit_reason if state.close > mean else None

    def _candidate(
        self,
        bar: Bar,
        day: date,
        state: DailyFeatures,
        stop: Decimal,
        target: Decimal,
    ) -> TradeCandidate:
        s = self.spec
        snapshot = state.snapshot()
        entry = self.calendar.sessions_after(day)
        deadline = self.calendar.sessions_after(entry.session_date, s.max_hold_sessions)
        # TradeCandidate requires minutes; the engine counts max_hold_sessions instead. This is
        # the same deadline in wall-clock minutes from the next open (the fill time).
        hold_minutes = -(-int((deadline.open - entry.open).total_seconds()) // 60)
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
            reference_price=state.close,
            stop_price=stop,
            target_price=target,
            max_hold_minutes=hold_minutes,
            entry_limit_offset_bps=s.entry_limit_offset_bps,
            entry_ttl_seconds=s.entry_ttl_seconds,
            gap_allowance_fraction=s.gap_allowance_bps * BPS,
            snapshot_hash=content_hash({"version": FEATURE_VERSION, "features": snapshot}),
            # valid through the next-open order's life (the engine trades it at that open only)
            expires_at=entry.open + timedelta(seconds=s.entry_ttl_seconds),
        )
        self._features[bar.instrument_id] = snapshot
        return candidate


def _build(spec: Any, calendar: Any, ticks: Mapping[str, Decimal]) -> DipBuy:
    if not isinstance(spec, DipBuySpec):
        raise TypeError(f"Candidate C needs a DipBuySpec, not {type(spec).__name__}")
    return DipBuy(spec, calendar, ticks)


RULE_FAMILY = RuleFamily(
    family=FAMILY,
    label="Candidate C (dip-buy in an uptrend)",
    parse=DipBuySpec.model_validate,
    build=_build,
    features=FEATURES,
)
