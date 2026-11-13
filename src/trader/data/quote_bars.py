"""Two-sided bars: one bar's bid side and ask side together (ADR-0052).

A two-sided dataset stores each epic as two ordinary history files, ``<id>.BID`` and
``<id>.ASK``, with exactly the same bar starts (``data/ingest.py``). This module loads them as
one sequence, so that a study fills a buy at the ask and a sell at the bid from the source's own
quotes instead of an assumed spread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.data.history import DataQualityError, HistoryBar
from trader.data.ingest import ASK_SUFFIX, BID_SUFFIX, load_bars
from trader.data.manifest import HistoryDataset


@dataclass(frozen=True, slots=True)
class QuoteBar:
    """One bar as quoted: what one could sell at (``bid``) and buy at (``ask``)."""

    bid: HistoryBar
    ask: HistoryBar

    def __post_init__(self) -> None:
        if self.bid.start != self.ask.start or self.bid.minutes != self.ask.minutes:
            raise DataQualityError("a quote bar's two sides must be the same bar")

    @property
    def start(self) -> datetime:
        return self.bid.start

    @property
    def minutes(self) -> int:
        return self.bid.minutes

    @property
    def end(self) -> datetime:
        return self.bid.end

    @property
    def mid_open(self) -> Decimal:
        return (self.bid.open + self.ask.open) / 2

    @property
    def mid_close(self) -> Decimal:
        return (self.bid.close + self.ask.close) / 2

    @property
    def spread_bps_at_close(self) -> Decimal:
        """The quoted spread at the close, in basis points of the mid."""
        return (self.ask.close - self.bid.close) / self.mid_close * 10000


def load_quote_bars(
    ds: HistoryDataset,
    instrument_id: str,
    repo_root: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[QuoteBar]:
    """The two-sided bars of ``instrument_id`` (without its side suffix), oldest first. Both
    files' content checksums are verified by :func:`load_bars`; sides that do not line up bar
    for bar are refused rather than joined loosely."""
    bid = load_bars(ds, instrument_id + BID_SUFFIX, repo_root, start=start, end=end)
    ask = load_bars(ds, instrument_id + ASK_SUFFIX, repo_root, start=start, end=end)
    if [b.start for b in bid] != [a.start for a in ask]:
        raise DataQualityError(f"{ds.name}/{instrument_id}: bid and ask bars do not line up")
    return [QuoteBar(b, a) for b, a in zip(bid, ask, strict=True)]


def fold_weekend_stub(bars: Sequence[QuoteBar]) -> list[QuoteBar]:
    """Daily bars of a market that opens on Sunday evening: the short weekend bar is the start
    of Monday's session, so it is joined to the bar that follows. The joined bar keeps the
    weekend bar's start and open (that is when and where the session opened) and runs to the
    end of the following bar. A weekend bar with nothing after it is an unfinished session and
    is left out."""
    out: list[QuoteBar] = []
    stub: QuoteBar | None = None
    for bar in bars:
        if bar.start.weekday() >= 5:
            stub = bar if stub is None else _join(stub, bar)
            continue
        out.append(bar if stub is None else _join(stub, bar))
        stub = None
    return out


def daily_from_hourly(bars: Sequence[QuoteBar], *, weekend_closed: bool) -> list[QuoteBar]:
    """Daily two-sided bars built from hourly ones: one bar per UTC day [00:00, 24:00) with the
    hours that quoted, the same day as the broker's own daily candle. For a market closed at the
    weekend (``weekend_closed``) the Saturday and Sunday hours - the Sunday-evening open - belong
    to Monday's bar. Each side keeps its own open (first hour), high, low and close (last hour);
    the bar starts at its first hour and ends at its last hour's end. A last group whose day has
    not ended (no later hour exists) is still built: callers pass only finished hours."""
    groups: dict[date, list[QuoteBar]] = {}
    for bar in bars:
        day = bar.start.date()
        if weekend_closed and day.weekday() >= 5:
            day += timedelta(days=7 - day.weekday())
        groups.setdefault(day, []).append(bar)
    out: list[QuoteBar] = []
    for day in sorted(groups):
        hours = sorted(groups[day], key=lambda b: b.start)
        out.append(
            QuoteBar(
                _span([h.bid for h in hours]),
                _span([h.ask for h in hours]),
            )
        )
    return out


def _span(side: Sequence[HistoryBar]) -> HistoryBar:
    first, last = side[0], side[-1]
    return HistoryBar(
        start=first.start,
        minutes=int((last.end - first.start).total_seconds() // 60),
        open=first.open,
        high=max(b.high for b in side),
        low=min(b.low for b in side),
        close=last.close,
        volume=sum((b.volume for b in side), start=Decimal(0)),
    )


def _join(first: QuoteBar, second: QuoteBar) -> QuoteBar:
    return QuoteBar(_join_side(first.bid, second.bid), _join_side(first.ask, second.ask))


def _join_side(first: HistoryBar, second: HistoryBar) -> HistoryBar:
    return HistoryBar(
        start=first.start,
        minutes=int((second.end - first.start).total_seconds() // 60),
        open=first.open,
        high=max(first.high, second.high),
        low=min(first.low, second.low),
        close=second.close,
        volume=first.volume + second.volume,
    )
