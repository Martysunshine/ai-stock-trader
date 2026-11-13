"""Reconciliation while a fill is in flight (execution/reconciliation.py, runtime/decision_loop.py).

2026-11-10 15:50:03 UTC: the stop exit for AMZN filled at the broker in the same second in which
a reconciliation was reading the account. The fill was not in the ledger yet, so the run failed
(``broker execution ... not in ledger; position ...: ledger 0.0733 vs broker 0; cash delta
mismatch``) and entries were paused for a minute over nothing.

These tests drive the real Alpaca adapter over the in-memory fake API (no network, SQLite) and
show that such a fill gets a short, bounded time to reach the ledger before anything is
recorded, while every difference the bot cannot explain by its own order fails, pauses and
alerts exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tests.test_paper_runtime import (
    ACCOUNT,
    DAY,
    FAST,
    FakeClock,
    FakeMarketData,
    _alpaca_env,
)
from trader.domain.orders import FeeStatus, Fill, OrderPurpose
from trader.domain.types import Side
from trader.execution.exits import ExitTrigger
from trader.execution.reconciliation import IN_FLIGHT_WINDOW
from trader.runtime.decision_loop import (
    IN_FLIGHT_RETRIES,
    IN_FLIGHT_RETRY_SECONDS,
    DecisionLoop,
)
from trader.runtime.paper import PaperEnvironment, PaperRunner
from trader.storage.db import Database
from trader.storage.schema import alerts, order_intents, reconciliation_runs

INSTRUMENT = "XNAS:AAPL"
SYMBOL = "AAPL"
QTY = Decimal("0.1")
PRICE = Decimal("200")


@dataclass
class Rig:
    """A started paper runner holding 0.1 AAPL, in the ledger and at the (fake) broker."""

    db: Database
    clock: FakeClock
    env: PaperEnvironment
    fake: Any
    runner: PaperRunner
    logs: list[str]
    #: Runs recorded before the test began (startup recovery and the fixture's clean run).
    earlier_runs: set[str] = field(default_factory=set)

    def run_ids(self) -> set[str]:
        with self.db.tx() as conn:
            return set(conn.execute(select(reconciliation_runs.c.run_id)).scalars())

    def flags(self) -> list[str]:
        with self.db.tx() as conn:
            return sorted(f.cause.value for f in self.env.pauses.all_active(conn))

    def alert_codes(self) -> list[str]:
        with self.db.tx() as conn:
            return list(conn.execute(select(alerts.c.code)).scalars())

    def runs(self) -> list[tuple[str, dict[str, list[str]]]]:
        """The runs the test itself recorded, oldest first: (status, summary)."""
        with self.db.tx() as conn:
            rows = conn.execute(
                select(reconciliation_runs).order_by(
                    reconciliation_runs.c.finished_at, reconciliation_runs.c.started_at
                )
            ).all()
        return [
            (r.status, json.loads(r.summary_json))
            for r in rows
            if r.run_id not in self.earlier_runs
        ]

    def ledger_position(self) -> Decimal:
        with self.db.tx() as conn:
            held = self.env.ledger.position(conn, ACCOUNT, INSTRUMENT)
        return Decimal(0) if held is None else held.quantity

    def sell(self) -> str:
        """The bot's own exit: a market sell of the holding, acknowledged and working."""
        created = self.env.exits.act(
            [
                ExitTrigger(
                    account_id=ACCOUNT,
                    instrument_id=INSTRUMENT,
                    purpose=OrderPurpose.EXIT_RISK_REDUCTION,
                    reason="stop",
                    entry_intent_id=None,
                )
            ],
            self.env.market.halted,
        )
        assert len(created) == 1
        self.runner.loop.pump()
        with self.db.tx() as conn:
            states = list(conn.execute(select(order_intents.c.state)).scalars())
        assert states == ["ACKNOWLEDGED"]
        (boid,) = self.fake.orders
        return str(boid)


@pytest.fixture
def lite(tmp_path: Path) -> Iterator[Database]:
    db = Database(f"sqlite:///{(tmp_path / 'in_flight.db').as_posix()}")
    db.create_schema()
    yield db
    db.dispose()


@pytest.fixture
def rig(lite: Database) -> Iterator[Rig]:
    clock = FakeClock(DAY.replace(hour=16))
    env, fake = _alpaca_env(lite, clock)
    logs: list[str] = []
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST, log=logs.append)
    runner.start()
    assert fake.stream_ready.wait(5), "fake trade stream did not start"
    fake.set_position(SYMBOL, QTY, PRICE)
    fake.cash -= QTY * PRICE
    with lite.tx() as conn:
        env.oms._post_fill(
            conn,
            Fill(
                broker=env.broker.broker,
                account_id=ACCOUNT,
                execution_id="seed-1",
                broker_order_id="seed",
                client_order_id="seed",
                instrument_id=INSTRUMENT,
                side=Side.BUY,
                quantity=QTY,
                price=PRICE,
                currency="USD",
                fee=Decimal(0),
                fee_currency="USD",
                fee_status=FeeStatus.FINAL,
                exec_time=clock.now(),
                receipt_time=clock.now(),
            ),
            intent_id=None,
        )
    assert runner._reconcile("paper-periodic").status == "PASSED"  # the starting point is clean
    rig = Rig(db=lite, clock=clock, env=env, fake=fake, runner=runner, logs=logs)
    rig.earlier_runs = rig.run_ids()
    yield rig
    runner.shutdown()  # stops the stream thread
    env.close()


def _assert_settled(rig: Rig, *, waited: str) -> None:
    """One PASSED run, recorded with what the first comparison found; nothing else happened."""
    assert rig.flags() == []
    assert rig.alert_codes() == []
    assert rig.ledger_position() == 0
    assert rig.env.exits.pending_liquidation() == {}
    (status, summary), *earlier = reversed(rig.runs())
    assert status == "PASSED" and all(s == "PASSED" for s, _ in earlier)
    assert summary["problems"] == []
    (note,) = [n for n in summary["notes"] if "fill in flight" in n]
    assert note.startswith(f"waited {waited} s for a fill in flight; the first comparison found: ")
    assert "position XNAS:AAPL: ledger 0.1 vs broker 0" in note
    assert "cash delta mismatch" in note


# --------------------------------------------------------------------------- a fill in flight


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "rest-only"])
def test_a_fill_landing_between_the_broker_reads_is_not_a_failure(rig: Rig, stream: bool) -> None:
    """The 2026-11-10 failure: open orders are read, the sell fills, then positions, executions
    and cash are read. The fill reaches the ledger one second later (by the stream, or - when
    the stream is silent - by the order's REST view) and the comparison is clean."""
    boid = rig.sell()
    rig.fake.before_next["list_positions"] = lambda: rig.fake.fill(boid, QTY, PRICE, stream=stream)
    before = rig.clock.now()
    result = rig.runner._reconcile("paper-periodic")
    assert result.status == "PASSED" and result.problems == []
    assert result.waited_seconds == 1
    assert rig.clock.now() - before == timedelta(seconds=1)
    assert rig.runner.summary.reconciliations == {"PASSED": 3}  # startup, fixture, this one
    _assert_settled(rig, waited="1")
    note = rig.runs()[-1][1]["notes"][-1]
    assert "broker execution " in note and " not in ledger" in note
    assert any(
        line.endswith("reconciliation PASSED after waiting 1 s for a fill in flight")
        for line in rig.logs
    )
    assert not any("FAILED" in line for line in rig.logs)
    again = rig.runner._reconcile("paper-periodic")  # the ledger stays consistent
    assert again.status == "PASSED" and again.waited_seconds == 0


def test_the_reconciliation_inside_the_decision_cycle_waits_too(rig: Rig) -> None:
    """With an order working, the cycle itself reconciles every 60 s (``resolve_unknowns``):
    the same fill in flight must not fail that run either."""
    boid = rig.sell()
    rig.clock.sleep(61)
    rig.fake.before_next["list_positions"] = lambda: rig.fake.fill(boid, QTY, PRICE)
    before = rig.clock.now()
    rig.runner.loop.resolve_unknowns()
    assert rig.clock.now() - before == timedelta(seconds=1)
    _assert_settled(rig, waited="1")


def test_a_fill_record_that_lags_its_order_status_is_given_time(rig: Rig) -> None:
    """The stream says "filled" but the broker's fill record is not readable yet: the order is
    final in the ledger, its fill is not. The order became final moments ago, so the position
    and cash differences are explained; the record appears and the next comparison is clean."""
    boid = rig.sell()
    rig.fake.hidden_activities.add(rig.fake.fill(boid, QTY, PRICE))

    def record_appears(seconds: float) -> None:
        rig.fake.hidden_activities.clear()
        FakeClock.sleep(rig.clock, seconds)

    rig.clock.sleep = record_appears  # type: ignore[method-assign]
    result = rig.runner._reconcile("paper-periodic")
    assert result.status == "PASSED" and result.waited_seconds == 1
    _assert_settled(rig, waited="1")


def test_an_order_final_for_longer_than_the_window_explains_nothing(rig: Rig) -> None:
    """The same missing fill record, but the order has been final for 11 s: that is no longer
    a fill in flight. The run fails at once, without waiting."""
    boid = rig.sell()
    rig.fake.hidden_activities.add(rig.fake.fill(boid, QTY, PRICE))
    rig.runner.loop.pump()  # the order is FILLED in the ledger; its fill is not there
    rig.clock.sleep(IN_FLIGHT_WINDOW.total_seconds() + 1)
    before = rig.clock.now()
    result = rig.runner._reconcile("paper-periodic")
    assert result.status == "FAILED" and result.waited_seconds == 0
    assert rig.clock.now() == before
    assert result.problems[0] == "position XNAS:AAPL: ledger 0.1 vs broker 0"
    assert rig.flags() == ["RECONCILIATION_FAILED"]


# --------------------------------------------------------------------------- real mismatches


def _foreign_fill(rig: Rig) -> None:
    """A trade the bot did not originate: 1 MSFT bought by hand at the broker."""
    boid = rig.fake.add_order(
        symbol="MSFT",
        side="buy",
        qty="1",
        status="new",
        client_order_id="by-hand-1",
        at=rig.clock.now(),
    )
    rig.fake.fill(boid, Decimal(1), Decimal(400), stream=False)


def test_an_execution_the_bot_did_not_originate_fails_at_once(rig: Rig) -> None:
    """No order of this account explains it: FAILED, paused and alerted as before, no wait."""
    _foreign_fill(rig)
    before = rig.clock.now()
    result = rig.runner._reconcile("paper-periodic")
    assert result.status == "FAILED" and result.waited_seconds == 0
    assert not result.in_flight_only
    assert rig.clock.now() == before
    assert len(result.problems) == 3
    assert result.problems[0].startswith("broker execution ")
    assert result.problems[0].endswith(" not in ledger")
    assert result.problems[1] == "position XNAS:MSFT: ledger 0 vs broker 1"
    assert result.problems[2].startswith("cash delta mismatch: ")
    status, summary = rig.runs()[-1]
    assert status == "FAILED" and summary == {"problems": result.problems, "notes": []}
    assert "RECONCILIATION_FAILED" in rig.flags()
    with rig.db.tx() as conn:
        (flag,) = [
            f for f in rig.env.pauses.all_active(conn) if f.cause.value == "RECONCILIATION_FAILED"
        ]
    assert flag.details == "; ".join(result.problems)
    # the event pump then meets the unsolicited execution: its own pause and HIGH alert
    assert rig.flags() == ["RECONCILIATION_FAILED", "UNSOLICITED_EXECUTION"]
    assert rig.alert_codes() == ["UNSOLICITED"]
    assert any("reconciliation FAILED: broker execution " in line for line in rig.logs)


def test_one_unexplained_difference_fails_at_once_even_with_a_fill_in_flight(rig: Rig) -> None:
    boid = rig.sell()
    _foreign_fill(rig)
    rig.fake.before_next["list_positions"] = lambda: rig.fake.fill(boid, QTY, PRICE)
    before = rig.clock.now()
    result = rig.runner._reconcile("paper-periodic")
    assert result.status == "FAILED" and result.waited_seconds == 0
    assert rig.clock.now() == before
    assert "RECONCILIATION_FAILED" in rig.flags()


# --------------------------------------------------------------------------- the wait is bounded


def test_the_wait_is_bounded_and_a_lasting_difference_fails_as_before(rig: Rig) -> None:
    """The broker's position differs from the ledger on an instrument with a working order and
    stays different: after the bounded wait the run fails, pauses and is logged as before."""
    rig.sell()
    rig.fake.set_position(SYMBOL, Decimal("0.05"), PRICE)  # no execution explains it
    comparisons = rig.fake.calls.count("get_account")
    before = rig.clock.now()
    result = rig.runner._reconcile("paper-periodic")
    assert result.status == "FAILED" and result.in_flight_only
    assert result.problems == ["position XNAS:AAPL: ledger 0.1 vs broker 0.05"]
    assert IN_FLIGHT_RETRIES == 3 and IN_FLIGHT_RETRY_SECONDS == 1
    assert rig.clock.now() - before == timedelta(seconds=3)
    assert rig.fake.calls.count("get_account") - comparisons == 4  # the first and three more
    assert result.waited_seconds == 3
    status, summary = rig.runs()[-1]
    assert status == "FAILED" and summary["problems"] == result.problems
    assert rig.flags() == ["RECONCILIATION_FAILED"]
    assert any(
        line.endswith("reconciliation FAILED: position XNAS:AAPL: ledger 0.1 vs broker 0.05")
        for line in rig.logs
    )
    # already paused for a failed reconciliation: there is no false pause left to avoid
    before = rig.clock.now()
    assert rig.runner._reconcile("paper-periodic").status == "FAILED"
    assert rig.clock.now() == before
    # the difference is gone: the next run passes and clears the pause, as always
    rig.fake.set_position(SYMBOL, QTY, PRICE)
    assert rig.runner._reconcile("paper-periodic").status == "PASSED"
    assert rig.flags() == []


def test_comparing_records_nothing_and_only_a_loop_that_can_wait_waits(rig: Rig) -> None:
    """``compare`` writes no run, pause or alert. ``Reconciler.reconcile`` (startup recovery)
    and a decision loop without a way to wait (replay) record the first verdict at once."""
    rig.sell()
    rig.fake.set_position(SYMBOL, Decimal("0.05"), PRICE)
    runs = len(rig.runs())
    verdict = rig.env.reconciler.compare(ACCOUNT)
    assert verdict.status == "FAILED" and verdict.in_flight_only
    assert len(rig.runs()) == runs and rig.flags() == [] and rig.alert_codes() == []

    before = rig.clock.now()
    strict = rig.env.reconciler.reconcile(ACCOUNT, cleared_by="startup-recovery")
    assert strict.status == "FAILED" and rig.clock.now() == before
    assert rig.flags() == ["RECONCILIATION_FAILED"]
    assert [status for status, _ in rig.runs()[runs:]] == ["FAILED"]

    rig.fake.set_position(SYMBOL, QTY, PRICE)
    assert rig.runner._reconcile("paper-periodic").status == "PASSED"  # clears the pause
    assert rig.flags() == []
    rig.clock.sleep(5)  # a new instant: pause flag ids are derived from the time they are raised
    rig.fake.set_position(SYMBOL, Decimal("0.05"), PRICE)
    before = rig.clock.now()
    replay_like = DecisionLoop(rig.env).reconcile(cleared_by="runner")
    assert replay_like.status == "FAILED" and replay_like.waited_seconds == 0
    assert rig.clock.now() == before
    assert rig.flags() == ["RECONCILIATION_FAILED"]
