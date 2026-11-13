"""PostgreSQL-only invariants: leadership/fencing, cross-track concurrent allocation, migrations.

These run against the docker-compose database and fail loudly if it is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, select

from tests.conftest import pg_url
from trader.accounting.fees import load_fee_schedule
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.policy import LoadedPolicy
from trader.config.track_profiles import load_track_profiles
from trader.data.calendars import Utc247Calendar
from trader.domain.account import CashFlowKind, RuleProfileKind
from trader.domain.candidates import EdgeEvidence, ReturnBasis, SignalScore, TradeCandidate
from trader.domain.market import Quote
from trader.domain.types import Direction, Mode, Track
from trader.ops.clock import ReplayClock
from trader.ops.leadership import LeadershipLost, LeadershipManager, LeadershipUnavailable
from trader.ops.pause import PauseService
from trader.risk.allocator import MarketInputs, PortfolioAllocator
from trader.risk.portfolio import AccountRuntime
from trader.storage.db import Database
from trader.storage.schema import accounts, metadata, portfolios, risk_reservations

pytestmark = pytest.mark.pg
ROOT = Path(__file__).resolve().parents[1]


def test_only_the_current_leader_can_act_and_stale_tokens_are_fenced(pg_db: Database) -> None:
    clock = ReplayClock(datetime(2026, 11, 27, 15, tzinfo=UTC))
    a = LeadershipManager(pg_db, account_id="acct-1", holder="worker-a", clock=clock)
    b = LeadershipManager(pg_db, account_id="acct-1", holder="worker-b", clock=clock)
    assert a.acquire() == 1
    with pytest.raises(LeadershipUnavailable):
        b.acquire()
    with pg_db.tx() as conn:
        a.assert_current(conn)
    a.simulate_lock_loss()  # worker A's session dies (network partition / crash)
    assert b.acquire() == 2
    with pytest.raises(LeadershipLost), pg_db.tx() as conn:
        a.assert_current(conn)
    with pg_db.tx() as conn:
        b.assert_current(conn)
    b.release()
    a.release()


def test_migration_matches_the_runtime_schema(pg_db: Database) -> None:
    """``alembic upgrade head`` produces exactly the schema the code uses (no drift)."""
    migrated = f"{pg_db.schema}_m"
    db = Database(pg_url(), schema=migrated)
    db.create_namespace()
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ROOT / "deploy" / "alembic.ini"),
                "-x",
                f"url={pg_url()}",
                "-x",
                f"schema={migrated}",
                "upgrade",
                "head",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONUTF8": "1"},
            check=True,
            capture_output=True,
        )
        with db.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), metadata)
    finally:
        db.drop_postgres_schema()
    assert diff == []


def _runtime(pf: str, account_id: str, track: Track) -> AccountRuntime:
    profiles = load_track_profiles(ROOT / "configs/tracks")
    specs = load_instruments(ROOT / "configs/instruments/synthetic_equities.yaml")
    caps = load_capabilities(ROOT / "configs/capabilities/replay.yaml")
    enabled_profile = profiles[Track.STOCKS]
    return AccountRuntime(
        account_id=account_id,
        alias=account_id,
        portfolio_id=pf,
        track=track,
        broker="replay",
        currency="USD",
        rule_profile=RuleProfileKind.UNVERIFIED,
        capabilities=caps,
        fees=load_fee_schedule(ROOT / "configs/fees/replay_demo.yaml"),
        specs=specs,
        profile=enabled_profile,
        calendar=Utc247Calendar(),
    )


def test_concurrent_cross_track_requests_cannot_double_allocate(
    pg_db: Database, policy: LoadedPolicy
) -> None:
    """Three account workers (one per track) race for the single shared USD 50 budget."""
    pf = f"pf-{uuid.uuid4().hex[:8]}"
    now = datetime(2026, 11, 27, 15, 35, 15, tzinfo=UTC)
    ledger = Ledger(pg_db)
    account_ids = [f"acct-{name}" for name in ("stocks", "crypto", "commodities")]
    split = [Decimal(20), Decimal(15), Decimal(15)]
    with pg_db.tx() as conn:
        conn.execute(
            insert(portfolios).values(
                portfolio_id=pf,
                base_currency="USD",
                initial_capital=Decimal(50),
                version=0,
                created_at=now,
            )
        )
        for i, acct in enumerate(account_ids):
            conn.execute(
                insert(accounts).values(
                    account_id=acct,
                    portfolio_id=pf,
                    alias=acct,
                    track="STOCKS",
                    broker="replay",
                    mode="replay",
                    currency="USD",
                    rule_profile="UNVERIFIED",
                    legal_type="SIMULATED",
                    allocated_capital=split[i],
                    broker_cash_baseline=None,
                    version=0,
                    created_at=now,
                )
            )
            # one USD 50 portfolio split across three accounts; each could afford an entry alone
            ledger.record_cash_flow(
                conn,
                account_id=acct,
                kind=CashFlowKind.INITIAL_CAPITAL,
                amount=split[i],
                currency="USD",
                at=now,
                ref="init",
            )
    runtimes = {a: _runtime(pf, a, Track.STOCKS) for a in account_ids}
    errors: list[BaseException] = []
    decisions = []

    def worker(acct: str, n: int) -> None:
        try:
            clock = ReplayClock(now)
            lm = {
                a: LeadershipManager(pg_db, account_id=a, holder=f"w-{a}", clock=clock)
                for a in account_ids
            }
            lm[acct].acquire()
            allocator = PortfolioAllocator(
                db=pg_db,
                ledger=Ledger(pg_db),
                pauses=PauseService(),
                policy=policy,
                mode=Mode.REPLAY,
                portfolio_id=pf,
                accounts=runtimes,
                leadership=lm,
                clock=clock,
                fx=FxBook(),
                namespace=f"ns-{acct}",
            )
            for i in range(n):
                instrument = "XNYS:SYNA" if i % 2 == 0 else "XNYS:SYNB"
                cand = TradeCandidate(
                    candidate_id=f"c-{acct}-{i}",
                    strategy_id="s",
                    strategy_version=1,
                    spec_hash="h",
                    instrument_id=instrument,
                    track=Track.STOCKS,
                    direction=Direction.LONG,
                    feed="SYNTHETIC",
                    bar_end=now - timedelta(seconds=15),
                    decision_time=now - timedelta(seconds=14),
                    reference_price=Decimal("101.21"),
                    stop_price=Decimal("100.93"),
                    target_price=Decimal("101.77"),
                    max_hold_minutes=60,
                    entry_limit_offset_bps=Decimal(5),
                    entry_ttl_seconds=120,
                    gap_allowance_fraction=Decimal("0.001"),
                    snapshot_hash="s",
                    expires_at=now + timedelta(seconds=45),
                )
                bid, ask = ("101.20", "101.24") if instrument == "XNYS:SYNA" else ("50.70", "50.72")
                q = Quote(
                    instrument_id=instrument,
                    feed="SYNTHETIC",
                    event_time=now,
                    receipt_time=now,
                    sequence=1,
                    bid=Decimal(bid),
                    ask=Decimal(ask),
                    bid_size=Decimal(300),
                    ask_size=Decimal(300),
                )
                score = SignalScore(
                    candidate_id=cand.candidate_id,
                    model_hash="m",
                    expected_gross_return=Decimal("0.006"),
                    uncertainty_margin=Decimal("0.0015"),
                    basis=ReturnBasis.MID_TO_MID,
                    evidence=EdgeEvidence.SYNTHETIC_FIXTURE,
                    edge_source_hash="e",
                )
                decisions.append(
                    allocator.reserve_entry(
                        account_id=acct,
                        candidate=cand,
                        score=score,
                        market=MarketInputs(quote=q, marks={}, halted=False),
                    )
                )
            lm[acct].release()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(a, 4)) for a in account_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    approved = [d for d in decisions if d.approved]
    with pg_db.tx() as conn:
        active = conn.execute(
            select(risk_reservations).where(
                risk_reservations.c.state == "ACTIVE", risk_reservations.c.portfolio_id == pf
            )
        ).all()
    assert len(decisions) == 12
    assert len(approved) == 1  # one pending entry globally: everything else is ENTRY_LIMIT_REACHED
    assert len(active) == 1
    assert sum(Decimal(r.notional_usd) for r in active) <= Decimal(10)
