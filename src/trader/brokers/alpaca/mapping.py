"""Alpaca JSON -> domain objects (architecture.md §3, §4, §7; ADR-0025).

Pure functions: no network, no clock reads. Every rule below fails closed with a
:class:`MappingError` (a :class:`~trader.brokers.base.BrokerError`) instead of guessing.

* **Numbers** are converted with ``Decimal(str(value))`` and refused unless they round-trip: a
  string must already be in plain decimal form (``"1.50"`` yes, ``"1e5"``, ``" 1"``, ``"NaN"``
  no), an int must compare equal, and a float (the SDK's JSON parser produces floats for JSON
  numbers; Alpaca documents these fields as strings) must be finite and equal to
  ``float(Decimal(str(value)))``. Booleans and ``None`` are never numbers.
* **Statuses** follow :data:`STATUS_RULES`, an explicit table over every Alpaca order status
  (documented or in the SDK's enum). Working statuses that our domain does not distinguish
  (``pending_new``, ``pending_review``, ``held``, ``done_for_day``, ...) map by cumulative
  quantity and are never terminal unless fully filled, so exposure stays reserved.
  ``replaced``, ``pending_replace``, ``suspended`` and any undocumented value raise
  :class:`UnmappedStatus`: the system never replaces orders, so these need an owner's review.
* **Versions**: ``broker_version`` is ``updated_at`` in integer epoch microseconds (nanosecond
  digits are truncated), so the REST and stream views of the same order state carry the same
  version and later states carry larger ones.
* **Execution ids**: a :class:`~trader.domain.orders.Fill` is built only from a REST ``FILL``
  account activity, whose ``id`` is the canonical execution id. The stream's ``execution_id`` is
  not documented to equal that id, so it is never used to post a fill.
* **Symbols** map to instrument ids through the configured specs; an unknown symbol becomes
  ``UNMAPPED:<symbol>`` so reconciliation reports it instead of the adapter dropping it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import ValidationError

from trader.brokers.base import BrokerError
from trader.domain.account import (
    AccountSnapshot,
    BrokerFeeCharge,
    BrokerPosition,
    LegalAccountType,
    RuleProfileKind,
)
from trader.domain.ids import MAX_CLIENT_ORDER_ID_LENGTH
from trader.domain.instruments import AnySpec
from trader.domain.orders import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderStatus,
    FeeStatus,
    Fill,
    OrderIntent,
    OrderType,
    TimeInForce,
)
from trader.domain.types import ZERO, DomainError, Side, canon_decimal

JsonObject = Mapping[str, object]

UNMAPPED_PREFIX = "UNMAPPED:"
#: Longest execution id accepted (Alpaca's are ~55 characters): ``broker_order_events.event_id``
#: is String(128) and carries it behind a prefix (``fill:`` here, ``rest-fill:`` in the reconciler).
MAX_EXECUTION_ID_LENGTH = 100
#: ``broker_order_id`` columns are String(96); client order IDs are at most 40 (domain/ids.py).
MAX_BROKER_ORDER_ID_LENGTH = 96
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)


class MappingError(BrokerError):
    """An Alpaca payload that cannot be mapped exactly; the adapter fails closed."""


class UnmappedStatus(MappingError):
    """An Alpaca order status with no safe meaning in this system (pause-worthy)."""

    def __init__(self, status: str, *, broker_order_id: str, client_order_id: str) -> None:
        super().__init__(
            f"Alpaca order {broker_order_id} (client {client_order_id}) has status "
            f"{status!r}, which this system does not map; owner review required"
        )
        self.status = status
        self.broker_order_id = broker_order_id
        self.client_order_id = client_order_id


# --------------------------------------------------------------------------- statuses

#: Map by cumulative quantity: ACCEPTED (none), PARTIALLY_FILLED (some) or FILLED (all).
WORKING: Final = "WORKING"
#: Raise :class:`UnmappedStatus`.
UNMAPPED: Final = "UNMAPPED"

#: Every Alpaca order status, documented ("Orders at Alpaca", checked 2026-11-08) or listed by the
#: pinned SDK's ``alpaca.trading.enums.OrderStatus`` (``held``, ``pending_review``), and its rule.
STATUS_RULES: Final[Mapping[str, BrokerOrderStatus | str]] = {
    "new": WORKING,
    "accepted": WORKING,
    "pending_new": WORKING,
    "pending_review": WORKING,
    "accepted_for_bidding": WORKING,
    "held": WORKING,
    "stopped": WORKING,
    "done_for_day": WORKING,
    "calculated": WORKING,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "pending_cancel": BrokerOrderStatus.PENDING_CANCEL,
    "canceled": BrokerOrderStatus.CANCELED,
    "expired": BrokerOrderStatus.EXPIRED,
    "rejected": BrokerOrderStatus.REJECTED,
    "replaced": UNMAPPED,
    "pending_replace": UNMAPPED,
    "suspended": UNMAPPED,
}

#: trade_updates events that report an execution (the fill itself is fetched over REST).
FILL_EVENTS: Final = frozenset({"fill", "partial_fill"})


def map_status(
    status: str,
    *,
    filled: Decimal,
    quantity: Decimal,
    broker_order_id: str,
    client_order_id: str,
) -> BrokerOrderStatus:
    rule = STATUS_RULES.get(status, UNMAPPED)
    if isinstance(rule, BrokerOrderStatus):
        return rule
    if rule == WORKING:
        if filled == ZERO:
            return BrokerOrderStatus.ACCEPTED
        if filled < quantity:
            return BrokerOrderStatus.PARTIALLY_FILLED
        return BrokerOrderStatus.FILLED
    raise UnmappedStatus(status, broker_order_id=broker_order_id, client_order_id=client_order_id)


# --------------------------------------------------------------------------- primitives


def parse_decimal(value: object, field: str) -> Decimal:
    """``Decimal(str(value))``, refused unless it round-trips exactly (module docstring)."""
    if value is None or isinstance(value, bool) or not isinstance(value, str | int | float):
        raise MappingError(f"{field}: expected a decimal number, got {type(value).__name__}")
    text = str(value)
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise MappingError(f"{field}: not a decimal number: {text!r}") from None
    if not result.is_finite():
        raise MappingError(f"{field}: NaN and infinite values are rejected")
    if isinstance(value, str):
        ok = format(result, "f") == text
    elif isinstance(value, int):
        ok = int(result) == value
    else:
        ok = math.isfinite(value) and float(result) == value
    if not ok:
        raise MappingError(f"{field}: {text!r} does not round-trip as a decimal")
    return result


def optional_decimal(value: object, field: str) -> Decimal | None:
    return None if value is None else parse_decimal(value, field)


def parse_timestamp(value: object, field: str) -> datetime:
    """RFC 3339 timestamp -> UTC datetime, truncated to whole microseconds."""
    if not isinstance(value, str):
        raise MappingError(f"{field}: expected an RFC 3339 timestamp, got {type(value).__name__}")
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise MappingError(f"{field}: not an RFC 3339 timestamp: {value!r}")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    micros = int((fraction or "").ljust(6, "0")[:6])
    try:
        # Wall-clock fields as written, then shifted by the stated offset to true UTC.
        local = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micros, UTC
        )
    except ValueError:
        raise MappingError(f"{field}: invalid date or time: {value!r}") from None
    if zone == "Z":
        offset = timedelta(0)
    else:
        sign = 1 if zone[0] == "+" else -1
        offset = sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    return local - offset


def epoch_micros(value: datetime) -> int:
    """Exact integer microseconds since the Unix epoch (the Alpaca ``broker_version``)."""
    return (value - _EPOCH) // _MICROSECOND


def _text(raw: JsonObject, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise MappingError(f"{key}: expected a non-empty string")
    return value


def _side(value: object) -> Side:
    if value == "buy":
        return Side.BUY
    if value == "sell":
        return Side.SELL
    raise MappingError(f"side: unsupported value {value!r}")


_ORDER_TYPES: Final[Mapping[str, OrderType]] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
}


# --------------------------------------------------------------------------- symbols


@dataclass(frozen=True, slots=True)
class SymbolMap:
    """Alpaca symbol <-> instrument id, built from the configured instrument specs."""

    by_symbol: Mapping[str, str]
    by_instrument: Mapping[str, str]

    @classmethod
    def from_specs(cls, specs: Mapping[str, AnySpec]) -> SymbolMap:
        by_symbol: dict[str, str] = {}
        by_instrument: dict[str, str] = {}
        for instrument_id, spec in specs.items():
            if spec.instrument_id != instrument_id:
                raise DomainError(f"instrument map key {instrument_id} != {spec.instrument_id}")
            if spec.identity.broker != "alpaca":
                raise DomainError(
                    f"{instrument_id} is configured for broker {spec.identity.broker!r}, not alpaca"
                )
            symbol = spec.identity.symbol
            if symbol in by_symbol:
                raise DomainError(f"symbol {symbol} is configured twice")
            by_symbol[symbol] = instrument_id
            by_instrument[instrument_id] = symbol
        return cls(by_symbol=by_symbol, by_instrument=by_instrument)

    def instrument_for(self, symbol: str) -> str:
        return self.by_symbol.get(symbol, UNMAPPED_PREFIX + symbol)


# --------------------------------------------------------------------------- objects


def map_order(raw: JsonObject, *, broker: str, account_id: str, symbols: SymbolMap) -> BrokerOrder:
    boid = _text(raw, "id")
    cid = _text(raw, "client_order_id")
    if len(cid) > MAX_CLIENT_ORDER_ID_LENGTH or len(boid) > MAX_BROKER_ORDER_ID_LENGTH:
        raise MappingError(f"order {boid[:40]} has ids longer than the ledger stores")
    if raw.get("qty") is None:
        raise MappingError(
            f"order {boid} has no share quantity (notional orders are never sent by this system)"
        )
    quantity = parse_decimal(raw.get("qty"), "qty")
    filled = parse_decimal(raw.get("filled_qty", "0"), "filled_qty")
    order_type = _ORDER_TYPES.get(str(raw.get("type") or raw.get("order_type")))
    if order_type is None:
        raise MappingError(f"order {boid}: unsupported order type {raw.get('type')!r}")
    limit_price = optional_decimal(raw.get("limit_price"), "limit_price")
    status = map_status(
        _text(raw, "status"),
        filled=filled,
        quantity=quantity,
        broker_order_id=boid,
        client_order_id=cid,
    )
    updated_at = parse_timestamp(raw.get("updated_at"), "updated_at")
    submitted = raw.get("submitted_at") or raw.get("created_at")
    try:
        return BrokerOrder(
            broker=broker,
            account_id=account_id,
            broker_order_id=boid,
            client_order_id=cid,
            instrument_id=symbols.instrument_for(_text(raw, "symbol")),
            side=_side(raw.get("side")),
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price if order_type is OrderType.LIMIT else None,
            status=status,
            filled_quantity=filled,
            broker_version=epoch_micros(updated_at),
            submitted_at=parse_timestamp(submitted, "submitted_at"),
            updated_at=updated_at,
        )
    except (ValidationError, DomainError) as exc:
        raise MappingError(f"order {boid} is inconsistent: {_first_error(exc)}") from None


def map_fill_activity(
    raw: JsonObject,
    *,
    broker: str,
    account_id: str,
    client_order_id: str,
    symbols: SymbolMap,
    currency: str,
    receipt_time: datetime,
) -> Fill:
    """A REST ``FILL`` activity -> :class:`Fill` whose execution id is the activity ``id``.

    Alpaca reports regulatory and other pass-through fees later as separate activities, and the
    fee schedule for this account is unverified, so every fill carries fee 0 with status
    ``PENDING`` (an accrual the ledger can finalise), never a claimed final fee.
    """
    if raw.get("activity_type", "FILL") != "FILL":
        raise MappingError(f"activity {raw.get('id')!r} is not a FILL")
    execution_id = _text(raw, "id")
    if len(execution_id) > MAX_EXECUTION_ID_LENGTH:
        raise MappingError("execution id is longer than the ledger accepts")
    try:
        return Fill(
            broker=broker,
            account_id=account_id,
            execution_id=execution_id,
            broker_order_id=_text(raw, "order_id"),
            client_order_id=client_order_id,
            instrument_id=symbols.instrument_for(_text(raw, "symbol")),
            side=_side(raw.get("side")),
            quantity=parse_decimal(raw.get("qty"), "qty"),
            price=parse_decimal(raw.get("price"), "price"),
            currency=currency,
            fee=ZERO,
            fee_currency=currency,
            fee_status=FeeStatus.PENDING,
            exec_time=parse_timestamp(raw.get("transaction_time"), "transaction_time"),
            receipt_time=receipt_time,
        )
    except (ValidationError, DomainError) as exc:
        raise MappingError(f"fill {execution_id} is inconsistent: {_first_error(exc)}") from None


#: ``status`` of a non-trade activity whose amount is in the account's cash.
FEE_ACTIVITY_CHARGED: Final = "executed"
#: ``status`` of a non-trade activity that was withdrawn: its amount is not in the cash.
FEE_ACTIVITY_CANCELED: Final = "canceled"


def map_fee_activity(raw: JsonObject, *, currency: str) -> BrokerFeeCharge | None:
    """A REST ``FEE`` activity -> :class:`BrokerFeeCharge`; ``None`` for a canceled one.

    Alpaca books a trading day's regulatory fees (sub types such as ``REG`` and ``CAT``) as one
    activity per fee type during the following night, with a negative ``net_amount`` and the
    booking time in ``created_at``. Any other status than executed or canceled is refused: what
    it means for the cash is not known.
    """
    if raw.get("activity_type") != "FEE":
        raise MappingError(f"activity {raw.get('id')!r} is not a FEE")
    charge_id = _text(raw, "id")
    status = raw.get("status")
    if status == FEE_ACTIVITY_CANCELED:
        return None
    if status != FEE_ACTIVITY_CHARGED:
        raise MappingError(f"fee activity {charge_id} has status {status!r}, which is not mapped")
    sub_type = raw.get("activity_sub_type")
    try:
        return BrokerFeeCharge(
            charge_id=charge_id,
            kind=f"FEE:{sub_type}" if isinstance(sub_type, str) and sub_type else "FEE",
            amount=-parse_decimal(raw.get("net_amount"), "net_amount"),
            currency=str(raw.get("currency") or currency),
            charged_at=parse_timestamp(raw.get("created_at"), "created_at"),
        )
    except (ValidationError, DomainError) as exc:
        raise MappingError(
            f"fee activity {charge_id} is inconsistent: {_first_error(exc)}"
        ) from None


def map_position(raw: JsonObject, *, symbols: SymbolMap) -> BrokerPosition:
    symbol = _text(raw, "symbol")
    quantity = parse_decimal(raw.get("qty"), "qty")
    side = raw.get("side", "long")
    if (side == "long" and quantity < 0) or (side == "short" and quantity > 0):
        raise MappingError(f"position {symbol}: side {side!r} contradicts quantity {quantity}")
    try:
        return BrokerPosition(
            instrument_id=symbols.instrument_for(symbol),
            quantity=quantity,
            avg_entry_price=parse_decimal(raw.get("avg_entry_price"), "avg_entry_price"),
            currency=str(raw.get("currency") or "USD"),
        )
    except (ValidationError, DomainError) as exc:
        raise MappingError(f"position {symbol} is inconsistent: {_first_error(exc)}") from None


#: Account flags reported as restrictions (any of them blocks new exposure upstream).
_RESTRICTION_FLAGS: Final = (
    "trading_blocked",
    "account_blocked",
    "trade_suspended_by_user",
    "transfers_blocked",
    "pattern_day_trader",
)


def map_account(
    raw: JsonObject,
    *,
    broker: str,
    account_id: str,
    as_of: datetime,
    positions: Sequence[BrokerPosition],
    open_order_count: int,
    legal_type: LegalAccountType,
    rule_profile: RuleProfileKind,
) -> AccountSnapshot:
    """``GET /v2/account`` -> :class:`AccountSnapshot`.

    The system never borrows, so ``buying_power`` is Alpaca's ``non_marginable_buying_power``
    (falling back to ``buying_power``), floored at zero. Alpaca's Trading API reports no settled
    cash figure; ``settled_cash`` repeats ``cash`` and the ledger's settlement schedule, not this
    field, governs available funds (risk/account_rules.py). Day trades remaining are unknown.
    """
    restrictions = [flag.upper() for flag in _RESTRICTION_FLAGS if raw.get(flag) is True]
    status = raw.get("status")
    if status != "ACTIVE":
        restrictions.append(f"STATUS_{status}")
    cash = parse_decimal(raw.get("cash"), "cash")
    power_raw = raw.get("non_marginable_buying_power", raw.get("buying_power"))
    try:
        return AccountSnapshot(
            broker=broker,
            account_id=account_id,
            as_of=as_of,
            currency=str(raw.get("currency") or ""),
            cash=cash,
            settled_cash=cash,
            buying_power=max(parse_decimal(power_raw, "buying_power"), ZERO),
            equity=parse_decimal(raw.get("equity"), "equity"),
            positions=tuple(positions),
            open_order_count=open_order_count,
            restrictions=tuple(restrictions),
            legal_type=legal_type,
            rule_profile=rule_profile,
            day_trades_remaining=None,
        )
    except (ValidationError, DomainError) as exc:
        raise MappingError(f"account snapshot is inconsistent: {_first_error(exc)}") from None


# --------------------------------------------------------------------------- stream


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    event: str
    order: JsonObject
    #: As reported by the stream; informational only (never a ledger execution id).
    stream_execution_id: str | None


def parse_trade_update(message: object) -> TradeUpdate:
    """A raw ``trade_updates`` message (``{"stream": ..., "data": {...}}`` or its ``data``)."""
    if not isinstance(message, Mapping):
        raise MappingError("trade update is not an object")
    data = message.get("data", message)
    if not isinstance(data, Mapping):
        raise MappingError("trade update has no data object")
    event = data.get("event")
    order = data.get("order")
    if not isinstance(event, str) or not isinstance(order, Mapping):
        raise MappingError("trade update lacks an event name or an order object")
    execution = data.get("execution_id")
    return TradeUpdate(
        event=event,
        order=order,
        stream_execution_id=execution if isinstance(execution, str) else None,
    )


# --------------------------------------------------------------------------- events


def status_event(order: BrokerOrder, *, source: str, alpaca_status: str) -> BrokerOrderEvent:
    """The order's state at ``broker_version``; the same id whichever source reported it."""
    return BrokerOrderEvent(
        broker=order.broker,
        account_id=order.account_id,
        event_id=f"{order.broker_order_id}:v{order.broker_version}",
        broker_order_id=order.broker_order_id,
        client_order_id=order.client_order_id,
        status=order.status,
        order_quantity=order.quantity,
        cum_filled_quantity=order.filled_quantity,
        event_time=order.updated_at,
        broker_version=order.broker_version,
        raw={"source": source, "alpaca_status": alpaca_status},
    )


def fill_event(order: BrokerOrder, fill: Fill) -> BrokerOrderEvent:
    """One event per canonical execution id, carrying the latest known order state."""
    return BrokerOrderEvent(
        broker=order.broker,
        account_id=order.account_id,
        event_id=f"fill:{fill.execution_id}",
        broker_order_id=order.broker_order_id,
        client_order_id=order.client_order_id,
        status=order.status,
        order_quantity=order.quantity,
        cum_filled_quantity=order.filled_quantity,
        event_time=order.updated_at,
        broker_version=order.broker_version,
        fill=fill,
        raw={"source": "alpaca-rest-activities"},
    )


# --------------------------------------------------------------------------- requests

_TIF: Final[Mapping[TimeInForce, str]] = {
    TimeInForce.DAY: "day",
    TimeInForce.IOC: "ioc",
    TimeInForce.GTC: "gtc",
}


def order_payload(intent: OrderIntent, *, symbol: str) -> dict[str, str]:
    """``POST /v2/orders`` body. Quantities and prices are sent as exact decimal strings."""
    payload = {
        "symbol": symbol,
        "qty": canon_decimal(intent.quantity),
        "side": "buy" if intent.side is Side.BUY else "sell",
        "type": "limit" if intent.order_type is OrderType.LIMIT else "market",
        "time_in_force": _TIF[intent.time_in_force],
        "client_order_id": intent.client_order_id,
    }
    if intent.limit_price is not None:
        payload["limit_price"] = canon_decimal(intent.limit_price)
    return payload


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            return str(errors[0].get("msg", exc))
    return str(exc)
