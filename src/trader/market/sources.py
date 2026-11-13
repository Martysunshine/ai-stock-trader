"""The three price sources of the dashboard's market feed (ADR-0045).

Each source turns one watchlist group into bars per instrument and series (``5m``, ``1h``,
``1d``); ``trader.market.feed`` merges them into the stored files. Sources only read market data.

* **Alpaca IEX (stocks, day rule ``nyse``).** One multi-symbol request per series. Only regular
  session bars are kept (09:30-16:00 New York time, weekdays; without the exchange calendar an
  early-close afternoon is kept too). ``1h`` buckets are built from 30-minute bars and start at the
  session open (09:30, 10:30, ..., 15:30), as the console rolls up 5-minute history; ``1d`` is
  Alpaca's daily bar, re-stamped from 00:00 New York to the 09:30 open. Keys come from the OS
  keyring (``require_alpaca``), only in request headers.
* **Kraken public OHLC (crypto, day rule ``utc``).** Keyless; ``fetch_kraken_ohlc`` returns the
  latest 720 committed rows per interval (5, 60 and 1440 minutes), paced one call a second.
  Deeper ``5m``/``1h`` history builds up as the feed keeps running.
* **Capital.com demo (CFDs, day rule ``utc``).** Mid prices (``CapitalPrices``). The newest bars
  since the last stored one, plus one older page per series and run until the retention horizon is
  reached or the source has nothing older.

A failure is reported per instrument where it is per instrument (Kraken, Capital.com) and for the
whole group where one request serves every symbol (Alpaca) or the session is refused.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from trader.config.secrets import SecretsError
from trader.data.history import HistoryBar
from trader.data.sources.alpaca_bars import (
    alpaca_time,
    fetch_alpaca_bars,
    fetch_bar_rows,
    history_bar_from_row,
)
from trader.data.sources.capital_prices import MAX_BARS, CapitalAuthError, CapitalPrices
from trader.data.sources.http import JsonTransport
from trader.data.sources.public_crypto import fetch_kraken_ohlc
from trader.market.watchlists import SERIES, SERIES_MINUTES, GroupConfig, SeriesName

if TYPE_CHECKING:
    from trader.config.alpaca_keys import AlpacaCredentials

#: A stored bar: ``[start_seconds, open, high, low, close, volume]`` (decimals as strings).
Row = list[int | str]
StoredSeries = Mapping[SeriesName, Sequence[Row]]
FetchedSeries = dict[SeriesName, list[HistoryBar]]

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_MINUTES = 390  # 09:30-16:00
ALPACA_FEED = "iex"


@dataclass
class FetchResult:
    """Bars per instrument symbol and series, and the instruments that failed (with why)."""

    bars: dict[str, FetchedSeries] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    #: Set when the source refused the session: no request before this time.
    retry_after: datetime | None = None


class GroupSource(Protocol):
    def fetch(
        self,
        group: GroupConfig,
        stored: Mapping[str, StoredSeries],
        *,
        now: datetime,
        retention: Mapping[SeriesName, timedelta],
    ) -> FetchResult: ...


def short_error(exc: BaseException, limit: int = 300) -> str:
    """One line for the index and the log. Callers never put key material in exceptions."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _last_start(rows: Sequence[Row]) -> int | None:
    return int(rows[-1][0]) if rows else None


def _first_start(rows: Sequence[Row]) -> int | None:
    return int(rows[0][0]) if rows else None


def _since(
    stored: Mapping[str, StoredSeries],
    symbols: Sequence[str],
    series: SeriesName,
    *,
    now: datetime,
    retention: timedelta,
    back: timedelta = timedelta(0),
) -> datetime:
    """Where an incremental multi-symbol request starts: the oldest "last stored bar" (minus
    ``back``), or the retention horizon when any symbol has nothing stored."""
    horizon = now - retention
    lasts = [_last_start(stored.get(s, {}).get(series, ())) for s in symbols]
    if not lasts or any(t is None for t in lasts):
        return horizon
    oldest = min(t for t in lasts if t is not None)
    return max(horizon, datetime.fromtimestamp(oldest, tz=UTC) - back)


# ----------------------------------------------------------------------------- stocks
def ny_minutes(ts: datetime) -> tuple[datetime, int]:
    """The New York local time of ``ts`` and its minutes after midnight."""
    local = ts.astimezone(NEW_YORK)
    return local, local.hour * 60 + local.minute


def regular_session(start: datetime, minutes: int) -> bool:
    """A bar lies inside the regular session (weekday, 09:30-16:00 New York time)."""
    local, mins = ny_minutes(start)
    open_min = REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute
    return local.weekday() < 5 and open_min <= mins and mins + minutes <= open_min + REGULAR_MINUTES


def session_open(ts: datetime) -> datetime:
    """09:30 New York on the local date of ``ts``, in UTC."""
    local = ts.astimezone(NEW_YORK)
    return datetime.combine(local.date(), REGULAR_OPEN, tzinfo=NEW_YORK).astimezone(UTC)


def session_hours(bars: Sequence[HistoryBar]) -> list[HistoryBar]:
    """Roll regular-session bars up to hours anchored at the open (the last one 30 minutes)."""
    out: list[HistoryBar] = []
    for bar in sorted(bars, key=lambda b: b.start):
        opened = session_open(bar.start)
        bucket = opened + timedelta(hours=(bar.start - opened) // timedelta(hours=1))
        if out and out[-1].start == bucket:
            cur = out[-1]
            out[-1] = HistoryBar(
                start=bucket,
                minutes=60,
                open=cur.open,
                high=max(cur.high, bar.high),
                low=min(cur.low, bar.low),
                close=bar.close,
                volume=cur.volume + bar.volume,
            )
        else:
            out.append(
                HistoryBar(
                    start=bucket,
                    minutes=60,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
            )
    return out


class AlpacaIexSource:
    def __init__(
        self, transport: JsonTransport, credentials: Callable[[], AlpacaCredentials]
    ) -> None:
        self._transport = transport
        self._credentials = credentials

    def fetch(
        self,
        group: GroupConfig,
        stored: Mapping[str, StoredSeries],
        *,
        now: datetime,
        retention: Mapping[SeriesName, timedelta],
    ) -> FetchResult:
        creds = self._credentials()
        by_source = {i.source_symbol: i.symbol for i in group.instruments}
        symbols = list(by_source)
        mine = {i.source_symbol: stored.get(i.symbol, {}) for i in group.instruments}
        result = FetchResult(bars={s: {} for s in by_source.values()})

        five = fetch_alpaca_bars(
            self._transport,
            creds,
            symbols,
            minutes=5,
            start=_since(mine, symbols, "5m", now=now, retention=retention["5m"]),
            end=now,
            feed=ALPACA_FEED,
        )
        # Re-fetch the whole last stored hour so its bucket is rebuilt from both halves.
        thirty = fetch_alpaca_bars(
            self._transport,
            creds,
            symbols,
            minutes=30,
            start=_since(mine, symbols, "1h", now=now, retention=retention["1h"]),
            end=now,
            feed=ALPACA_FEED,
        )
        daily = fetch_bar_rows(
            self._transport,
            creds,
            symbols,
            timeframe="1Day",
            start=_since(
                mine, symbols, "1d", now=now, retention=retention["1d"], back=timedelta(days=1)
            ),
            end=now,
            feed=ALPACA_FEED,
        )
        for source_symbol, symbol in by_source.items():
            result.bars[symbol] = {
                "5m": [b for b in five.get(source_symbol, []) if regular_session(b.start, 5)],
                "1h": session_hours(
                    [b for b in thirty.get(source_symbol, []) if regular_session(b.start, 30)]
                ),
                "1d": [
                    history_bar_from_row(
                        row, start=session_open(alpaca_time(row["t"])), minutes=REGULAR_MINUTES
                    )
                    for row in daily.get(source_symbol, [])
                ],
            }
        return result


# ----------------------------------------------------------------------------- crypto
class KrakenPublicSource:
    def __init__(self, transport: JsonTransport) -> None:
        self._transport = transport

    def fetch(
        self,
        group: GroupConfig,
        stored: Mapping[str, StoredSeries],
        *,
        now: datetime,
        retention: Mapping[SeriesName, timedelta],
    ) -> FetchResult:
        result = FetchResult()
        for inst in group.instruments:
            try:
                result.bars[inst.symbol] = {
                    series: fetch_kraken_ohlc(
                        self._transport, inst.source_symbol, minutes=SERIES_MINUTES[series]
                    )
                    for series in SERIES
                }
            except Exception as exc:  # one pair failing must not stop the others
                result.errors[inst.symbol] = short_error(exc)
        return result


# ----------------------------------------------------------------------------- CFDs
class CapitalDemoSource:
    def __init__(self, prices: CapitalPrices) -> None:
        self._prices = prices
        #: (symbol, series) whose source had nothing older than what is stored.
        self._exhausted: set[tuple[str, SeriesName]] = set()

    @property
    def backoff_until(self) -> datetime | None:
        return self._prices.backoff_until

    def fetch(
        self,
        group: GroupConfig,
        stored: Mapping[str, StoredSeries],
        *,
        now: datetime,
        retention: Mapping[SeriesName, timedelta],
    ) -> FetchResult:
        result = FetchResult()
        for inst in group.instruments:
            try:
                result.bars[inst.symbol] = {
                    series: self._series(
                        inst.symbol,
                        inst.source_symbol,
                        series,
                        stored.get(inst.symbol, {}).get(series, ()),
                        now=now,
                        retention=retention[series],
                    )
                    for series in SERIES
                }
            except (CapitalAuthError, SecretsError):
                raise  # no session (refused, or no keys): the whole group stops, with one message
            except Exception as exc:
                result.errors[inst.symbol] = short_error(exc)
        result.retry_after = self._prices.backoff_until
        return result

    def _series(
        self,
        symbol: str,
        epic: str,
        series: SeriesName,
        rows: Sequence[Row],
        *,
        now: datetime,
        retention: timedelta,
    ) -> list[HistoryBar]:
        minutes = SERIES_MINUTES[series]
        step = minutes * 60
        last = _last_start(rows)
        wanted = MAX_BARS if last is None else math.ceil((now.timestamp() - last) / step) + 1
        bars = self._prices.fetch_bars(epic, minutes=minutes, max_bars=max(2, wanted))
        starts = [int(b.start.timestamp()) for b in bars[:1]]
        stored_first = _first_start(rows)
        if stored_first is not None:
            starts.append(stored_first)
        if not starts or (symbol, series) in self._exhausted:
            return bars
        first = min(starts)
        if first - step <= (now - retention).timestamp():
            return bars  # the stored series already reaches the retention horizon
        older = self._prices.fetch_bars(
            epic, minutes=minutes, max_bars=MAX_BARS, to=datetime.fromtimestamp(first, tz=UTC)
        )
        if not any(b.start.timestamp() < first for b in older):
            self._exhausted.add((symbol, series))
        return older + bars
