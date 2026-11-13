"""An in-memory stand-in for the Alpaca paper Trading API (no network, deterministic).

Implements :class:`trader.brokers.alpaca.transport.AlpacaTransport` with the JSON shapes of the
Alpaca documentation (numbers as strings, RFC 3339 timestamps). Order ids are UUIDs derived from
a counter; ``updated_at`` advances by at least one microsecond per change even under a frozen
clock. The stream's ``execution_id`` is deliberately different from the REST activity ``id``.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.brokers.alpaca.mapping import JsonObject, parse_timestamp
from trader.brokers.alpaca.transport import DefiniteRejection, NotFound, TradeHandler
from trader.brokers.base import BrokerTimeout
from trader.ops.clock import ReplayClock

PAPER_KEY = "PKFAKEKEYID0000000001"
PAPER_SECRET = "fake-paper-secret-0123456789abcdefghij"
_NS = uuid.UUID("6f1f3d2e-8f7c-4c11-9a52-2a1b8f0e7d10")
#: Statuses the fake files under ``status=closed`` (Alpaca does not document its grouping).
CLOSED_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})


def ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ts_nanos(value: datetime, extra: str = "417") -> str:
    """The same instant with nanosecond digits, as the stream sometimes reports it."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f") + extra + "Z"


class FakeSession:
    """One fake ``trade_updates`` session: ``run`` blocks until ``stop`` or ``disconnect``."""

    def __init__(self, fake: FakeAlpaca, on_connected: Callable[[], None]) -> None:
        self.fake = fake
        self.on_connected = on_connected
        self.handler: TradeHandler | None = None
        self.running = threading.Event()
        self.ended = threading.Event()

    def subscribe_trade_updates(self, handler: TradeHandler) -> None:
        self.handler = handler

    def run(self) -> None:
        self.on_connected()
        with self.fake._lock:
            self.running.set()
            self.fake._flush(self)
        self.fake.stream_ready.set()
        self.ended.wait(30)
        self.running.clear()

    def stop(self) -> None:
        self.ended.set()

    def deliver(self, message: Mapping[str, Any]) -> None:
        assert self.handler is not None
        asyncio.run(self.handler(dict(message)))


class FakeAlpaca:
    def __init__(self, clock: ReplayClock) -> None:
        self.clock = clock
        self.orders: dict[str, dict[str, Any]] = {}
        self.activities: list[dict[str, Any]] = []
        #: FEE activities (``charge_fee``): fees Alpaca books on its own the night after a trade.
        self.fee_activities: list[dict[str, Any]] = []
        self.positions: dict[str, tuple[Decimal, Decimal]] = {}
        self.cash = Decimal("100000")
        self.account_overrides: dict[str, Any] = {}
        self.assets: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.closed_statuses = CLOSED_STATUSES
        #: One-shot fault for the next submit: timeout_before | timeout_after | reject_403 |
        #: response_status:<alpaca status>.
        self.submit_fault: str | None = None
        #: Faults per method name: exception raised by the next call(s).
        self.fail_next: dict[str, list[BaseException]] = {}
        #: Activities withheld from REST (simulates the activity feed lagging the stream).
        self.hidden_activities: set[str] = set()
        #: One-shot actions per method name, run just before that method next answers
        #: (simulates the account changing between two REST reads, e.g. a fill landing).
        self.before_next: dict[str, Callable[[], None]] = {}
        #: How far the broker's clock (``get_clock``) is ahead of the injected clock.
        self.clock_ahead = timedelta(0)
        self.sessions: list[FakeSession] = []
        self.stream_ready = threading.Event()
        self._buffer: list[dict[str, Any]] = []
        self._seq = 0
        self._last: datetime | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        """Strictly increasing timestamps, at least the clock's time."""
        now = self.clock.now()
        if self._last is not None and now <= self._last:
            now = self._last + timedelta(microseconds=1)
        self._last = now
        return now

    def _uuid(self, kind: str) -> str:
        self._seq += 1
        return str(uuid.uuid5(_NS, f"{kind}-{self._seq}"))

    def _record(self, name: str) -> None:
        self.calls.append(name)
        queued = self.fail_next.get(name)
        if queued:
            raise queued.pop(0)
        action = self.before_next.pop(name, None)
        if action is not None:
            action()

    def _push(self, event: str, order: dict[str, Any], **extra: Any) -> None:
        message = {
            "stream": "trade_updates",
            "data": {"event": event, "order": dict(order), **extra},
        }
        with self._lock:
            session = self.sessions[-1] if self.sessions else None
            if session is not None and session.running.is_set() and session.handler is not None:
                session.deliver(message)
            else:
                self._buffer.append(message)

    def _flush(self, session: FakeSession) -> None:
        buffered, self._buffer = self._buffer, []
        for message in buffered:
            session.deliver(message)

    def push_raw(self, message: Mapping[str, Any]) -> None:
        session = self.sessions[-1]
        session.deliver(message)

    def _update(self, boid: str, **changes: Any) -> dict[str, Any]:
        order = self.orders[boid]
        order.update(changes)
        order["updated_at"] = ts(self._now())
        return order

    # ------------------------------------------------------------------ simulation
    def fill(self, boid: str, quantity: Decimal, price: Decimal, *, stream: bool = True) -> str:
        order = self.orders[boid]
        filled = Decimal(order["filled_qty"]) + quantity
        total = Decimal(order["qty"])
        assert filled <= total
        status = "filled" if filled == total else "partially_filled"
        at = self._now()
        order.update(
            filled_qty=str(filled),
            filled_avg_price=str(price),
            status=status,
            updated_at=ts(at),
        )
        if status == "filled":
            order["filled_at"] = ts(at)
        activity_id = f"{at:%Y%m%d%H%M%S}{at.microsecond // 1000:03d}::{self._uuid('activity')}"
        side = order["side"]
        self.activities.append(
            {
                "id": activity_id,
                "activity_type": "FILL",
                "transaction_time": ts(at),
                "type": "fill" if status == "filled" else "partial_fill",
                "price": str(price),
                "qty": str(quantity),
                "side": side,
                "symbol": order["symbol"],
                "leaves_qty": str(total - filled),
                "order_id": boid,
                "cum_qty": str(filled),
                "order_status": status,
            }
        )
        held, avg = self.positions.get(order["symbol"], (Decimal(0), Decimal(0)))
        if side == "buy":
            new = held + quantity
            avg = (held * avg + quantity * price) / new
            self.cash -= quantity * price
        else:
            new = held - quantity
            self.cash += quantity * price
        self.positions[order["symbol"]] = (new, avg)
        if new == 0:
            del self.positions[order["symbol"]]
        if stream:
            self._push(
                "fill" if status == "filled" else "partial_fill",
                order,
                execution_id=self._uuid("stream-execution"),
                timestamp=ts_nanos(at),
                price=str(price),
                qty=str(quantity),
                position_qty=str(new),
            )
        return activity_id

    def confirm_cancel(self, boid: str) -> None:
        order = self._update(boid, status="canceled")
        order["canceled_at"] = order["updated_at"]
        self._push("canceled", order, timestamp=order["updated_at"])

    def set_status(self, boid: str, status: str, *, stream: bool = True) -> None:
        order = self._update(boid, status=status)
        if stream:
            self._push(status, order)

    def set_position(self, symbol: str, quantity: Decimal, price: Decimal) -> None:
        self.positions[symbol] = (quantity, price)

    def add_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: str,
        status: str,
        client_order_id: str,
        at: datetime,
        order_type: str = "market",
    ) -> str:
        boid = self._uuid("order")
        stamp = ts(at)
        self.orders[boid] = {
            "id": boid,
            "client_order_id": client_order_id,
            "created_at": stamp,
            "updated_at": stamp,
            "submitted_at": stamp,
            "symbol": symbol,
            "asset_class": "us_equity",
            "qty": qty,
            "notional": None,
            "filled_qty": qty if status == "filled" else "0",
            "filled_avg_price": None,
            "order_type": order_type,
            "type": order_type,
            "side": side,
            "time_in_force": "day",
            "limit_price": None,
            "status": status,
            "extended_hours": False,
        }
        return boid

    # ------------------------------------------------------------------ transport protocol
    def get_account(self) -> JsonObject:
        self._record("get_account")
        account: dict[str, Any] = {
            "id": "11111111-2222-3333-4444-555555555555",
            "account_number": "PA0000FAKE",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": str(self.cash),
            "buying_power": str(self.cash * 2),
            "non_marginable_buying_power": str(self.cash),
            "equity": str(self.cash),
            "trading_blocked": False,
            "transfers_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
            "pattern_day_trader": False,
            "multiplier": "2",
        }
        account.update(self.account_overrides)
        return account

    def list_positions(self) -> list[JsonObject]:
        self._record("list_positions")
        return [
            {
                "symbol": symbol,
                "qty": str(qty),
                "avg_entry_price": str(avg),
                "side": "long" if qty > 0 else "short",
                "asset_class": "us_equity",
            }
            for symbol, (qty, avg) in sorted(self.positions.items())
        ]

    def list_orders(self, *, status: str, after: str | None, limit: int) -> list[JsonObject]:
        self._record("list_orders")
        rows = list(self.orders.values())
        if status == "open":
            rows = [r for r in rows if r["status"] not in self.closed_statuses]
        elif status == "closed":
            rows = [r for r in rows if r["status"] in self.closed_statuses]
        if after is not None:
            bound = parse_timestamp(after, "after")
            rows = [r for r in rows if parse_timestamp(r["submitted_at"], "s") > bound]
        rows.sort(key=lambda r: parse_timestamp(r["submitted_at"], "s"))
        return [dict(r) for r in rows[:limit]]

    def get_order(self, order_id: str) -> JsonObject:
        self._record("get_order")
        if order_id not in self.orders:
            raise NotFound(http_status=404, code="40410000", message="order not found")
        return dict(self.orders[order_id])

    def get_order_by_client_id(self, client_order_id: str) -> JsonObject | None:
        self._record("get_order_by_client_id")
        for order in self.orders.values():
            if order["client_order_id"] == client_order_id:
                return dict(order)
        return None

    def submit_order(self, payload: Mapping[str, str]) -> JsonObject:
        self._record("submit_order")
        fault, self.submit_fault = self.submit_fault, None
        if fault == "timeout_before":
            raise BrokerTimeout("fake: timed out before Alpaca received the order")
        if fault == "reject_403":
            raise DefiniteRejection(
                http_status=403, code="40310000", message="insufficient buying power"
            )
        if any(o["client_order_id"] == payload["client_order_id"] for o in self.orders.values()):
            raise DefiniteRejection(
                http_status=422, code="40010001", message="client_order_id must be unique"
            )
        boid = self._uuid("order")
        stamp = ts(self._now())
        order = {
            "id": boid,
            "client_order_id": payload["client_order_id"],
            "created_at": stamp,
            "updated_at": stamp,
            "submitted_at": stamp,
            "filled_at": None,
            "canceled_at": None,
            "symbol": payload["symbol"],
            "asset_class": "us_equity",
            "qty": payload["qty"],
            "notional": None,
            "filled_qty": "0",
            "filled_avg_price": None,
            "order_class": "",
            "order_type": payload["type"],
            "type": payload["type"],
            "side": payload["side"],
            "time_in_force": payload["time_in_force"],
            "limit_price": payload.get("limit_price"),
            "stop_price": None,
            "status": "accepted",
            "extended_hours": False,
        }
        self.orders[boid] = order
        response = dict(order)
        if fault is not None and fault.startswith("response_status:"):
            response["status"] = fault.split(":", 1)[1]
        new = self._update(boid, status="new")
        self._push("new", new)
        if fault == "timeout_after":
            raise BrokerTimeout("fake: timed out after Alpaca accepted the order")
        return response

    def cancel_order(self, order_id: str) -> None:
        self._record("cancel_order")
        order = self.orders.get(order_id)
        if order is None:
            raise NotFound(http_status=404, code="40410000", message="order not found")
        if order["status"] in self.closed_statuses:
            raise DefiniteRejection(
                http_status=422, code="42210000", message="order is not cancelable"
            )
        self._push("pending_cancel", self._update(order_id, status="pending_cancel"))

    def list_fill_activities(
        self,
        *,
        order_id: str | None,
        direction: str,
        page_token: str | None,
        page_size: int,
    ) -> list[JsonObject]:
        """Activity ids start with the transaction time, so id order is time order."""
        self._record("list_fill_activities")
        rows = [a for a in self.activities if a["id"] not in self.hidden_activities]
        if order_id is not None:
            rows = [a for a in rows if a["order_id"] == order_id]
        newest_first = direction == "desc"
        rows.sort(key=lambda a: a["id"], reverse=newest_first)
        if page_token is not None:
            rows = [
                a for a in rows if (a["id"] < page_token if newest_first else a["id"] > page_token)
            ]
        return [dict(a) for a in rows[:page_size]]

    def list_fee_activities(
        self,
        *,
        direction: str,
        page_token: str | None,
        page_size: int,
    ) -> list[JsonObject]:
        """Fee activity ids start with the trade date, so id order is trade-date order."""
        self._record("list_fee_activities")
        newest_first = direction == "desc"
        rows = sorted(self.fee_activities, key=lambda a: a["id"], reverse=newest_first)
        if page_token is not None:
            rows = [
                a for a in rows if (a["id"] < page_token if newest_first else a["id"] > page_token)
            ]
        return [dict(a) for a in rows[:page_size]]

    def charge_fee(
        self,
        amount: Decimal,
        *,
        trade_date: str,
        created_at: datetime,
        sub_type: str = "REG",
        status: str = "executed",
    ) -> dict[str, Any]:
        """Book a fee the way Alpaca does: an activity, and the cash falls by ``amount``."""
        activity = {
            "id": f"{trade_date.replace('-', '')}000000000::{self._uuid('fee')}",
            "activity_type": "FEE",
            "activity_sub_type": sub_type,
            "date": trade_date,
            "created_at": ts(created_at),
            "net_amount": str(-amount),
            "description": f"{sub_type} fee on {trade_date}",
            "status": status,
            "currency": "USD",
        }
        self.fee_activities.append(activity)
        if status == "executed":
            self.cash -= amount
        return activity

    def get_clock(self) -> JsonObject:
        self._record("get_clock")
        now = self.clock.now() + self.clock_ahead
        return {
            "timestamp": ts(now),
            "is_open": True,
            "next_open": ts(now + timedelta(days=1)),
            "next_close": ts(now + timedelta(hours=2)),
        }

    def get_asset(self, symbol: str) -> JsonObject:
        self._record("get_asset")
        if symbol not in self.assets:
            raise NotFound(http_status=404, code="40410000", message="asset not found")
        return dict(self.assets[symbol])

    def open_trade_stream(self, on_connected: Callable[[], None]) -> FakeSession:
        self._record("open_trade_stream")
        session = FakeSession(self, on_connected)
        self.sessions.append(session)
        return session
