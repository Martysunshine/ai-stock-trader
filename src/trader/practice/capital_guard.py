"""PreToolUse guard for the assistant's Capital.com DEMO orders (ADR-0044, ADR-0049).

Claude Code runs ``python -m trader.practice.capital_guard`` before every Capital.com order
preview (``.claude/settings.json``). It reads the hook input (JSON with ``tool_name`` and
``tool_input``) from stdin, and blocks the call with exit code 2 and a reason on stderr unless:

* the epic is a practice epic and the size is inside its practice range;
* the order carries a stop (level, distance or amount);
* a PLAN for this epic, direction and size was journalled within the plan window, not yet opened
  or cancelled, and a given stop level equals the planned stop;
* fewer than the maximum practice trades are open and fewer were opened today.

Two sleeves trade on the demo account, each with its own journal: CFDs and crypto (BTCUSD,
ETHUSD). An order is checked against the sleeve that owns its epic. Kraken simulator orders are
blocked: that venue is retired.

An order update needs an AMEND journalled within the window. Execution needs a preview ID, so
guarding the previews guards every order. capital-mcp 3207be4 reports but does not enforce its
own position caps, which is why this guard exists. Any error blocks (fail closed).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TextIO

from trader.ops.clock import SystemClock
from trader.practice.journal import VENUES, JournalError, PracticeJournal

PREFIX = "mcp__capital__"
NEW_ORDER_TOOLS = frozenset({"cap_trade_preview_position", "cap_trade_preview_working_order"})
UPDATE_TOOLS = frozenset({"cap_trade_preview_working_order_update"})
KRAKEN_PREFIX = "mcp__kraken__"
KRAKEN_BUY, KRAKEN_SELL = "kraken_paper_buy", "kraken_paper_sell"


class Blocked(Exception):
    """The order breaks a practice rule."""


def _dec(value: Any, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise Blocked(f"{what} {value!r} is not a number") from exc


def check(
    tool_name: str, tool_input: Mapping[str, Any], journal: PracticeJournal, now: datetime
) -> None:
    """Raise :class:`Blocked` when the call breaks a practice rule; return for allowed calls."""
    tool = tool_name.removeprefix(PREFIX)
    since = journal.window_start(now)
    if tool in UPDATE_TOOLS:
        if not journal.recent_amend(since):
            raise Blocked("journal an AMEND (trader journal add AMEND) before updating an order")
        return
    if tool not in NEW_ORDER_TOOLS:
        return
    epic = str(tool_input.get("epic", ""))
    try:
        rule = journal.rules.epic(epic)
    except JournalError as exc:
        raise Blocked(str(exc)) from exc
    size = _dec(tool_input.get("size"), "size")
    if not rule.min_size <= size <= rule.max_size:
        raise Blocked(
            f"{epic} size {size} outside the practice range {rule.min_size}..{rule.max_size}"
        )
    stops = [tool_input.get(k) for k in ("stop_level", "stop_distance", "stop_amount")]
    if all(v is None for v in stops):
        raise Blocked(
            "every practice order needs a stop (stop_level, stop_distance or stop_amount)"
        )
    plan = journal.latest_plan(epic, since)
    if plan is None:
        raise Blocked(
            f"no PLAN for {epic} in the last {journal.rules.plan_window_minutes} minutes: "
            "journal one first (trader journal add PLAN)"
        )
    p = plan["payload"]
    if str(tool_input.get("direction", "")).upper() != p["direction"]:
        raise Blocked(f"direction differs from PLAN {p['trade_id']} ({p['direction']})")
    if size != Decimal(p["size"]):
        raise Blocked(f"size {size} differs from PLAN {p['trade_id']} ({p['size']})")
    level = tool_input.get("stop_level")
    if level is not None and _dec(level, "stop_level") != Decimal(p["stop"]):
        raise Blocked(f"stop_level {level} differs from PLAN {p['trade_id']} stop ({p['stop']})")
    # a chat's orders count as one setup of their own; the trader's trades count separately
    # (ADR-0056), but every trade counts toward the whole sleeve's limit
    if len(journal.open_trades_of(None)) >= journal.rules.open_trades_per_setup:
        raise Blocked(f"{journal.rules.open_trades_per_setup} practice trades are already open")
    if len(journal.open_trades()) >= journal.rules.max_open_trades:
        raise Blocked(f"the sleeve's {journal.rules.max_open_trades} open trades are all in use")
    if journal.opens_on(now.date(), None) >= journal.rules.max_opens_per_utc_day:
        raise Blocked(f"{journal.rules.max_opens_per_utc_day} practice trades were opened today")
    conflict = journal.group_conflict(epic)
    if conflict is not None:
        raise Blocked(
            f"{epic} is in the group {conflict[0]} with the open trade {conflict[1]}: "
            "one open trade per group"
        )


def check_kraken(tool_name: str) -> None:
    """The Kraken paper simulator takes no more orders (ADR-0049): the crypto sleeve trades
    Bitcoin and Ether as Capital.com demo CFDs, because a 1.6% round trip cannot be earned back
    by a trade of a few days."""
    if tool_name.removeprefix(KRAKEN_PREFIX) in (KRAKEN_BUY, KRAKEN_SELL):
        raise Blocked(
            "the Kraken simulator is retired for the crypto sleeve (ADR-0049): plan BTCUSD or "
            "ETHUSD on Capital.com demo (trader journal add PLAN --venue crypto-cfd)"
        )


def route(
    tool_name: str,
    tool_input: Mapping[str, Any],
    journals: Sequence[PracticeJournal],
    now: datetime,
) -> None:
    """Check a Capital.com order against the sleeve that owns its epic: the CFD sleeve or the
    crypto sleeve, each with its own journal, limits and open-trade count. An order update needs
    a recent AMEND in either journal."""
    tool = tool_name.removeprefix(PREFIX)
    if tool in UPDATE_TOOLS:
        if not any(j.recent_amend(j.window_start(now)) for j in journals):
            raise Blocked("journal an AMEND (trader journal add AMEND) before updating an order")
        return
    if tool not in NEW_ORDER_TOOLS:
        return
    epic = str(tool_input.get("epic", ""))
    owner = next((j for j in journals if epic in j.rules.epics), None)
    if owner is None:
        raise Blocked(f"{epic} is not a practice epic")
    check(tool_name, tool_input, owner, now)


def main(stdin: TextIO = sys.stdin, stderr: TextIO = sys.stderr) -> int:
    try:
        event = json.loads(stdin.read() or "{}")
        tool_name = str(event.get("tool_name", ""))
        tool_input = event.get("tool_input") or {}
        now = SystemClock().now()
        if tool_name.startswith(KRAKEN_PREFIX):
            check_kraken(tool_name)
        else:
            route(tool_name, tool_input, [PracticeJournal(venue=v) for v in VENUES], now)
    except Blocked as exc:
        print(f"practice guard blocked the order: {exc}", file=stderr)
        return 2
    except Exception as exc:  # fail closed: an unreadable journal or rules file blocks the order
        print(f"practice guard failed closed: {type(exc).__name__}: {exc}", file=stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
