"""Reconciliation and unknown-outcome resolution (architecture.md §5, §7, §9).

* ``resolve_unknown`` queries by the persisted client order ID, then applies the broker's view
  (order snapshot + executions). Absence establishes non-acceptance only after the adapter's
  *certified* number of lookups; otherwise the intent stays ``UNRESOLVED`` and entries stay
  frozen. Nothing is ever resubmitted with a new ID.
* ``reconcile`` compares positions, open orders, executions and the cash subledger delta with
  the broker. Cent-level differences are explained as rounding; anything larger, any unknown
  order/position/execution, or any unresolved intent fails the run and persists a pause. A
  PASSED run clears HEALTH flags raised before it started (never loss-breach flags).
  Fee accruals still pending are added back to the ledger's cash first: the ledger takes an
  estimate at the fill, the broker charges later or, on a paper account, never (ADR-0050).
* ``reconcile`` is ``compare`` (read the broker, return the verdict, record nothing) followed by
  ``record`` (the run row; a pause when FAILED, an alert when the broker could not be read;
  cleared health flags when PASSED). The broker is read in several requests, not one snapshot,
  so a fill that lands in between makes its execution, position and cash disagree with the
  ledger for a moment. ``compare`` therefore says whether *every* problem it found is explained
  by an order of this account whose fill may still be on its way (``in_flight_only``). A caller
  that can wait (the paper decision loop) may then compare again before recording; the verdict
  rule itself never changes, and only a comparison without any problem passes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Connection, and_, insert, or_, select, update

from trader.accounting.ledger import Ledger
from trader.brokers.base import BrokerAdapter, BrokerError, BrokerTimeout, OrderScope
from trader.domain.hashing import canonical_json
from trader.domain.ids import deterministic_id
from trader.domain.orders import (
    LIVE_STATES,
    BrokerOrder,
    BrokerOrderEvent,
    OrderState,
)
from trader.domain.types import CENT, ZERO
from trader.execution.oms import OrderManager
from trader.ops.clock import Clock
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.risk.allocator import ENTRY_PLAN_VALIDITY
from trader.storage.db import Database
from trader.storage.schema import accounts, alerts, fills, order_intents, reconciliation_runs

UNKNOWN_STATES = (OrderState.SUBMITTING, OrderState.UNKNOWN_OUTCOME, OrderState.UNRESOLVED)
#: States in which the broker holds the order and may fill it at any moment.
WORKING_STATES = (
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_REQUESTED,
)
#: Final states an order can reach with a fill still on its way to the ledger.
FILLABLE_FINAL_STATES = (OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED)


def broker_cent_rounding(executions: Iterable[tuple[str, Decimal, Decimal]]) -> Decimal:
    """How far cent rounding has moved the broker's cash from the ledger's (broker minus ledger).

    The broker books each execution's traded value rounded to the cent; the ledger keeps the
    exact product of quantity and price. With fractional quantities every fill therefore
    differs by up to half a cent, and the differences add up: on 2026-11-10 four AMZN fills
    left the broker at -0.21 USD and the ledger at -0.198125 USD, more than the one-cent
    tolerance, and every later reconciliation failed. The per-fill differences are known
    exactly, so they are computed rather than covered by a tolerance that grows with trading.
    ``executions`` are ``(side, quantity, price)`` of the account's fills.
    """
    total = ZERO
    for side, quantity, price in executions:
        exact = Decimal(quantity) * Decimal(price)
        booked = exact.quantize(CENT, rounding=ROUND_HALF_UP)
        total += (exact - booked) if side == "BUY" else (booked - exact)
    return total


#: How long after an order became final its fill, position and cash may still be settling.
IN_FLIGHT_WINDOW = timedelta(seconds=10)


@dataclass
class ReconciliationResult:
    run_id: str
    status: str
    #: When the comparison started. A PASSED run clears only health flags raised up to then.
    started_at: datetime
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cleared_flags: list[str] = field(default_factory=list)
    #: FAILED, and every problem is explained by an order of this account that is working at
    #: the broker or became final moments ago: its fill may not have reached the ledger yet.
    in_flight_only: bool = False
    #: The broker could not be read (recording this result also raises an alert).
    broker_unavailable: bool = False
    #: Seconds the caller waited for an in-flight fill before this comparison (0: none).
    waited_seconds: float = 0.0


class Reconciler:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        oms: OrderManager,
        pauses: PauseService,
        adapters: dict[str, BrokerAdapter],
        clock: Clock,
        portfolio_id: str,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.oms = oms
        self.pauses = pauses
        self._adapters = adapters
        self.clock = clock
        self.portfolio_id = portfolio_id
        self._run_seq = 0

    # ------------------------------------------------------------------ unknowns
    def resolve_unknown(self, account_id: str) -> list[tuple[str, OrderState]]:
        adapter = self._adapters[account_id]
        caps = adapter.capabilities()
        lookups = caps.order_visibility_lookups
        window = caps.order_visibility_window_seconds
        with self.db.tx() as conn:
            rows = conn.execute(
                select(order_intents)
                .where(
                    order_intents.c.account_id == account_id,
                    order_intents.c.state.in_(tuple(s.value for s in UNKNOWN_STATES)),
                )
                .order_by(order_intents.c.seq)
            ).all()
        outcomes = []
        for row in rows:
            broker_order = adapter.get_order_by_client_id(row.client_order_id)
            now = self.clock.now()
            if broker_order is not None:
                self._apply_snapshot(broker_order, adapter)
                with self.db.tx() as conn:
                    state = OrderState(self._state(conn, row.intent_id))
                outcomes.append((row.intent_id, state))
                continue
            with self.db.tx() as conn:
                attempts = int(row.lookup_attempts) + 1
                values: dict[str, object] = {"lookup_attempts": attempts, "updated_at": now}
                # Non-acceptance needs the certified lookup count AND the certified visibility
                # window, measured conservatively from approval plus the maximum plan validity.
                elapsed = (now - row.created_at).total_seconds()
                certified = lookups is not None and window is not None
                if (
                    certified
                    and attempts >= (lookups or 0)
                    and elapsed >= (window or 0) + ENTRY_PLAN_VALIDITY.total_seconds()
                ):
                    values.update(state=OrderState.NOT_ACCEPTED.value, final_cum_qty=ZERO)
                    new_state = OrderState.NOT_ACCEPTED
                else:
                    values["state"] = OrderState.UNRESOLVED.value
                    new_state = OrderState.UNRESOLVED
                    self.pauses.raise_flag(
                        conn,
                        scope=Scope.ACCOUNT,
                        scope_id=account_id,
                        cause=PauseCause.UNRESOLVED_ORDER,
                        details=f"{row.client_order_id} not visible after {attempts} lookup(s)",
                        now=now,
                    )
                conn.execute(
                    update(order_intents)
                    .where(order_intents.c.intent_id == row.intent_id)
                    .values(**values)
                )
                if new_state is OrderState.NOT_ACCEPTED:
                    self.oms.sync_reservation(conn, row.intent_id, now)
            outcomes.append((row.intent_id, new_state))
        return outcomes

    def _apply_snapshot(self, order: BrokerOrder, adapter: BrokerAdapter) -> None:
        """Apply a REST view of an order: its executions first, then its status."""
        for fill in adapter.list_executions(broker_order_id=order.broker_order_id):
            self.oms.apply_event(
                BrokerOrderEvent(
                    broker=order.broker,
                    account_id=order.account_id,
                    event_id=f"rest-fill:{fill.execution_id}",
                    broker_order_id=order.broker_order_id,
                    client_order_id=order.client_order_id,
                    status=order.status,
                    order_quantity=order.quantity,
                    cum_filled_quantity=order.filled_quantity,
                    event_time=order.updated_at,
                    broker_version=0,
                    fill=fill,
                    raw={"source": "rest-executions"},
                )
            )
        self.oms.apply_event(
            BrokerOrderEvent(
                broker=order.broker,
                account_id=order.account_id,
                event_id=f"rest:{order.broker_order_id}:v{order.broker_version}",
                broker_order_id=order.broker_order_id,
                client_order_id=order.client_order_id,
                status=order.status,
                order_quantity=order.quantity,
                cum_filled_quantity=order.filled_quantity,
                event_time=order.updated_at,
                broker_version=order.broker_version,
                raw={"source": "rest-order"},
            )
        )

    @staticmethod
    def _state(conn: Connection, intent_id: str) -> str:
        return str(
            conn.execute(
                select(order_intents.c.state).where(order_intents.c.intent_id == intent_id)
            ).scalar_one()
        )

    # ------------------------------------------------------------------ full run
    def reconcile(self, account_id: str, *, cleared_by: str = "reconciler") -> ReconciliationResult:
        """Compare the ledger with the broker and record the verdict (see the module docstring).

        A broker that cannot be read (an adapter ``BrokerError`` such as an unmapped order
        status, or a timeout) fails the run instead of escaping the caller's loop: the run is
        recorded FAILED, an alert names the cause and entries pause until a later run passes.
        """
        return self.record(account_id, self.compare(account_id), cleared_by=cleared_by)

    def compare(self, account_id: str) -> ReconciliationResult:
        """Read the broker and compare it with the ledger; the verdict is returned, not recorded.

        Unknown outcomes are resolved and live orders are brought up to date from the broker
        first, as always. No run row, pause or alert is written for the verdict: that is
        :meth:`record`. ``in_flight_only`` tells a caller that can wait whether comparing again
        in a moment could settle the differences (see the module docstring).
        """
        started = self.clock.now()
        self._run_seq += 1
        run_id = deterministic_id("recon", account_id, started.isoformat(), str(self._run_seq))
        result = ReconciliationResult(run_id=run_id, status="PASSED", started_at=started)
        try:
            self._compare(account_id, result)
        except (BrokerError, BrokerTimeout, ConnectionError, TimeoutError) as exc:
            detail = f"broker unavailable during reconciliation: {type(exc).__name__}: {exc}"
            result.problems.append(detail[:2000])
            result.broker_unavailable = True
            result.in_flight_only = False
        result.status = "FAILED" if result.problems else "PASSED"
        return result

    def record(
        self, account_id: str, result: ReconciliationResult, *, cleared_by: str
    ) -> ReconciliationResult:
        """Persist a comparison: the run row, then the pause or the cleared health flags.

        FAILED raises ``RECONCILIATION_FAILED`` (and a HIGH alert when the broker could not be
        read). PASSED clears the health flags raised up to ``result.started_at``.
        """
        finished = self.clock.now()
        with self.db.tx() as conn:
            conn.execute(
                insert(reconciliation_runs).values(
                    run_id=result.run_id,
                    account_id=account_id,
                    started_at=result.started_at,
                    finished_at=finished,
                    status=result.status,
                    summary_json=canonical_json(
                        {"problems": result.problems, "notes": result.notes}
                    ),
                )
            )
            if result.broker_unavailable:
                conn.execute(
                    insert(alerts).values(
                        raised_at=finished,
                        severity="HIGH",
                        code="RECONCILIATION_BROKER_UNAVAILABLE",
                        account_id=account_id,
                        message="; ".join(result.problems)[:2000],
                    )
                )
            if result.problems:
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=account_id,
                    cause=PauseCause.RECONCILIATION_FAILED,
                    details="; ".join(result.problems)[:2000],
                    now=finished,
                )
            else:
                result.cleared_flags = self.pauses.clear_health_flags(
                    conn,
                    scope_ids=[account_id, self.portfolio_id],
                    reconciliation_run_id=result.run_id,
                    cleared_by=cleared_by,
                    now=finished,
                )
        return result

    def _in_flight_orders(
        self, conn: Connection, account_id: str, now: datetime
    ) -> tuple[set[str], set[str]]:
        """Client order IDs and instruments of this account's orders whose fill may still be on
        its way: working at the broker, or final for less than :data:`IN_FLIGHT_WINDOW`."""
        rows = conn.execute(
            select(order_intents.c.client_order_id, order_intents.c.instrument_id).where(
                order_intents.c.account_id == account_id,
                or_(
                    order_intents.c.state.in_(tuple(s.value for s in WORKING_STATES)),
                    and_(
                        order_intents.c.state.in_(tuple(s.value for s in FILLABLE_FINAL_STATES)),
                        order_intents.c.updated_at >= now - IN_FLIGHT_WINDOW,
                    ),
                ),
            )
        ).all()
        return {r.client_order_id for r in rows}, {r.instrument_id for r in rows}

    def _compare(self, account_id: str, result: ReconciliationResult) -> None:
        adapter = self._adapters[account_id]
        self.resolve_unknown(account_id)
        # Bring live local orders up to date from the broker's REST view.
        with self.db.tx() as conn:
            live = conn.execute(
                select(order_intents).where(
                    order_intents.c.account_id == account_id,
                    order_intents.c.state.in_(tuple(s.value for s in LIVE_STATES)),
                )
            ).all()
        seen_at_broker: set[str] = set()
        for row in live:
            if (
                OrderState(row.state) is OrderState.PENDING_SUBMIT
                or OrderState(row.state) in UNKNOWN_STATES
            ):
                continue
            snapshot = adapter.get_order_by_client_id(row.client_order_id)
            if snapshot is not None:
                seen_at_broker.add(row.client_order_id)
                self._apply_snapshot(snapshot, adapter)

        broker_open = {o.client_order_id: o for o in adapter.list_orders(OrderScope.OPEN)}
        broker_positions = {p.instrument_id: p.quantity for p in adapter.get_positions()}
        broker_executions = adapter.list_executions()
        snapshot_account = adapter.get_account()
        fee_charges = adapter.list_fee_charges()

        #: Problems no in-flight order explains: one of these and waiting cannot help.
        unexplained = 0
        with self.db.tx() as conn:
            in_flight, in_flight_instruments = self._in_flight_orders(
                conn, account_id, self.clock.now()
            )
            # 1. unresolved or unknown intents
            unresolved = conn.execute(
                select(order_intents.c.client_order_id, order_intents.c.state).where(
                    order_intents.c.account_id == account_id,
                    order_intents.c.state.in_(tuple(s.value for s in UNKNOWN_STATES)),
                )
            ).all()
            for cid, state in unresolved:
                result.problems.append(f"order {cid} still {state}")
                unexplained += 1

            # 2. open orders
            local_live = {
                r.client_order_id: r
                for r in conn.execute(
                    select(order_intents).where(
                        order_intents.c.account_id == account_id,
                        order_intents.c.state.in_(tuple(s.value for s in LIVE_STATES)),
                    )
                ).all()
            }
            for cid in broker_open:
                if cid not in local_live:
                    result.problems.append(f"broker has an open order {cid} unknown to the ledger")
                    unexplained += 1
            for cid, row in local_live.items():
                if OrderState(row.state) is OrderState.PENDING_SUBMIT:
                    continue
                if cid not in broker_open:
                    result.problems.append(f"local live order {cid} is not open at the broker")
                    # Explained only when the broker showed this order a moment ago: it then
                    # closed between the two reads (a fill or a cancel arriving just now).
                    unexplained += cid not in seen_at_broker or cid not in in_flight

            # 3. executions
            known = {
                r.execution_id
                for r in conn.execute(
                    select(fills.c.execution_id).where(fills.c.account_id == account_id)
                ).all()
            }
            for fill in broker_executions:
                if fill.execution_id not in known:
                    result.problems.append(f"broker execution {fill.execution_id} not in ledger")
                    unexplained += fill.client_order_id not in in_flight

            # 4. positions
            local_positions = {
                p.instrument_id: p.quantity for p in self.ledger.all_positions(conn, account_id)
            }
            for iid in sorted(set(local_positions) | set(broker_positions)):
                lq = local_positions.get(iid, ZERO)
                bq = broker_positions.get(iid, ZERO)
                if lq != bq:
                    result.problems.append(f"position {iid}: ledger {lq} vs broker {bq}")
                    unexplained += iid not in in_flight_instruments

            # 5. cash subledger delta
            acct = conn.execute(select(accounts).where(accounts.c.account_id == account_id)).one()
            if acct.broker_cash_baseline is not None:
                local_delta = self.ledger.cash(conn, account_id, acct.currency) - Decimal(
                    acct.allocated_capital
                )
                broker_delta = snapshot_account.cash - Decimal(acct.broker_cash_baseline)
                # the ledger's cash as the broker books it: each fill rounded to the cent
                rounding = broker_cent_rounding(
                    (r.side, r.quantity, r.price)
                    for r in conn.execute(
                        select(fills.c.side, fills.c.quantity, fills.c.price).where(
                            fills.c.account_id == account_id,
                            fills.c.currency == acct.currency,
                        )
                    ).all()
                )
                # fees the ledger accrued from its cash that the broker has not charged (yet)
                accrued = self.ledger.pending_fee_accruals(conn, account_id)
                # fees the broker took from its cash on its own since the baseline (ADR-0054):
                # the ledger holds its estimates, or nothing for fills from before ADR-0050
                charged = sum(
                    (c.amount for c in fee_charges if c.currency == acct.currency), start=ZERO
                )
                diff = abs(local_delta + rounding + accrued - charged - broker_delta)
                if rounding:
                    result.notes.append(f"broker cent rounding of the fills {rounding}")
                if accrued:
                    result.notes.append(f"fees accrued and not charged by the broker {accrued}")
                if charged:
                    result.notes.append(
                        f"fees charged by the broker apart from the fills {charged}"
                        f" ({len(fee_charges)} charge(s) since the baseline)"
                    )
                if diff > CENT:
                    result.problems.append(
                        f"cash delta mismatch: ledger {local_delta} vs broker {broker_delta}"
                        + (f" (broker cent rounding {rounding} allowed for)" if rounding else "")
                        + (f" (accrued fees {accrued} allowed for)" if accrued else "")
                        + (f" (broker fee charges {charged} allowed for)" if charged else "")
                    )
                    unexplained += not in_flight
                elif diff > 0:
                    result.notes.append(f"cent-level cash rounding difference {diff}")
        result.in_flight_only = bool(result.problems) and unexplained == 0
