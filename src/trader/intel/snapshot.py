"""What was known about one instrument at a moment (ADR-0047).

``snapshot`` is a pure function of the stored records and a time. A record counts only if this
machine had already seen it (``first_seen_at <= at``), in the revision that was current then, so
the answer for a past moment does not change when the source later edits or adds an item.

The feed appends a snapshot whenever its content changes; the console shows the newest one taken
at or before a trade's decision time. The same function is the definition a later research
feature must use, so the numbers a person saw and the numbers a test uses cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trader.domain.hashing import content_hash
from trader.domain.types import canon_decimal, iso_utc, require_utc
from trader.intel.config import Target
from trader.intel.records import StoredRecord

SNAPSHOT_VERSION = "intel-snapshot-v1"
NEWS_SHORT = timedelta(hours=2)
NEWS_LONG = timedelta(hours=24)
LATEST_ITEMS = 3


def visible(records: Iterable[StoredRecord], at: datetime) -> list[StoredRecord]:
    """The revision of each record that was current at ``at``, oldest publication first."""
    current: dict[str, StoredRecord] = {}
    for row in records:
        if row.first_seen_at > at:
            continue
        held = current.get(row.id)
        if held is None or row.rev > held.rev:
            current[row.id] = row
    return sorted(current.values(), key=lambda r: (r.effective_at, r.id))


def _decimal(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    return number if number.is_finite() else Decimal(0)


def _item(row: StoredRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.record.title,
        "category": row.record.category,
        "at": iso_utc(row.effective_at),
        "url": row.record.url,
    }


def _news(rows: list[StoredRecord], target: Target, at: datetime) -> dict[str, Any] | None:
    if not target.news:
        return None
    mine = [r for r in rows if r.record.kind == "news" and target.id in r.record.targets]
    # A syndicated story (same headline) counts once: its first copy.
    first_copy: dict[str, StoredRecord] = {}
    for row in mine:
        first_copy.setdefault(row.headline_key or row.id, row)
    stories = sorted(first_copy.values(), key=lambda r: (r.effective_at, r.id))
    long_window = [r for r in stories if at - NEWS_LONG < r.effective_at <= at]
    categories: dict[str, int] = {}
    for row in long_window:
        categories[row.record.category] = categories.get(row.record.category, 0) + 1
    return {
        "count_2h": sum(1 for r in long_window if r.effective_at > at - NEWS_SHORT),
        "count_24h": len(long_window),
        "categories_24h": dict(sorted(categories.items())),
        "last_at": iso_utc(stories[-1].effective_at) if stories else None,
        "latest": [_item(r) for r in reversed(stories[-LATEST_ITEMS:])],
        "proxy": target.news_proxy,
    }


def _insiders(
    rows: list[StoredRecord], target: Target, at: datetime, window: timedelta
) -> dict[str, Any] | None:
    if target.cik is None:
        return None
    mine = [
        r
        for r in rows
        if r.record.kind == "insider"
        and target.id in r.record.targets
        and at - window < r.effective_at <= at
    ]
    buy_value = sum((_decimal(r.record.facts.get("buy_value")) for r in mine), Decimal(0))
    sell_value = sum((_decimal(r.record.facts.get("sell_value")) for r in mine), Decimal(0))
    return {
        "window_days": window.days,
        "filings": len(mine),
        "buys": sum(1 for r in mine if _decimal(r.record.facts.get("buy_shares")) > 0),
        "sells": sum(1 for r in mine if _decimal(r.record.facts.get("sell_shares")) > 0),
        "buy_value": canon_decimal(buy_value),
        "sell_value": canon_decimal(sell_value),
        "net_value": canon_decimal(buy_value - sell_value),
        "last_at": iso_utc(mine[-1].effective_at) if mine else None,
        "latest": [_item(r) for r in reversed(mine[-LATEST_ITEMS:])],
    }


def _premarket(rows: list[StoredRecord], target: Target) -> dict[str, Any] | None:
    if not target.premarket:
        return None
    mine = [r for r in rows if r.record.kind == "premarket" and target.id in r.record.targets]
    if not mine:
        return {"session": None}
    newest = max(mine, key=lambda r: (str(r.record.facts.get("session", "")), r.first_seen_at))
    facts = newest.record.facts
    keys = ("session", "prior_close", "last", "gap_pct", "volume", "high", "low", "bars", "through")
    return {key: facts.get(key) for key in keys}


def snapshot(
    records: Iterable[StoredRecord],
    target: Target,
    at: datetime,
    *,
    statuses: Mapping[str, str],
    insider_days: int = 90,
) -> dict[str, Any]:
    """What was known about ``target`` at ``at``. ``hash`` covers everything but ``as_of``."""
    at = require_utc(at)
    rows = visible(records, at)
    body: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "id": target.id,
        "news": _news(rows, target, at),
        "insiders": _insiders(rows, target, at, timedelta(days=insider_days)),
        "premarket": _premarket(rows, target),
        "sources": dict(sorted(statuses.items())),
    }
    return {**body, "as_of": iso_utc(at), "hash": content_hash(body)}
