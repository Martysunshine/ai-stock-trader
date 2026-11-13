"""Order intents, approved plans, broker orders, events and fills (architecture.md §3, §7).

An :class:`ApprovedPlan` can only be produced by the risk engine
(``trader.risk.approval.issue_plan``). Its ``digest`` binds the intent hash, account version,
policy hash, model hash, quote time, validity window and client order ID; changing any of
them invalidates the approval (architecture.md §2).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from trader.domain.base import DomainModel
from trader.domain.hashing import content_hash, sha256_hex
from trader.domain.types import (
    ZERO,
    CurrencyCode,
    DomainError,
    NonNegDec,
    PosDec,
    Side,
    Track,
    UtcDatetime,
    canon_decimal,
    iso_utc,
)


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT_STOP = "EXIT_STOP"
    EXIT_TARGET = "EXIT_TARGET"
    EXIT_TIME = "EXIT_TIME"
    EXIT_SESSION = "EXIT_SESSION"
    EXIT_RISK_REDUCTION = "EXIT_RISK_REDUCTION"
    EXIT_LIQUIDATION = "EXIT_LIQUIDATION"

    @property
    def is_exit(self) -> bool:
        return self is not OrderPurpose.ENTRY


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    IOC = "IOC"
    GTC = "GTC"


class OrderState(StrEnum):
    """Local OMS state of an intent. See ``trader.execution.states`` for transitions."""

    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTING = "SUBMITTING"
    NOT_SENT = "NOT_SENT"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    UNRESOLVED = "UNRESOLVED"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


#: States in which the broker may still execute quantity: they count as live exposure.
LIVE_STATES = frozenset(
    {
        OrderState.PENDING_SUBMIT,
        OrderState.SUBMITTING,
        OrderState.UNKNOWN_OUTCOME,
        OrderState.UNRESOLVED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
    }
)
TERMINAL_STATES = frozenset(
    {
        OrderState.NOT_SENT,
        OrderState.NOT_ACCEPTED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    }
)


class OrderIntent(DomainModel):
    intent_id: str
    client_order_id: str = Field(min_length=4, max_length=40)
    portfolio_id: str
    account_id: str
    instrument_id: str
    track: Track
    side: Side
    purpose: OrderPurpose
    order_type: OrderType
    quantity: PosDec
    limit_price: PosDec | None
    time_in_force: TimeInForce
    reason: str
    created_at: UtcDatetime
    candidate_id: str | None = None
    parent_intent_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> OrderIntent:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise DomainError("limit order without limit price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise DomainError("market order must not carry a limit price")
        if self.purpose is OrderPurpose.ENTRY and self.side is not Side.BUY:
            raise DomainError("v1 entries are long-only buys")
        if self.purpose.is_exit and self.side is not Side.SELL:
            raise DomainError("v1 exits are sells of owned quantity")
        return self

    def intent_hash(self) -> str:
        return content_hash(self.model_dump(exclude={"schema_version"}))


def plan_digest(
    *,
    intent_hash: str,
    account_version: int,
    policy_hash: str,
    model_hash: str,
    quote_time: datetime | None,
    valid_until: datetime,
    client_order_id: str,
    reserved_cash_usd: Decimal,
    reserved_risk_usd: Decimal,
) -> str:
    parts = [
        intent_hash,
        str(account_version),
        policy_hash,
        model_hash,
        iso_utc(quote_time) if quote_time else "-",
        iso_utc(valid_until),
        client_order_id,
        canon_decimal(reserved_cash_usd),
        canon_decimal(reserved_risk_usd),
    ]
    return sha256_hex("|".join(parts))


class ApprovedPlan(DomainModel):
    intent: OrderIntent
    intent_hash: str
    decision_id: str
    account_version: int = Field(ge=0)
    policy_hash: str
    model_hash: str
    quote_time: UtcDatetime | None
    approved_at: UtcDatetime
    valid_until: UtcDatetime
    reserved_cash_usd: NonNegDec
    reserved_risk_usd: NonNegDec
    exit_method: str
    digest: str

    def expected_digest(self) -> str:
        return plan_digest(
            intent_hash=self.intent.intent_hash(),
            account_version=self.account_version,
            policy_hash=self.policy_hash,
            model_hash=self.model_hash,
            quote_time=self.quote_time,
            valid_until=self.valid_until,
            client_order_id=self.intent.client_order_id,
            reserved_cash_usd=self.reserved_cash_usd,
            reserved_risk_usd=self.reserved_risk_usd,
        )

    def is_authentic(self) -> bool:
        return (
            self.intent_hash == self.intent.intent_hash() and self.digest == self.expected_digest()
        )

    def is_valid_at(self, now: datetime) -> bool:
        return self.approved_at <= now < self.valid_until


class BrokerOrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            BrokerOrderStatus.FILLED,
            BrokerOrderStatus.CANCELED,
            BrokerOrderStatus.EXPIRED,
            BrokerOrderStatus.REJECTED,
        )


class FeeStatus(StrEnum):
    FINAL = "FINAL"
    PENDING = "PENDING"


class Fill(DomainModel):
    broker: str
    account_id: str
    execution_id: str = Field(min_length=1)
    broker_order_id: str
    client_order_id: str
    instrument_id: str
    side: Side
    quantity: PosDec
    price: PosDec
    currency: CurrencyCode
    fee: NonNegDec
    fee_currency: CurrencyCode
    fee_status: FeeStatus
    exec_time: UtcDatetime
    receipt_time: UtcDatetime


class BrokerOrder(DomainModel):
    broker: str
    account_id: str
    broker_order_id: str
    client_order_id: str
    instrument_id: str
    side: Side
    order_type: OrderType
    quantity: PosDec
    limit_price: PosDec | None
    status: BrokerOrderStatus
    filled_quantity: NonNegDec
    broker_version: int = Field(ge=0)
    submitted_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> BrokerOrder:
        _check_status(self.status, self.filled_quantity, self.quantity)
        return self


def _check_status(status: BrokerOrderStatus, cum: Decimal, qty: Decimal) -> None:
    if cum > qty:
        raise DomainError("cumulative filled quantity exceeds order quantity")
    if status is BrokerOrderStatus.FILLED and cum != qty:
        raise DomainError("contradictory status: FILLED with remaining quantity")
    if status is BrokerOrderStatus.PARTIALLY_FILLED and not (ZERO < cum < qty):
        raise DomainError("contradictory status: PARTIALLY_FILLED without a partial quantity")
    if status is BrokerOrderStatus.REJECTED and cum != 0:
        raise DomainError("contradictory status: REJECTED after executions")


class BrokerOrderEvent(DomainModel):
    broker: str
    account_id: str
    event_id: str = Field(min_length=1)
    broker_order_id: str
    client_order_id: str
    status: BrokerOrderStatus
    order_quantity: PosDec
    cum_filled_quantity: NonNegDec
    event_time: UtcDatetime
    broker_version: int = Field(ge=0)
    fill: Fill | None = None
    raw: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> BrokerOrderEvent:
        _check_status(self.status, self.cum_filled_quantity, self.order_quantity)
        if self.fill is not None and (
            self.fill.client_order_id != self.client_order_id
            or self.fill.broker_order_id != self.broker_order_id
        ):
            raise DomainError("fill does not belong to this order event")
        return self

    @property
    def remaining_quantity(self) -> Decimal:
        return self.order_quantity - self.cum_filled_quantity


class SubmissionResult(DomainModel):
    accepted: bool
    broker_order_id: str | None
    status: BrokerOrderStatus
    reject_reason: str | None = None


class CancellationResult(DomainModel):
    """A cancel *request* result. "Cancel requested" is not "canceled" (architecture.md §7)."""

    request_accepted: bool
    broker_order_id: str
    detail: str = ""
