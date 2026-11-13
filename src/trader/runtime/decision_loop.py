"""The broker-agnostic decision cycle shared by replay and paper (architecture.md §6).

One :meth:`DecisionLoop.cycle` runs, in order:

1. apply broker events (fills, acknowledgements, cancels) through the order manager;
2. mark positions conservatively and evaluate loss windows (pauses persist);
3. evaluate exits continuously - independent of entry pauses;
4. cancel entry remainders whose time-to-live expired;
5. persist newly *available* completed bars, generate candidates from them, score them and pass
   them to the shared allocator; submit approved intents;
6. apply broker events again, then resolve unknown outcomes by reconciliation (never by
   resubmission).

What differs between modes stays outside this module: the replay runner advances a simulated
clock and lets the simulated broker match orders on each recorded event; the paper runner polls
completed bars and quotes on a real clock and receives broker events from the adapter's stream.
Neither may reorder these steps, so a decision taken in paper is the decision replay would take
on the same bars and quotes.

Reconciliation and a fill in flight (paper): the broker is read in several requests, so a fill
that lands while they run shows up as an execution, a position and cash the ledger does not have
yet. When *every* difference is explained by an order of this account that is working or became
final moments ago, a loop that was given a way to wait compares again - at most
:data:`IN_FLIGHT_RETRIES` times, :data:`IN_FLIGHT_RETRY_SECONDS` apart, applying queued broker
events before each - and only then records the verdict. A comparison passes only without any
difference; if the differences remain, the run fails, pauses and is logged exactly as before.
Replay never waits: its simulated broker reports a fill and its effects together.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select

from trader.accounting.equity import account_equity, portfolio_equity
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.brokers.base import BrokerAdapter
from trader.config.runtime_config import ResolvedConfig
from trader.data.market_view import MarketView
from trader.domain.candidates import DecisionRecord
from trader.domain.market import Bar
from trader.domain.orders import LIVE_STATES, OrderPurpose, OrderState
from trader.execution._gateway import SubmissionGateway
from trader.execution.exits import ExitManager
from trader.execution.oms import OrderManager
from trader.execution.reconciliation import Reconciler, ReconciliationResult
from trader.ops.clock import Clock
from trader.ops.leadership import LeadershipManager
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.ops.recovery import startup_recovery
from trader.risk.allocator import MarketInputs, PortfolioAllocator
from trader.risk.loss_limits import LossMonitor, LossStatus, window_key_and_start
from trader.risk.portfolio import AccountRuntime
from trader.storage.db import Database
from trader.storage.helpers import insert_ignore
from trader.storage.schema import bars as bars_table
from trader.storage.schema import order_intents
from trader.strategies.candidate_a import CandidateA
from trader.strategies.edge import EdgeEstimator

#: Periodic reconciliation interval while orders are working or a health pause is active.
HEALTH_RECONCILE_SECONDS = 60
#: Extra comparisons a reconciliation may make while a fill is in flight (module docstring).
IN_FLIGHT_RETRIES = 3
#: Seconds between those comparisons: at most 3 s of waiting in total.
IN_FLIGHT_RETRY_SECONDS = 1.0


class LoopEnvironment(Protocol):
    """What the decision cycle needs from an environment (replay or paper)."""

    @property
    def config(self) -> ResolvedConfig: ...
    @property
    def db(self) -> Database: ...
    @property
    def clock(self) -> Clock: ...
    @property
    def ledger(self) -> Ledger: ...
    @property
    def pauses(self) -> PauseService: ...
    @property
    def loss_monitor(self) -> LossMonitor: ...
    @property
    def accounts(self) -> dict[str, AccountRuntime]: ...
    @property
    def leadership(self) -> dict[str, LeadershipManager]: ...
    @property
    def broker(self) -> BrokerAdapter: ...
    @property
    def oms(self) -> OrderManager: ...
    @property
    def gateway(self) -> SubmissionGateway: ...
    @property
    def reconciler(self) -> Reconciler: ...
    @property
    def allocator(self) -> PortfolioAllocator: ...
    @property
    def exits(self) -> ExitManager: ...
    @property
    def market(self) -> MarketView: ...
    @property
    def strategy(self) -> CandidateA: ...
    @property
    def edge(self) -> EdgeEstimator: ...
    @property
    def fx(self) -> FxBook: ...
    @property
    def portfolio_id(self) -> str: ...
    @property
    def extras(self) -> dict[str, object]: ...


@dataclass
class StepStats:
    events: int = 0
    candidates: int = 0
    approved: int = 0
    decisions: list[DecisionRecord] | None = None


def no_bars() -> Iterable[Bar]:
    return ()


class DecisionLoop:
    """The decision cycle for the single account an environment drives.

    ``settle_wait`` is how the loop waits for a fill in flight during a reconciliation (paper:
    the clock's ``sleep``). Without it (replay) a reconciliation never waits.
    """

    def __init__(
        self, env: LoopEnvironment, *, settle_wait: Callable[[float], None] | None = None
    ) -> None:
        self.env = env
        self.account_id = next(iter(env.accounts))
        self.stats = StepStats(decisions=[])
        self.last_loss_status: LossStatus | None = None
        #: When the loop last reconciled (unknown outcome, periodic health or explicit request).
        self.last_reconcile_at: datetime | None = None
        self._window_keys: tuple[str, ...] = ()
        self._settle_wait = settle_wait

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Startup recovery (architecture.md §9), then a forced loss-window evaluation."""
        results = startup_recovery(self.env, [self.account_id])
        self.env.extras["startup_reconciliation"] = {k: v.status for k, v in results.items()}
        self.check_losses(force=True)

    # ------------------------------------------------------------------ one cycle
    def cycle(self, *, market_event: bool, bars: Callable[[], Iterable[Bar]] = no_bars) -> None:
        """Run one decision cycle.

        ``market_event`` says the market view received a quote or trading status since the last
        cycle (losses are then re-marked while positions are open). ``bars`` is called once, at
        step 5, and returns the completed bars that became available for decisions.
        """
        fills = self.pump()
        keys = self.current_window_keys()
        new_window = keys != self._window_keys
        if fills or new_window or market_event:
            # Record beginning-of-window equity as soon as a window starts, even when flat.
            self.check_losses(force=fills > 0 or new_window)
        self.exits()
        self.entry_timeouts()
        for bar in bars():
            self.persist_bar(bar)
            self.decide(bar.instrument_id)
        self.pump()
        self.resolve_unknowns()
        self.stats.events += 1

    # ------------------------------------------------------------------ pieces
    def pump(self) -> int:
        """Apply every queued broker event; returns the number of fills posted.

        ``OrderManager.pump`` turns an adapter that cannot report its events into an alert plus
        an ``UNKNOWN_ORDER`` pause instead of an exception in the run loop (ADR-0033).
        """
        return self.env.oms.pump(self.env.broker).count("FILL_POSTED")

    def has_positions(self) -> bool:
        with self.env.db.tx() as conn:
            return bool(self.env.ledger.open_positions(conn, [self.account_id]))

    def current_window_keys(self) -> tuple[str, ...]:
        account = self.env.accounts[self.account_id]
        kinds = tuple(sorted(set(account.profile.loss_windows) | {"UTC_DAY", "UTC_WEEK"}))
        now = self.env.clock.now()
        return tuple(window_key_and_start(k, now)[0] for k in kinds)

    def check_losses(self, *, force: bool = False) -> None:
        env = self.env
        if not force and not self.has_positions():
            return
        now = env.clock.now()
        self._window_keys = self.current_window_keys()
        marks = env.market.marks()
        account = env.accounts[self.account_id]
        policy = env.config.policy.policy
        with env.db.tx() as conn:
            acc = account_equity(
                conn,
                env.ledger,
                account_id=self.account_id,
                currency=account.currency,
                marks=marks,
                fees=account.fees,
            )
            pe = portfolio_equity(
                [acc], fx=env.fx, now=now, initial_capital_usd=policy.initial_trading_capital
            )
            ny = tuple(w for w in account.profile.loss_windows if w.startswith("NY_"))
            if ny:
                env.loss_monitor.evaluate(
                    conn,
                    scope=Scope.ACCOUNT,
                    scope_id=self.account_id,
                    portfolio_id=env.portfolio_id,
                    windows=ny,
                    now=now,
                    equity=acc.conservative_equity,
                    cum_external_flows=acc.net_external_flows,
                    initial_capital=policy.initial_trading_capital,
                )
            self.last_loss_status = env.loss_monitor.evaluate(
                conn,
                scope=Scope.PORTFOLIO,
                scope_id=env.portfolio_id,
                portfolio_id=env.portfolio_id,
                windows=("UTC_DAY", "UTC_WEEK"),
                now=now,
                equity=pe.conservative_equity_usd,
                cum_external_flows=pe.net_external_flows_usd,
                initial_capital=policy.initial_trading_capital,
            )

    def exits(self) -> None:
        env = self.env
        triggers = env.exits.evaluate(env.market.quotes, env.market.halted)
        if triggers:
            env.exits.act(triggers, env.market.halted)

    def entry_timeouts(self) -> None:
        env = self.env
        now = env.clock.now()
        with env.db.tx() as conn:
            rows = conn.execute(
                select(order_intents.c.intent_id).where(
                    order_intents.c.purpose == OrderPurpose.ENTRY.value,
                    order_intents.c.state.in_(
                        (OrderState.ACKNOWLEDGED.value, OrderState.PARTIALLY_FILLED.value)
                    ),
                    order_intents.c.entry_ttl_until <= now,
                )
            ).all()
        for row in rows:
            env.gateway.request_cancel(row.intent_id)

    def cancel_working_entries(self) -> list[str]:
        """Request cancellation of every working entry order (shutdown: no new exposure)."""
        with self.env.db.tx() as conn:
            rows = conn.execute(
                select(order_intents.c.intent_id).where(
                    order_intents.c.account_id == self.account_id,
                    order_intents.c.purpose == OrderPurpose.ENTRY.value,
                    order_intents.c.state.in_(
                        (OrderState.ACKNOWLEDGED.value, OrderState.PARTIALLY_FILLED.value)
                    ),
                )
            ).all()
        return [row.intent_id for row in rows if self.env.gateway.request_cancel(row.intent_id)]

    def decide(self, instrument_id: str) -> None:
        env = self.env
        now = env.clock.now()
        candidate = env.strategy.on_bar(env.market.bars[instrument_id], now)
        if candidate is None:
            return
        self.stats.candidates += 1
        score = env.edge.score(candidate)
        decision = env.allocator.reserve_entry(
            account_id=self.account_id,
            candidate=candidate,
            score=score,
            market=MarketInputs(
                quote=env.market.quotes.get(instrument_id),
                marks=env.market.marks(),
                halted=instrument_id in env.market.halted,
                market_spread_bps=env.market.market_spreads.get(instrument_id),
            ),
        )
        assert self.stats.decisions is not None
        self.stats.decisions.append(decision)
        if decision.approved:
            self.stats.approved += 1
            env.gateway.submit_pending()

    def resolve_unknowns(self) -> None:
        """Reconcile on unknown outcomes immediately; otherwise periodically while orders are
        live or a health pause is active (lost account-stream events are recovered by REST)."""
        env = self.env
        with env.db.tx() as conn:
            states = set(
                conn.execute(
                    select(order_intents.c.state).where(
                        order_intents.c.account_id == self.account_id,
                        order_intents.c.state.in_(tuple(s.value for s in LIVE_STATES)),
                    )
                ).scalars()
            )
            flagged = any(
                f.category.value == "HEALTH"
                for f in env.pauses.active_flags(
                    conn, portfolio_id=env.portfolio_id, account_id=self.account_id
                )
            )
        unknown = bool(
            states
            & {
                OrderState.SUBMITTING.value,
                OrderState.UNKNOWN_OUTCOME.value,
                OrderState.UNRESOLVED.value,
            }
        )
        working = bool(states - {OrderState.PENDING_SUBMIT.value})
        now = env.clock.now()
        due = (
            self.last_reconcile_at is None
            or (now - self.last_reconcile_at).total_seconds() >= HEALTH_RECONCILE_SECONDS
        )
        if unknown or ((flagged or working) and due):
            self.last_reconcile_at = now
            self._reconcile_settled(cleared_by="runner")  # the cycle just applied broker events
            self.pump()

    def reconcile(self, *, cleared_by: str) -> ReconciliationResult:
        """An explicit full reconciliation (paper: every 60 s, at the close and at shutdown).

        Queued broker events are applied first, so a fill that already arrived is in the ledger
        before the ledger is compared with the broker.
        """
        self.last_reconcile_at = self.env.clock.now()
        self.pump()
        result = self._reconcile_settled(cleared_by=cleared_by)
        self.pump()
        return result

    def _reconcile_settled(self, *, cleared_by: str) -> ReconciliationResult:
        """Compare, give a fill in flight a short bounded chance to arrive, then record.

        Nothing is recorded (no run, pause or alert) before the last comparison. The recorded
        run keeps the start of the first comparison, so a flag raised while waiting is never
        cleared by this run.
        """
        reconciler = self.env.reconciler
        wait = self._settle_wait
        first = result = reconciler.compare(self.account_id)
        retries = 0
        while wait is not None and retries < IN_FLIGHT_RETRIES and self._worth_waiting(result):
            wait(IN_FLIGHT_RETRY_SECONDS)
            retries += 1
            self.pump()
            result = reconciler.compare(self.account_id)
        if retries:
            result.started_at = first.started_at
            result.waited_seconds = retries * IN_FLIGHT_RETRY_SECONDS
            result.notes.append(
                f"waited {result.waited_seconds:g} s for a fill in flight; the first comparison "
                f"found: {'; '.join(first.problems)}"
            )
        return reconciler.record(self.account_id, result, cleared_by=cleared_by)

    def _worth_waiting(self, result: ReconciliationResult) -> bool:
        """Whether waiting could spare a false pause: every difference is explained by a fill
        in flight, and the account is not already paused for a failed reconciliation (then
        there is no false pause to avoid, and the exits must not be kept waiting)."""
        if not result.in_flight_only:
            return False
        env = self.env
        with env.db.tx() as conn:
            flags = env.pauses.active_flags(
                conn, portfolio_id=env.portfolio_id, account_id=self.account_id
            )
        return all(f.cause is not PauseCause.RECONCILIATION_FAILED for f in flags)

    def persist_bar(self, bar: Bar) -> None:
        with self.env.db.tx() as conn:
            insert_ignore(
                conn,
                bars_table,
                {
                    "instrument_id": bar.instrument_id,
                    "feed": bar.feed,
                    "minutes": bar.minutes,
                    "start": bar.start,
                    "end": bar.end,
                    "available_time": bar.available_time,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "trade_count": bar.trade_count,
                    "vwap": bar.vwap,
                },
                ["instrument_id", "feed", "minutes", "start"],
            )


__all__ = [
    "HEALTH_RECONCILE_SECONDS",
    "IN_FLIGHT_RETRIES",
    "IN_FLIGHT_RETRY_SECONDS",
    "DecisionLoop",
    "LoopEnvironment",
    "StepStats",
    "no_bars",
]
