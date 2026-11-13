"""Deterministic replay broker implementing the same contract as the paper adapter.

Fill rules (documented in docs/replay_broker.md and tested in tests/test_replay_broker.py):

1. An order becomes executable only for quotes whose *event time* is at least ``latency`` after
   the broker accepted it: never at the signal bar's close.
2. Marketable buys execute at ``ask * (1 + slippage)``, sells at ``bid * (1 - slippage)``.
   A limit order executes only if that price respects its limit, and then at that price.
3. Quantity per quote is capped by ``participation * displayed size`` (no infinite liquidity);
   ``PARTIAL_FIRST_FILL`` can force a deterministic partial execution.
4. Nothing executes while an instrument is halted; the first quote after a resume is used even
   if it gaps through a stop (stops are triggers, not guaranteed prices).
5. A cancel request is acknowledged immediately but takes effect on the next market event for
   that instrument; fills can race it (``FILL_AFTER_CANCEL_REQUEST``).
6. Open DAY orders expire at the session close.
7. Fees use the configured schedule at its charging granularity, per execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trader.accounting.fees import FeeSchedule
from trader.brokers.base import (
    BrokerTimeout,
    OrderScope,
    SimulatedCrash,
    UnauthorizedSubmission,
)
from trader.brokers.replay.faults import FaultKind, FaultPlan, FaultState
from trader.domain.account import (
    AccountSnapshot,
    BrokerFeeCharge,
    BrokerPosition,
    LegalAccountType,
    RuleProfileKind,
)
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.instruments import AnySpec
from trader.domain.market import AnyMarketEvent, Quote, TradingStatus
from trader.domain.orders import (
    ApprovedPlan,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderStatus,
    CancellationResult,
    FeeStatus,
    Fill,
    OrderType,
    SubmissionResult,
)
from trader.domain.types import BPS, ZERO, Side, round_down_to_step
from trader.ops.clock import Clock


@dataclass
class _Order:
    broker_order_id: str
    client_order_id: str
    instrument_id: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    purpose: str
    accepted_at: datetime
    status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED
    filled: Decimal = ZERO
    version: int = 0
    updated_at: datetime | None = None
    cancel_requested: bool = False
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal


@dataclass
class _Holding:
    quantity: Decimal = ZERO
    cost: Decimal = ZERO


class ReplayBroker:
    broker = "replay"

    def __init__(
        self,
        *,
        account_id: str,
        clock: Clock,
        capabilities: BrokerCapabilities,
        fees: FeeSchedule,
        specs: dict[str, AnySpec],
        starting_cash: Decimal = Decimal("100000"),
        latency: timedelta = timedelta(milliseconds=500),
        slippage_bps: Decimal = ZERO,
        participation: Decimal = Decimal("0.5"),
        faults: FaultPlan | None = None,
    ) -> None:
        self.account_id = account_id
        self.clock = clock
        self._caps = capabilities
        self.fees = fees
        self.specs = specs
        self.cash = starting_cash
        self.latency = latency
        self.slippage = slippage_bps * BPS
        self.participation = participation
        self.faults = FaultState(faults or FaultPlan())
        self._authority: object | None = None
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._holdings: dict[str, _Holding] = {}
        self._quotes: dict[str, Quote] = {}
        self._halted: set[str] = set()
        self._events: list[BrokerOrderEvent] = []
        self._executions: list[Fill] = []
        self._seq = 0
        self._exec_seq = 0
        self._visibility_misses: dict[str, int] = {}
        #: Number of times each client order ID reached the broker (duplicate detection).
        self.submission_attempts: dict[str, int] = {}

    # ------------------------------------------------------------------ protocol
    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    def bind_authority(self, authority: object) -> None:
        """Bind the submission gateway's authority.

        A newly started gateway supersedes the previous one (like a fencing token): a stale
        worker that survived a restart can no longer submit through this adapter.
        """
        self._authority = authority

    def get_account(self) -> AccountSnapshot:
        positions = tuple(
            BrokerPosition(
                instrument_id=iid,
                quantity=h.quantity,
                avg_entry_price=(h.cost / h.quantity) if h.quantity else ZERO,
                currency="USD",
            )
            for iid, h in sorted(self._holdings.items())
            if h.quantity != 0
        )
        equity = self.cash + sum(
            (
                h.quantity * self._quotes[i].bid
                for i, h in self._holdings.items()
                if i in self._quotes
            ),
            start=ZERO,
        )
        return AccountSnapshot(
            broker=self.broker,
            account_id=self.account_id,
            as_of=self.clock.now(),
            currency="USD",
            cash=self.cash,
            settled_cash=self.cash,
            buying_power=max(self.cash, ZERO),
            equity=equity,
            positions=positions,
            open_order_count=sum(1 for o in self._orders.values() if o.is_open),
            legal_type=LegalAccountType.SIMULATED,
            rule_profile=RuleProfileKind.UNVERIFIED,
        )

    def get_positions(self) -> list[BrokerPosition]:
        return list(self.get_account().positions)

    def list_orders(self, scope: OrderScope) -> list[BrokerOrder]:
        orders = [self._snapshot(o) for o in self._orders.values()]
        if scope is OrderScope.OPEN:
            return [o for o in orders if not o.status.is_terminal]
        if scope is OrderScope.CLOSED:
            return [o for o in orders if o.status.is_terminal]
        return orders

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        boid = self._by_client.get(client_order_id)
        if boid is None:
            return None
        delayed = self.faults.for_order(client_order_id, FaultKind.DELAYED_VISIBILITY)
        if delayed is not None and delayed.lookups is not None:
            misses = self._visibility_misses.get(client_order_id, 0)
            if misses < delayed.lookups:
                self._visibility_misses[client_order_id] = misses + 1
                return None
        return self._snapshot(self._orders[boid])

    def list_executions(self, *, broker_order_id: str | None = None) -> list[Fill]:
        if broker_order_id is None:
            return list(self._executions)
        return [f for f in self._executions if f.broker_order_id == broker_order_id]

    def list_fee_charges(self) -> list[BrokerFeeCharge]:
        """None: the simulated broker reports every fee on the fill itself."""
        return []

    def submit(self, plan: ApprovedPlan, *, authority: object) -> SubmissionResult:
        if self._authority is None or authority is not self._authority:
            raise UnauthorizedSubmission("only the submission gateway may submit orders")
        now = self.clock.now()
        if not plan.is_authentic():
            raise UnauthorizedSubmission("approved plan digest does not match its contents")
        if not plan.is_valid_at(now):
            raise UnauthorizedSubmission("approved plan is expired or not yet valid")
        intent = plan.intent
        cid = intent.client_order_id
        self.submission_attempts[cid] = self.submission_attempts.get(cid, 0) + 1
        faults = self.faults.register_submission(cid, intent.purpose.value)
        kinds = {f.kind for f in faults}
        if FaultKind.TIMEOUT_BEFORE_ACCEPT in kinds and self.submission_attempts[cid] == 1:
            raise BrokerTimeout("simulated timeout before the broker accepted the order")
        if cid in self._by_client:
            # Client order IDs are unique at this broker: a repeat is rejected, never re-filled.
            return SubmissionResult(
                accepted=False,
                broker_order_id=self._by_client[cid],
                status=BrokerOrderStatus.REJECTED,
                reject_reason="duplicate client_order_id",
            )
        if FaultKind.REJECT in kinds:
            return SubmissionResult(
                accepted=False,
                broker_order_id=None,
                status=BrokerOrderStatus.REJECTED,
                reject_reason="simulated broker rejection",
            )
        spec = self.specs.get(intent.instrument_id)
        if spec is None:
            return SubmissionResult(
                accepted=False,
                broker_order_id=None,
                status=BrokerOrderStatus.REJECTED,
                reject_reason="unknown instrument",
            )
        if intent.side is Side.SELL:
            held = self._holdings.get(intent.instrument_id, _Holding()).quantity
            open_sells = sum(
                (
                    o.remaining
                    for o in self._orders.values()
                    if o.is_open and o.side is Side.SELL and o.instrument_id == intent.instrument_id
                ),
                start=ZERO,
            )
            if intent.quantity > held - open_sells:
                return SubmissionResult(
                    accepted=False,
                    broker_order_id=None,
                    status=BrokerOrderStatus.REJECTED,
                    reject_reason="insufficient position (shorting not permitted)",
                )
        self._seq += 1
        boid = f"rb-{self._seq:06d}"
        order = _Order(
            broker_order_id=boid,
            client_order_id=cid,
            instrument_id=intent.instrument_id,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            purpose=intent.purpose.value,
            accepted_at=now,
            updated_at=now,
        )
        self._orders[boid] = order
        self._by_client[cid] = boid
        self._emit(order, now)
        if FaultKind.TIMEOUT_AFTER_ACCEPT in kinds:
            raise BrokerTimeout("simulated timeout after the broker accepted the order")
        if FaultKind.CRASH_AFTER_ACCEPT in kinds:
            raise SimulatedCrash("simulated worker crash after broker acceptance")
        return SubmissionResult(
            accepted=True, broker_order_id=boid, status=BrokerOrderStatus.ACCEPTED
        )

    def cancel(self, broker_order_id: str) -> CancellationResult:
        order = self._orders.get(broker_order_id)
        if order is None or not order.is_open:
            return CancellationResult(
                request_accepted=False, broker_order_id=broker_order_id, detail="not open"
            )
        order.cancel_requested = True
        order.version += 1
        order.updated_at = self.clock.now()
        self._emit(order, self.clock.now(), status=BrokerOrderStatus.PENDING_CANCEL)
        return CancellationResult(request_accepted=True, broker_order_id=broker_order_id)

    def drain_events(self) -> list[BrokerOrderEvent]:
        events, self._events = self._events, []
        # A lost account stream: these events never arrive; only REST reconciliation can see them.
        events = [
            e
            for e in events
            if not self.faults.for_order(e.client_order_id, FaultKind.SUPPRESS_STREAM_EVENTS)
        ]
        dup = [
            e
            for e in events
            if self.faults.for_order(e.client_order_id, FaultKind.DUPLICATE_EVENTS)
        ]
        if dup:
            events = [e for ev in events for e in ([ev, ev] if ev in dup else [ev])]
        if any(
            self.faults.for_order(e.client_order_id, FaultKind.OUT_OF_ORDER_EVENTS) for e in events
        ):
            events = list(reversed(events))
        return events

    # ------------------------------------------------------------------ simulation
    def on_market_event(self, event: AnyMarketEvent) -> None:
        iid = event.instrument_id
        if isinstance(event, TradingStatus):
            if event.halted:
                self._halted.add(iid)
            else:
                self._halted.discard(iid)
            return
        if not isinstance(event, Quote):
            return
        self._quotes[iid] = event
        for order in sorted(self._orders.values(), key=lambda o: o.broker_order_id):
            if order.instrument_id != iid or not order.is_open:
                continue
            if order.cancel_requested:
                if self.faults.for_order(
                    order.client_order_id, FaultKind.FILL_AFTER_CANCEL_REQUEST
                ):
                    self._try_fill(order, event)
                if order.is_open:
                    self._finish(order, BrokerOrderStatus.CANCELED, event.event_time)
                continue
            self._try_fill(order, event)

    def end_of_session(self, at: datetime) -> None:
        for order in sorted(self._orders.values(), key=lambda o: o.broker_order_id):
            if order.is_open:
                self._finish(order, BrokerOrderStatus.EXPIRED, at)

    def inject_manual_position(self, instrument_id: str, quantity: Decimal, price: Decimal) -> None:
        """Fault: someone traded the account outside the system."""
        h = self._holdings.setdefault(instrument_id, _Holding())
        h.quantity += quantity
        h.cost += quantity * price
        self.cash -= quantity * price

    # ------------------------------------------------------------------ internals
    def _try_fill(self, order: _Order, quote: Quote) -> None:
        if order.instrument_id in self._halted:
            return
        if quote.event_time < order.accepted_at + self.latency:
            return
        spec = self.specs[order.instrument_id]
        if order.side is Side.BUY:
            price = quote.ask * (1 + self.slippage)
            displayed = quote.ask_size
            if order.limit_price is not None and price > order.limit_price:
                return
        else:
            price = quote.bid * (1 - self.slippage)
            displayed = quote.bid_size
            if order.limit_price is not None and price < order.limit_price:
                return
        # Conservative rounding of the simulated execution price: buys up, sells down.
        price = price.quantize(
            Decimal("0.0001"), rounding=ROUND_CEILING if order.side is Side.BUY else ROUND_FLOOR
        )
        if order.limit_price is not None and (
            (order.side is Side.BUY and price > order.limit_price)
            or (order.side is Side.SELL and price < order.limit_price)
        ):
            return
        quantity = min(order.remaining, displayed * self.participation)
        forced = self.faults.fraction(order.client_order_id, FaultKind.PARTIAL_FIRST_FILL)
        if forced is not None and order.filled == 0:
            quantity = min(quantity, order.quantity * forced)
        quantity = round_down_to_step(quantity, spec.quantity_step)
        if quantity <= 0:
            return
        notional = quantity * price
        fee = self.fees.fee(order.side, quantity, notional)
        self._exec_seq += 1
        pending = self.faults.for_order(order.client_order_id, FaultKind.FEE_PENDING) is not None
        fill = Fill(
            broker=self.broker,
            account_id=self.account_id,
            execution_id=f"rx-{self._exec_seq:06d}",
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            instrument_id=order.instrument_id,
            side=order.side,
            quantity=quantity,
            price=price,
            currency=spec.currency,
            fee=fee,
            fee_currency=spec.currency,
            fee_status=FeeStatus.PENDING if pending else FeeStatus.FINAL,
            exec_time=quote.event_time,
            receipt_time=quote.receipt_time,
        )
        holding = self._holdings.setdefault(order.instrument_id, _Holding())
        if order.side is Side.BUY:
            holding.quantity += quantity
            holding.cost += notional
            self.cash -= notional + fee
        else:
            avg = holding.cost / holding.quantity if holding.quantity else ZERO
            holding.quantity -= quantity
            holding.cost -= avg * quantity if holding.quantity else holding.cost
            self.cash += notional - fee
        order.filled += quantity
        order.fills.append(fill)
        self._executions.append(fill)
        order.version += 1
        order.updated_at = quote.event_time
        order.status = (
            BrokerOrderStatus.FILLED if order.remaining == 0 else BrokerOrderStatus.PARTIALLY_FILLED
        )
        self._emit(order, quote.event_time, fill=fill)

    def _finish(self, order: _Order, status: BrokerOrderStatus, at: datetime) -> None:
        order.status = status
        order.version += 1
        order.updated_at = at
        self._emit(order, at)

    def _emit(
        self,
        order: _Order,
        at: datetime,
        *,
        fill: Fill | None = None,
        status: BrokerOrderStatus | None = None,
    ) -> None:
        st = status or order.status
        self._events.append(
            BrokerOrderEvent(
                broker=self.broker,
                account_id=self.account_id,
                event_id=f"{order.broker_order_id}:v{order.version}",
                broker_order_id=order.broker_order_id,
                client_order_id=order.client_order_id,
                status=st,
                order_quantity=order.quantity,
                cum_filled_quantity=order.filled,
                event_time=at,
                broker_version=order.version,
                fill=fill,
                raw={"purpose": order.purpose},
            )
        )

    def _snapshot(self, order: _Order) -> BrokerOrder:
        return BrokerOrder(
            broker=self.broker,
            account_id=self.account_id,
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            instrument_id=order.instrument_id,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
            status=order.status,
            filled_quantity=order.filled,
            broker_version=order.version,
            submitted_at=order.accepted_at,
            updated_at=order.updated_at or order.accepted_at,
        )
