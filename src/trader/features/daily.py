"""Streaming daily-bar features (feature version ``daily-v1``).

One :class:`DailyFeatures` instance per instrument consumes completed *session* bars in session
order, on one of two calendars (``data/calendars.py``):

* **XNYS** - one bar per regular session, ``start`` = the session open, ``end`` = its close
  (16:00 New York or the early close);
* **UTC days** (``Utc247Calendar``, crypto) - one bar per UTC day, ``start`` = 00:00 UTC,
  ``end`` = the next 00:00 UTC (1440 minutes); every date is a session.

Research replays and a live process feed the same object with the same bars, so their values
match by construction (docs/ml_research_guide.md §3). The XNYS behaviour does not depend on the
UTC-day support: the calendar is only ever asked for session dates and the next session.

Indicators (all exact ``Decimal``; no binary float is ever involved):

* **SMA(n)** of closes from an exact running sum over the last ``n`` closes. Closes carry at most
  12 decimal places (the storage quantum, ADR-0022), so the sums never round; only the read-out
  ``sum / n`` is quantised (12 dp, half-even).
* **Wilder RSI(n)** (J. Welles Wilder, *New Concepts in Technical Trading Systems*, 1978): the
  average gain and loss of close-to-close changes are seeded with the simple mean of the first
  ``n`` changes and then smoothed ``avg = (avg * (n - 1) + x) / n``; every average is quantised to
  12 dp (half-even) after each step, so the recursion is deterministic and reproducible by any
  implementation that follows the same rule. ``RSI = 100 * gain / (gain + loss)`` (algebraically
  Wilder's ``100 - 100 / (1 + RS)``), quantised to 12 dp. With no movement at all over the
  window (both averages zero) the RSI is defined as 50 - neutral - so a stale, flat series can
  never look oversold (fail closed). The first value needs ``n + 1`` closes, as in TA-Lib.
* **Wilder ATR(n)** (Wilder, 1978): true range ``max(H - L, |H - C_prev|, |L - C_prev|)``,
  seeded with the mean of the first ``n`` true ranges (each with a previous close, so ``n + 1``
  bars) and smoothed like the RSI averages, quantised to 12 dp.
* **Month end** from the calendar: a session is a month-end session when the next session
  falls in a later month (so 30 April is not an XNYS month end when it is a Saturday; the last
  session of April is). On the UTC-day calendar the month's last calendar day is its month end
  (the bar ``[last day 00:00, first of the next month 00:00)`` UTC). ``month_end_sma`` is the
  SMA of the last ``k`` month-end closes including the current one (Faber, *A Quantitative
  Approach to Tactical Asset Allocation*, 2007, uses month-end prices).

Missing data (the documented choice). Weekends, holidays and early closes are not gaps: they
are not sessions or are complete sessions. A bar whose session is not the calendar's next session
after the previous bar's means **a whole session is missing** (on the UTC-day calendar: a whole
UTC day): the state resets and must warm up again (fail closed; nothing is interpolated).
``gaps`` counts these resets. Bars that do not advance the session (two bars for one session, or
out of order) raise ``ValueError``, because the input is not daily data.

Splits. :meth:`DailyFeatures.rescale` applies a split point-in-time (prices divided by the split
ratio, shares after per share before) to every stored price and price-difference state, each
rescaled value quantised with :func:`rescale_price` (12 dp, half-even), and the sums rebuilt from
the stored values. Unlike ``streaming-v1`` (5-minute bars, reset at every session open anyway),
the daily state is *not* reset at a split: a reset would silence a 200-session rule for most of a
year. The rescaled SMAs equal a batch recomputation over bars adjusted with the same
``rescale_price`` exactly; the Wilder averages were rounded on the old share basis, so they agree
with that batch to within the 12-dp quantum (the smoothing forgets the difference geometrically).
``tests/test_daily_features.py`` checks both.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal

from trader.data.calendars import ExchangeCalendar, SessionCalendar
from trader.domain.market import Bar
from trader.domain.types import ZERO

FEATURE_VERSION = "daily-v1"

#: Quantum of every derived value (the storage quantum of ADR-0022: 12 decimal places).
QUANTUM = Decimal("0.000000000001")
#: Explicit arithmetic context, so results never depend on the caller's thread context.
_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
HUNDRED = Decimal(100)
NEUTRAL_RSI = Decimal(50)
#: Upper bound on XNYS sessions in one calendar month (used for warm-up estimates only).
MAX_SESSIONS_PER_MONTH = 23
#: Upper bound on UTC-day sessions in one calendar month (warm-up estimates on 24/7 markets).
MAX_UTC_DAYS_PER_MONTH = 31


def max_sessions_per_month(calendar: SessionCalendar) -> int:
    """Most sessions a calendar month can hold: 23 XNYS sessions, 31 UTC days."""
    return (
        MAX_SESSIONS_PER_MONTH if isinstance(calendar, ExchangeCalendar) else MAX_UTC_DAYS_PER_MONTH
    )


def quantize(value: Decimal) -> Decimal:
    """Round to 12 decimal places, half-even, in the module's explicit context."""
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN, context=_CTX)


def divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """``numerator / denominator`` quantised to 12 dp (deterministic in any thread context)."""
    return quantize(_CTX.divide(numerator, denominator))


def rescale_price(value: Decimal, ratio: Decimal) -> Decimal:
    """A price (or price difference) on the post-split share basis: ``value / ratio``, 12 dp.

    ``ratio`` is shares after per share before (2-for-1 -> 2). A batch reference that adjusts
    historical bars for a split must use this function to reproduce the streaming values.
    """
    if ratio <= 0:
        raise ValueError("a split ratio must be positive")
    return divide(value, ratio)


def _sum(values: Iterable[Decimal]) -> Decimal:
    total = ZERO
    for v in values:
        total = _CTX.add(total, v)
    return total


def next_session_date(calendar: SessionCalendar, day: date) -> date:
    """The calendar's next session date after ``day``."""
    return calendar.sessions_after(day).session_date


def is_month_end(calendar: SessionCalendar, day: date) -> bool:
    """True when ``day``'s next session is in a later month (the month's last session)."""
    following = next_session_date(calendar, day)
    return (following.year, following.month) != (day.year, day.month)


class _ExactWindow:
    """Exact running sum over the last ``length`` values (each with at most 12 dp)."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.values: deque[Decimal] = deque()
        self.total = ZERO

    def push(self, value: Decimal) -> None:
        self.values.append(value)
        self.total = _CTX.add(self.total, value)
        if len(self.values) > self.length:
            self.total = _CTX.subtract(self.total, self.values.popleft())

    @property
    def full(self) -> bool:
        return len(self.values) == self.length

    def mean(self) -> Decimal:
        if not self.full:
            raise LookupError(f"window of {self.length} is not warm ({len(self.values)} values)")
        return divide(self.total, Decimal(self.length))

    def rescale(self, ratio: Decimal) -> None:
        self.values = deque(rescale_price(v, ratio) for v in self.values)
        self.total = _sum(self.values)

    def clear(self) -> None:
        self.values.clear()
        self.total = ZERO


class _WilderAverage:
    """Wilder smoothing: seeded by the mean of the first ``period`` inputs, quantised to 12 dp."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.seed: list[Decimal] = []
        self.value: Decimal | None = None

    def push(self, x: Decimal) -> None:
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.period:
                self.value = divide(_sum(self.seed), Decimal(self.period))
                self.seed = []
            return
        n = Decimal(self.period)
        self.value = divide(_CTX.add(_CTX.multiply(self.value, n - 1), x), n)

    @property
    def ready(self) -> bool:
        return self.value is not None

    def rescale(self, ratio: Decimal) -> None:
        self.seed = [rescale_price(v, ratio) for v in self.seed]
        if self.value is not None:
            self.value = rescale_price(self.value, ratio)

    def clear(self) -> None:
        self.seed = []
        self.value = None


@dataclass(frozen=True, slots=True)
class DailyWindows:
    """Which daily indicators an instrument state maintains (only what a rule needs)."""

    sma_periods: tuple[int, ...] = ()
    rsi_period: int | None = None
    atr_period: int | None = None
    month_end_sma_months: int | None = None

    def __post_init__(self) -> None:
        if any(n < 1 for n in self.sma_periods) or len(set(self.sma_periods)) != len(
            self.sma_periods
        ):
            raise ValueError("SMA periods must be distinct positive integers")
        for name, value in (
            ("rsi_period", self.rsi_period),
            ("atr_period", self.atr_period),
            ("month_end_sma_months", self.month_end_sma_months),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")

    @property
    def warmup_sessions(self) -> int:
        """XNYS sessions of contiguous data that certainly make every configured value ready.

        The month-end average needs ``k`` month ends, which takes at most
        ``MAX_SESSIONS_PER_MONTH * k`` sessions; a study's ``warmup_sessions`` should be at least
        this value so its first window starts warm. :meth:`warmup_sessions_on` gives the bound
        for another calendar.
        """
        return self._warmup(MAX_SESSIONS_PER_MONTH)

    def warmup_sessions_on(self, calendar: SessionCalendar) -> int:
        """:attr:`warmup_sessions` on ``calendar``: on UTC days the month-end average needs at
        most ``31 * k`` sessions (``k`` month ends), the other windows the same bar counts."""
        return self._warmup(max_sessions_per_month(calendar))

    def _warmup(self, per_month: int) -> int:
        needed = [1, *self.sma_periods]
        if self.rsi_period is not None:
            needed.append(self.rsi_period + 1)
        if self.atr_period is not None:
            needed.append(self.atr_period + 1)
        if self.month_end_sma_months is not None:
            needed.append(per_month * self.month_end_sma_months)
        return max(needed)


class DailyFeatures:
    """Causal daily indicators for one instrument (``daily-v1``)."""

    def __init__(self, windows: DailyWindows, calendar: SessionCalendar) -> None:
        self.w = windows
        self.calendar = calendar
        self.gaps = 0
        self.count = 0
        self._day: date | None = None
        self._close: Decimal | None = None
        self._month_end = False
        self._sma = {n: _ExactWindow(n) for n in windows.sma_periods}
        self._gain = _WilderAverage(windows.rsi_period) if windows.rsi_period else None
        self._loss = _WilderAverage(windows.rsi_period) if windows.rsi_period else None
        self._atr = _WilderAverage(windows.atr_period) if windows.atr_period else None
        months = windows.month_end_sma_months
        self._month_ends = _ExactWindow(months) if months else None

    # ------------------------------------------------------------------ updates
    def reset(self) -> None:
        self.count = 0
        self._day = None
        self._close = None
        self._month_end = False
        for window in self._sma.values():
            window.clear()
        for avg in (self._gain, self._loss, self._atr):
            if avg is not None:
                avg.clear()
        if self._month_ends is not None:
            self._month_ends.clear()

    def update(self, bar: Bar) -> None:
        """Add one completed session bar; a missing session resets the state first."""
        if not bar.complete:
            raise ValueError("only completed bars may update features")
        day = self.calendar.session_date_of(bar.start)
        if self._day is not None:
            if day <= self._day:
                raise ValueError(
                    f"{bar.instrument_id}: daily bars must advance one session at a time "
                    f"({day.isoformat()} after {self._day.isoformat()})"
                )
            if day != next_session_date(self.calendar, self._day):
                self.gaps += 1
                self.reset()
        prev = self._close
        if prev is not None:
            change = _CTX.subtract(bar.close, prev)
            if self._gain is not None and self._loss is not None:
                self._gain.push(change if change > 0 else ZERO)
                self._loss.push(-change if change < 0 else ZERO)
            if self._atr is not None:
                self._atr.push(
                    max(
                        _CTX.subtract(bar.high, bar.low),
                        abs(_CTX.subtract(bar.high, prev)),
                        abs(_CTX.subtract(bar.low, prev)),
                    )
                )
        for window in self._sma.values():
            window.push(bar.close)
        self._month_end = is_month_end(self.calendar, day)
        if self._month_end and self._month_ends is not None:
            self._month_ends.push(bar.close)
        self._day = day
        self._close = bar.close
        self.count += 1

    def rescale(self, ratio: Decimal) -> None:
        """Apply a split point-in-time (``ratio`` = shares after per share before)."""
        if self._close is not None:
            self._close = rescale_price(self._close, ratio)
        for window in self._sma.values():
            window.rescale(ratio)
        for avg in (self._gain, self._loss, self._atr):
            if avg is not None:
                avg.rescale(ratio)
        if self._month_ends is not None:
            self._month_ends.rescale(ratio)

    # ------------------------------------------------------------------ reads
    @property
    def session(self) -> date | None:
        """Session date of the last bar."""
        return self._day

    @property
    def close(self) -> Decimal:
        if self._close is None:
            raise LookupError("no bar since the last reset")
        return self._close

    @property
    def is_month_end(self) -> bool:
        """The last bar's session is the last session of its month."""
        return self._month_end

    @property
    def ready(self) -> bool:
        """Every configured indicator has a value."""
        if self._close is None:
            return False
        if not all(w.full for w in self._sma.values()):
            return False
        for avg in (self._gain, self._loss, self._atr):
            if avg is not None and not avg.ready:
                return False
        return self._month_ends is None or self._month_ends.full

    def sma(self, period: int) -> Decimal:
        window = self._sma.get(period)
        if window is None:
            raise KeyError(f"SMA({period}) is not configured")
        return window.mean()

    def rsi(self) -> Decimal:
        if self._gain is None or self._loss is None:
            raise KeyError("RSI is not configured")
        gain, loss = self._gain.value, self._loss.value
        if gain is None or loss is None:
            raise LookupError("RSI is not warm")
        total = _CTX.add(gain, loss)
        if total == 0:
            return NEUTRAL_RSI
        return divide(_CTX.multiply(HUNDRED, gain), total)

    def atr(self) -> Decimal:
        if self._atr is None:
            raise KeyError("ATR is not configured")
        if self._atr.value is None:
            raise LookupError("ATR is not warm")
        return self._atr.value

    def month_end_sma(self) -> Decimal:
        """SMA of the last ``k`` month-end closes (including the last bar when it is one)."""
        if self._month_ends is None:
            raise KeyError("the month-end SMA is not configured")
        return self._month_ends.mean()

    def snapshot(self) -> dict[str, Decimal]:
        """Every configured value at the last bar, for ``snapshot_hash`` (requires ``ready``)."""
        if not self.ready:
            raise LookupError("features are not warm")
        out: dict[str, Decimal] = {"close": self.close}
        for n in sorted(self._sma):
            out[f"sma_{n}"] = self.sma(n)
        if self.w.rsi_period is not None:
            out[f"rsi_{self.w.rsi_period}"] = self.rsi()
        if self.w.atr_period is not None:
            out[f"atr_{self.w.atr_period}"] = self.atr()
        if self.w.month_end_sma_months is not None:
            out[f"month_end_sma_{self.w.month_end_sma_months}"] = self.month_end_sma()
        out["month_end"] = Decimal(1) if self._month_end else ZERO
        return out
