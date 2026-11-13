"""Loss windows, high-water drawdown and the capital floor (risk_and_validation.md §1).

* Daily/weekly loss = window start equity + external flows in the window - current conservative
  equity, compared with a fraction of the start equity.
* Stock accounts use New York session/ISO-week windows; the portfolio additionally uses a UTC day
  (00:00) and a Monday-00:00-UTC week so 24/7 crypto and weekend exposure are covered (CRYPTO §3).
  Every applicable window must pass.
* Drawdown uses cash-flow-adjusted equity against its persisted high-water mark; the USD 45 floor
  uses the same adjusted equity. Whichever triggers first pauses entries.
* All counters derive from persisted rows; restarts and venue/track changes never reset them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import Connection, desc, insert, select, update

from trader.config.policy import RiskPolicy
from trader.data.calendars import NEW_YORK
from trader.domain.types import ZERO, require_utc
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.storage.schema import equity_snapshots, loss_windows

WINDOW_CAUSE = {
    "NY_SESSION": PauseCause.DAILY_NY,
    "NY_WEEK": PauseCause.WEEKLY_NY,
    "UTC_DAY": PauseCause.DAILY_UTC,
    "UTC_WEEK": PauseCause.WEEKLY_UTC,
}


def window_key_and_start(kind: str, now: datetime) -> tuple[str, datetime]:
    now = require_utc(now)
    if kind == "UTC_DAY":
        start = datetime.combine(now.date(), time(0), tzinfo=UTC)
        return now.date().isoformat(), start
    if kind == "UTC_WEEK":
        monday = now.date() - timedelta(days=now.weekday())
        iso = monday.isocalendar()
        return f"{iso.year}-W{iso.week:02d}", datetime.combine(monday, time(0), tzinfo=UTC)
    local = now.astimezone(NEW_YORK)
    if kind == "NY_SESSION":
        start = datetime.combine(local.date(), time(0), tzinfo=NEW_YORK).astimezone(UTC)
        return local.date().isoformat(), start
    if kind == "NY_WEEK":
        monday = local.date() - timedelta(days=local.weekday())
        iso = monday.isocalendar()
        start = datetime.combine(monday, time(0), tzinfo=NEW_YORK).astimezone(UTC)
        return f"{iso.year}-W{iso.week:02d}", start
    raise ValueError(f"unknown loss window {kind}")


@dataclass(frozen=True, slots=True)
class Breach:
    cause: PauseCause
    window_key: str
    loss_usd: Decimal
    limit_usd: Decimal
    detail: str


@dataclass(frozen=True, slots=True)
class LossStatus:
    breaches: tuple[Breach, ...]
    window_losses: dict[str, Decimal]
    drawdown_fraction: Decimal
    adjusted_equity: Decimal
    high_water_mark: Decimal


class LossMonitor:
    def __init__(self, policy: RiskPolicy, pauses: PauseService) -> None:
        self.policy = policy
        self.pauses = pauses

    def _limit_fraction(self, kind: str) -> Decimal:
        if kind in ("NY_SESSION", "UTC_DAY"):
            return self.policy.loss_limits.daily_loss_fraction
        return self.policy.loss_limits.weekly_loss_fraction

    def _window_row(
        self,
        conn: Connection,
        *,
        scope: Scope,
        scope_id: str,
        kind: str,
        now: datetime,
        equity: Decimal,
        flows: Decimal,
        record: bool,
    ) -> tuple[str, Decimal, Decimal, datetime | None]:
        key, start = window_key_and_start(kind, now)
        row = conn.execute(
            select(loss_windows).where(
                loss_windows.c.scope == scope.value,
                loss_windows.c.scope_id == scope_id,
                loss_windows.c.kind == kind,
                loss_windows.c.window_key == key,
            )
        ).first()
        if row is not None:
            return key, Decimal(row.start_equity), Decimal(row.start_flows), row.breached_at
        prior = conn.execute(
            select(equity_snapshots.c.equity, equity_snapshots.c.cum_external_flows)
            .where(
                equity_snapshots.c.scope_id == scope_id,
                equity_snapshots.c.scope == scope.value,
                equity_snapshots.c.ts <= start,
            )
            .order_by(desc(equity_snapshots.c.ts), desc(equity_snapshots.c.id))
            .limit(1)
        ).first()
        start_equity, start_flows = equity, flows
        if prior is not None and Decimal(prior.equity) > equity:
            # Conservative when the process was down across the boundary: the higher start
            # equity makes any loss in the window larger, never smaller.
            start_equity, start_flows = Decimal(prior.equity), Decimal(prior.cum_external_flows)
        if not record:
            return key, start_equity, start_flows, None
        conn.execute(
            insert(loss_windows).values(
                scope=scope.value,
                scope_id=scope_id,
                kind=kind,
                window_key=key,
                start_time=now,
                start_equity=start_equity,
                start_flows=start_flows,
                breached_at=None,
            )
        )
        return key, start_equity, start_flows, None

    def evaluate(
        self,
        conn: Connection,
        *,
        scope: Scope,
        scope_id: str,
        portfolio_id: str,
        windows: tuple[str, ...],
        now: datetime,
        equity: Decimal,
        cum_external_flows: Decimal,
        initial_capital: Decimal,
        record: bool = True,
    ) -> LossStatus:
        """Evaluate every window, persist the snapshot and raise loss-breach pauses."""
        breaches: list[Breach] = []
        losses: dict[str, Decimal] = {}
        for kind in windows:
            key, start_equity, start_flows, breached_at = self._window_row(
                conn,
                scope=scope,
                scope_id=scope_id,
                kind=kind,
                now=now,
                equity=equity,
                flows=cum_external_flows,
                record=record,
            )
            loss = start_equity + (cum_external_flows - start_flows) - equity
            limit = self._limit_fraction(kind) * start_equity
            losses[kind] = loss
            if loss >= limit and limit > 0:
                breaches.append(
                    Breach(
                        cause=WINDOW_CAUSE[kind],
                        window_key=key,
                        loss_usd=loss,
                        limit_usd=limit,
                        detail=f"{kind} {key}: loss {loss:.4f} >= limit {limit:.4f} USD",
                    )
                )
                if breached_at is None and record:
                    conn.execute(
                        update(loss_windows)
                        .where(
                            loss_windows.c.scope == scope.value,
                            loss_windows.c.scope_id == scope_id,
                            loss_windows.c.kind == kind,
                            loss_windows.c.window_key == key,
                        )
                        .values(breached_at=now)
                    )

        adjusted = equity - (cum_external_flows - initial_capital)
        last = conn.execute(
            select(equity_snapshots.c.high_water_mark, equity_snapshots.c.equity)
            .where(equity_snapshots.c.scope == scope.value, equity_snapshots.c.scope_id == scope_id)
            .order_by(desc(equity_snapshots.c.ts), desc(equity_snapshots.c.id))
            .limit(1)
        ).first()
        prev_hwm = Decimal(last.high_water_mark) if last is not None else initial_capital
        hwm = max(prev_hwm, adjusted)
        drawdown = (hwm - adjusted) / hwm if hwm > 0 else ZERO
        ll = self.policy.loss_limits
        if drawdown >= ll.max_high_water_drawdown_fraction:
            breaches.append(
                Breach(
                    cause=PauseCause.DRAWDOWN,
                    window_key="hwm",
                    loss_usd=hwm - adjusted,
                    limit_usd=ll.max_high_water_drawdown_fraction * hwm,
                    detail=f"drawdown {drawdown:.4%} from high-water mark {hwm:.4f}",
                )
            )
        if adjusted <= ll.initial_capital_pause_floor:
            breaches.append(
                Breach(
                    cause=PauseCause.CAPITAL_FLOOR,
                    window_key="floor",
                    loss_usd=initial_capital - adjusted,
                    limit_usd=initial_capital - ll.initial_capital_pause_floor,
                    detail=f"cash-flow-adjusted equity {adjusted:.4f} <= floor {ll.initial_capital_pause_floor}",
                )
            )
        if record:
            if last is None or Decimal(last.equity) != equity or prev_hwm != hwm:
                conn.execute(
                    insert(equity_snapshots).values(
                        scope=scope.value,
                        scope_id=scope_id,
                        ts=now,
                        equity=equity,
                        cum_external_flows=cum_external_flows,
                        high_water_mark=hwm,
                    )
                )
            for breach in breaches:
                self.pauses.raise_flag(
                    conn,
                    scope=Scope.PORTFOLIO,
                    scope_id=portfolio_id,
                    cause=breach.cause,
                    details=breach.detail,
                    now=now,
                )
        return LossStatus(
            breaches=tuple(breaches),
            window_losses=losses,
            drawdown_fraction=drawdown,
            adjusted_equity=adjusted,
            high_water_mark=hwm,
        )

    def cause_still_breached(self, status: LossStatus, cause: PauseCause) -> bool:
        return any(b.cause is cause for b in status.breaches)
