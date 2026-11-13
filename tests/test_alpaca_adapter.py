"""Alpaca PAPER adapter (milestone M4; architecture.md §4, §7; ADR-0025). No network.

* A broker contract suite runs the same scenarios against :class:`ReplayBroker` and
  :class:`AlpacaPaperAdapter` over an in-memory fake transport (tests/alpaca_fake.py).
* Alpaca-specific rules: paper-only construction, the SDK's paper endpoint, exact decimals, the
  status table, canonical REST execution ids, version ordering, pacing, history scoping,
  credential hygiene and the trade_updates thread.
* The gateway's cancel guard: a cancel with an unknown outcome is recorded and pauses entries.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Protocol

import pytest
from alpaca.common.exceptions import APIError
from sqlalchemy import select

from tests.alpaca_fake import PAPER_KEY, PAPER_SECRET, FakeAlpaca, ts_nanos
from tests.helpers import ENTRY_DECISION, harness
from trader.accounting.fees import load_fee_schedule
from trader.brokers.alpaca import transport as transport_module
from trader.brokers.alpaca.adapter import (
    POLL_INTERVAL_WITHOUT_STREAM,
    AlpacaPaperAdapter,
    PaperOnlyError,
)
from trader.brokers.alpaca.mapping import (
    STATUS_RULES,
    MappingError,
    SymbolMap,
    UnmappedStatus,
    epoch_micros,
    map_order,
    map_position,
    map_status,
    parse_decimal,
    parse_timestamp,
)
from trader.brokers.alpaca.pacing import TokenBucket
from trader.brokers.alpaca.stream import TradeUpdateStream
from trader.brokers.alpaca.transport import (
    DefiniteRejection,
    NotFound,
    SdkTransport,
    TradeHandler,
    translate_api_error,
)
from trader.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerTimeout,
    OrderScope,
    UnauthorizedSubmission,
)
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.brokers.replay.broker import ReplayBroker
from trader.brokers.replay.faults import FaultKind, FaultPlan, FaultSpec
from trader.data.sources.alpaca_history import AlpacaCredentials
from trader.domain.account import LegalAccountType, RuleProfileKind
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.instruments import EquitySpec, InstrumentIdentity
from trader.domain.market import Quote
from trader.domain.orders import (
    ApprovedPlan,
    BrokerOrderEvent,
    BrokerOrderStatus,
    OrderIntent,
    OrderPurpose,
    OrderState,
    OrderType,
    TimeInForce,
)
from trader.domain.types import DomainError, EvidenceLevel, Mode, ProductType, Side, Track
from trader.ops.clock import Clock, ReplayClock
from trader.risk.approval import issue_plan
from trader.storage.db import Database
from trader.storage.schema import alerts, broker_order_events, order_intents

ROOT = Path(__file__).resolve().parents[1]
ALPACA_PACKAGE = ROOT / "src" / "trader" / "brokers" / "alpaca"
T0 = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
ACCOUNT = "acct-contract"
UNKNOWN_ORDER_ID = "00000000-0000-4000-8000-000000000000"
CREDS = AlpacaCredentials(PAPER_KEY, PAPER_SECRET)


def alpaca_caps() -> BrokerCapabilities:
    return load_capabilities(ROOT / "configs" / "capabilities" / "alpaca_documented.yaml")


def aapl_spec(**changes: Any) -> EquitySpec:
    fields: dict[str, Any] = {
        "identity": InstrumentIdentity(
            instrument_id="XNAS:AAPL",
            broker="alpaca",
            broker_asset_id="b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
            symbol="AAPL",
            venue_mic="XNAS",
            currency="USD",
            product_type=ProductType.ORDINARY_EQUITY,
            track=Track.STOCKS,
        ),
        "quantity_step": Decimal("0.000000001"),
        "min_quantity": Decimal("0.000000001"),
        "min_notional": Decimal("1.00"),
        "price_tick": Decimal("0.01"),
        "tradable": True,
        "fractionable": True,
        "evidence": EvidenceLevel.DOCUMENTED,
        "as_of": T0,
        "source": "test fixture",
    }
    fields.update(changes)
    return EquitySpec(**fields)


def make_adapter(
    fake: FakeAlpaca,
    *,
    since: datetime | None = None,
    specs: dict[str, Any] | None = None,
    caps: BrokerCapabilities | None = None,
    sleep: Callable[[float], None] | None = None,
) -> AlpacaPaperAdapter:
    return AlpacaPaperAdapter(
        account_id=ACCOUNT,
        credentials=CREDS,
        capabilities=caps or alpaca_caps(),
        instruments=specs if specs is not None else {"XNAS:AAPL": aapl_spec()},
        clock=fake.clock,
        mode=Mode.PAPER,
        since=since or T0 - timedelta(hours=1),
        transport=fake,
        sleep=sleep or (lambda _s: None),
    )


def make_plan(
    clock: Clock,
    instrument_id: str,
    cid: str,
    *,
    side: Side = Side.BUY,
    quantity: Decimal = Decimal("2"),
    order_type: OrderType | None = None,
    tif: TimeInForce = TimeInForce.DAY,
    valid_for: timedelta = timedelta(seconds=10),
) -> ApprovedPlan:
    now = clock.now()
    entry = side is Side.BUY
    kind = order_type or (OrderType.LIMIT if entry else OrderType.MARKET)
    intent = OrderIntent(
        intent_id=f"i-{cid}",
        client_order_id=cid,
        portfolio_id="pf-test",
        account_id=ACCOUNT,
        instrument_id=instrument_id,
        track=Track.STOCKS,
        side=side,
        purpose=OrderPurpose.ENTRY if entry else OrderPurpose.EXIT_STOP,
        order_type=kind,
        quantity=quantity,
        limit_price=Decimal("100.00") if kind is OrderType.LIMIT else None,
        time_in_force=tif,
        reason="adapter contract test",
        created_at=now,
    )
    return issue_plan(
        intent=intent,
        decision_id=f"d-{cid}",
        account_version=0,
        policy_hash="policy",
        model_hash="model",
        quote_time=now,
        approved_at=now,
        valid_until=now + valid_for,
        reserved_cash_usd=Decimal("200"),
        reserved_risk_usd=Decimal("1"),
        exit_method="test",
    )


# =========================================================================== contract suite


class Driver(Protocol):
    adapter: BrokerAdapter
    clock: ReplayClock
    instrument_id: str

    def fill(self, broker_order_id: str, quantity: Decimal) -> None: ...

    def hold(self, quantity: Decimal) -> None: ...

    def settle_cancel(self, broker_order_id: str) -> None: ...

    def close(self) -> None: ...


class ReplayDriver:
    _faults: ClassVar[dict[str, FaultKind]] = {
        "timeout_before": FaultKind.TIMEOUT_BEFORE_ACCEPT,
        "timeout_after": FaultKind.TIMEOUT_AFTER_ACCEPT,
        "reject": FaultKind.REJECT,
    }

    def __init__(self, fault: str | None) -> None:
        self.clock = ReplayClock(T0)
        self.instrument_id = "XNYS:SYNA"
        faults = (FaultSpec(kind=self._faults[fault]),) if fault else ()
        self.broker = ReplayBroker(
            account_id=ACCOUNT,
            clock=self.clock,
            capabilities=load_capabilities(ROOT / "configs" / "capabilities" / "replay.yaml"),
            fees=load_fee_schedule(ROOT / "configs" / "fees" / "replay_demo.yaml"),
            specs=load_instruments(ROOT / "configs" / "instruments" / "synthetic_equities.yaml"),
            faults=FaultPlan(faults=faults),
        )
        self.adapter: BrokerAdapter = self.broker
        self._seq = 0

    def _quote(self, ask: str, size: Decimal) -> None:
        self.clock.advance_to(self.clock.now() + timedelta(seconds=2))
        self._seq += 1
        now = self.clock.now()
        self.broker.on_market_event(
            Quote(
                instrument_id=self.instrument_id,
                feed="SYNTHETIC",
                event_time=now,
                receipt_time=now,
                sequence=self._seq,
                bid=Decimal(ask) - Decimal("0.05"),
                ask=Decimal(ask),
                bid_size=size,
                ask_size=size,
            )
        )

    def fill(self, broker_order_id: str, quantity: Decimal) -> None:
        self._quote("99.95", quantity * 2)  # participation 0.5 of the displayed size

    def hold(self, quantity: Decimal) -> None:
        self.broker.inject_manual_position(self.instrument_id, quantity, Decimal("100"))

    def settle_cancel(self, broker_order_id: str) -> None:
        self._quote("101.00", Decimal("10"))  # above the limit: the cancel takes effect

    def close(self) -> None:
        pass


class AlpacaDriver:
    _faults: ClassVar[dict[str, str]] = {
        "timeout_before": "timeout_before",
        "timeout_after": "timeout_after",
        "reject": "reject_403",
    }

    def __init__(self, fault: str | None) -> None:
        self.clock = ReplayClock(T0)
        self.instrument_id = "XNAS:AAPL"
        self.fake = FakeAlpaca(self.clock)
        self.fake.submit_fault = self._faults[fault] if fault else None
        self.alpaca = make_adapter(self.fake)
        self.adapter: BrokerAdapter = self.alpaca
        self.alpaca.start_stream()
        assert self.fake.stream_ready.wait(5), "fake trade stream did not start"

    def fill(self, broker_order_id: str, quantity: Decimal) -> None:
        self.fake.fill(broker_order_id, quantity, Decimal("99.95"))

    def hold(self, quantity: Decimal) -> None:
        self.fake.set_position("AAPL", quantity, Decimal("100"))

    def settle_cancel(self, broker_order_id: str) -> None:
        self.fake.confirm_cancel(broker_order_id)

    def close(self) -> None:
        self.alpaca.stop()


DriverFactory = Callable[..., Driver]
DRIVERS: dict[str, Callable[[str | None], Driver]] = {
    "replay": ReplayDriver,
    "alpaca": AlpacaDriver,
}


@pytest.fixture(params=sorted(DRIVERS))
def make_driver(request: pytest.FixtureRequest) -> Iterator[DriverFactory]:
    made: list[Driver] = []

    def factory(fault: str | None = None) -> Driver:
        driver = DRIVERS[request.param](fault)
        made.append(driver)
        return driver

    yield factory
    for driver in made:
        driver.close()


def _bound(driver: Driver) -> object:
    authority = object()
    driver.adapter.bind_authority(authority)
    return authority


def test_contract_submit_is_acknowledged_and_visible_by_client_id(
    make_driver: DriverFactory,
) -> None:
    d = make_driver()
    authority = _bound(d)
    result = d.adapter.submit(
        make_plan(d.clock, d.instrument_id, "co-contract-0001"), authority=authority
    )
    assert result.accepted and result.broker_order_id
    order = d.adapter.get_order_by_client_id("co-contract-0001")
    assert order is not None
    assert order.broker_order_id == result.broker_order_id
    assert order.status is BrokerOrderStatus.ACCEPTED
    assert (order.quantity, order.filled_quantity) == (Decimal("2"), Decimal("0"))
    assert [o.client_order_id for o in d.adapter.list_orders(OrderScope.OPEN)] == [
        "co-contract-0001"
    ]
    events = d.adapter.drain_events()
    assert events and {e.client_order_id for e in events} == {"co-contract-0001"}
    assert events[-1].status is BrokerOrderStatus.ACCEPTED
    assert d.adapter.get_order_by_client_id("co-contract-unknown") is None


def test_contract_duplicate_client_order_id_is_rejected_and_never_double_filled(
    make_driver: DriverFactory,
) -> None:
    d = make_driver()
    authority = _bound(d)
    plan = make_plan(d.clock, d.instrument_id, "co-contract-0002")
    first = d.adapter.submit(plan, authority=authority)
    second = d.adapter.submit(plan, authority=authority)
    assert first.accepted
    assert not second.accepted and second.status is BrokerOrderStatus.REJECTED
    assert second.broker_order_id == first.broker_order_id
    assert first.broker_order_id is not None
    d.fill(first.broker_order_id, Decimal("2"))
    d.adapter.drain_events()
    orders = [
        o for o in d.adapter.list_orders(OrderScope.ALL) if o.client_order_id == "co-contract-0002"
    ]
    assert len(orders) == 1 and orders[0].status is BrokerOrderStatus.FILLED
    executions = d.adapter.list_executions(broker_order_id=first.broker_order_id)
    assert sum(f.quantity for f in executions) == Decimal("2")


def test_contract_cancel_request_is_not_a_cancel(make_driver: DriverFactory) -> None:
    d = make_driver()
    authority = _bound(d)
    result = d.adapter.submit(
        make_plan(d.clock, d.instrument_id, "co-contract-0003"), authority=authority
    )
    assert result.broker_order_id is not None
    d.adapter.drain_events()
    request = d.adapter.cancel(result.broker_order_id)
    assert request.request_accepted
    events = d.adapter.drain_events()
    assert any(e.status is BrokerOrderStatus.PENDING_CANCEL for e in events)
    assert not any(e.status is BrokerOrderStatus.CANCELED for e in events)
    d.settle_cancel(result.broker_order_id)
    events = d.adapter.drain_events()
    assert events and events[-1].status is BrokerOrderStatus.CANCELED
    assert not d.adapter.cancel(result.broker_order_id).request_accepted  # no longer open
    assert not d.adapter.cancel(UNKNOWN_ORDER_ID).request_accepted


def test_contract_partial_fill_reports_one_execution(make_driver: DriverFactory) -> None:
    d = make_driver()
    authority = _bound(d)
    result = d.adapter.submit(
        make_plan(d.clock, d.instrument_id, "co-contract-0004"), authority=authority
    )
    assert result.broker_order_id is not None
    d.adapter.drain_events()
    d.fill(result.broker_order_id, Decimal("1"))
    events = d.adapter.drain_events()
    fills = [e.fill for e in events if e.fill is not None]
    assert len(fills) == 1 and fills[0].quantity == Decimal("1")
    assert events[-1].status is BrokerOrderStatus.PARTIALLY_FILLED
    assert events[-1].cum_filled_quantity == Decimal("1")
    order = d.adapter.get_order_by_client_id("co-contract-0004")
    assert order is not None and order.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("1")
    executions = d.adapter.list_executions(broker_order_id=result.broker_order_id)
    assert [f.execution_id for f in executions] == [fills[0].execution_id]
    assert not [e for e in d.adapter.drain_events() if e.fill is not None]  # never re-emitted


def test_contract_timeout_before_acceptance_is_an_unknown_outcome(
    make_driver: DriverFactory,
) -> None:
    d = make_driver("timeout_before")
    authority = _bound(d)
    with pytest.raises(BrokerTimeout):
        d.adapter.submit(
            make_plan(d.clock, d.instrument_id, "co-contract-0005"), authority=authority
        )
    assert d.adapter.get_order_by_client_id("co-contract-0005") is None


def test_contract_timeout_after_acceptance_is_resolved_by_client_id(
    make_driver: DriverFactory,
) -> None:
    d = make_driver("timeout_after")
    authority = _bound(d)
    with pytest.raises(BrokerTimeout):
        d.adapter.submit(
            make_plan(d.clock, d.instrument_id, "co-contract-0006"), authority=authority
        )
    order = d.adapter.get_order_by_client_id("co-contract-0006")
    assert order is not None and order.status is BrokerOrderStatus.ACCEPTED


def test_contract_definite_rejection_is_not_accepted(make_driver: DriverFactory) -> None:
    d = make_driver("reject")
    authority = _bound(d)
    result = d.adapter.submit(
        make_plan(d.clock, d.instrument_id, "co-contract-0007"), authority=authority
    )
    assert not result.accepted and result.status is BrokerOrderStatus.REJECTED
    assert result.reject_reason
    assert d.adapter.get_order_by_client_id("co-contract-0007") is None


def test_contract_only_the_bound_authority_submits_valid_plans(
    make_driver: DriverFactory,
) -> None:
    d = make_driver()
    plan = make_plan(d.clock, d.instrument_id, "co-contract-0008")
    with pytest.raises(UnauthorizedSubmission):
        d.adapter.submit(plan, authority=object())  # nothing bound yet
    authority = _bound(d)
    with pytest.raises(UnauthorizedSubmission):
        d.adapter.submit(plan, authority=object())
    tampered = plan.model_copy(
        update={"intent": plan.intent.model_copy(update={"quantity": Decimal("3")})}
    )
    with pytest.raises(UnauthorizedSubmission):
        d.adapter.submit(tampered, authority=authority)
    d.clock.advance_to(plan.valid_until)
    with pytest.raises(UnauthorizedSubmission):
        d.adapter.submit(plan, authority=authority)
    assert d.adapter.get_order_by_client_id("co-contract-0008") is None


def test_contract_sells_never_exceed_the_position(make_driver: DriverFactory) -> None:
    d = make_driver()
    authority = _bound(d)
    d.hold(Decimal("1"))
    too_many = make_plan(
        d.clock, d.instrument_id, "co-contract-0009", side=Side.SELL, quantity=Decimal("2")
    )
    result = d.adapter.submit(too_many, authority=authority)
    assert not result.accepted and "shorting" in (result.reject_reason or "")
    owned = make_plan(
        d.clock, d.instrument_id, "co-contract-0010", side=Side.SELL, quantity=Decimal("1")
    )
    assert d.adapter.submit(owned, authority=authority).accepted


# =========================================================================== paper only


@pytest.mark.parametrize("mode", [Mode.REPLAY, Mode.SHADOW, Mode.LIVE])
def test_construction_is_refused_outside_paper_mode(mode: Mode) -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    with pytest.raises(PaperOnlyError, match="paper mode"):
        AlpacaPaperAdapter(
            account_id=ACCOUNT,
            credentials=CREDS,
            capabilities=alpaca_caps(),
            instruments={"XNAS:AAPL": aapl_spec()},
            clock=fake.clock,
            mode=mode,
            since=T0,
            transport=fake,
        )


@pytest.mark.parametrize("environment", ["live", "demo", "replay"])
def test_construction_is_refused_for_a_non_paper_profile(environment: str) -> None:
    caps = alpaca_caps().model_copy(update={"environment": environment})
    with pytest.raises(PaperOnlyError, match="environment"):
        make_adapter(FakeAlpaca(ReplayClock(T0)), caps=caps)
    replay_caps = load_capabilities(ROOT / "configs" / "capabilities" / "replay.yaml")
    with pytest.raises(PaperOnlyError):
        make_adapter(FakeAlpaca(ReplayClock(T0)), caps=replay_caps)


def test_construction_is_refused_for_a_live_looking_key_or_foreign_specs() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    with pytest.raises(PaperOnlyError, match="LIVE"):
        AlpacaPaperAdapter(
            account_id=ACCOUNT,
            credentials=AlpacaCredentials("AKLIVEKEY000000000001", PAPER_SECRET),
            capabilities=alpaca_caps(),
            instruments={},
            clock=fake.clock,
            mode=Mode.PAPER,
            since=T0,
            transport=fake,
        )
    synthetic = load_instruments(ROOT / "configs" / "instruments" / "synthetic_equities.yaml")
    with pytest.raises(DomainError, match="not alpaca"):
        make_adapter(fake, specs=dict(synthetic))


def test_the_adapter_takes_no_url_and_the_package_names_no_live_endpoint() -> None:
    params = inspect.signature(AlpacaPaperAdapter.__init__).parameters
    assert not [p for p in params if re.search("url|endpoint|base|host", p)]
    live_host = re.compile(r"(?<!paper-)api\.alpaca\.markets")
    for path in sorted(ALPACA_PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not live_host.search(text), path.name
        for banned in ("url_override", "TRADING_LIVE", "TRADING_STREAM_LIVE", "base_url"):
            assert banned not in text, f"{path.name} mentions {banned}"
        assert "paper=False" not in text.replace(" ", ""), path.name


def test_every_sdk_client_is_constructed_with_paper_true() -> None:
    tree = ast.parse((ALPACA_PACKAGE / "transport.py").read_text(encoding="utf-8"))
    sdk = {"TradingClient", "_PaperTradingClient", "TradingStream", "_ConnectionAwareStream"}
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in sdk
    ]
    assert len(calls) == 2
    for call in calls:
        paper = [k for k in call.keywords if k.arg == "paper"]
        assert len(paper) == 1 and isinstance(paper[0].value, ast.Constant)
        assert paper[0].value.value is True
        assert not [k for k in call.keywords if k.arg in ("url_override", "base_url")]


def test_sdk_transport_targets_the_paper_endpoint_with_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "_inject_truststore", lambda: None)
    transport = SdkTransport(CREDS, request_timeout_seconds=7.5)
    client = transport._client
    assert str(client._base_url.value) == "https://paper-api.alpaca.markets"
    assert client._sandbox is True and client._use_raw_data is True
    assert client._retry_codes == [429]  # never auto-retry an ambiguous 504 POST
    stream = transport.open_trade_stream(lambda: None)
    assert str(stream._endpoint.value) == "wss://paper-api.alpaca.markets/stream"
    assert stream._raw_data is True

    seen: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        seen.update(method=method, url=url, **kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            text='{"is_open": true}',
            json=lambda: {"is_open": True},
            status_code=200,
        )

    monkeypatch.setattr(client._session, "request", fake_request)
    assert transport.get_clock() == {"is_open": True}
    assert seen["timeout"] == 7.5
    assert seen["url"] == "https://paper-api.alpaca.markets/v2/clock"
    assert seen["headers"]["APCA-API-KEY-ID"] == PAPER_KEY  # sent only as a header


def test_truststore_is_injected_once_before_any_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(transport_module, "_truststore_injected", False)
    monkeypatch.setattr(
        transport_module.truststore, "inject_into_ssl", lambda: calls.append("inject")
    )
    SdkTransport(CREDS)
    SdkTransport(CREDS)
    assert calls == ["inject"]


# =========================================================================== outcomes


def _api_error(status: int | None, body: str) -> APIError:
    http_error = (
        None if status is None else SimpleNamespace(response=SimpleNamespace(status_code=status))
    )
    return APIError(body, http_error)


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (
            403,
            '{"code":40310000,"message":"insufficient buying power"}',
            DefiniteRejection,
            "40310000",
        ),
        (422, '{"code":40010001,"message":"qty must be > 0"}', DefiniteRejection, "40010001"),
        (
            401,
            '{"code":40110000,"message":"request is not authorized"}',
            DefiniteRejection,
            "40110000",
        ),
        (404, '{"code":40410000,"message":"order not found"}', NotFound, "40410000"),
        (429, '{"code":42910000,"message":"rate limit exceeded"}', BrokerTimeout, None),
        (408, "request timeout", BrokerTimeout, None),
        (500, '{"code":50010000,"message":"internal server error"}', BrokerTimeout, None),
        (502, "<html>bad gateway</html>", BrokerTimeout, None),
        (504, "", BrokerTimeout, None),
        (None, "connection reset", BrokerTimeout, None),
    ],
)
def test_sdk_errors_are_definite_or_unknown(
    status: int | None, body: str, kind: type[Exception], code: str | None
) -> None:
    error = translate_api_error(_api_error(status, body))
    assert type(error) is kind
    if isinstance(error, DefiniteRejection):
        assert error.http_status == status and error.code == code
    else:
        assert not isinstance(error, BrokerError)  # the gateway treats it as unknown outcome


def test_duplicate_client_order_id_refusal_is_recognised() -> None:
    error = translate_api_error(
        _api_error(422, '{"code":40010001,"message":"client_order_id must be unique"}')
    )
    assert isinstance(error, DefiniteRejection) and error.is_duplicate_client_order_id


@pytest.mark.parametrize(
    "exc", [ConnectionError("reset"), TimeoutError("read"), ValueError("json")]
)
def test_transport_failures_are_unknown_outcomes(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr(transport_module, "_inject_truststore", lambda: None)
    transport = SdkTransport(CREDS)

    def broken(method: str, url: str, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(transport._client._session, "request", broken)
    with pytest.raises(BrokerTimeout):
        transport.submit_order({"symbol": "AAPL"})


def test_a_duplicate_at_alpaca_is_an_unknown_outcome_not_a_rejection() -> None:
    """A second worker (or a restart) re-sending a client ID Alpaca already holds."""
    fake = FakeAlpaca(ReplayClock(T0))
    first, second = make_adapter(fake), make_adapter(fake)
    a1, a2 = object(), object()
    first.bind_authority(a1)
    second.bind_authority(a2)
    plan = make_plan(fake.clock, "XNAS:AAPL", "co-dup-remote")
    assert first.submit(plan, authority=a1).accepted
    with pytest.raises(BrokerTimeout, match="already holds"):
        second.submit(plan, authority=a2)
    assert len([o for o in fake.orders.values() if o["client_order_id"] == "co-dup-remote"]) == 1


def test_an_unmappable_submit_response_is_an_unknown_outcome() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    fake.submit_fault = "response_status:replaced"
    with pytest.raises(BrokerTimeout, match="outcome unknown"):
        adapter.submit(make_plan(fake.clock, "XNAS:AAPL", "co-odd-status"), authority=authority)


def test_a_response_status_of_rejected_is_not_accepted() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    fake.submit_fault = "response_status:rejected"
    result = adapter.submit(make_plan(fake.clock, "XNAS:AAPL", "co-rejected"), authority=authority)
    assert not result.accepted and result.broker_order_id is not None


def test_cancel_timeouts_raise_and_definite_refusals_return() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    result = adapter.submit(make_plan(fake.clock, "XNAS:AAPL", "co-cancel-t"), authority=authority)
    assert result.broker_order_id is not None
    fake.fail_next["cancel_order"] = [BrokerTimeout("fake: cancel timed out")]
    with pytest.raises(BrokerTimeout):
        adapter.cancel(result.broker_order_id)
    assert not adapter.cancel("../account").request_accepted  # never reaches the transport
    assert fake.calls.count("cancel_order") == 1


# =========================================================================== order shapes


@pytest.mark.parametrize(
    ("kwargs", "spec_changes", "reason"),
    [
        ({"tif": TimeInForce.IOC}, {}, "only DAY"),
        ({"order_type": OrderType.MARKET}, {}, "LIMIT DAY"),
        ({"quantity": Decimal("0.5")}, {}, "fractional.limit.day"),
        ({"quantity": Decimal("0.5")}, {"fractionable": False}, "non-fractionable"),
        ({}, {"evidence": EvidenceLevel.UNKNOWN}, "not certified"),
        ({}, {"tradable": False}, "not certified"),
    ],
)
def test_unsupported_order_shapes_are_refused_without_sending(
    kwargs: dict[str, Any], spec_changes: dict[str, Any], reason: str
) -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake, specs={"XNAS:AAPL": aapl_spec(**spec_changes)})
    authority = object()
    adapter.bind_authority(authority)
    result = adapter.submit(
        make_plan(fake.clock, "XNAS:AAPL", "co-shape-0001", **kwargs), authority=authority
    )
    assert not result.accepted and reason in (result.reject_reason or "")
    assert "submit_order" not in fake.calls


def test_exits_need_only_a_known_instrument_and_a_usable_market_capability() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake, specs={"XNAS:AAPL": aapl_spec(evidence=EvidenceLevel.UNKNOWN)})
    authority = object()
    adapter.bind_authority(authority)
    fake.set_position("AAPL", Decimal("0.5"), Decimal("100"))
    plan = make_plan(
        fake.clock, "XNAS:AAPL", "co-exit-frac", side=Side.SELL, quantity=Decimal("0.5")
    )
    assert adapter.submit(plan, authority=authority).accepted
    sent = next(iter(fake.orders.values()))
    assert (sent["qty"], sent["type"], sent["time_in_force"], sent["side"]) == (
        "0.5",
        "market",
        "day",
        "sell",
    )
    unknown = make_plan(fake.clock, "XNAS:MSFT", "co-exit-unknown", side=Side.SELL)
    result = adapter.submit(unknown, authority=authority)
    assert not result.accepted and result.reject_reason == "unknown instrument"


def test_entries_are_sent_as_exact_limit_day_buys() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    adapter.submit(make_plan(fake.clock, "XNAS:AAPL", "co-entry-0001"), authority=authority)
    sent = next(iter(fake.orders.values()))
    assert sent["client_order_id"] == "co-entry-0001"
    assert (sent["qty"], sent["limit_price"], sent["type"], sent["time_in_force"]) == (
        "2",
        "100",
        "limit",
        "day",
    )


def test_a_plan_that_expires_while_pacing_is_not_sent() -> None:
    clock = ReplayClock(T0)
    fake = FakeAlpaca(clock)

    def sleep(seconds: float) -> None:
        clock.advance_to(clock.now() + timedelta(seconds=seconds))

    adapter = make_adapter(fake, sleep=sleep)
    authority = object()
    adapter.bind_authority(authority)
    for _ in range(10):  # use up the burst capacity
        adapter.get_positions()
    plan = make_plan(clock, "XNAS:AAPL", "co-late-0001", valid_for=timedelta(milliseconds=200))
    result = adapter.submit(plan, authority=authority)
    assert not result.accepted and "expired" in (result.reject_reason or "")
    assert "submit_order" not in fake.calls


# =========================================================================== mapping


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.50", Decimal("1.50")),
        ("0", Decimal("0")),
        ("-2.5", Decimal("-2.5")),
        ("0.000000001", Decimal("0.000000001")),
        ("179.08", Decimal("179.08")),
        (7, Decimal(7)),
        (0.1, Decimal("0.1")),
        (105.8988475, Decimal("105.8988475")),
    ],
)
def test_numbers_become_exact_decimals(value: object, expected: Decimal) -> None:
    parsed = parse_decimal(value, "field")
    assert parsed == expected and str(parsed) == str(expected)


@pytest.mark.parametrize(
    "value",
    [
        "1e5",
        " 1",
        "1.0 ",
        "NaN",
        "Infinity",
        "",
        "abc",
        "+1",
        "007",
        "1,5",
        True,
        None,
        float("nan"),
        float("inf"),
        [],
        {},
    ],
)
def test_numbers_that_do_not_round_trip_are_refused(value: object) -> None:
    with pytest.raises(MappingError):
        parse_decimal(value, "field")


def test_versions_are_epoch_microseconds_identical_across_representations() -> None:
    micro = parse_timestamp("2026-11-27T15:00:01.123456Z", "t")
    nano = parse_timestamp("2026-11-27T15:00:01.123456789Z", "t")
    offset = parse_timestamp("2026-11-27T10:00:01.123456-05:00", "t")
    assert micro == nano == offset
    assert micro.tzinfo is UTC
    version = epoch_micros(micro)
    assert version == 1_795_791_601_123_456 and version > 2**31
    later = parse_timestamp("2026-11-27T15:00:01.123457Z", "t")
    assert epoch_micros(later) == version + 1
    assert epoch_micros(parse_timestamp("2026-11-27T15:00:01Z", "t")) < version


@pytest.mark.parametrize(
    "value",
    ["2026-11-27 15:00:01Z", "2026-11-27T15:00:01", "2026-13-01T00:00:00Z", 1700000000, None],
)
def test_malformed_timestamps_are_refused(value: object) -> None:
    with pytest.raises(MappingError):
        parse_timestamp(value, "t")


DOCUMENTED_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "pending_review",
    "accepted_for_bidding",
    "held",
    "stopped",
    "done_for_day",
    "calculated",
    "partially_filled",
    "filled",
    "pending_cancel",
    "canceled",
    "expired",
    "rejected",
    "replaced",
    "pending_replace",
    "suspended",
}


def test_every_documented_alpaca_status_has_an_explicit_rule() -> None:
    assert set(STATUS_RULES) == DOCUMENTED_STATUSES


def test_every_status_the_pinned_sdk_knows_has_an_explicit_rule() -> None:
    from alpaca.trading.enums import OrderStatus

    assert {s.value for s in OrderStatus} <= set(STATUS_RULES)


@pytest.mark.parametrize(
    ("status", "filled", "expected"),
    [
        ("new", "0", BrokerOrderStatus.ACCEPTED),
        ("accepted", "0", BrokerOrderStatus.ACCEPTED),
        ("pending_new", "0", BrokerOrderStatus.ACCEPTED),
        ("pending_review", "0", BrokerOrderStatus.ACCEPTED),
        ("accepted_for_bidding", "0", BrokerOrderStatus.ACCEPTED),
        ("held", "0", BrokerOrderStatus.ACCEPTED),
        ("stopped", "0", BrokerOrderStatus.ACCEPTED),
        ("done_for_day", "0", BrokerOrderStatus.ACCEPTED),
        ("done_for_day", "1", BrokerOrderStatus.PARTIALLY_FILLED),
        ("calculated", "1", BrokerOrderStatus.PARTIALLY_FILLED),
        ("calculated", "2", BrokerOrderStatus.FILLED),
        ("partially_filled", "1", BrokerOrderStatus.PARTIALLY_FILLED),
        ("filled", "2", BrokerOrderStatus.FILLED),
        ("pending_cancel", "1", BrokerOrderStatus.PENDING_CANCEL),
        ("canceled", "1", BrokerOrderStatus.CANCELED),
        ("expired", "0", BrokerOrderStatus.EXPIRED),
        ("rejected", "0", BrokerOrderStatus.REJECTED),
    ],
)
def test_status_mapping_table(status: str, filled: str, expected: BrokerOrderStatus) -> None:
    mapped = map_status(
        status,
        filled=Decimal(filled),
        quantity=Decimal("2"),
        broker_order_id="b",
        client_order_id="c",
    )
    assert mapped is expected
    # a working status is never terminal: exposure stays reserved until the broker says so
    if STATUS_RULES[status] == "WORKING" and filled != "2":
        assert not mapped.is_terminal


@pytest.mark.parametrize("status", ["replaced", "pending_replace", "suspended", "brand_new_2027"])
def test_unmapped_statuses_raise(status: str) -> None:
    with pytest.raises(UnmappedStatus, match="owner review"):
        map_status(
            status, filled=Decimal(0), quantity=Decimal(1), broker_order_id="b", client_order_id="c"
        )


def _raw_order(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "client_order_id": "co-map-0001",
        "created_at": "2026-11-27T15:00:00.000001Z",
        "updated_at": "2026-11-27T15:00:00.500000Z",
        "submitted_at": "2026-11-27T15:00:00.000002Z",
        "symbol": "AAPL",
        "qty": "2",
        "filled_qty": "1",
        "type": "limit",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": "100.25",
        "status": "partially_filled",
    }
    raw.update(changes)
    return raw


def test_orders_map_exactly_and_unknown_symbols_stay_visible() -> None:
    symbols = SymbolMap.from_specs({"XNAS:AAPL": aapl_spec()})
    order = map_order(_raw_order(), broker="alpaca_paper", account_id=ACCOUNT, symbols=symbols)
    assert order.instrument_id == "XNAS:AAPL" and order.limit_price == Decimal("100.25")
    assert order.broker_version == epoch_micros(parse_timestamp(_raw_order()["updated_at"], "u"))
    other = map_order(
        _raw_order(symbol="TSLA"), broker="alpaca_paper", account_id=ACCOUNT, symbols=symbols
    )
    assert other.instrument_id == "UNMAPPED:TSLA"
    position = map_position(
        {"symbol": "TSLA", "qty": "3", "avg_entry_price": "200", "side": "long"}, symbols=symbols
    )
    assert position.instrument_id == "UNMAPPED:TSLA" and position.quantity == Decimal("3")


@pytest.mark.parametrize(
    "changes",
    [
        {"qty": None, "notional": "500"},
        {"type": "stop"},
        {"status": "filled"},  # contradicts filled_qty 1 of 2
        {"filled_qty": "3"},
        {"qty": 1.5e400},
        {"updated_at": "yesterday"},
        {"side": "short"},
        {"client_order_id": "x" * 41},  # longer than the ledger stores
    ],
)
def test_inconsistent_orders_are_refused(changes: dict[str, Any]) -> None:
    symbols = SymbolMap.from_specs({"XNAS:AAPL": aapl_spec()})
    with pytest.raises(MappingError):
        map_order(_raw_order(**changes), broker="alpaca_paper", account_id=ACCOUNT, symbols=symbols)


# =========================================================================== events and fills


@pytest.fixture
def live() -> Iterator[tuple[FakeAlpaca, AlpacaPaperAdapter, object]]:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    adapter.start_stream()
    assert fake.stream_ready.wait(5)
    yield fake, adapter, authority
    adapter.stop()


def _submit(adapter: AlpacaPaperAdapter, authority: object, cid: str) -> str:
    result = adapter.submit(make_plan(adapter.clock, "XNAS:AAPL", cid), authority=authority)
    assert result.accepted and result.broker_order_id is not None
    return result.broker_order_id


def test_fills_carry_the_rest_activity_id_never_the_stream_execution_id(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    fake, adapter, authority = live
    boid = _submit(adapter, authority, "co-exec-0001")
    adapter.drain_events()
    activity_id = fake.fill(boid, Decimal("1"), Decimal("99.95"))
    events = adapter.drain_events()
    fills = [e.fill for e in events if e.fill is not None]
    assert [f.execution_id for f in fills] == [activity_id]
    assert all(e.event_id == f"fill:{activity_id}" for e in events if e.fill is not None)
    fill = fills[0]
    assert (fill.price, fill.quantity, fill.side) == (Decimal("99.95"), Decimal("1"), Side.BUY)
    assert fill.fee == 0 and fill.fee_status.value == "PENDING"  # fees arrive later, never guessed
    assert fill.client_order_id == "co-exec-0001" and fill.instrument_id == "XNAS:AAPL"
    rest = adapter.list_executions(broker_order_id=boid)
    assert [r.execution_id for r in rest] == [activity_id]
    assert rest[0].model_dump(exclude={"receipt_time"}) == fill.model_dump(exclude={"receipt_time"})
    assert [r.execution_id for r in adapter.list_executions()] == [activity_id]
    fake.fill(boid, Decimal("1"), Decimal("99.90"))
    events = adapter.drain_events()
    assert len([e for e in events if e.fill is not None]) == 1  # only the new execution
    assert events[-1].status is BrokerOrderStatus.FILLED


def test_a_stream_fill_waits_for_its_rest_activity(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    fake, adapter, authority = live
    boid = _submit(adapter, authority, "co-lag-0001")
    adapter.drain_events()
    activity_id = fake.fill(boid, Decimal("1"), Decimal("99.95"))
    fake.hidden_activities.add(activity_id)  # the activity feed lags the stream
    events = adapter.drain_events()
    assert [e.status for e in events] == [BrokerOrderStatus.PARTIALLY_FILLED]
    assert all(e.fill is None for e in events)
    assert not adapter.drain_events()  # retried, still not visible: nothing invented
    fake.hidden_activities.clear()
    events = adapter.drain_events()
    assert [e.fill.execution_id for e in events if e.fill is not None] == [activity_id]
    assert not adapter.drain_events()


def test_stream_and_rest_views_of_one_state_share_version_and_event_id(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    fake, adapter, authority = live
    boid = _submit(adapter, authority, "co-version-01")
    adapter.drain_events()
    order = dict(fake.orders[boid])
    rest_view = adapter.get_order_by_client_id("co-version-01")
    assert rest_view is not None
    updated = parse_timestamp(order["updated_at"], "u")
    fake.push_raw(
        {
            "stream": "trade_updates",
            "data": {"event": "new", "order": {**order, "updated_at": ts_nanos(updated)}},
        }
    )
    (event,) = adapter.drain_events()
    assert event.broker_version == rest_view.broker_version == epoch_micros(updated)
    assert event.event_id == f"{boid}:v{rest_view.broker_version}"
    fake.fill(boid, Decimal("1"), Decimal("99.95"))
    later = adapter.drain_events()
    assert min(e.broker_version for e in later) > rest_view.broker_version


def test_a_stream_reconnection_triggers_a_rest_refresh_of_working_orders(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    fake, adapter, authority = live
    boid = _submit(adapter, authority, "co-gap-00001")
    adapter.drain_events()
    fake.fill(boid, Decimal("2"), Decimal("99.95"), stream=False)  # lost during a gap
    fake.calls.clear()
    fake.sessions[-1].on_connected()  # the SDK reconnected silently
    events = adapter.drain_events()
    assert "get_order" in fake.calls
    assert events[-1].status is BrokerOrderStatus.FILLED
    assert [e.fill.quantity for e in events if e.fill is not None] == [Decimal("2")]


def test_working_orders_are_polled_while_a_live_stream_is_silent(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    """A live stream thread does not prove a connected socket (alpaca-py reconnects inside it),
    so a fill the stream never reports is still found by the 5 s poll, not the 60 s
    reconciliation."""
    fake, adapter, authority = live
    boid = _submit(adapter, authority, "co-silent-0001")
    adapter.drain_events()
    fake.fill(boid, Decimal("2"), Decimal("99.95"), stream=False)  # the socket is down
    fake.calls.clear()
    clock = adapter.clock
    assert isinstance(clock, ReplayClock)
    clock.advance_to(clock.now() + POLL_INTERVAL_WITHOUT_STREAM)
    events = adapter.drain_events()
    assert "get_order" in fake.calls
    assert events[-1].status is BrokerOrderStatus.FILLED
    assert [e.fill.quantity for e in events if e.fill is not None] == [Decimal("2")]


def test_an_unmapped_stream_status_raises_but_keeps_the_mapped_events(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    fake, adapter, authority = live
    good = _submit(adapter, authority, "co-good-0001")
    odd = _submit(adapter, authority, "co-odd-00001")
    adapter.drain_events()
    fake.fill(good, Decimal("1"), Decimal("99.95"))
    fake.set_status(odd, "suspended")
    with pytest.raises(UnmappedStatus, match="suspended"):
        adapter.drain_events()
    assert adapter.anomalies and "suspended" in adapter.anomalies[0]
    kept = adapter.drain_events()
    assert [e.fill.quantity for e in kept if e.fill is not None] == [Decimal("1")]
    assert {e.broker_order_id for e in kept} == {good}
    with pytest.raises(UnmappedStatus):
        adapter.get_order_by_client_id("co-odd-00001")  # REST views fail closed too


def test_an_unexpected_failure_while_draining_loses_no_event(
    live: tuple[FakeAlpaca, AlpacaPaperAdapter, object],
) -> None:
    fake, adapter, authority = live
    boid = _submit(adapter, authority, "co-crash-0001")
    adapter.drain_events()
    activity_id = fake.fill(boid, Decimal("1"), Decimal("99.95"))
    fake.fail_next["list_fill_activities"] = [RuntimeError("transport bug")]
    with pytest.raises(RuntimeError):
        adapter.drain_events()
    events = adapter.drain_events()
    assert events[0].status is BrokerOrderStatus.PARTIALLY_FILLED  # the kept stream event
    assert [e.fill.execution_id for e in events if e.fill is not None] == [activity_id]


def test_cancel_requests_are_followed_up_over_rest_without_a_stream() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    boid = _submit(adapter, authority, "co-nostream-1")
    assert adapter.cancel(boid).request_accepted
    events = adapter.drain_events()
    assert [e.status for e in events] == [
        BrokerOrderStatus.ACCEPTED,  # the submit response
        BrokerOrderStatus.PENDING_CANCEL,  # REST refresh after the cancel request
    ]


def test_without_a_stream_working_orders_are_polled_on_the_injected_clock() -> None:
    clock = ReplayClock(T0)
    fake = FakeAlpaca(clock)
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    boid = _submit(adapter, authority, "co-poll-00001")
    first = fake.fill(boid, Decimal("1"), Decimal("99.95"), stream=False)
    events = adapter.drain_events()
    assert [e.fill.execution_id for e in events if e.fill is not None] == [first]
    assert events[-1].status is BrokerOrderStatus.PARTIALLY_FILLED
    second = fake.fill(boid, Decimal("1"), Decimal("99.90"), stream=False)
    fake.calls.clear()
    assert adapter.drain_events() == []  # within the poll interval: no request at all
    assert fake.calls == []
    clock.advance_to(T0 + POLL_INTERVAL_WITHOUT_STREAM)
    events = adapter.drain_events()
    assert [e.fill.execution_id for e in events if e.fill is not None] == [second]
    assert events[-1].status is BrokerOrderStatus.FILLED
    clock.advance_to(T0 + 3 * POLL_INTERVAL_WITHOUT_STREAM)
    fake.calls.clear()
    assert adapter.drain_events() == []  # nothing left working: nothing polled
    assert "get_order" not in fake.calls


# =========================================================================== account, scope


def test_account_snapshot_reports_unverified_rules_and_restrictions() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    fake.set_position("AAPL", Decimal("1.5"), Decimal("100"))
    snapshot = adapter.get_account()
    assert snapshot.broker == "alpaca_paper" and snapshot.account_id == ACCOUNT
    assert snapshot.legal_type is LegalAccountType.UNKNOWN
    assert snapshot.rule_profile is RuleProfileKind.UNVERIFIED
    assert snapshot.cash == Decimal("100000") and snapshot.buying_power == Decimal("100000")
    assert [(p.instrument_id, p.quantity) for p in snapshot.positions] == [
        ("XNAS:AAPL", Decimal("1.5"))
    ]
    assert snapshot.restrictions == () and snapshot.day_trades_remaining is None
    fake.account_overrides = {"trading_blocked": True, "status": "ACCOUNT_UPDATED"}
    assert set(adapter.get_account().restrictions) == {"TRADING_BLOCKED", "STATUS_ACCOUNT_UPDATED"}


def test_history_is_scoped_to_since_but_open_orders_never_are() -> None:
    clock = ReplayClock(T0 - timedelta(hours=3))
    fake = FakeAlpaca(clock)
    old_open = fake.add_order(
        symbol="AAPL",
        side="buy",
        qty="1",
        status="new",
        client_order_id="manual-open",
        at=clock.now(),
    )
    old_done = fake.add_order(
        symbol="AAPL",
        side="buy",
        qty="1",
        status="new",
        client_order_id="manual-done",
        at=clock.now(),
    )
    fake.fill(old_done, Decimal("1"), Decimal("90"))
    clock.advance_to(T0)
    adapter = make_adapter(fake, since=T0 - timedelta(hours=1))
    authority = object()
    adapter.bind_authority(authority)
    new = _submit(adapter, authority, "co-scope-0001")
    fake.fill(new, Decimal("2"), Decimal("99.95"))
    assert [o.broker_order_id for o in adapter.list_orders(OrderScope.OPEN)] == [old_open]
    assert [o.broker_order_id for o in adapter.list_orders(OrderScope.CLOSED)] == [new]
    assert {o.broker_order_id for o in adapter.list_orders(OrderScope.ALL)} == {old_open, new}
    assert [f.broker_order_id for f in adapter.list_executions()] == [new]
    # one order's executions are always complete, whenever they happened
    assert len(adapter.list_executions(broker_order_id=old_done)) == 1


def test_drained_fills_of_an_older_working_order_start_at_since() -> None:
    clock = ReplayClock(T0 - timedelta(hours=3))
    fake = FakeAlpaca(clock)
    old = fake.add_order(
        symbol="AAPL",
        side="buy",
        qty="3",
        status="new",
        client_order_id="manual-old",
        at=clock.now(),
    )
    fake.fill(old, Decimal("1"), Decimal("90"), stream=False)  # before since
    clock.advance_to(T0)
    adapter = make_adapter(fake, since=T0 - timedelta(hours=1))
    assert [o.broker_order_id for o in adapter.list_orders(OrderScope.OPEN)] == [old]
    adapter.drain_events()
    recent = fake.fill(old, Decimal("1"), Decimal("91"), stream=False)
    clock.advance_to(T0 + POLL_INTERVAL_WITHOUT_STREAM)
    events = adapter.drain_events()
    assert [e.fill.execution_id for e in events if e.fill is not None] == [recent]
    assert len(adapter.list_executions(broker_order_id=old)) == 2  # REST history stays complete


def test_an_order_alpaca_files_as_closed_but_still_working_is_open() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    fake.closed_statuses = fake.closed_statuses | {"done_for_day"}
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    boid = _submit(adapter, authority, "co-dfd-000001")
    fake.set_status(boid, "done_for_day", stream=False)
    (order,) = adapter.list_orders(OrderScope.OPEN)
    assert order.broker_order_id == boid and order.status is BrokerOrderStatus.ACCEPTED
    assert adapter.list_orders(OrderScope.CLOSED) == []
    assert adapter.get_account().open_order_count == 1
    fake.set_status(boid, "expired", stream=False)
    assert adapter.list_orders(OrderScope.OPEN) == []
    assert [o.status for o in adapter.list_orders(OrderScope.CLOSED)] == [BrokerOrderStatus.EXPIRED]


def test_order_and_activity_pagination_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.brokers.alpaca.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ORDER_PAGE_SIZE", 3)
    monkeypatch.setattr(adapter_module, "ACTIVITY_PAGE_SIZE", 2)
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    ids = [_submit(adapter, authority, f"co-page-{n:05d}") for n in range(7)]
    for boid in ids:
        fake.fill(boid, Decimal("1"), Decimal("99"))
    assert [o.broker_order_id for o in adapter.list_orders(OrderScope.OPEN)] == ids
    assert len(adapter.list_executions()) == 7
    assert fake.calls.count("list_fill_activities") >= 4


def test_recent_executions_are_read_newest_first_and_stop_at_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No server-side time filter is trusted (Alpaca's ``after`` refers to ``created_at``)."""
    import trader.brokers.alpaca.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ACTIVITY_PAGE_SIZE", 2)
    clock = ReplayClock(T0 - timedelta(hours=3))
    fake = FakeAlpaca(clock)
    for n in range(5):
        old = fake.add_order(
            symbol="AAPL",
            side="buy",
            qty="1",
            status="new",
            client_order_id=f"old-{n}",
            at=clock.now(),
        )
        fake.fill(old, Decimal("1"), Decimal("90"))
        clock.advance_to(clock.now() + timedelta(minutes=1))
    clock.advance_to(T0)
    adapter = make_adapter(fake, since=T0 - timedelta(minutes=30))
    authority = object()
    adapter.bind_authority(authority)
    recent = [_submit(adapter, authority, f"co-recent-{n:03d}") for n in range(3)]
    for boid in recent:
        fake.fill(boid, Decimal("2"), Decimal("99"))
        clock.advance_to(clock.now() + timedelta(seconds=1))
    fake.calls.clear()
    executions = adapter.list_executions()
    assert [f.broker_order_id for f in executions] == recent  # oldest first
    assert fake.calls.count("list_fill_activities") == 2  # [r3, r2], [r1, old]: stop


# =========================================================================== pacing


def test_requests_are_paced_below_the_rate_limit_in_any_minute() -> None:
    clock = ReplayClock(T0)

    def sleep(seconds: float) -> None:
        clock.advance_to(clock.now() + timedelta(seconds=seconds))

    bucket = TokenBucket(limit_per_minute=200, clock=clock, sleep=sleep)
    stamps = []
    for _ in range(450):
        bucket.acquire()
        stamps.append(clock.now())
    window = timedelta(seconds=60)
    worst = max(sum(1 for t in stamps if s <= t < s + window) for s in stamps)
    assert worst <= 200
    assert bucket.waits > 0
    assert stamps[-1] - stamps[0] < timedelta(seconds=150)  # paced, not stalled


def test_pacing_never_deadlocks_on_a_frozen_clock() -> None:
    slept: list[float] = []
    bucket = TokenBucket(limit_per_minute=200, clock=ReplayClock(T0), sleep=slept.append)
    for _ in range(40):
        bucket.acquire()
    assert len(slept) == 30
    assert abs(sum(slept) - 30 * 60 / 190) < 0.01


def test_every_adapter_request_takes_a_token() -> None:
    fake = FakeAlpaca(ReplayClock(T0))
    slept: list[float] = []
    adapter = make_adapter(fake, sleep=slept.append)
    for _ in range(12):
        adapter.get_positions()
    assert len(slept) == 2  # burst capacity 10, then paced


# =========================================================================== hygiene


def test_credentials_never_appear_in_reprs_errors_or_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(transport_module, "_inject_truststore", lambda: None)
    transport = SdkTransport(CREDS)

    def unauthorized(method: str, url: str, **kwargs: Any) -> Any:
        raise APIError(
            '{"code":40110000,"message":"request is not authorized"}',
            SimpleNamespace(response=SimpleNamespace(status_code=401), request=kwargs),
        )

    monkeypatch.setattr(transport._client._session, "request", unauthorized)
    texts = [repr(transport), repr(CREDS)]
    with pytest.raises(DefiniteRejection) as refused:
        transport.get_account()
    texts += [str(refused.value), repr(refused.value)]
    fake = FakeAlpaca(ReplayClock(T0))
    adapter = make_adapter(fake)
    authority = object()
    adapter.bind_authority(authority)
    adapter.start_stream()
    assert fake.stream_ready.wait(5)
    try:
        boid = _submit(adapter, authority, "co-secret-001")
        fake.set_status(boid, "suspended")
        with pytest.raises(UnmappedStatus) as odd:
            adapter.drain_events()
        texts += [str(odd.value), repr(adapter), *adapter.anomalies]
    finally:
        adapter.stop()
    blob = "\n".join([*texts, caplog.text])
    assert PAPER_KEY not in blob and PAPER_SECRET not in blob


# =========================================================================== stream thread


class _ScriptedSession:
    def __init__(
        self, on_connected: Callable[[], None], message: dict[str, Any], *, fail: bool
    ) -> None:
        self.on_connected = on_connected
        self.message = message
        self.fail = fail
        self.handler: TradeHandler | None = None
        self.ended = threading.Event()
        self.running = threading.Event()

    def subscribe_trade_updates(self, handler: TradeHandler) -> None:
        self.handler = handler

    def run(self) -> None:
        assert self.handler is not None
        self.on_connected()
        asyncio.run(self.handler(self.message))
        if self.fail:
            raise RuntimeError("websocket closed")
        self.running.set()
        self.ended.wait(10)

    def stop(self) -> None:
        self.ended.set()


def test_the_sdk_stream_reports_each_connection_after_authenticating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (re)connection hook relies on alpaca-py 0.44.0's ``TradingStream._start_ws``."""
    from alpaca.trading.stream import TradingStream

    monkeypatch.setattr(transport_module, "_inject_truststore", lambda: None)
    steps: list[str] = []
    for name in ("_connect", "_auth", "_subscribe_trade_updates"):

        async def step(self: Any, _name: str = name) -> None:
            steps.append(_name)

        assert inspect.iscoroutinefunction(getattr(TradingStream, name))
        monkeypatch.setattr(TradingStream, name, step)
    stream = SdkTransport(CREDS).open_trade_stream(lambda: steps.append("connected"))
    asyncio.run(stream._start_ws())  # type: ignore[attr-defined]
    asyncio.run(stream._start_ws())  # type: ignore[attr-defined]
    assert steps == ["_connect", "_auth", "_subscribe_trade_updates", "connected"] * 2


def test_the_trade_stream_reconnects_with_backoff_and_stops() -> None:
    sessions: list[_ScriptedSession] = []

    def open_session(on_connected: Callable[[], None]) -> _ScriptedSession:
        session = _ScriptedSession(on_connected, {"n": len(sessions)}, fail=not sessions)
        sessions.append(session)
        return session

    clock = ReplayClock(T0)
    stream = TradeUpdateStream(
        open_session=open_session, clock=clock, min_backoff_seconds=0.01, max_backoff_seconds=0.05
    )
    stream.start()
    try:
        for _ in range(500):
            if len(sessions) == 2 and sessions[1].running.is_set():
                break
            threading.Event().wait(0.01)
        assert len(sessions) == 2 and sessions[1].running.is_set()
        assert [m for _, m in stream.drain()] == [{"n": 0}, {"n": 1}]
        assert stream.connections == 2 and stream.gaps == (T0,)
        assert stream.running
    finally:
        stream.stop()
    assert not stream.running
    with pytest.raises(RuntimeError):
        stream.start()


# =========================================================================== gateway guard


def _cancel_fails(sqlite_db: Database, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> Any:
    """Run to a partially filled entry, make the broker's cancel fail, request a cancel."""
    h = harness(
        sqlite_db,
        faults=FaultPlan(
            faults=(FaultSpec(kind=FaultKind.PARTIAL_FIRST_FILL, fraction=Decimal("0.4")),)
        ),
    )
    h.run_until(ENTRY_DECISION + timedelta(seconds=40))  # partially filled, rest working
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    before = OrderState(entry.state)
    assert before in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED)

    def broken_cancel(broker_order_id: str) -> Any:
        raise failure

    monkeypatch.setattr(h.env.broker, "cancel", broken_cancel)
    assert h.env.gateway.request_cancel(entry.intent_id) is False  # did not escape
    with h.env.db.tx() as conn:
        state = conn.execute(
            select(order_intents.c.state).where(order_intents.c.intent_id == entry.intent_id)
        ).scalar_one()
        recorded = conn.execute(
            select(alerts.c.code, alerts.c.message).where(alerts.c.code == "CANCEL_OUTCOME_UNKNOWN")
        ).all()
    assert OrderState(state) is before  # still live exposure, nothing assumed
    assert "UNKNOWN_ORDER" in h.flags()
    assert len(recorded) == 1 and entry.client_order_id in recorded[0].message
    return h


@pytest.mark.parametrize(
    "failure",
    [BrokerError("broker down"), ConnectionError("reset"), TimeoutError("read timed out")],
)
def test_a_cancel_transport_failure_is_recorded_and_pauses_entries(
    sqlite_db: Database, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _cancel_fails(sqlite_db, monkeypatch, failure)


def test_a_cancel_timeout_pauses_and_the_run_loop_carries_on(
    sqlite_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _cancel_fails(sqlite_db, monkeypatch, BrokerTimeout("simulated cancel timeout"))
    monkeypatch.undo()
    h.run_to_end()  # reconciliation reads the broker's view; exits still run
    assert h.position() == 0


class _StubAdapter:
    def __init__(self, account_id: str, failure: Exception | None) -> None:
        self.account_id = account_id
        self.failure = failure

    def drain_events(self) -> list[BrokerOrderEvent]:
        if self.failure is not None:
            raise self.failure
        return []


@pytest.mark.parametrize(
    "failure",
    [
        UnmappedStatus("suspended", broker_order_id="b-1", client_order_id="co-odd"),
        MappingError("unparseable order"),
        BrokerTimeout("stream fetch timed out"),
        ConnectionError("reset"),
    ],
)
def test_unreadable_broker_events_are_recorded_and_pause_entries(
    sqlite_db: Database, failure: Exception
) -> None:
    """The OMS pump turns an adapter error (e.g. an unmapped Alpaca status) into a pause."""
    h = harness(sqlite_db)
    h.run_until(ENTRY_DECISION)
    assert "UNKNOWN_ORDER" not in h.flags()
    adapter = _StubAdapter(h.account, failure)
    assert h.env.oms.pump(adapter) == []  # type: ignore[arg-type]
    with h.env.db.tx() as conn:
        recorded = (
            conn.execute(
                select(alerts.c.message).where(alerts.c.code == "BROKER_EVENTS_UNAVAILABLE")
            )
            .scalars()
            .all()
        )
    assert "UNKNOWN_ORDER" in h.flags()
    assert len(recorded) == 1 and type(failure).__name__ in recorded[0]
    adapter.failure = None
    assert h.env.oms.pump(adapter) == []  # type: ignore[arg-type]


def test_a_bug_in_an_adapter_is_not_mistaken_for_a_broker_condition(sqlite_db: Database) -> None:
    h = harness(sqlite_db)
    with pytest.raises(RuntimeError):
        h.env.oms.pump(_StubAdapter(h.account, RuntimeError("bug")))  # type: ignore[arg-type]


# =========================================================================== storage


def test_epoch_microsecond_versions_fit_the_runtime_schema(any_db: Database) -> None:
    """Migration 0002: broker versions are 64-bit (PostgreSQL INTEGER would overflow)."""
    h = harness(any_db)
    h.run_until(ENTRY_DECISION + timedelta(seconds=40))
    entry = next(i for i in h.intents() if i.purpose == "ENTRY")
    version = epoch_micros(ENTRY_DECISION)
    assert version > 2**31
    cum, qty = Decimal(entry.cum_filled_qty), Decimal(entry.quantity)
    status = (
        BrokerOrderStatus.ACCEPTED
        if cum == 0
        else BrokerOrderStatus.PARTIALLY_FILLED
        if cum < qty
        else BrokerOrderStatus.FILLED
    )
    event = BrokerOrderEvent(
        broker="replay",
        account_id=entry.account_id,
        event_id=f"{entry.broker_order_id}:v{version}",
        broker_order_id=entry.broker_order_id,
        client_order_id=entry.client_order_id,
        status=status,
        order_quantity=qty,
        cum_filled_quantity=cum,
        event_time=ENTRY_DECISION + timedelta(seconds=40),
        broker_version=version,
    )
    assert h.env.oms.apply_event(event) == "APPLIED"
    with h.env.db.tx() as conn:
        stored = conn.execute(
            select(order_intents.c.last_broker_version).where(
                order_intents.c.intent_id == entry.intent_id
            )
        ).scalar_one()
        logged = conn.execute(
            select(broker_order_events.c.broker_version).where(
                broker_order_events.c.event_id == event.event_id
            )
        ).scalar_one()
    assert int(stored) == int(logged) == version
    stale = event.model_copy(update={"event_id": "stale", "broker_version": version - 1})
    assert h.env.oms.apply_event(stale) == "STALE"
