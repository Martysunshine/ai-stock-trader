"""The first demonstrator (instructions.md §7, implementation_roadmap.md §4) as named scenarios.

| Scenario   | What it demonstrates                                                              |
| ---------- | --------------------------------------------------------------------------------- |
| baseline   | eligible + rejected candidates, exact sizing, ack -> partial fill -> cancel ->    |
|            | exit, ledger == fill-derived P&L, reservations cleared, reconciliation passes     |
| timeout    | timeout after broker acceptance: reconciled by client ID, no duplicate order      |
| crash      | worker crash after acceptance: restart recovers one intent and one position       |
| costly     | an IBKR-like 1% (min USD 0.01) fractional fee schedule: no trade (TOO_COSTLY)     |
| loss       | halt + 6% gap through the stop: daily loss breach; pause survives a restart;      |
|            | resume refused without review and while the breach holds; allowed next session    |

All scenarios use SYNTHETIC data and a declared fixture edge. They demonstrate software
behaviour only and are never evidence of profitability.
"""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from trader.brokers.base import SimulatedCrash
from trader.brokers.replay.faults import FaultKind, FaultPlan, FaultSpec
from trader.config.host import current_host_role
from trader.config.runtime_config import load_config
from trader.data.recorded_session import RecordedSession, load_session
from trader.ops.clock import ReplayClock
from trader.ops.owner_actions import current_loss_breaches, resume_loss_breaches
from trader.reporting.replay_report import RunReport, build_report
from trader.runtime.environment import ReplayEnvironment, build_replay_environment
from trader.runtime.replay_runner import ReplayRunner, session_close_for
from trader.storage.db import Database
from trader.storage.schema import order_intents


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    session: str
    faults: FaultPlan = field(default_factory=FaultPlan)
    fee_override: str | None = None
    restart_before: datetime | None = None


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(
        "baseline",
        "eligible and rejected candidates; partial fill, remainder cancel, exit, reconcile",
        "synthetic_base_v1",
        FaultPlan(faults=(FaultSpec(kind=FaultKind.PARTIAL_FIRST_FILL, fraction=Decimal("0.4")),)),
    ),
    "timeout": Scenario(
        "timeout",
        "HTTP timeout after the broker accepted the entry; reconcile by client order ID",
        "synthetic_base_v1",
        FaultPlan(faults=(FaultSpec(kind=FaultKind.TIMEOUT_AFTER_ACCEPT),)),
    ),
    "crash": Scenario(
        "crash",
        "worker crash after broker acceptance, before local acknowledgement; restart recovers",
        "synthetic_base_v1",
        FaultPlan(faults=(FaultSpec(kind=FaultKind.CRASH_AFTER_ACCEPT),)),
    ),
    "costly": Scenario(
        "costly",
        "costly broker (1% min USD 0.01 per fractional side): costs exceed the edge -> no trade",
        "synthetic_base_v1",
        fee_override="fees/ibkr_us_fractional.yaml",
    ),
    "loss": Scenario(
        "loss",
        "halt then a 6% gap through the stop: daily loss breach, pause persists across restart",
        "synthetic_gap_v1",
        restart_before=datetime(2026, 11, 27, 16, 20, tzinfo=UTC),
    ),
}


@dataclass
class ScenarioResult:
    name: str
    report: RunReport
    broker_submissions: dict[str, int]
    restarts: int
    checks: dict[str, object] = field(default_factory=dict)


def _new_env(
    config_path: Path,
    db: Database,
    session: RecordedSession,
    scenario: Scenario,
    run_id: str,
    clock: ReplayClock,
    broker: object | None = None,
) -> ReplayEnvironment:
    config = load_config(config_path, host_role=current_host_role())
    fee_override = config.resolve(scenario.fee_override) if scenario.fee_override else None
    return build_replay_environment(
        config,
        db=db,
        session=session,
        run_id=run_id,
        clock=clock,
        fee_override=fee_override,
        faults=scenario.faults,
        broker=broker,  # type: ignore[arg-type]
    )


def _warm_up(runner: ReplayRunner, session: RecordedSession, upto: int) -> None:
    """Rebuild in-memory market state from the persisted journal after a restart."""
    env = runner.env
    for event in session.events[:upto]:
        env.market.on_event(event)
        env.market.finalize(event.receipt_time, event.instrument_id)


def run_scenario(
    name: str,
    *,
    repo_root: Path,
    run_dir: Path,
    config_path: Path | None = None,
    db_url: str | None = None,
    db_schema: str | None = None,
) -> ScenarioResult:
    scenario = SCENARIOS[name]
    config_path = config_path or repo_root / "configs" / "replay.yaml"
    session = load_session(repo_root / "research" / "sessions" / scenario.session)
    run_dir.mkdir(parents=True, exist_ok=True)
    if db_url is None:
        db_file = run_dir / "replay.db"
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(db_file) + suffix)
            if stale.exists():
                stale.unlink()
        db_url = f"sqlite:///{db_file.as_posix()}"
    db = Database(db_url, schema=db_schema)
    db.create_schema()
    run_id = f"demo-{name}"
    clock = ReplayClock(session.events[0].receipt_time - timedelta(minutes=1))
    env = _new_env(config_path, db, session, scenario, run_id, clock)
    runner = ReplayRunner(env)
    runner.start()
    runner.journal(session.events)
    events = session.events
    restarts = 0
    checks: dict[str, object] = {}
    index = 0
    try:
        for index, event in enumerate(events):  # noqa: B007 - index used after a crash
            if (
                scenario.restart_before is not None
                and event.receipt_time >= scenario.restart_before
                and restarts == 0
            ):
                break
            runner.step(event)
        else:
            index = len(events)
        resume_at = index
    except SimulatedCrash:
        checks["crashed_at"] = env.clock.now().isoformat()
        resume_at = index + 1  # the crashed event was already seen by the broker
    if resume_at < len(events) or "crashed_at" in checks:
        # ---- restart: brand-new worker objects over the same database and remote broker ----
        restarts += 1
        broker = env.broker
        env.close()
        db = Database(db_url, schema=db_schema)
        env = _new_env(config_path, db, session, scenario, run_id, clock, broker=broker)
        runner = ReplayRunner(env)
        _warm_up(runner, session, resume_at)
        runner.start()
        checks["after_restart_flags"] = _flag_causes(env)
        for event in events[resume_at:]:
            runner.step(event)
    runner.finish(session_close_for(env, session.manifest.session_date))

    account_id = runner.account_id
    if name == "loss":
        checks.update(_loss_resume_checks(env, account_id))
    with env.db.tx() as conn:
        report = build_report(
            conn,
            ledger=env.ledger,
            pauses=env.pauses,
            run_id=run_id,
            scenario=name,
            account_id=account_id,
            currency="USD",
            evidence_class="SYNTHETIC_REPLAY",
            notes=[
                scenario.description,
                f"session {session.manifest.name} ({session.manifest_hash[:12]})",
            ],
        )
        intents = conn.execute(select(order_intents.c.client_order_id)).scalars().all()
    submissions = {cid: env.broker.submission_attempts.get(cid, 0) for cid in intents}
    (run_dir / "report.md").write_text(report.to_markdown(), encoding="utf-8")
    (run_dir / "report.json").write_text(
        json.dumps(
            {"report": report.to_dict(), "checks": checks, "broker_submissions": submissions},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    env.close()
    return ScenarioResult(name, report, submissions, restarts, checks)


def _flag_causes(env: ReplayEnvironment) -> list[str]:
    with env.db.tx() as conn:
        return sorted(f.cause.value for f in env.pauses.all_active(conn))


def _loss_resume_checks(env: ReplayEnvironment, account_id: str) -> dict[str, object]:
    account = env.accounts[account_id]
    reviewer = getpass.getuser() or "owner"
    ny = tuple(w for w in account.profile.loss_windows if w.startswith("NY_"))

    def attempt(note: str) -> list[str]:
        still = current_loss_breaches(
            db=env.db,
            ledger=env.ledger,
            policy=env.config.policy,
            portfolio_id=env.portfolio_id,
            account_id=account_id,
            currency=account.currency,
            fees=account.fees,
            marks=env.market.marks(),
            now=env.clock.now(),
            ny_windows=ny,
        )
        outcome = resume_loss_breaches(
            db=env.db,
            pauses=env.pauses,
            policy=env.config.policy,
            portfolio_id=env.portfolio_id,
            reviewer=reviewer,
            note=note,
            still_breached=still,
            figures={"still_breached": ",".join(sorted(still))},
            now=env.clock.now(),
        )
        return outcome.refused

    checks: dict[str, object] = {}
    checks["refused_without_review"] = attempt("")
    checks["refused_same_session"] = attempt(
        "Reviewed the gap loss; stop could not execute during halt."
    )
    next_session = datetime(2026, 11, 30, 14, 35, tzinfo=UTC)
    env.clock.advance_to(next_session)
    checks["refused_next_session"] = attempt(
        "Reviewed the 2026-11-27 gap loss on the next session; resuming the replay pilot."
    )
    checks["flags_after_review"] = _flag_causes(env)
    return checks
