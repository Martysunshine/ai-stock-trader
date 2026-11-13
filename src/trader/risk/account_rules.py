"""Account-rule profiles (markets_and_brokers.md §4).

Each profile behaves explicitly; an unknown regime never silently assumes permission. No profile
borrows: available funds are cash (TRUE_CASH: settled cash) minus active entry reservations,
never broker buying power beyond cash. "No borrowing" does not change the legal account type.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trader.domain.account import RuleProfileKind
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.types import ZERO, Mode


@dataclass(frozen=True, slots=True)
class FundsInputs:
    cash: Decimal
    settled_cash: Decimal
    active_entry_reservations: Decimal


def spendable_cash(profile: RuleProfileKind, funds: FundsInputs) -> Decimal:
    if profile is RuleProfileKind.TRUE_CASH:
        base = funds.settled_cash
    else:
        base = funds.cash
    return max(base - funds.active_entry_reservations, ZERO)


def entry_rule_problems(
    profile: RuleProfileKind,
    *,
    mode: Mode,
    capabilities: BrokerCapabilities,
    day_trades_remaining: int | None,
    plans_same_day_exit: bool,
) -> list[str]:
    """Reasons the account-rule profile forbids a new entry (empty list = permitted)."""
    problems: list[str] = []
    real_money = mode is Mode.LIVE
    if profile is RuleProfileKind.UNVERIFIED:
        if real_money:
            problems.append("account-rule profile UNVERIFIED: live entries are blocked")
        return problems
    if profile is RuleProfileKind.LEGACY_PDT and plans_same_day_exit:
        if day_trades_remaining is None:
            problems.append("LEGACY_PDT: remaining day-trade capacity is unknown")
        elif day_trades_remaining < 1:
            problems.append("LEGACY_PDT: no day-trade capacity left for the planned round trip")
    if (
        profile is RuleProfileKind.NEW_INTRADAY_MARGIN
        and capabilities.cash_availability == "UNKNOWN"
    ):
        problems.append("NEW_INTRADAY_MARGIN: broker cash-availability semantics are not certified")
    if (
        profile is RuleProfileKind.TRUE_CASH
        and capabilities.cash_availability == "BROKER_BUYING_POWER"
    ):
        problems.append("TRUE_CASH profile but the broker reports buying power, not settled funds")
    return problems
