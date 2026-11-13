"""Broker capability profiles with explicit evidence levels (architecture.md §4).

Unsupported *or unknown* combinations remain disabled. Fractional limit, stop, stop-limit,
bracket/OCO and extended-hours behaviour are separate capabilities. Paper evidence cannot
certify live availability.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from trader.domain.account import LegalAccountType, RuleProfileKind
from trader.domain.base import DomainModel
from trader.domain.types import DomainError, EvidenceLevel, UtcDatetime

#: Canonical capability names used across adapters.
FRACTIONAL_MARKET_DAY = "fractional.market.day"
FRACTIONAL_LIMIT_DAY = "fractional.limit.day"
FRACTIONAL_STOP_DAY = "fractional.stop.day"
FRACTIONAL_STOP_LIMIT_DAY = "fractional.stop_limit.day"
FRACTIONAL_BRACKET = "fractional.bracket"
WHOLE_SHARE_BRACKET = "whole.bracket"
EXTENDED_HOURS = "extended_hours"
NATIVE_PROTECTIVE_STOP = "native_protective_stop"
CLIENT_ORDER_ID_UNIQUE = "client_order_id_unique"
SHORT_SELLING = "short_selling"
MARGIN_BORROWING = "margin_borrowing"


class Capability(DomainModel):
    supported: bool | None = Field(description="None means unknown, which is treated as disabled")
    evidence: EvidenceLevel
    source: str = ""
    notes: str = ""

    @property
    def usable(self) -> bool:
        return self.supported is True and self.evidence is not EvidenceLevel.UNKNOWN


class DeadManTimer(DomainModel):
    """A venue "cancel all after" timer (e.g. Kraken CancelAllOrdersAfter)."""

    enabled: bool
    scope: Literal["ALL_ORDERS", "ENTRY_ORDERS_ONLY", "UNKNOWN"]


class BrokerCapabilities(DomainModel):
    broker: str
    environment: Literal["replay", "paper", "demo", "live"]
    profile_version: str
    as_of: UtcDatetime
    account_legal_type: LegalAccountType
    rule_profile: RuleProfileKind
    capabilities: dict[str, Capability]
    cash_availability: Literal["SETTLED_ONLY", "BROKER_BUYING_POWER", "UNKNOWN"]
    market_data_feed: str
    market_data_consolidated: bool
    order_visibility_lookups: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Certified number of client-ID lookups after which absence establishes "
            "non-acceptance. None = not certified: unknown outcomes stay UNRESOLVED."
        ),
    )
    order_visibility_window_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Certified time after submission within which an accepted order becomes visible by "
            "client ID. Non-acceptance requires BOTH the lookups and this elapsed window."
        ),
    )
    dead_man_timer: DeadManTimer | None = None
    uses_funding_endpoints: Literal[False] = False
    fee_schedule_id: str
    rate_limit_per_minute: int | None = None

    @model_validator(mode="after")
    def _safe(self) -> BrokerCapabilities:
        timer = self.dead_man_timer
        if timer is not None and timer.enabled and timer.scope != "ENTRY_ORDERS_ONLY":
            raise DomainError(
                "a dead-man timer that can cancel protective orders must not be enabled "
                "(CRYPTO_TRADING_TRACK.md §7)"
            )
        return self

    def capability(self, name: str) -> Capability:
        return self.capabilities.get(
            name, Capability(supported=None, evidence=EvidenceLevel.UNKNOWN, source="not listed")
        )

    def usable(self, name: str) -> bool:
        return self.capability(name).usable

    def missing(self, names: tuple[str, ...]) -> list[str]:
        return [n for n in names if not self.usable(n)]

    def live_confirmed(self, name: str) -> bool:
        cap = self.capability(name)
        return cap.usable and cap.evidence is EvidenceLevel.LIVE_CONFIRMED
