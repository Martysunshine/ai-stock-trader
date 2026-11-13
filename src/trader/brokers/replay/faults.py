"""Deterministic fault injection for the replay broker (risk_and_validation.md §6, §9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from trader.domain.base import ConfigModel
from trader.domain.types import NonNegDec


class FaultKind(StrEnum):
    TIMEOUT_BEFORE_ACCEPT = "TIMEOUT_BEFORE_ACCEPT"
    TIMEOUT_AFTER_ACCEPT = "TIMEOUT_AFTER_ACCEPT"
    CRASH_AFTER_ACCEPT = "CRASH_AFTER_ACCEPT"
    DELAYED_VISIBILITY = "DELAYED_VISIBILITY"
    REJECT = "REJECT"
    PARTIAL_FIRST_FILL = "PARTIAL_FIRST_FILL"
    FILL_AFTER_CANCEL_REQUEST = "FILL_AFTER_CANCEL_REQUEST"
    DUPLICATE_EVENTS = "DUPLICATE_EVENTS"
    OUT_OF_ORDER_EVENTS = "OUT_OF_ORDER_EVENTS"
    FEE_PENDING = "FEE_PENDING"
    SUPPRESS_STREAM_EVENTS = "SUPPRESS_STREAM_EVENTS"


class FaultSpec(ConfigModel):
    kind: FaultKind
    purpose: str = Field(default="ENTRY", description="order purpose the fault applies to")
    nth: int = Field(default=1, ge=1, description="applies to the nth submission of that purpose")
    fraction: NonNegDec | None = None
    lookups: int | None = Field(default=None, ge=1)


class FaultPlan(ConfigModel):
    faults: tuple[FaultSpec, ...] = ()


@dataclass
class FaultState:
    plan: FaultPlan
    submissions: dict[str, int] = field(default_factory=dict)
    by_client_id: dict[str, list[FaultSpec]] = field(default_factory=dict)

    def register_submission(self, client_order_id: str, purpose: str) -> list[FaultSpec]:
        if client_order_id in self.by_client_id:
            return self.by_client_id[client_order_id]
        n = self.submissions.get(purpose, 0) + 1
        self.submissions[purpose] = n
        matched = [f for f in self.plan.faults if f.purpose == purpose and f.nth == n]
        self.by_client_id[client_order_id] = matched
        return matched

    def for_order(self, client_order_id: str, kind: FaultKind) -> FaultSpec | None:
        for fault in self.by_client_id.get(client_order_id, []):
            if fault.kind is kind:
                return fault
        return None

    def fraction(self, client_order_id: str, kind: FaultKind) -> Decimal | None:
        fault = self.for_order(client_order_id, kind)
        return fault.fraction if fault else None
