"""Alpaca stock-bar requests without the market-data stack (ADR-0045).

``GET /v2/stocks/bars`` pages, the row parsing and the fixed-interval bars, split out of
``alpaca_history`` so the dashboard's market feed (``trader.market``) can fetch bars without
importing pandas, numpy or ``exchange_calendars``. ``alpaca_history`` re-exports every name here,
so its callers are unchanged; the session-calendar daily bars, quotes and corporate actions stay
there.

Keys travel only in request headers (``AlpacaCredentials.headers``) and never appear in errors.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from trader.config.alpaca_keys import AlpacaCredentials
from trader.data.history import DataQualityError, HistoryBar
from trader.data.sources.http import JsonTransport
from trader.domain.types import iso_utc, require_utc

ALPACA_DATA = "https://data.alpaca.markets"
ALPACA_FEEDS = frozenset({"iex", "sip"})


def alpaca_number(value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise DataQualityError("Alpaca number arrived as a binary float or boolean")
    if isinstance(value, (Decimal, int, str)):
        return Decimal(value)
    raise DataQualityError(f"unexpected numeric value {value!r}")


def alpaca_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise DataQualityError(f"unexpected timestamp {value!r}")
    return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def fetch_bar_rows(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbols: Sequence[str],
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
    feed: str,
) -> dict[str, list[dict[str, Any]]]:
    """Every page of ``GET /v2/stocks/bars`` for ``symbols``, rows grouped by symbol."""
    if feed not in ALPACA_FEEDS:
        raise ValueError("feed must be 'iex' or 'sip'")
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    token: str | None = None
    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": iso_utc(start),
            "end": iso_utc(end),
            "limit": "10000",
            "adjustment": "raw",
            "feed": feed,
            "sort": "asc",
        }
        if token:
            params["page_token"] = token
        payload = transport.get_json(f"{ALPACA_DATA}/v2/stocks/bars", params, credentials.headers())
        if not isinstance(payload, dict) or not isinstance(payload.get("bars", {}), dict):
            raise DataQualityError("unexpected Alpaca bars payload")
        for symbol, rows in (payload.get("bars") or {}).items():
            if symbol not in out:
                raise DataQualityError(f"Alpaca returned an unrequested symbol {symbol}")
            for row in rows:
                if not isinstance(row, dict):
                    raise DataQualityError(f"unexpected Alpaca bar row for {symbol}")
                out[symbol].append(row)
        token = payload.get("next_page_token")
        if not token:
            return out


def history_bar_from_row(row: dict[str, Any], *, start: datetime, minutes: int) -> HistoryBar:
    return HistoryBar(
        start=start,
        minutes=minutes,
        open=alpaca_number(row["o"]),
        high=alpaca_number(row["h"]),
        low=alpaca_number(row["l"]),
        close=alpaca_number(row["c"]),
        volume=alpaca_number(row["v"]),
        vwap=alpaca_number(row["vw"]) if row.get("vw") is not None else None,
        trade_count=int(row["n"]) if row.get("n") is not None else None,
    )


def fetch_alpaca_bars(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbols: Sequence[str],
    *,
    minutes: int,
    start: datetime,
    end: datetime,
    feed: str,
) -> dict[str, list[HistoryBar]]:
    """Fixed-interval bars (``timeframe=<minutes>Min``) per symbol, as the source sent them."""
    rows = fetch_bar_rows(
        transport,
        credentials,
        symbols,
        timeframe=f"{minutes}Min",
        start=start,
        end=end,
        feed=feed,
    )
    return {
        symbol: [
            history_bar_from_row(r, start=alpaca_time(r["t"]), minutes=minutes) for r in symbol_rows
        ]
        for symbol, symbol_rows in rows.items()
    }
