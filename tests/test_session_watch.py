"""Operational rejections are told apart from market and risk outcomes (ADR-0043)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trader.domain.candidates import DecisionRecord, EdgeEvidence, RejectReason
from trader.domain.types import Track
from trader.runtime.session_watch import SessionWatch, operational_reasons

NOW = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)


def _decision(*reasons: RejectReason, approved: bool = False, n: int = 1) -> DecisionRecord:
    return DecisionRecord(
        decision_id=f"d{n}",
        candidate_id=f"c{n}",
        instrument_id="XNAS:NVDA",
        track=Track.STOCKS,
        decided_at=NOW,
        approved=approved,
        reasons=reasons,
        details=tuple(f"{r.value} detail" for r in reasons),
        policy_hash="p",
        model_hash="practice:planned-target:v1",
        evidence=EdgeEvidence.PRACTICE,
        quantity=Decimal("0.1") if approved else Decimal(0),
        intent_id=f"i{n}" if approved else None,
        entry_limit_price=None,
        notional_usd=Decimal(0),
        planned_loss_usd=Decimal(0),
        estimated_costs_usd=Decimal(0),
        expected_net_usd=Decimal(0),
        effective_sizing_equity_usd=Decimal(1000),
        binding_constraint="",
    )


@pytest.mark.parametrize(
    "reason",
    [
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
    ],
)
def test_system_reasons_are_operational(reason: RejectReason) -> None:
    assert operational_reasons(_decision(reason), strategy_entries=3) == (reason,)


@pytest.mark.parametrize(
    "reason",
    [
        RejectReason.NO_EDGE,
        RejectReason.TOO_COSTLY,
        RejectReason.RISK_LIMIT,
        RejectReason.SPREAD_TOO_WIDE,
        RejectReason.SIZE_BELOW_MINIMUM,
        RejectReason.OUTSIDE_ENTRY_WINDOW,
    ],
)
def test_market_and_risk_outcomes_are_not_operational(reason: RejectReason) -> None:
    assert operational_reasons(_decision(reason), strategy_entries=0) == ()


def test_the_entry_limit_is_operational_only_before_any_strategy_entry() -> None:
    limit = _decision(RejectReason.ENTRY_LIMIT_REACHED)
    assert operational_reasons(limit, strategy_entries=0) == (RejectReason.ENTRY_LIMIT_REACHED,)
    assert operational_reasons(limit, strategy_entries=None) == (RejectReason.ENTRY_LIMIT_REACHED,)
    assert operational_reasons(limit, strategy_entries=4) == ()
    approved = _decision(approved=True)
    assert operational_reasons(approved, strategy_entries=0) == ()


def test_the_watch_counts_a_session_and_writes_its_lines() -> None:
    watch = SessionWatch(date(2026, 11, 10))
    seen = watch.observe(
        [
            _decision(RejectReason.ENTRY_LIMIT_REACHED, RejectReason.NO_EDGE, n=1),
            _decision(RejectReason.TOO_COSTLY, n=2),
            _decision(approved=True, n=3),
        ],
        strategy_entries=0,
    )
    assert seen == (RejectReason.ENTRY_LIMIT_REACHED,)
    assert watch.blocked and (watch.candidates, watch.approved) == (3, 1)
    assert watch.first_detail is not None and watch.first_detail.startswith("XNAS:NVDA: ")
    line = watch.bar_line(seen, 0)
    assert line.startswith("!! entries blocked (operational): ENTRY_LIMIT_REACHED x1 - ")
    assert line.endswith("strategy entries this session 0")
    assert watch.summary_line(0) == (
        "session 2026-11-10 summary: candidates 3, approved 1, strategy entries 0, rejections "
        "{'ENTRY_LIMIT_REACHED': 1, 'NO_EDGE': 1, 'TOO_COSTLY': 1}, operational "
        "{'ENTRY_LIMIT_REACHED': 1}"
    )
