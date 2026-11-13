"""Alpaca news requests for the intel feed (ADR-0047).

``GET /v1beta1/news`` pages for a set of symbols. Only the headline, the short summary and the
metadata are kept: ``include_content=false`` is always sent and any ``content`` or ``images``
field in an answer is dropped here, so an article body never reaches the store or a prompt.

Everything in an item is untrusted text from an external publisher. This module only checks
shapes and types; cleaning and length caps happen where the text is stored (``trader.intel``).

Keys travel only in request headers (``AlpacaCredentials.headers``) and never appear in errors.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.config.alpaca_keys import AlpacaCredentials
from trader.data.history import DataQualityError
from trader.data.sources.alpaca_bars import ALPACA_DATA, alpaca_time
from trader.data.sources.http import JsonTransport
from trader.domain.types import iso_utc

ALPACA_NEWS = f"{ALPACA_DATA}/v1beta1/news"
#: Alpaca's largest page for this endpoint.
NEWS_PAGE_LIMIT = 50


@dataclass(frozen=True, slots=True)
class NewsItem:
    """One news item as the source sent it (text not yet cleaned)."""

    source_id: str
    headline: str
    summary: str
    author: str
    source: str
    url: str
    symbols: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DataQualityError(f"unexpected Alpaca news field {key}")
    return value


def parse_news_item(row: object) -> NewsItem:
    if not isinstance(row, dict):
        raise DataQualityError("unexpected Alpaca news row")
    raw_id = row.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or str(raw_id) == "":
        raise DataQualityError("Alpaca news row without an id")
    symbols = row.get("symbols") or []
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise DataQualityError("unexpected Alpaca news symbols")
    created = alpaca_time(row.get("created_at"))
    updated = alpaca_time(row.get("updated_at")) if row.get("updated_at") else created
    return NewsItem(
        source_id=str(raw_id),
        headline=_text(row, "headline"),
        summary=_text(row, "summary"),
        author=_text(row, "author"),
        source=_text(row, "source"),
        url=_text(row, "url"),
        symbols=tuple(symbols),
        created_at=created,
        updated_at=updated,
    )


def fetch_news(
    transport: JsonTransport,
    credentials: AlpacaCredentials,
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    limit: int = NEWS_PAGE_LIMIT,
    max_pages: int = 10,
) -> list[NewsItem]:
    """News for ``symbols`` between ``start`` and ``end``, oldest update first.

    At most ``max_pages`` pages are read. A longer backlog is simply cut off: because the order
    is oldest first, the caller continues from the last item's ``updated_at`` on its next run.
    """
    if not symbols:
        return []
    if not 1 <= limit <= NEWS_PAGE_LIMIT:
        raise ValueError(f"limit must be 1..{NEWS_PAGE_LIMIT}")
    items: list[NewsItem] = []
    token: str | None = None
    for _ in range(max_pages):
        params = {
            "symbols": ",".join(symbols),
            "start": iso_utc(start),
            "end": iso_utc(end),
            "limit": str(limit),
            "sort": "asc",
            "include_content": "false",
        }
        if token:
            params["page_token"] = token
        payload = transport.get_json(ALPACA_NEWS, params, credentials.headers())
        if not isinstance(payload, dict) or not isinstance(payload.get("news") or [], list):
            raise DataQualityError("unexpected Alpaca news payload")
        items.extend(parse_news_item(row) for row in payload.get("news") or [])
        token = payload.get("next_page_token")
        if not token:
            break
    return items
