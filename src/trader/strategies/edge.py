"""Edge estimators: how a candidate gets an expected gross return.

There is no validated model yet (M3). The default :class:`NoEdgeEstimator` returns nothing, so
every candidate is rejected with ``NO_EDGE`` - rule-only candidates do not trade.

:class:`FixtureEdgeEstimator` exists only to exercise the cost-aware admission path in replay. It
reads a *declared* assumption from a synthetic session manifest, refuses to run outside replay
or against any other session, and labels its scores ``SYNTHETIC_FIXTURE`` evidence, which the
risk engine accepts in replay only. Position size never depends on the edge value.

:class:`PracticeEdgeEstimator` is the owner's practice mode (ADR-0040): it *assumes* the
strategy's planned target is reached, so the risk engine's conservative net-edge check becomes a
cost-coverage gate (the target must pay both spread crossings and every fee at the exact size).
That is an assumption, not an estimate: scores are ``PRACTICE`` evidence, accepted in paper and
replay only, and never count as research or forward-paper evidence.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Protocol

from trader.data.recorded_session import RecordedSession
from trader.domain.candidates import (
    EdgeEvidence,
    ReturnBasis,
    SignalScore,
    TradeCandidate,
)
from trader.domain.types import Direction, Mode

#: Model hash every practice score carries; decisions and model_versions rows show it.
PRACTICE_MODEL_HASH = "practice:planned-target:v1"
PRACTICE_MODES = frozenset({Mode.PAPER, Mode.REPLAY})


class EdgeEstimator(Protocol):
    def score(self, candidate: TradeCandidate) -> SignalScore | None: ...


class NoEdgeEstimator:
    def score(self, candidate: TradeCandidate) -> SignalScore | None:
        return None


class FixtureEdgeEstimator:
    def __init__(self, session: RecordedSession, mode: Mode) -> None:
        if mode is not Mode.REPLAY:
            raise ValueError("fixture edge assumptions are permitted in replay only")
        if not session.manifest.synthetic:
            raise ValueError("fixture edge assumptions require a synthetic session")
        assumptions = session.manifest.edge_assumptions
        if assumptions is None:
            raise ValueError("the session manifest declares no edge assumptions")
        self._assumptions = assumptions
        self._source = session.manifest_hash
        self.model_hash = f"fixture-edge:{session.manifest_hash[:16]}"

    def score(self, candidate: TradeCandidate) -> SignalScore | None:
        a = self._assumptions
        return SignalScore(
            candidate_id=candidate.candidate_id,
            model_hash=self.model_hash,
            expected_gross_return=a.expected_gross_return,
            uncertainty_margin=a.uncertainty_margin,
            basis=a.basis,
            evidence=EdgeEvidence.SYNTHETIC_FIXTURE,
            edge_source_hash=self._source,
        )


class PracticeEdgeEstimator:
    """Practice mode: the planned target distance as the expected gross return (ADR-0040).

    Mid-to-mid basis, so the sizer charges both spread crossings; no uncertainty margin, so
    the cost gate is exactly "target move > all round-trip costs at this size". A candidate
    without a target, or whose target is not beyond the reference price, gets no score.
    """

    model_hash = PRACTICE_MODEL_HASH

    def __init__(self, mode: Mode) -> None:
        if mode not in PRACTICE_MODES:
            raise ValueError(f"practice mode is permitted in paper and replay only, not {mode}")
        self._source = hashlib.sha256(PRACTICE_MODEL_HASH.encode()).hexdigest()

    def score(self, candidate: TradeCandidate) -> SignalScore | None:
        target = candidate.target_price
        if target is None:
            return None
        ref = candidate.reference_price
        move = target - ref if candidate.direction is Direction.LONG else ref - target
        if move <= 0:
            return None
        return SignalScore(
            candidate_id=candidate.candidate_id,
            model_hash=self.model_hash,
            expected_gross_return=move / ref,
            uncertainty_margin=Decimal(0),
            basis=ReturnBasis.MID_TO_MID,
            evidence=EdgeEvidence.PRACTICE,
            edge_source_hash=self._source,
        )
