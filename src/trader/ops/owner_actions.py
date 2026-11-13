"""Owner-facing pause/resume actions with recorded authorship (architecture.md §9).

Loss-breach resumes need a reviewer and a written note, are persisted in ``owner_reviews`` and
are refused while the breach condition still holds in the current window.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.accounting.equity import account_equity, portfolio_equity
from trader.accounting.fees import FeeSchedule
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.config.policy import LoadedPolicy
from trader.ops.pause import PauseCategory, PauseFlag, PauseService, ResumeRefused, Scope
from trader.risk.loss_limits import LossMonitor
from trader.storage.db import Database


@dataclass(frozen=True)
class ResumeOutcome:
    cleared: list[str]
    refused: list[str]


def current_loss_breaches(
    *,
    db: Database,
    ledger: Ledger,
    policy: LoadedPolicy,
    portfolio_id: str,
    account_id: str,
    currency: str,
    fees: FeeSchedule | None,
    marks: Mapping[str, Decimal],
    now: datetime,
    ny_windows: tuple[str, ...],
) -> set[str]:
    """Causes of loss breaches that still hold now (evaluated without persisting anything)."""
    monitor = LossMonitor(policy.policy, PauseService())
    with db.tx() as conn:
        acc = account_equity(
            conn, ledger, account_id=account_id, currency=currency, marks=marks, fees=fees
        )
        pe = portfolio_equity(
            [acc],
            fx=FxBook("USD"),
            now=now,
            initial_capital_usd=policy.policy.initial_trading_capital,
        )
        causes: set[str] = set()
        if ny_windows:
            st = monitor.evaluate(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=account_id,
                portfolio_id=portfolio_id,
                windows=ny_windows,
                now=now,
                equity=acc.conservative_equity,
                cum_external_flows=acc.net_external_flows,
                initial_capital=policy.policy.initial_trading_capital,
                record=False,
            )
            causes |= {b.cause.value for b in st.breaches}
        st = monitor.evaluate(
            conn,
            scope=Scope.PORTFOLIO,
            scope_id=portfolio_id,
            portfolio_id=portfolio_id,
            windows=("UTC_DAY", "UTC_WEEK"),
            now=now,
            equity=pe.conservative_equity_usd,
            cum_external_flows=pe.net_external_flows_usd,
            initial_capital=policy.policy.initial_trading_capital,
            record=False,
        )
        causes |= {b.cause.value for b in st.breaches}
    return causes


def resume_loss_breaches(
    *,
    db: Database,
    pauses: PauseService,
    policy: LoadedPolicy,
    portfolio_id: str,
    reviewer: str,
    note: str,
    still_breached: set[str],
    figures: dict[str, str],
    now: datetime,
) -> ResumeOutcome:
    cleared: list[str] = []
    refused: list[str] = []
    with db.tx() as conn:
        flags: list[PauseFlag] = [
            f
            for f in pauses.active_flags(conn, portfolio_id=portfolio_id)
            if f.category is PauseCategory.LOSS_BREACH
        ]
    for flag in flags:
        try:
            with db.tx() as conn:
                pauses.resume_loss_breach(
                    conn,
                    flag_id=flag.flag_id,
                    reviewer=reviewer,
                    note=note,
                    figures=figures,
                    policy_hash=policy.policy_hash,
                    breach_still_active=flag.cause.value in still_breached,
                    now=now,
                )
            cleared.append(flag.flag_id)
        except ResumeRefused as exc:
            refused.append(f"{flag.cause.value}: {exc}")
    return ResumeOutcome(cleared=cleared, refused=refused)
