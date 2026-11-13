"""Durable pause flags (risk_and_validation.md §1, §4; architecture.md §9).

* ``LOSS_BREACH`` – cleared only by a recorded owner review, and refused while the breach still
  holds in the current window. Never automatic.
* ``HEALTH`` – cleared only by a PASSED reconciliation run that finished after the flag was
  raised. A remaining loss flag keeps entries blocked regardless.
* ``OWNER`` – an explicit owner pause, cleared by an owner resume.

Pauses block **new exposure only**. Risk-reducing exits are never blocked by any flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Connection, and_, insert, or_, select, update

from trader.domain.hashing import canonical_json
from trader.domain.ids import deterministic_id
from trader.domain.types import DomainError
from trader.storage.schema import owner_reviews, pause_state, reconciliation_runs


class PauseCategory(StrEnum):
    LOSS_BREACH = "LOSS_BREACH"
    HEALTH = "HEALTH"
    OWNER = "OWNER"


class PauseCause(StrEnum):
    DAILY_NY = "DAILY_NY"
    WEEKLY_NY = "WEEKLY_NY"
    DAILY_UTC = "DAILY_UTC"
    WEEKLY_UTC = "WEEKLY_UTC"
    DRAWDOWN = "DRAWDOWN"
    CAPITAL_FLOOR = "CAPITAL_FLOOR"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    UNRESOLVED_ORDER = "UNRESOLVED_ORDER"
    LEADERSHIP_LOST = "LEADERSHIP_LOST"
    STALE_ACCOUNT = "STALE_ACCOUNT"
    FEED_GAP = "FEED_GAP"
    FEED_MISMATCH = "FEED_MISMATCH"
    CLOCK_SKEW = "CLOCK_SKEW"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    CAPABILITY_CHANGED = "CAPABILITY_CHANGED"
    MISSING_PROTECTION = "MISSING_PROTECTION"
    ABNORMAL_PATTERN = "ABNORMAL_PATTERN"
    UNSOLICITED_EXECUTION = "UNSOLICITED_EXECUTION"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    OWNER_REQUESTED = "OWNER_REQUESTED"


LOSS_CAUSES = frozenset(
    {
        PauseCause.DAILY_NY,
        PauseCause.WEEKLY_NY,
        PauseCause.DAILY_UTC,
        PauseCause.WEEKLY_UTC,
        PauseCause.DRAWDOWN,
        PauseCause.CAPITAL_FLOOR,
    }
)


def category_of(cause: PauseCause) -> PauseCategory:
    if cause in LOSS_CAUSES:
        return PauseCategory.LOSS_BREACH
    if cause is PauseCause.OWNER_REQUESTED:
        return PauseCategory.OWNER
    return PauseCategory.HEALTH


class Scope(StrEnum):
    PORTFOLIO = "PORTFOLIO"
    ACCOUNT = "ACCOUNT"


@dataclass(frozen=True, slots=True)
class PauseFlag:
    flag_id: str
    scope: Scope
    scope_id: str
    category: PauseCategory
    cause: PauseCause
    raised_at: datetime
    details: str


class ResumeRefused(DomainError):
    pass


def _flag(row: object) -> PauseFlag:
    r = row._mapping  # type: ignore[attr-defined]
    return PauseFlag(
        flag_id=r["flag_id"],
        scope=Scope(r["scope"]),
        scope_id=r["scope_id"],
        category=PauseCategory(r["category"]),
        cause=PauseCause(r["cause"]),
        raised_at=r["raised_at"],
        details=r["details"],
    )


class PauseService:
    # ------------------------------------------------------------------ raise
    def raise_flag(
        self,
        conn: Connection,
        *,
        scope: Scope,
        scope_id: str,
        cause: PauseCause,
        details: str,
        now: datetime,
    ) -> str:
        """Persist a pause. Idempotent while an identical active flag exists."""
        existing = conn.execute(
            select(pause_state.c.flag_id).where(
                pause_state.c.scope == scope.value,
                pause_state.c.scope_id == scope_id,
                pause_state.c.cause == cause.value,
                pause_state.c.active.is_(True),
            )
        ).first()
        if existing is not None:
            return str(existing.flag_id)
        flag_id = deterministic_id("flag", scope.value, scope_id, cause.value, now.isoformat())
        conn.execute(
            insert(pause_state).values(
                flag_id=flag_id,
                scope=scope.value,
                scope_id=scope_id,
                category=category_of(cause).value,
                cause=cause.value,
                raised_at=now,
                details=details,
                active=True,
            )
        )
        return flag_id

    # ------------------------------------------------------------------ query
    def active_flags(
        self, conn: Connection, *, portfolio_id: str, account_id: str | None = None
    ) -> list[PauseFlag]:
        scope_filter = and_(
            pause_state.c.scope == Scope.PORTFOLIO.value, pause_state.c.scope_id == portfolio_id
        )
        if account_id is not None:
            scope_filter = or_(
                scope_filter,
                and_(
                    pause_state.c.scope == Scope.ACCOUNT.value,
                    pause_state.c.scope_id == account_id,
                ),
            )
        rows = conn.execute(
            select(pause_state)
            .where(pause_state.c.active.is_(True), scope_filter)
            .order_by(pause_state.c.raised_at, pause_state.c.flag_id)
        ).all()
        return [_flag(r) for r in rows]

    def all_active(self, conn: Connection) -> list[PauseFlag]:
        rows = conn.execute(
            select(pause_state)
            .where(pause_state.c.active.is_(True))
            .order_by(pause_state.c.raised_at, pause_state.c.flag_id)
        ).all()
        return [_flag(r) for r in rows]

    # ------------------------------------------------------------------ clear
    def clear_health_flags(
        self,
        conn: Connection,
        *,
        scope_ids: list[str],
        reconciliation_run_id: str,
        cleared_by: str,
        now: datetime,
    ) -> list[str]:
        """Clear HEALTH flags covered by a PASSED reconciliation run finished after them."""
        run = conn.execute(
            select(reconciliation_runs).where(reconciliation_runs.c.run_id == reconciliation_run_id)
        ).first()
        if run is None or run.status != "PASSED":
            raise ResumeRefused("health flags clear only after a PASSED reconciliation run")
        rows = conn.execute(
            select(pause_state).where(
                pause_state.c.active.is_(True),
                pause_state.c.category == PauseCategory.HEALTH.value,
                pause_state.c.scope_id.in_(scope_ids),
                pause_state.c.raised_at <= run.started_at,
            )
        ).all()
        cleared = []
        for row in rows:
            conn.execute(
                update(pause_state)
                .where(pause_state.c.flag_id == row.flag_id)
                .values(
                    active=False,
                    cleared_at=now,
                    cleared_by=cleared_by,
                    clear_evidence=reconciliation_run_id,
                )
            )
            cleared.append(str(row.flag_id))
        return cleared

    def owner_pause(
        self, conn: Connection, *, portfolio_id: str, reason: str, by: str, now: datetime
    ) -> str:
        return self.raise_flag(
            conn,
            scope=Scope.PORTFOLIO,
            scope_id=portfolio_id,
            cause=PauseCause.OWNER_REQUESTED,
            details=f"{reason} (by {by})",
            now=now,
        )

    def owner_resume(self, conn: Connection, *, portfolio_id: str, by: str, now: datetime) -> int:
        result = conn.execute(
            update(pause_state)
            .where(
                pause_state.c.active.is_(True),
                pause_state.c.category == PauseCategory.OWNER.value,
                pause_state.c.scope_id == portfolio_id,
            )
            .values(active=False, cleared_at=now, cleared_by=by, clear_evidence="owner-resume")
        )
        return int(result.rowcount)

    def resume_loss_breach(
        self,
        conn: Connection,
        *,
        flag_id: str,
        reviewer: str,
        note: str,
        figures: dict[str, str],
        policy_hash: str,
        breach_still_active: bool,
        now: datetime,
    ) -> str:
        """Clear one loss-breach flag after a recorded owner review."""
        row = conn.execute(select(pause_state).where(pause_state.c.flag_id == flag_id)).first()
        if row is None or not row.active:
            raise ResumeRefused(f"no active pause flag {flag_id}")
        if row.category != PauseCategory.LOSS_BREACH.value:
            raise ResumeRefused("this command clears loss-breach flags only")
        if len(note.strip()) < 10 or not reviewer.strip():
            raise ResumeRefused(
                "a loss-breach resume requires a reviewer and a written review note"
            )
        if breach_still_active:
            raise ResumeRefused(
                "the loss condition still holds in the current window; resuming would re-trigger "
                "immediately (wait for the window to roll, or change capital/policy deliberately)"
            )
        review_id = deterministic_id("review", flag_id, now.isoformat())
        conn.execute(
            insert(owner_reviews).values(
                review_id=review_id,
                flag_id=flag_id,
                reviewer=reviewer,
                note=note,
                reviewed_at=now,
                figures_json=canonical_json(figures),
                policy_hash=policy_hash,
            )
        )
        conn.execute(
            update(pause_state)
            .where(pause_state.c.flag_id == flag_id)
            .values(active=False, cleared_at=now, cleared_by=reviewer, clear_evidence=review_id)
        )
        return review_id
