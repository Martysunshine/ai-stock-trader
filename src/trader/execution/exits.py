"""Exit management: stop, target, maximum hold, session close and loss-breach reduction.

Exits are a separate path from entries (instructions.md §5): no pause, stale quote or spread
rule blocks a valid risk-reducing sell. Every exit sells at most the reconciled owned quantity
minus quantity already committed to open sells, so overlapping exits (e.g. a time exit racing a
stop) can never oversell. If the entry remainder is still working when an exit triggers, its
cancellation is requested first; any late fill becomes owned quantity and is exited too.

Replay/paper use monitored synthetic exits (ADR-0005). They cannot protect during a complete
system or network outage; live use requires certified broker-held protection by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import insert, select

from trader.accounting.ledger import Ledger
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.domain.market import Quote
from trader.domain.orders import OrderPurpose, OrderState
from trader.domain.types import ZERO
from trader.execution._gateway import SubmissionGateway
from trader.ops.clock import Clock
from trader.ops.pause import PauseCategory, PauseService
from trader.risk.allocator import PortfolioAllocator
from trader.risk.portfolio import AccountRuntime
from trader.storage.db import Database
from trader.storage.schema import alerts, exit_plans, order_intents


@dataclass(frozen=True, slots=True)
class ExitTrigger:
    account_id: str
    instrument_id: str
    purpose: OrderPurpose
    reason: str
    entry_intent_id: str | None


class ExitManager:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        pauses: PauseService,
        allocator: PortfolioAllocator,
        gateway: SubmissionGateway,
        accounts: dict[str, AccountRuntime],
        clock: Clock,
        portfolio_id: str,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.pauses = pauses
        self.allocator = allocator
        self.gateway = gateway
        self.accounts = accounts
        self.clock = clock
        self.portfolio_id = portfolio_id

    def evaluate(self, quotes: dict[str, Quote], halted: set[str]) -> list[ExitTrigger]:
        now = self.clock.now()
        triggers: list[ExitTrigger] = []
        with self.db.tx() as conn:
            loss_breach = any(
                f.category is PauseCategory.LOSS_BREACH
                for f in self.pauses.active_flags(conn, portfolio_id=self.portfolio_id)
            )
            positions = self.ledger.open_positions(conn, sorted(self.accounts))
            plans = {
                (r.account_id, r.instrument_id): r
                for r in conn.execute(select(exit_plans).where(exit_plans.c.active.is_(True))).all()
            }
        for pos in positions:
            if pos.quantity <= 0:
                continue
            account = self.accounts[pos.account_id]
            plan = plans.get((pos.account_id, pos.instrument_id))
            quote = quotes.get(pos.instrument_id)
            trigger = self._trigger(account, plan, quote, now, loss_breach)
            if trigger is None:
                continue
            purpose, reason = trigger
            triggers.append(
                ExitTrigger(
                    account_id=pos.account_id,
                    instrument_id=pos.instrument_id,
                    purpose=purpose,
                    reason=reason,
                    entry_intent_id=plan.entry_intent_id if plan is not None else None,
                )
            )
        return triggers

    def _trigger(
        self,
        account: AccountRuntime,
        plan: object | None,
        quote: Quote | None,
        now: datetime,
        loss_breach: bool,
    ) -> tuple[OrderPurpose, str] | None:
        if loss_breach:
            return OrderPurpose.EXIT_RISK_REDUCTION, "loss-limit breach: reduce holdings"
        if plan is None:
            # A position without a plan (e.g. unsolicited) is reduced conservatively.
            return OrderPurpose.EXIT_RISK_REDUCTION, "position without an exit plan"
        p = plan._mapping  # type: ignore[attr-defined]
        if quote is not None:
            if quote.bid <= Decimal(p["stop_price"]):
                return OrderPurpose.EXIT_STOP, f"bid {quote.bid} <= stop {p['stop_price']}"
            target = p["target_price"]
            if target is not None and quote.bid >= Decimal(target):
                return OrderPurpose.EXIT_TARGET, f"bid {quote.bid} >= target {target}"
        if now >= p["max_hold_until"]:
            return OrderPurpose.EXIT_TIME, "maximum holding period reached"
        calendar = account.calendar
        if isinstance(calendar, ExchangeCalendar):
            session = calendar.session_at(now)
            if session is None or now >= calendar.close_procedure_start(session):
                return OrderPurpose.EXIT_SESSION, "session close procedure"
        elif isinstance(calendar, Utc247Calendar):
            pass
        return None

    def act(self, triggers: list[ExitTrigger], halted: set[str]) -> list[str]:
        """Cancel working entry remainders, create reducing exits and submit them."""
        created: list[str] = []
        for trig in triggers:
            self._cancel_working_entries(trig.account_id, trig.instrument_id)
            intent_id = self.allocator.reserve_exit(
                account_id=trig.account_id,
                instrument_id=trig.instrument_id,
                purpose=trig.purpose,
                reason=trig.reason,
                parent_intent_id=trig.entry_intent_id,
            )
            if intent_id is not None:
                created.append(intent_id)
                if trig.instrument_id in halted:
                    self._alert(
                        trig.account_id,
                        f"exit for {trig.instrument_id} queued while halted: exposure persists "
                        "until the broker confirms the fill",
                    )
        if created:
            self.gateway.submit_pending()
        return created

    def _cancel_working_entries(self, account_id: str, instrument_id: str) -> None:
        with self.db.tx() as conn:
            rows = conn.execute(
                select(order_intents.c.intent_id).where(
                    order_intents.c.account_id == account_id,
                    order_intents.c.instrument_id == instrument_id,
                    order_intents.c.purpose == OrderPurpose.ENTRY.value,
                    order_intents.c.state.in_(
                        (OrderState.ACKNOWLEDGED.value, OrderState.PARTIALLY_FILLED.value)
                    ),
                )
            ).all()
        for row in rows:
            self.gateway.request_cancel(row.intent_id)

    def pending_liquidation(self) -> dict[str, Decimal]:
        """Owned quantity not yet confirmed closed (reported, never claimed as closed)."""
        with self.db.tx() as conn:
            return {
                f"{p.account_id}:{p.instrument_id}": p.quantity
                for p in self.ledger.open_positions(conn, sorted(self.accounts))
                if p.quantity != ZERO
            }

    def _alert(self, account_id: str, message: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                insert(alerts).values(
                    raised_at=self.clock.now(),
                    severity="HIGH",
                    code="EXIT_PENDING",
                    account_id=account_id,
                    message=message,
                )
            )
