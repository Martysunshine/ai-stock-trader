"""Read-only market state for strategies (they never see a broker or account)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from trader.data.bars import BarBuilder
from trader.domain.market import AnyMarketEvent, Bar, Quote, Trade, TradingStatus
from trader.domain.types import ZERO


class MarketView:
    def __init__(self, *, feed: str, decision_minutes: int) -> None:
        self.feed = feed
        self.decision_minutes = decision_minutes
        self.quotes: dict[str, Quote] = {}
        #: Full-market spread estimates in bps where the feed is one venue (ADR-0061); set by
        #: the paper runtime, absent when unknown (the quote's own spread is used then).
        self.market_spreads: dict[str, Decimal] = {}
        self.halted: set[str] = set()
        self._builders: dict[str, BarBuilder] = {}
        self.bars: dict[str, list[Bar]] = defaultdict(list)
        self._session_volume: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._session_notional: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._session_open: dict[str, Decimal] = {}

    def on_event(self, event: AnyMarketEvent) -> None:
        iid = event.instrument_id
        if isinstance(event, Quote):
            if event.feed == self.feed:
                self.quotes[iid] = event
        elif isinstance(event, Trade):
            if event.feed != self.feed:
                return  # never silently mix feeds (instructions.md §5)
            builder = self._builders.get(iid)
            if builder is None:
                builder = BarBuilder(iid, self.feed, self.decision_minutes)
                self._builders[iid] = builder
            builder.add(event)
            self._session_volume[iid] += event.size
            self._session_notional[iid] += event.size * event.price
            self._session_open.setdefault(iid, event.price)
        elif isinstance(event, TradingStatus):
            if event.halted:
                self.halted.add(iid)
            else:
                self.halted.discard(iid)

    def finalize(self, now: datetime, instrument_id: str | None = None) -> list[Bar]:
        """Finalise bars whose availability time has passed (optionally for one instrument).

        Decisions for an instrument are taken on that instrument's own fresh quote, so the
        runner finalises per instrument; other instruments' bars wait for their own quotes.
        """
        finished: list[Bar] = []
        targets = [instrument_id] if instrument_id is not None else sorted(self._builders)
        for iid in targets:
            if iid not in self._builders:
                continue
            for bar in self._builders[iid].finalize_ready(now):
                self.bars[iid].append(bar)
                finished.append(bar)
        return finished

    def add_completed_bar(self, bar: Bar) -> bool:
        """Append a bar completed by the data source (paper polls completed bars).

        Same rules as bars built here: one feed only, complete bars only, strictly increasing
        start times. A bar older than (or equal to) the newest one held is a late arrival and
        never rewrites history; it is refused (``False``).
        """
        if bar.feed != self.feed or not bar.complete or bar.minutes != self.decision_minutes:
            return False
        held = self.bars[bar.instrument_id]
        if held and bar.start <= held[-1].start:
            return False
        held.append(bar)
        return True

    def marks(self) -> dict[str, Decimal]:
        """Conservative marks for long positions: the bid."""
        return {iid: q.bid for iid, q in self.quotes.items()}

    def session_open_price(self, instrument_id: str) -> Decimal | None:
        return self._session_open.get(instrument_id)

    def reset_session(self) -> None:
        self._session_volume.clear()
        self._session_notional.clear()
        self._session_open.clear()
