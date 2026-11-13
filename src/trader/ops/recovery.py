"""Startup recovery (architecture.md §9).

On every start: raise ``RECOVERY_PENDING`` (entries disabled), acquire account leadership, let
expired approvals lapse (``NOT_SENT``), resolve unknown/submitting intents by client order ID and
run a full reconciliation. Only a PASSED reconciliation clears health flags - including
``RECOVERY_PENDING`` - and never a loss-breach flag. A discrepancy keeps the worker paused rather
than relaunching into an unresolved position/order state.
"""

from __future__ import annotations

from typing import Protocol

from trader.execution._gateway import SubmissionGateway
from trader.execution.reconciliation import Reconciler, ReconciliationResult
from trader.ops.clock import Clock
from trader.ops.leadership import LeadershipManager
from trader.ops.pause import PauseCause, PauseService, Scope
from trader.storage.db import Database


class _Env(Protocol):
    @property
    def db(self) -> Database: ...
    @property
    def clock(self) -> Clock: ...
    @property
    def pauses(self) -> PauseService: ...
    @property
    def leadership(self) -> dict[str, LeadershipManager]: ...
    @property
    def gateway(self) -> SubmissionGateway: ...
    @property
    def reconciler(self) -> Reconciler: ...


def startup_recovery(env: _Env, account_ids: list[str]) -> dict[str, ReconciliationResult]:
    now = env.clock.now()
    with env.db.tx() as conn:
        for account_id in account_ids:
            env.pauses.raise_flag(
                conn,
                scope=Scope.ACCOUNT,
                scope_id=account_id,
                cause=PauseCause.RECOVERY_PENDING,
                details="startup: new entries disabled until reconciliation passes",
                now=now,
            )
    for account_id in account_ids:
        env.leadership[account_id].acquire()
    # Entry approvals persisted before a crash but never sent lapse (NOT_SENT) and release their
    # reservation: entries stay disabled until recovery completes, and a fresh decision is made
    # later. Pending risk-reducing exits are still sent.
    env.gateway.abandon_pending_entries("startup recovery")
    env.gateway.submit_pending()
    return {
        account_id: env.reconciler.reconcile(account_id, cleared_by="startup-recovery")
        for account_id in account_ids
    }
