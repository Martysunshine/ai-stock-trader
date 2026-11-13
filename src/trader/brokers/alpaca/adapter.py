"""Alpaca PAPER broker adapter (milestone M4; architecture.md §4, §7; markets_and_brokers.md §2).

Implements :class:`~trader.brokers.base.BrokerAdapter` over :class:`AlpacaTransport`.

Paper only, by construction: the constructor refuses any mode but ``Mode.PAPER``, any capability
profile whose environment is not ``paper`` or whose broker is not ``alpaca``, and a key ID that
looks like a live key. There is no base-URL parameter anywhere; :class:`SdkTransport` uses the
SDK's paper endpoint. ``runtime/live_guard.py`` certifies no live adapter.

Submission mirrors :class:`~trader.brokers.replay.broker.ReplayBroker`: only the bound gateway
authority may submit an authentic, unexpired plan; a client order ID this adapter already saw is
rejected locally and never sent twice; a sell beyond the broker position minus open sells is
refused (no shorting, even on a margin account). Entries are LIMIT DAY buys and exits MARKET DAY
sells (risk/allocator.py); a fractional quantity needs the matching *usable* capability.

Outcomes: a definite refusal is ``SubmissionResult(accepted=False)``; anything that may have
reached Alpaca without a definite answer raises :class:`~trader.brokers.base.BrokerTimeout`
(the gateway records ``UNKNOWN_OUTCOME``, pauses, and the reconciler resolves by client ID).
Alpaca's duplicate-client-order-ID refusal is such a case (the order exists); it raises the
:class:`~trader.brokers.base.DuplicateClientOrderId` subclass. A response or lookup that cannot
be mapped exactly raises :class:`MappingError` (``UnmappedStatus`` for an unmapped order
status); ``drain_events`` keeps every event it did map for the next call, and
``OrderManager.pump`` turns the error into an alert and an ``UNKNOWN_ORDER`` pause.

Certification (``trader paper certify``, domain/certification.py): only an adapter constructed
with the run's :class:`~trader.domain.certification.CertificationBudget` lets that run's plans
(``model_hash`` naming the grant) exercise the capabilities under test - a fractional LIMIT DAY
buy and a fractional MARKET DAY buy - and only within the USD 5 notional cap. Every submission
of such an adapter, exits included, counts against the run's order budget and is refused,
unsent, once the budget is used or the grant has expired.

Events and fills: a ``trade_updates`` message yields the order's state event
(``"{order_id}:v{updated_at in epoch microseconds}"``, the same id and version as the REST view of
that state). Executions come only from REST ``FILL`` activities, whose activity ``id`` is the one
canonical execution id; a stream fill triggers that REST fetch, retried on later drains until the
activities cover the order's filled quantity. Each (re)connection of the stream triggers a REST
refresh of the orders still working; while no stream runs, working orders are re-read every
:data:`POLL_INTERVAL_WITHOUT_STREAM`. Every REST call is paced by a token bucket on the injected
clock (200 requests/minute unless the profile states a limit). Order and execution history is
scoped to ``since``; open orders and positions are always reported in full.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Literal, TypeVar

from trader.brokers.alpaca.mapping import (
    FILL_EVENTS,
    JsonObject,
    MappingError,
    SymbolMap,
    fill_event,
    map_account,
    map_fee_activity,
    map_fill_activity,
    map_order,
    map_position,
    order_payload,
    parse_timestamp,
    parse_trade_update,
    status_event,
)
from trader.brokers.alpaca.pacing import DEFAULT_RATE_LIMIT_PER_MINUTE, TokenBucket
from trader.brokers.alpaca.stream import TradeUpdateStream
from trader.brokers.alpaca.transport import (
    AlpacaTransport,
    DefiniteRejection,
    NotFound,
    RateLimitObserver,
    SdkTransport,
)
from trader.brokers.base import (
    BrokerError,
    BrokerTimeout,
    DuplicateClientOrderId,
    OrderScope,
    UnauthorizedSubmission,
)
from trader.config.secrets import LIVE_KEY_PREFIX
from trader.data.sources.alpaca_history import AlpacaCredentials
from trader.domain.account import (
    AccountSnapshot,
    BrokerFeeCharge,
    BrokerPosition,
    LegalAccountType,
    RuleProfileKind,
)
from trader.domain.capabilities import (
    FRACTIONAL_LIMIT_DAY,
    FRACTIONAL_MARKET_DAY,
    BrokerCapabilities,
)
from trader.domain.certification import (
    CERTIFICATION_MAX_ORDER_NOTIONAL_USD,
    AssetMetadata,
    CertificationBudget,
)
from trader.domain.instruments import InstrumentSpec
from trader.domain.orders import (
    ApprovedPlan,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderStatus,
    CancellationResult,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderType,
    SubmissionResult,
    TimeInForce,
)
from trader.domain.types import ZERO, DomainError, Mode, Side, require_utc
from trader.ops.clock import Clock

log = logging.getLogger(__name__)

BROKER = "alpaca_paper"
#: ``BrokerCapabilities.broker`` of an Alpaca profile (configs/capabilities/alpaca_*.yaml).
PROFILE_BROKER = "alpaca"
ORDER_PAGE_SIZE = 500
ACTIVITY_PAGE_SIZE = 100
#: While no trade stream is running, ``drain_events`` re-reads working orders over REST at most
#: this often (injected clock), so fills are still discovered without the websocket.
POLL_INTERVAL_WITHOUT_STREAM = timedelta(seconds=5)
_ORDER_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ONE_MICROSECOND = timedelta(microseconds=1)
#: A fee is booked the night after its trade date; a week covers weekends and late bookings.
FEE_BOOKING_LAG = timedelta(days=7)


def _iso_date(value: object) -> date | None:
    """``YYYY-MM-DD`` -> date; ``None`` when the value is not such a date."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_T = TypeVar("_T")


class PaperOnlyError(DomainError):
    """The adapter refuses anything but an Alpaca paper account in paper mode."""


@dataclass
class _Tracked:
    """The newest known state of one Alpaca order and the executions already emitted."""

    order: BrokerOrder
    alpaca_status: str
    executions: set[str] = field(default_factory=set)
    fills_quantity: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class MarketClock:
    as_of: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


def refuse_unless_paper(
    mode: Mode, capabilities: BrokerCapabilities, credentials: AlpacaCredentials
) -> None:
    if mode is not Mode.PAPER:
        raise PaperOnlyError(f"the Alpaca adapter runs only in paper mode, not {mode.value}")
    if capabilities.environment != "paper":
        raise PaperOnlyError(
            f"capability profile {capabilities.profile_version} is for environment "
            f"{capabilities.environment!r}; the Alpaca adapter accepts only 'paper'"
        )
    if capabilities.broker != PROFILE_BROKER:
        raise PaperOnlyError(
            f"capability profile {capabilities.profile_version} is for broker "
            f"{capabilities.broker!r}, not {PROFILE_BROKER!r}"
        )
    if credentials.key_id.strip().upper().startswith(LIVE_KEY_PREFIX):
        raise PaperOnlyError("the key ID looks like a LIVE Alpaca key; only paper keys are used")


class AlpacaPaperAdapter:
    broker = BROKER

    def __init__(
        self,
        *,
        account_id: str,
        credentials: AlpacaCredentials,
        capabilities: BrokerCapabilities,
        instruments: Mapping[str, InstrumentSpec],
        clock: Clock,
        mode: Mode,
        since: datetime,
        transport: AlpacaTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        certification: CertificationBudget | None = None,
    ) -> None:
        refuse_unless_paper(mode, capabilities, credentials)
        if certification is not None and certification.grant.account_id != account_id:
            raise PaperOnlyError("the certification grant is for another account")
        #: Set only by ``trader paper certify`` (domain/certification.py): this run's plans may
        #: exercise the capabilities under test, and every submission counts against its budget.
        self._certification = certification
        self.account_id = account_id
        self.clock = clock
        self.since = require_utc(since)
        self._caps = capabilities
        self._specs = dict(instruments)
        self._symbols = SymbolMap.from_specs(self._specs)
        self._transport: AlpacaTransport = (
            transport if transport is not None else SdkTransport(credentials)
        )
        self._bucket = TokenBucket(
            limit_per_minute=capabilities.rate_limit_per_minute or DEFAULT_RATE_LIMIT_PER_MINUTE,
            clock=clock,
            sleep=sleep if sleep is not None else time.sleep,
        )
        self._authority: object | None = None
        self._by_client: dict[str, str] = {}
        self._tracked: dict[str, _Tracked] = {}
        self._pending: list[BrokerOrderEvent] = []
        self._refresh: set[str] = set()
        self._fill_sync: set[str] = set()
        self._stream: TradeUpdateStream | None = None
        self._connections_seen = 0
        self._last_poll: datetime | None = None
        #: Number of times each client order ID reached ``submit`` (duplicate detection).
        self.submission_attempts: dict[str, int] = {}
        #: Mapping failures seen while draining events (owner review; never silently dropped).
        self.anomalies: list[str] = []
        #: Why a submission was definitely refused, by client order ID (locally or by Alpaca).
        self.rejections: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"AlpacaPaperAdapter(account_id={self.account_id!r}, broker={self.broker!r})"

    # ------------------------------------------------------------------ lifecycle
    def start_stream(self) -> None:
        """Start the background ``trade_updates`` stream (idempotent)."""
        if self._stream is not None:
            return
        self._stream = TradeUpdateStream(
            open_session=self._transport.open_trade_stream, clock=self.clock
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()

    # ------------------------------------------------------------------ protocol
    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    def bind_authority(self, authority: object) -> None:
        """Bind the submission gateway's authority (a newer gateway supersedes the old one)."""
        self._authority = authority

    def get_account(self) -> AccountSnapshot:
        raw = self._rest(self._transport.get_account)
        positions = self.get_positions()
        open_orders = self.list_orders(OrderScope.OPEN)
        certified = self._caps.rule_profile is not RuleProfileKind.UNVERIFIED
        return map_account(
            raw,
            broker=self.broker,
            account_id=self.account_id,
            as_of=self.clock.now(),
            positions=positions,
            open_order_count=len(open_orders),
            legal_type=self._caps.account_legal_type if certified else LegalAccountType.UNKNOWN,
            rule_profile=self._caps.rule_profile,
        )

    def get_positions(self) -> list[BrokerPosition]:
        positions = [
            map_position(raw, symbols=self._symbols)
            for raw in self._rest(self._transport.list_positions)
        ]
        return sorted(positions, key=lambda p: p.instrument_id)

    def list_orders(self, scope: OrderScope) -> list[BrokerOrder]:
        """Working orders whenever submitted; terminal ones submitted at or after ``since``.

        Open/closed follows the mapped status (as in the replay broker), not Alpaca's own
        grouping: both of Alpaca's lists are read (its closed list from ``since``), so an order
        Alpaca files as closed but that is still working for this system (e.g. ``done_for_day``)
        is reported as OPEN and keeps its exposure. The closed list is read second, so an order
        that closed between the two reads is reported in its newer state.
        """
        raws: dict[str, JsonObject] = {}
        raws.update((str(r.get("id")), r) for r in self._orders_raw("open", None))
        after = _iso_micros(self.since - _ONE_MICROSECOND)
        raws.update((str(r.get("id")), r) for r in self._orders_raw("closed", after))
        orders = [self._observe(raw) for raw in raws.values()]
        working = [o for o in orders if not o.status.is_terminal]
        closed = [o for o in orders if o.status.is_terminal and o.submitted_at >= self.since]
        selected = {OrderScope.OPEN: working, OrderScope.CLOSED: closed}.get(
            scope, working + closed
        )
        return sorted(selected, key=lambda o: (o.submitted_at, o.broker_order_id))

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        raw = self._rest(partial(self._transport.get_order_by_client_id, client_order_id))
        return None if raw is None else self._observe(raw)

    def list_executions(self, *, broker_order_id: str | None = None) -> list[Fill]:
        """FILL activities (canonical execution ids), oldest first: every execution at or after
        ``since``, or one order's complete history."""
        if broker_order_id is None:
            raws = self._activities(order_id=None, since=self.since)
        elif not _ORDER_ID.fullmatch(broker_order_id):
            return []
        else:
            raws = [
                raw
                for raw in self._activities(order_id=broker_order_id, since=None)
                if raw.get("order_id") == broker_order_id
            ]
        return [self._fill(raw) for raw in raws]

    def list_fee_charges(self) -> list[BrokerFeeCharge]:
        """FEE activities booked at or after ``since``, oldest first (ADR-0054).

        Alpaca books a trading day's regulatory fees during the following night, so a fee
        counts from its booking time (``created_at``), not its trade date: one booked before
        ``since`` is already in the cash baseline. Pages are read newest first. Activity ids
        start with the trade date, so reading stops once a page reaches trade dates more than
        :data:`FEE_BOOKING_LAG` before ``since``; a row without a readable date never stops it.
        """
        charges: list[BrokerFeeCharge] = []
        seen: set[str] = set()
        token: str | None = None
        oldest_trade_date = (self.since - FEE_BOOKING_LAG).date()
        while True:
            page = self._rest(
                partial(
                    self._transport.list_fee_activities,
                    direction="desc",
                    page_token=token,
                    page_size=ACTIVITY_PAGE_SIZE,
                )
            )
            reached_since = False
            for raw in page:
                activity_id = raw.get("id")
                if not isinstance(activity_id, str):
                    raise MappingError("activity without an id")
                if activity_id in seen:
                    continue
                seen.add(activity_id)
                trade_date = _iso_date(raw.get("date"))
                if trade_date is not None and trade_date < oldest_trade_date:
                    reached_since = True
                    continue
                charge = map_fee_activity(raw, currency="USD")
                if charge is not None and charge.charged_at >= self.since:
                    charges.append(charge)
            if reached_since or len(page) < ACTIVITY_PAGE_SIZE:
                return sorted(charges, key=lambda c: (c.charged_at, c.charge_id))
            last = page[-1].get("id")
            if not isinstance(last, str) or last == token:
                raise MappingError("activity pagination did not advance")
            token = last

    def submit(self, plan: ApprovedPlan, *, authority: object) -> SubmissionResult:
        if self._authority is None or authority is not self._authority:
            raise UnauthorizedSubmission("only the submission gateway may submit orders")
        if not plan.is_authentic():
            raise UnauthorizedSubmission("approved plan digest does not match its contents")
        if not plan.is_valid_at(self.clock.now()):
            raise UnauthorizedSubmission("approved plan is expired or not yet valid")
        intent = plan.intent
        cid = intent.client_order_id
        self.submission_attempts[cid] = self.submission_attempts.get(cid, 0) + 1
        known = self._by_client.get(cid)
        if known is not None:
            # Client order IDs are unique: a repeat is rejected here and never sent again.
            return SubmissionResult(
                accepted=False,
                broker_order_id=known,
                status=BrokerOrderStatus.REJECTED,
                reject_reason="duplicate client_order_id",
            )
        spec = self._specs.get(intent.instrument_id)
        if spec is None:
            return self._refuse(cid, "unknown instrument")
        problem = self._shape_problem(plan, spec)
        if problem is None and intent.side is Side.SELL:
            problem = self._short_sale_problem(intent)
        if problem is not None:
            return self._refuse(cid, problem)
        payload = order_payload(intent, symbol=spec.identity.symbol)
        self._bucket.acquire()
        if not plan.is_valid_at(self.clock.now()):
            return self._refuse(cid, "approved plan expired while waiting for a request slot")
        budget = self._certification
        if budget is not None:
            if not budget.grant.is_active(self.clock.now()):
                return self._refuse(cid, "the certification grant has expired; nothing was sent")
            if not budget.consume(cid):
                return self._refuse(
                    cid,
                    f"certification order budget exhausted ({budget.grant.max_orders} "
                    "submissions); nothing was sent",
                )
        try:
            raw = self._transport.submit_order(payload)
        except DefiniteRejection as exc:
            if exc.is_duplicate_client_order_id:
                raise DuplicateClientOrderId(
                    cid,
                    detail=f"Alpaca already holds it: HTTP {exc.http_status} code {exc.code}: "
                    f"{exc.message}",
                ) from None
            return self._refuse(
                cid, f"alpaca HTTP {exc.http_status} code {exc.code}: {exc.message}"
            )
        try:
            order = self._observe(raw)
        except MappingError as exc:
            raise BrokerTimeout(
                f"{cid}: unmappable order response, outcome unknown: {exc}"
            ) from None
        if order.client_order_id != cid:
            raise BrokerTimeout(f"{cid}: Alpaca answered for client order {order.client_order_id}")
        self._pending.append(
            status_event(order, source="alpaca-submit", alpaca_status=str(raw.get("status")))
        )
        if order.status is BrokerOrderStatus.REJECTED:
            self.rejections[cid] = "Alpaca reported the order as rejected"
            return SubmissionResult(
                accepted=False,
                broker_order_id=order.broker_order_id,
                status=BrokerOrderStatus.REJECTED,
                reject_reason=self.rejections[cid],
            )
        return SubmissionResult(
            accepted=True, broker_order_id=order.broker_order_id, status=order.status
        )

    def cancel(self, broker_order_id: str) -> CancellationResult:
        """Request a cancel. A timeout raises ``BrokerTimeout`` (the request may have landed)."""
        if not _ORDER_ID.fullmatch(broker_order_id):
            return CancellationResult(
                request_accepted=False,
                broker_order_id=broker_order_id,
                detail="not an Alpaca order id",
            )
        try:
            self._rest(partial(self._transport.cancel_order, broker_order_id))
        except NotFound:
            return CancellationResult(
                request_accepted=False, broker_order_id=broker_order_id, detail="order not found"
            )
        except DefiniteRejection as exc:
            return CancellationResult(
                request_accepted=False,
                broker_order_id=broker_order_id,
                detail=f"alpaca HTTP {exc.http_status} code {exc.code}: {exc.message}",
            )
        self._refresh.add(broker_order_id)
        return CancellationResult(
            request_accepted=True, broker_order_id=broker_order_id, detail="cancel requested"
        )

    def drain_events(self) -> list[BrokerOrderEvent]:
        """Stream state events, REST refreshes and new canonical fills, in that order.

        A message or order that cannot be mapped raises after the batch is assembled (the first
        problem; every problem is logged and kept in :attr:`anomalies`); the events that were
        mapped are kept and returned by the next call, so nothing is lost. A REST timeout here
        is not raised: the refresh or fill fetch is retried on the next call.
        """
        events, self._pending = self._pending, []
        try:
            problems = self._collect(events)
        except Exception:
            self._pending = events + self._pending
            raise
        if problems:
            for problem in problems:
                log.warning("alpaca order update not applied: %s", problem)
                self.anomalies.append(str(problem))
            self._pending = events + self._pending
            raise problems[0]
        return events

    def _collect(self, events: list[BrokerOrderEvent]) -> list[BrokerError]:
        """Append stream, refresh and fill events; return the problems met on the way."""
        problems: list[BrokerError] = []
        if self._stream is not None:
            for _received_at, message in self._stream.drain():
                try:
                    update = parse_trade_update(message)
                    order = self._observe(update.order)
                except MappingError as exc:
                    problems.append(exc)
                    continue
                events.append(
                    status_event(
                        order,
                        source="alpaca-stream",
                        alpaca_status=str(update.order.get("status")),
                    )
                )
                if update.event in FILL_EVENTS:
                    self._fill_sync.add(order.broker_order_id)
            connections = self._stream.connections
            if connections != self._connections_seen:
                # Events during the gap may be lost: re-read every order still working.
                self._connections_seen = connections
                self._refresh.update(self._working())
        # Poll working orders on the interval even while the stream thread runs: alpaca-py
        # reconnects inside that thread, so a live thread does not prove a connected socket, and
        # fills must not wait for the 60 s reconciliation (review finding, 2026-11-08). The cost
        # is one GET per working order every 5 s, far below the 200/min rate limit.
        now = self.clock.now()
        if self._last_poll is None or now - self._last_poll >= POLL_INTERVAL_WITHOUT_STREAM:
            self._last_poll = now
            self._refresh.update(self._working())
        for boid in sorted(self._refresh):
            try:
                raw = self._rest(partial(self._transport.get_order, boid))
                order = self._observe(raw)
            except BrokerTimeout:
                continue  # retried on the next drain
            except BrokerError as exc:
                problems.append(exc)
            else:
                events.append(
                    status_event(order, source="alpaca-rest", alpaca_status=str(raw.get("status")))
                )
            self._refresh.discard(boid)
        for boid in sorted(self._fill_sync):
            try:
                events.extend(self._new_fill_events(boid))
            except BrokerTimeout:
                continue
            except BrokerError as exc:
                problems.append(exc)
                self._fill_sync.discard(boid)
        return problems

    # ------------------------------------------------------------------ extras
    def market_clock(self) -> MarketClock:
        raw = self._rest(self._transport.get_clock)
        return MarketClock(
            as_of=parse_timestamp(raw.get("timestamp"), "timestamp"),
            is_open=raw.get("is_open") is True,
            next_open=parse_timestamp(raw.get("next_open"), "next_open"),
            next_close=parse_timestamp(raw.get("next_close"), "next_close"),
        )

    def verify_instruments(self) -> list[str]:
        """Differences between the configured specs and Alpaca's asset metadata."""
        problems: list[str] = []
        for instrument_id, spec in sorted(self._specs.items()):
            symbol = spec.identity.symbol
            try:
                asset = self._rest(partial(self._transport.get_asset, symbol))
            except NotFound:
                problems.append(f"{instrument_id}: Alpaca has no asset {symbol}")
                continue
            if asset.get("status") != "active" or asset.get("tradable") is not True:
                problems.append(f"{instrument_id}: {symbol} is not active and tradable at Alpaca")
            if (asset.get("fractionable") is True) != spec.fractionable:
                problems.append(f"{instrument_id}: fractionable differs from Alpaca")
            if asset.get("id") != spec.identity.broker_asset_id:
                problems.append(f"{instrument_id}: broker_asset_id differs from the Alpaca id")
        return problems

    def asset_metadata(self, instrument_id: str) -> AssetMetadata:
        """Alpaca's asset record for a configured instrument (``GET /v2/assets/{symbol}``)."""
        spec = self._specs.get(instrument_id)
        if spec is None:
            raise MappingError(f"{instrument_id} is not a configured instrument")
        symbol = spec.identity.symbol
        raw = self._rest(partial(self._transport.get_asset, symbol))
        if raw.get("symbol") != symbol:
            raise MappingError(f"asset response for {raw.get('symbol')!r}, expected {symbol!r}")
        flags = {}
        for name in ("tradable", "fractionable", "marginable", "shortable"):
            value = raw.get(name, False)
            if not isinstance(value, bool):
                raise MappingError(f"asset {symbol}: {name} is not a boolean")
            flags[name] = value
        return AssetMetadata(
            instrument_id=instrument_id,
            symbol=symbol,
            broker_asset_id=str(raw.get("id", "")),
            asset_class=str(raw.get("class", "")),
            exchange=str(raw.get("exchange", "")),
            status=str(raw.get("status", "")),
            tradable=flags["tradable"],
            fractionable=flags["fractionable"],
            marginable=flags["marginable"],
            shortable=flags["shortable"],
        )

    def rate_limit_observation(self) -> dict[str, str]:
        """The newest rate-limit response headers, when the transport exposes them."""
        if isinstance(self._transport, RateLimitObserver):
            return dict(self._transport.rate_limit_headers())
        return {}

    # ------------------------------------------------------------------ internals
    def _working(self) -> list[str]:
        """Broker order ids of tracked orders that are not terminal."""
        return [boid for boid, t in self._tracked.items() if not t.order.status.is_terminal]

    def _rest(self, call: Callable[[], _T]) -> _T:
        self._bucket.acquire()
        return call()

    def _observe(self, raw: JsonObject) -> BrokerOrder:
        """Map an order and remember its newest state."""
        order = map_order(
            raw, broker=self.broker, account_id=self.account_id, symbols=self._symbols
        )
        tracked = self._tracked.get(order.broker_order_id)
        if tracked is None:
            tracked = _Tracked(order=order, alpaca_status=str(raw.get("status")))
            self._tracked[order.broker_order_id] = tracked
        elif order.broker_version >= tracked.order.broker_version:
            tracked.order = order
            tracked.alpaca_status = str(raw.get("status"))
        self._by_client[order.client_order_id] = order.broker_order_id
        if tracked.order.filled_quantity > tracked.fills_quantity:
            self._fill_sync.add(order.broker_order_id)
        return order

    def _orders_raw(self, status: str, after: str | None) -> list[JsonObject]:
        out: list[JsonObject] = []
        seen: set[str] = set()
        cursor = after
        while True:
            page = self._rest(
                partial(
                    self._transport.list_orders, status=status, after=cursor, limit=ORDER_PAGE_SIZE
                )
            )
            fresh = 0
            for raw in page:
                boid = raw.get("id")
                if not isinstance(boid, str):
                    raise MappingError("order without an id")
                if boid not in seen:
                    seen.add(boid)
                    out.append(raw)
                    fresh += 1
            if len(page) < ORDER_PAGE_SIZE:
                return out
            if fresh == 0:
                raise MappingError("order pagination did not advance")
            last = parse_timestamp(page[-1].get("submitted_at"), "submitted_at")
            # ``after`` is exclusive: step back one microsecond and de-duplicate by id.
            cursor = _iso_micros(last - _ONE_MICROSECOND)

    def _activities(self, *, order_id: str | None, since: datetime | None) -> list[JsonObject]:
        """FILL activities, de-duplicated by id and returned oldest first.

        With ``since`` the pages are read newest first and reading stops at the first activity
        older than ``since``. Alpaca documents the activities ``after`` filter in terms of
        ``created_at``, which is null for FILL activities, so no server-side time filter is
        relied on: the bound is applied to ``transaction_time`` here.
        """
        out: list[JsonObject] = []
        seen: set[str] = set()
        token: str | None = None
        direction: Literal["asc", "desc"] = "asc" if since is None else "desc"
        while True:
            page = self._rest(
                partial(
                    self._transport.list_fill_activities,
                    order_id=order_id,
                    direction=direction,
                    page_token=token,
                    page_size=ACTIVITY_PAGE_SIZE,
                )
            )
            reached_since = False
            for raw in page:
                activity_id = raw.get("id")
                if not isinstance(activity_id, str):
                    raise MappingError("activity without an id")
                when = parse_timestamp(raw.get("transaction_time"), "transaction_time")
                if since is not None and when < since:
                    reached_since = True
                elif activity_id not in seen:
                    seen.add(activity_id)
                    out.append(raw)
            if reached_since or len(page) < ACTIVITY_PAGE_SIZE:
                return out if since is None else out[::-1]
            last = page[-1].get("id")
            if not isinstance(last, str) or last == token:
                raise MappingError("activity pagination did not advance")
            token = last

    def _order_for(self, broker_order_id: str) -> BrokerOrder:
        tracked = self._tracked.get(broker_order_id)
        if tracked is not None:
            return tracked.order
        if not _ORDER_ID.fullmatch(broker_order_id):
            raise MappingError(f"activity refers to a malformed order id {broker_order_id!r}")
        return self._observe(self._rest(partial(self._transport.get_order, broker_order_id)))

    def _fill(self, raw: JsonObject) -> Fill:
        order = self._order_for(str(raw.get("order_id")))
        spec = self._specs.get(order.instrument_id)
        currency = spec.currency if spec is not None else "USD"
        return map_fill_activity(
            raw,
            broker=self.broker,
            account_id=self.account_id,
            client_order_id=order.client_order_id,
            symbols=self._symbols,
            currency=currency,
            receipt_time=self.clock.now(),
        )

    def _new_fill_events(self, broker_order_id: str) -> list[BrokerOrderEvent]:
        tracked = self._tracked[broker_order_id]
        fresh: dict[str, Fill] = {}
        for raw in self._activities(order_id=broker_order_id, since=None):
            if raw.get("order_id") != broker_order_id:
                continue
            fill = self._fill(raw)
            if fill.execution_id not in tracked.executions:
                fresh.setdefault(fill.execution_id, fill)
        # Commit only after every activity mapped, so a failure cannot lose an execution.
        for fill in fresh.values():
            tracked.executions.add(fill.execution_id)
            tracked.fills_quantity += fill.quantity
        if tracked.fills_quantity >= tracked.order.filled_quantity:
            self._fill_sync.discard(broker_order_id)
        if tracked.fills_quantity > tracked.order.filled_quantity:
            self._refresh.add(broker_order_id)  # the order moved on; fetch its newer state
        # History before ``since`` counts towards the order's quantity but is never emitted.
        return [fill_event(tracked.order, f) for f in fresh.values() if f.exec_time >= self.since]

    def _refuse(self, client_order_id: str, reason: str) -> SubmissionResult:
        """A definite refusal (nothing created at Alpaca), remembered for owner reports."""
        self.rejections[client_order_id] = reason
        return _not_sent(reason)

    def _certifies(self, plan: ApprovedPlan) -> bool:
        """Whether ``plan`` was approved for this adapter's certification run."""
        budget = self._certification
        return budget is not None and budget.grant.covers(plan.model_hash)

    def _shape_problem(self, plan: ApprovedPlan, spec: InstrumentSpec) -> str | None:
        intent = plan.intent
        certifying = self._certifies(plan)
        if intent.time_in_force is not TimeInForce.DAY:
            return "only DAY orders are sent to Alpaca in v1"
        if intent.purpose is OrderPurpose.ENTRY:
            # A certification run's fractional MARKET DAY buy is the one non-limit entry.
            shapes = (OrderType.LIMIT, OrderType.MARKET) if certifying else (OrderType.LIMIT,)
            if intent.order_type not in shapes or intent.side is not Side.BUY:
                return "entries are LIMIT DAY buys"
            certification = spec.certify()
            if not certification.certified:
                return "instrument not certified: " + "; ".join(certification.problems)
            if certifying:
                problem = self._certification_cap_problem(plan)
                if problem is not None:
                    return problem
        elif intent.order_type is not OrderType.MARKET or intent.side is not Side.SELL:
            return "exits are MARKET DAY sells"
        if intent.quantity != intent.quantity.to_integral_value():
            if not spec.fractionable:
                return "fractional quantity on a non-fractionable instrument"
            needed = (
                FRACTIONAL_LIMIT_DAY
                if intent.order_type is OrderType.LIMIT
                else FRACTIONAL_MARKET_DAY
            )
            budget = self._certification
            under_test = (
                certifying and budget is not None and needed in budget.grant.capabilities_under_test
            )
            if not self._caps.usable(needed) and not under_test:
                return f"capability {needed} is not usable in {self._caps.profile_version}"
        return None

    def _certification_cap_problem(self, plan: ApprovedPlan) -> str | None:
        """Defence in depth: a certification buy stays within the run's notional cap."""
        budget = self._certification
        if budget is None:  # pragma: no cover - only called while certifying
            return "not a certification run"
        cap = min(budget.grant.max_order_notional_usd, CERTIFICATION_MAX_ORDER_NOTIONAL_USD)
        intent = plan.intent
        if plan.reserved_cash_usd > cap:
            return f"certification order bound {plan.reserved_cash_usd} USD exceeds the cap {cap}"
        if intent.limit_price is not None and intent.quantity * intent.limit_price > cap:
            return f"certification limit order notional exceeds the cap {cap} USD"
        return None

    def _short_sale_problem(self, intent: OrderIntent) -> str | None:
        try:
            held = sum(
                (
                    p.quantity
                    for p in self.get_positions()
                    if p.instrument_id == intent.instrument_id
                ),
                start=ZERO,
            )
            open_sells = sum(
                (
                    o.quantity - o.filled_quantity
                    for o in self.list_orders(OrderScope.OPEN)
                    if o.side is Side.SELL and o.instrument_id == intent.instrument_id
                ),
                start=ZERO,
            )
        except BrokerError as exc:
            return f"pre-trade position check failed ({type(exc).__name__}); nothing was sent"
        except BrokerTimeout:
            return "pre-trade position check timed out; nothing was sent"
        if intent.quantity > held - open_sells:
            return "insufficient position (shorting not permitted)"
        return None


def _not_sent(reason: str) -> SubmissionResult:
    """A definite refusal: the order was not created at Alpaca."""
    return SubmissionResult(
        accepted=False,
        broker_order_id=None,
        status=BrokerOrderStatus.REJECTED,
        reject_reason=reason,
    )


def _iso_micros(value: datetime) -> str:
    return require_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = [
    "BROKER",
    "POLL_INTERVAL_WITHOUT_STREAM",
    "AlpacaPaperAdapter",
    "MarketClock",
    "PaperOnlyError",
    "refuse_unless_paper",
]
