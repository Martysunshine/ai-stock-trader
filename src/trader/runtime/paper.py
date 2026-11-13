"""Paper runtime (milestone M4): the replay decision cycle on a real clock against the broker's
paper account (architecture.md §1, §6, §9; implementation_roadmap.md M4).

Environment (:func:`build_paper_environment`)

* PostgreSQL only (instructions.md §3, ADR-0001); the schema is brought to the Alembic head
  programmatically before anything is read or written.
* The adapter and the account's capability profile must both be *paper*; live is impossible.
* The paper account must be flat (no positions, no open orders) unless this database already
  holds the account's recorded baseline (a restart of the same deployment). The USD 50
  subledger is created once by :func:`trader.runtime.environment.bootstrap`, with the adapter's
  cash as the broker cash baseline: reconciliation compares the *change* in broker cash with the
  subledger, so the paper balance (about USD 100,000) is never treated as trading capital.
* Identifiers: the allocator namespace is ``paper/<account>/<session date>/<baseline time>``.
  Client order IDs therefore never repeat across sessions, and a database reset (which records a
  new baseline time) can never re-issue a client order ID the broker has already seen.
* Edge: :class:`~trader.strategies.edge.NoEdgeEstimator` unless one is injected. Without a
  validated model every candidate is rejected ``NO_EDGE`` - the intended safe behaviour.

Runner (:class:`PaperRunner`). At startup, after recovery, the system clock is measured once
against the broker's clock (``ops/clock_check.py``): the offset goes into the log, and beyond the
policy limit a ``!!`` line and a ``SYSTEM_CLOCK_OFFSET`` alert say that entries will be paused
until the clock is synchronised. The check never refuses or delays the run. Then, per regular
session (XNYS calendar, early closes included):

1. sleep until the open; warm features up with the bars already completed in this session;
2. every ``quote_interval`` poll the latest quotes and run the shared decision cycle (broker
   events, loss windows, exits, entry time-outs, unknown-outcome resolution);
3. after each bar end plus ``bar_delay`` poll the completed bar, retrying a few times when the
   source has not published it yet, and run the cycle with it: features -> strategy -> risk ->
   approval -> outbox -> gateway submit;
4. reconcile every ``reconcile_interval`` (60 s) and after the close: open DAY orders expire at
   the broker at the close, which reconciliation observes by client order ID (never assumed);
   a fill that lands while the broker is being read gets a few seconds to reach the ledger
   before a failure is recorded (``runtime/decision_loop.py``);
5. source time ahead of the local clock beyond the policy limit raises a ``CLOCK_SKEW`` pause;
6. on a stop request (Ctrl+C): no new entries, working entry orders are cancelled, a final
   reconciliation runs, and positions still held are reported as pending liquidation.

Halts are not observed by polling; a halted stock's quotes go stale, which blocks entries
(``STALE_DATA``). Monitored exits cannot protect while the process is stopped (ADR-0005).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select

from trader.accounting.fees import load_fee_schedule
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.brokers.base import BrokerAdapter, OrderScope
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.runtime_config import AccountConfig, ResolvedConfig
from trader.data.bars import floor_time
from trader.data.calendars import ExchangeCalendar, Session
from trader.data.history import DataQualityError
from trader.data.market_view import MarketView
from trader.data.sources.http import TransportError
from trader.domain.account import BrokerPosition
from trader.domain.candidates import RejectReason
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.instruments import AnySpec
from trader.domain.market import Bar
from trader.domain.orders import LIVE_STATES, BrokerOrder, OrderPurpose
from trader.domain.types import DomainError, Mode, iso_utc
from trader.execution._gateway import SubmissionGateway
from trader.execution.exits import ExitManager, ExitTrigger
from trader.execution.oms import OrderManager
from trader.execution.reconciliation import Reconciler, ReconciliationResult
from trader.ops.clock import SleepingClock
from trader.ops.clock_check import ClockOffsetUnknown, measure_clock_offset
from trader.ops.leadership import LeadershipManager
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.risk.allocator import PortfolioAllocator
from trader.risk.loss_limits import LossMonitor, window_key_and_start
from trader.risk.portfolio import AccountRuntime, entries_since
from trader.runtime.decision_loop import DecisionLoop, StepStats
from trader.runtime.environment import account_runtime, bootstrap, wire_execution
from trader.runtime.paper_data import (
    PaperMarketData,
    SpreadReference,
    bar_from_source,
    feed_id,
    quote_from_source,
)
from trader.runtime.session_watch import ALERT_CODE, SessionWatch
from trader.storage.db import Database
from trader.storage.schema import accounts, alerts, order_intents
from trader.strategies.candidate_a import CandidateA, load_candidate_a_spec
from trader.strategies.edge import EdgeEstimator, NoEdgeEstimator

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "deploy" / "migrations"
DEFAULT_HOLDER = "paper-worker"
DEFAULT_FEED = feed_id("iex")
#: Alert raised at startup when the system clock is off by more than the policy limit.
CLOCK_ALERT_CODE = "SYSTEM_CLOCK_OFFSET"
CLOCK_SYNC_HINT = (
    "Windows: Settings > Time & language > Date & time > Sync now, or run w32tm /resync as "
    "administrator"
)


class PaperStartRefused(DomainError):
    """The paper runtime refuses to start; nothing was submitted."""


class PaperBroker(BrokerAdapter, Protocol):
    """A paper broker adapter: the broker contract plus its account-event stream."""

    def start_stream(self) -> None: ...

    def stop(self) -> None: ...


class BrokerTime(Protocol):
    """The broker's own clock reading (``as_of``), as the Alpaca adapter's market clock has."""

    @property
    def as_of(self) -> datetime: ...


@runtime_checkable
class BrokerClockSource(Protocol):
    """A broker adapter that can report the broker's current time."""

    def market_clock(self) -> BrokerTime: ...


# --------------------------------------------------------------------------- environment


@dataclass
class PaperEnvironment:
    config: ResolvedConfig
    db: Database
    clock: SleepingClock
    ledger: Ledger
    pauses: PauseService
    loss_monitor: LossMonitor
    accounts: dict[str, AccountRuntime]
    leadership: dict[str, LeadershipManager]
    broker: PaperBroker
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
    baseline_at: datetime | None = None
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def portfolio_id(self) -> str:
        return self.config.runtime.portfolio_id

    @property
    def account(self) -> AccountRuntime:
        return next(iter(self.accounts.values()))

    @property
    def calendar(self) -> ExchangeCalendar:
        calendar = self.account.calendar
        if not isinstance(calendar, ExchangeCalendar):
            raise PaperStartRefused("the paper runtime drives exchange-calendar (stock) accounts")
        return calendar

    def set_session(self, session_date: date) -> str:
        """Point the allocator's ID namespace at a session (see module docstring)."""
        if self.baseline_at is None:
            raise PaperStartRefused("no recorded baseline: the ID namespace is undefined")
        namespace = paper_namespace(self.account.account_id, session_date, self.baseline_at)
        self.allocator.namespace = namespace
        return namespace

    def close(self) -> None:
        for lm in self.leadership.values():
            lm.release()
        self.db.dispose()


@dataclass(frozen=True, slots=True)
class RecordedBaseline:
    mode: str
    broker_cash_baseline: Decimal | None
    created_at: datetime


def paper_namespace(account_id: str, session_date: date, baseline_at: datetime) -> str:
    return f"paper/{account_id}/{session_date.isoformat()}/{iso_utc(baseline_at)}"


def paper_account(config: ResolvedConfig, alias: str | None = None) -> AccountConfig:
    """The paper account a command drives; refuses any other mode (never a fallback)."""
    runtime = config.runtime
    if runtime.mode is not Mode.PAPER:
        raise PaperStartRefused(
            f"paper commands only accept a paper configuration, not {runtime.mode.value} "
            "(no fallback)"
        )
    if alias is not None:
        return runtime.account(alias)
    if len(runtime.accounts) != 1:
        raise PaperStartRefused("several accounts configured: pass --account-alias")
    return runtime.accounts[0]


def paper_database_url(config: ResolvedConfig) -> str:
    """The PostgreSQL URL from the environment variable the configuration names."""
    database = config.runtime.database
    if database.backend != "postgresql":
        raise PaperStartRefused("paper mode requires PostgreSQL; SQLite is replay-only (ADR-0001)")
    url = database.url(config.config_dir)
    if not url.startswith("postgresql"):
        raise PaperStartRefused(
            f"{database.url_env} must hold a PostgreSQL URL (postgresql+psycopg://...); SQLite "
            "is replay-only (ADR-0001)"
        )
    return url


def alembic_config(
    url: str,
    *,
    schema: str | None = None,
    script_location: Path = MIGRATIONS_DIR,
    output_buffer: TextIO | None = None,
) -> Config:
    """The programmatic equivalent of ``alembic -c deploy/alembic.ini -x url=... [-x schema=...]``."""
    if not (script_location / "env.py").exists():
        raise PaperStartRefused(f"Alembic migrations not found at {script_location}")
    x_args = [f"url={url}"] + ([f"schema={schema}"] if schema else [])
    cfg = Config(output_buffer=output_buffer, cmd_opts=argparse.Namespace(x=x_args))
    cfg.set_main_option("script_location", str(script_location))
    return cfg


def upgrade_to_head(db: Database, *, script_location: Path = MIGRATIONS_DIR) -> None:
    """Bring the runtime schema to the Alembic head (idempotent)."""
    if not db.is_postgres:
        raise PaperStartRefused("paper mode requires PostgreSQL; SQLite is replay-only (ADR-0001)")
    command.upgrade(
        alembic_config(db.url, schema=db.schema, script_location=script_location), "head"
    )


def recorded_baseline(db: Database, account_id: str) -> RecordedBaseline | None:
    with db.tx() as conn:
        row = conn.execute(
            select(accounts.c.mode, accounts.c.broker_cash_baseline, accounts.c.created_at).where(
                accounts.c.account_id == account_id
            )
        ).first()
    if row is None:
        return None
    return RecordedBaseline(
        mode=str(row.mode),
        broker_cash_baseline=row.broker_cash_baseline,
        created_at=row.created_at,
    )


def adapter_since(db: Database, account_id: str, *, now: datetime) -> datetime:
    """Where the adapter's execution history starts: the recorded baseline, else ``now``."""
    upgrade_to_head(db)
    recorded = recorded_baseline(db, account_id)
    return recorded.created_at if recorded is not None else now


def check_account_state(
    *,
    account_id: str,
    recorded: RecordedBaseline | None,
    positions: Sequence[BrokerPosition],
    open_orders: Sequence[BrokerOrder],
    require_baseline: bool = False,
) -> None:
    """A fresh baseline needs a flat account; a recorded one must be a paper baseline."""
    if recorded is None:
        if require_baseline:
            raise PaperStartRefused(
                f"this database holds no paper baseline for {account_id}; start the bot with "
                "`trader paper run` first"
            )
        if positions or open_orders:
            raise PaperStartRefused(
                f"the paper account {account_id} is not flat ({len(positions)} position(s), "
                f"{len(open_orders)} open order(s)) and this database holds no recorded baseline "
                "for it. Flatten the paper account at the broker, or use the database that "
                "recorded its baseline. Nothing was started."
            )
        return
    if recorded.mode != Mode.PAPER.value:
        raise PaperStartRefused(
            f"account {account_id} is recorded in {recorded.mode} mode in this database"
        )
    if recorded.broker_cash_baseline is None:
        raise PaperStartRefused(f"account {account_id} has no recorded broker cash baseline")


def ensure_leadership_free(env: PaperEnvironment) -> None:
    """Refuse early (:class:`~trader.ops.leadership.LeadershipUnavailable`) while another worker
    leads the account (architecture.md §5, §9).

    Startup recovery raises ``RECOVERY_PENDING`` *before* it takes leadership, so a second
    ``paper run`` (or an owner ``close-positions`` while the bot runs) would otherwise pause the
    running worker's entries until its next reconciliation. A short-lived probe of the advisory
    lock refuses first and leaves no flag behind; startup recovery then takes leadership itself.
    """
    probe = LeadershipManager(
        env.db, account_id=env.account.account_id, holder=env.holder, clock=env.clock
    )
    probe.acquire()
    probe.release()


def _check_adapter(
    adapter: PaperBroker, acct_cfg: AccountConfig, capabilities: BrokerCapabilities
) -> None:
    if capabilities.environment != "paper":
        raise PaperStartRefused(
            f"capability profile {capabilities.profile_version} is {capabilities.environment}, "
            "not paper"
        )
    if adapter.account_id != acct_cfg.account_id:
        raise PaperStartRefused(
            f"adapter account {adapter.account_id} differs from configured {acct_cfg.account_id}"
        )
    if adapter.capabilities().environment != "paper":
        raise PaperStartRefused("the broker adapter is not a paper adapter; live is impossible")


def build_paper_environment(
    config: ResolvedConfig,
    *,
    clock: SleepingClock,
    adapter: PaperBroker,
    database: Database,
    account_alias: str | None = None,
    edge: EdgeEstimator | None = None,
    holder: str = DEFAULT_HOLDER,
    market_feed: str = DEFAULT_FEED,
    require_baseline: bool = False,
    specs: Mapping[str, AnySpec] | None = None,
) -> PaperEnvironment:
    """Validate, migrate, check the paper account, wire and bootstrap (see module docstring).

    ``specs`` replaces the configured instrument specs (``trader paper certify`` passes the one
    it verified against the broker's asset record for this run only).
    """
    acct_cfg = paper_account(config, account_alias)
    capabilities = load_capabilities(config.resolve(acct_cfg.capability_profile))
    _check_adapter(adapter, acct_cfg, capabilities)
    if not database.is_postgres:
        raise PaperStartRefused("paper mode requires PostgreSQL; SQLite is replay-only (ADR-0001)")
    upgrade_to_head(database)
    recorded = recorded_baseline(database, acct_cfg.account_id)
    check_account_state(
        account_id=acct_cfg.account_id,
        recorded=recorded,
        positions=adapter.get_positions(),
        open_orders=adapter.list_orders(OrderScope.OPEN),
        require_baseline=require_baseline,
    )
    return _assemble(
        config,
        acct_cfg,
        capabilities=capabilities,
        clock=clock,
        adapter=adapter,
        database=database,
        edge=edge,
        holder=holder,
        market_feed=market_feed,
        specs=specs,
    )


def _assemble(
    config: ResolvedConfig,
    acct_cfg: AccountConfig,
    *,
    capabilities: BrokerCapabilities,
    clock: SleepingClock,
    adapter: PaperBroker,
    database: Database,
    edge: EdgeEstimator | None,
    holder: str,
    market_feed: str,
    specs: Mapping[str, AnySpec] | None = None,
) -> PaperEnvironment:
    """Wire and bootstrap without the safety checks of :func:`build_paper_environment`.

    Only :func:`build_paper_environment` may call this in production; tests use it over an
    in-memory schema.
    """
    fees = load_fee_schedule(config.resolve(acct_cfg.fee_schedule))
    catalogue = (
        dict(specs) if specs is not None else load_instruments(config.resolve(acct_cfg.instruments))
    )
    account = account_runtime(
        config, acct_cfg, capabilities=capabilities, fees=fees, specs=catalogue
    )
    if not isinstance(account.calendar, ExchangeCalendar):
        raise PaperStartRefused("the paper runtime drives exchange-calendar (stock) accounts")
    core = wire_execution(
        config,
        db=database,
        clock=clock,
        account=account,
        adapter=adapter,
        holder=holder,
        namespace="paper-unbound",
    )
    env = PaperEnvironment(
        config=config,
        db=database,
        clock=clock,
        ledger=core.ledger,
        pauses=core.pauses,
        loss_monitor=LossMonitor(config.policy.policy, core.pauses),
        accounts=core.accounts,
        leadership=core.leadership,
        broker=adapter,
        oms=core.oms,
        gateway=core.gateway,
        reconciler=core.reconciler,
        allocator=core.allocator,
        exits=core.exits,
        market=MarketView(feed=market_feed, decision_minutes=account.profile.decision_bar_minutes),
        strategy=CandidateA(load_candidate_a_spec(), ExchangeCalendar("XNYS")),
        edge=edge if edge is not None else NoEdgeEstimator(),
        fx=core.fx,
        holder=holder,
    )
    bootstrap(env, broker_cash_baseline=adapter.get_account().cash)
    recorded = recorded_baseline(database, account.account_id)
    if recorded is None:  # pragma: no cover - bootstrap always records the account
        raise PaperStartRefused("bootstrap did not record the account baseline")
    env.baseline_at = recorded.created_at
    env.set_session(env.calendar.session_date_of(clock.now()))
    return env


# --------------------------------------------------------------------------- runner


@dataclass(frozen=True, slots=True)
class PaperSchedule:
    """Polling cadence (seconds). The defaults keep Alpaca data calls far below rate limits."""

    bar_delay_seconds: float = 5
    bar_retry_seconds: float = 10
    bar_attempts: int = 3
    quote_interval_seconds: float = 5
    reconcile_interval_seconds: float = 60
    close_grace_seconds: float = 120
    max_sleep_seconds: float = 1


@dataclass
class PaperRunSummary:
    sessions_completed: int = 0
    bar_polls: int = 0
    bars: int = 0
    late_bars: int = 0
    unusable_bars: int = 0
    missing_bars: int = 0
    quote_polls: int = 0
    unusable_quotes: int = 0
    feed_errors: int = 0
    clock_skew_events: int = 0
    reconciliations: dict[str, int] = field(default_factory=dict)
    live_after_close: list[str] = field(default_factory=list)
    stopped: str | None = None
    final_reconciliation: str | None = None
    open_positions: dict[str, Decimal] = field(default_factory=dict)
    session_summaries: list[str] = field(default_factory=list)

    def lines(self, stats: StepStats) -> list[str]:
        decisions = stats.decisions or []
        reasons: dict[str, int] = {}
        for d in decisions:
            for r in d.reasons:
                reasons[r.value] = reasons.get(r.value, 0) + 1
        out = [
            f"sessions completed : {self.sessions_completed}",
            f"bars               : {self.bars} used, {self.late_bars} late, "
            f"{self.unusable_bars} unusable, {self.missing_bars} missing "
            f"({self.bar_polls} polls)",
            f"quotes             : {self.quote_polls} polls, {self.unusable_quotes} unusable",
            f"candidates         : {stats.candidates}; approved {stats.approved}; "
            f"rejections {dict(sorted(reasons.items())) or 'none'}",
            f"feed errors        : {self.feed_errors}; clock-skew events {self.clock_skew_events}",
            f"reconciliations    : {dict(sorted(self.reconciliations.items())) or 'none'}",
            f"final reconcile    : {self.final_reconciliation or 'not run'}",
        ]
        if self.live_after_close:
            out.append(f"live after close   : {', '.join(self.live_after_close)}")
        out.extend(self.session_summaries)
        for key, qty in sorted(self.open_positions.items()):
            out.append(f"PENDING LIQUIDATION {key} qty={qty} (monitored exits stop with the bot)")
        if self.stopped:
            out.append(f"stopped            : {self.stopped}")
        return out


@dataclass
class _BarPoll:
    due: datetime
    boundary: datetime
    attempt: int
    symbols: tuple[str, ...]


def paper_loop(env: PaperEnvironment) -> DecisionLoop:
    """The decision loop of a paper environment. Unlike replay it runs on a real clock, so a
    reconciliation may wait on that clock for a fill in flight (``runtime/decision_loop.py``)."""
    return DecisionLoop(env, settle_wait=lambda seconds: env.clock.sleep(seconds))


class PaperRunner:
    """Drives one paper account through regular sessions (see module docstring)."""

    def __init__(
        self,
        env: PaperEnvironment,
        *,
        market_data: PaperMarketData,
        schedule: PaperSchedule | None = None,
        log: Callable[[str], None] | None = None,
        spread_reference: SpreadReference | None = None,
    ) -> None:
        if market_data.feed != env.market.feed:
            raise PaperStartRefused(
                f"market data feed {market_data.feed} differs from the market view feed "
                f"{env.market.feed} (never mix feeds)"
            )
        self.env = env
        self.clock = env.clock
        self.data = market_data
        self.spread_reference = spread_reference
        self.schedule = schedule or PaperSchedule()
        self.loop = paper_loop(env)
        self.summary = PaperRunSummary()
        self.calendar = env.calendar
        self._log = log
        self._symbols = {
            spec.identity.symbol: iid for iid, spec in sorted(env.account.specs.items())
        }
        self._minutes = env.account.profile.decision_bar_minutes
        policy = env.config.policy.policy
        self._skew_limit = timedelta(milliseconds=policy.quality.max_clock_skew_milliseconds)
        self._sequence = 0
        self._session: Session | None = None
        self._watch: SessionWatch | None = None
        self._decisions_seen = 0
        self._stop_reason: str | None = None
        self._started = False
        self._shut_down = False

    # ------------------------------------------------------------------ control
    @property
    def stop_requested(self) -> bool:
        return self._stop_reason is not None

    def request_stop(self, reason: str = "owner request") -> None:
        """Stop after the current step: no new entries, then cancel entries and reconcile."""
        if self._stop_reason is None:
            self._stop_reason = reason

    def log(self, message: str) -> None:
        if self._log is not None:
            self._log(f"{self.clock.now():%Y-%m-%d %H:%M:%S}Z {message}")

    # ------------------------------------------------------------------ lifecycle
    def run(self, *, sessions: int | None = None) -> PaperRunSummary:
        """Run until ``sessions`` regular sessions completed or a stop is requested."""
        if sessions is not None and sessions < 1:
            raise ValueError("sessions must be at least 1")
        try:
            self.start()
            while not self.stop_requested and (
                sessions is None or self.summary.sessions_completed < sessions
            ):
                if not self.run_session(self.session_for(self.clock.now())):
                    break
        except KeyboardInterrupt:
            self.request_stop("interrupted (Ctrl+C)")
        finally:
            self.shutdown()
        return self.summary

    def start(self) -> None:
        """Start the account stream, then startup recovery (entries stay off until it passes),
        then the one-off check of the system clock against the broker's."""
        ensure_leadership_free(self.env)
        self.env.broker.start_stream()
        self.loop.start()
        self._started = True
        # Startup recovery just reconciled: the 60-second health/periodic cadence starts now.
        self.loop.last_reconcile_at = self.clock.now()
        startup = self.env.extras.get("startup_reconciliation", {})
        if isinstance(startup, dict):
            for status in startup.values():
                self._count_reconciliation(str(status))
        self.log(f"startup recovery: reconciliation {startup}")
        self.check_clock()

    def check_clock(self) -> None:
        """Measure the system clock against the broker's clock once and say what was found.

        Within the policy limit: one line with the offset, so every session log records it.
        Beyond it: a ``!!`` line and a HIGH alert, because source times ahead of a slow system
        clock raise ``CLOCK_SKEW`` again and again and no entry is taken (2026-11-10). The run
        is never refused or delayed for it - the pause already protects entries - and a
        measurement that fails is only noted.
        """
        broker = self.env.broker
        if not isinstance(broker, BrokerClockSource):
            self.log("system clock not checked: this broker adapter reports no clock")
            return
        try:
            measured = measure_clock_offset(self.clock, lambda: broker.market_clock().as_of)
        except ClockOffsetUnknown as exc:
            self.log(f"system clock not checked: {exc}")
            return
        limit = int(self._skew_limit.total_seconds() * 1000)
        offset = measured.describe("the broker's clock")
        where = f"system clock is {offset} (limit {limit} ms)"
        if not measured.exceeds(self._skew_limit):
            self.log(where)
            return
        if measured.system_clock_is_behind:
            effect = "new entries will be paused"
        else:
            effect = "quotes will look older than they are and entries may be refused as stale"
        line = f"{where}: {effect} until the clock is synchronised ({CLOCK_SYNC_HINT})"
        self.log(f"!! {line}")
        self._raise_alert(CLOCK_ALERT_CODE, line)

    def shutdown(self) -> None:
        """No new entries; cancel working entries; final reconciliation; stop the stream."""
        if self._shut_down:
            return
        self._shut_down = True
        self.summary.stopped = self._stop_reason
        try:
            if self._started:
                cancelled = self.loop.cancel_working_entries()
                if cancelled:
                    self.log(f"shutdown: cancel requested for {len(cancelled)} working entry")
                self.loop.pump()
                result = self._reconcile("shutdown")
                self.summary.final_reconciliation = result.status
                self.summary.open_positions = self.env.exits.pending_liquidation()
        except Exception as exc:  # shutdown is best effort: the next start reconciles again
            self.summary.final_reconciliation = f"ERROR {type(exc).__name__}: {exc}"
            self.log(f"shutdown reconciliation failed: {exc}")
        finally:
            self.env.broker.stop()

    def session_for(self, now: datetime) -> Session:
        """The session in progress, else today's if it has not opened yet, else the next one."""
        current = self.calendar.session_at(now)
        if current is not None:
            return current
        today = self.calendar.session_for_date(self.calendar.session_date_of(now))
        if today is not None and now < today.open:
            return today
        return self.calendar.next_session(self.calendar.session_date_of(now))

    # ------------------------------------------------------------------ one session
    def run_session(self, session: Session) -> bool:
        """Trade one regular session; ``False`` when a stop request ended it early."""
        namespace = self.env.set_session(session.session_date)
        self._session = session
        self._watch = SessionWatch(session.session_date)
        self._decisions_seen = len(self.loop.stats.decisions or [])
        self.log(
            f"session {session.session_date}: regular hours {session.open:%H:%M}-"
            f"{session.close:%H:%M} UTC, id namespace {namespace}"
        )
        if not self._sleep_until(session.open):
            return False
        sched = self.schedule
        step = timedelta(minutes=self._minutes)
        delay = timedelta(seconds=sched.bar_delay_seconds)
        now = self.clock.now()
        self.warm_up(session, now)
        next_boundary = floor_time(now, self._minutes) + step
        next_quote = now
        retries: list[_BarPoll] = []
        while not self.stop_requested:
            now = self.clock.now()
            if now >= session.close:
                break
            if next_boundary < session.close and now >= next_boundary + delay:
                self._poll_boundary(next_boundary, tuple(self._symbols), 1, retries)
                next_boundary += step
                next_quote = self.clock.now() + timedelta(seconds=sched.quote_interval_seconds)
                continue
            due = [r for r in retries if r.due <= now]
            if due:
                for poll in sorted(due, key=lambda r: (r.due, r.boundary)):
                    retries.remove(poll)
                    self._poll_boundary(poll.boundary, poll.symbols, poll.attempt, retries)
                next_quote = self.clock.now() + timedelta(seconds=sched.quote_interval_seconds)
                continue
            if now >= next_quote:
                self.tick()
                next_quote = now + timedelta(seconds=sched.quote_interval_seconds)
            if self._reconcile_due(self.clock.now()):
                self._reconcile("paper-periodic")
            wake = min(
                [next_boundary + delay, next_quote, session.close] + [r.due for r in retries]
            )
            if not self._sleep_until(wake):
                return False
        if self.stop_requested:
            return False
        if not self.close_session(session):
            return False
        self.summary.sessions_completed += 1
        return True

    def warm_up(self, session: Session, now: datetime) -> None:
        """Load the bars completed earlier in this session as features (no decisions)."""
        boundary = floor_time(now, self._minutes)
        if boundary <= session.open:
            return
        added = 0
        for bar in self._fetch_bars(tuple(self._symbols), session.open, boundary):
            if self.env.market.add_completed_bar(bar):
                self.loop.persist_bar(bar)
                added += 1
        self.summary.bars += added
        self.log(f"warm-up: {added} completed bar(s) since the open (no decisions)")

    def tick(self) -> None:
        """Fresh quotes, then the shared cycle (fills, losses, exits, time-outs, unknowns)."""
        self.refresh_quotes()
        self.loop.cycle(market_event=True)

    def process_boundary(self, boundary: datetime, symbols: Sequence[str]) -> list[str]:
        """Poll the bars ending at ``boundary`` and decide on them; returns symbols missing."""
        session = self._session
        if session is None:
            raise RuntimeError("process_boundary outside a session")
        self.summary.bar_polls += 1
        fetched = self._fetch_bars(tuple(symbols), session.open, boundary)
        # The slower read first: quotes come last, so the decision sees the freshest quote.
        self.refresh_market_spreads(symbols)
        self.refresh_quotes()
        decide = []
        for bar in fetched:
            if not self.env.market.add_completed_bar(bar):
                self.summary.late_bars += 1
                continue
            self.summary.bars += 1
            if bar.end == boundary and not self.stop_requested:
                decide.append(bar)
            else:
                self.loop.persist_bar(bar)  # late but newer: a feature, never a decision
        self.loop.cycle(market_event=True, bars=lambda: decide)
        missing = []
        for symbol in symbols:
            held = self.env.market.bars[self._symbols[symbol]]
            if not held or held[-1].end != boundary:
                missing.append(symbol)
        stats = self.loop.stats
        self.log(
            f"bars ending {boundary:%H:%M}: {len(decide)} decided, missing "
            f"{','.join(missing) or 'none'}; candidates {stats.candidates}, "
            f"approved {stats.approved}"
        )
        self._watch_decisions()
        return missing

    def close_session(self, session: Session) -> bool:
        """After the close: DAY orders expire at the broker; observe it by reconciliation."""
        grace = timedelta(seconds=self.schedule.close_grace_seconds)
        self.log("session closed: waiting for DAY-order expiry at the broker, then reconciling")
        if not self._sleep_until(session.close + grace):
            return False
        self.loop.pump()
        result = self._reconcile("end-of-session")
        live = self._live_orders()
        if live:
            self.summary.live_after_close.extend(live)
            with self.env.db.tx() as conn:
                conn.execute(
                    insert(alerts).values(
                        raised_at=self.clock.now(),
                        severity="HIGH",
                        code="DAY_ORDER_NOT_EXPIRED",
                        account_id=self.loop.account_id,
                        message=f"live after the close: {', '.join(live)}"[:2000],
                    )
                )
        self.env.market.reset_session()
        self._close_watch()
        self.log(
            f"session {session.session_date} done: end-of-session reconciliation {result.status}"
        )
        return True

    # ------------------------------------------------------------------ session watch
    def _strategy_entries(self) -> int:
        """Strategy entries sent in the current New York session (certification excluded)."""
        _key, start = window_key_and_start("NY_SESSION", self.clock.now())
        with self.env.db.tx() as conn:
            return entries_since(conn, [self.loop.account_id], start)

    def _raise_alert(self, code: str, message: str) -> None:
        with self.env.db.tx() as conn:
            conn.execute(
                insert(alerts).values(
                    raised_at=self.clock.now(),
                    severity="HIGH",
                    code=code,
                    account_id=self.loop.account_id,
                    message=message[:2000],
                )
            )

    def _watch_decisions(self) -> None:
        """Count this bar's decisions; flag operational rejections loudly (ADR-0043)."""
        watch = self._watch
        decisions = self.loop.stats.decisions or []
        new = decisions[self._decisions_seen :]
        self._decisions_seen = len(decisions)
        if watch is None or not new:
            return
        limited = any(RejectReason.ENTRY_LIMIT_REACHED in d.reasons for d in new)
        entries = self._strategy_entries() if limited else None
        reasons = watch.observe(new, entries if limited else 0)
        if not reasons:
            return
        line = watch.bar_line(reasons, entries)
        self.log(line)
        if watch.alerts_written == 0:
            watch.alerts_written += 1
            self._raise_alert(ALERT_CODE, line.removeprefix("!! "))

    def _close_watch(self) -> None:
        watch = self._watch
        if watch is None:
            return
        line = watch.summary_line(self._strategy_entries())
        self.summary.session_summaries.append(line)
        self.log(line)
        if watch.blocked and watch.alerts_written < 2:
            watch.alerts_written += 1
            self._raise_alert(ALERT_CODE, f"session summary: {line}")
        self._watch = None

    # ------------------------------------------------------------------ pieces
    def refresh_quotes(self) -> None:
        try:
            raw = self.data.latest_quotes(sorted(self._symbols))
        except (TransportError, DataQualityError) as exc:
            self._feed_problem("latest quotes", exc)
            return
        self.summary.quote_polls += 1
        now = self.clock.now()
        for symbol, latest in sorted(raw.items()):
            instrument_id = self._symbols.get(symbol)
            if instrument_id is None:
                continue
            self._sequence += 1
            quote, skew = quote_from_source(
                latest,
                instrument_id=instrument_id,
                feed=self.data.feed,
                received_at=now,
                sequence=self._sequence,
            )
            self._note_skew(skew, f"{symbol} quote")
            if quote is None:
                self.summary.unusable_quotes += 1
                continue
            held = self.env.market.quotes.get(instrument_id)
            if held is not None and quote.event_time < held.event_time:
                continue  # an older observation never replaces a newer one
            self.env.market.on_event(quote)

    def refresh_market_spreads(self, symbols: Sequence[str]) -> None:
        """Full-market spread estimates for the symbols about to be decided (ADR-0061).

        A symbol without a fresh estimate loses its old one, so the entry check falls back to
        the venue quote's own spread.
        """
        if self.spread_reference is None:
            return
        estimates = self.spread_reference.spreads_bps(symbols, now=self.clock.now())
        for symbol in symbols:
            instrument_id = self._symbols.get(symbol)
            if instrument_id is None:
                continue
            value = estimates.get(symbol)
            if value is None:
                self.env.market.market_spreads.pop(instrument_id, None)
            else:
                self.env.market.market_spreads[instrument_id] = value

    def _poll_boundary(
        self, boundary: datetime, symbols: tuple[str, ...], attempt: int, retries: list[_BarPoll]
    ) -> None:
        missing = self.process_boundary(boundary, symbols)
        if not missing:
            return
        if attempt < self.schedule.bar_attempts:
            retries.append(
                _BarPoll(
                    due=self.clock.now() + timedelta(seconds=self.schedule.bar_retry_seconds),
                    boundary=boundary,
                    attempt=attempt + 1,
                    symbols=tuple(missing),
                )
            )
        else:
            self.summary.missing_bars += len(missing)

    def _fetch_bars(
        self, symbols: tuple[str, ...], session_open: datetime, boundary: datetime
    ) -> list[Bar]:
        """Completed regular-hours bars ending by ``boundary``, newer than those already held."""
        step = timedelta(minutes=self._minutes)
        starts = []
        for symbol in symbols:
            held = self.env.market.bars[self._symbols[symbol]]
            last = held[-1].start if held else None
            starts.append(
                last + step if last is not None and last >= session_open else session_open
            )
        start = min(starts, default=boundary)
        if start >= boundary:
            return []
        try:
            raw = self.data.completed_bars(
                symbols, minutes=self._minutes, start=start, end=boundary - timedelta(seconds=1)
            )
        except (TransportError, DataQualityError) as exc:
            self._feed_problem("completed bars", exc)
            return []
        now = self.clock.now()
        out: list[Bar] = []
        for symbol in symbols:
            instrument_id = self._symbols[symbol]
            for source_bar in sorted(raw.get(symbol, []), key=lambda b: b.start):
                if source_bar.end > boundary or source_bar.start < session_open:
                    continue  # still forming, or outside regular hours
                bar, skew = bar_from_source(
                    source_bar, instrument_id=instrument_id, feed=self.data.feed, received_at=now
                )
                self._note_skew(skew, f"{symbol} bar ending {source_bar.end:%H:%M}")
                if bar is None:
                    self.summary.unusable_bars += 1
                    continue
                out.append(bar)
        return out

    def _reconcile(self, cleared_by: str) -> ReconciliationResult:
        result = self.loop.reconcile(cleared_by=cleared_by)
        self._count_reconciliation(result.status)
        if result.problems:
            self.log(f"reconciliation {result.status}: {'; '.join(result.problems)}")
        elif result.waited_seconds:
            self.log(
                f"reconciliation {result.status} after waiting {result.waited_seconds:g} s for "
                "a fill in flight"
            )
        return result

    def _count_reconciliation(self, status: str) -> None:
        self.summary.reconciliations[status] = self.summary.reconciliations.get(status, 0) + 1

    def _reconcile_due(self, now: datetime) -> bool:
        last = self.loop.last_reconcile_at
        return last is None or (now - last).total_seconds() >= (
            self.schedule.reconcile_interval_seconds
        )

    def _note_skew(self, skew: timedelta, what: str) -> None:
        if skew <= self._skew_limit:
            return
        self.summary.clock_skew_events += 1
        ms = int(skew.total_seconds() * 1000)
        limit = int(self._skew_limit.total_seconds() * 1000)
        with self.env.db.tx() as conn:
            self.env.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=self.loop.account_id,
                cause=PauseCause.CLOCK_SKEW,
                details=f"{what}: source time {ms} ms ahead of the local clock (limit {limit} ms)",
                now=self.clock.now(),
            )

    def _feed_problem(self, what: str, exc: Exception) -> None:
        self.summary.feed_errors += 1
        self.log(f"market data unavailable ({what}): {exc}")
        with self.env.db.tx() as conn:
            self.env.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=self.loop.account_id,
                cause=PauseCause.FEED_GAP,
                details=f"{what}: {type(exc).__name__}: {exc}"[:2000],
                now=self.clock.now(),
            )

    def _live_orders(self) -> list[str]:
        with self.env.db.tx() as conn:
            rows = conn.execute(
                select(order_intents.c.client_order_id, order_intents.c.state).where(
                    order_intents.c.account_id == self.loop.account_id,
                    order_intents.c.state.in_(tuple(s.value for s in LIVE_STATES)),
                )
            ).all()
        return [f"{r.client_order_id} {r.state}" for r in rows]

    def _sleep_until(self, when: datetime) -> bool:
        """Sleep in short slices so a stop request is honoured promptly; ``False`` if stopped."""
        while not self.stop_requested:
            remaining = (when - self.clock.now()).total_seconds()
            if remaining <= 0:
                return True
            self.clock.sleep(min(remaining, self.schedule.max_sleep_seconds))
        return False


# --------------------------------------------------------------------------- owner commands


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    requested: dict[str, Decimal]
    remaining: dict[str, Decimal]
    reconciliation: str


def reconcile_paper(env: PaperEnvironment) -> ReconciliationResult:
    """One owner-requested reconciliation (``trader reconcile``). Leader only; never submits.

    Raises :class:`~trader.ops.leadership.LeadershipUnavailable` while a paper worker runs (it
    reconciles every 60 seconds itself).
    """
    loop = paper_loop(env)
    leadership = env.leadership[loop.account_id]
    leadership.acquire()
    try:
        return loop.reconcile(cleared_by="owner-reconcile")
    finally:
        leadership.release()


def close_paper_positions(
    env: PaperEnvironment, *, timeout_seconds: float = 120, poll_seconds: float = 10
) -> CloseOutcome:
    """Owner liquidation (``trader close-positions``): reducing market sells for every recorded
    position, then wait for *confirmed* fills. Never claims a closure the broker has not filled.
    """
    loop = paper_loop(env)
    ensure_leadership_free(env)
    env.broker.start_stream()
    try:
        loop.start()
        with env.db.tx() as conn:
            positions = [
                p for p in env.ledger.open_positions(conn, [loop.account_id]) if p.quantity > 0
            ]
        requested = env.exits.pending_liquidation()
        if positions:
            env.exits.act(
                [
                    ExitTrigger(
                        account_id=p.account_id,
                        instrument_id=p.instrument_id,
                        purpose=OrderPurpose.EXIT_RISK_REDUCTION,
                        reason="owner close-positions",
                        entry_intent_id=None,
                    )
                    for p in positions
                ],
                env.market.halted,
            )
            deadline = env.clock.now() + timedelta(seconds=timeout_seconds)
            while env.exits.pending_liquidation() and env.clock.now() < deadline:
                env.clock.sleep(poll_seconds)
                loop.reconcile(cleared_by="close-positions")
        loop.pump()
        result = loop.reconcile(cleared_by="close-positions")
        return CloseOutcome(
            requested=requested,
            remaining=env.exits.pending_liquidation(),
            reconciliation=result.status,
        )
    finally:
        env.broker.stop()


__all__ = [
    "CLOCK_ALERT_CODE",
    "DEFAULT_FEED",
    "BrokerClockSource",
    "CloseOutcome",
    "PaperBroker",
    "PaperEnvironment",
    "PaperRunSummary",
    "PaperRunner",
    "PaperSchedule",
    "PaperStartRefused",
    "adapter_since",
    "alembic_config",
    "build_paper_environment",
    "check_account_state",
    "close_paper_positions",
    "ensure_leadership_free",
    "paper_account",
    "paper_database_url",
    "paper_loop",
    "paper_namespace",
    "reconcile_paper",
    "recorded_baseline",
    "upgrade_to_head",
]
