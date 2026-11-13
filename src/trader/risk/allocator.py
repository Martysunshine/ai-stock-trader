"""The shared cross-track allocator: the only path that creates order intents.

One transaction, serialised per portfolio (``pg_advisory_xact_lock`` / SQLite ``BEGIN IMMEDIATE``):

1. lock the portfolio, 2. verify account leadership (fencing token), 3. read pause flags,
4. rebuild exposure across every track and account from the database, 5. evaluate (pure risk
engine), 6. persist candidate, decision, intent, approved plan, reservation, outbox row and exit
plan, and bump the account/portfolio versions (architecture.md §5: "Persist approved intent,
its reservation, and an outbox entry in one transaction").

A partial unique index allows only one ACTIVE entry reservation per portfolio (ADR-0010), so
even a buggy concurrent writer cannot create a second pending entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from trader.accounting.fx import FxBook, StaleFxError
from trader.accounting.ledger import Ledger
from trader.config.policy import LoadedPolicy
from trader.data.calendars import ExchangeCalendar
from trader.domain.candidates import (
    DecisionRecord,
    EdgeEvidence,
    RejectReason,
    SignalScore,
    TradeCandidate,
)
from trader.domain.certification import CertificationGrant
from trader.domain.hashing import canonical_json
from trader.domain.ids import client_order_id, deterministic_id
from trader.domain.market import Quote
from trader.domain.orders import (
    OrderIntent,
    OrderPurpose,
    OrderState,
    OrderType,
    TimeInForce,
)
from trader.domain.types import ZERO, Mode, Side
from trader.ops.clock import Clock
from trader.ops.leadership import LeadershipLost, LeadershipManager
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.risk.approval import issue_plan
from trader.risk.engine import (
    CertificationOrderContext,
    EntryContext,
    approve_reducing_exit,
    evaluate_certification_order,
    evaluate_entry,
)
from trader.risk.portfolio import (
    AccountRuntime,
    PortfolioReader,
    committed_sell_quantity,
    entries_in_session,
)
from trader.risk.sizing import SizingResult
from trader.storage.db import Database
from trader.storage.helpers import insert_ignore
from trader.storage.schema import (
    candidates,
    exit_plans,
    model_versions,
    order_intents,
    portfolios,
    reservation_events,
    risk_decisions,
    risk_reservations,
    submission_outbox,
)

ENTRY_PLAN_VALIDITY = timedelta(seconds=10)
EXIT_PLAN_VALIDITY = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class MarketInputs:
    quote: Quote | None
    marks: Mapping[str, Decimal]
    halted: bool
    #: Full-market spread estimate in bps when the quote is one venue's book (ADR-0061).
    market_spread_bps: Decimal | None = None


class PortfolioAllocator:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        pauses: PauseService,
        policy: LoadedPolicy,
        mode: Mode,
        portfolio_id: str,
        accounts: Mapping[str, AccountRuntime],
        leadership: Mapping[str, LeadershipManager],
        clock: Clock,
        fx: FxBook,
        namespace: str,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.pauses = pauses
        self.policy = policy
        self.mode = mode
        self.portfolio_id = portfolio_id
        self.accounts = accounts
        self.leadership = leadership
        self.clock = clock
        self.fx = fx
        self.namespace = namespace
        self.reader = PortfolioReader(
            ledger,
            policy.policy.initial_trading_capital,
            policy.policy.gap_allowance_floor_fraction,
        )

    # ------------------------------------------------------------------ entries
    def reserve_entry(
        self,
        *,
        account_id: str,
        candidate: TradeCandidate,
        score: SignalScore | None,
        market: MarketInputs,
    ) -> DecisionRecord:
        account = self.accounts[account_id]
        spec = account.specs[candidate.instrument_id]
        now = self.clock.now()
        with self.db.tx() as conn:
            self.db.lock_portfolio(conn, self.portfolio_id)
            insert_ignore(
                conn,
                candidates,
                {
                    "candidate_id": candidate.candidate_id,
                    "strategy_id": candidate.strategy_id,
                    "instrument_id": candidate.instrument_id,
                    "decision_time": candidate.decision_time,
                    "payload_json": canonical_json(candidate),
                },
                ["candidate_id"],
            )
            try:
                self.leadership[account_id].assert_current(conn)
            except LeadershipLost as exc:
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=account_id,
                    cause=PauseCause.LEADERSHIP_LOST,
                    details=str(exc),
                    now=now,
                )
                return self._record_rejection(
                    conn, account, candidate, score, (RejectReason.LEADERSHIP,), (str(exc),), ZERO
                )

            all_ids = sorted(self.accounts)
            flags = tuple(
                self.pauses.active_flags(
                    conn, portfolio_id=self.portfolio_id, account_id=account_id
                )
            )
            settlement_day = now.date()
            snapshot = self.reader.snapshot(
                conn,
                accounts=self.accounts,
                marks=market.marks,
                fx=self.fx,
                now=now,
                settlement_day=settlement_day,
            )
            try:
                fx_to_usd: Decimal | None = self.fx.cost_rate(spec.currency, now)
            except StaleFxError:
                fx_to_usd = None
            calendar = account.calendar
            session = calendar.session_at(now)
            in_window = False
            if session is not None:
                start, end = calendar.entry_window(session)
                in_window = start <= now < end
            ctx = EntryContext(
                mode=self.mode,
                now=now,
                policy=self.policy,
                track_profile=account.profile,
                spec=spec,
                capabilities=account.capabilities,
                fees=account.fees,
                candidate=candidate,
                score=score,
                quote=market.quote,
                market_spread_bps=market.market_spread_bps,
                fx_to_usd=fx_to_usd,
                effective_equity_usd=snapshot.equity.effective_sizing_equity_usd,
                portfolio_spendable_usd=snapshot.portfolio_spendable_usd,
                account_spendable_usd=snapshot.account_spendable_usd[account_id],
                exposure=snapshot.exposure,
                active_flags=flags,
                entries_in_session=entries_in_session(
                    conn, account=account, all_account_ids=all_ids, now=now
                ),
                in_entry_window=in_window,
                halted=market.halted,
                rule_profile=account.rule_profile,
                unknown_orders_outstanding=snapshot.unknown_orders > 0,
                instrument_busy=candidate.instrument_id in snapshot.busy_instruments,
            )
            decision = evaluate_entry(ctx)
            equity_used = snapshot.equity.effective_sizing_equity_usd
            if not decision.approved:
                return self._record_rejection(
                    conn,
                    account,
                    candidate,
                    score,
                    decision.reasons,
                    decision.details,
                    equity_used,
                    sizing=decision.sizing,
                    limit_price=decision.limit_price,
                )
            sizing = decision.sizing
            if sizing is None or decision.limit_price is None or market.quote is None:
                raise RuntimeError("approved decision lacks sizing, limit price or quote")
            intent_seq = self.db.next_counter(conn, "intent")
            intent_id = deterministic_id("intent", self.namespace, candidate.candidate_id, "ENTRY")
            intent = OrderIntent(
                intent_id=intent_id,
                client_order_id=client_order_id(f"{self.namespace}|{account_id}", intent_id),
                portfolio_id=self.portfolio_id,
                account_id=account_id,
                instrument_id=candidate.instrument_id,
                track=candidate.track,
                side=Side.BUY,
                purpose=OrderPurpose.ENTRY,
                order_type=OrderType.LIMIT,
                quantity=sizing.quantity,
                limit_price=decision.limit_price,
                time_in_force=TimeInForce.DAY,
                reason=f"{candidate.strategy_id} v{candidate.strategy_version} breakout",
                created_at=now,
                candidate_id=candidate.candidate_id,
            )
            account_version = self.ledger.bump_version(conn, account_id)
            conn.execute(
                update(portfolios)
                .where(portfolios.c.portfolio_id == self.portfolio_id)
                .values(version=portfolios.c.version + 1)
            )
            decision_id = deterministic_id("decision", self.namespace, candidate.candidate_id)
            if score is None:
                raise RuntimeError("approved decision without a score")
            plan = issue_plan(
                intent=intent,
                decision_id=decision_id,
                account_version=account_version,
                policy_hash=self.policy.policy_hash,
                model_hash=score.model_hash,
                quote_time=market.quote.event_time,
                approved_at=now,
                valid_until=min(now + ENTRY_PLAN_VALIDITY, candidate.expires_at),
                reserved_cash_usd=sizing.cash_required_usd,
                reserved_risk_usd=sizing.planned_loss_usd,
                exit_method="synthetic-monitored-exit" if self.mode is not Mode.LIVE else "native",
            )
            record = DecisionRecord(
                decision_id=decision_id,
                candidate_id=candidate.candidate_id,
                instrument_id=candidate.instrument_id,
                track=candidate.track,
                decided_at=now,
                approved=True,
                reasons=(),
                policy_hash=self.policy.policy_hash,
                model_hash=score.model_hash,
                evidence=score.evidence,
                quantity=sizing.quantity,
                entry_limit_price=decision.limit_price,
                notional_usd=sizing.notional_usd,
                planned_loss_usd=sizing.planned_loss_usd,
                estimated_costs_usd=sizing.estimated_costs_usd,
                expected_net_usd=sizing.expected_net_usd,
                effective_sizing_equity_usd=equity_used,
                binding_constraint=sizing.binding_constraint,
                intent_id=intent_id,
            )
            self._insert_decision(conn, account_id, record)
            self._register_model(conn, score)
            conn.execute(
                insert(order_intents).values(
                    intent_id=intent_id,
                    seq=intent_seq,
                    portfolio_id=self.portfolio_id,
                    account_id=account_id,
                    client_order_id=intent.client_order_id,
                    instrument_id=intent.instrument_id,
                    track=intent.track.value,
                    side=intent.side.value,
                    purpose=intent.purpose.value,
                    order_type=intent.order_type.value,
                    quantity=intent.quantity,
                    limit_price=intent.limit_price,
                    time_in_force=intent.time_in_force.value,
                    state=OrderState.PENDING_SUBMIT.value,
                    decision_id=decision_id,
                    candidate_id=candidate.candidate_id,
                    parent_intent_id=None,
                    intent_hash=plan.intent_hash,
                    plan_json=plan.model_dump_json(),
                    plan_digest=plan.digest,
                    valid_until=plan.valid_until,
                    entry_ttl_until=now + timedelta(seconds=candidate.entry_ttl_seconds),
                    broker_order_id=None,
                    cum_filled_qty=ZERO,
                    final_cum_qty=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            reservation_id = deterministic_id("reservation", intent_id)
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(risk_reservations).values(
                            reservation_id=reservation_id,
                            portfolio_id=self.portfolio_id,
                            account_id=account_id,
                            intent_id=intent_id,
                            kind="ENTRY",
                            state="ACTIVE",
                            instrument_id=candidate.instrument_id,
                            track=candidate.track.value,
                            exposure_tags=",".join(spec.exposure_tags),
                            reserved_qty=sizing.quantity,
                            consumed_qty=ZERO,
                            released_qty=ZERO,
                            notional_usd=sizing.notional_usd,
                            fees_usd=sizing.round_trip_fees_usd,
                            planned_loss_usd=sizing.planned_loss_usd,
                            policy_hash=self.policy.policy_hash,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            except IntegrityError as exc:  # second pending entry: defence in depth
                raise RuntimeError(
                    "storage refused a second active entry reservation for the portfolio"
                ) from exc
            conn.execute(
                insert(reservation_events).values(
                    reservation_id=reservation_id,
                    event_time=now,
                    kind="RESERVE",
                    quantity=sizing.quantity,
                    note=f"notional {sizing.notional_usd} USD, planned loss {sizing.planned_loss_usd}",
                )
            )
            conn.execute(
                insert(submission_outbox).values(
                    outbox_id=deterministic_id("outbox", intent_id),
                    intent_id=intent_id,
                    status="PENDING",
                    fencing_token=None,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(exit_plans).values(
                    entry_intent_id=intent_id,
                    account_id=account_id,
                    instrument_id=candidate.instrument_id,
                    stop_price=candidate.stop_price,
                    target_price=candidate.target_price,
                    max_hold_until=now + timedelta(minutes=candidate.max_hold_minutes),
                    planned_loss_usd=sizing.planned_loss_usd,
                    active=True,
                    created_at=now,
                )
            )
            return record

    # ------------------------------------------------------------------ certification
    def reserve_certification_order(
        self,
        *,
        account_id: str,
        grant: CertificationGrant,
        step: str,
        instrument_id: str,
        order_type: OrderType,
        quantity: Decimal,
        limit_price: Decimal | None,
        market: MarketInputs,
        market_open: bool,
        orders_used: int,
    ) -> DecisionRecord:
        """One ``trader paper certify`` buy (domain/certification.py), in the entry transaction.

        Same steps as :meth:`reserve_entry` - portfolio lock, leadership, pauses, exposure,
        pure evaluation (:func:`~trader.risk.engine.evaluate_certification_order`), then decision,
        intent, approved plan, reservation and outbox in one transaction - but the plan's
        ``model_hash`` names the grant's run and there is no candidate, no edge and no exit plan
        (the run sells the bought quantity itself through :meth:`reserve_exit`).
        """
        account = self.accounts[account_id]
        spec = account.specs[instrument_id]
        now = self.clock.now()
        candidate_id = deterministic_id("certcand", self.namespace, grant.run_id, step)
        decision_id = deterministic_id("decision", self.namespace, grant.run_id, step)
        with self.db.tx() as conn:
            self.db.lock_portfolio(conn, self.portfolio_id)
            leadership_problem: str | None = None
            try:
                self.leadership[account_id].assert_current(conn)
            except LeadershipLost as exc:
                leadership_problem = str(exc)
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=account_id,
                    cause=PauseCause.LEADERSHIP_LOST,
                    details=leadership_problem,
                    now=now,
                )
            flags = tuple(
                self.pauses.active_flags(
                    conn, portfolio_id=self.portfolio_id, account_id=account_id
                )
            )
            snapshot = self.reader.snapshot(
                conn,
                accounts=self.accounts,
                marks=market.marks,
                fx=self.fx,
                now=now,
                settlement_day=now.date(),
            )
            try:
                fx_to_usd: Decimal | None = self.fx.cost_rate(spec.currency, now)
            except StaleFxError:
                fx_to_usd = None
            decision = evaluate_certification_order(
                CertificationOrderContext(
                    mode=self.mode,
                    now=now,
                    policy=self.policy,
                    grant=grant,
                    evidence=EdgeEvidence.CERTIFICATION,
                    spec=spec,
                    capabilities=account.capabilities,
                    rule_profile=account.rule_profile,
                    side=Side.BUY,
                    order_type=order_type,
                    quantity=quantity,
                    limit_price=limit_price,
                    quote=market.quote,
                    fx_to_usd=fx_to_usd,
                    market_open=market_open,
                    active_flags=flags,
                    unknown_orders_outstanding=snapshot.unknown_orders > 0,
                    instrument_busy=instrument_id in snapshot.busy_instruments,
                    pending_entries=snapshot.exposure.pending_entries,
                    orders_used=orders_used,
                    account_spendable_usd=snapshot.account_spendable_usd[account_id],
                )
            )
            reasons, details = decision.reasons, decision.details
            if leadership_problem is not None:
                reasons = (RejectReason.LEADERSHIP, *reasons)
                details = (leadership_problem, *details)
            bound = decision.notional_bound_usd
            fees = decision.fee_allowance_usd
            equity = max(snapshot.equity.effective_sizing_equity_usd, ZERO)
            approved = decision.approved and leadership_problem is None
            intent_id = deterministic_id("intent", self.namespace, grant.run_id, step)
            record = DecisionRecord(
                decision_id=decision_id,
                candidate_id=candidate_id,
                instrument_id=instrument_id,
                track=account.track,
                decided_at=now,
                approved=approved,
                reasons=() if approved else reasons,
                details=() if approved else details,
                policy_hash=self.policy.policy_hash,
                model_hash=grant.model_hash,
                evidence=EdgeEvidence.CERTIFICATION,
                quantity=quantity if approved else ZERO,
                entry_limit_price=limit_price,
                notional_usd=max(bound, ZERO),
                planned_loss_usd=max(bound, ZERO),
                estimated_costs_usd=fees,
                expected_net_usd=-fees,
                effective_sizing_equity_usd=equity,
                binding_constraint=f"certification cap {grant.max_order_notional_usd} USD",
                intent_id=intent_id if approved else None,
            )
            self._insert_decision(conn, account_id, record)
            insert_ignore(
                conn,
                model_versions,
                {
                    "model_hash": grant.model_hash,
                    "kind": EdgeEvidence.CERTIFICATION.value,
                    "evidence": EdgeEvidence.CERTIFICATION.value,
                    "description": f"paper certification run {grant.run_id} (not an edge)",
                    "registered_at": now,
                },
                ["model_hash"],
            )
            if not approved:
                return record
            quote = market.quote
            if quote is None:  # pragma: no cover - the evaluation requires a quote
                raise RuntimeError("approved certification order without a quote")
            intent_seq = self.db.next_counter(conn, "intent")
            intent = OrderIntent(
                intent_id=intent_id,
                client_order_id=client_order_id(f"{self.namespace}|{account_id}", intent_id),
                portfolio_id=self.portfolio_id,
                account_id=account_id,
                instrument_id=instrument_id,
                track=account.track,
                side=Side.BUY,
                purpose=OrderPurpose.ENTRY,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
                reason=f"paper certification {grant.run_id} step {step}",
                created_at=now,
            )
            account_version = self.ledger.bump_version(conn, account_id)
            conn.execute(
                update(portfolios)
                .where(portfolios.c.portfolio_id == self.portfolio_id)
                .values(version=portfolios.c.version + 1)
            )
            plan = issue_plan(
                intent=intent,
                decision_id=decision_id,
                account_version=account_version,
                policy_hash=self.policy.policy_hash,
                model_hash=grant.model_hash,
                quote_time=quote.event_time,
                approved_at=now,
                valid_until=min(now + ENTRY_PLAN_VALIDITY, grant.expires_at),
                reserved_cash_usd=bound,
                reserved_risk_usd=bound,
                exit_method="certification-run",
            )
            conn.execute(
                insert(order_intents).values(
                    intent_id=intent_id,
                    seq=intent_seq,
                    portfolio_id=self.portfolio_id,
                    account_id=account_id,
                    client_order_id=intent.client_order_id,
                    instrument_id=instrument_id,
                    track=account.track.value,
                    side=Side.BUY.value,
                    purpose=OrderPurpose.ENTRY.value,
                    order_type=order_type.value,
                    quantity=quantity,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY.value,
                    state=OrderState.PENDING_SUBMIT.value,
                    decision_id=decision_id,
                    candidate_id=None,
                    parent_intent_id=None,
                    intent_hash=plan.intent_hash,
                    plan_json=plan.model_dump_json(),
                    plan_digest=plan.digest,
                    valid_until=plan.valid_until,
                    entry_ttl_until=None,
                    broker_order_id=None,
                    cum_filled_qty=ZERO,
                    final_cum_qty=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            reservation_id = deterministic_id("reservation", intent_id)
            conn.execute(
                insert(risk_reservations).values(
                    reservation_id=reservation_id,
                    portfolio_id=self.portfolio_id,
                    account_id=account_id,
                    intent_id=intent_id,
                    kind="ENTRY",
                    state="ACTIVE",
                    instrument_id=instrument_id,
                    track=account.track.value,
                    exposure_tags=",".join(spec.exposure_tags),
                    reserved_qty=quantity,
                    consumed_qty=ZERO,
                    released_qty=ZERO,
                    notional_usd=bound,
                    fees_usd=fees,
                    planned_loss_usd=bound,
                    policy_hash=self.policy.policy_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(reservation_events).values(
                    reservation_id=reservation_id,
                    event_time=now,
                    kind="RESERVE",
                    quantity=quantity,
                    note=f"certification {grant.run_id} {step}: bound {bound} USD",
                )
            )
            conn.execute(
                insert(submission_outbox).values(
                    outbox_id=deterministic_id("outbox", intent_id),
                    intent_id=intent_id,
                    status="PENDING",
                    fencing_token=None,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return record

    # ------------------------------------------------------------------ exits
    def reserve_exit(
        self,
        *,
        account_id: str,
        instrument_id: str,
        purpose: OrderPurpose,
        reason: str,
        parent_intent_id: str | None,
    ) -> str | None:
        """Create a reducing sell for the owned, not-yet-committed quantity (or nothing)."""
        if not purpose.is_exit:
            raise ValueError("reserve_exit only creates exits")
        account = self.accounts[account_id]
        spec = account.specs[instrument_id]
        now = self.clock.now()
        with self.db.tx() as conn:
            self.leadership[account_id].assert_current(conn)
            position = self.ledger.position(conn, account_id, instrument_id)
            owned = position.quantity if position else ZERO
            committed = committed_sell_quantity(conn, account_id, instrument_id)
            approval = approve_reducing_exit(
                spec=spec,
                owned_quantity=owned,
                committed_sell_quantity=committed,
                requested_quantity=owned,
            )
            if approval is None:
                return None
            seq = self.db.next_counter(conn, "intent")
            intent_id = deterministic_id(
                "intent", self.namespace, account_id, instrument_id, purpose.value, str(seq)
            )
            intent = OrderIntent(
                intent_id=intent_id,
                client_order_id=client_order_id(f"{self.namespace}|{account_id}", intent_id),
                portfolio_id=self.portfolio_id,
                account_id=account_id,
                instrument_id=instrument_id,
                track=account.track,
                side=Side.SELL,
                purpose=purpose,
                order_type=OrderType.MARKET,
                quantity=approval.quantity,
                limit_price=None,
                time_in_force=TimeInForce.DAY,
                reason=reason,
                created_at=now,
                parent_intent_id=parent_intent_id,
            )
            version = self.ledger.bump_version(conn, account_id)
            plan = issue_plan(
                intent=intent,
                decision_id=f"exit:{intent_id}",
                account_version=version,
                policy_hash=self.policy.policy_hash,
                model_hash="exit-rule",
                quote_time=None,
                approved_at=now,
                valid_until=now + EXIT_PLAN_VALIDITY,
                reserved_cash_usd=ZERO,
                reserved_risk_usd=ZERO,
                exit_method="reducing-market-sell",
            )
            conn.execute(
                insert(order_intents).values(
                    intent_id=intent_id,
                    seq=seq,
                    portfolio_id=self.portfolio_id,
                    account_id=account_id,
                    client_order_id=intent.client_order_id,
                    instrument_id=instrument_id,
                    track=account.track.value,
                    side=Side.SELL.value,
                    purpose=purpose.value,
                    order_type=OrderType.MARKET.value,
                    quantity=approval.quantity,
                    limit_price=None,
                    time_in_force=TimeInForce.DAY.value,
                    state=OrderState.PENDING_SUBMIT.value,
                    decision_id=None,
                    candidate_id=None,
                    parent_intent_id=parent_intent_id,
                    intent_hash=plan.intent_hash,
                    plan_json=plan.model_dump_json(),
                    plan_digest=plan.digest,
                    valid_until=plan.valid_until,
                    entry_ttl_until=None,
                    broker_order_id=None,
                    cum_filled_qty=ZERO,
                    final_cum_qty=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(submission_outbox).values(
                    outbox_id=deterministic_id("outbox", intent_id),
                    intent_id=intent_id,
                    status="PENDING",
                    fencing_token=None,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return intent_id

    # ------------------------------------------------------------------ helpers
    def _record_rejection(
        self,
        conn: Connection,
        account: AccountRuntime,
        candidate: TradeCandidate,
        score: SignalScore | None,
        reasons: tuple[RejectReason, ...],
        details: tuple[str, ...],
        equity: Decimal,
        *,
        sizing: SizingResult | None = None,
        limit_price: Decimal | None = None,
    ) -> DecisionRecord:
        s = sizing
        record = DecisionRecord(
            decision_id=deterministic_id("decision", self.namespace, candidate.candidate_id),
            candidate_id=candidate.candidate_id,
            instrument_id=candidate.instrument_id,
            track=candidate.track,
            decided_at=self.clock.now(),
            approved=False,
            reasons=reasons,
            details=details,
            policy_hash=self.policy.policy_hash,
            model_hash=score.model_hash if score else "none",
            evidence=score.evidence if score else EdgeEvidence.NONE,
            quantity=s.quantity if s else ZERO,
            entry_limit_price=limit_price,
            notional_usd=max(s.notional_usd, ZERO) if s else ZERO,
            planned_loss_usd=max(s.planned_loss_usd, ZERO) if s else ZERO,
            estimated_costs_usd=max(s.estimated_costs_usd, ZERO) if s else ZERO,
            expected_net_usd=s.expected_net_usd if s else ZERO,
            effective_sizing_equity_usd=max(equity, ZERO),
            binding_constraint=s.binding_constraint if s else "",
        )
        self._insert_decision(conn, account.account_id, record)
        if score is not None:
            self._register_model(conn, score)
        return record

    def _insert_decision(self, conn: Connection, account_id: str, record: DecisionRecord) -> None:
        seq = self.db.next_counter(conn, "decision")
        conn.execute(
            insert(risk_decisions).values(
                decision_id=record.decision_id,
                seq=seq,
                candidate_id=record.candidate_id,
                portfolio_id=self.portfolio_id,
                account_id=account_id,
                approved=record.approved,
                reasons=",".join(r.value for r in record.reasons),
                decided_at=record.decided_at,
                policy_hash=record.policy_hash,
                model_hash=record.model_hash,
                payload_json=canonical_json(record),
            )
        )

    def _register_model(self, conn: Connection, score: SignalScore) -> None:
        insert_ignore(
            conn,
            model_versions,
            {
                "model_hash": score.model_hash,
                "kind": "FIXTURE_EDGE"
                if score.evidence is EdgeEvidence.SYNTHETIC_FIXTURE
                else score.evidence.value,
                "evidence": score.evidence.value,
                "description": f"edge source {score.edge_source_hash}",
                "registered_at": self.clock.now(),
            },
            ["model_hash"],
        )

    def has_active_entry(self, conn: Connection) -> bool:
        return (
            conn.execute(
                select(risk_reservations.c.reservation_id).where(
                    risk_reservations.c.portfolio_id == self.portfolio_id,
                    risk_reservations.c.kind == "ENTRY",
                    risk_reservations.c.state == "ACTIVE",
                )
            ).first()
            is not None
        )


__all__ = ["ENTRY_PLAN_VALIDITY", "ExchangeCalendar", "MarketInputs", "PortfolioAllocator"]
