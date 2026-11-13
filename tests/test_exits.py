"""Exit path invariants (instructions.md §5; architecture.md §8; risk §9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.helpers import ENTRY_DECISION, SYNA, harness, quote
from trader.domain.orders import OrderPurpose, OrderState
from trader.ops.pause import PauseCause, Scope
from trader.storage.db import Database

T = ENTRY_DECISION


def test_stop_while_entries_are_paused_still_exits_without_overselling(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    h.run_until(T + timedelta(minutes=1))
    owned = h.position()
    assert owned > 0
    with h.env.db.tx() as conn:  # an owner pause blocks entries only
        h.env.pauses.owner_pause(
            conn, portfolio_id=h.env.portfolio_id, reason="test", by="t", now=h.env.clock.now()
        )
    t = datetime(2026, 11, 27, 15, 37, 0, tzinfo=UTC)
    h.step(quote(SYNA, t, "100.50", "100.54", 91001))  # through the stop at 100.93
    exits = [i for i in h.intents() if i.purpose == OrderPurpose.EXIT_STOP.value]
    assert len(exits) == 1
    assert Decimal(exits[0].quantity) == owned  # never more than owned
    h.step(quote(SYNA, t + timedelta(seconds=30), "100.40", "100.44", 91002))
    assert h.position() == 0
    assert "OWNER_REQUESTED" in h.flags()  # the pause itself remains


def test_time_exit_racing_a_stop_never_creates_two_sells(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    h.run_until(T + timedelta(minutes=1))
    owned = h.position()
    # jump past the maximum hold AND through the stop on the same quote
    t = T + timedelta(minutes=61)
    h.env.clock.advance_to(t)
    h.step(quote(SYNA, t, "100.50", "100.54", 92001))
    h.step(quote(SYNA, t + timedelta(seconds=1), "100.40", "100.44", 92002))
    sells = [i for i in h.intents() if i.side == "SELL"]
    assert sum(Decimal(s.quantity) for s in sells) == owned
    assert h.position() == 0


def test_stale_quote_blocks_entries_but_not_exits(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    h.run_until(T + timedelta(minutes=1))
    with h.env.db.tx() as conn:
        h.env.pauses.raise_flag(
            conn,
            scope=Scope.ACCOUNT,
            scope_id=h.account,
            cause=PauseCause.FEED_GAP,
            details="test",
            now=h.env.clock.now(),
        )
    exits = h.env.exits.act(
        [
            type(
                "T",
                (),
                {
                    "account_id": h.account,
                    "instrument_id": SYNA,
                    "purpose": OrderPurpose.EXIT_RISK_REDUCTION,
                    "reason": "test",
                    "entry_intent_id": None,
                },
            )()
        ],  # type: ignore[list-item]
        set(),
    )
    assert len(exits) == 1


def test_halt_and_gap_through_stop_is_reported_honestly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = Database(f"sqlite:///{(tmp_path / 'gap.db').as_posix()}")
    db.create_schema()
    h = harness(db, session_name="synthetic_gap_v1")
    h.run_until(datetime(2026, 11, 27, 16, 15, tzinfo=UTC))
    sells = [i for i in h.intents() if i.side == "SELL"]
    assert len(sells) == 1 and OrderState(sells[0].state) is OrderState.FILLED
    fill = next(f for f in h.fills() if f.side == "SELL")
    assert Decimal(fill.price) < Decimal("100.93")  # executed far below the planned stop
    assert Decimal(fill.realized_net) < Decimal("-0.125")  # loss exceeded the planned risk budget
    assert {"DAILY_NY", "DAILY_UTC"} <= set(h.flags())
    db.dispose()
