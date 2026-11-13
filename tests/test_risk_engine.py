"""Fail-closed admission (instructions.md §5 "Risk and execution"; architecture.md §6 step 7)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.policy import LoadedPolicy, load_policy
from trader.config.track_profiles import load_track_profiles
from trader.domain.account import RuleProfileKind
from trader.domain.candidates import (
    EdgeEvidence,
    RejectReason,
    ReturnBasis,
    SignalScore,
    TradeCandidate,
)
from trader.domain.market import Quote
from trader.domain.types import Direction, Mode, Track
from trader.ops.pause import PauseCategory, PauseCause, PauseFlag, Scope
from trader.risk.engine import EntryContext, approve_reducing_exit, evaluate_entry
from trader.risk.sizing import ExposureState

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 27, 15, 35, 15, 120000, tzinfo=UTC)
SPECS = load_instruments(ROOT / "configs/instruments/synthetic_equities.yaml")
CAPS = load_capabilities(ROOT / "configs/capabilities/replay.yaml")
PROFILES = load_track_profiles(ROOT / "configs/tracks")


def candidate(**kw: object) -> TradeCandidate:
    base = TradeCandidate(
        candidate_id="cand-1",
        strategy_id="candidate_a",
        strategy_version=1,
        spec_hash="h",
        instrument_id="XNYS:SYNA",
        track=Track.STOCKS,
        direction=Direction.LONG,
        feed="SYNTHETIC",
        bar_end=NOW - timedelta(seconds=15),
        decision_time=NOW - timedelta(seconds=14),
        reference_price=Decimal("101.21"),
        stop_price=Decimal("100.93"),
        target_price=Decimal("101.77"),
        max_hold_minutes=60,
        entry_limit_offset_bps=Decimal(5),
        entry_ttl_seconds=120,
        gap_allowance_fraction=Decimal("0.001"),
        snapshot_hash="s",
        expires_at=NOW + timedelta(seconds=45),
    )
    return base.model_copy(update=kw)


def score(**kw: object) -> SignalScore:
    base = SignalScore(
        candidate_id="cand-1",
        model_hash="fixture",
        expected_gross_return=Decimal("0.006"),
        uncertainty_margin=Decimal("0.0015"),
        basis=ReturnBasis.MID_TO_MID,
        evidence=EdgeEvidence.SYNTHETIC_FIXTURE,
        edge_source_hash="e",
    )
    return base.model_copy(update=kw)


def quote(
    bid: str = "101.20", ask: str = "101.24", feed: str = "SYNTHETIC", age_s: float = 0.0
) -> Quote:
    t = NOW - timedelta(seconds=age_s)
    return Quote(
        instrument_id="XNYS:SYNA",
        feed=feed,
        event_time=t,
        receipt_time=t,
        sequence=1,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(300),
        ask_size=Decimal(300),
    )


def ctx(policy: LoadedPolicy, **kw: object) -> EntryContext:
    base = EntryContext(
        mode=Mode.REPLAY,
        now=NOW,
        policy=policy,
        track_profile=PROFILES[Track.STOCKS],
        spec=SPECS["XNYS:SYNA"],
        capabilities=CAPS,
        fees=load_fee_schedule(ROOT / "configs/fees/replay_demo.yaml"),
        candidate=candidate(),
        score=score(),
        quote=quote(),
        fx_to_usd=Decimal(1),
        effective_equity_usd=Decimal(50),
        portfolio_spendable_usd=Decimal(50),
        account_spendable_usd=Decimal(50),
        exposure=ExposureState(),
        active_flags=(),
        entries_in_session=0,
        in_entry_window=True,
        halted=False,
        rule_profile=RuleProfileKind.UNVERIFIED,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_clean_context_is_approved_with_a_bounded_limit(policy: LoadedPolicy) -> None:
    d = evaluate_entry(ctx(policy))
    assert d.approved, d.details
    assert d.limit_price == Decimal("101.29")  # ask * (1 + 5 bps) rounded down to the tick


FLAG = PauseFlag(
    "f", Scope.PORTFOLIO, "pf", PauseCategory.LOSS_BREACH, PauseCause.DAILY_NY, NOW, "loss"
)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"active_flags": (FLAG,)}, RejectReason.PAUSED),
        ({"unknown_orders_outstanding": True}, RejectReason.PAUSED),
        ({"quote": None}, RejectReason.STALE_DATA),
        ({"quote": quote(age_s=3)}, RejectReason.STALE_DATA),
        ({"halted": True}, RejectReason.STALE_DATA),
        ({"fx_to_usd": None}, RejectReason.STALE_DATA),
        ({"clock_skew": timedelta(milliseconds=300)}, RejectReason.STALE_DATA),
        ({"account_snapshot_age": timedelta(seconds=6)}, RejectReason.STALE_DATA),
        ({"quote": quote(feed="IEX")}, RejectReason.FEED_MISMATCH),
        ({"quote": quote(bid="101.00", ask="101.24")}, RejectReason.SPREAD_TOO_WIDE),
        ({"in_entry_window": False}, RejectReason.OUTSIDE_ENTRY_WINDOW),
        ({"entries_in_session": 2}, RejectReason.ENTRY_LIMIT_REACHED),
        ({"exposure": ExposureState(pending_entries=1)}, RejectReason.ENTRY_LIMIT_REACHED),
        ({"exposure": ExposureState(open_positions=2)}, RejectReason.ENTRY_LIMIT_REACHED),
        ({"instrument_busy": True}, RejectReason.RISK_LIMIT),
        ({"score": None}, RejectReason.NO_EDGE),
        ({"score": score(candidate_id="other")}, RejectReason.NO_EDGE),
        ({"mode": Mode.PAPER}, RejectReason.MODE_NOT_PERMITTED),
        (
            {
                "capabilities": load_capabilities(
                    ROOT / "configs/capabilities/alpaca_documented.yaml"
                )
            },
            RejectReason.CAPABILITY,
        ),
    ],
)
def test_fail_closed_matrix(
    policy: LoadedPolicy, overrides: dict[str, object], reason: RejectReason
) -> None:
    d = evaluate_entry(ctx(policy, **overrides))
    assert not d.approved
    assert reason in d.reasons, d.details


def test_synthetic_evidence_is_refused_outside_replay(policy: LoadedPolicy) -> None:
    paper_policy = load_policy(ROOT / "configs/policy/base.yaml", mode=Mode.PAPER)
    d = evaluate_entry(ctx(paper_policy, mode=Mode.PAPER))
    assert RejectReason.NO_EDGE in d.reasons


def test_live_requires_live_confirmed_native_protection_and_verified_rules(
    policy: LoadedPolicy,
) -> None:
    live_policy = load_policy(ROOT / "configs/policy/base.yaml", mode=Mode.LIVE, live_enabled=True)
    d = evaluate_entry(
        ctx(live_policy, mode=Mode.LIVE, score=score(evidence=EdgeEvidence.VALIDATED_BUNDLE))
    )
    assert not d.approved
    assert RejectReason.NO_EXIT_PLAN in d.reasons
    assert RejectReason.ACCOUNT_RULE in d.reasons  # UNVERIFIED rule profile blocks live entries


def test_model_output_cannot_bypass_controls(policy: LoadedPolicy) -> None:
    normal = evaluate_entry(ctx(policy))
    huge = evaluate_entry(ctx(policy, score=score(expected_gross_return=Decimal("1000"))))
    assert normal.sizing and huge.sizing
    assert normal.sizing.quantity == huge.sizing.quantity
    with pytest.raises(ValueError):
        SignalScore.model_validate({**score().model_dump(), "expected_gross_return": "NaN"})
    with pytest.raises(ValueError):
        SignalScore.model_validate({**score().model_dump(), "quantity": "5"})
    paused = evaluate_entry(
        ctx(policy, score=score(expected_gross_return=Decimal("1000")), active_flags=(FLAG,))
    )
    assert not paused.approved


def test_short_and_forbidden_products_are_rejected_for_stocks(policy: LoadedPolicy) -> None:
    short = candidate(direction=Direction.SHORT, stop_price=Decimal("101.50"))
    d = evaluate_entry(ctx(policy, candidate=short))
    assert RejectReason.FORBIDDEN_PRODUCT in d.reasons


def test_reducing_exit_never_exceeds_owned_minus_committed() -> None:
    spec = SPECS["XNYS:SYNA"]
    assert (
        approve_reducing_exit(
            spec=spec,
            owned_quantity=Decimal("0.1"),
            committed_sell_quantity=Decimal("0.1"),
            requested_quantity=Decimal("0.1"),
        )
        is None
    )
    ok = approve_reducing_exit(
        spec=spec,
        owned_quantity=Decimal("0.1"),
        committed_sell_quantity=Decimal("0.04"),
        requested_quantity=Decimal("0.1"),
    )
    assert ok is not None and ok.quantity == Decimal("0.06")
