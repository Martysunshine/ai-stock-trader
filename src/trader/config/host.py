"""Host-role guard (instructions.md §6.1).

"A live host must reject a paper-only configuration and vice versa." The role comes from the
``TRADER_HOST_ROLE`` environment variable and defaults to ``DEV``, which can never run live.
"""

from __future__ import annotations

import os
from enum import StrEnum

from trader.domain.types import Mode

HOST_ROLE_ENV = "TRADER_HOST_ROLE"


class HostRole(StrEnum):
    DEV = "DEV"
    PAPER = "PAPER"
    LIVE = "LIVE"


class HostRoleError(RuntimeError):
    pass


_ALLOWED: dict[HostRole, frozenset[Mode]] = {
    HostRole.DEV: frozenset({Mode.REPLAY, Mode.SHADOW, Mode.PAPER}),
    HostRole.PAPER: frozenset({Mode.REPLAY, Mode.SHADOW, Mode.PAPER}),
    HostRole.LIVE: frozenset({Mode.LIVE}),
}


def current_host_role(environ: dict[str, str] | None = None) -> HostRole:
    env = os.environ if environ is None else environ
    raw = env.get(HOST_ROLE_ENV, HostRole.DEV.value).strip().upper()
    try:
        return HostRole(raw)
    except ValueError as exc:
        raise HostRoleError(
            f"invalid {HOST_ROLE_ENV}={raw!r}; expected DEV, PAPER or LIVE"
        ) from exc


def check_host_role(role: HostRole, mode: Mode) -> None:
    if mode not in _ALLOWED[role]:
        raise HostRoleError(
            f"host role {role.value} refuses a {mode.value} configuration "
            f"(allowed: {sorted(m.value for m in _ALLOWED[role])})"
        )
