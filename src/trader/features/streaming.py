"""Streaming causal features (feature version ``streaming-v1``).

One :class:`RollingBarFeatures` instance per instrument consumes *completed* bars in time
order and updates exact running sums in O(1). Research replays and a live process feed the same
object with the same bars, so their features match by construction; ``features/parity.py``
checks this against a direct batch recomputation.

Exactness: running sums hold products of exact decimals (``(H+L+C) * V``, true ranges,
absolute close changes) whose digits stay far below the 28-digit context, so additions and
removals never round and long runs cannot drift. Division happens only when a value is read.

Data gaps: a bar that does not start where the previous one ended resets the state, so every
window is contiguous and must warm up again (fail closed; no interpolation). With
``window_bars=None`` the sums are cumulative since the last reset, which gives a *session*
VWAP when the caller resets at each session open.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from trader.domain.market import Bar
from trader.domain.types import ZERO

FEATURE_VERSION = "streaming-v1"


class _RollingSum:
    def __init__(self, length: int | None) -> None:
        self.length = length
        self.values: deque[Decimal] = deque()
        self.total = ZERO

    def push(self, value: Decimal) -> None:
        self.values.append(value)
        self.total += value
        if self.length is not None and len(self.values) > self.length:
            self.total -= self.values.popleft()

    @property
    def full(self) -> bool:
        return self.length is None or len(self.values) == self.length

    def clear(self) -> None:
        self.values.clear()
        self.total = ZERO


@dataclass(frozen=True, slots=True)
class FeatureWindows:
    minutes: int
    reference_window_bars: int | None  # None = cumulative since reset (session reference)
    use_source_vwap: bool
    atr_period: int
    long_atr_period: int
    efficiency_window: int
    breakout_lookback: int
    context_minutes: int
    context_lookback: int

    @property
    def warmup_bars(self) -> int:
        per = self.context_minutes // self.minutes
        needed = [
            self.atr_period + 1,
            self.long_atr_period + 1,
            self.efficiency_window + 1,
            self.breakout_lookback + 1,
            per * (self.context_lookback + 1) + per - 1,
        ]
        if self.reference_window_bars is not None:
            needed.append(self.reference_window_bars)
        return max(needed)


class RollingBarFeatures:
    def __init__(self, windows: FeatureWindows) -> None:
        if windows.context_minutes % windows.minutes:
            raise ValueError("context interval must be a multiple of the bar interval")
        self.w = windows
        self.per_context = windows.context_minutes // windows.minutes
        keep = max(windows.breakout_lookback + 1, self.per_context, 2)
        self.recent: deque[Bar] = deque(maxlen=keep)
        self.count = 0
        self.gaps = 0
        self._ref_num = _RollingSum(windows.reference_window_bars)
        self._ref_den = _RollingSum(windows.reference_window_bars)
        self._tr = _RollingSum(windows.atr_period)
        self._tr_long = _RollingSum(windows.long_atr_period)
        self._abs_change = _RollingSum(windows.efficiency_window)
        self._closes: deque[Decimal] = deque(maxlen=windows.efficiency_window + 1)
        self._context: deque[Decimal] = deque(maxlen=windows.context_lookback + 1)

    # ------------------------------------------------------------------ updates
    def reset(self) -> None:
        self.recent.clear()
        self.count = 0
        for s in (self._ref_num, self._ref_den, self._tr, self._tr_long, self._abs_change):
            s.clear()
        self._closes.clear()
        self._context.clear()

    def update(self, bar: Bar) -> None:
        if not bar.complete:
            raise ValueError("only completed bars may update features")
        if self.recent and bar.start != self.recent[-1].end:
            self.gaps += 1
            self.reset()
        prev = self.recent[-1] if self.recent else None
        if prev is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
            self._abs_change.push(abs(bar.close - prev.close))
        if self.w.use_source_vwap:
            self._ref_num.push(bar.vwap * bar.volume * 3)
        else:
            self._ref_num.push((bar.high + bar.low + bar.close) * bar.volume)
        self._ref_den.push(bar.volume * 3)
        self._tr.push(tr)
        self._tr_long.push(tr)
        self._closes.append(bar.close)
        self.recent.append(bar)
        self.count += 1
        self._maybe_close_context(bar)

    def _maybe_close_context(self, bar: Bar) -> None:
        epoch_minutes = int(bar.end.timestamp()) // 60
        if epoch_minutes % self.w.context_minutes:
            return
        if self.count < self.per_context:
            return
        group = list(self.recent)[-self.per_context :]
        aligned_start = int(group[0].start.timestamp()) // 60
        if aligned_start % self.w.context_minutes == 0:
            self._context.append(bar.close)

    # ------------------------------------------------------------------ reads
    @property
    def ready(self) -> bool:
        return self.count >= self.w.warmup_bars and len(self._context) == self._context.maxlen

    @property
    def last(self) -> Bar:
        return self.recent[-1]

    def reference_price(self) -> Decimal:
        if self._ref_den.total > 0:
            return self._ref_num.total / self._ref_den.total
        return self.last.close

    def atr(self) -> Decimal:
        return self._tr.total / self.w.atr_period

    def long_atr(self) -> Decimal:
        return self._tr_long.total / self.w.long_atr_period

    def efficiency_ratio(self) -> Decimal:
        """|net change| / path length over the efficiency window (0 = choppy, 1 = straight)."""
        path = self._abs_change.total
        if path == 0:
            return ZERO
        return abs(self._closes[-1] - self._closes[0]) / path

    def prior_high(self) -> Decimal:
        """Highest high of the ``breakout_lookback`` bars before the last one."""
        bars = list(self.recent)
        return max(b.high for b in bars[-self.w.breakout_lookback - 1 : -1])

    def context_return(self) -> Decimal:
        """Return over ``context_lookback`` completed context bars."""
        return self._context[-1] / self._context[0] - 1

    def snapshot(self) -> dict[str, Decimal]:
        return {
            "close": self.last.close,
            "reference": self.reference_price(),
            "atr": self.atr(),
            "long_atr": self.long_atr(),
            "efficiency_ratio": self.efficiency_ratio(),
            "prior_high": self.prior_high(),
            "context_return": self.context_return(),
        }

    def rescale(self, divisor: Decimal) -> None:
        """A split invalidates price-level state; rebuild it from new bars (fail closed)."""
        del divisor
        self.reset()
