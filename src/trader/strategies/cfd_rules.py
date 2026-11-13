"""Rules for CFD studies (ADR-0052, ADR-0055), read on the mid prices of the broker's own quotes.

The two daily rules decide on a completed daily bar (``data/quote_bars.daily_from_hourly``) and are acted
on at the instrument's next open by ``research/cfd_engine.py``. Their indicators are the
``daily-v1`` features of ``features/daily.py``, unchanged, fed with mid-price session bars.

* **CFD_INDEX_DIP** - the Connors and Alvarez (2008) RSI(2) dip-buy with the frozen Candidate C
  parameters (``strategies/specs/candidate_c_etf_daily_v1.yaml``; a test asserts they agree): a
  close above its 200-day average with a 2-period Wilder RSI below 10 is bought; the stop is
  3 x ATR(10) below the close; the profit exit is the first close above the 5-day average, sold
  at the next open (the 5-day average at the signal is only the sizer's cost gate); out after
  ``max_hold_hours`` at the latest. Long only.
* **CFD_WEEKLY_MOMENTUM** - time-series momentum over one week: on the daily bar of
  ``decision_weekday`` (Sunday: the bar that closes on Monday 00:00 UTC), long when the close is
  above the close ``lookback_days`` earlier, short when below; stop ``stop_atr_multiplier`` x
  ATR(``atr_period``) from the close, target ``reward_risk`` times that distance on the other
  side, out after ``max_hold_hours``. When several instruments signal together the larger
  absolute move is taken first (``priority``).

The hourly rule (ADR-0055) decides on a completed hourly bar and is a day trade: it is closed at
a fixed time of the same UTC day.

* **CFD_HOURLY_CHANNEL** - the channel is the highest high and the lowest low of the previous
  ``channel_hours`` hourly bars. A close above it or below it is a break. ``mode: breakout``
  trades with the break (long above, short below); ``mode: fade`` trades against it. The stop is
  ``stop_atr_multiplier`` x the mean true range of the last ``atr_hours`` bars from the close,
  the target ``reward_risk`` times that distance on the other side. Decisions are taken only on
  bars that end from ``entry_first_hour_utc`` to ``entry_last_hour_utc``; the trade is closed at
  ``flat_hour_utc`` of that day at the latest. Bars are counted, not clock hours: after a
  weekend or a daily break the channel reaches back across it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from trader.config.policy import reject_floats
from trader.data.calendars import SessionCalendar
from trader.data.quote_bars import QuoteBar
from trader.domain.base import ConfigModel
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.domain.types import Direction, PosDec
from trader.features.daily import FEATURE_VERSION, HUNDRED, DailyFeatures, DailyWindows
from trader.research.cfd_engine import CfdSignal

MID_FEED = "CAPITAL_DEMO_MID"


class _CfdSpecBase(ConfigModel):
    strategy_id: str = Field(min_length=3)
    version: int = Field(ge=1)
    feature_version: str
    max_hold_hours: int = Field(ge=1, le=24 * 31)
    research_status: str

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="python"))


class CfdIndexDipSpec(_CfdSpecBase):
    family: Literal["CFD_INDEX_DIP"]
    trend_sma_days: int = Field(ge=2)
    rsi_period: int = Field(ge=1)
    rsi_entry_below: PosDec
    exit_sma_days: int = Field(ge=1)
    atr_period: int = Field(ge=1)
    stop_atr_multiplier: PosDec
    exit_reason: str = "close_above_sma"

    @model_validator(mode="after")
    def _consistent(self) -> CfdIndexDipSpec:
        if self.feature_version != FEATURE_VERSION:
            raise ValueError("rule specification and feature implementation versions differ")
        if self.rsi_entry_below >= HUNDRED:
            raise ValueError("rsi_entry_below must be below 100")
        if self.exit_sma_days >= self.trend_sma_days:
            raise ValueError("the exit average must be shorter than the trend average")
        return self

    def windows(self) -> DailyWindows:
        return DailyWindows(
            sma_periods=(self.exit_sma_days, self.trend_sma_days),
            rsi_period=self.rsi_period,
            atr_period=self.atr_period,
        )


class CfdWeeklyMomentumSpec(_CfdSpecBase):
    family: Literal["CFD_WEEKLY_MOMENTUM"]
    lookback_days: int = Field(ge=1)
    #: Weekday of the decision bar's session, Monday = 0 ... Sunday = 6.
    decision_weekday: int = Field(ge=0, le=6)
    atr_period: int = Field(ge=1)
    stop_atr_multiplier: PosDec
    reward_risk: PosDec

    @model_validator(mode="after")
    def _consistent(self) -> CfdWeeklyMomentumSpec:
        if self.feature_version != FEATURE_VERSION:
            raise ValueError("rule specification and feature implementation versions differ")
        return self

    def windows(self) -> DailyWindows:
        return DailyWindows(atr_period=self.atr_period)


#: The hourly indicators of this module: a bar-count channel and a mean true range.
HOURLY_FEATURE_VERSION = "hourly-v1"


class CfdHourlyChannelSpec(_CfdSpecBase):
    family: Literal["CFD_HOURLY_CHANNEL"]
    mode: Literal["breakout", "fade"]
    channel_hours: int = Field(ge=2, le=240)
    atr_hours: int = Field(ge=2, le=240)
    stop_atr_multiplier: PosDec
    reward_risk: PosDec
    #: A decision is taken only on an hourly bar that ends at one of these UTC hours.
    entry_first_hour_utc: int = Field(ge=0, le=23)
    entry_last_hour_utc: int = Field(ge=0, le=23)
    #: The trade is closed at this UTC hour of the decision's day at the latest.
    flat_hour_utc: int = Field(ge=1, le=23)

    @model_validator(mode="after")
    def _consistent(self) -> CfdHourlyChannelSpec:
        if self.feature_version != HOURLY_FEATURE_VERSION:
            raise ValueError("rule specification and feature implementation versions differ")
        if not self.entry_first_hour_utc <= self.entry_last_hour_utc < self.flat_hour_utc:
            raise ValueError("the entry hours must end before the flat hour of the same day")
        if self.max_hold_hours != self.flat_hour_utc - self.entry_first_hour_utc:
            raise ValueError(
                "max_hold_hours must be the span from the first entry to the flat hour"
            )
        return self


CfdSpec = CfdIndexDipSpec | CfdWeeklyMomentumSpec | CfdHourlyChannelSpec


def load_cfd_spec(path: Path) -> CfdSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    reject_floats(raw)
    family = raw.get("family") if isinstance(raw, dict) else None
    if family == "CFD_INDEX_DIP":
        return CfdIndexDipSpec.model_validate(raw)
    if family == "CFD_WEEKLY_MOMENTUM":
        return CfdWeeklyMomentumSpec.model_validate(raw)
    if family == "CFD_HOURLY_CHANNEL":
        return CfdHourlyChannelSpec.model_validate(raw)
    raise ValueError(f"{path}: not a CFD rule specification (family {family!r})")


def _down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def mid_bar(instrument_id: str, bar: QuoteBar) -> Bar:
    """The mid-price session bar the daily features read."""
    mid = {f: (getattr(bar.bid, f) + getattr(bar.ask, f)) / 2 for f in ("open", "high", "low")}
    close = bar.mid_close
    return Bar(
        instrument_id=instrument_id,
        feed=MID_FEED,
        minutes=bar.minutes,
        start=bar.start,
        end=bar.end,
        available_time=bar.end + timedelta(seconds=1),
        complete=True,
        open=mid["open"],
        high=mid["high"],
        low=mid["low"],
        close=close,
        volume=bar.bid.volume,
        trade_count=0,
        vwap=close,
    )


class _DailyRule:
    def __init__(
        self,
        spec: CfdIndexDipSpec | CfdWeeklyMomentumSpec,
        calendars: Mapping[str, SessionCalendar],
        ticks: Mapping[str, Decimal],
    ) -> None:
        self.spec = spec
        self.calendars = calendars
        self.ticks = ticks
        self._state: dict[str, DailyFeatures] = {}

    @property
    def strategy_id(self) -> str:
        return self.spec.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    def gaps(self) -> int:
        return sum(s.gaps for s in self._state.values())

    def _update(self, instrument_id: str, bar: QuoteBar) -> tuple[DailyFeatures, Bar]:
        state = self._state.get(instrument_id)
        if state is None:
            state = DailyFeatures(self.spec.windows(), self.calendars[instrument_id])
            self._state[instrument_id] = state
        session_bar = mid_bar(instrument_id, bar)
        state.update(session_bar)
        return state, session_bar

    def _signal_id(self, instrument_id: str, bar: QuoteBar) -> str:
        return f"{self.spec.strategy_id}:{instrument_id}:{bar.end.isoformat()}"


class CfdIndexDip(_DailyRule):
    spec: CfdIndexDipSpec

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None:
        state, _ = self._update(instrument_id, bar)
        if not state.ready:
            return None
        s = self.spec
        close = state.close
        if close <= state.sma(s.trend_sma_days) or state.rsi() >= s.rsi_entry_below:
            return None
        volatility = state.atr()
        tick = self.ticks[instrument_id]
        stop = _down(close - s.stop_atr_multiplier * volatility, tick)
        target = _down(state.sma(s.exit_sma_days), tick)
        if volatility <= 0 or stop <= 0 or stop >= close or target <= 0:
            return None
        return CfdSignal(
            signal_id=self._signal_id(instrument_id, bar),
            instrument_id=instrument_id,
            direction=Direction.LONG,
            decided_at=bar.end,
            reference=close,
            stop=stop,
            target=target,
            target_role="COST_GATE",
            max_hold_minutes=s.max_hold_hours * 60,
        )

    def exit_signal(self, instrument_id: str, bar: QuoteBar, direction: Direction) -> str | None:
        state = self._state.get(instrument_id)
        if direction is not Direction.LONG or state is None or state.session is None:
            return None
        try:
            mean = state.sma(self.spec.exit_sma_days)
        except LookupError:  # not warm again after a missing session
            return None
        return self.spec.exit_reason if state.close > mean else None


class CfdWeeklyMomentum(_DailyRule):
    spec: CfdWeeklyMomentumSpec

    def __init__(
        self,
        spec: CfdWeeklyMomentumSpec,
        calendars: Mapping[str, SessionCalendar],
        ticks: Mapping[str, Decimal],
    ) -> None:
        super().__init__(spec, calendars, ticks)
        self._closes: dict[str, deque[tuple[date, Decimal]]] = {}

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None:
        state, session_bar = self._update(instrument_id, bar)
        s = self.spec
        closes = self._closes.setdefault(instrument_id, deque(maxlen=s.lookback_days + 1))
        if state.count == 1:  # first bar or a reset after a missing day: start again
            closes.clear()
        day = state.session
        assert day is not None
        closes.append((day, session_bar.close))
        if day.weekday() != s.decision_weekday or len(closes) <= s.lookback_days:
            return None
        if not state.ready:
            return None
        then, now = closes[0][1], closes[-1][1]
        change = now / then - 1
        if change == 0:
            return None
        distance = s.stop_atr_multiplier * state.atr()
        if distance <= 0:
            return None
        tick = self.ticks[instrument_id]
        long = change > 0
        # the stop rounds away from the close and the target towards it
        stop = _down(now - distance, tick) if long else _up(now + distance, tick)
        reward = s.reward_risk * abs(now - stop)
        target = _down(now + reward, tick) if long else _up(now - reward, tick)
        if stop <= 0 or target <= 0:
            return None
        return CfdSignal(
            signal_id=self._signal_id(instrument_id, bar),
            instrument_id=instrument_id,
            direction=Direction.LONG if long else Direction.SHORT,
            decided_at=bar.end,
            reference=now,
            stop=stop,
            target=target,
            target_role="EXIT",
            max_hold_minutes=s.max_hold_hours * 60,
            priority=abs(change),
        )

    def exit_signal(self, instrument_id: str, bar: QuoteBar, direction: Direction) -> str | None:
        del instrument_id, bar, direction
        return None


class CfdHourlyChannel:
    """The hourly channel rule (module docstring). One state per instrument: the mid high and
    low of the last bars, their true ranges and the last close."""

    def __init__(self, spec: CfdHourlyChannelSpec, ticks: Mapping[str, Decimal]) -> None:
        self.spec = spec
        self.ticks = ticks
        self._bars: dict[str, deque[tuple[Decimal, Decimal]]] = {}
        self._ranges: dict[str, deque[Decimal]] = {}
        self._last_close: dict[str, Decimal] = {}

    @property
    def strategy_id(self) -> str:
        return self.spec.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    def gaps(self) -> int:
        return 0

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None:
        s = self.spec
        high = (bar.bid.high + bar.ask.high) / 2
        low = (bar.bid.low + bar.ask.low) / 2
        close = bar.mid_close
        earlier = self._bars.setdefault(instrument_id, deque(maxlen=s.channel_hours))
        ranges = self._ranges.setdefault(instrument_id, deque(maxlen=s.atr_hours))
        previous = self._last_close.get(instrument_id)
        ranges.append(
            high - low
            if previous is None
            else max(high - low, abs(high - previous), abs(low - previous))
        )
        signal = self._decide(instrument_id, bar, close, earlier, ranges)
        earlier.append((high, low))
        self._last_close[instrument_id] = close
        return signal

    def _decide(
        self,
        instrument_id: str,
        bar: QuoteBar,
        close: Decimal,
        earlier: deque[tuple[Decimal, Decimal]],
        ranges: deque[Decimal],
    ) -> CfdSignal | None:
        s = self.spec
        if len(earlier) < s.channel_hours or len(ranges) < s.atr_hours:
            return None
        end = bar.end
        on_the_hour = bar.minutes == 60 and end.minute == 0 and end.second == 0
        if not on_the_hour or not s.entry_first_hour_utc <= end.hour <= s.entry_last_hour_utc:
            return None
        top = max(h for h, _ in earlier)
        bottom = min(low for _, low in earlier)
        if bottom <= close <= top:
            return None
        volatility = sum(ranges, start=Decimal(0)) / len(ranges)
        distance = s.stop_atr_multiplier * volatility
        if distance <= 0:
            return None
        broke_up = close > top
        long = broke_up if s.mode == "breakout" else not broke_up
        tick = self.ticks[instrument_id]
        # the stop rounds away from the close and the target towards it
        stop = _down(close - distance, tick) if long else _up(close + distance, tick)
        reward = s.reward_risk * abs(close - stop)
        target = _down(close + reward, tick) if long else _up(close - reward, tick)
        if stop <= 0 or target <= 0:
            return None
        flat = datetime.combine(end.date(), time(s.flat_hour_utc), tzinfo=end.tzinfo)
        beyond = (close - top) if broke_up else (bottom - close)
        return CfdSignal(
            signal_id=f"{s.strategy_id}:{instrument_id}:{end.isoformat()}",
            instrument_id=instrument_id,
            direction=Direction.LONG if long else Direction.SHORT,
            decided_at=end,
            reference=close,
            stop=stop,
            target=target,
            target_role="EXIT",
            max_hold_minutes=int((flat - end).total_seconds() // 60),
            priority=beyond / volatility,
            deadline=flat,
        )

    def exit_signal(self, instrument_id: str, bar: QuoteBar, direction: Direction) -> str | None:
        del instrument_id, bar, direction
        return None


def build_cfd_rule(
    spec: CfdSpec,
    calendars: Mapping[str, SessionCalendar],
    ticks: Mapping[str, Decimal],
) -> CfdIndexDip | CfdWeeklyMomentum | CfdHourlyChannel:
    if isinstance(spec, CfdIndexDipSpec):
        return CfdIndexDip(spec, calendars, ticks)
    if isinstance(spec, CfdHourlyChannelSpec):
        return CfdHourlyChannel(spec, ticks)
    return CfdWeeklyMomentum(spec, calendars, ticks)


__all__ = [
    "HOURLY_FEATURE_VERSION",
    "CfdHourlyChannel",
    "CfdHourlyChannelSpec",
    "CfdIndexDip",
    "CfdIndexDipSpec",
    "CfdSpec",
    "CfdWeeklyMomentum",
    "CfdWeeklyMomentumSpec",
    "build_cfd_rule",
    "load_cfd_spec",
    "mid_bar",
]
