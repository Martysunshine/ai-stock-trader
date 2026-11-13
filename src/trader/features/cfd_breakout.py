"""Features of an hourly channel-break candidate, for the veto filter of trial T-0026.

Version ``cfd-breakout-v1``. Nine plain numbers, computed for one candidate of the hourly
channel rule (``strategies/cfd_rules.py``, ``CFD_HOURLY_CHANNEL``) from the broker's hourly bid
and ask bars **that ended at or before the candidate's decision time**, and from the candidate's
own side and flat time. The decision bar is the bar whose end is the decision time; nothing that
starts after it is ever read (:meth:`BreakoutFeatureBook.features` cuts the history there, and
a test moves every later bar to prove it). Prices are mid prices, as the rule reads them; bars
are counted, not clock hours, as the rule counts them. Floats are fine here: these values only
feed a model's score, never a price, size or amount of money.

========================  ==================================================================
``break_atr``             How far the decision bar's mid close lies beyond the channel - the
                          highest mid high and lowest mid low of the ``channel_hours`` bars
                          before it - in units of the mean true range of the last
                          ``atr_hours`` bars (decision bar included). Positive beyond the
                          channel, negative inside it. For the rule's own signals this is its
                          ``priority``.
``return_24h``            Log return of the mid close over the last 24 bars, signed by the
                          candidate's side (positive when the market moved the trade's way).
``return_72h``            The same over the last 72 bars.
``vol_ratio_24_168``      Standard deviation of the last 24 hourly log returns of the mid
                          close divided by that of the last 168 (one week); 1 when the longer
                          one is zero.
``spread_bps``            The broker's quoted spread at the decision bar, in basis points of
                          its mid close: the wider of the quotes at its open and at its close
                          (the two simultaneous quotes, as ``research/cfd_engine.apply_costs``
                          reads them). Raw quotes: no floor, no cost view.
``direction``             +1 for a long, -1 for a short.
``hour_sin``, ``hour_cos``  The decision time's UTC hour of day ``h`` as ``sin(2 pi h / 24)``
                          and ``cos(2 pi h / 24)``.
``hours_to_flat``         Hours from the decision time to the time by which the trade is
                          closed (the rule's flat time, 21:00 UTC on the crypto sleeve).
========================  ==================================================================

A candidate with fewer than 168 earlier bars (or fewer than the channel and range windows) is
refused with :class:`InsufficientHistory` rather than scored on a shorter window: every value is
then finite and means the same thing in every row.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

import numpy as np
from numpy.typing import NDArray

from trader.data.quote_bars import QuoteBar
from trader.domain.types import Direction

FEATURE_VERSION = "cfd-breakout-v1"
FEATURE_NAMES: tuple[str, ...] = (
    "break_atr",
    "return_24h",
    "return_72h",
    "vol_ratio_24_168",
    "spread_bps",
    "direction",
    "hour_sin",
    "hour_cos",
    "hours_to_flat",
)
SHORT_RETURN_BARS = 24
LONG_RETURN_BARS = 72
SHORT_VOL_BARS = 24
LONG_VOL_BARS = 168

#: One line per feature, bound into a trial's registration.
FEATURE_DEFINITIONS: dict[str, str] = {
    "break_atr": "(mid close beyond the channel of the previous channel_hours bars) / mean true "
    "range of the last atr_hours bars, decision bar included; negative inside the channel",
    "return_24h": "side x ln(mid close / mid close 24 bars earlier)",
    "return_72h": "side x ln(mid close / mid close 72 bars earlier)",
    "vol_ratio_24_168": "sd of the last 24 hourly log returns / sd of the last 168 (1 if zero)",
    "spread_bps": "max(quoted spread at the decision bar's open, at its close) / mid close x 1e4",
    "direction": "+1 long, -1 short",
    "hour_sin": "sin(2 pi h / 24), h = UTC hour of the decision time",
    "hour_cos": "cos(2 pi h / 24), h = UTC hour of the decision time",
    "hours_to_flat": "hours from the decision time to the trade's flat time",
}

FloatArray = NDArray[np.float64]


class InsufficientHistory(ValueError):
    """Too few bars before the decision bar for every feature's window."""


@dataclass(frozen=True, slots=True)
class _Series:
    ends: tuple[datetime, ...]
    high: FloatArray
    low: FloatArray
    close: FloatArray
    spread_bps: FloatArray


def _series(bars: Sequence[QuoteBar]) -> _Series:
    ordered = sorted(bars, key=lambda b: b.start)
    for a, b in pairwise(ordered):
        if b.start < a.end:
            raise ValueError(f"bars overlap at {b.start.isoformat()}")

    def mid(field: str) -> FloatArray:
        return np.asarray(
            [float((getattr(b.bid, field) + getattr(b.ask, field)) / 2) for b in ordered],
            dtype=np.float64,
        )

    close = mid("close")
    spread = np.asarray(
        [
            float(max(b.ask.open - b.bid.open, b.ask.close - b.bid.close) / b.mid_close) * 1e4
            for b in ordered
        ],
        dtype=np.float64,
    )
    return _Series(
        ends=tuple(b.end for b in ordered),
        high=mid("high"),
        low=mid("low"),
        close=close,
        spread_bps=spread,
    )


class BreakoutFeatureBook:
    """Every instrument's hourly bars, read only up to a decision time.

    ``channel_hours`` and ``atr_hours`` are the rule's own windows (24 and 24 for T-0023).
    """

    version = FEATURE_VERSION
    names = FEATURE_NAMES

    def __init__(
        self,
        bars: Mapping[str, Sequence[QuoteBar]],
        *,
        channel_hours: int,
        atr_hours: int,
    ) -> None:
        if channel_hours < 2 or atr_hours < 2:
            raise ValueError("the channel and range windows need at least two bars")
        self.channel_hours = channel_hours
        self.atr_hours = atr_hours
        self._series = {iid: _series(b) for iid, b in bars.items()}

    @property
    def lookback(self) -> int:
        """Bars needed before the decision bar."""
        return max(self.channel_hours, self.atr_hours, LONG_RETURN_BARS, LONG_VOL_BARS)

    def decision_index(self, instrument_id: str, decided_at: datetime) -> int:
        """The index of the bar that ends at ``decided_at``; bars after it are never read."""
        series = self._series[instrument_id]
        index = bisect_right(series.ends, decided_at) - 1
        if index < 0 or series.ends[index] != decided_at:
            raise ValueError(
                f"{instrument_id}: no bar ends at the decision time {decided_at.isoformat()}"
            )
        return index

    def features(
        self,
        instrument_id: str,
        decided_at: datetime,
        direction: Direction,
        flat_at: datetime,
    ) -> dict[str, float]:
        i = self.decision_index(instrument_id, decided_at)
        if i < self.lookback:
            raise InsufficientHistory(
                f"{instrument_id} at {decided_at.isoformat()}: {i} earlier bars, "
                f"{self.lookback} needed"
            )
        s = self._series[instrument_id]
        # the window read: the decision bar (last row, j) and the lookback bars before it
        lo = i - self.lookback
        high, low, close = s.high[lo : i + 1], s.low[lo : i + 1], s.close[lo : i + 1]
        j = self.lookback
        top = float(high[j - self.channel_hours : j].max())
        bottom = float(low[j - self.channel_hours : j].min())
        c = float(close[j])
        beyond = c - top if c > top else (bottom - c if c < bottom else -min(c - bottom, top - c))
        k = np.arange(j - self.atr_hours + 1, j + 1)
        previous = close[k - 1]
        true_range = np.maximum.reduce(
            [high[k] - low[k], np.abs(high[k] - previous), np.abs(low[k] - previous)]
        )
        mean_range = float(true_range.mean())
        if mean_range <= 0:
            raise InsufficientHistory(f"{instrument_id}: zero mean true range")
        side = 1.0 if direction is Direction.LONG else -1.0
        log_close = np.log(close)
        returns = np.diff(log_close[j - LONG_VOL_BARS :])
        long_sd = float(returns.std())
        short_sd = float(returns[-SHORT_VOL_BARS:].std())
        hour = decided_at.hour + decided_at.minute / 60
        out = {
            "break_atr": beyond / mean_range,
            "return_24h": side * float(log_close[j] - log_close[j - SHORT_RETURN_BARS]),
            "return_72h": side * float(log_close[j] - log_close[j - LONG_RETURN_BARS]),
            "vol_ratio_24_168": short_sd / long_sd if long_sd > 0 else 1.0,
            "spread_bps": float(s.spread_bps[i]),
            "direction": side,
            "hour_sin": math.sin(2 * math.pi * hour / 24),
            "hour_cos": math.cos(2 * math.pi * hour / 24),
            "hours_to_flat": (flat_at - decided_at).total_seconds() / 3600,
        }
        if not all(math.isfinite(v) for v in out.values()):
            raise InsufficientHistory(f"{instrument_id} at {decided_at.isoformat()}: not finite")
        return out


__all__ = [
    "FEATURE_DEFINITIONS",
    "FEATURE_NAMES",
    "FEATURE_VERSION",
    "BreakoutFeatureBook",
    "InsufficientHistory",
]
