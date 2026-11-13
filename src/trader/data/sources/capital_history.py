"""Deep two-sided history from Capital.com DEMO, for research datasets (ADR-0052).

``/prices`` returns at most 1000 bars a request, the newest ones up to ``to``. This walks
backwards from the requested end: each page's oldest bar gives the next ``to``, one second
earlier, until the requested start is reached or the source has nothing older. How far back the
source goes differs by epic and resolution and is not documented, so the walk reports where and
why it stopped instead of assuming a depth.

Read only, demo host only: it uses :class:`CapitalPrices` and adds no request of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trader.data.history import HistoryBar
from trader.data.sources.capital_prices import MAX_BARS, QuotePage
from trader.data.sources.http import TransportError
from trader.domain.types import iso_utc, require_utc

#: A page cap far above any real walk (ten years of hourly bars is about 60 pages); it only
#: stops a source that keeps answering without ever getting older.
MAX_PAGES = 400

StopReason = str


class QuoteSource(Protocol):
    """What the walk needs from :class:`CapitalPrices`."""

    def fetch_quote_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> QuotePage: ...


@dataclass(frozen=True, slots=True)
class QuoteHistory:
    """Bid and ask bars of one epic, oldest first and aligned, with how the walk went."""

    bid: tuple[HistoryBar, ...]
    ask: tuple[HistoryBar, ...]
    pages: int
    one_sided: int
    crossed: int
    #: ``reached_start`` (the request is covered), ``source_exhausted`` (an empty page, a page
    #: with nothing older, or the source's 404 for a range it does not hold) or ``page_cap``.
    stopped: StopReason

    def report(self) -> dict[str, object]:
        return {
            "pages": self.pages,
            "rows": len(self.bid),
            "first_start": iso_utc(self.bid[0].start) if self.bid else None,
            "last_start": iso_utc(self.bid[-1].start) if self.bid else None,
            "rows_skipped_one_sided": self.one_sided,
            "rows_skipped_ask_below_bid": self.crossed,
            "stopped": self.stopped,
        }


def fetch_capital_quote_history(
    prices: QuoteSource,
    epic: str,
    *,
    minutes: int,
    start: datetime,
    end: datetime,
    max_pages: int = MAX_PAGES,
) -> QuoteHistory:
    """Every two-sided bar of ``epic`` with ``start <= bar.start < end``, paged backwards."""
    start, end = require_utc(start), require_utc(end)
    if end <= start:
        raise ValueError("the history request ends before it starts")
    bid: dict[datetime, HistoryBar] = {}
    ask: dict[datetime, HistoryBar] = {}
    pages = one_sided = crossed = 0
    to = end - timedelta(seconds=1)
    oldest_seen: datetime | None = None
    stopped: StopReason = "page_cap"
    while pages < max_pages:
        try:
            page = prices.fetch_quote_bars(epic, minutes=minutes, max_bars=MAX_BARS, to=to)
        except TransportError as exc:
            # the source answers 404 (error.prices.not-found) for a range it does not hold
            if exc.status == 404 and pages > 0:
                stopped = "source_exhausted"
                break
            raise
        pages += 1
        one_sided += page.one_sided
        crossed += page.crossed
        if not page.bid:
            stopped = "source_exhausted"
            break
        for b, a in zip(page.bid, page.ask, strict=True):
            if start <= b.start < end:
                bid[b.start] = b
                ask[a.start] = a
        oldest = page.bid[0].start
        if oldest <= start:
            stopped = "reached_start"
            break
        if oldest_seen is not None and oldest >= oldest_seen:
            stopped = "source_exhausted"  # the same page again: nothing older exists
            break
        oldest_seen = oldest
        to = oldest - timedelta(seconds=1)
    order = sorted(bid)
    return QuoteHistory(
        bid=tuple(bid[t] for t in order),
        ask=tuple(ask[t] for t in order),
        pages=pages,
        one_sided=one_sided,
        crossed=crossed,
        stopped=stopped,
    )
