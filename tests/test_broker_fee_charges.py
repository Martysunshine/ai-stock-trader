"""Fees the broker books on its own (ADR-0054): brokers/alpaca, execution/reconciliation.py.

Alpaca's paper account does charge regulatory fees. It books them as ``FEE`` account activities
during the night after the trade date, one per fee type: on 2026-11-11 at 01:07 and 01:16 UTC it
took 0.01 USD (CAT) and 0.01 USD (REG) for the six fills of 2026-11-10. The ledger knew nothing
of them, so the next start's reconciliation found the broker's cash 0.02 USD lower than it could
explain and paused entries for the session.

Reconciliation now reads the fees booked since the baseline and allows for them when it compares
cash. Cash that no fee activity explains still fails.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.test_modelled_fees import AMZN_FILLS, _fill, _post, _post_session, started
from trader.brokers.alpaca.adapter import ACTIVITY_PAGE_SIZE, FEE_BOOKING_LAG
from trader.brokers.alpaca.mapping import MappingError, map_fee_activity
from trader.domain.orders import FeeStatus
from trader.runtime.paper import PaperEnvironment, PaperRunner

__all__ = ["started"]  # the fixture of tests/test_modelled_fees.py

Started = tuple[PaperEnvironment, Any, PaperRunner]
BROKER_FILL_CASH = sum((cash for *_, cash in AMZN_FILLS), start=Decimal(0))  # -0.21
NIGHT = timedelta(hours=9)  # 16:00 UTC on the trade date + 9 h = 01:00 UTC the next day


def _book_the_night(env: PaperEnvironment, fake: Any) -> None:
    """What Alpaca booked for 2026-11-10: one cent of CAT and one cent of REG."""
    booked = env.clock.now() + NIGHT
    trade_date = env.clock.now().date().isoformat()
    fake.charge_fee(Decimal("0.01"), trade_date=trade_date, created_at=booked, sub_type="CAT")
    fake.charge_fee(
        Decimal("0.01"),
        trade_date=trade_date,
        created_at=booked + timedelta(minutes=9),
        sub_type="REG",
    )


def test_reconciliation_passes_after_the_broker_books_the_days_fees(started: Started) -> None:
    env, fake, runner = started
    _post_session(env)  # the ledger accrues its own estimate, 0.08
    fake.cash += BROKER_FILL_CASH
    _book_the_night(env, fake)
    result = runner._reconcile("paper-periodic")
    assert result.status == "PASSED", result.problems
    assert any(
        "fees charged by the broker apart from the fills 0.02 (2 charge(s)" in note
        for note in result.notes
    )
    assert any("fees accrued and not charged by the broker 0.08" in note for note in result.notes)


def test_fills_recorded_without_any_fee_reconcile_once_the_broker_charges(
    started: Started,
) -> None:
    """The 2026-11-10 fills: posted before fees were accrued, so the ledger holds no fee."""
    env, fake, runner = started
    _post(
        env,
        *(
            _fill(env, n, side, quantity, price, status=FeeStatus.FINAL)
            for n, (side, quantity, price, _cash) in enumerate(AMZN_FILLS)
        ),
    )
    with env.db.tx() as conn:
        assert env.ledger.fee_totals(conn, env.account.account_id) == 0
    fake.cash += BROKER_FILL_CASH
    _book_the_night(env, fake)
    result = runner._reconcile("paper-periodic")
    assert result.status == "PASSED", result.problems


def test_without_reading_the_charges_the_same_books_would_fail(started: Started) -> None:
    """The failure of 2026-11-11: the cash fell by the fees and nothing explained it."""
    env, fake, runner = started
    _post_session(env)
    fake.cash += BROKER_FILL_CASH - Decimal("0.02")  # the fees left the cash, no activity
    result = runner._reconcile("paper-periodic")
    assert result.status == "FAILED"
    assert any(problem.startswith("cash delta mismatch") for problem in result.problems)


def test_cash_that_no_fee_activity_explains_still_fails(started: Started) -> None:
    env, fake, runner = started
    _post_session(env)
    fake.cash += BROKER_FILL_CASH - Decimal("0.03")
    _book_the_night(env, fake)
    result = runner._reconcile("paper-periodic")
    assert result.status == "FAILED"
    assert any(
        problem.startswith("cash delta mismatch")
        and "(broker fee charges 0.02 allowed for)" in problem
        for problem in result.problems
    )


def test_a_fee_booked_before_the_baseline_is_already_in_it(started: Started) -> None:
    env, fake, runner = started
    _post_session(env)
    fake.cash += BROKER_FILL_CASH
    before = env.clock.now() - timedelta(hours=15)  # the night before the baseline
    activity = fake.charge_fee(
        Decimal("0.01"),
        trade_date=(before.date() - timedelta(days=1)).isoformat(),
        created_at=before,
    )
    fake.cash += Decimal("0.01")  # the baseline was read after this fee left the cash
    assert env.broker.list_fee_charges() == []
    result = runner._reconcile("paper-periodic")
    assert result.status == "PASSED", result.problems
    assert not any("fees charged by the broker" in note for note in result.notes)
    assert activity["status"] == "executed"


def test_a_canceled_fee_is_not_counted(started: Started) -> None:
    env, fake, runner = started
    _post_session(env)
    fake.cash += BROKER_FILL_CASH
    fake.charge_fee(
        Decimal("0.01"),
        trade_date=env.clock.now().date().isoformat(),
        created_at=env.clock.now() + NIGHT,
        status="canceled",
    )
    assert env.broker.list_fee_charges() == []
    assert runner._reconcile("paper-periodic").status == "PASSED"


def test_a_fee_in_a_status_nobody_mapped_fails_the_run(started: Started) -> None:
    env, fake, runner = started
    _post_session(env)
    fake.cash += BROKER_FILL_CASH
    fake.charge_fee(
        Decimal("0.01"),
        trade_date=env.clock.now().date().isoformat(),
        created_at=env.clock.now() + NIGHT,
        status="correct",
    )
    result = runner._reconcile("paper-periodic")
    assert result.status == "FAILED"
    assert result.broker_unavailable
    assert "status 'correct', which is not mapped" in result.problems[0]


def test_charges_come_oldest_first_across_pages(started: Started) -> None:
    env, fake, _runner = started
    start = env.clock.now()
    count = ACTIVITY_PAGE_SIZE + 50
    for n in range(count):
        day = start + timedelta(days=n)
        fake.charge_fee(Decimal("0.01"), trade_date=day.date().isoformat(), created_at=day + NIGHT)
    fake.calls.clear()
    charges = env.broker.list_fee_charges()
    assert len(charges) == count
    assert [c.charged_at for c in charges] == sorted(c.charged_at for c in charges)
    assert sum(c.amount for c in charges) == Decimal("0.01") * count
    assert fake.calls.count("list_fee_activities") == 2


def test_reading_stops_at_trade_dates_well_before_the_baseline(started: Started) -> None:
    env, fake, _runner = started
    start = env.clock.now()
    old = start - FEE_BOOKING_LAG - timedelta(days=1)
    for n in range(3 * ACTIVITY_PAGE_SIZE):
        day = old - timedelta(days=n)
        fake.charge_fee(Decimal("0.01"), trade_date=day.date().isoformat(), created_at=day + NIGHT)
    fake.charge_fee(Decimal("0.02"), trade_date=start.date().isoformat(), created_at=start + NIGHT)
    fake.calls.clear()
    charges = env.broker.list_fee_charges()
    assert [c.amount for c in charges] == [Decimal("0.02")]
    assert fake.calls.count("list_fee_activities") == 1  # the first page reached the old dates


def test_a_fee_activity_maps_to_a_positive_charge() -> None:
    raw = {
        "id": "20261110000000000::9e2dd356-1362-4b49-8f28-1115eef88306",
        "activity_type": "FEE",
        "activity_sub_type": "REG",
        "date": "2026-11-10",
        "created_at": "2026-11-11T01:16:23.783453Z",
        "net_amount": "-0.01",
        "description": "REG fee for proceed of $59.8 on 2026-11-10",
        "status": "executed",
        "currency": "USD",
    }
    charge = map_fee_activity(raw, currency="USD")
    assert charge is not None
    assert (charge.kind, charge.amount, charge.currency) == ("FEE:REG", Decimal("0.01"), "USD")
    assert charge.charged_at.isoformat() == "2026-11-11T01:16:23.783453+00:00"
    assert map_fee_activity({**raw, "status": "canceled"}, currency="USD") is None
    with pytest.raises(MappingError, match="is not a FEE"):
        map_fee_activity({**raw, "activity_type": "FILL"}, currency="USD")
    with pytest.raises(MappingError, match="net_amount"):
        map_fee_activity({**raw, "net_amount": None}, currency="USD")
