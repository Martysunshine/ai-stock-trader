"""Order manager: applies broker events and fills to intents, the ledger and reservations.

* Duplicate events are ignored (unique ``(broker, account_id, event_id)``).
* Fills are facts: every new execution is posted exactly once, even when its status event is
  stale, late, or arrives after a cancel request or a terminal status.
* Status follows ``trader.execution.states`` and never regresses; stale broker versions are
  ignored.
* A reservation is consumed by posted fills and released **only** for the quantity the broker
  confirmed it will never fill (terminal status with final cumulative quantity), once every fill
  up to that quantity is posted.
* An execution the system did not originate pauses entries (``UNSOLICITED_EXECUTION``).
* An adapter whose events cannot be read (``pump``) raises an alert and pauses entries
  (``UNKNOWN_ORDER``) instead of escaping the run loop (architecture.md §7).
* For an account configured to accrue modelled fees (ADR-0050), a fill the broker reports with
  no fee while its fee is pending is posted with the fee its schedule charges, as an accrual.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, insert, select, update

from trader.accounting.fees import FeeSchedule
from trader.accounting.ledger import Ledger, LedgerInvariantError
from trader.brokers.base import BrokerAdapter, BrokerError, BrokerTimeout
from trader.data.calendars import ExchangeCalendar, Utc247Calendar, us_equity_settlement_date
from trader.domain.hashing import canonical_json
from trader.domain.orders import BrokerOrderEvent, FeeStatus, Fill, OrderPurpose, OrderState
from trader.domain.types import ZERO, Track
from trader.execution.states import is_terminal, next_state
from trader.ops.clock import Clock
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.storage.db import Database
from trader.storage.helpers import insert_ignore
from trader.storage.schema import (
    alerts,
    broker_order_events,
    exit_plans,
    fills,
    order_intents,
    reservation_events,
    risk_reservations,
)


class OrderManager:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        pauses: PauseService,
        clock: Clock,
        calendars: dict[str, ExchangeCalendar | Utc247Calendar],
        tracks: dict[str, Track],
        modelled_fees: Mapping[str, FeeSchedule] | None = None,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.pauses = pauses
        self.clock = clock
        self.calendars = calendars
        self.tracks = tracks
        #: account id -> the schedule whose charge is accrued on fills reported without a fee
        self.modelled_fees: dict[str, FeeSchedule] = dict(modelled_fees or {})

    # ------------------------------------------------------------------ events
    def pump(self, adapter: BrokerAdapter) -> list[str]:
        """Drain one adapter's events and apply them in order; returns their dispositions.

        An adapter that cannot report its events - a ``BrokerError`` such as an Alpaca order in
        an unmapped status, or a transport failure - does not stop the run loop: an alert records
        it and entries pause (``UNKNOWN_ORDER``, cleared only by a PASSED reconciliation). The
        adapter keeps the events it could map and returns them on its next drain.
        """
        try:
            events = adapter.drain_events()
        except (BrokerError, BrokerTimeout, ConnectionError, TimeoutError) as exc:
            detail = f"broker events unavailable: {type(exc).__name__}: {exc}"[:2000]
            with self.db.tx() as conn:
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=adapter.account_id,
                    cause=PauseCause.UNKNOWN_ORDER,
                    details=detail,
                    now=self.clock.now(),
                )
                self._alert(conn, adapter.account_id, "BROKER_EVENTS_UNAVAILABLE", detail)
            return []
        return [self.apply_event(event) for event in events]

    def apply_event(self, event: BrokerOrderEvent) -> str:
        """Apply one broker event; returns its disposition (for audit and tests)."""
        now = self.clock.now()
        with self.db.tx() as conn:
            inserted = insert_ignore(
                conn,
                broker_order_events,
                {
                    "broker": event.broker,
                    "account_id": event.account_id,
                    "event_id": event.event_id,
                    "broker_order_id": event.broker_order_id,
                    "client_order_id": event.client_order_id,
                    "status": event.status.value,
                    "cum_filled_qty": event.cum_filled_quantity,
                    "broker_version": event.broker_version,
                    "event_time": event.event_time,
                    "received_at": now,
                    "disposition": "PENDING",
                    "raw_json": canonical_json(event.raw),
                },
                ["broker", "account_id", "event_id"],
            )
            if not inserted:
                return "DUPLICATE"
            disposition = self._apply(conn, event, now)
            conn.execute(
                update(broker_order_events)
                .where(
                    broker_order_events.c.broker == event.broker,
                    broker_order_events.c.account_id == event.account_id,
                    broker_order_events.c.event_id == event.event_id,
                )
                .values(disposition=disposition)
            )
            return disposition

    def _apply(self, conn: Connection, event: BrokerOrderEvent, now: datetime) -> str:
        intent = conn.execute(
            select(order_intents).where(
                order_intents.c.account_id == event.account_id,
                order_intents.c.client_order_id == event.client_order_id,
            )
        ).first()
        if intent is None:
            if event.fill is not None:
                self._post_fill(conn, event.fill, intent_id=None)
            self._unsolicited(
                conn, event.account_id, f"unknown client order {event.client_order_id}"
            )
            return "UNSOLICITED"

        disposition = "APPLIED"
        if event.fill is not None:
            posted = self.post_fill_for_intent(conn, intent, event.fill)
            disposition = "FILL_POSTED" if posted else "FILL_DUPLICATE"

        if event.broker_version <= int(intent.last_broker_version):
            self.sync_reservation(conn, intent.intent_id, now)
            return disposition if event.fill is not None else "STALE"

        current = OrderState(intent.state)
        new_state = (
            next_state(current, event.status)
            if current is not OrderState.PENDING_SUBMIT
            else current
        )
        values: dict[str, object] = {
            "last_broker_version": event.broker_version,
            "broker_order_id": event.broker_order_id,
            "updated_at": now,
        }
        if new_state is not current:
            values["state"] = new_state.value
        if event.status.is_terminal and intent.final_cum_qty is None:
            values["final_cum_qty"] = event.cum_filled_quantity
        conn.execute(
            update(order_intents)
            .where(order_intents.c.intent_id == intent.intent_id)
            .values(**values)
        )
        self.sync_reservation(conn, intent.intent_id, now)
        return disposition

    # ------------------------------------------------------------------ fills
    def post_fill_for_intent(self, conn: Connection, intent: object, fill: Fill) -> bool:
        i = intent._mapping  # type: ignore[attr-defined]
        if fill.instrument_id != i["instrument_id"] or fill.side.value != i["side"]:
            self._unsolicited(conn, fill.account_id, f"fill {fill.execution_id} mismatches intent")
            return False
        final = i["final_cum_qty"]
        posted_qty = self.posted_quantity(conn, i["intent_id"])
        if final is not None and posted_qty + fill.quantity > Decimal(final):
            # A broker fact beyond the confirmed final quantity: post it, but pause.
            self.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=fill.account_id,
                cause=PauseCause.RECONCILIATION_FAILED,
                details=f"execution {fill.execution_id} exceeds final quantity of {i['intent_id']}",
                now=self.clock.now(),
            )
        posted = self._post_fill(conn, fill, intent_id=i["intent_id"])
        if posted:
            cum = self.posted_quantity(conn, i["intent_id"])
            conn.execute(
                update(order_intents)
                .where(order_intents.c.intent_id == i["intent_id"])
                .values(cum_filled_qty=cum, updated_at=self.clock.now())
            )
            position = self.ledger.position(conn, fill.account_id, fill.instrument_id)
            if position is not None and position.quantity == 0:
                conn.execute(
                    update(exit_plans)
                    .where(
                        exit_plans.c.account_id == fill.account_id,
                        exit_plans.c.instrument_id == fill.instrument_id,
                    )
                    .values(active=False)
                )
        return posted

    def _post_fill(self, conn: Connection, fill: Fill, *, intent_id: str | None) -> bool:
        cal = self.calendars[fill.account_id]
        if isinstance(cal, ExchangeCalendar):
            trade_date = cal.session_date_of(fill.exec_time)
            settles = us_equity_settlement_date(trade_date, calendar=cal)
        else:
            trade_date = fill.exec_time.date()
            settles = trade_date
        try:
            result = self.ledger.post_fill(
                conn,
                self._with_modelled_fee(conn, fill),
                intent_id=intent_id,
                owner_track=self.tracks[fill.account_id],
                settles_on=settles,
                trade_date=trade_date,
            )
        except LedgerInvariantError as exc:
            self.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=fill.account_id,
                cause=PauseCause.RECONCILIATION_FAILED,
                details=f"ledger invariant: {exc}",
                now=self.clock.now(),
            )
            self._alert(conn, fill.account_id, "LEDGER_INVARIANT", str(exc))
            return False
        return result.posted

    def _with_modelled_fee(self, conn: Connection, fill: Fill) -> Fill:
        """The fill carrying the fee its account's schedule charges, when the broker gave none.

        Only for an account configured to accrue modelled fees, and only while the broker's fee
        is pending and zero: a fee the broker did report is never replaced. A schedule charges
        per order, so the fill takes the order's fee on everything executed so far less what its
        earlier fills already carry; an order filled in parts costs what a single fill would.
        """
        schedule = self.modelled_fees.get(fill.account_id)
        if (
            schedule is None
            or not schedule.known
            or fill.fee != 0
            or fill.fee_status is not FeeStatus.PENDING
        ):
            return fill
        earlier = conn.execute(
            select(fills.c.quantity, fills.c.price, fills.c.fee).where(
                fills.c.broker == fill.broker,
                fills.c.account_id == fill.account_id,
                fills.c.broker_order_id == fill.broker_order_id,
                fills.c.execution_id != fill.execution_id,
            )
        ).all()
        quantity = fill.quantity + sum((Decimal(r.quantity) for r in earlier), start=ZERO)
        notional = fill.quantity * fill.price + sum(
            (Decimal(r.quantity) * Decimal(r.price) for r in earlier), start=ZERO
        )
        booked = sum((Decimal(r.fee) for r in earlier), start=ZERO)
        fee = schedule.fee(fill.side, quantity, notional) - booked
        return fill.model_copy(update={"fee": fee}) if fee > 0 else fill

    def posted_quantity(self, conn: Connection, intent_id: str) -> Decimal:
        rows = conn.execute(
            select(fills.c.quantity).where(fills.c.intent_id == intent_id)
        ).scalars()
        return sum((Decimal(r) for r in rows), start=ZERO)

    # ------------------------------------------------------------------ reservations
    def sync_reservation(self, conn: Connection, intent_id: str, now: datetime) -> None:
        res = conn.execute(
            select(risk_reservations).where(risk_reservations.c.intent_id == intent_id)
        ).first()
        if res is None or res.state != "ACTIVE":
            return
        intent = conn.execute(
            select(order_intents).where(order_intents.c.intent_id == intent_id)
        ).one()
        consumed = self.posted_quantity(conn, intent_id)
        if consumed != Decimal(res.consumed_qty):
            conn.execute(
                insert(reservation_events).values(
                    reservation_id=res.reservation_id,
                    event_time=now,
                    kind="CONSUME",
                    quantity=consumed - Decimal(res.consumed_qty),
                    note="posted fills",
                )
            )
        values: dict[str, object] = {"consumed_qty": consumed, "updated_at": now}
        state = OrderState(intent.state)
        closable = state in (OrderState.NOT_SENT, OrderState.NOT_ACCEPTED, OrderState.REJECTED) or (
            is_terminal(state)
            and intent.final_cum_qty is not None
            and consumed >= Decimal(intent.final_cum_qty)
        )
        if closable:
            release = Decimal(res.reserved_qty) - consumed
            values.update(released_qty=release, state="CLOSED")
            conn.execute(
                insert(reservation_events).values(
                    reservation_id=res.reservation_id,
                    event_time=now,
                    kind="RELEASE",
                    quantity=release,
                    note=f"confirmed {state.value}; never-fillable remainder released",
                )
            )
            if consumed == 0 and intent.purpose == OrderPurpose.ENTRY.value:
                conn.execute(
                    update(exit_plans)
                    .where(exit_plans.c.entry_intent_id == intent_id)
                    .values(active=False)
                )
        conn.execute(
            update(risk_reservations)
            .where(risk_reservations.c.reservation_id == res.reservation_id)
            .values(**values)
        )

    # ------------------------------------------------------------------ helpers
    def _unsolicited(self, conn: Connection, account_id: str, detail: str) -> None:
        self.pauses.raise_flag(
            conn,
            scope=Scope.ACCOUNT,
            scope_id=account_id,
            cause=PauseCause.UNSOLICITED_EXECUTION,
            details=detail,
            now=self.clock.now(),
        )
        self._alert(conn, account_id, "UNSOLICITED", detail)

    def _alert(self, conn: Connection, account_id: str, code: str, message: str) -> None:
        conn.execute(
            insert(alerts).values(
                raised_at=self.clock.now(),
                severity="HIGH",
                code=code,
                account_id=account_id,
                message=message,
            )
        )
