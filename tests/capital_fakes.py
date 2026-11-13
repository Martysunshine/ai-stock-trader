"""A fake Capital.com demo broker for the practice trader's tests (ADR-0056).

Its answers have the shapes the demo host gave on 2026-11-11 to ``python -m trader.practice
certify`` (``runs/practice/certification/20261111T164834Z.json``): numbers as exact decimals,
UTC times without a zone, a position's deal id under ``affectedDeals`` with status OPENED.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.config.secrets import CapitalDemoCredentials
from trader.data.sources.capital_prices import CAPITAL_DEMO_HOST, CapitalPrices
from trader.data.sources.http import TransportError, check_order_url
from trader.practice.capital_orders import CapitalOrders

API = f"{CAPITAL_DEMO_HOST}/api/v1"
CREDS = CapitalDemoCredentials("demo-key", "owner@mailbox.test", "pw")

#: The certification's answers, as recorded (account identifiers were never in them).
OPEN_CONFIRM = {
    "date": "2026-11-11T16:48:35.149",
    "status": "OPEN",
    "dealStatus": "ACCEPTED",
    "epic": "BTCUSD",
    "dealReference": "o_3b8fdf63-727d-4155-ad8f-33a98310d6fc",
    "dealId": "00000000-7281-eda5-049d-be3a0055311e",
    "affectedDeals": [{"dealId": "00000000-7281-eda8-049d-be3a0055311e", "status": "OPENED"}],
    "level": Decimal("74183.9"),
    "size": Decimal("0.00010"),
    "direction": "BUY",
    "guaranteedStop": False,
    "trailingStop": False,
}
CLOSE_CONFIRM = {
    "date": "2026-11-11T16:48:35.846",
    "status": "CLOSED",
    "dealStatus": "ACCEPTED",
    "epic": "BTCUSD",
    "dealReference": "p_00000000-7281-eda8-049d-be3a0055311e",
    "dealId": "00000000-7281-eda8-049d-be3a0055311e",
    "affectedDeals": [{"dealId": "00000000-7281-eda8-049d-be3a0055311e", "status": "FULLY_CLOSED"}],
    "level": Decimal("74133.9"),
    "size": Decimal("0.00010"),
    "direction": "SELL",
    "stopLevel": Decimal("71958.14"),
    "profitLevel": Decimal("75667.33"),
    "guaranteedStop": False,
    "trailingStop": False,
    "profit": Decimal("-0.005"),
    "profitCurrency": "USD",
}
POSITION = {
    "position": {
        "contractSize": 1,
        "createdDate": "2026-11-11T17:48:35.149",
        "createdDateUTC": "2026-11-11T16:48:35.149",
        "dealId": "00000000-7281-eda8-049d-be3a0055311e",
        "dealReference": "p_00000000-7281-eda8-049d-be3a0055311e",
        "workingOrderId": "00000000-7281-eda5-049d-be3a0055311e",
        "size": Decimal("0.00010"),
        "leverage": 2,
        "upl": Decimal("-0.005"),
        "direction": "BUY",
        "level": Decimal("74183.9"),
        "currency": "USD",
        "guaranteedStop": False,
        "stopLevel": Decimal("71958.14"),
        "profitLevel": Decimal("75667.33"),
    },
    "market": {"epic": "BTCUSD", "bid": Decimal("74133.65"), "offer": Decimal("74183.65")},
}
MARKET = {
    "instrument": {"epic": "BTCUSD", "currency": "USD"},
    "dealingRules": {
        "minDealSize": {"unit": "POINTS", "value": Decimal("0.0001")},
        "minSizeIncrement": {"unit": "POINTS", "value": Decimal("0.0001")},
        "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": Decimal("0.01")},
    },
    "snapshot": {
        "marketStatus": "TRADEABLE",
        "bid": Decimal("74133.65"),
        "offer": Decimal("74183.65"),
        "updateTime": "2026-11-11T17:48:34.746",
    },
}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000")


class FakeBroker:
    """A demo account in memory. ``quotes`` sets each epic's bid and offer, ``status`` its
    market status. Scripted faults: ``lose_confirms`` (the broker never confirms), ``reject``
    (the next order is rejected), ``fail_send`` (the next order call raises a network error)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.hedging = False
        self.quotes: dict[str, tuple[Decimal, Decimal]] = {
            "BTCUSD": (Decimal("74133.65"), Decimal("74183.65")),
            "ETHUSD": (Decimal("2049.00"), Decimal("2050.80")),
        }
        self.status: dict[str, str] = {}
        self.min_size: dict[str, Decimal] = {
            "BTCUSD": Decimal("0.0001"),
            "ETHUSD": Decimal("0.001"),
        }
        self.positions: dict[str, dict[str, Any]] = {}
        self.confirms: dict[str, dict[str, Any]] = {}
        self.activities: list[dict[str, Any]] = []
        self.bars: dict[str, list[dict[str, Any]]] = {}
        self.sent: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.lose_confirms = False
        self.reject = False
        self.fail_send = False
        self.requests = 0
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ transport protocol
    def open_session(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> tuple[Any, dict[str, str]]:
        return {}, {"cst": "c", "x-security-token": "t"}

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.requests += 1
        path = url.removeprefix(API)
        if path == "/accounts/preferences":
            return {"hedgingMode": self.hedging, "leverages": {}}
        if path.startswith("/markets/"):
            epic = path.split("/")[-1]
            bid, offer = self.quotes[epic]
            return {
                **MARKET,
                "instrument": {"epic": epic},
                "dealingRules": {
                    "minDealSize": {"value": self.min_size.get(epic, Decimal("0.01"))}
                },
                "snapshot": {
                    "marketStatus": self.status.get(epic, "TRADEABLE"),
                    "bid": bid,
                    "offer": offer,
                },
            }
        if path == "/positions":
            return {"positions": [dict(p) for p in self.positions.values()]}
        if path.startswith("/confirms/"):
            ref = path.split("/")[-1]
            if ref not in self.confirms or self.lose_confirms:
                raise TransportError("HTTP 404", status=404)
            return self.confirms[ref]
        if path == "/history/activity":
            return {"activities": list(reversed(self.activities))}
        if path.startswith("/prices/"):
            epic = path.split("/")[-1]
            rows = self.bars.get(epic, [])
            if params and params.get("to", "") > self.clock.now().strftime("%Y-%m-%dT%H:%M:%S"):
                # the demo host refuses a page that ends in the future (seen 2026-11-11)
                raise TransportError(f"HTTP 400 from {url}", status=400)
            if params and "to" in params:
                rows = [r for r in rows if r["snapshotTimeUTC"] <= params["to"]]
            count = int(params["max"]) if params and "max" in params else len(rows)
            return {"prices": rows[-count:] if count else []}
        raise TransportError(f"HTTP 404 from {url}", status=404)

    def send_json(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        check_order_url(method, url)
        self.sent.append((method, url, body))
        if self.fail_send:
            self.fail_send = False
            raise TransportError(f"network error for {method} {url}: timed out")
        n = next(self._ids)
        if method == "POST":
            assert body is not None
            ref = f"o_ref{n}"
            if self.reject:
                self.reject = False
                self.confirms[ref] = {
                    "dealReference": ref,
                    "dealStatus": "REJECTED",
                    "status": "REJECTED",
                    "reason": "RISK_CHECK",
                    "epic": body["epic"],
                }
                return {"dealReference": ref}
            epic, direction, size = body["epic"], body["direction"], Decimal(body["size"])
            bid, offer = self.quotes[epic]
            level = offer if direction == "BUY" else bid
            deal = f"deal-{n}"
            self.positions[deal] = {
                "position": {
                    "dealId": deal,
                    "dealReference": f"p_{deal}",
                    "direction": direction,
                    "size": size,
                    "level": level,
                    "stopLevel": Decimal(body["stopLevel"]),
                    "profitLevel": (
                        Decimal(body["profitLevel"]) if "profitLevel" in body else None
                    ),
                    "createdDateUTC": _stamp(self.clock.now()),
                    "currency": "USD",
                },
                "market": {"epic": epic, "bid": bid, "offer": offer},
            }
            self.confirms[ref] = {
                "date": _stamp(self.clock.now()),
                "status": "OPEN",
                "dealStatus": "ACCEPTED",
                "epic": epic,
                "dealReference": ref,
                "dealId": f"wo-{n}",
                "affectedDeals": [{"dealId": deal, "status": "OPENED"}],
                "level": level,
                "size": size,
                "direction": direction,
            }
            return {"dealReference": ref}
        deal = url.rsplit("/", 1)[-1]
        if deal not in self.positions:
            raise TransportError(f"HTTP 404 from {method} {url}", status=404)
        return {"dealReference": self.close(deal, "USER")}

    # ------------------------------------------------------------------ the broker's own moves
    def close(self, deal: str, source: str, level: Decimal | None = None) -> str:
        """Close a position: by the user (``USER``), at its stop (``SL``) or target (``TP``)."""
        row = self.positions.pop(deal)
        pos, epic = row["position"], row["market"]["epic"]
        bid, offer = self.quotes[epic]
        if level is None:
            level = bid if pos["direction"] == "BUY" else offer
        side = 1 if pos["direction"] == "BUY" else -1
        profit = (level - pos["level"]) * pos["size"] * side
        ref = f"p_{deal}"
        closing = "SELL" if pos["direction"] == "BUY" else "BUY"
        self.confirms[ref] = {
            "date": _stamp(self.clock.now()),
            "status": "CLOSED",
            "dealStatus": "ACCEPTED",
            "epic": epic,
            "dealReference": ref,
            "dealId": deal,
            "affectedDeals": [{"dealId": deal, "status": "FULLY_CLOSED"}],
            "level": level,
            "size": pos["size"],
            "direction": closing,
            "profit": profit,
            "profitCurrency": "USD",
        }
        self.activities.append(
            {
                "dateUTC": _stamp(self.clock.now()),
                "epic": epic,
                "dealId": deal,
                "source": source,
                "type": "POSITION",
                "status": "ACCEPTED",
                "details": {
                    "dealReference": ref,
                    "size": pos["size"],
                    "direction": closing,
                    "level": level,
                    "openPrice": pos["level"],
                    "currency": "USD",
                },
            }
        )
        return ref


def orders_for(broker: FakeBroker) -> CapitalOrders:
    prices = CapitalPrices(broker, lambda: CREDS, clock=broker.clock, sleep=lambda _: None)
    return CapitalOrders(prices, broker)
