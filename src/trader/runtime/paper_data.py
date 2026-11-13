"""Market data for the paper runtime: polled completed bars and latest quotes.

Candidate A decides on *completed* five-minute bars only (strategy_and_profitability.md §4), so
the paper runtime polls the data API shortly after each bar end instead of building bars from a
trade stream, and polls the latest quote for the spread and staleness checks and for monitored
exits (ADR-0005). Both come from one feed (IEX by default; instructions.md §5: never mix feeds).

Conversion rules (fail closed, never repair):

* A bar's *availability time* is the local receipt time, never earlier than its end. Only bars
  with a source VWAP are usable (the stock baseline uses the source VWAP; nothing is
  substituted), and an inconsistent bar is dropped.
* A quote keeps the source event time; its receipt time is the local clock. With real clocks
  the source can be *ahead* of the local clock, which the domain model rejects (receipt before
  event). The receipt time is then clamped to the event time and the skew is returned to the
  caller, which raises a ``CLOCK_SKEW`` pause when it exceeds the policy limit instead of
  crashing. One-sided or crossed books are dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trader.data.history import DataQualityError, HistoryBar
from trader.data.sources.alpaca_history import (
    AlpacaCredentials,
    LatestQuote,
    fetch_alpaca_bars,
    fetch_alpaca_latest_quotes,
    fetch_alpaca_recent_spreads_bps,
)
from trader.data.sources.http import JsonTransport, TransportError
from trader.domain.market import Bar, Quote
from trader.domain.types import DomainError, require_utc

ZERO_SKEW = timedelta(0)


class PaperMarketData(Protocol):
    """Polled market data. ``feed`` is the feed identifier stamped on bars and quotes."""

    @property
    def feed(self) -> str: ...

    def completed_bars(
        self, symbols: Sequence[str], *, minutes: int, start: datetime, end: datetime
    ) -> dict[str, list[HistoryBar]]: ...

    def latest_quotes(self, symbols: Sequence[str]) -> dict[str, LatestQuote]: ...


def feed_id(source_feed: str) -> str:
    """The feed identifier for an Alpaca data feed (``iex`` -> ``alpaca-iex``)."""
    return f"alpaca-{source_feed}"


class AlpacaMarketData:
    """Alpaca market-data REST polling (``/v2/stocks/bars``, ``/v2/stocks/quotes/latest``)."""

    def __init__(
        self, transport: JsonTransport, credentials: AlpacaCredentials, *, source_feed: str = "iex"
    ) -> None:
        if source_feed not in {"iex", "sip"}:
            raise ValueError("feed must be 'iex' or 'sip'")
        self._transport = transport
        self._credentials = credentials
        self._source_feed = source_feed

    @property
    def feed(self) -> str:
        return feed_id(self._source_feed)

    def completed_bars(
        self, symbols: Sequence[str], *, minutes: int, start: datetime, end: datetime
    ) -> dict[str, list[HistoryBar]]:
        return fetch_alpaca_bars(
            self._transport,
            self._credentials,
            symbols,
            minutes=minutes,
            start=start,
            end=end,
            feed=self._source_feed,
        )

    def latest_quotes(self, symbols: Sequence[str]) -> dict[str, LatestQuote]:
        return fetch_alpaca_latest_quotes(
            self._transport, self._credentials, symbols, feed=self._source_feed
        )


class SpreadReference(Protocol):
    """A full-market spread estimate per symbol, in bps (ADR-0061).

    The IEX quote is one venue's book: its bid is never above and its ask never below the
    consolidated best, and its spread is often ten times wider than the market's. Alpaca's paper
    broker fills at the consolidated price, so the entry spread check and the spread cost use
    this estimate. A symbol without an estimate is absent; the caller then keeps the venue quote.
    """

    @property
    def source(self) -> str: ...

    def spreads_bps(self, symbols: Sequence[str], *, now: datetime) -> dict[str, Decimal]: ...


#: A free data plan serves consolidated quotes only once they are 15 minutes old.
SIP_DELAY = timedelta(minutes=16)
#: The window of delayed quotes the median is taken over, and how many quotes at most.
SIP_WINDOW = timedelta(minutes=15)
SIP_QUOTES = 200
#: Fewer two-sided quotes than this in the window: no estimate (the venue quote stays).
SIP_MIN_QUOTES = 20


#: Requests in flight at once. One request takes about 0.7 s, so 20 symbols one after another
#: took 14 s on 2026-11-11: longer than a quote may age before a decision.
SIP_WORKERS = 8


class DelayedSipSpreads:
    """Median consolidated spread of the last quotes that are 16 to 31 minutes old (ADR-0061).

    One request per symbol, at most once per ``refresh`` per symbol, several symbols at a time.
    A failed request drops the symbol's estimate, so the entry check falls back to the venue
    quote (fail closed).
    """

    def __init__(
        self,
        transport: JsonTransport,
        credentials: AlpacaCredentials,
        *,
        refresh: timedelta = timedelta(minutes=5),
        workers: int = SIP_WORKERS,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self._transport = transport
        self._credentials = credentials
        self._refresh = refresh
        self._workers = workers
        self._held: dict[str, tuple[datetime, Decimal]] = {}

    @property
    def source(self) -> str:
        return "alpaca-sip-delayed"

    def _estimate(self, symbol: str, now: datetime) -> Decimal | None:
        try:
            spreads = fetch_alpaca_recent_spreads_bps(
                self._transport,
                self._credentials,
                symbol,
                start=now - SIP_DELAY - SIP_WINDOW,
                end=now - SIP_DELAY,
                feed="sip",
                limit=SIP_QUOTES,
            )
        except (TransportError, DataQualityError, ValueError):
            return None
        if len(spreads) < SIP_MIN_QUOTES:
            return None
        return median_decimal(spreads)

    def spreads_bps(self, symbols: Sequence[str], *, now: datetime) -> dict[str, Decimal]:
        now = require_utc(now)
        due = []
        for symbol in dict.fromkeys(symbols):
            held = self._held.get(symbol)
            if held is None or now - held[0] >= self._refresh:
                self._held.pop(symbol, None)
                due.append(symbol)
        if len(due) > 1 and self._workers > 1:
            with ThreadPoolExecutor(max_workers=min(self._workers, len(due))) as pool:
                estimates = list(pool.map(lambda s: self._estimate(s, now), due))
        else:
            estimates = [self._estimate(symbol, now) for symbol in due]
        for symbol, estimate in zip(due, estimates, strict=True):
            if estimate is not None:
                self._held[symbol] = (now, estimate)
        return {symbol: self._held[symbol][1] for symbol in symbols if symbol in self._held}


def median_decimal(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def quote_from_source(
    raw: LatestQuote,
    *,
    instrument_id: str,
    feed: str,
    received_at: datetime,
    sequence: int,
) -> tuple[Quote | None, timedelta]:
    """A domain quote (or ``None`` when the book is unusable) and the clock skew observed.

    The skew is how far the source event time is *ahead* of the local receipt time (zero when
    the local clock is not behind).
    """
    received_at = require_utc(received_at)
    skew = max(raw.event_time - received_at, ZERO_SKEW)
    if raw.bid <= 0 or raw.ask <= 0 or raw.ask < raw.bid:
        return None, skew
    try:
        quote = Quote(
            instrument_id=instrument_id,
            feed=feed,
            event_time=raw.event_time,
            receipt_time=max(received_at, raw.event_time),
            sequence=sequence,
            bid=raw.bid,
            ask=raw.ask,
            bid_size=max(raw.bid_size, Decimal(0)),
            ask_size=max(raw.ask_size, Decimal(0)),
        )
    except (DomainError, ValueError):
        return None, skew
    return quote, skew


def bar_from_source(
    raw: HistoryBar, *, instrument_id: str, feed: str, received_at: datetime
) -> tuple[Bar | None, timedelta]:
    """A completed domain bar available from ``received_at`` (or ``None``) and the skew."""
    received_at = require_utc(received_at)
    skew = max(raw.end - received_at, ZERO_SKEW)
    if raw.vwap is None or raw.ohlc_problems():
        return None, skew
    try:
        bar = Bar(
            instrument_id=instrument_id,
            feed=feed,
            minutes=raw.minutes,
            start=raw.start,
            end=raw.end,
            available_time=max(received_at, raw.end),
            complete=True,
            open=raw.open,
            high=raw.high,
            low=raw.low,
            close=raw.close,
            volume=raw.volume,
            trade_count=raw.trade_count or 0,
            vwap=min(max(raw.vwap, raw.low), raw.high),
        )
    except (DomainError, ValueError):
        return None, skew
    return bar, skew


__all__ = [
    "AlpacaMarketData",
    "DelayedSipSpreads",
    "PaperMarketData",
    "SpreadReference",
    "bar_from_source",
    "feed_id",
    "median_decimal",
    "quote_from_source",
]
