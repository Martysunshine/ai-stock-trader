"""Account snapshots, rule profiles and external cash flows (architecture.md §3, §5)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from trader.domain.base import DomainModel
from trader.domain.types import CurrencyCode, Dec, NonNegDec, UtcDatetime


class RuleProfileKind(StrEnum):
    """markets_and_brokers.md §4 account-rule profiles. UNVERIFIED never permits live entries."""

    LEGACY_PDT = "LEGACY_PDT"
    NEW_INTRADAY_MARGIN = "NEW_INTRADAY_MARGIN"
    TRUE_CASH = "TRUE_CASH"
    UNVERIFIED = "UNVERIFIED"


class LegalAccountType(StrEnum):
    """The legal type is preserved: "no borrowing" does not make a margin account a cash account."""

    CASH = "CASH"
    MARGIN = "MARGIN"
    LIMITED_MARGIN = "LIMITED_MARGIN"
    CFD = "CFD"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    SIMULATED = "SIMULATED"
    UNKNOWN = "UNKNOWN"


class CashFlowKind(StrEnum):
    INITIAL_CAPITAL = "INITIAL_CAPITAL"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DISTRIBUTION = "DISTRIBUTION"


class BrokerPosition(DomainModel):
    instrument_id: str
    quantity: Dec
    avg_entry_price: NonNegDec
    currency: CurrencyCode


class BrokerFeeCharge(DomainModel):
    """A fee the broker took from the account's cash on its own, not as part of a fill.

    Alpaca books a trading day's regulatory fees as account activities during the following
    night, on the paper account too (ADR-0054). ``amount`` is positive for money taken.
    """

    charge_id: str
    kind: str
    amount: Dec
    currency: CurrencyCode
    charged_at: UtcDatetime


class AccountSnapshot(DomainModel):
    broker: str
    account_id: str
    as_of: UtcDatetime
    currency: CurrencyCode
    cash: Dec
    settled_cash: Dec
    buying_power: NonNegDec
    equity: Dec
    positions: tuple[BrokerPosition, ...] = ()
    open_order_count: int = Field(ge=0, default=0)
    restrictions: tuple[str, ...] = ()
    legal_type: LegalAccountType
    rule_profile: RuleProfileKind
    day_trades_remaining: int | None = None
