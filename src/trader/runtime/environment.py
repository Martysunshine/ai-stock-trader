"""Wiring for a replay environment: the same risk, OMS and accounting code paper mode uses.

Only the clock, event source, broker adapter and declared capability profile are replay-specific
(architecture.md §1). Re-building an environment over the same database (and the same simulated
broker, which stands in for the remote venue) models a worker restart.

:func:`account_runtime`, :func:`wire_execution` and :func:`bootstrap` are shared with the paper
environment (``runtime/paper.py``), so both modes run identical risk, OMS and accounting objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy import insert, select

from trader.accounting.fees import FeeSchedule, load_fee_schedule
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.brokers.base import BrokerAdapter
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.brokers.replay.broker import ReplayBroker
from trader.brokers.replay.faults import FaultPlan
from trader.config.runtime_config import AccountConfig, ResolvedConfig
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.market_view import MarketView
from trader.data.recorded_session import RecordedSession
from trader.domain.account import CashFlowKind, LegalAccountType, RuleProfileKind
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.hashing import canonical_json, content_hash
from trader.domain.instruments import AnySpec
from trader.domain.types import Mode
from trader.execution._gateway import SubmissionGateway
from trader.execution.exits import ExitManager
from trader.execution.oms import OrderManager
from trader.execution.reconciliation import Reconciler
from trader.ops.clock import Clock, ReplayClock
from trader.ops.leadership import LeadershipManager
from trader.ops.pause import PauseService
from trader.risk.allocator import PortfolioAllocator
from trader.risk.loss_limits import LossMonitor
from trader.risk.portfolio import AccountRuntime
from trader.storage.db import Database
from trader.storage.helpers import insert_ignore
from trader.storage.schema import (
    accounts,
    capability_profiles,
    instruments,
    policy_versions,
    portfolios,
)
from trader.strategies.candidate_a import CandidateA, load_candidate_a_spec
from trader.strategies.edge import EdgeEstimator, FixtureEdgeEstimator, NoEdgeEstimator

REPLAY_BROKER_STARTING_CASH = Decimal("100000")


@dataclass
class ReplayEnvironment:
    config: ResolvedConfig
    run_id: str
    db: Database
    clock: ReplayClock
    session: RecordedSession
    ledger: Ledger
    pauses: PauseService
    loss_monitor: LossMonitor
    accounts: dict[str, AccountRuntime]
    leadership: dict[str, LeadershipManager]
    broker: ReplayBroker
    oms: OrderManager
    gateway: SubmissionGateway
    reconciler: Reconciler
    allocator: PortfolioAllocator
    exits: ExitManager
    market: MarketView
    strategy: CandidateA
    edge: EdgeEstimator
    fx: FxBook
    holder: str
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def portfolio_id(self) -> str:
        return self.config.runtime.portfolio_id

    def close(self) -> None:
        for lm in self.leadership.values():
            lm.release()
        self.db.dispose()


def account_runtime(
    config: ResolvedConfig,
    acct_cfg: AccountConfig,
    *,
    capabilities: BrokerCapabilities,
    fees: FeeSchedule,
    specs: dict[str, AnySpec],
) -> AccountRuntime:
    """The account as the risk engine sees it: capabilities, fees, instruments, calendar."""
    profile = config.tracks[acct_cfg.track]
    calendar: ExchangeCalendar | Utc247Calendar = (
        ExchangeCalendar("XNYS") if profile.calendar == "XNYS" else Utc247Calendar()
    )
    return AccountRuntime(
        account_id=acct_cfg.account_id,
        alias=acct_cfg.alias,
        portfolio_id=config.runtime.portfolio_id,
        track=acct_cfg.track,
        broker=acct_cfg.broker,
        currency="USD",
        rule_profile=capabilities.rule_profile,
        capabilities=capabilities,
        fees=fees,
        specs=specs,
        profile=profile,
        calendar=calendar,
    )


@dataclass
class ExecutionCore:
    """The mode-independent services around one broker adapter (architecture.md §2)."""

    accounts: dict[str, AccountRuntime]
    ledger: Ledger
    pauses: PauseService
    fx: FxBook
    leadership: dict[str, LeadershipManager]
    oms: OrderManager
    gateway: SubmissionGateway
    reconciler: Reconciler
    allocator: PortfolioAllocator
    exits: ExitManager


def wire_execution(
    config: ResolvedConfig,
    *,
    db: Database,
    clock: Clock,
    account: AccountRuntime,
    adapter: BrokerAdapter,
    holder: str,
    namespace: str,
) -> ExecutionCore:
    """Ledger, pauses, leadership, OMS, gateway, reconciler, allocator and exits for one account.

    The submission gateway binds its authority to ``adapter`` here; nothing else can submit.
    """
    runtime = config.runtime
    accounts_map = {account.account_id: account}
    ledger = Ledger(db)
    pauses = PauseService()
    fx = FxBook("USD")
    leadership = {
        account.account_id: LeadershipManager(
            db, account_id=account.account_id, holder=holder, clock=clock
        )
    }
    oms = OrderManager(
        db=db,
        ledger=ledger,
        pauses=pauses,
        clock=clock,
        calendars={account.account_id: account.calendar},
        tracks={account.account_id: account.track},
        modelled_fees=(
            {account.account_id: account.fees}
            if runtime.account(account.alias).accrue_modelled_fees
            else None
        ),
    )
    adapters: dict[str, BrokerAdapter] = {account.account_id: adapter}
    gateway = SubmissionGateway(
        db=db,
        ledger=ledger,
        adapters=adapters,
        leadership=leadership,
        pauses=pauses,
        oms=oms,
        clock=clock,
    )
    reconciler = Reconciler(
        db=db,
        ledger=ledger,
        oms=oms,
        pauses=pauses,
        adapters=adapters,
        clock=clock,
        portfolio_id=runtime.portfolio_id,
    )
    allocator = PortfolioAllocator(
        db=db,
        ledger=ledger,
        pauses=pauses,
        policy=config.policy,
        mode=runtime.mode,
        portfolio_id=runtime.portfolio_id,
        accounts=accounts_map,
        leadership=leadership,
        clock=clock,
        fx=fx,
        namespace=namespace,
    )
    exits = ExitManager(
        db=db,
        ledger=ledger,
        pauses=pauses,
        allocator=allocator,
        gateway=gateway,
        accounts=accounts_map,
        clock=clock,
        portfolio_id=runtime.portfolio_id,
    )
    return ExecutionCore(
        accounts=accounts_map,
        ledger=ledger,
        pauses=pauses,
        fx=fx,
        leadership=leadership,
        oms=oms,
        gateway=gateway,
        reconciler=reconciler,
        allocator=allocator,
        exits=exits,
    )


def build_replay_environment(
    config: ResolvedConfig,
    *,
    db: Database,
    session: RecordedSession,
    run_id: str,
    clock: ReplayClock,
    fee_override: Path | None = None,
    faults: FaultPlan | None = None,
    broker: ReplayBroker | None = None,
    use_fixture_edge: bool = True,
    holder: str = "worker-1",
) -> ReplayEnvironment:
    runtime = config.runtime
    if runtime.mode is not Mode.REPLAY:
        raise ValueError("replay environments require a replay configuration")
    if len(runtime.accounts) != 1:
        raise ValueError("the replay demonstrator drives exactly one account")
    acct_cfg = runtime.accounts[0]
    capabilities = load_capabilities(config.resolve(acct_cfg.capability_profile))
    fees: FeeSchedule = load_fee_schedule(fee_override or config.resolve(acct_cfg.fee_schedule))
    specs = load_instruments(config.resolve(acct_cfg.instruments))
    account = account_runtime(config, acct_cfg, capabilities=capabilities, fees=fees, specs=specs)
    if broker is None:
        broker = ReplayBroker(
            account_id=account.account_id,
            clock=clock,
            capabilities=capabilities,
            fees=fees,
            specs=dict(specs),
            starting_cash=REPLAY_BROKER_STARTING_CASH,
            faults=faults,
        )
    core = wire_execution(
        config,
        db=db,
        clock=clock,
        account=account,
        adapter=broker,
        holder=holder,
        namespace=run_id,
    )
    edge: EdgeEstimator = (
        FixtureEdgeEstimator(session, runtime.mode) if use_fixture_edge else NoEdgeEstimator()
    )
    strategy = CandidateA(load_candidate_a_spec(), ExchangeCalendar("XNYS"))
    env = ReplayEnvironment(
        config=config,
        run_id=run_id,
        db=db,
        clock=clock,
        session=session,
        ledger=core.ledger,
        pauses=core.pauses,
        loss_monitor=LossMonitor(config.policy.policy, core.pauses),
        accounts=core.accounts,
        leadership=core.leadership,
        broker=broker,
        oms=core.oms,
        gateway=core.gateway,
        reconciler=core.reconciler,
        allocator=core.allocator,
        exits=core.exits,
        market=MarketView(
            feed=session.manifest.feed, decision_minutes=account.profile.decision_bar_minutes
        ),
        strategy=strategy,
        edge=edge,
        fx=core.fx,
        holder=holder,
    )
    bootstrap(env, broker_cash_baseline=broker.get_account().cash)
    return env


class BootstrapTarget(Protocol):
    """What :func:`bootstrap` needs (a replay or a paper environment)."""

    @property
    def config(self) -> ResolvedConfig: ...
    @property
    def db(self) -> Database: ...
    @property
    def clock(self) -> Clock: ...
    @property
    def ledger(self) -> Ledger: ...
    @property
    def accounts(self) -> dict[str, AccountRuntime]: ...
    @property
    def portfolio_id(self) -> str: ...


def bootstrap(env: BootstrapTarget, *, broker_cash_baseline: Decimal) -> None:
    """Create portfolio/account rows and the initial USD 50 capital entry once.

    The broker cash baseline is recorded only when the account row is created: reconciliation
    compares the *change* in broker cash since then with the subledger (architecture.md §5).
    """
    policy = env.config.policy
    now: datetime = env.clock.now()
    with env.db.tx() as conn:
        insert_ignore(
            conn,
            policy_versions,
            {
                "policy_hash": policy.policy_hash,
                "canonical_json": policy.canonical_json,
                "source": policy.source,
                "first_seen_at": now,
            },
            ["policy_hash"],
        )
        exists = conn.execute(
            select(portfolios.c.portfolio_id).where(portfolios.c.portfolio_id == env.portfolio_id)
        ).first()
        if exists is None:
            conn.execute(
                insert(portfolios).values(
                    portfolio_id=env.portfolio_id,
                    base_currency=policy.policy.base_currency,
                    initial_capital=policy.policy.initial_trading_capital,
                    version=0,
                    created_at=now,
                )
            )
        for account in env.accounts.values():
            row = conn.execute(
                select(accounts.c.account_id).where(accounts.c.account_id == account.account_id)
            ).first()
            if row is None:
                conn.execute(
                    insert(accounts).values(
                        account_id=account.account_id,
                        portfolio_id=env.portfolio_id,
                        alias=account.alias,
                        track=account.track.value,
                        broker=account.broker,
                        mode=env.config.runtime.mode.value,
                        currency=account.currency,
                        rule_profile=account.rule_profile.value,
                        legal_type=account.capabilities.account_legal_type.value,
                        allocated_capital=policy.policy.initial_trading_capital,
                        broker_cash_baseline=broker_cash_baseline,
                        version=0,
                        created_at=now,
                    )
                )
                env.ledger.record_cash_flow(
                    conn,
                    account_id=account.account_id,
                    kind=CashFlowKind.INITIAL_CAPITAL,
                    amount=policy.policy.initial_trading_capital,
                    currency=account.currency,
                    at=now,
                    ref="initial-trading-capital",
                )
            caps = account.capabilities
            insert_ignore(
                conn,
                capability_profiles,
                {
                    "profile_version": caps.profile_version,
                    "broker": caps.broker,
                    "profile_hash": content_hash(caps),
                    "profile_json": canonical_json(caps),
                    "recorded_at": now,
                },
                ["profile_version"],
            )
            for spec in account.specs.values():
                insert_ignore(
                    conn,
                    instruments,
                    {
                        "instrument_id": spec.instrument_id,
                        "track": spec.identity.track.value,
                        "product_type": spec.identity.product_type.value,
                        "spec_hash": content_hash(spec),
                        "spec_json": canonical_json(spec),
                        "updated_at": now,
                    },
                    ["instrument_id"],
                )


__all__ = [
    "BootstrapTarget",
    "ExecutionCore",
    "LegalAccountType",
    "ReplayEnvironment",
    "RuleProfileKind",
    "account_runtime",
    "bootstrap",
    "build_replay_environment",
    "wire_execution",
]
