"""The submission gateway: the ONLY code that calls ``BrokerAdapter.submit``/``cancel``.

``tests/test_boundaries.py`` enforces this with an AST scan. Each submission:

1. **Claim** (one transaction): verify leadership (fencing token), reload the persisted intent and
   approved plan, re-derive the plan digest from persisted rows, check validity window and (for
   entries) the account version; mark the intent ``SUBMITTING`` *before* any network call.
2. **Send** outside the transaction. A timeout or transport error is an *unknown outcome*: the
   intent becomes ``UNKNOWN_OUTCOME``, entries pause (``UNKNOWN_ORDER``) and the reconciler
   resolves it by client order ID. A new client ID is never generated for the same trade and
   blind retries are impossible (architecture.md §7).
3. **Record** the acknowledgement or definitive rejection.

A cancel request whose outcome is unknown (timeout or transport error) never escapes the run
loop: it is recorded as an alert and pauses entries until reconciliation has read the order
(architecture.md §7).

``trader paper certify`` alone may re-send one resting certification limit buy unchanged
(:meth:`SubmissionGateway.probe_duplicate_submission`) to demonstrate that the broker refuses a
client order ID it already holds; nothing else ever sends an approved plan twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Connection, Row, insert, select, update

from trader.accounting.ledger import Ledger
from trader.brokers.base import BrokerAdapter, BrokerError, BrokerTimeout, DuplicateClientOrderId
from trader.domain.certification import CertificationError, CertificationGrant
from trader.domain.orders import ApprovedPlan, OrderPurpose, OrderState, OrderType
from trader.domain.types import ZERO, Mode, Side
from trader.execution.oms import OrderManager
from trader.ops.clock import Clock
from trader.ops.leadership import LeadershipLost, LeadershipManager
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.risk.portfolio import committed_sell_quantity
from trader.storage.db import Database
from trader.storage.schema import alerts, order_intents, submission_outbox

ProbeOutcome = Literal[
    "REJECTED_DUPLICATE", "ACCEPTED_TWICE", "REJECTED", "NOT_SENT", "UNKNOWN", "NOT_RUN"
]


@dataclass(frozen=True, slots=True)
class DuplicateProbe:
    """The outcome of re-sending a certification order's client order ID (paper only)."""

    outcome: ProbeOutcome
    detail: str
    client_order_id: str
    original_broker_order_id: str | None
    duplicate_broker_order_id: str | None


class SubmissionGateway:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        adapters: dict[str, BrokerAdapter],
        leadership: dict[str, LeadershipManager],
        pauses: PauseService,
        oms: OrderManager,
        clock: Clock,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self._adapters = adapters
        self.leadership = leadership
        self.pauses = pauses
        self.oms = oms
        self.clock = clock
        self._authority = object()
        for adapter in adapters.values():
            adapter.bind_authority(self._authority)

    # ------------------------------------------------------------------ submit
    def submit_pending(self) -> list[tuple[str, OrderState]]:
        with self.db.tx() as conn:
            rows = conn.execute(
                select(submission_outbox.c.outbox_id, order_intents.c.seq)
                .join(order_intents, order_intents.c.intent_id == submission_outbox.c.intent_id)
                .where(submission_outbox.c.status == "PENDING")
                .order_by(order_intents.c.seq)
            ).all()
        return [(row.outbox_id, self.submit(row.outbox_id)) for row in rows]

    def submit(self, outbox_id: str) -> OrderState:
        claimed = self._claim(outbox_id)
        if isinstance(claimed, OrderState):
            return claimed
        plan, account_id = claimed
        adapter = self._adapters[account_id]
        try:
            result = adapter.submit(plan, authority=self._authority)
        except (BrokerTimeout, BrokerError, ConnectionError, TimeoutError) as exc:
            return self._unknown_outcome(plan, account_id, exc)
        now = self.clock.now()
        with self.db.tx() as conn:
            intent = self._intent(conn, plan.intent.intent_id)
            current = OrderState(intent.state)
            if result.accepted:
                values: dict[str, object] = {
                    "broker_order_id": result.broker_order_id,
                    "updated_at": now,
                }
                if current is OrderState.SUBMITTING:
                    values["state"] = OrderState.ACKNOWLEDGED.value
                conn.execute(
                    update(order_intents)
                    .where(order_intents.c.intent_id == plan.intent.intent_id)
                    .values(**values)
                )
                state = OrderState(values.get("state", current.value))  # type: ignore[arg-type]
            else:
                conn.execute(
                    update(order_intents)
                    .where(order_intents.c.intent_id == plan.intent.intent_id)
                    .values(state=OrderState.REJECTED.value, final_cum_qty=ZERO, updated_at=now)
                )
                self.oms.sync_reservation(conn, plan.intent.intent_id, now)
                state = OrderState.REJECTED
            conn.execute(
                update(submission_outbox)
                .where(submission_outbox.c.outbox_id == outbox_id)
                .values(status="DONE", updated_at=now)
            )
        return state

    def _claim(self, outbox_id: str) -> tuple[ApprovedPlan, str] | OrderState:
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute(
                select(submission_outbox).where(submission_outbox.c.outbox_id == outbox_id)
            ).one()
            intent = self._intent(conn, row.intent_id)
            account_id = intent.account_id
            if row.status != "PENDING":
                return OrderState(intent.state)
            try:
                token = self.leadership[account_id].assert_current(conn)
            except LeadershipLost as exc:
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=account_id,
                    cause=PauseCause.LEADERSHIP_LOST,
                    details=str(exc),
                    now=now,
                )
                return OrderState(intent.state)
            plan = ApprovedPlan.model_validate_json(intent.plan_json)
            problem = self._verify(conn, plan, intent, now)
            if problem is not None:
                conn.execute(
                    update(order_intents)
                    .where(order_intents.c.intent_id == intent.intent_id)
                    .values(state=OrderState.NOT_SENT.value, final_cum_qty=ZERO, updated_at=now)
                )
                conn.execute(
                    update(submission_outbox)
                    .where(submission_outbox.c.outbox_id == outbox_id)
                    .values(status="ABANDONED", updated_at=now)
                )
                self.oms.sync_reservation(conn, intent.intent_id, now)
                return OrderState.NOT_SENT
            conn.execute(
                update(order_intents)
                .where(order_intents.c.intent_id == intent.intent_id)
                .values(state=OrderState.SUBMITTING.value, updated_at=now)
            )
            conn.execute(
                update(submission_outbox)
                .where(submission_outbox.c.outbox_id == outbox_id)
                .values(
                    status="CLAIMED",
                    fencing_token=token,
                    attempts=submission_outbox.c.attempts + 1,
                    updated_at=now,
                )
            )
            return plan, account_id

    def _verify(
        self, conn: Connection, plan: ApprovedPlan, intent: Row[Any], now: datetime
    ) -> str | None:
        i = intent._mapping
        if OrderState(i["state"]) is not OrderState.PENDING_SUBMIT:
            return f"intent is {i['state']}, not PENDING_SUBMIT"
        if not plan.is_authentic():
            return "approved plan digest does not match its contents"
        if plan.digest != i["plan_digest"] or plan.intent_hash != i["intent_hash"]:
            return "approved plan differs from the persisted approval"
        if (
            plan.intent.client_order_id != i["client_order_id"]
            or plan.intent.quantity != Decimal(i["quantity"])
            or plan.intent.instrument_id != i["instrument_id"]
            or plan.intent.side.value != i["side"]
        ):
            return "approved plan differs from the persisted intent"
        if not plan.is_valid_at(now):
            return "approved plan expired before submission"
        if plan.intent.purpose is OrderPurpose.ENTRY:
            if self.ledger.version(conn, i["account_id"]) != plan.account_version:
                return "account state changed after approval (version mismatch)"
        else:
            position = self.ledger.position(conn, i["account_id"], i["instrument_id"])
            owned = position.quantity if position else ZERO
            others = committed_sell_quantity(conn, i["account_id"], i["instrument_id"]) - Decimal(
                i["quantity"]
            )
            if Decimal(i["quantity"]) > owned - others:
                return "exit quantity exceeds owned minus committed sells"
        return None

    def _unknown_outcome(
        self, plan: ApprovedPlan, account_id: str, exc: BaseException
    ) -> OrderState:
        now = self.clock.now()
        with self.db.tx() as conn:
            conn.execute(
                update(order_intents)
                .where(
                    order_intents.c.intent_id == plan.intent.intent_id,
                    order_intents.c.state == OrderState.SUBMITTING.value,
                )
                .values(state=OrderState.UNKNOWN_OUTCOME.value, updated_at=now)
            )
            conn.execute(
                update(submission_outbox)
                .where(submission_outbox.c.intent_id == plan.intent.intent_id)
                .values(status="DONE", updated_at=now)
            )
            self.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=account_id,
                cause=PauseCause.UNKNOWN_ORDER,
                details=f"{plan.intent.client_order_id}: {type(exc).__name__}: {exc}",
                now=now,
            )
            return OrderState(self._intent(conn, plan.intent.intent_id).state)

    def abandon_pending_entries(self, reason: str) -> list[str]:
        """Lapse never-sent entry approvals (startup recovery: re-decide instead of resuming)."""
        now = self.clock.now()
        abandoned: list[str] = []
        with self.db.tx() as conn:
            rows = conn.execute(
                select(order_intents.c.intent_id)
                .join(submission_outbox, submission_outbox.c.intent_id == order_intents.c.intent_id)
                .where(
                    submission_outbox.c.status == "PENDING",
                    order_intents.c.purpose == OrderPurpose.ENTRY.value,
                    order_intents.c.state == OrderState.PENDING_SUBMIT.value,
                )
            ).all()
            for row in rows:
                conn.execute(
                    update(order_intents)
                    .where(order_intents.c.intent_id == row.intent_id)
                    .values(state=OrderState.NOT_SENT.value, final_cum_qty=ZERO, updated_at=now)
                )
                conn.execute(
                    update(submission_outbox)
                    .where(submission_outbox.c.intent_id == row.intent_id)
                    .values(status="ABANDONED", updated_at=now)
                )
                self.oms.sync_reservation(conn, row.intent_id, now)
                abandoned.append(str(row.intent_id))
        return abandoned

    # ------------------------------------------------------------------ certification
    def probe_duplicate_submission(
        self, intent_id: str, *, probe: BrokerAdapter, grant: CertificationGrant
    ) -> DuplicateProbe:
        """Certification only: send an acknowledged certification limit buy's approved plan a
        second time, unchanged - same client order ID - through ``probe``.

        ``probe`` is a second adapter instance for the same paper account with no memory of the
        first submission: it stands in for a restarted worker, so the broker itself must refuse
        the repeated client order ID. Only a resting LIMIT buy of this grant's run qualifies (it
        rests far below the market, so even a wrongly accepted duplicate cannot fill), and the
        plan must still be valid. The intent, its reservation and the outbox are left untouched.
        A duplicate the broker accepts is cancelled at once, pauses entries and raises an alert;
        an unknown outcome pauses entries until reconciliation has read the account.
        """
        now = self.clock.now()
        if grant.mode is not Mode.PAPER or not grant.is_active(now):
            raise CertificationError("the duplicate probe needs an active paper certification")
        if probe.account_id != grant.account_id or probe.capabilities().environment != "paper":
            raise CertificationError("the duplicate probe needs a paper adapter for the account")
        with self.db.tx() as conn:
            intent = self._intent(conn, intent_id)
            if intent.account_id != grant.account_id:
                raise CertificationError("the intent belongs to another account")
            self.leadership[intent.account_id].assert_current(conn)
            plan = ApprovedPlan.model_validate_json(intent.plan_json)
            original = str(intent.broker_order_id) if intent.broker_order_id else None
            state = OrderState(intent.state)
        problem = None
        if not grant.covers(plan.model_hash):
            problem = "not a certification order of this run"
        elif plan.intent.order_type is not OrderType.LIMIT or plan.intent.side is not Side.BUY:
            problem = "only a resting certification LIMIT buy may be re-sent"
        elif state is not OrderState.ACKNOWLEDGED or original is None:
            problem = f"the original order is {state.value}, not acknowledged and resting"
        elif not plan.is_authentic():
            problem = "approved plan digest does not match its contents"
        elif not plan.is_valid_at(now):
            problem = "the approved plan expired before the probe"
        cid = plan.intent.client_order_id
        if problem is not None:
            return DuplicateProbe("NOT_RUN", problem, cid, original, None)
        probe.bind_authority(self._authority)
        try:
            result = probe.submit(plan, authority=self._authority)
        except DuplicateClientOrderId as exc:
            outcome = DuplicateProbe("REJECTED_DUPLICATE", str(exc), cid, original, None)
        except (BrokerTimeout, BrokerError, ConnectionError, TimeoutError) as exc:
            detail = f"duplicate probe outcome unknown: {type(exc).__name__}: {exc}"
            self._certification_alert(plan.intent.account_id, "HIGH", detail, pause=True)
            return DuplicateProbe("UNKNOWN", detail, cid, original, None)
        else:
            if not result.accepted:
                kind: ProbeOutcome = "NOT_SENT" if result.broker_order_id is None else "REJECTED"
                outcome = DuplicateProbe(
                    kind, result.reject_reason or "refused", cid, original, result.broker_order_id
                )
            else:
                second = result.broker_order_id
                detail = f"the broker ACCEPTED client order ID {cid} twice ({original}, {second})"
                if second is not None and second != original:
                    try:
                        probe.cancel(second)
                    except (BrokerTimeout, BrokerError, ConnectionError, TimeoutError) as exc:
                        detail += f"; cancelling the duplicate failed: {type(exc).__name__}"
                self._certification_alert(plan.intent.account_id, "CRITICAL", detail, pause=True)
                return DuplicateProbe("ACCEPTED_TWICE", detail, cid, original, second)
        self._certification_alert(
            plan.intent.account_id,
            "INFO",
            f"duplicate probe of {cid}: {outcome.outcome}: {outcome.detail}",
            pause=False,
        )
        return outcome

    def _certification_alert(
        self, account_id: str, severity: str, message: str, *, pause: bool
    ) -> None:
        now = self.clock.now()
        with self.db.tx() as conn:
            conn.execute(
                insert(alerts).values(
                    raised_at=now,
                    severity=severity,
                    code="CERTIFICATION_DUPLICATE_PROBE",
                    account_id=account_id,
                    message=message[:2000],
                )
            )
            if pause:
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=account_id,
                    cause=PauseCause.UNKNOWN_ORDER,
                    details=message[:2000],
                    now=now,
                )

    # ------------------------------------------------------------------ cancel
    def request_cancel(self, intent_id: str) -> bool:
        """Ask the broker to cancel. Returns True if the request was accepted (NOT canceled)."""
        with self.db.tx() as conn:
            intent = self._intent(conn, intent_id)
            state = OrderState(intent.state)
            if state not in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                return False
            if intent.broker_order_id is None:
                return False
            self.leadership[intent.account_id].assert_current(conn)
            broker_order_id = str(intent.broker_order_id)
            account_id = str(intent.account_id)
            client_order_id = str(intent.client_order_id)
        try:
            result = self._adapters[account_id].cancel(broker_order_id)
        except (BrokerTimeout, BrokerError, ConnectionError, TimeoutError) as exc:
            self._cancel_outcome_unknown(account_id, client_order_id, broker_order_id, exc)
            return False
        if result.request_accepted:
            with self.db.tx() as conn:
                conn.execute(
                    update(order_intents)
                    .where(
                        order_intents.c.intent_id == intent_id,
                        order_intents.c.state.in_(
                            (OrderState.ACKNOWLEDGED.value, OrderState.PARTIALLY_FILLED.value)
                        ),
                    )
                    .values(state=OrderState.CANCEL_REQUESTED.value, updated_at=self.clock.now())
                )
        return result.request_accepted

    def _cancel_outcome_unknown(
        self, account_id: str, client_order_id: str, broker_order_id: str, exc: BaseException
    ) -> None:
        """A cancel whose outcome is unknown: the order may or may not be canceling.

        The intent keeps its live state (its exposure stays reserved: it is not marked
        ``CANCEL_REQUESTED``), an alert records the failure and entries pause (``UNKNOWN_ORDER``)
        until a PASSED reconciliation has read the broker's view of the order. The run loop
        continues; a caller may ask again later, which is safe because a cancel request for the
        same broker order is idempotent and never creates an order.
        """
        now = self.clock.now()
        message = (
            f"cancel of {client_order_id} ({broker_order_id}) has an unknown outcome: "
            f"{type(exc).__name__}: {exc}"
        )
        with self.db.tx() as conn:
            conn.execute(
                insert(alerts).values(
                    raised_at=now,
                    severity="HIGH",
                    code="CANCEL_OUTCOME_UNKNOWN",
                    account_id=account_id,
                    message=message[:2000],
                )
            )
            self.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=account_id,
                cause=PauseCause.UNKNOWN_ORDER,
                details=message[:2000],
                now=now,
            )

    @staticmethod
    def _intent(conn: Connection, intent_id: str) -> Row[Any]:
        return conn.execute(
            select(order_intents).where(order_intents.c.intent_id == intent_id)
        ).one()
