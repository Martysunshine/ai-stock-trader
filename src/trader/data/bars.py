"""Causal bar construction (instructions.md §5; architecture.md §6 step 3).

Bars are built from trades of one feed. A bar ``[start, end)`` is finalised only when the clock
reaches ``end + finalization_delay`` (its *availability time*); trades that arrive after that
are late corrections and never rewrite an already-used bar. Intervals without trades produce no
bar; they are reported as gaps rather than fabricated (distinguish no-trade from missing data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.domain.market import Bar, Trade
from trader.domain.types import ZERO, require_utc


def floor_time(ts: datetime, minutes: int) -> datetime:
    ts = require_utc(ts)
    epoch_minutes = int(ts.timestamp() // 60)
    floored = epoch_minutes - (epoch_minutes % minutes)
    return datetime.fromtimestamp(floored * 60, tz=UTC)


@dataclass
class _Accumulator:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO
    notional: Decimal = ZERO
    count: int = 0


@dataclass
class BarBuilder:
    instrument_id: str
    feed: str
    minutes: int
    finalization_delay: timedelta = timedelta(seconds=1)
    _current: dict[datetime, _Accumulator] = field(default_factory=dict)
    late_trades: int = 0
    _last_final_end: datetime | None = None

    def add(self, trade: Trade) -> None:
        if trade.instrument_id != self.instrument_id or trade.feed != self.feed:
            raise ValueError("trade belongs to another instrument or feed")
        start = floor_time(trade.event_time, self.minutes)
        if self._last_final_end is not None and start < self._last_final_end:
            self.late_trades += 1  # arrived after its bar was finalised: never rewrite history
            return
        acc = self._current.get(start)
        if acc is None:
            acc = _Accumulator(start, trade.price, trade.price, trade.price, trade.price)
            self._current[start] = acc
        acc.high = max(acc.high, trade.price)
        acc.low = min(acc.low, trade.price)
        acc.close = trade.price
        acc.volume += trade.size
        acc.notional += trade.size * trade.price
        acc.count += 1

    def finalize_ready(self, now: datetime) -> list[Bar]:
        now = require_utc(now)
        ready: list[Bar] = []
        for start in sorted(self._current):
            end = start + timedelta(minutes=self.minutes)
            available = end + self.finalization_delay
            if available > now:
                break
            acc = self._current.pop(start)
            ready.append(
                Bar(
                    instrument_id=self.instrument_id,
                    feed=self.feed,
                    minutes=self.minutes,
                    start=start,
                    end=end,
                    available_time=available,
                    complete=True,
                    open=acc.open,
                    high=acc.high,
                    low=acc.low,
                    close=acc.close,
                    volume=acc.volume,
                    trade_count=acc.count,
                    vwap=acc.notional / acc.volume,
                )
            )
            self._last_final_end = end
        return ready


def aggregate(bars: list[Bar], minutes: int) -> Bar:
    """Combine consecutive completed bars into one higher-timeframe bar."""
    if not bars:
        raise ValueError("no bars to aggregate")
    volume = sum((b.volume for b in bars), start=ZERO)
    notional = sum((b.vwap * b.volume for b in bars), start=ZERO)
    return Bar(
        instrument_id=bars[0].instrument_id,
        feed=bars[0].feed,
        minutes=minutes,
        start=bars[0].start,
        end=bars[-1].end,
        available_time=max(b.available_time for b in bars),
        complete=all(b.complete for b in bars),
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=volume,
        trade_count=sum(b.trade_count for b in bars),
        vwap=notional / volume if volume > 0 else bars[-1].close,
    )
