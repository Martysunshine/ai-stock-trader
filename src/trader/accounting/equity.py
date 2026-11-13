"""Conservative equity, capital base and effective sizing equity.

``effective_sizing_equity = min(capital_base, conservative_current_equity)``
(strategy_and_profitability.md §9): unrealised gains never increase sizing, while losses and
adverse marks reduce it immediately. Positions are marked at the bid, less estimated exit fees.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection

from trader.accounting.fees import FeeSchedule
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.domain.types import ZERO, Side


@dataclass(frozen=True, slots=True)
class AccountEquity:
    account_id: str
    currency: str
    cash: Decimal
    positions_value: Decimal
    estimated_exit_fees: Decimal
    conservative_equity: Decimal
    capital_base: Decimal
    net_external_flows: Decimal
    unmarked_instruments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioEquity:
    accounts: tuple[AccountEquity, ...]
    conservative_equity_usd: Decimal
    capital_base_usd: Decimal
    net_external_flows_usd: Decimal

    @property
    def effective_sizing_equity_usd(self) -> Decimal:
        return min(self.capital_base_usd, self.conservative_equity_usd)

    @property
    def cash_flow_adjusted_equity_usd(self) -> Decimal:
        """Equity with external contributions removed, relative to the initial capital.

        Used for the drawdown and USD 45 floor checks so that a later deposit can never hide a
        trading loss (ADR-0004).
        """
        return self.conservative_equity_usd - (
            self.net_external_flows_usd - self.initial_capital_usd
        )

    initial_capital_usd: Decimal = ZERO


def account_equity(
    conn: Connection,
    ledger: Ledger,
    *,
    account_id: str,
    currency: str,
    marks: Mapping[str, Decimal],
    fees: FeeSchedule | None,
) -> AccountEquity:
    cash = ledger.cash(conn, account_id, currency)
    value = ZERO
    exit_fees = ZERO
    unmarked: list[str] = []
    for pos in ledger.all_positions(conn, account_id):
        if pos.quantity == 0:
            continue
        mark = marks.get(pos.instrument_id)
        if mark is None:
            # No observed bid yet: value at the lower of nothing better than cost; flagged.
            unmarked.append(pos.instrument_id)
            mark = pos.avg_price
        value += pos.quantity * mark
        if fees is not None and fees.known:
            exit_fees += fees.fee(Side.SELL, pos.quantity, pos.quantity * mark)
    return AccountEquity(
        account_id=account_id,
        currency=currency,
        cash=cash,
        positions_value=value,
        estimated_exit_fees=exit_fees,
        conservative_equity=cash + value - exit_fees,
        capital_base=ledger.capital_base(conn, account_id),
        net_external_flows=ledger.net_external_flows(conn, account_id),
        unmarked_instruments=tuple(unmarked),
    )


def portfolio_equity(
    accounts: list[AccountEquity],
    *,
    fx: FxBook,
    now: datetime,
    initial_capital_usd: Decimal,
) -> PortfolioEquity:
    equity = ZERO
    capital = ZERO
    flows = ZERO
    for acc in accounts:
        rate = fx.asset_rate(acc.currency, now)
        equity += acc.conservative_equity * rate
        capital += acc.capital_base * rate
        flows += acc.net_external_flows * rate
    return PortfolioEquity(
        accounts=tuple(accounts),
        conservative_equity_usd=equity,
        capital_base_usd=capital,
        net_external_flows_usd=flows,
        initial_capital_usd=initial_capital_usd,
    )
