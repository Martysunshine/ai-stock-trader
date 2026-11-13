"""Adversarial execution tests on the real replay pipeline (risk_and_validation.md §9;
architecture.md §7, §10; CRYPTO §9; COMMODITIES §10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.helpers import ENTRY_DECISION, SYNA, harness, quote
from trader.brokers.base import SimulatedCrash, UnauthorizedSubmission
from trader.brokers.replay.faults import FaultKind, FaultPlan, FaultSpec
from trader.domain.orders import ApprovedPlan, OrderState
from trader.storage.db import Database
from trader.storage.schema import broker_order_events, order_intents

T = ENTRY_DECISION


def _plan(fault: FaultSpec) -> FaultPlan:
    return FaultPlan(faults=(fault,))


def test_timeout_after_acceptance_reconciles_without_a_duplicate(sqlite_db: Database) -> None:
    h = harness(sqlite_db, faults=_plan(FaultSpec(kind=FaultKind.TIMEOUT_AFTER_ACCEPT)))
    h.run_until(T)
    entries = [i for i in h.intents() if i.purpose == "ENTRY"]
    assert len(entries) == 1
    assert OrderState(entries[0].state) in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED)
    assert h.env.broker.submission_attempts[entries[0].client_order_id] == 1
    assert "UNKNOWN_ORDER" not in h.flags()  # cleared by a PASSED reconciliation, not by retrying
    h.run_to_end()
    assert all(v == 1 for v in h.env.broker.submission_attempts.values())
    assert h.position() == 0


def test_timeout_before_acceptance_with_certified_visibility_is_not_resubmitted(
    sqlite_db: Database,
) -> None:
    h = harness(sqlite_db, faults=_plan(FaultSpec(kind=FaultKind.TIMEOUT_BEFORE_ACCEPT)))
    h.run_until(T + timedelta(seconds=20))
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    assert OrderState(entry.state) is OrderState.UNRESOLVED  # lookups alone never suffice
    assert [r.state for r in h.reservations()] == ["ACTIVE"]  # exposure stays reserved
    h.run_until(T + timedelta(minutes=3))
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    assert OrderState(entry.state) is OrderState.NOT_ACCEPTED  # certified lookups AND window
    assert h.env.broker.submission_attempts[entry.client_order_id] == 1  # never re-sent
    assert all(r.state == "CLOSED" for r in h.reservations())  # released only once established
    assert h.position() == 0


def test_delayed_visibility_keeps_entries_frozen_until_resolved(sqlite_db: Database) -> None:
    """Timeout plus a lost account stream: only REST lookups can resolve the order."""
    h = harness(
        sqlite_db,
        faults=FaultPlan(
            faults=(
                FaultSpec(kind=FaultKind.TIMEOUT_AFTER_ACCEPT),
                FaultSpec(kind=FaultKind.SUPPRESS_STREAM_EVENTS),
                FaultSpec(kind=FaultKind.DELAYED_VISIBILITY, lookups=2),
            )
        ),
    )
    h.run_until(T)
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    assert OrderState(entry.state) is OrderState.UNRESOLVED
    assert "UNRESOLVED_ORDER" in h.flags() or "UNKNOWN_ORDER" in h.flags()
    h.run_until(T + timedelta(minutes=2))
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    assert OrderState(entry.state) not in (OrderState.UNRESOLVED, OrderState.UNKNOWN_OUTCOME)
    assert h.env.broker.submission_attempts[entry.client_order_id] == 1
    # fills that never arrived on the stream were recovered from REST executions
    assert h.position() == h.env.broker.get_positions()[0].quantity


def test_crash_after_acceptance_recovers_one_intent_and_one_position(sqlite_db: Database) -> None:
    h = harness(sqlite_db, faults=_plan(FaultSpec(kind=FaultKind.CRASH_AFTER_ACCEPT)))
    with pytest.raises(SimulatedCrash):
        h.run_until(T)
    h.index += 1  # the crashing event was already delivered to the broker
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    assert OrderState(entry.state) is OrderState.SUBMITTING  # persisted before the network call
    h2 = h.restart()
    entry = next(i for i in h2.intents() if i.purpose == "ENTRY")
    assert OrderState(entry.state) is OrderState.ACKNOWLEDGED
    assert "RECOVERY_PENDING" not in h2.flags()
    h2.run_until(T + timedelta(minutes=2))
    assert len([i for i in h2.intents() if i.purpose == "ENTRY"]) == 1
    assert h2.env.broker.submission_attempts[entry.client_order_id] == 1
    assert h2.position() > 0
    h2.run_to_end()
    assert h2.position() == 0


def test_duplicate_and_out_of_order_events_never_double_count(sqlite_db: Database) -> None:
    h = harness(
        sqlite_db,
        faults=FaultPlan(
            faults=(
                FaultSpec(kind=FaultKind.DUPLICATE_EVENTS),
                FaultSpec(kind=FaultKind.OUT_OF_ORDER_EVENTS),
                FaultSpec(kind=FaultKind.PARTIAL_FIRST_FILL, fraction=Decimal("0.5")),
            )
        ),
    )
    h.run_until(T + timedelta(minutes=5))
    broker_qty = h.env.broker.get_positions()[0].quantity
    assert h.position() == broker_qty  # ledger == broker despite duplicates/reordering
    fills = h.fills()
    assert len({f.execution_id for f in fills}) == len(fills)
    with h.env.db.tx() as conn:
        dispositions = [r.disposition for r in conn.execute(select(broker_order_events))]
    assert "DUPLICATE" not in dispositions  # duplicates never reach the table...
    assert h.env.broker.submission_attempts and all(
        v == 1 for v in h.env.broker.submission_attempts.values()
    )


def test_partial_fill_then_cancel_releases_only_the_confirmed_remainder(
    sqlite_db: Database,
) -> None:
    h = harness(
        sqlite_db,
        faults=_plan(FaultSpec(kind=FaultKind.PARTIAL_FIRST_FILL, fraction=Decimal("0.4"))),
    )
    h.run_until(T + timedelta(minutes=1))
    res = h.reservations()[0]
    assert res.state == "ACTIVE" and Decimal(res.consumed_qty) > 0  # still live exposure
    h.run_until(T + timedelta(minutes=3))
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    res = h.reservations()[0]
    assert OrderState(entry.state) is OrderState.CANCELED
    assert res.state == "CLOSED"
    assert Decimal(res.consumed_qty) == Decimal(entry.cum_filled_qty) == h.position()
    assert Decimal(res.consumed_qty) + Decimal(res.released_qty) == Decimal(res.reserved_qty)


def test_fill_after_cancel_request_is_recognised_and_exited(sqlite_db: Database) -> None:
    h = harness(
        sqlite_db,
        faults=FaultPlan(
            faults=(
                FaultSpec(kind=FaultKind.PARTIAL_FIRST_FILL, fraction=Decimal("0.4")),
                FaultSpec(kind=FaultKind.FILL_AFTER_CANCEL_REQUEST),
            )
        ),
    )
    h.run_until(T + timedelta(seconds=40))  # partial fill done, rest working
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    assert h.env.gateway.request_cancel(entry.intent_id)
    with h.env.db.tx() as conn:
        state = conn.execute(
            select(order_intents.c.state).where(order_intents.c.intent_id == entry.intent_id)
        ).scalar_one()
    assert state == OrderState.CANCEL_REQUESTED.value  # requested is not canceled
    # the next quote is inside the limit: the broker fills the remainder before honouring the cancel
    h.step(quote(SYNA, datetime(2026, 11, 27, 15, 36, 0, tzinfo=UTC), "101.20", "101.24", 90001))
    entry = next(i for i in h.intents() if i.intent_id == entry.intent_id)
    assert Decimal(entry.cum_filled_qty) == Decimal(entry.quantity)
    assert h.position() == Decimal(entry.quantity)  # the late fill is a real, managed position
    h.run_to_end()
    assert h.position() == 0


def test_manual_broker_position_is_a_discrepancy_that_pauses(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    h.run_until(datetime(2026, 11, 27, 15, 10, tzinfo=UTC))
    h.env.broker.inject_manual_position(SYNA, Decimal("0.5"), Decimal("100.5"))
    result = h.env.reconciler.reconcile(h.account)
    assert result.status == "FAILED"
    assert "RECONCILIATION_FAILED" in h.flags()
    h.run_until(T)
    assert not [i for i in h.intents() if i.purpose == "ENTRY"]  # no entries while discrepant


def test_only_the_gateway_authority_can_submit(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    h.run_until(T)
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    plan = ApprovedPlan.model_validate_json(entry.plan_json)
    broker = h.env.broker
    with pytest.raises(UnauthorizedSubmission):
        broker.submit(plan, authority=object())
    forged = plan.model_copy(
        update={"intent": plan.intent.model_copy(update={"quantity": Decimal(5)})}
    )
    with pytest.raises(UnauthorizedSubmission):
        broker.submit(forged, authority=h.env.gateway._authority)
    expired = plan.model_copy()
    h.env.clock.advance_to(plan.valid_until + timedelta(seconds=1))
    with pytest.raises(UnauthorizedSubmission):
        broker.submit(expired, authority=h.env.gateway._authority)
    for field in ("account_version", "policy_hash", "model_hash", "valid_until"):
        value: object = {
            "account_version": plan.account_version + 1,
            "policy_hash": "x" * 64,
            "model_hash": "other",
            "valid_until": plan.valid_until + timedelta(hours=1),
        }[field]
        assert not plan.model_copy(update={field: value}).is_authentic()


def test_stale_worker_cannot_submit_after_a_new_gateway_binds(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    old_authority = h.env.gateway._authority
    h.run_until(T)
    h2 = h.restart()
    entry = next(i for i in h2.intents() if i.purpose == "ENTRY")
    plan = ApprovedPlan.model_validate_json(entry.plan_json)
    with pytest.raises(UnauthorizedSubmission):
        h2.env.broker.submit(plan, authority=old_authority)
