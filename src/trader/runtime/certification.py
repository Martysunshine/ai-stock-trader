"""Paper certification run: ``trader paper certify`` (implementation_roadmap.md M4; architecture.md
§4 evidence levels, §7 order lifecycle; markets_and_brokers.md §2).

configs/capabilities/alpaca_documented.yaml lists what Alpaca *documents*. This run demonstrates,
on the owner's PAPER account, the items that are still UNKNOWN there, with a handful of tiny
orders, and writes the evidence to ``runs/paper/certification/<UTC timestamp>.json``
(:class:`CertificationReport`; no secrets). ``trader paper certify-profile`` turns a report into a
*proposed* profile (``runtime/certified_profile.py``).

Bounds (domain/certification.py): every order's notional bound is at most USD 5 and the run sends
at most 6 orders (the plan uses 4). The run holds a :class:`~trader.domain.certification.
CertificationGrant`; every order still passes the risk engine
(:func:`~trader.risk.engine.evaluate_certification_order`), the approval factory and the
submission gateway, and the adapter refuses, unsent, anything beyond the grant.

Steps, after a preflight that sends nothing (broker clock open and at least 15 minutes before the
close, asset active/tradable/fractionable, account flat in the symbol, startup reconciliation
PASSED, fresh quote):

1. ``fractional_limit_day``: a fractional LIMIT DAY buy (about USD 4) resting far below the bid;
   lookups and seconds until it is visible by client order ID;
2. ``client_order_id_unique``: the same approved plan again through a second adapter instance
   (a restarted worker): Alpaca must refuse the client order ID; exactly one order may exist;
3. ``cancel``: cancel order 1, timing the request until Alpaca reports it terminal;
4. ``fractional_market_day``: a fractional MARKET DAY buy (USD 1-2), wait for the fill (fills and
   the broker cash change recorded), then the ordinary reducing MARKET DAY sell of the bought
   quantity, wait for the fill; reconcile (it must pass) and check the account is flat.

Market-order steps never run while the broker clock says the market is closed; the run refuses
before any order instead. Anything left working is cancelled at the end; a position that could
not be sold is reported as PENDING LIQUIDATION (``trader close-positions --config``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import Field
from sqlalchemy import select

from trader.brokers.base import BrokerAdapter, BrokerError, BrokerTimeout, OrderScope
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.runtime_config import ResolvedConfig
from trader.data.history import DataQualityError
from trader.data.sources.http import TransportError
from trader.domain.base import ConfigModel
from trader.domain.certification import (
    CERTIFICATION_MAX_ORDER_NOTIONAL_USD,
    CERTIFICATION_MAX_ORDERS,
    AssetMetadata,
    CertificationBudget,
    CertificationGrant,
    certification_run_id,
)
from trader.domain.hashing import content_hash
from trader.domain.instruments import AnySpec, EquitySpec
from trader.domain.market import Quote
from trader.domain.orders import BrokerOrder, BrokerOrderStatus, OrderPurpose, OrderState, OrderType
from trader.domain.types import (
    ZERO,
    Dec,
    DomainError,
    EvidenceLevel,
    Mode,
    UtcDatetime,
    canon_decimal,
    iso_utc,
    round_down_to_step,
)
from trader.execution.exits import ExitTrigger
from trader.ops.clock import SleepingClock
from trader.ops.leadership import LeadershipUnavailable
from trader.risk.allocator import MarketInputs
from trader.risk.engine import CERTIFICATION_MARKET_PRICE_BUFFER, CERTIFICATION_MIN_PASSIVE_DISCOUNT
from trader.runtime.decision_loop import DecisionLoop
from trader.runtime.paper import (
    PaperBroker,
    PaperEnvironment,
    PaperStartRefused,
    ensure_leadership_free,
    paper_account,
)
from trader.runtime.paper_data import PaperMarketData, quote_from_source
from trader.storage.db import Database
from trader.storage.schema import fills, order_intents

REPORT_KIND: Final = "alpaca-paper-certification"
REPORT_VERSION: Final = 1
DEFAULT_REPORT_DIR = Path("runs") / "paper" / "certification"
CERTIFICATION_HOLDER = "paper-certify"
#: Orders the plan sends: passive limit, its duplicate, market buy, market sell.
PLANNED_SUBMISSIONS = 4
_MS = Decimal("0.001")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
StepStatus = Literal["PASSED", "FAILED", "SKIPPED"]


class CertificationRefused(DomainError):
    """The certification run refuses to place orders (nothing was sent)."""


# --------------------------------------------------------------------------- broker protocol


class MarketClockView(Protocol):
    @property
    def as_of(self) -> datetime: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def next_open(self) -> datetime: ...
    @property
    def next_close(self) -> datetime: ...


class CertificationBroker(PaperBroker, Protocol):
    """What the run needs from the paper adapter beyond the broker contract."""

    rejections: dict[str, str]

    def market_clock(self) -> MarketClockView: ...

    def asset_metadata(self, instrument_id: str) -> AssetMetadata: ...

    def rate_limit_observation(self) -> dict[str, str]: ...


#: Builds an adapter for the run: (instrument specs or None = configured, budget or None).
AdapterFactory = Callable[
    [Mapping[str, AnySpec] | None, CertificationBudget | None], CertificationBroker
]
EnvironmentFactory = Callable[[CertificationBroker, Mapping[str, AnySpec]], PaperEnvironment]


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True, slots=True)
class CertificationPlan:
    """What the run will do; printed before anything is sent (``--yes`` required)."""

    symbol: str = "AAPL"
    passive_notional_usd: Decimal = Decimal("4.00")
    #: The passive limit price as a fraction of the bid (it must never fill).
    passive_limit_fraction: Decimal = Decimal("0.70")
    market_notional_usd: Decimal = Decimal("1.50")
    max_orders: int = CERTIFICATION_MAX_ORDERS
    max_order_notional_usd: Decimal = CERTIFICATION_MAX_ORDER_NOTIONAL_USD
    grant_minutes: int = 20
    min_minutes_before_close: int = 15
    visibility_lookups: int = 20
    visibility_poll_seconds: Decimal = Decimal("0.2")
    wait_seconds: int = 30
    poll_seconds: Decimal = Decimal("0.5")
    quote_attempts: int = 5

    def __post_init__(self) -> None:
        cap = self.max_order_notional_usd
        problems = []
        if not _SYMBOL.fullmatch(self.symbol):
            problems.append(f"symbol {self.symbol!r} is not a US equity ticker")
        if not ZERO < cap <= CERTIFICATION_MAX_ORDER_NOTIONAL_USD:
            problems.append(
                f"the per-order cap must be in (0, {CERTIFICATION_MAX_ORDER_NOTIONAL_USD}]"
            )
        if not ZERO < self.passive_notional_usd <= cap:
            problems.append("the passive order notional must be positive and within the cap")
        if not Decimal("1") <= self.market_notional_usd <= Decimal("2"):
            problems.append("the market round trip is USD 1-2")
        if self.market_notional_usd * (1 + CERTIFICATION_MARKET_PRICE_BUFFER) > cap:
            problems.append("the market order's bound would exceed the cap")
        if not ZERO < self.passive_limit_fraction <= 1 - CERTIFICATION_MIN_PASSIVE_DISCOUNT:
            problems.append(
                f"the passive limit must rest at least {CERTIFICATION_MIN_PASSIVE_DISCOUNT} below "
                "the bid"
            )
        if not PLANNED_SUBMISSIONS <= self.max_orders <= CERTIFICATION_MAX_ORDERS:
            problems.append(f"max orders must be {PLANNED_SUBMISSIONS}..{CERTIFICATION_MAX_ORDERS}")
        if not 1 <= self.grant_minutes <= 30:
            problems.append("the grant lasts 1-30 minutes")
        if min(self.visibility_lookups, self.wait_seconds, self.quote_attempts) < 1:
            problems.append("lookups, waits and quote attempts must be positive")
        if self.min_minutes_before_close < 1:
            problems.append("keep a margin before the close")
        if problems:
            raise CertificationRefused("invalid certification plan: " + "; ".join(problems))

    def as_record(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "passive_notional_usd": str(self.passive_notional_usd),
            "passive_limit_fraction": str(self.passive_limit_fraction),
            "market_notional_usd": str(self.market_notional_usd),
            "max_orders": str(self.max_orders),
            "max_order_notional_usd": str(self.max_order_notional_usd),
            "grant_minutes": str(self.grant_minutes),
            "min_minutes_before_close": str(self.min_minutes_before_close),
        }

    def lines(self, *, account: str, profile_version: str) -> list[str]:
        """The plan as printed before ``--yes``."""
        pct = canon_decimal(self.passive_limit_fraction * 100)
        return [
            f"Paper certification plan - Alpaca PAPER account {account}, symbol {self.symbol}",
            f"  base capability profile : {profile_version}",
            f"  hard limits             : at most {self.max_orders} broker submissions; at most "
            f"USD {self.max_order_notional_usd} notional per order; paper only",
            "  preflight (no orders)   : broker clock open and >= "
            f"{self.min_minutes_before_close} min before the close; asset active, tradable and "
            "fractionable; account flat in the symbol; startup reconciliation PASSED; fresh quote",
            f"  1. LIMIT DAY buy, fractional, ~USD {self.passive_notional_usd} at {pct}% of the "
            "bid (rests far below the market); count lookups until visible by client order ID",
            "  2. the same client order ID again from a fresh adapter: Alpaca must refuse it",
            "  3. cancel order 1 and time the cancellation",
            f"  4. MARKET DAY buy, fractional, ~USD {self.market_notional_usd}; wait for the fill",
            "  5. MARKET DAY sell of the bought quantity; wait for the fill; reconcile (must pass)",
            "  report: runs/paper/certification/<UTC timestamp>.json (no secrets)",
        ]


# --------------------------------------------------------------------------- report


class ClockRecord(ConfigModel):
    as_of: UtcDatetime
    local_time: UtcDatetime
    is_open: bool
    next_open: UtcDatetime
    next_close: UtcDatetime


class AssetRecord(ConfigModel):
    instrument_id: str
    symbol: str
    broker_asset_id: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    fractionable: bool
    marginable: bool
    shortable: bool


class FillRecord(ConfigModel):
    execution_id: str
    quantity: Dec
    price: Dec
    fee: Dec
    fee_status: str
    exec_time: UtcDatetime


class OrderRecord(ConfigModel):
    step: str
    side: str
    order_type: str
    time_in_force: str = "DAY"
    quantity: Dec
    limit_price: Dec | None = None
    notional_bound_usd: Dec
    market_open: bool
    intent_id: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    risk_decision: str
    state_after_submit: str | None = None
    #: Alpaca's status at the first lookup that found the order (visibility probe).
    broker_status_after_submit: str | None = None
    final_state: str | None = None
    broker_status: str | None = None
    refused_by: Literal["risk", "adapter", "broker"] | None = None
    refusal: str | None = None
    fills: tuple[FillRecord, ...] = ()


class VisibilityRecord(ConfigModel):
    step: str
    client_order_id: str
    visible: bool
    lookups: int
    seconds: Dec


class DuplicateRecord(ConfigModel):
    outcome: str
    detail: str
    broker_orders_with_client_id: int
    filled_quantity: Dec


class CancelRecord(ConfigModel):
    request_accepted: bool
    terminal: bool
    seconds: Dec | None
    lookups: int
    statuses_seen: tuple[str, ...]
    final_status: str | None
    filled_quantity: Dec


class CashRecord(ConfigModel):
    """Broker cash around the round trip; ``observed_*_fees_usd`` = what the broker took beyond
    the traded notional (pass-through fees, rounding)."""

    broker_cash_before: Dec | None = None
    broker_cash_after_buy: Dec | None = None
    broker_cash_after_sell: Dec | None = None
    buy_notional_usd: Dec | None = None
    sell_notional_usd: Dec | None = None
    observed_buy_fees_usd: Dec | None = None
    observed_sell_fees_usd: Dec | None = None


class ReconciliationRecord(ConfigModel):
    label: str
    status: str
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class StepRecord(ConfigModel):
    name: str
    status: StepStatus
    detail: str
    started_at: UtcDatetime
    finished_at: UtcDatetime


class CertificationReport(ConfigModel):
    """Evidence of one certification run (``runs/paper/certification/<UTC timestamp>.json``)."""

    report_version: Literal[1] = 1
    kind: Literal["alpaca-paper-certification"] = "alpaca-paper-certification"
    run_id: str
    status: Literal["COMPLETED", "INCOMPLETE", "REFUSED"]
    account_id: str
    broker: str
    environment: Literal["paper"]
    capability_profile_version: str
    fee_schedule_id: str
    symbol: str
    instrument_id: str | None
    started_at: UtcDatetime
    finished_at: UtcDatetime
    max_orders: int
    max_order_notional_usd: Dec
    broker_submissions: int
    plan: dict[str, str]
    market_clock: ClockRecord | None = None
    asset: AssetRecord | None = None
    steps: tuple[StepRecord, ...] = ()
    orders: tuple[OrderRecord, ...] = ()
    visibility: tuple[VisibilityRecord, ...] = ()
    duplicate: DuplicateRecord | None = None
    cancel: CancelRecord | None = None
    cash: CashRecord | None = None
    reconciliations: tuple[ReconciliationRecord, ...] = ()
    #: Broker and ledger both hold none of the symbol after the round trip (``None``: not run).
    flat_after_round_trip: bool | None = None
    rate_limit_headers: dict[str, str] = Field(default_factory=dict)
    pending_liquidation: dict[str, Dec] = Field(default_factory=dict)
    refusal: str | None = None
    report_hash: str = ""

    def body_hash(self) -> str:
        return content_hash(self.model_dump(mode="python", exclude={"report_hash"}))

    def verified(self) -> CertificationReport:
        """``self`` when ``report_hash`` matches the content (else raises)."""
        if self.report_hash != self.body_hash():
            raise CertificationRefused("the certification report was altered (hash mismatch)")
        return self

    def step(self, name: str) -> StepRecord | None:
        return next((s for s in self.steps if s.name == name), None)

    def passed(self, name: str) -> bool:
        record = self.step(name)
        return record is not None and record.status == "PASSED"

    def lines(self) -> list[str]:
        out = [f"certification {self.run_id}: {self.status}"]
        for s in self.steps:
            out.append(f"  {s.name:<24} {s.status:<7} {s.detail}")
        for v in self.visibility:
            out.append(
                f"  visible {v.step:<16} {'yes' if v.visible else 'NO '} after {v.lookups} "
                f"lookup(s), {v.seconds} s"
            )
        for r in self.reconciliations:
            out.append(f"  reconciliation {r.label:<16} {r.status} {'; '.join(r.problems)}")
        out.append(f"  broker submissions      : {self.broker_submissions}/{self.max_orders}")
        if self.rate_limit_headers:
            out.append(f"  rate-limit headers      : {self.rate_limit_headers}")
        for key, qty in sorted(self.pending_liquidation.items()):
            out.append(f"  PENDING LIQUIDATION {key} qty={qty}")
        if self.refusal:
            out.append(f"  refused: {self.refusal}")
        return out


@dataclass
class ReportBuilder:
    run_id: str
    account_id: str
    broker: str
    profile_version: str
    fee_schedule_id: str
    plan: CertificationPlan
    started_at: datetime
    instrument_id: str | None = None
    market_clock: ClockRecord | None = None
    asset: AssetRecord | None = None
    steps: list[StepRecord] = field(default_factory=list)
    orders: list[OrderRecord] = field(default_factory=list)
    visibility: list[VisibilityRecord] = field(default_factory=list)
    duplicate: DuplicateRecord | None = None
    cancel: CancelRecord | None = None
    cash: CashRecord | None = None
    reconciliations: list[ReconciliationRecord] = field(default_factory=list)
    flat_after_round_trip: bool | None = None
    rate_limit_headers: dict[str, str] = field(default_factory=dict)
    pending_liquidation: dict[str, Decimal] = field(default_factory=dict)
    refusal: str | None = None
    broker_submissions: int = 0

    def add_step(
        self, name: str, status: StepStatus, detail: str, started_at: datetime, now: datetime
    ) -> None:
        self.steps.append(
            StepRecord(
                name=name, status=status, detail=detail, started_at=started_at, finished_at=now
            )
        )

    def build(self, finished_at: datetime) -> CertificationReport:
        if self.refusal is not None and self.broker_submissions == 0:
            status: Literal["COMPLETED", "INCOMPLETE", "REFUSED"] = "REFUSED"
        elif (
            self.steps
            and all(s.status == "PASSED" for s in self.steps)
            and not (self.pending_liquidation)
        ):
            status = "COMPLETED"
        else:
            status = "INCOMPLETE"
        body = CertificationReport(
            run_id=self.run_id,
            status=status,
            account_id=self.account_id,
            broker=self.broker,
            environment="paper",
            capability_profile_version=self.profile_version,
            fee_schedule_id=self.fee_schedule_id,
            symbol=self.plan.symbol,
            instrument_id=self.instrument_id,
            started_at=self.started_at,
            finished_at=finished_at,
            max_orders=self.plan.max_orders,
            max_order_notional_usd=self.plan.max_order_notional_usd,
            broker_submissions=self.broker_submissions,
            plan=self.plan.as_record(),
            market_clock=self.market_clock,
            asset=self.asset,
            steps=tuple(self.steps),
            orders=tuple(self.orders),
            visibility=tuple(self.visibility),
            duplicate=self.duplicate,
            cancel=self.cancel,
            cash=self.cash,
            reconciliations=tuple(self.reconciliations),
            flat_after_round_trip=self.flat_after_round_trip,
            rate_limit_headers=dict(self.rate_limit_headers),
            pending_liquidation=dict(self.pending_liquidation),
            refusal=self.refusal,
        )
        return body.model_copy(update={"report_hash": body.body_hash()})


def report_path(report_dir: Path, run_id: str) -> Path:
    """``<dir>/<UTC timestamp>.json`` (the run id without its prefix)."""
    return report_dir / f"{run_id.removeprefix('certification-')}.json"


def write_report(report: CertificationReport, report_dir: Path) -> Path:
    path = report_path(report_dir, report.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise CertificationRefused(f"{path} already exists; reports are never overwritten")
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def load_report(path: Path) -> CertificationReport:
    report = CertificationReport.model_validate_json(path.read_text(encoding="utf-8"))
    return report.verified()


# --------------------------------------------------------------------------- helpers


def _seconds(delta: timedelta) -> Decimal:
    return (
        Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 10**6
    ).quantize(_MS)


def clock_record(view: MarketClockView, local_time: datetime) -> ClockRecord:
    return ClockRecord(
        as_of=view.as_of,
        local_time=local_time,
        is_open=view.is_open,
        next_open=view.next_open,
        next_close=view.next_close,
    )


def asset_record(asset: AssetMetadata) -> AssetRecord:
    return AssetRecord(
        instrument_id=asset.instrument_id,
        symbol=asset.symbol,
        broker_asset_id=asset.broker_asset_id,
        asset_class=asset.asset_class,
        exchange=asset.exchange,
        status=asset.status,
        tradable=asset.tradable,
        fractionable=asset.fractionable,
        marginable=asset.marginable,
        shortable=asset.shortable,
    )


def verified_spec(
    spec: AnySpec, asset: AssetMetadata, *, fetched_at: datetime, run_id: str
) -> EquitySpec:
    """The configured spec with the broker's asset record applied, for this run only.

    Refuses unless the asset is an active, tradable, fractionable US equity matching the spec.
    """
    if not isinstance(spec, EquitySpec):
        raise CertificationRefused(f"{spec.instrument_id} is not an ordinary equity")
    problems = asset.certification_problems()
    if asset.symbol != spec.identity.symbol or not asset.broker_asset_id:
        problems.append("the asset record does not match the configured instrument")
    if not spec.fractionable:
        problems.append(f"{spec.instrument_id} is configured as non-fractionable")
    if problems:
        raise CertificationRefused("asset not certifiable: " + "; ".join(problems))
    identity = spec.identity.model_copy(update={"broker_asset_id": asset.broker_asset_id})
    data = spec.model_dump(mode="python")
    data.update(
        identity=identity.model_dump(mode="python"),
        tradable=True,
        fractionable=True,
        evidence=EvidenceLevel.PAPER_DEMONSTRATED,
        as_of=fetched_at,
        source=f"Alpaca paper /v2/assets/{asset.symbol} read by {run_id} (this run only)",
    )
    return EquitySpec.model_validate(data)


def check_market_clock(view: MarketClockView, *, min_minutes_before_close: int) -> None:
    """Refuse unless the broker clock says the regular session is open with time to spare."""
    if not view.is_open:
        raise CertificationRefused(
            f"the market is closed (Alpaca clock {iso_utc(view.as_of)}; next open "
            f"{iso_utc(view.next_open)}): market-order steps need regular trading hours. "
            "Nothing was sent."
        )
    remaining = view.next_close - view.as_of
    if remaining < timedelta(minutes=min_minutes_before_close):
        raise CertificationRefused(
            f"only {int(remaining.total_seconds() // 60)} minute(s) to the close; the run needs "
            f"{min_minutes_before_close}. Nothing was sent."
        )


@dataclass
class _Poll:
    order: BrokerOrder | None
    lookups: int
    seconds: Decimal
    done: bool
    statuses: list[str]


# --------------------------------------------------------------------------- runner


class CertificationRunner:
    """Runs the plan's steps on an assembled paper environment (see the module docstring)."""

    def __init__(
        self,
        env: PaperEnvironment,
        *,
        broker: CertificationBroker,
        probe: BrokerAdapter,
        budget: CertificationBudget,
        plan: CertificationPlan,
        market_data: PaperMarketData,
        report: ReportBuilder,
        instrument_id: str,
        use_stream: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if env.broker is not broker:
            raise CertificationRefused("the environment must drive the certification adapter")
        if env.config.runtime.mode is not Mode.PAPER:
            raise CertificationRefused("certification runs only in paper mode")
        if broker.capabilities().environment != "paper":
            raise CertificationRefused("the adapter is not a paper adapter")
        if market_data.feed != env.market.feed:
            raise CertificationRefused("the quote feed differs from the market view feed")
        self.env = env
        self.clock: SleepingClock = env.clock
        self.broker = broker
        self.probe = probe
        self.budget = budget
        self.grant: CertificationGrant = budget.grant
        self.plan = plan
        self.data = market_data
        self.report = report
        self.instrument_id = instrument_id
        self.spec = env.account.specs[instrument_id]
        self.loop = DecisionLoop(env)
        self.use_stream = use_stream
        self._log = log
        self._sequence = 0
        self._started = False

    def log(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        try:
            self._start()
            self._passive_limit_steps()
            self._market_round_trip()
        except (CertificationRefused, LeadershipUnavailable) as exc:
            self.report.refusal = str(exc)
            self.log(f"refused: {exc}")
        except Exception as exc:  # recorded; the run still cleans up and reconciles
            self._step("error", "FAILED", f"{type(exc).__name__}: {exc}", self.clock.now())
            self.log(f"certification stopped by {type(exc).__name__}: {exc}")
        finally:
            self._finish()

    def _start(self) -> None:
        ensure_leadership_free(self.env)
        if self.use_stream:
            self.broker.start_stream()
        self.loop.start()
        self._started = True
        startup = self.env.extras.get("startup_reconciliation", {})
        status = str(startup.get(self.loop.account_id)) if isinstance(startup, dict) else "?"
        self.report.reconciliations.append(ReconciliationRecord(label="startup", status=status))
        if status != "PASSED":
            raise CertificationRefused(
                f"startup reconciliation {status}: resolve it (`trader reconcile`) before "
                "certifying. Nothing was sent."
            )
        view = self.broker.market_clock()
        check_market_clock(view, min_minutes_before_close=self.plan.min_minutes_before_close)
        held = [p for p in self.broker.get_positions() if p.instrument_id == self.instrument_id]
        if held:
            raise CertificationRefused(
                f"the paper account already holds {self.plan.symbol}; the round trip needs it "
                "flat. Nothing was sent."
            )
        with self.env.db.tx() as conn:
            position = self.env.ledger.position(conn, self.loop.account_id, self.instrument_id)
        if position is not None and position.quantity != 0:
            raise CertificationRefused(f"the ledger holds {self.plan.symbol}. Nothing was sent.")

    # ------------------------------------------------------------------ steps 1-3
    def _passive_limit_steps(self) -> None:
        started = self.clock.now()
        quote = self._quote()
        if quote is None:
            raise CertificationRefused("no fresh quote for the symbol. Nothing was sent.")
        limit = round_down_to_step(
            quote.bid * self.plan.passive_limit_fraction, self.spec.price_tick
        )
        if limit <= 0:
            raise CertificationRefused("the bid is too low for a passive limit. Nothing was sent.")
        quantity = round_down_to_step(
            self.plan.passive_notional_usd / limit, self.spec.quantity_step
        )
        order = self._place("fractional_limit_day", OrderType.LIMIT, quantity, limit, quote)
        if order.intent_id is None or order.client_order_id is None:
            self._step("fractional_limit_day", "FAILED", self._why(order), started)
            self._skip(("client_order_id_unique", "cancel"), "the limit order was not accepted")
            return
        accepted = order.state_after_submit == OrderState.ACKNOWLEDGED.value
        self._step(
            "fractional_limit_day",
            "PASSED" if accepted else "FAILED",
            f"{order.quantity} @ {order.limit_price} {order.state_after_submit}"
            + ("" if accepted else f": {self._why(order)}"),
            started,
        )
        if not accepted:
            self._skip(("client_order_id_unique", "cancel"), "the limit order is not resting")
            return
        self._duplicate_step(order.intent_id, order.client_order_id)
        self._cancel_step(order.intent_id, order.client_order_id)

    def _duplicate_step(self, intent_id: str, cid: str) -> None:
        started = self.clock.now()
        probe = self.env.gateway.probe_duplicate_submission(
            intent_id, probe=self.probe, grant=self.grant
        )
        same = [o for o in self.broker.list_orders(OrderScope.ALL) if o.client_order_id == cid]
        count = len({o.broker_order_id for o in same})
        filled = sum((o.filled_quantity for o in same), start=ZERO)
        self.report.duplicate = DuplicateRecord(
            outcome=probe.outcome,
            detail=probe.detail,
            broker_orders_with_client_id=count,
            filled_quantity=filled,
        )
        ok = probe.outcome == "REJECTED_DUPLICATE" and count == 1 and filled == 0
        self._step(
            "client_order_id_unique",
            "PASSED" if ok else "FAILED",
            f"{probe.outcome}; {count} broker order(s) with the ID, filled {filled}",
            started,
        )

    def _cancel_step(self, intent_id: str, cid: str) -> None:
        started = self.clock.now()
        self.loop.pump()
        accepted = self.env.gateway.request_cancel(intent_id)
        poll = self._poll(
            cid,
            started_at=started,
            until=lambda o: o.status.is_terminal,
            max_lookups=None,
            poll_seconds=self.plan.poll_seconds,
        )
        self.loop.pump()
        self._reconcile("after-cancel")
        final = poll.order
        state = self._state(intent_id)
        filled = final.filled_quantity if final is not None else ZERO
        self.report.cancel = CancelRecord(
            request_accepted=accepted,
            terminal=poll.done,
            seconds=poll.seconds if poll.done else None,
            lookups=poll.lookups,
            statuses_seen=tuple(poll.statuses),
            final_status=final.status.value if final is not None else None,
            filled_quantity=filled,
        )
        self._update_order(intent_id, final_state=state, order=final)
        ok = (
            accepted
            and poll.done
            and final is not None
            and final.status is BrokerOrderStatus.CANCELED
            and filled == 0
            and state == OrderState.CANCELED.value
        )
        self._step(
            "cancel",
            "PASSED" if ok else "FAILED",
            f"request {'accepted' if accepted else 'REFUSED'}; "
            f"{final.status.value if final else 'not visible'} after {poll.seconds} s; "
            f"local state {state}",
            started,
        )

    # ------------------------------------------------------------------ step 4
    def _market_round_trip(self) -> None:
        name = "fractional_market_day"
        started = self.clock.now()
        quote = self._quote()
        if quote is None:
            self._step(name, "FAILED", "no fresh quote", started)
            return
        quantity = round_down_to_step(
            self.plan.market_notional_usd / quote.ask, self.spec.quantity_step
        )
        cash = CashRecord(broker_cash_before=self.broker.get_account().cash)
        self.report.cash = cash
        buy = self._place(name, OrderType.MARKET, quantity, None, quote)
        if buy.intent_id is None or buy.client_order_id is None:
            self._step(name, "FAILED", f"buy: {self._why(buy)}", started)
            return
        buy_fills = self._await_fill(buy.intent_id, buy.client_order_id)
        if buy_fills is None:
            self._step(name, "FAILED", "the market buy did not fill in time", started)
            return
        buy_notional = sum((f.quantity * f.price for f in buy_fills), start=ZERO)
        cash = cash.model_copy(
            update={
                "broker_cash_after_buy": self.broker.get_account().cash,
                "buy_notional_usd": buy_notional,
            }
        )
        self.report.cash = cash
        before_sell = self.clock.now()
        created = self.env.exits.act(
            [
                ExitTrigger(
                    account_id=self.loop.account_id,
                    instrument_id=self.instrument_id,
                    purpose=OrderPurpose.EXIT_RISK_REDUCTION,
                    reason=f"paper certification {self.grant.run_id} round trip",
                    entry_intent_id=buy.intent_id,
                )
            ],
            set(),
        )
        sell = self._record_exit(created, before_sell)
        if sell is None or sell.intent_id is None or sell.client_order_id is None:
            self._step(name, "FAILED", "the sell was not sent", started)
            return
        sell_fills = self._await_fill(sell.intent_id, sell.client_order_id)
        if sell_fills is not None:
            sell_notional = sum((f.quantity * f.price for f in sell_fills), start=ZERO)
            after_sell = self.broker.get_account().cash
            before = cash.broker_cash_before
            after_buy = cash.broker_cash_after_buy
            cash = cash.model_copy(
                update={
                    "broker_cash_after_sell": after_sell,
                    "sell_notional_usd": sell_notional,
                    "observed_buy_fees_usd": (
                        None
                        if before is None or after_buy is None
                        else before - after_buy - buy_notional
                    ),
                    "observed_sell_fees_usd": (
                        None if after_buy is None else sell_notional - (after_sell - after_buy)
                    ),
                }
            )
            self.report.cash = cash
        result = self._reconcile("after-round-trip")
        broker_qty = sum(
            (
                p.quantity
                for p in self.broker.get_positions()
                if p.instrument_id == self.instrument_id
            ),
            start=ZERO,
        )
        with self.env.db.tx() as conn:
            position = self.env.ledger.position(conn, self.loop.account_id, self.instrument_id)
        ledger_qty = position.quantity if position is not None else ZERO
        flat = broker_qty == 0 and ledger_qty == 0
        self.report.flat_after_round_trip = flat
        ok = sell_fills is not None and flat and result == "PASSED"
        self._step(
            name,
            "PASSED" if ok else "FAILED",
            f"buy {buy.quantity} filled; sell {'filled' if sell_fills else 'NOT filled'}; "
            f"flat {flat} (broker {broker_qty}, ledger {ledger_qty}); reconciliation {result}",
            started,
        )

    # ------------------------------------------------------------------ orders
    def _place(
        self,
        step: str,
        order_type: OrderType,
        quantity: Decimal,
        limit: Decimal | None,
        quote: Quote,
    ) -> OrderRecord:
        """Risk-approve, persist and submit one certification buy; measure its visibility."""
        open_now = self.broker.market_clock().is_open
        decision = self.env.allocator.reserve_certification_order(
            account_id=self.loop.account_id,
            grant=self.grant,
            step=step,
            instrument_id=self.instrument_id,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit,
            market=MarketInputs(quote=quote, marks=self.env.market.marks(), halted=False),
            market_open=open_now,
            orders_used=self.budget.used,
        )
        record = OrderRecord(
            step=step,
            side="BUY",
            order_type=order_type.value,
            quantity=quantity,
            limit_price=limit,
            notional_bound_usd=decision.notional_usd,
            market_open=open_now,
            risk_decision="APPROVED"
            if decision.approved
            else "; ".join([*(r.value for r in decision.reasons), *decision.details]),
        )
        if not decision.approved or decision.intent_id is None:
            record = record.model_copy(
                update={"refused_by": "risk", "refusal": record.risk_decision}
            )
            self.report.orders.append(record)
            return record
        return self._submit(record, decision.intent_id)

    def _submit(self, record: OrderRecord, intent_id: str) -> OrderRecord:
        cid = self._client_order_id(intent_id)
        started = self.clock.now()
        self.env.gateway.submit_pending()
        return self._after_submit(record, intent_id, cid, started)

    def _record_exit(self, created: list[str], started: datetime) -> OrderRecord | None:
        if not created:
            return None
        intent_id = created[0]
        with self.env.db.tx() as conn:
            row = conn.execute(
                select(order_intents).where(order_intents.c.intent_id == intent_id)
            ).one()
        record = OrderRecord(
            step="fractional_market_day",
            side="SELL",
            order_type=str(row.order_type),
            quantity=Decimal(row.quantity),
            notional_bound_usd=ZERO,
            market_open=self.broker.market_clock().is_open,
            risk_decision="APPROVED (reducing exit)",
        )
        return self._after_submit(record, intent_id, str(row.client_order_id), started)

    def _after_submit(
        self, record: OrderRecord, intent_id: str, cid: str, started: datetime
    ) -> OrderRecord:
        state = self._state(intent_id)
        record = record.model_copy(
            update={"intent_id": intent_id, "client_order_id": cid, "state_after_submit": state}
        )
        if state in (OrderState.REJECTED.value, OrderState.NOT_SENT.value):
            reason = self.broker.rejections.get(cid, "refused (no reason recorded)")
            by: Literal["adapter", "broker"] = (
                "broker" if reason.lower().startswith("alpaca") else "adapter"
            )
            record = record.model_copy(update={"refused_by": by, "refusal": reason})
        else:
            poll = self._poll(
                cid,
                started_at=started,
                until=lambda _o: True,
                max_lookups=self.plan.visibility_lookups,
                poll_seconds=self.plan.visibility_poll_seconds,
            )
            self.report.visibility.append(
                VisibilityRecord(
                    step=f"{record.step}:{record.side.lower()}",
                    client_order_id=cid,
                    visible=poll.done,
                    lookups=poll.lookups,
                    seconds=poll.seconds,
                )
            )
            if poll.order is not None:
                record = record.model_copy(
                    update={
                        "broker_order_id": poll.order.broker_order_id,
                        "broker_status_after_submit": poll.order.status.value,
                        "broker_status": poll.order.status.value,
                    }
                )
        self.report.orders.append(record)
        return record

    def _await_fill(self, intent_id: str, cid: str) -> list[FillRecord] | None:
        """Wait until the broker reports the order filled and every fill is in the ledger."""
        started = self.clock.now()
        deadline = started + timedelta(seconds=self.plan.wait_seconds)
        order: BrokerOrder | None = None
        while True:
            try:
                order = self.broker.get_order_by_client_id(cid) or order
            except (BrokerError, BrokerTimeout) as exc:
                self.log(f"lookup of {cid} failed: {type(exc).__name__}")
            self.loop.pump()
            state = self._state(intent_id)
            posted = self._fills(intent_id)
            done_qty = sum((f.quantity for f in posted), start=ZERO)
            if (
                order is not None
                and order.status is BrokerOrderStatus.FILLED
                and state == OrderState.FILLED.value
                and done_qty == order.filled_quantity
            ):
                self._update_order(intent_id, final_state=state, order=order, fills=posted)
                return posted
            if self.clock.now() >= deadline:
                if order is not None and order.status is BrokerOrderStatus.FILLED:
                    self._reconcile("fill-catch-up")  # REST executions, then the order state
                    state, posted = self._state(intent_id), self._fills(intent_id)
                    if state == OrderState.FILLED.value:
                        self._update_order(intent_id, final_state=state, order=order, fills=posted)
                        return posted
                self._update_order(intent_id, final_state=state, order=order, fills=posted)
                return None
            self.clock.sleep(float(self.plan.poll_seconds))

    def _poll(
        self,
        cid: str,
        *,
        started_at: datetime,
        until: Callable[[BrokerOrder], bool],
        max_lookups: int | None,
        poll_seconds: Decimal,
    ) -> _Poll:
        """Look the order up by client order ID until ``until`` holds (or the budget is used)."""
        deadline = started_at + timedelta(seconds=self.plan.wait_seconds)
        lookups = 0
        statuses: list[str] = []
        last: BrokerOrder | None = None
        while True:
            lookups += 1
            try:
                order = self.broker.get_order_by_client_id(cid)
            except (BrokerError, BrokerTimeout) as exc:
                self.log(f"lookup of {cid} failed: {type(exc).__name__}")
                order = None
            now = self.clock.now()
            if order is not None:
                last = order
                if not statuses or statuses[-1] != order.status.value:
                    statuses.append(order.status.value)
                if until(order):
                    return _Poll(order, lookups, _seconds(now - started_at), True, statuses)
            exhausted = max_lookups is not None and lookups >= max_lookups
            if exhausted or now >= deadline:
                return _Poll(last, lookups, _seconds(now - started_at), False, statuses)
            self.clock.sleep(float(poll_seconds))

    # ------------------------------------------------------------------ data
    def _quote(self) -> Quote | None:
        """A fresh quote for the symbol (a few attempts), recorded in the market view."""
        max_age = timedelta(seconds=self.env.config.policy.policy.quality.max_quote_age_seconds)
        for attempt in range(self.plan.quote_attempts):
            if attempt:
                self.clock.sleep(1)
            try:
                raw = self.data.latest_quotes([self.plan.symbol])
            except (TransportError, DataQualityError) as exc:
                self.log(f"latest quote unavailable: {exc}")
                continue
            latest = raw.get(self.plan.symbol)
            if latest is None:
                continue
            self._sequence += 1
            now = self.clock.now()
            quote, _skew = quote_from_source(
                latest,
                instrument_id=self.instrument_id,
                feed=self.data.feed,
                received_at=now,
                sequence=self._sequence,
            )
            if quote is None or now - quote.event_time > max_age:
                continue
            self.env.market.on_event(quote)
            return quote
        return None

    def _client_order_id(self, intent_id: str) -> str:
        with self.env.db.tx() as conn:
            return str(
                conn.execute(
                    select(order_intents.c.client_order_id).where(
                        order_intents.c.intent_id == intent_id
                    )
                ).scalar_one()
            )

    def _state(self, intent_id: str) -> str:
        with self.env.db.tx() as conn:
            return str(
                conn.execute(
                    select(order_intents.c.state).where(order_intents.c.intent_id == intent_id)
                ).scalar_one()
            )

    def _fills(self, intent_id: str) -> list[FillRecord]:
        with self.env.db.tx() as conn:
            rows = conn.execute(
                select(fills).where(fills.c.intent_id == intent_id).order_by(fills.c.exec_time)
            ).all()
        return [
            FillRecord(
                execution_id=str(r.execution_id),
                quantity=Decimal(r.quantity),
                price=Decimal(r.price),
                fee=Decimal(r.fee),
                fee_status=str(r.fee_status),
                exec_time=r.exec_time,
            )
            for r in rows
        ]

    def _update_order(
        self,
        intent_id: str,
        *,
        final_state: str,
        order: BrokerOrder | None,
        fills: list[FillRecord] | None = None,
    ) -> None:
        for i, record in enumerate(self.report.orders):
            if record.intent_id == intent_id:
                update: dict[str, object] = {"final_state": final_state}
                if order is not None:
                    update["broker_status"] = order.status.value
                    update["broker_order_id"] = order.broker_order_id
                if fills is not None:
                    update["fills"] = tuple(fills)
                self.report.orders[i] = record.model_copy(update=update)

    def _reconcile(self, label: str) -> str:
        result = self.loop.reconcile(cleared_by=f"certification-{label}")
        self.report.reconciliations.append(
            ReconciliationRecord(
                label=label,
                status=result.status,
                problems=tuple(result.problems),
                notes=tuple(result.notes),
            )
        )
        return result.status

    # ------------------------------------------------------------------ bookkeeping
    def _step(self, name: str, status: StepStatus, detail: str, started: datetime) -> None:
        self.report.add_step(name, status, detail, started, self.clock.now())
        self.log(f"{name}: {status} {detail}")

    def _skip(self, names: tuple[str, ...], why: str) -> None:
        now = self.clock.now()
        for name in names:
            self.report.add_step(name, "SKIPPED", why, now, now)

    @staticmethod
    def _why(order: OrderRecord) -> str:
        return f"refused by {order.refused_by or '?'}: {order.refusal or order.risk_decision}"

    def _finish(self) -> None:
        try:
            if self._started:
                cancelled = self.loop.cancel_working_entries()
                if cancelled:
                    self.log(f"cleanup: cancel requested for {len(cancelled)} working order(s)")
                    self.clock.sleep(float(self.plan.poll_seconds))
                self.loop.pump()
                self._reconcile("final")
                self.report.pending_liquidation = self.env.exits.pending_liquidation()
        except Exception as exc:  # best effort: the next start reconciles again
            self.report.reconciliations.append(
                ReconciliationRecord(label="final", status=f"ERROR {type(exc).__name__}: {exc}")
            )
        finally:
            self.report.broker_submissions = self.budget.used
            self.report.rate_limit_headers = self.broker.rate_limit_observation()
            if self.use_stream:
                self.broker.stop()


# --------------------------------------------------------------------------- orchestration


def certify_paper_account(
    config: ResolvedConfig,
    *,
    clock: SleepingClock,
    market_data: PaperMarketData,
    make_adapter: AdapterFactory,
    make_environment: EnvironmentFactory,
    plan: CertificationPlan,
    account_alias: str | None = None,
    use_stream: bool = True,
    log: Callable[[str], None] | None = None,
) -> CertificationReport:
    """Preflight, grant, environment and run; always returns a report (status REFUSED when
    nothing was sent). ``make_environment`` is :func:`~trader.runtime.paper.
    build_paper_environment` in production (PostgreSQL, flat-account check, bootstrap)."""
    acct = paper_account(config, account_alias)
    profile = load_capabilities(config.resolve(acct.capability_profile))
    configured = load_instruments(config.resolve(acct.instruments))
    started = clock.now()
    run_id = certification_run_id(started)
    report = ReportBuilder(
        run_id=run_id,
        account_id=acct.account_id,
        broker=acct.broker,
        profile_version=profile.profile_version,
        fee_schedule_id=profile.fee_schedule_id,
        plan=plan,
        started_at=started,
    )
    env: PaperEnvironment | None = None
    try:
        matches = [iid for iid, s in configured.items() if s.identity.symbol == plan.symbol]
        if len(matches) != 1:
            raise CertificationRefused(
                f"{plan.symbol} is not a configured instrument of {acct.alias}"
            )
        instrument_id = report.instrument_id = matches[0]
        reader = make_adapter(None, None)
        view = reader.market_clock()
        report.market_clock = clock_record(view, clock.now())
        check_market_clock(view, min_minutes_before_close=plan.min_minutes_before_close)
        asset = reader.asset_metadata(instrument_id)
        report.asset = asset_record(asset)
        spec = verified_spec(
            configured[instrument_id], asset, fetched_at=clock.now(), run_id=run_id
        )
        specs: dict[str, AnySpec] = {**configured, instrument_id: spec}
        now = clock.now()
        grant = CertificationGrant(
            run_id=run_id,
            account_id=acct.account_id,
            mode=Mode.PAPER,
            issued_at=now,
            expires_at=now + timedelta(minutes=plan.grant_minutes),
            max_orders=plan.max_orders,
            max_order_notional_usd=plan.max_order_notional_usd,
        )
        budget = CertificationBudget(grant)
        broker = make_adapter(specs, budget)
        probe = make_adapter(specs, budget)
        env = make_environment(broker, specs)
        runner = CertificationRunner(
            env,
            broker=broker,
            probe=probe,
            budget=budget,
            plan=plan,
            market_data=market_data,
            report=report,
            instrument_id=instrument_id,
            use_stream=use_stream,
            log=log,
        )
        runner.run()
    except (CertificationRefused, PaperStartRefused, BrokerError, BrokerTimeout) as exc:
        report.refusal = str(exc)
    finally:
        if env is not None:
            env.close()
    return report.build(clock.now())


def make_certification_environment(
    config: ResolvedConfig,
    *,
    clock: SleepingClock,
    database: Database,
    account_alias: str | None,
) -> EnvironmentFactory:
    """The production :data:`EnvironmentFactory` (``build_paper_environment``)."""
    from trader.runtime.paper import build_paper_environment

    def make(broker: CertificationBroker, specs: Mapping[str, AnySpec]) -> PaperEnvironment:
        return build_paper_environment(
            config,
            clock=clock,
            adapter=broker,
            database=database,
            account_alias=account_alias,
            holder=CERTIFICATION_HOLDER,
            specs=specs,
        )

    return make


__all__ = [
    "CERTIFICATION_HOLDER",
    "DEFAULT_REPORT_DIR",
    "PLANNED_SUBMISSIONS",
    "REPORT_KIND",
    "CertificationBroker",
    "CertificationPlan",
    "CertificationRefused",
    "CertificationReport",
    "CertificationRunner",
    "ReportBuilder",
    "certify_paper_account",
    "check_market_clock",
    "load_report",
    "make_certification_environment",
    "report_path",
    "verified_spec",
    "write_report",
]
