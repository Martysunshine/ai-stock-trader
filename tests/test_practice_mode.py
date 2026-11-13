"""Owner practice mode (ADR-0040): paper/replay only, a cost-coverage gate, never evidence."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_risk_engine import candidate, ctx
from trader.cli.main import PRACTICE_BANNER, _practice_edge, app
from trader.config.host import HostRole
from trader.config.policy import LoadedPolicy, load_policy
from trader.config.runtime_config import load_config
from trader.domain.candidates import EdgeEvidence, RejectReason, ReturnBasis
from trader.domain.types import Direction, Mode
from trader.risk.engine import ACCEPTED_EVIDENCE, evaluate_entry
from trader.strategies.edge import PRACTICE_MODEL_HASH, PracticeEdgeEstimator

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.SHADOW])
def test_practice_mode_refuses_live_and_shadow(mode: Mode) -> None:
    with pytest.raises(ValueError, match="paper and replay only"):
        PracticeEdgeEstimator(mode)


def test_practice_evidence_is_accepted_in_paper_and_replay_only() -> None:
    assert EdgeEvidence.PRACTICE in ACCEPTED_EVIDENCE[Mode.PAPER]
    assert EdgeEvidence.PRACTICE in ACCEPTED_EVIDENCE[Mode.REPLAY]
    assert EdgeEvidence.PRACTICE not in ACCEPTED_EVIDENCE[Mode.SHADOW]
    assert EdgeEvidence.PRACTICE not in ACCEPTED_EVIDENCE[Mode.LIVE]


def test_the_practice_score_is_the_planned_target_distance() -> None:
    cand = candidate()  # long: reference 101.21, target 101.77
    s = PracticeEdgeEstimator(Mode.PAPER).score(cand)
    assert s is not None
    assert s.expected_gross_return == (Decimal("101.77") - Decimal("101.21")) / Decimal("101.21")
    assert s.uncertainty_margin == 0
    assert s.basis is ReturnBasis.MID_TO_MID  # both spread crossings are charged
    assert s.evidence is EdgeEvidence.PRACTICE
    assert s.model_hash == PRACTICE_MODEL_HASH and s.candidate_id == cand.candidate_id

    short = candidate(
        direction=Direction.SHORT, stop_price=Decimal("101.49"), target_price=Decimal("100.65")
    )
    ss = PracticeEdgeEstimator(Mode.REPLAY).score(short)
    assert ss is not None and ss.expected_gross_return > 0


@pytest.mark.parametrize("target", [None, Decimal("101.21"), Decimal("100.00")])
def test_no_target_beyond_the_reference_means_no_practice_trade(target: Decimal | None) -> None:
    assert PracticeEdgeEstimator(Mode.PAPER).score(candidate(target_price=target)) is None


def test_practice_takes_only_trades_whose_target_covers_round_trip_costs(
    policy: LoadedPolicy,
) -> None:
    est = PracticeEdgeEstimator(Mode.REPLAY)
    wide = candidate()
    taken = evaluate_entry(ctx(policy, candidate=wide, score=est.score(wide)))
    assert taken.approved, taken.details
    assert taken.evidence is EdgeEvidence.PRACTICE and taken.model_hash == PRACTICE_MODEL_HASH
    assert taken.sizing is not None and taken.sizing.expected_net_usd > 0

    tight = candidate(target_price=Decimal("101.25"))  # 4 bps: less than spread and fees
    refused = evaluate_entry(ctx(policy, candidate=tight, score=est.score(tight)))
    assert not refused.approved
    assert RejectReason.TOO_COSTLY in refused.reasons, refused.details


def test_a_practice_score_never_passes_in_live(policy: LoadedPolicy) -> None:
    live_policy = load_policy(ROOT / "configs/policy/base.yaml", mode=Mode.LIVE, live_enabled=True)
    s = PracticeEdgeEstimator(Mode.PAPER).score(candidate())
    d = evaluate_entry(ctx(live_policy, mode=Mode.LIVE, score=s))
    assert not d.approved
    assert RejectReason.NO_EDGE in d.reasons


def test_paper_mode_accepts_the_practice_score_as_edge_evidence() -> None:
    paper_policy = load_policy(ROOT / "configs/policy/base.yaml", mode=Mode.PAPER)
    s = PracticeEdgeEstimator(Mode.PAPER).score(candidate())
    d = evaluate_entry(ctx(paper_policy, mode=Mode.PAPER, score=s))
    assert RejectReason.NO_EDGE not in d.reasons, d.details


def test_paper_run_offers_practice_and_labels_it() -> None:
    cfg = load_config(ROOT / "configs/paper.yaml", host_role=HostRole.DEV)
    assert isinstance(_practice_edge(cfg), PracticeEdgeEstimator)
    assert "ADR-0040" in PRACTICE_BANNER and "never count" in PRACTICE_BANNER
    out = CliRunner().invoke(app, ["paper", "run", "--help"], terminal_width=200).output
    assert "--practice" in out
