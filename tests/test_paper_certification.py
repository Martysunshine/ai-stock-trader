"""Paper certification (``trader paper certify``; domain/certification.py, risk/engine.py,
execution/_gateway.py probe, runtime/certification.py, runtime/certified_profile.py).

No test touches the network or a real broker: the Alpaca adapter runs over the in-memory fake
transport (tests/alpaca_fake.py) on an injected clock and an SQLite ledger. The integrator runs
the real certification with the owner.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from sqlalchemy import func, select
from typer.testing import CliRunner

from tests.alpaca_fake import PAPER_KEY, PAPER_SECRET, FakeAlpaca
from tests.test_risk_engine import FLAG, ctx, score
from trader.brokers.alpaca.adapter import AlpacaPaperAdapter
from trader.brokers.alpaca.mapping import MappingError
from trader.brokers.alpaca.transport import RATE_LIMIT_HEADERS, SdkTransport
from trader.brokers.base import BrokerTimeout, DuplicateClientOrderId, OrderScope
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.cli.main import app
from trader.config import secrets
from trader.config.host import HostRole
from trader.config.policy import LoadedPolicy, load_policy
from trader.config.runtime_config import ResolvedConfig, load_config
from trader.data.sources.alpaca_history import AlpacaCredentials, LatestQuote
from trader.domain.account import RuleProfileKind
from trader.domain.candidates import EdgeEvidence, RejectReason, ReturnBasis, SignalScore
from trader.domain.capabilities import (
    CLIENT_ORDER_ID_UNIQUE,
    FRACTIONAL_LIMIT_DAY,
    FRACTIONAL_MARKET_DAY,
    NATIVE_PROTECTIVE_STOP,
    BrokerCapabilities,
)
from trader.domain.certification import (
    CERTIFICATION_MAX_ORDER_NOTIONAL_USD,
    CERTIFICATION_MAX_ORDERS,
    AssetMetadata,
    CertificationBudget,
    CertificationError,
    CertificationGrant,
    certification_run_id,
)
from trader.domain.instruments import AnySpec, EquitySpec
from trader.domain.market import Quote
from trader.domain.orders import (
    ApprovedPlan,
    OrderIntent,
    OrderPurpose,
    OrderState,
    OrderType,
    TimeInForce,
)
from trader.domain.types import EvidenceLevel, Mode, Side, Track
from trader.reporting.feasibility import capability_matrix_md
from trader.risk.approval import issue_plan
from trader.risk.engine import (
    ACCEPTED_EVIDENCE,
    CERTIFICATION_EVIDENCE,
    CertificationOrderContext,
    evaluate_certification_order,
    evaluate_entry,
)
from trader.risk.loss_limits import window_key_and_start
from trader.risk.portfolio import entries_since
from trader.runtime import paper
from trader.runtime.certification import (
    CERTIFICATION_HOLDER,
    CertificationBroker,
    CertificationPlan,
    CertificationRefused,
    CertificationReport,
    certify_paper_account,
    load_report,
    report_path,
    verified_spec,
    write_report,
)
from trader.runtime.certified_profile import propose_certified_profile
from trader.runtime.paper import DEFAULT_FEED, PaperEnvironment
from trader.storage.db import Database
from trader.storage.schema import alerts, fills, order_intents

ROOT = Path(__file__).resolve().parents[1]
PAPER_YAML = ROOT / "configs" / "paper.yaml"
DOCUMENTED = ROOT / "configs" / "capabilities" / "alpaca_documented.yaml"
ACCOUNT = yaml.safe_load(PAPER_YAML.read_text(encoding="utf-8"))["accounts"][0]["account_id"]
DB_ENV = yaml.safe_load(PAPER_YAML.read_text(encoding="utf-8"))["database"]["url_env"]
AAPL = "XNAS:AAPL"
AAPL_ASSET_ID = "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"
#: Monday 2026-11-09, 11:00 New York: the regular session is open.
T0 = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)
BID, ASK = Decimal("199.99"), Decimal("200.01")
CREDS = AlpacaCredentials(PAPER_KEY, PAPER_SECRET)
cli = CliRunner()


# --------------------------------------------------------------------------- fakes


class FakeClock:
    """``sleep`` advances simulated time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(microseconds=int(seconds * 1_000_000))


class CertFake(FakeAlpaca):
    """The fake paper API plus what a certification run meets: market orders fill at the touch,
    a cancel completes after a few lookups, a clock that can be closed, rate-limit headers."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)  # type: ignore[arg-type]
        self.market_open = True
        self.minutes_to_close = 120
        self.cancel_after_lookups = 2
        self.hidden_lookups = 0
        self.reject_fractional_limits = False
        self._cancelling: dict[str, int] = {}
        self.assets["AAPL"] = {
            "id": AAPL_ASSET_ID,
            "class": "us_equity",
            "exchange": "NASDAQ",
            "symbol": "AAPL",
            "name": "Apple Inc. Common Stock",
            "status": "active",
            "tradable": True,
            "marginable": True,
            "shortable": True,
            "easy_to_borrow": True,
            "fractionable": True,
        }

    def submit_order(self, payload: Mapping[str, str]) -> dict[str, Any]:
        if (
            self.reject_fractional_limits
            and payload["type"] == "limit"
            and Decimal(payload["qty"]) != Decimal(payload["qty"]).to_integral_value()
        ):
            from trader.brokers.alpaca.transport import DefiniteRejection

            self._record("submit_order")
            raise DefiniteRejection(
                http_status=422, code="40010001", message="fractional orders must be market orders"
            )
        response = dict(super().submit_order(payload))
        self.hidden_lookups = 1
        if payload["type"] == "market":
            price = ASK if payload["side"] == "buy" else BID
            self.fill(str(response["id"]), Decimal(payload["qty"]), price, stream=False)
        return response

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        for boid, left in list(self._cancelling.items()):
            if left <= 1:
                del self._cancelling[boid]
                self.confirm_cancel(boid)
            else:
                self._cancelling[boid] = left - 1
        if self.hidden_lookups > 0:  # a just-submitted order is not visible on the first lookup
            self.hidden_lookups -= 1
            self._record("get_order_by_client_id")
            return None
        found = super().get_order_by_client_id(client_order_id)
        return None if found is None else dict(found)

    def cancel_order(self, order_id: str) -> None:
        super().cancel_order(order_id)
        self._cancelling[order_id] = self.cancel_after_lookups

    def get_clock(self) -> dict[str, Any]:
        self._record("get_clock")
        now = self.clock.now()
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        close = now + timedelta(minutes=self.minutes_to_close)
        return {
            "timestamp": stamp,
            "is_open": self.market_open,
            "next_open": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_close": close.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def rate_limit_headers(self) -> dict[str, str]:
        return {"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "187"}


class FakeQuotes:
    feed = DEFAULT_FEED

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def latest_quotes(self, symbols: Sequence[str]) -> dict[str, LatestQuote]:
        return {
            s: LatestQuote(
                symbol=s,
                bid=BID,
                ask=ASK,
                bid_size=Decimal(100),
                ask_size=Decimal(100),
                event_time=self.clock.now(),
            )
            for s in symbols
        }

    def completed_bars(self, *args: object, **kwargs: object) -> dict[str, list[object]]:
        return {}


def _config() -> ResolvedConfig:
    """The real paper config, pinned to the documented base profile: these tests exercise the
    upgrade from it, whichever certified profile the owner has since adopted in paper.yaml."""
    config = load_config(PAPER_YAML, host_role=HostRole.DEV)
    accounts = config.runtime.accounts
    pinned = accounts[0].model_copy(
        update={"capability_profile": DOCUMENTED.relative_to(config.config_dir).as_posix()}
    )
    runtime = config.runtime.model_copy(
        update={"accounts": type(accounts)([pinned, *accounts[1:]])}
    )
    return replace(config, runtime=runtime)


def _caps() -> BrokerCapabilities:
    return load_capabilities(DOCUMENTED)


@pytest.fixture
def lite(tmp_path: Path) -> Iterator[Database]:
    db = Database(f"sqlite:///{(tmp_path / 'paper.db').as_posix()}")
    db.create_schema()
    yield db
    db.dispose()


class Rig:
    """One certification run over the fake paper API and an SQLite ledger."""

    def __init__(self, db: Database, *, start: datetime = T0) -> None:
        self.db = db
        self.clock = FakeClock(start)
        self.fake = CertFake(self.clock)
        self.config = _config()
        self.adapters: list[AlpacaPaperAdapter] = []
        self.env: PaperEnvironment | None = None

    def make_adapter(
        self, specs: Mapping[str, AnySpec] | None, budget: CertificationBudget | None
    ) -> CertificationBroker:
        acct = self.config.runtime.accounts[0]
        adapter = AlpacaPaperAdapter(
            account_id=acct.account_id,
            credentials=CREDS,
            capabilities=_caps(),
            instruments=dict(specs)
            if specs is not None
            else load_instruments(self.config.resolve(acct.instruments)),
            clock=self.clock,
            mode=Mode.PAPER,
            since=T0 - timedelta(hours=1),
            transport=self.fake,
            sleep=lambda _s: None,
            certification=budget,
        )
        self.adapters.append(adapter)
        return adapter

    def make_environment(
        self, broker: CertificationBroker, specs: Mapping[str, AnySpec]
    ) -> PaperEnvironment:
        acct = self.config.runtime.accounts[0]
        self.env = paper._assemble(
            self.config,
            acct,
            capabilities=_caps(),
            clock=self.clock,
            adapter=broker,
            database=self.db,
            edge=None,
            holder=CERTIFICATION_HOLDER,
            market_feed=DEFAULT_FEED,
            specs=specs,
        )
        return self.env

    def run(self, plan: CertificationPlan | None = None) -> CertificationReport:
        return certify_paper_account(
            self.config,
            clock=self.clock,
            market_data=FakeQuotes(self.clock),
            make_adapter=self.make_adapter,
            make_environment=self.make_environment,
            plan=plan or CertificationPlan(),
            use_stream=False,
        )

    def submissions(self) -> int:
        return self.fake.calls.count("submit_order")


@pytest.fixture
def completed(tmp_path: Path) -> CertificationReport:
    db = Database(f"sqlite:///{(tmp_path / 'done.db').as_posix()}")
    db.create_schema()
    try:
        return Rig(db).run()
    finally:
        db.dispose()


# --------------------------------------------------------------------------- the grant


def _grant(**changes: Any) -> CertificationGrant:
    fields: dict[str, Any] = {
        "run_id": certification_run_id(T0),
        "account_id": ACCOUNT,
        "mode": Mode.PAPER,
        "issued_at": T0,
        "expires_at": T0 + timedelta(minutes=20),
    }
    fields.update(changes)
    return CertificationGrant(**fields)


@pytest.mark.parametrize("mode", [Mode.REPLAY, Mode.SHADOW, Mode.LIVE])
def test_a_grant_exists_only_in_paper_mode(mode: Mode) -> None:
    with pytest.raises(CertificationError, match="only in paper mode"):
        _grant(mode=mode)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_order_notional_usd": CERTIFICATION_MAX_ORDER_NOTIONAL_USD + Decimal("0.01")},
        {"max_order_notional_usd": Decimal(0)},
        {"max_orders": CERTIFICATION_MAX_ORDERS + 1},
        {"max_orders": 0},
        {"expires_at": T0 + timedelta(hours=2)},
        {"expires_at": T0},
        {"capabilities_under_test": frozenset({NATIVE_PROTECTIVE_STOP})},
        {"run_id": "anything-else"},
    ],
)
def test_a_grant_can_lower_but_never_raise_the_hard_limits(changes: dict[str, Any]) -> None:
    with pytest.raises(CertificationError):
        _grant(**changes)


def test_the_budget_refuses_beyond_the_grant() -> None:
    budget = CertificationBudget(_grant(max_orders=2))
    assert budget.consume("a") and budget.consume("b")
    assert not budget.consume("c")
    assert budget.used == 2 and budget.remaining == 0


# --------------------------------------------------------------------------- risk engine


def _paper_policy() -> LoadedPolicy:
    return load_policy(ROOT / "configs" / "policy" / "base.yaml", mode=Mode.PAPER)


def _spec(**changes: Any) -> EquitySpec:
    spec = load_instruments(ROOT / "configs" / "instruments" / "stocks_research.yaml")[AAPL]
    asset = AssetMetadata(
        instrument_id=AAPL,
        symbol="AAPL",
        broker_asset_id=AAPL_ASSET_ID,
        asset_class="us_equity",
        exchange="NASDAQ",
        status="active",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=True,
    )
    verified = verified_spec(spec, asset, fetched_at=T0, run_id=certification_run_id(T0))
    return verified.model_copy(update=changes)


def _quote(age_s: int = 0, bid: Decimal = BID, ask: Decimal = ASK) -> Quote:
    t = T0 - timedelta(seconds=age_s)
    return Quote(
        instrument_id=AAPL,
        feed=DEFAULT_FEED,
        event_time=t,
        receipt_time=t,
        sequence=1,
        bid=bid,
        ask=ask,
        bid_size=Decimal(100),
        ask_size=Decimal(100),
    )


def _cctx(**changes: Any) -> CertificationOrderContext:
    base = CertificationOrderContext(
        mode=Mode.PAPER,
        now=T0,
        policy=_paper_policy(),
        grant=_grant(),
        evidence=EdgeEvidence.CERTIFICATION,
        spec=_spec(),
        capabilities=_caps(),
        rule_profile=RuleProfileKind.UNVERIFIED,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.028573469"),
        limit_price=Decimal("139.99"),
        quote=_quote(),
        fx_to_usd=Decimal(1),
        market_open=True,
        active_flags=(),
        unknown_orders_outstanding=False,
        instrument_busy=False,
        pending_entries=0,
        orders_used=0,
        account_spendable_usd=Decimal(50),
    )
    return replace(base, **changes)


def test_a_paper_certification_order_within_the_cap_is_approved() -> None:
    limit = evaluate_certification_order(_cctx())
    assert limit.approved, limit.details
    assert limit.capability == FRACTIONAL_LIMIT_DAY
    assert limit.notional_bound_usd == Decimal("0.028573469") * Decimal("139.99")
    market = evaluate_certification_order(
        _cctx(order_type=OrderType.MARKET, limit_price=None, quantity=Decimal("0.0075"))
    )
    assert market.approved, market.details
    assert market.capability == FRACTIONAL_MARKET_DAY
    assert market.notional_bound_usd == Decimal("0.0075") * ASK * Decimal("1.05")


@pytest.mark.parametrize("mode", [Mode.REPLAY, Mode.SHADOW, Mode.LIVE])
def test_certification_evidence_is_refused_outside_paper(mode: Mode) -> None:
    policy = load_policy(ROOT / "configs" / "policy" / "base.yaml", mode=mode)
    decision = evaluate_certification_order(_cctx(mode=mode, policy=policy))
    assert not decision.approved
    assert RejectReason.MODE_NOT_PERMITTED in decision.reasons
    assert CERTIFICATION_EVIDENCE[mode] == frozenset()


def test_ordinary_paper_runs_never_accept_certification_evidence() -> None:
    """No grant (an ordinary paper run) or another evidence: refused; and the ordinary entry
    path accepts CERTIFICATION in no mode."""
    assert not evaluate_certification_order(_cctx(grant=None)).approved
    other = evaluate_certification_order(_cctx(evidence=EdgeEvidence.VALIDATED_BUNDLE))
    assert RejectReason.MODE_NOT_PERMITTED in other.reasons
    expired = evaluate_certification_order(_cctx(now=T0 + timedelta(minutes=21)))
    assert "the certification grant has expired" in expired.details
    assert all(EdgeEvidence.CERTIFICATION not in ok for ok in ACCEPTED_EVIDENCE.values())


@pytest.mark.parametrize("mode", list(Mode))
def test_the_ordinary_entry_path_rejects_certification_evidence(mode: Mode) -> None:
    entry_policy = load_policy(ROOT / "configs" / "policy" / "base.yaml", mode=mode)
    decision = evaluate_entry(
        ctx(entry_policy, mode=mode, score=score(evidence=EdgeEvidence.CERTIFICATION))
    )
    assert not decision.approved
    assert RejectReason.NO_EDGE in decision.reasons
    assert any("CERTIFICATION evidence is not accepted" in d for d in decision.details)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        # the hard USD 5 cap: limit notional, market bound (ask x 1.05) and the grant's own cap
        ({"quantity": Decimal("0.036")}, RejectReason.RISK_LIMIT),
        (
            {"order_type": OrderType.MARKET, "limit_price": None, "quantity": Decimal("0.0239")},
            RejectReason.RISK_LIMIT,
        ),
        ({"grant": _grant(max_order_notional_usd=Decimal("3"))}, RejectReason.RISK_LIMIT),
        ({"orders_used": CERTIFICATION_MAX_ORDERS}, RejectReason.ENTRY_LIMIT_REACHED),
        ({"limit_price": Decimal("185.00"), "quantity": Decimal("0.02")}, RejectReason.RISK_LIMIT),
        ({"market_open": False}, RejectReason.OUTSIDE_ENTRY_WINDOW),
        ({"quote": _quote(age_s=5)}, RejectReason.STALE_DATA),
        ({"quote": None}, RejectReason.STALE_DATA),
        ({"active_flags": (FLAG,)}, RejectReason.PAUSED),
        ({"instrument_busy": True}, RejectReason.RISK_LIMIT),
        ({"side": Side.SELL}, RejectReason.FORBIDDEN_PRODUCT),
        ({"spec": _spec(evidence=EvidenceLevel.UNKNOWN)}, RejectReason.CAPABILITY),
        (
            {"grant": _grant(capabilities_under_test=frozenset({FRACTIONAL_MARKET_DAY}))},
            RejectReason.CAPABILITY,
        ),
        ({"quantity": Decimal("0.0000000001")}, RejectReason.SIZE_BELOW_MINIMUM),
        ({"account_spendable_usd": Decimal("1")}, RejectReason.INSUFFICIENT_FUNDS),
        (
            {
                "order_type": OrderType.MARKET,
                "limit_price": None,
                "quantity": Decimal("0.0075"),
                "quote": _quote(bid=Decimal("198"), ask=Decimal("200.01")),
            },
            RejectReason.SPREAD_TOO_WIDE,
        ),
    ],
)
def test_certification_orders_fail_closed(changes: dict[str, Any], reason: RejectReason) -> None:
    decision = evaluate_certification_order(_cctx(**changes))
    assert not decision.approved
    assert reason in decision.reasons, decision.details


def test_the_cap_is_a_hard_constant() -> None:
    assert Decimal("5") == CERTIFICATION_MAX_ORDER_NOTIONAL_USD
    assert CERTIFICATION_MAX_ORDERS == 6
    with pytest.raises(CertificationRefused):
        CertificationPlan(passive_notional_usd=Decimal("5.01"))
    with pytest.raises(CertificationRefused):
        CertificationPlan(market_notional_usd=Decimal("2.50"))
    with pytest.raises(CertificationRefused):
        CertificationPlan(passive_limit_fraction=Decimal("0.95"))


# --------------------------------------------------------------------------- the adapter


def _plan(
    clock: FakeClock,
    cid: str,
    *,
    order_type: OrderType = OrderType.LIMIT,
    quantity: Decimal = Decimal("0.02"),
    limit: Decimal | None = Decimal("139.99"),
    model_hash: str | None = None,
    reserved: Decimal = Decimal("2.80"),
) -> ApprovedPlan:
    now = clock.now()
    intent = OrderIntent(
        intent_id=f"i-{cid}",
        client_order_id=cid,
        portfolio_id="pf-main",
        account_id=ACCOUNT,
        instrument_id=AAPL,
        track=Track.STOCKS,
        side=Side.BUY,
        purpose=OrderPurpose.ENTRY,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit if order_type is OrderType.LIMIT else None,
        time_in_force=TimeInForce.DAY,
        reason="certification adapter test",
        created_at=now,
    )
    return issue_plan(
        intent=intent,
        decision_id=f"d-{cid}",
        account_version=0,
        policy_hash="policy",
        model_hash=model_hash or _grant().model_hash,
        quote_time=now,
        approved_at=now,
        valid_until=now + timedelta(seconds=10),
        reserved_cash_usd=reserved,
        reserved_risk_usd=reserved,
        exit_method="test",
    )


def _adapter(fake: CertFake, budget: CertificationBudget | None) -> AlpacaPaperAdapter:
    return AlpacaPaperAdapter(
        account_id=ACCOUNT,
        credentials=CREDS,
        capabilities=_caps(),
        instruments={AAPL: _spec()},
        clock=fake.clock,
        mode=Mode.PAPER,
        since=T0 - timedelta(hours=1),
        transport=fake,
        sleep=lambda _s: None,
        certification=budget,
    )


def test_without_a_budget_the_adapter_sends_no_certification_shape() -> None:
    fake = CertFake(FakeClock(T0))
    adapter = _adapter(fake, None)
    authority = object()
    adapter.bind_authority(authority)
    limit = adapter.submit(_plan(fake.clock, "co-cert-limit"), authority=authority)
    market = adapter.submit(
        _plan(
            fake.clock, "co-cert-market", order_type=OrderType.MARKET, quantity=Decimal("0.0075")
        ),
        authority=authority,
    )
    assert not limit.accepted and "fractional.limit.day is not usable" in str(limit.reject_reason)
    assert not market.accepted and market.reject_reason == "entries are LIMIT DAY buys"
    assert "submit_order" not in fake.calls
    assert adapter.rejections["co-cert-limit"] == limit.reject_reason


def test_a_budgeted_adapter_sends_only_this_runs_plans_within_cap_and_budget() -> None:
    fake = CertFake(FakeClock(T0))
    budget = CertificationBudget(_grant(max_orders=2))
    adapter = _adapter(fake, budget)
    authority = object()
    adapter.bind_authority(authority)
    foreign = adapter.submit(
        _plan(fake.clock, "co-foreign", model_hash="fixture-model"), authority=authority
    )
    assert not foreign.accepted and "not usable" in str(foreign.reject_reason)
    over_cap = adapter.submit(
        _plan(fake.clock, "co-over", reserved=Decimal("5.01")), authority=authority
    )
    assert not over_cap.accepted and "exceeds the cap" in str(over_cap.reject_reason)
    big_limit = adapter.submit(
        _plan(fake.clock, "co-big", quantity=Decimal("0.04")), authority=authority
    )
    assert not big_limit.accepted and "exceeds the cap" in str(big_limit.reject_reason)
    assert "submit_order" not in fake.calls
    assert adapter.submit(_plan(fake.clock, "co-1"), authority=authority).accepted
    market = _plan(fake.clock, "co-2", order_type=OrderType.MARKET, quantity=Decimal("0.0075"))
    assert adapter.submit(market, authority=authority).accepted
    third = adapter.submit(_plan(fake.clock, "co-3"), authority=authority)
    assert not third.accepted and "budget exhausted" in str(third.reject_reason)
    assert fake.calls.count("submit_order") == 2 and budget.used == 2


def test_an_expired_grant_sends_nothing() -> None:
    clock = FakeClock(T0)
    fake = CertFake(clock)
    budget = CertificationBudget(_grant(expires_at=T0 + timedelta(seconds=5)))
    adapter = _adapter(fake, budget)
    authority = object()
    adapter.bind_authority(authority)
    clock.sleep(6)
    result = adapter.submit(_plan(clock, "co-late"), authority=authority)
    assert not result.accepted and "expired" in str(result.reject_reason)
    assert "submit_order" not in fake.calls


def test_a_grant_for_another_account_is_refused() -> None:
    fake = CertFake(FakeClock(T0))
    with pytest.raises(ValueError, match="another account"):
        _adapter(fake, CertificationBudget(_grant(account_id="acct-other")))


def test_a_repeated_client_order_id_is_a_duplicate_unknown_outcome() -> None:
    fake = CertFake(FakeClock(T0))
    budget = CertificationBudget(_grant())
    first, second = _adapter(fake, budget), _adapter(fake, budget)
    a1, a2 = object(), object()
    first.bind_authority(a1)
    second.bind_authority(a2)
    plan = _plan(fake.clock, "co-dup")
    assert first.submit(plan, authority=a1).accepted
    with pytest.raises(DuplicateClientOrderId, match="already holds") as raised:
        second.submit(plan, authority=a2)
    assert isinstance(raised.value, BrokerTimeout)  # the gateway still treats it as unknown
    assert "HTTP 422" in raised.value.detail


def test_asset_metadata_and_rate_limit_headers() -> None:
    fake = CertFake(FakeClock(T0))
    adapter = _adapter(fake, None)
    asset = adapter.asset_metadata(AAPL)
    assert asset.broker_asset_id == AAPL_ASSET_ID and asset.fractionable
    assert asset.certification_problems() == []
    assert adapter.rate_limit_observation()["X-RateLimit-Limit"] == "200"
    fake.assets["AAPL"]["fractionable"] = False
    assert "AAPL is not fractionable" in adapter.asset_metadata(AAPL).certification_problems()
    fake.assets["AAPL"]["tradable"] = "yes"
    with pytest.raises(MappingError, match="not a boolean"):
        adapter.asset_metadata(AAPL)


def test_the_sdk_transport_keeps_only_rate_limit_headers() -> None:
    transport = SdkTransport(CREDS)

    class Response:
        headers: ClassVar[dict[str, str]] = {
            "X-RateLimit-Limit": "200",
            "X-RateLimit-Remaining": "199",
            "X-RateLimit-Reset": "1762700000",
            "Set-Cookie": "session=secret",
            "Authorization": "Bearer secret",
        }

    hooks = transport._client._session.hooks["response"]
    assert transport._observe_headers in hooks
    hooks[-1](Response())
    seen = transport.rate_limit_headers()
    assert set(seen) == set(RATE_LIMIT_HEADERS)
    assert "secret" not in json.dumps(seen)


# --------------------------------------------------------------------------- the run


def test_a_certification_run_demonstrates_each_unknown_capability(lite: Database) -> None:
    rig = Rig(lite)
    report = rig.run()
    assert report.status == "COMPLETED", report.lines()
    assert [s.name for s in report.steps] == [
        "fractional_limit_day",
        "client_order_id_unique",
        "cancel",
        "fractional_market_day",
    ]
    assert all(s.status == "PASSED" for s in report.steps)
    # bounded: four broker submissions, every order within USD 5
    assert report.broker_submissions == 4 == rig.submissions()
    assert all(o.notional_bound_usd <= Decimal(5) for o in report.orders)
    limit, buy, sell = report.orders
    assert (limit.order_type, buy.order_type, sell.order_type) == ("LIMIT", "MARKET", "MARKET")
    assert limit.limit_price == Decimal("139.99") and limit.final_state == "CANCELED"
    assert Decimal(1) <= buy.quantity * ASK <= Decimal(2)
    assert buy.final_state == sell.final_state == "FILLED"
    assert sum(f.quantity for f in buy.fills) == buy.quantity == sell.quantity
    # Alpaca reports no fee while it is pending; the paper ledger accrues what the account's
    # schedule charges for each order (ADR-0050): CAT on the buy; SEC, TAF and CAT on the sell
    assert all(f.fee_status == "PENDING" for f in buy.fills + sell.fills)
    assert sum(f.fee for f in buy.fills) == Decimal("0.01")
    assert sum(f.fee for f in sell.fills) == Decimal("0.03")
    # client order ID uniqueness: Alpaca refused the repeat; one order, never filled
    assert report.duplicate is not None
    assert report.duplicate.outcome == "REJECTED_DUPLICATE"
    assert report.duplicate.broker_orders_with_client_id == 1
    assert report.duplicate.filled_quantity == 0
    # visibility: each order hidden on the first lookup, visible on the second
    assert [(v.visible, v.lookups) for v in report.visibility] == [(True, 2)] * 3
    # cancel: requested, then terminal after the fake's two lookups
    assert report.cancel is not None and report.cancel.request_accepted
    assert report.cancel.final_status == "CANCELED"
    assert report.cancel.statuses_seen[-1] == "CANCELED"
    # cash, reconciliation, rate limit, flat
    assert report.cash is not None
    assert report.cash.observed_buy_fees_usd == 0 == report.cash.observed_sell_fees_usd
    assert {r.label: r.status for r in report.reconciliations} == {
        "startup": "PASSED",
        "after-cancel": "PASSED",
        "after-round-trip": "PASSED",
        "final": "PASSED",
    }
    assert report.rate_limit_headers["X-RateLimit-Limit"] == "200"
    assert report.pending_liquidation == {}
    assert report.asset is not None and report.asset.broker_asset_id == AAPL_ASSET_ID
    assert report.market_clock is not None and report.market_clock.is_open
    assert report.report_hash == report.body_hash()
    # everything went through approval and the gateway: intents, plans and ledger fills
    with lite.tx() as conn:
        intents = conn.execute(select(order_intents).order_by(order_intents.c.seq)).all()
        posted = conn.execute(select(fills)).all()
        probe = conn.execute(
            select(alerts.c.message).where(alerts.c.code == "CERTIFICATION_DUPLICATE_PROBE")
        ).scalars()
        probe_messages = list(probe)
    assert [(i.purpose, i.order_type, i.state) for i in intents] == [
        ("ENTRY", "LIMIT", "CANCELED"),
        ("ENTRY", "MARKET", "FILLED"),
        ("EXIT_RISK_REDUCTION", "MARKET", "FILLED"),
    ]
    plans = [ApprovedPlan.model_validate_json(i.plan_json) for i in intents]
    assert all(p.is_authentic() for p in plans)
    assert plans[0].model_hash == plans[1].model_hash == f"certification:{report.run_id}"
    assert len(posted) == 2
    assert len(probe_messages) == 1 and "REJECTED_DUPLICATE" in probe_messages[0]


def test_the_report_is_written_without_secrets_and_verifies(
    completed: CertificationReport, tmp_path: Path
) -> None:
    path = write_report(completed, tmp_path / "runs")
    assert path == report_path(tmp_path / "runs", completed.run_id)
    assert path.name == "20261109T160000Z.json"
    text = path.read_text(encoding="utf-8")
    assert PAPER_KEY not in text and PAPER_SECRET not in text
    assert load_report(path) == completed
    with pytest.raises(CertificationRefused, match="never overwritten"):
        write_report(completed, tmp_path / "runs")
    tampered = json.loads(text)
    tampered["broker_submissions"] = 1
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(CertificationRefused, match="altered"):
        load_report(path)


def test_a_closed_market_refuses_before_any_order(lite: Database) -> None:
    rig = Rig(lite)
    rig.fake.market_open = False
    report = rig.run()
    assert report.status == "REFUSED"
    assert report.refusal is not None and "market is closed" in report.refusal
    assert rig.submissions() == 0 and report.broker_submissions == 0
    assert report.steps == () and rig.env is None  # refused before the environment was built


def test_too_close_to_the_close_refuses(lite: Database) -> None:
    rig = Rig(lite)
    rig.fake.minutes_to_close = 10
    report = rig.run()
    assert report.status == "REFUSED" and "to the close" in str(report.refusal)
    assert rig.submissions() == 0


def test_a_held_position_in_the_symbol_refuses_before_any_order(lite: Database) -> None:
    rig = Rig(lite)
    rig.fake.set_position("AAPL", Decimal("0.5"), Decimal("190"))
    report = rig.run()
    assert report.status == "REFUSED"
    assert rig.submissions() == 0


def test_a_non_fractionable_asset_refuses(lite: Database) -> None:
    rig = Rig(lite)
    rig.fake.assets["AAPL"]["fractionable"] = False
    report = rig.run()
    assert report.status == "REFUSED" and "not fractionable" in str(report.refusal)
    assert rig.submissions() == 0


def test_a_broker_refusing_fractional_limits_is_recorded_and_the_run_goes_on(
    lite: Database,
) -> None:
    rig = Rig(lite)
    rig.fake.reject_fractional_limits = True
    report = rig.run()
    assert report.status == "INCOMPLETE"
    statuses = {s.name: s.status for s in report.steps}
    assert statuses == {
        "fractional_limit_day": "FAILED",
        "client_order_id_unique": "SKIPPED",
        "cancel": "SKIPPED",
        "fractional_market_day": "PASSED",
    }
    limit = report.orders[0]
    assert limit.refused_by == "broker" and "market orders" in str(limit.refusal)
    proposal = propose_certified_profile(report, _caps(), report_ref="r.json")
    # a refusal is never attributed: the capability stays UNKNOWN and the reviewer gets a note
    assert proposal.profile.capability(FRACTIONAL_LIMIT_DAY) == _caps().capability(
        FRACTIONAL_LIMIT_DAY
    )
    assert any("market orders" in note for note in proposal.notes)
    assert proposal.profile.capability(CLIENT_ORDER_ID_UNIQUE).supported is None  # not shown
    assert proposal.profile.capability(FRACTIONAL_MARKET_DAY).evidence is (
        EvidenceLevel.PAPER_DEMONSTRATED
    )


def test_a_second_worker_holding_the_account_refuses(
    lite: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.ops.leadership import LeadershipManager, LeadershipUnavailable

    class HeldElsewhere(LeadershipManager):
        def acquire(self) -> int:
            raise LeadershipUnavailable(f"another worker leads account {self.account_id}")

    monkeypatch.setattr(paper, "LeadershipManager", HeldElsewhere)
    rig = Rig(lite)
    report = rig.run()
    assert report.status == "REFUSED" and "another worker leads" in str(report.refusal)
    assert rig.submissions() == 0
    with lite.tx() as conn:
        assert rig.env is not None and rig.env.pauses.all_active(conn) == []


# --------------------------------------------------------------------------- the gateway probe


def test_the_duplicate_probe_refuses_anything_but_a_resting_certification_limit(
    lite: Database,
) -> None:
    rig = Rig(lite)
    report = rig.run()
    env = rig.env
    assert env is not None and report.status == "COMPLETED"
    with lite.tx() as conn:
        rows = conn.execute(select(order_intents).order_by(order_intents.c.seq)).all()
    # the run's grant has not expired yet, but each intent is terminal or not a limit buy
    grant = _grant(run_id=report.run_id, issued_at=rig.clock.now() - timedelta(minutes=1))
    env.leadership[ACCOUNT].acquire()
    try:
        for row in rows:
            probe = env.gateway.probe_duplicate_submission(
                row.intent_id, probe=rig.adapters[-1], grant=grant
            )
            assert probe.outcome == "NOT_RUN"
    finally:
        env.leadership[ACCOUNT].release()
    with pytest.raises(CertificationError):
        env.gateway.probe_duplicate_submission(
            rows[0].intent_id,
            probe=rig.adapters[-1],
            grant=_grant(run_id=report.run_id, account_id="acct-other"),
        )
    assert rig.submissions() == 4


# --------------------------------------------------------------------------- the generator


def test_the_proposed_profile_upgrades_only_what_was_demonstrated(
    completed: CertificationReport, tmp_path: Path
) -> None:
    base = _caps()
    proposal = propose_certified_profile(completed, base, report_ref="runs/x.json")
    profile = proposal.profile
    for name in (FRACTIONAL_LIMIT_DAY, FRACTIONAL_MARKET_DAY, CLIENT_ORDER_ID_UNIQUE):
        cap = profile.capability(name)
        assert cap.supported is True and cap.evidence is EvidenceLevel.PAPER_DEMONSTRATED
        assert completed.run_id in cap.source
    # visibility: two lookups observed -> 4 lookups, window at least 30 s
    assert (profile.order_visibility_lookups, profile.order_visibility_window_seconds) == (4, 30)
    assert profile.rate_limit_per_minute == 200
    # everything else is untouched: UNKNOWN stays UNKNOWN, never live, still paper
    untouched = set(base.capabilities) - {
        FRACTIONAL_LIMIT_DAY,
        FRACTIONAL_MARKET_DAY,
        CLIENT_ORDER_ID_UNIQUE,
    }
    assert untouched and all(profile.capabilities[n] == base.capabilities[n] for n in untouched)
    assert profile.capability(NATIVE_PROTECTIVE_STOP).supported is None
    assert profile.environment == "paper" and profile.rule_profile is base.rule_profile
    assert not any(
        c.evidence is EvidenceLevel.LIVE_CONFIRMED for c in profile.capabilities.values()
    )
    assert profile.profile_version == "alpaca-paper-certified-2026-11-09"
    # the YAML loads exactly as the runtime loads profiles, and the matrix tells them apart
    out = tmp_path / "caps" / "alpaca_paper_certified.yaml"
    out.parent.mkdir()
    out.write_text(
        proposal.yaml_text(
            report_ref="runs/x.json", base_ref="documented", report_hash=completed.report_hash
        ),
        encoding="utf-8",
    )
    assert load_capabilities(out) == profile
    (tmp_path / "caps" / "alpaca_documented.yaml").write_text(
        DOCUMENTED.read_text(encoding="utf-8"), encoding="utf-8"
    )
    matrix = capability_matrix_md(tmp_path / "caps")
    assert "alpaca (paper, alpaca-paper-certified-2026-11-09)" in matrix
    assert "alpaca (paper, alpaca-documented-2026-11-07)" in matrix


def test_the_generator_refuses_unusable_reports(completed: CertificationReport) -> None:
    base = _caps()
    with pytest.raises(CertificationRefused, match="altered"):
        propose_certified_profile(
            completed.model_copy(update={"broker_submissions": 1}), base, report_ref="r"
        )
    other_base = base.model_copy(update={"profile_version": "alpaca-other"})
    with pytest.raises(CertificationRefused, match="pass the base profile"):
        propose_certified_profile(completed, other_base, report_ref="r")
    refused = _rehash(completed, status="REFUSED", broker_submissions=0)
    with pytest.raises(CertificationRefused, match="sent no orders"):
        propose_certified_profile(refused, base, report_ref="r")


def test_failed_steps_upgrade_nothing(completed: CertificationReport) -> None:
    failed = _rehash(
        completed,
        steps=tuple(s.model_copy(update={"status": "FAILED"}) for s in completed.steps),
        visibility=completed.visibility[:2],
        rate_limit_headers={},
        flat_after_round_trip=False,
    )
    with pytest.raises(CertificationRefused, match="demonstrates nothing"):
        propose_certified_profile(failed, _caps(), report_ref="r")


def _rehash(report: CertificationReport, **changes: Any) -> CertificationReport:
    body = report.model_copy(update=changes)
    return body.model_copy(update={"report_hash": body.body_hash()})


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def no_paper_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv(DB_ENV, raising=False)
    monkeypatch.delenv(secrets.KEY_ENV, raising=False)
    monkeypatch.delenv(secrets.SECRET_ENV, raising=False)
    monkeypatch.setenv("TRADER_HOST_ROLE", "DEV")
    monkeypatch.setattr(secrets, "_get", lambda service, user: None)
    return monkeypatch


def test_certify_prints_the_plan_and_sends_nothing_without_yes(
    no_paper_env: pytest.MonkeyPatch,
) -> None:
    result = cli.invoke(app, ["paper", "certify"])
    assert result.exit_code == 0, result.output
    assert "at most 6 broker submissions; at most USD 5 notional per order" in result.output
    assert "LIMIT DAY buy, fractional, ~USD 4.00 at 70% of the bid" in result.output
    assert "MARKET DAY buy, fractional, ~USD 1.50" in result.output
    assert "nothing was sent: re-run with --yes" in result.output


def test_certify_refuses_without_prerequisites(no_paper_env: pytest.MonkeyPatch) -> None:
    replay = cli.invoke(
        app, ["paper", "certify", "--yes", "--config", str(ROOT / "configs/replay.yaml")]
    )
    assert replay.exit_code == 2 and "no fallback" in replay.output
    no_db = cli.invoke(app, ["paper", "certify", "--yes"])
    assert no_db.exit_code == 2 and DB_ENV in no_db.output
    unknown = cli.invoke(app, ["paper", "certify", "--symbol", "ZZZZZZZZZZZZ"])
    assert unknown.exit_code == 2 and "invalid certification plan" in unknown.output


def test_certify_profile_writes_a_loadable_proposal_once(
    completed: CertificationReport, tmp_path: Path
) -> None:
    path = write_report(completed, tmp_path / "runs")
    out = tmp_path / "alpaca_paper_certified.yaml"
    args = ["paper", "certify-profile", "--report", str(path), "--out", str(out)]
    first = cli.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "fractional.limit.day: UNKNOWN -> yes (paper_demonstrated)" in first.output
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# PROPOSED Alpaca paper capability profile")
    assert completed.report_hash in text
    assert load_capabilities(out).capability(CLIENT_ORDER_ID_UNIQUE).supported is True
    again = cli.invoke(app, args)
    assert again.exit_code == 2 and "--force" in again.output
    assert cli.invoke(app, [*args, "--force"]).exit_code == 0


def test_signal_scores_cannot_claim_certification_by_accident() -> None:
    """A score object may carry CERTIFICATION, but nothing in the ordinary path accepts it."""
    s = SignalScore(
        candidate_id="c",
        model_hash="m",
        expected_gross_return=Decimal("0.01"),
        uncertainty_margin=Decimal(0),
        basis=ReturnBasis.EXECUTABLE,
        evidence=EdgeEvidence.CERTIFICATION,
        edge_source_hash="e",
    )
    assert s.evidence not in ACCEPTED_EVIDENCE[Mode.PAPER]


def test_the_order_states_used_by_the_report_exist() -> None:
    assert {OrderState.ACKNOWLEDGED, OrderState.CANCELED, OrderState.FILLED} <= set(OrderState)
    assert OrderScope.ALL.value == "ALL"


def test_certification_orders_do_not_use_the_strategy_entry_allowance(lite: Database) -> None:
    """Regression 2026-11-09: certification buys used up max_new_entries_per_session (ADR-0043)."""
    rig = Rig(lite)
    assert rig.run().status == "COMPLETED"
    _, start = window_key_and_start("NY_SESSION", rig.clock.now())
    with lite.tx() as conn:
        sent = conn.execute(
            select(func.count())
            .select_from(order_intents)
            .where(order_intents.c.purpose == "ENTRY")
        ).scalar_one()
        assert sent == 2  # the resting limit buy and the market buy were really submitted
        assert entries_since(conn, [rig.config.runtime.accounts[0].account_id], start) == 0
