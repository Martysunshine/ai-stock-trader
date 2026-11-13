"""Capital.com DEMO orders for the practice trader (ADR-0056).

The trader's only way to the broker. It shares the price reader's logged-in session
(:meth:`trader.data.sources.capital_prices.CapitalPrices.call`), so the demo host stays the one
hard-coded in that module and every address here is built from it. Reads use the ordinary
paced JSON transport; the two writes go through ``send_json``, which accepts only opening a
position and closing one, sends each once and never retries.

What the broker answers is kept as it came (exact decimals): ``tests/test_capital_orders.py``
pins the shapes, recorded from the demo host by ``python -m trader.practice certify``.

* ``open_position``: ``POST /api/v1/positions`` with the epic, side, size, and a stop and target
  held by the broker; answers ``{"dealReference": ...}``.
* ``confirm``: ``GET /api/v1/confirms/{dealReference}``: ``dealStatus`` ACCEPTED or REJECTED,
  the fill ``level``, and the position's deal id under ``affectedDeals``.
* ``positions``: ``GET /api/v1/positions``, each ``{"position": {...}, "market": {...}}``.
* ``close_position``: ``DELETE /api/v1/positions/{dealId}``.
* ``market``: ``GET /api/v1/markets/{epic}``: dealing rules and the live quote and status.
* ``preferences``: ``GET /api/v1/accounts/preferences``: ``hedgingMode``.
* ``activity``: ``GET /api/v1/history/activity``: how and at what level a position ended.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from trader.data.sources.capital_prices import CAPITAL_DEMO_HOST, CapitalPrices
from trader.data.sources.http import POSITIONS_PATH, OrderTransport
from trader.domain.types import require_utc

API = f"{CAPITAL_DEMO_HOST}/api/v1"
POSITIONS_URL = CAPITAL_DEMO_HOST + POSITIONS_PATH
_EPIC = re.compile(r"^[A-Z0-9_.]{1,40}$")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class OrderError(RuntimeError):
    """The broker's answer cannot be used as it is."""


def _dec(value: object, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise OrderError(f"{what}: unexpected value {value!r}")
    return Decimal(value)


def _stamp(ts: datetime) -> str:
    return require_utc(ts).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True, slots=True)
class Confirmation:
    deal_reference: str
    accepted: bool
    #: The open position's deal id (``affectedDeals`` with status OPENED), when accepted.
    deal_id: str | None
    level: Decimal | None
    size: Decimal | None
    reason: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Position:
    deal_id: str
    deal_reference: str | None
    epic: str
    direction: Literal["BUY", "SELL"]
    size: Decimal
    level: Decimal
    stop: Decimal | None
    target: Decimal | None
    created_utc: str | None


@dataclass(frozen=True, slots=True)
class Quote:
    epic: str
    bid: Decimal
    offer: Decimal
    status: str
    min_size: Decimal | None
    raw: Mapping[str, Any]

    @property
    def tradeable(self) -> bool:
        return self.status == "TRADEABLE"

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.bid + self.offer) / 2
        return (self.offer - self.bid) / mid * 10_000


def parse_confirmation(payload: Any) -> Confirmation:
    if not isinstance(payload, dict) or not isinstance(payload.get("dealReference"), str):
        raise OrderError(f"unexpected confirmation {str(payload)[:200]}")
    accepted = payload.get("dealStatus") == "ACCEPTED"
    deal_id = None
    for deal in payload.get("affectedDeals") or []:
        if isinstance(deal, dict) and deal.get("status") == "OPENED":
            deal_id = str(deal.get("dealId"))
    if deal_id is None and accepted and payload.get("status") == "OPEN":
        deal_id = str(payload.get("dealId")) if payload.get("dealId") else None
    level = payload.get("level")
    size = payload.get("size")
    return Confirmation(
        deal_reference=payload["dealReference"],
        accepted=accepted,
        deal_id=deal_id,
        level=None if level is None else _dec(level, "confirm level"),
        size=None if size is None else _dec(size, "confirm size"),
        reason=str(payload.get("reason") or payload.get("status") or ""),
        raw=payload,
    )


def parse_positions(payload: Any) -> list[Position]:
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        raise OrderError(f"unexpected positions answer {str(payload)[:200]}")
    out: list[Position] = []
    for row in payload["positions"]:
        pos, market = row.get("position") or {}, row.get("market") or {}
        direction = pos.get("direction")
        if direction not in ("BUY", "SELL") or not pos.get("dealId"):
            raise OrderError(f"unexpected position {str(row)[:200]}")
        out.append(
            Position(
                deal_id=str(pos["dealId"]),
                deal_reference=pos.get("dealReference"),
                epic=str(market.get("epic") or pos.get("epic") or ""),
                direction=direction,
                size=_dec(pos.get("size"), "position size"),
                level=_dec(pos.get("level"), "position level"),
                stop=None if pos.get("stopLevel") is None else _dec(pos["stopLevel"], "stop"),
                target=(
                    None if pos.get("profitLevel") is None else _dec(pos["profitLevel"], "target")
                ),
                created_utc=pos.get("createdDateUTC"),
            )
        )
    return out


def parse_quote(epic: str, payload: Any) -> Quote:
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise OrderError(f"unexpected market answer for {epic}: {str(payload)[:200]}")
    snap = payload["snapshot"]
    rules = payload.get("dealingRules") or {}
    minimum = (rules.get("minDealSize") or {}).get("value")
    return Quote(
        epic=epic,
        bid=_dec(snap.get("bid"), f"{epic} bid"),
        offer=_dec(snap.get("offer"), f"{epic} offer"),
        status=str(snap.get("marketStatus") or ""),
        min_size=None if minimum is None else _dec(minimum, f"{epic} minimum size"),
        raw=payload,
    )


class CapitalOrders:
    """Demo orders and the reads that go with them, on the price reader's session."""

    def __init__(self, prices: CapitalPrices, transport: OrderTransport) -> None:
        self._prices = prices
        self._transport = transport

    def _get(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        return self._prices.call(
            lambda headers: self._transport.get_json(url, params, headers), "broker read"
        )

    # ------------------------------------------------------------------ reads
    def preferences(self) -> Mapping[str, Any]:
        answer = self._get(f"{API}/accounts/preferences")
        if not isinstance(answer, dict):
            raise OrderError("unexpected preferences answer")
        return answer

    def hedging(self) -> bool:
        return self.preferences().get("hedgingMode") is True

    def market(self, epic: str) -> Quote:
        if not _EPIC.fullmatch(epic):
            raise ValueError(f"not a Capital.com epic: {epic!r}")
        return parse_quote(epic, self._get(f"{API}/markets/{epic}"))

    def positions(self) -> list[Position]:
        return parse_positions(self._get(POSITIONS_URL))

    def confirm(self, deal_reference: str) -> Confirmation:
        if not _REF.fullmatch(deal_reference):
            raise ValueError(f"not a deal reference: {deal_reference!r}")
        return parse_confirmation(self._get(f"{API}/confirms/{deal_reference}"))

    def activity(self, since: datetime, until: datetime) -> list[Mapping[str, Any]]:
        params = {"from": _stamp(since), "to": _stamp(until), "detailed": "true"}
        answer = self._get(f"{API}/history/activity", params)
        rows = answer.get("activities") if isinstance(answer, dict) else None
        if not isinstance(rows, list):
            raise OrderError("unexpected activity answer")
        return rows

    # ------------------------------------------------------------------ the two writes
    def open_position(
        self,
        epic: str,
        direction: Literal["BUY", "SELL"],
        size: Decimal,
        *,
        stop: Decimal,
        target: Decimal | None,
    ) -> str:
        """Send one market order with its stop (and target) held at the broker; return its deal
        reference. Sent once: a lost answer is looked up by the caller, never re-sent."""
        if not _EPIC.fullmatch(epic) or direction not in ("BUY", "SELL") or size <= 0:
            raise ValueError("an order needs an epic, a side and a positive size")
        body: dict[str, Any] = {
            "epic": epic,
            "direction": direction,
            "size": str(size),
            "guaranteedStop": False,
            "trailingStop": False,
            "stopLevel": str(stop),
        }
        if target is not None:
            body["profitLevel"] = str(target)
        answer = self._prices.call(
            lambda headers: self._transport.send_json("POST", POSITIONS_URL, body, headers),
            "order",
        )
        ref = answer.get("dealReference") if isinstance(answer, dict) else None
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise OrderError(f"order answered without a deal reference: {str(answer)[:200]}")
        return ref

    def close_position(self, deal_id: str) -> str:
        """Close one position at the market; return the closing deal reference."""
        if not _REF.fullmatch(deal_id):
            raise ValueError(f"not a deal id: {deal_id!r}")
        url = f"{POSITIONS_URL}/{deal_id}"
        answer = self._prices.call(
            lambda headers: self._transport.send_json("DELETE", url, None, headers), "close"
        )
        ref = answer.get("dealReference") if isinstance(answer, dict) else None
        if not isinstance(ref, str):
            raise OrderError(f"close answered without a deal reference: {str(answer)[:200]}")
        return ref
