"""Persisted pauses and loss windows (risk_and_validation.md §1, §4; CRYPTO §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert

from trader.config.policy import LoadedPolicy
from trader.ops.pause import PauseCause, PauseService, ResumeRefused, Scope
from trader.risk.loss_limits import LossMonitor, window_key_and_start
from trader.storage.db import Database
from trader.storage.schema import reconciliation_runs

PF = "pf"


def _evaluate(
    db: Database,
    policy: LoadedPolicy,
    now: datetime,
    equity: str,
    windows: tuple[str, ...] = ("UTC_DAY", "UTC_WEEK"),
) -> list[str]:
    monitor = LossMonitor(policy.policy, PauseService())
    with db.tx() as conn:
        status = monitor.evaluate(
            conn,
            scope=Scope.PORTFOLIO,
            scope_id=PF,
            portfolio_id=PF,
            windows=windows,
            now=now,
            equity=Decimal(equity),
            cum_external_flows=Decimal(50),
            initial_capital=Decimal(50),
        )
    return sorted(b.cause.value for b in status.breaches)


def test_daily_loss_breach_persists_across_a_restart(
    any_db: Database, policy: LoadedPolicy
) -> None:
    t0 = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert _evaluate(any_db, policy, t0, "50") == []
    assert _evaluate(any_db, policy, t0 + timedelta(hours=1), "49.49") == ["DAILY_UTC"]
    # a "restart": brand-new service objects reading the same database
    fresh = PauseService()
    with any_db.tx() as conn:
        assert [f.cause for f in fresh.active_flags(conn, portfolio_id=PF)] == [
            PauseCause.DAILY_UTC
        ]
    # recovering equity does not clear the flag automatically
    _evaluate(any_db, policy, t0 + timedelta(hours=2), "50")
    with any_db.tx() as conn:
        assert fresh.active_flags(conn, portfolio_id=PF)


def test_loss_resume_requires_review_and_is_refused_while_breached(
    sqlite_db: Database, policy: LoadedPolicy
) -> None:
    t0 = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    _evaluate(sqlite_db, policy, t0, "50")
    _evaluate(sqlite_db, policy, t0 + timedelta(minutes=5), "49.40")
    svc = PauseService()
    with sqlite_db.tx() as conn:
        flag = svc.active_flags(conn, portfolio_id=PF)[0]
    with pytest.raises(ResumeRefused, match="review note"), sqlite_db.tx() as conn:
        svc.resume_loss_breach(
            conn,
            flag_id=flag.flag_id,
            reviewer="owner",
            note="",
            figures={},
            policy_hash="h",
            breach_still_active=False,
            now=t0,
        )
    with pytest.raises(ResumeRefused, match="still holds"), sqlite_db.tx() as conn:
        svc.resume_loss_breach(
            conn,
            flag_id=flag.flag_id,
            reviewer="owner",
            note="reviewed the loss",
            figures={},
            policy_hash="h",
            breach_still_active=True,
            now=t0,
        )
    with sqlite_db.tx() as conn:
        svc.resume_loss_breach(
            conn,
            flag_id=flag.flag_id,
            reviewer="owner",
            note="reviewed the loss",
            figures={},
            policy_hash="h",
            breach_still_active=False,
            now=t0,
        )
        assert svc.active_flags(conn, portfolio_id=PF) == []


def test_health_flags_clear_only_after_a_newer_passed_reconciliation(sqlite_db: Database) -> None:
    svc = PauseService()
    t = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    with sqlite_db.tx() as conn:
        svc.raise_flag(
            conn,
            scope=Scope.ACCOUNT,
            scope_id="a",
            cause=PauseCause.UNKNOWN_ORDER,
            details="x",
            now=t,
        )
        svc.raise_flag(
            conn, scope=Scope.PORTFOLIO, scope_id="a", cause=PauseCause.DAILY_NY, details="x", now=t
        )
        for run_id, status, started in (
            ("old", "PASSED", t - timedelta(minutes=1)),
            ("bad", "FAILED", t),
            ("new", "PASSED", t + timedelta(seconds=1)),
        ):
            conn.execute(
                insert(reconciliation_runs).values(
                    run_id=run_id,
                    account_id="a",
                    started_at=started,
                    finished_at=started,
                    status=status,
                    summary_json="{}",
                )
            )
    with sqlite_db.tx() as conn:
        assert (
            svc.clear_health_flags(
                conn, scope_ids=["a"], reconciliation_run_id="old", cleared_by="t", now=t
            )
            == []
        )
    with pytest.raises(ResumeRefused), sqlite_db.tx() as conn:
        svc.clear_health_flags(
            conn, scope_ids=["a"], reconciliation_run_id="bad", cleared_by="t", now=t
        )
    with sqlite_db.tx() as conn:
        cleared = svc.clear_health_flags(
            conn, scope_ids=["a"], reconciliation_run_id="new", cleared_by="t", now=t
        )
        assert len(cleared) == 1
        remaining = [f.cause for f in svc.all_active(conn)]
    assert remaining == [PauseCause.DAILY_NY]  # loss flags never clear by reconciliation


def test_weekend_crypto_loss_window_and_monday_rollover(
    sqlite_db: Database, policy: LoadedPolicy
) -> None:
    sat = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    assert window_key_and_start("UTC_WEEK", sat)[0] == "2026-W48"
    _evaluate(sqlite_db, policy, sat, "50")
    assert _evaluate(sqlite_db, policy, sat + timedelta(hours=1), "48.70") == [
        "DAILY_UTC",
        "WEEKLY_UTC",
    ]
    monday = datetime(2026, 11, 30, 0, 0, 1, tzinfo=UTC)
    key, start = window_key_and_start("UTC_WEEK", monday)
    assert key == "2026-W49" and start == datetime(2026, 11, 30, tzinfo=UTC)
    with sqlite_db.tx() as conn:  # flags persist through the rollover until reviewed
        assert {f.cause for f in PauseService().active_flags(conn, portfolio_id=PF)} == {
            PauseCause.DAILY_UTC,
            PauseCause.WEEKLY_UTC,
        }


def test_new_york_windows_follow_local_dates_across_dst() -> None:
    late_ny = datetime(2026, 11, 28, 3, 0, tzinfo=UTC)  # 22:00 New York on 27 Nov
    assert window_key_and_start("NY_SESSION", late_ny)[0] == "2026-11-27"
    assert window_key_and_start("UTC_DAY", late_ny)[0] == "2026-11-28"
    before_dst, after_dst = (
        datetime(2026, 3, 6, 15, tzinfo=UTC),
        datetime(2026, 3, 9, 15, tzinfo=UTC),
    )
    assert window_key_and_start("NY_SESSION", before_dst)[1].hour == 5  # EST midnight = 05:00 UTC
    assert window_key_and_start("NY_SESSION", after_dst)[1].hour == 4  # EDT midnight = 04:00 UTC


def test_drawdown_and_capital_floor(sqlite_db: Database, policy: LoadedPolicy) -> None:
    t0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    _evaluate(sqlite_db, policy, t0, "50", windows=())
    assert _evaluate(sqlite_db, policy, t0 + timedelta(days=3), "45", windows=()) == [
        "CAPITAL_FLOOR",
        "DRAWDOWN",
    ]
