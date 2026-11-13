"""Corporate actions: splits and symbol changes (risk_and_validation.md §6, §9).

* **Raw execution prices are preserved.** Stored bars are never rewritten; an adjusted research
  series is derived on demand.
* **Adjustment is point-in-time.** At decision time ``t`` only splits whose ex-date session has
  opened by ``t`` are applied, so a feature never "knows" about a future split (classic
  backward adjustment over the whole history would leak future splits into past price levels).
* A split multiplies a held quantity by ``new_rate / old_rate`` and divides prices by the same
  factor; cost basis is unchanged. Outstanding orders on the instrument are invalidated, because
  their prices and quantities refer to the pre-split share.
* A symbol change keeps the same instrument identity (``instrument_id``/broker asset ID); only
  the display symbol changes (architecture.md §3).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trader.data.calendars import ExchangeCalendar
from trader.data.history import HistoryBar
from trader.domain.types import DomainError, require_utc


@dataclass(frozen=True, slots=True)
class SplitEvent:
    instrument_id: str
    ex_date: date
    new_rate: Decimal
    old_rate: Decimal
    source: str = ""

    def __post_init__(self) -> None:
        if self.new_rate <= 0 or self.old_rate <= 0:
            raise DomainError("split rates must be positive")

    @property
    def ratio(self) -> Decimal:
        """Shares after the split per share before it (2-for-1 -> 2; 1-for-10 -> 0.1)."""
        return self.new_rate / self.old_rate


@dataclass(frozen=True, slots=True)
class SymbolChange:
    instrument_id: str
    effective_date: date
    old_symbol: str
    new_symbol: str
    source: str = ""


def split_effective_time(split: SplitEvent, calendar: ExchangeCalendar) -> datetime:
    session = calendar.session_for_date(split.ex_date)
    if session is None:
        session = calendar.next_session(split.ex_date)
    return session.open


def point_in_time_adjusted(
    bars: Sequence[HistoryBar],
    splits: Iterable[SplitEvent],
    *,
    as_of: datetime,
    calendar: ExchangeCalendar,
) -> list[HistoryBar]:
    """Bars rescaled to the share basis in force at ``as_of``.

    A bar is divided by the product of the ratios of every split that became effective after
    the bar started and no later than ``as_of``. Splits effective after ``as_of`` are ignored.
    """
    as_of = require_utc(as_of)
    effective = sorted(
        (split_effective_time(s, calendar), s.ratio)
        for s in splits
        if split_effective_time(s, calendar) <= as_of
    )
    if not effective:
        return list(bars)
    out: list[HistoryBar] = []
    for bar in bars:
        divisor = Decimal(1)
        for when, ratio in effective:
            if bar.start < when:
                divisor *= ratio
        out.append(bar if divisor == 1 else bar.scaled(divisor))
    return out


def symbol_on(changes: Iterable[SymbolChange], instrument_id: str, day: date, initial: str) -> str:
    symbol = initial
    for change in sorted(changes, key=lambda c: c.effective_date):
        if change.instrument_id == instrument_id and change.effective_date <= day:
            symbol = change.new_symbol
    return symbol
