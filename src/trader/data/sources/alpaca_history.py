"""Alpaca stock bars, latest quotes and corporate actions (markets_and_brokers.md §2, §6).

The paper runtime (``runtime/paper.py``) polls the same bars endpoint for *completed* five-minute
bars and the latest-quote endpoint for the entry spread check; research uses the history.

Requires the owner's Alpaca API keys (a free paper account suffices for historical data). Keys
are read from the environment variables ``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY`` at run
time only; they are never stored in configuration, manifests or logs (instructions.md §5). With
no keys the dataset is recorded as BLOCKED with the owner action required.

Bars are requested **raw** (``adjustment=raw``): execution research needs actual traded prices,
and split adjustment is applied point-in-time by the research engine from the corporate-action
records (risk_and_validation.md §6). The ``feed`` parameter (``iex`` or ``sip``) is recorded in
the manifest because IEX volume is single-venue and must never be mixed with consolidated
volume (instructions.md §5).

The response shapes follow Alpaca's documentation and are exercised with fixtures. The daily-bar
shape (``timeframe=1Day``: one row per session stamped 00:00 New York) and the asset records
used for the ETF instrument ids were checked against live read-only responses on 2026-11-09
(``data/daily_bars.py``).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trader.config.alpaca_keys import KEY_ENV as KEY_ENV
from trader.config.alpaca_keys import SECRET_ENV as SECRET_ENV
from trader.config.alpaca_keys import AlpacaCredentials as AlpacaCredentials
from trader.data.calendars import ExchangeCalendar
from trader.data.corporate_actions import SplitEvent, SymbolChange
from trader.data.daily_bars import (
    DAILY_TIMEFRAME,
    expected_session_opens,
    session_slot,
    source_day_stamp,
)
from trader.data.history import DataQualityError, HistoryBar

# The light row fetch lives in ``alpaca_bars`` (no pandas or calendars; ADR-0045). Re-exported
# here, including the old private names, so existing callers are unchanged.
from trader.data.sources.alpaca_bars import ALPACA_DATA as ALPACA_DATA
from trader.data.sources.alpaca_bars import alpaca_number as alpaca_number
from trader.data.sources.alpaca_bars import alpaca_time as alpaca_time
from trader.data.sources.alpaca_bars import fetch_alpaca_bars as fetch_alpaca_bars
from trader.data.sources.alpaca_bars import fetch_bar_rows as fetch_bar_rows
from trader.data.sources.alpaca_bars import history_bar_from_row as history_bar_from_row
from trader.data.sources.http import JsonTransport
from trader.domain.types import iso_utc

_num = alpaca_number
_ts = alpaca_time
_fetch_bar_rows = fetch_bar_rows
_history_bar = history_bar_from_row


def credentials_from_env() -> AlpacaCredentials | None:
    key, secret = os.environ.get(KEY_ENV, ""), os.environ.get(SECRET_ENV, "")
    if not key or not secret:
        return None
    return AlpacaCredentials(key, secret)


def fetch_alpaca_daily_bars(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    feed: str,
    calendar: ExchangeCalendar,
) -> dict[str, list[HistoryBar]]:
    """Daily bars (``timeframe=1Day``) for the sessions with ``start <= open < end``.

    Alpaca stamps a daily bar 00:00 New York of its session date and treats ``end`` as
    inclusive, so the request runs from the first to the last expected session's stamp. Each row
    becomes the bar ``[session open, session close)`` of the session it labels (early closes
    included); a row that labels no session keeps its own timestamp so the quality check counts
    and excludes it (``data/daily_bars.py``).
    """
    opens, _ = expected_session_opens(calendar, start, end)
    if not opens:
        return {s: [] for s in symbols}
    rows = _fetch_bar_rows(
        transport,
        credentials,
        symbols,
        timeframe=DAILY_TIMEFRAME,
        start=source_day_stamp(calendar.session_date_of(opens[0])),
        end=source_day_stamp(calendar.session_date_of(opens[-1])),
        feed=feed,
    )
    out: dict[str, list[HistoryBar]] = {}
    for symbol, symbol_rows in rows.items():
        bars: list[HistoryBar] = []
        for row in symbol_rows:
            bar_start, bar_minutes = session_slot(calendar, _ts(row["t"]))
            bars.append(_history_bar(row, start=bar_start, minutes=bar_minutes))
        out[symbol] = bars
    return out


@dataclass(frozen=True, slots=True)
class LatestQuote:
    """The latest top-of-book quote of one symbol, exactly as the source reported it."""

    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    event_time: datetime


def fetch_alpaca_latest_quotes(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbols: Sequence[str],
    *,
    feed: str,
) -> dict[str, LatestQuote]:
    """``GET /v2/stocks/quotes/latest`` for several symbols (paper decisions, spread check).

    Symbols without a quote are simply absent. One-sided or crossed books are returned as
    reported; the caller decides whether a quote is usable (it never invents a side).
    """
    if feed not in {"iex", "sip"}:
        raise ValueError("feed must be 'iex' or 'sip'")
    if not symbols:
        return {}
    payload = transport.get_json(
        f"{ALPACA_DATA}/v2/stocks/quotes/latest",
        {"symbols": ",".join(symbols), "feed": feed},
        credentials.headers(),
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("quotes") or {}, dict):
        raise DataQualityError("unexpected Alpaca latest-quotes payload")
    out: dict[str, LatestQuote] = {}
    for symbol, row in (payload.get("quotes") or {}).items():
        if symbol not in symbols:
            raise DataQualityError(f"Alpaca returned an unrequested symbol {symbol}")
        if not isinstance(row, dict):
            raise DataQualityError(f"unexpected Alpaca quote for {symbol}")
        out[symbol] = LatestQuote(
            symbol=symbol,
            bid=_num(row.get("bp", 0)),
            ask=_num(row.get("ap", 0)),
            bid_size=_num(row.get("bs", 0)),
            ask_size=_num(row.get("as", 0)),
            event_time=_ts(row.get("t")),
        )
    return out


def fetch_alpaca_recent_spreads_bps(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    feed: str,
    limit: int,
) -> list[Decimal]:
    """Spreads in bps of the last ``limit`` two-sided quotes of one symbol in ``[start, end]``.

    ``GET /v2/stocks/quotes`` sorted newest first. One-sided and crossed quotes are left out.
    A free data plan serves the consolidated (``sip``) feed only when ``end`` is at least 15
    minutes old; the caller chooses ``end`` accordingly (ADR-0061).
    """
    if feed not in {"iex", "sip"}:
        raise ValueError("feed must be 'iex' or 'sip'")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    payload = transport.get_json(
        f"{ALPACA_DATA}/v2/stocks/quotes",
        {
            "symbols": symbol,
            "start": iso_utc(start),
            "end": iso_utc(end),
            "feed": feed,
            "limit": str(limit),
            "sort": "desc",
        },
        credentials.headers(),
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("quotes") or {}, dict):
        raise DataQualityError("unexpected Alpaca quotes payload")
    rows = (payload.get("quotes") or {}).get(symbol) or []
    if not isinstance(rows, list):
        raise DataQualityError(f"unexpected Alpaca quotes for {symbol}")
    out: list[Decimal] = []
    for row in rows:
        if not isinstance(row, dict):
            raise DataQualityError(f"unexpected Alpaca quote for {symbol}")
        bid, ask = _num(row.get("bp", 0)), _num(row.get("ap", 0))
        if bid <= 0 or ask < bid:
            continue
        out.append((ask - bid) / ((ask + bid) / 2) * Decimal(10_000))
    return out


def fetch_alpaca_corporate_actions(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbols: Sequence[str],
    *,
    start: date,
    end: date,
    instrument_ids: dict[str, str],
) -> tuple[list[SplitEvent], list[SymbolChange]]:
    splits: list[SplitEvent] = []
    changes: list[SymbolChange] = []
    token: str | None = None
    while True:
        params = {
            "symbols": ",".join(symbols),
            "types": "forward_split,reverse_split,name_change",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": "1000",
        }
        if token:
            params["page_token"] = token
        payload = transport.get_json(
            f"{ALPACA_DATA}/v1/corporate-actions", params, credentials.headers()
        )
        actions: dict[str, Any] = (payload or {}).get("corporate_actions") or {}
        for kind in ("forward_splits", "reverse_splits"):
            for row in actions.get(kind, []):
                symbol = str(row["symbol"])
                splits.append(
                    SplitEvent(
                        instrument_id=instrument_ids.get(symbol, symbol),
                        ex_date=date.fromisoformat(str(row["ex_date"])),
                        new_rate=_num(row["new_rate"]),
                        old_rate=_num(row["old_rate"]),
                        source=f"alpaca:{kind}",
                    )
                )
        for row in actions.get("name_changes", []):
            old = str(row["old_symbol"])
            changes.append(
                SymbolChange(
                    instrument_id=instrument_ids.get(
                        old, instrument_ids.get(str(row["new_symbol"]), old)
                    ),
                    effective_date=date.fromisoformat(str(row["process_date"])),
                    old_symbol=old,
                    new_symbol=str(row["new_symbol"]),
                    source="alpaca:name_changes",
                )
            )
        token = (payload or {}).get("next_page_token")
        if not token:
            return sorted(splits, key=lambda s: (s.ex_date, s.instrument_id)), changes
