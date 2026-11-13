"""Operational rejections in a paper session, surfaced at once (ADR-0043).

A rejection is *operational* when the system, not the market, stopped an entry: a pause, stale
or mismatched data, a missing capability, a configuration that forbids the product or the mode,
unknown costs, an account rule, missing funds, or the per-session entry limit while the strategy
itself has entered nothing (on 2026-11-09 certification orders had used up that allowance).
Market and risk outcomes - no edge, too costly, risk limit, spread too wide, size below the
minimum, outside the entry window - are the risk engine doing its job and are only counted.

:class:`SessionWatch` keeps the per-session counts. ``PaperRunner`` logs a ``!!`` line for every
bar with operational rejections, writes at most two ``ENTRIES_BLOCKED`` alerts per session (the
first occurrence and a summary at the close) and prints a session summary line.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from trader.domain.candidates import DecisionRecord, RejectReason

#: Reasons that always mean "the system could not trade", whatever the market did.
ALWAYS_OPERATIONAL = frozenset(
    {
        RejectReason.PAUSED,
        RejectReason.STALE_DATA,
        RejectReason.FEED_MISMATCH,
        RejectReason.CAPABILITY,
        RejectReason.LEADERSHIP,
        RejectReason.MODE_NOT_PERMITTED,
        RejectReason.FORBIDDEN_PRODUCT,
        RejectReason.UNKNOWN_COSTS,
        RejectReason.ACCOUNT_RULE,
        RejectReason.INSUFFICIENT_FUNDS,
    }
)
ALERT_CODE = "ENTRIES_BLOCKED"


def operational_reasons(
    decision: DecisionRecord, strategy_entries: int | None
) -> tuple[RejectReason, ...]:
    """The operational reasons of a rejected decision (none for an approved one).

    ``strategy_entries`` is the number of strategy entries already sent in this session; the
    entry limit is operational only while it is 0 (``None`` = not known, treated as 0).
    """
    if decision.approved:
        return ()
    out = [r for r in decision.reasons if r in ALWAYS_OPERATIONAL]
    if RejectReason.ENTRY_LIMIT_REACHED in decision.reasons and not strategy_entries:
        out.append(RejectReason.ENTRY_LIMIT_REACHED)
    return tuple(out)


@dataclass
class SessionWatch:
    session_date: date
    candidates: int = 0
    approved: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    operational: Counter[str] = field(default_factory=Counter)
    first_detail: str | None = None
    alerts_written: int = 0

    def observe(
        self, decisions: Iterable[DecisionRecord], strategy_entries: int | None
    ) -> tuple[RejectReason, ...]:
        """Count a bar's decisions; returns the distinct operational reasons seen in them."""
        seen: list[RejectReason] = []
        for d in decisions:
            self.candidates += 1
            if d.approved:
                self.approved += 1
                continue
            for r in d.reasons:
                self.reasons[r.value] += 1
            for r in operational_reasons(d, strategy_entries):
                self.operational[r.value] += 1
                if r not in seen:
                    seen.append(r)
                if self.first_detail is None:
                    detail = "; ".join(d.details) or r.value
                    self.first_detail = f"{d.instrument_id}: {detail}"[:300]
        return tuple(seen)

    @property
    def blocked(self) -> bool:
        return bool(self.operational)

    def bar_line(self, reasons: tuple[RejectReason, ...], strategy_entries: int | None) -> str:
        counts = ", ".join(f"{r.value} x{self.operational[r.value]}" for r in reasons)
        return (
            f"!! entries blocked (operational): {counts} - {self.first_detail}; "
            f"strategy entries this session {strategy_entries if strategy_entries is not None else '?'}"
        )

    def summary_line(self, strategy_entries: int | None) -> str:
        return (
            f"session {self.session_date} summary: candidates {self.candidates}, approved "
            f"{self.approved}, strategy entries "
            f"{strategy_entries if strategy_entries is not None else '?'}, rejections "
            f"{dict(sorted(self.reasons.items())) or 'none'}, operational "
            f"{dict(sorted(self.operational.items())) or 'none'}"
        )
