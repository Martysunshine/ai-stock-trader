"""Ledger, P&L, fees, settlement and account-rule profiles (architecture.md §5; markets §4)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert

from trader.accounting.ledger import Ledger, LedgerInvariantError
from trader.data.calendars import federal_reserve_only_holidays, us_equity_settlement_date
from trader.domain.account import CashFlowKind, RuleProfileKind
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.orders import FeeStatus, Fill
from trader.domain.types import Mode, Side, Track
from trader.risk.account_rules import FundsInputs, entry_rule_problems, spendable_cash
from trader.storage.db import Database
from trader.storage.schema import accounts, portfolios

T = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
ACCT = "acct-test"


def _open(db: Database, ledger: Ledger) -> None:
    with db.tx() as conn:
        conn.execute(
            insert(portfolios).values(
                portfolio_id="pf",
                base_currency="USD",
                initial_capital=Decimal(50),
                version=0,
                created_at=T,
            )
        )
        conn.execute(
            insert(accounts).values(
                account_id=ACCT,
                portfolio_id="pf",
                alias="t",
                track="STOCKS",
                broker="replay",
                mode="replay",
                currency="USD",
                rule_profile="UNVERIFIED",
                legal_type="SIMULATED",
                allocated_capital=Decimal(50),
                broker_cash_baseline=None,
                version=0,
                created_at=T,
            )
        )
        ledger.record_cash_flow(
            conn,
            account_id=ACCT,
            kind=CashFlowKind.INITIAL_CAPITAL,
            amount=Decimal(50),
            currency="USD",
            at=T,
            ref="init",
        )


def _fill(
    exec_id: str,
    side: Side,
    qty: str,
    price: str,
    fee: str = "0",
    status: FeeStatus = FeeStatus.FINAL,
) -> Fill:
    return Fill(
        broker="replay",
        account_id=ACCT,
        execution_id=exec_id,
        broker_order_id="b1",
        client_order_id="co-test",
        instrument_id="XNYS:SYNA",
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        currency="USD",
        fee=Decimal(fee),
        fee_currency="USD",
        fee_status=status,
        exec_time=T,
        receipt_time=T,
    )


def _post(db: Database, ledger: Ledger, fill: Fill) -> bool:
    with db.tx() as conn:
        return ledger.post_fill(
            conn,
            fill,
            intent_id=None,
            owner_track=Track.STOCKS,
            settles_on=date(2026, 11, 30),
            trade_date=date(2026, 11, 27),
        ).posted


def test_realized_net_matches_the_specification_formula(any_db: Database) -> None:
    ledger = Ledger(any_db)
    _open(any_db, ledger)
    _post(any_db, ledger, _fill("x1", Side.BUY, "0.1", "100", "0.01"))
    _post(any_db, ledger, _fill("x2", Side.SELL, "0.04", "102", "0.01"))
    _post(any_db, ledger, _fill("x3", Side.SELL, "0.06", "99", "0.01"))
    with any_db.tx() as conn:
        # q*(exit-entry) - entry fees - exit fees, summed over closed quantity
        expected = (Decimal("0.04") * 2 + Decimal("0.06") * -1) - Decimal("0.01") - Decimal("0.02")
        assert ledger.realized_net_total(conn, ACCT) == expected
        assert ledger.cash(conn, ACCT, "USD") == Decimal(50) + expected
        assert ledger.capital_base(conn, ACCT) == Decimal(50) + expected
        pos = ledger.position(conn, ACCT, "XNYS:SYNA")
        assert pos is not None and pos.quantity == 0 and pos.cost_basis == 0


def test_duplicate_fill_is_posted_once(any_db: Database) -> None:
    ledger = Ledger(any_db)
    _open(any_db, ledger)
    assert _post(any_db, ledger, _fill("x1", Side.BUY, "0.1", "100"))
    assert not _post(any_db, ledger, _fill("x1", Side.BUY, "0.1", "100"))
    with any_db.tx() as conn:
        assert ledger.cash(conn, ACCT, "USD") == Decimal(40)


def test_selling_more_than_owned_is_an_invariant_violation(sqlite_db: Database) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    _post(sqlite_db, ledger, _fill("x1", Side.BUY, "0.1", "100"))
    with pytest.raises(LedgerInvariantError):
        _post(sqlite_db, ledger, _fill("x2", Side.SELL, "0.2", "100"))


def test_fee_accrual_is_reconciled_to_the_final_charge(sqlite_db: Database) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    _post(sqlite_db, ledger, _fill("x1", Side.BUY, "0.1", "100", "0.02", FeeStatus.PENDING))
    with sqlite_db.tx() as conn:
        diff = ledger.finalize_fee(
            conn,
            broker="replay",
            account_id=ACCT,
            execution_id="x1",
            final_fee=Decimal("0.03"),
            currency="USD",
            at=T,
        )
        assert diff == Decimal("0.01")
        assert ledger.cash(conn, ACCT, "USD") == Decimal("40") - Decimal("0.03")
        assert ledger.fee_totals(conn, ACCT) == Decimal("0.03")


def test_deposits_are_contributions_not_trading_profit(sqlite_db: Database) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    with sqlite_db.tx() as conn:
        ledger.record_cash_flow(
            conn,
            account_id=ACCT,
            kind=CashFlowKind.DEPOSIT,
            amount=Decimal(10),
            currency="USD",
            at=T,
            ref="owner deposit",
        )
        assert ledger.realized_net_total(conn, ACCT) == 0
        assert ledger.net_external_flows(conn, ACCT) == Decimal(60)


def test_us_settlement_calendar_skips_weekends_holidays_and_fed_only_days() -> None:
    assert us_equity_settlement_date(date(2026, 11, 27)) == date(2026, 11, 30)  # Fri -> Mon
    assert date(2026, 11, 11) in federal_reserve_only_holidays(2026)  # Veterans Day (Wed)
    assert us_equity_settlement_date(date(2026, 11, 10)) == date(2026, 11, 12)  # skips Veterans Day
    assert us_equity_settlement_date(date(2026, 10, 9)) == date(2026, 10, 13)  # skips Columbus Day
    assert us_equity_settlement_date(date(2026, 11, 25)) == date(2026, 11, 27)  # skips Thanksgiving


def test_true_cash_profile_cannot_reuse_unsettled_proceeds(sqlite_db: Database) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    _post(sqlite_db, ledger, _fill("x1", Side.BUY, "0.1", "100"))
    _post(sqlite_db, ledger, _fill("x2", Side.SELL, "0.1", "100"))
    with sqlite_db.tx() as conn:
        cash = ledger.cash(conn, ACCT, "USD")
        settled = ledger.settled_cash(conn, ACCT, "USD", date(2026, 11, 27))
        settled_monday = ledger.settled_cash(conn, ACCT, "USD", date(2026, 11, 30))
    assert cash == Decimal(50) and settled == Decimal(40) and settled_monday == Decimal(50)
    funds = FundsInputs(cash=cash, settled_cash=settled, active_entry_reservations=Decimal(0))
    assert spendable_cash(RuleProfileKind.TRUE_CASH, funds) == Decimal(40)
    assert spendable_cash(RuleProfileKind.NEW_INTRADAY_MARGIN, funds) == Decimal(50)


def _caps(cash_availability: str) -> BrokerCapabilities:
    return BrokerCapabilities.model_validate(
        {
            "broker": "x",
            "environment": "paper",
            "profile_version": "v",
            "as_of": "2026-11-07T00:00:00Z",
            "account_legal_type": "CASH",
            "rule_profile": "TRUE_CASH",
            "capabilities": {},
            "cash_availability": cash_availability,
            "market_data_feed": "IEX",
            "market_data_consolidated": False,
            "fee_schedule_id": "f",
        }
    )


def test_each_account_rule_profile_behaves_explicitly() -> None:
    settled = _caps("SETTLED_ONLY")
    assert (
        entry_rule_problems(
            RuleProfileKind.UNVERIFIED,
            mode=Mode.PAPER,
            capabilities=settled,
            day_trades_remaining=None,
            plans_same_day_exit=True,
        )
        == []
    )
    assert entry_rule_problems(
        RuleProfileKind.UNVERIFIED,
        mode=Mode.LIVE,
        capabilities=settled,
        day_trades_remaining=None,
        plans_same_day_exit=True,
    )
    assert entry_rule_problems(
        RuleProfileKind.LEGACY_PDT,
        mode=Mode.PAPER,
        capabilities=settled,
        day_trades_remaining=None,
        plans_same_day_exit=True,
    )
    assert entry_rule_problems(
        RuleProfileKind.LEGACY_PDT,
        mode=Mode.PAPER,
        capabilities=settled,
        day_trades_remaining=0,
        plans_same_day_exit=True,
    )
    assert (
        entry_rule_problems(
            RuleProfileKind.LEGACY_PDT,
            mode=Mode.PAPER,
            capabilities=settled,
            day_trades_remaining=2,
            plans_same_day_exit=True,
        )
        == []
    )
    assert entry_rule_problems(
        RuleProfileKind.NEW_INTRADAY_MARGIN,
        mode=Mode.PAPER,
        capabilities=_caps("UNKNOWN"),
        day_trades_remaining=None,
        plans_same_day_exit=True,
    )
    assert entry_rule_problems(
        RuleProfileKind.TRUE_CASH,
        mode=Mode.PAPER,
        capabilities=_caps("BROKER_BUYING_POWER"),
        day_trades_remaining=None,
        plans_same_day_exit=True,
    )
