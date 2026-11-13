"""Causal features for the trend-continuation baseline (feature version ``momentum-v1``).

Every function takes only *completed* bars available at the decision time; none looks ahead.
"""

from __future__ import annotations

from decimal import Decimal

from trader.domain.market import Bar
from trader.domain.types import ZERO

FEATURE_VERSION = "momentum-v1"


def session_vwap(bars: list[Bar]) -> Decimal:
    volume = sum((b.volume for b in bars), start=ZERO)
    if volume <= 0:
        raise ValueError("no volume")
    return sum((b.vwap * b.volume for b in bars), start=ZERO) / volume


def true_ranges(bars: list[Bar]) -> list[Decimal]:
    out: list[Decimal] = []
    for i, bar in enumerate(bars):
        if i == 0:
            out.append(bar.high - bar.low)
        else:
            prev = bars[i - 1].close
            out.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    return out


def atr(bars: list[Bar], period: int) -> Decimal:
    if len(bars) < period + 1:
        raise ValueError("not enough bars for ATR warm-up")
    ranges = true_ranges(bars)[-period:]
    return sum(ranges, start=ZERO) / period


def prior_high(bars: list[Bar], lookback: int) -> Decimal:
    """Highest high of the ``lookback`` completed bars *before* the last one."""
    if len(bars) < lookback + 1:
        raise ValueError("not enough bars for the breakout range")
    return max(b.high for b in bars[-lookback - 1 : -1])


def trailing_return(bars: list[Bar], n: int) -> Decimal:
    if len(bars) < n + 1:
        raise ValueError("not enough bars for trailing return")
    return bars[-1].close / bars[-1 - n].close - 1
