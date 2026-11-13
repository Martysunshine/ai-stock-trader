"""Test harness: real replay environments over the synthetic sessions with injectable faults."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from trader.brokers.replay.broker import ReplayBroker
from trader.brokers.replay.faults import FaultPlan
from trader.config.host import HostRole
from trader.config.runtime_config import load_config
from trader.data.recorded_session import RecordedSession, load_session
from trader.domain.market import Quote, Trade, TradingStatus
from trader.ops.clock import ReplayClock
from trader.runtime.environment import ReplayEnvironment, build_replay_environment
from trader.runtime.replay_runner import ReplayRunner
from trader.storage.db import Database
from trader.storage.schema import fills, order_intents, risk_reservations

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNA = "XNYS:SYNA"
SYNB = "XNYS:SYNB"
FEED = "SYNTHETIC"
ENTRY_DECISION = datetime(2026, 11, 27, 15, 35, 20, tzinfo=UTC)  # just after the SYNA entry


def session(name: str = "synthetic_base_v1") -> RecordedSession:
    return load_session(REPO_ROOT / "research" / "sessions" / name)


@dataclass
class Harness:
    env: ReplayEnvironment
    runner: ReplayRunner
    session: RecordedSession
    db_url: str
    db_schema: str | None
    index: int = 0

    def run_until(self, when: datetime) -> None:
        events = self.session.events
        while self.index < len(events) and events[self.index].receipt_time < when:
            self.runner.step(events[self.index])
            self.index += 1

    def run_to_end(self) -> None:
        self.run_until(datetime(2100, 1, 1, tzinfo=UTC))

    def step(self, event: Quote | Trade | TradingStatus) -> None:
        self.runner.step(event)

    def restart(self) -> Harness:
        """Discard all worker objects; keep the database and the (remote) simulated broker."""
        broker = self.env.broker
        clock = self.env.clock
        self.env.close()
        db = Database(self.db_url, schema=self.db_schema)
        env = make_env_on(db, self.session, clock=clock, broker=broker, faults=None)
        runner = ReplayRunner(env)
        for event in self.session.events[: self.index]:
            env.market.on_event(event)
            env.market.finalize(event.receipt_time, event.instrument_id)
        runner.start()
        return Harness(env, runner, self.session, self.db_url, self.db_schema, self.index)

    @property
    def account(self) -> str:
        return next(iter(self.env.accounts))

    # ---------------------------------------------------------------- queries
    def intents(self) -> list[object]:
        with self.env.db.tx() as conn:
            return list(conn.execute(select(order_intents).order_by(order_intents.c.seq)).all())

    def fills(self) -> list[object]:
        with self.env.db.tx() as conn:
            return list(conn.execute(select(fills).order_by(fills.c.execution_id)).all())

    def reservations(self) -> list[object]:
        with self.env.db.tx() as conn:
            return list(conn.execute(select(risk_reservations)).all())

    def position(self, instrument: str = SYNA) -> Decimal:
        with self.env.db.tx() as conn:
            pos = self.env.ledger.position(conn, self.account, instrument)
            return pos.quantity if pos else Decimal(0)

    def flags(self) -> list[str]:
        with self.env.db.tx() as conn:
            return sorted(f.cause.value for f in self.env.pauses.all_active(conn))

    def cash(self) -> Decimal:
        with self.env.db.tx() as conn:
            return self.env.ledger.cash(conn, self.account, "USD")


def make_env_on(
    db: Database,
    sess: RecordedSession,
    *,
    clock: ReplayClock,
    broker: ReplayBroker | None = None,
    faults: FaultPlan | None = None,
    fee_override: str | None = None,
    use_fixture_edge: bool = True,
) -> ReplayEnvironment:
    config = load_config(REPO_ROOT / "configs" / "replay.yaml", host_role=HostRole.DEV)
    env = build_replay_environment(
        config,
        db=db,
        session=sess,
        run_id="test-run",
        clock=clock,
        fee_override=config.resolve(fee_override) if fee_override else None,
        faults=faults,
        broker=broker,
        use_fixture_edge=use_fixture_edge,
    )
    return env


def harness(
    db: Database,
    *,
    session_name: str = "synthetic_base_v1",
    faults: FaultPlan | None = None,
    fee_override: str | None = None,
    use_fixture_edge: bool = True,
) -> Harness:
    sess = session(session_name)
    clock = ReplayClock(sess.events[0].receipt_time - timedelta(minutes=1))
    env = make_env_on(
        db,
        sess,
        clock=clock,
        faults=faults,
        fee_override=fee_override,
        use_fixture_edge=use_fixture_edge,
    )
    runner = ReplayRunner(env)
    runner.start()
    return Harness(env, runner, sess, db.url, db.schema)


def quote(instrument: str, at: datetime, bid: str, ask: str, seq: int, size: str = "300") -> Quote:
    return Quote(
        instrument_id=instrument,
        feed=FEED,
        event_time=at,
        receipt_time=at + timedelta(milliseconds=120),
        sequence=seq,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(size),
        ask_size=Decimal(size),
    )
