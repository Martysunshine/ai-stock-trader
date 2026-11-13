"""Broker adapter contract (architecture.md §4).

Adapted from the proposed asynchronous interface to a synchronous one for M1 (ADR-0014): the
deterministic replay loop is single-threaded, and a future streaming adapter will feed its
WebSocket events into ``drain_events`` from a background thread.

Submission is authority-gated: an adapter accepts ``submit`` only together with the opaque
authority object bound by ``trader.execution._gateway.SubmissionGateway``, and only for an
authentic, unexpired :class:`~trader.domain.orders.ApprovedPlan`. Adapters expose no funding or
withdrawal operation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from trader.domain.account import AccountSnapshot, BrokerFeeCharge, BrokerPosition
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.orders import (
    ApprovedPlan,
    BrokerOrder,
    BrokerOrderEvent,
    CancellationResult,
    Fill,
    SubmissionResult,
)


class BrokerError(Exception):
    """A definite broker-side error (not an unknown outcome)."""


class BrokerTimeout(Exception):
    """The request outcome is unknown: the broker may or may not have accepted it."""


class DuplicateClientOrderId(BrokerTimeout):
    """The broker refused a client order ID it already holds.

    For the submission path this stays an *unknown* outcome (an order with this ID exists and is
    resolved by client ID, never re-sent). The paper certification probe reads it as the
    demonstration that the broker enforces client order ID uniqueness.
    """

    def __init__(self, client_order_id: str, *, detail: str) -> None:
        super().__init__(
            f"{client_order_id}: the broker already holds this client order ID; resolve by "
            f"client ID ({detail})"
        )
        self.client_order_id = client_order_id
        self.detail = detail


class UnauthorizedSubmission(Exception):
    """Raised when anything other than the submission gateway tries to submit."""


class SimulatedCrash(BaseException):
    """Replay fault: the worker process dies after the broker accepted an order."""


class OrderScope(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ALL = "ALL"


class BrokerAdapter(Protocol):
    broker: str
    account_id: str

    def capabilities(self) -> BrokerCapabilities: ...

    def bind_authority(self, authority: object) -> None: ...

    def get_account(self) -> AccountSnapshot: ...

    def get_positions(self) -> list[BrokerPosition]: ...

    def list_orders(self, scope: OrderScope) -> list[BrokerOrder]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def list_executions(self, *, broker_order_id: str | None = None) -> list[Fill]: ...

    def list_fee_charges(self) -> list[BrokerFeeCharge]:
        """Fees the broker took from cash apart from any fill, since the account's baseline.

        Reconciliation allows for them when it compares cash (ADR-0054). A broker that reports
        every fee on the fill itself returns an empty list.
        """
        ...

    def submit(self, plan: ApprovedPlan, *, authority: object) -> SubmissionResult: ...

    def cancel(self, broker_order_id: str) -> CancellationResult: ...

    def drain_events(self) -> list[BrokerOrderEvent]: ...
