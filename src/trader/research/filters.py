"""Research candidate filters and their controls (strategy_and_profitability.md §6-§7).

A filter sees one candidate that a frozen rule proposed, together with its candidate features,
and may only accept or veto it. It has no channel to size, price or time an order: the research
engine asks it *before* any portfolio check and records a veto as ``NO_EDGE`` with the binding
constraint ``filter:<filter_id>`` (docs/ml_research_guide.md §5 "Integration point").

Every filter decision is logged with a score that is a :class:`~decimal.Decimal` quantised to six
decimal places (or ``None``), never a float, because the log is hashed.

Controls (strategy §6: "trading less" must not be mistaken for skill):

* :class:`HashRandomReject` rejects each candidate independently with probability ``rate``. The
  draw is ``u = int(sha256(salt + candidate_id)[:8 bytes]) / 2**64``, uniform on ``[0, 1)``;
  the candidate is rejected when ``u < rate`` (compared exactly, without rounding). The same
  salt and candidate always give the same draw, so a run is reproducible and order-independent.
* :class:`TurnoverMatchedReject` is the equal-turnover control. For each *evaluation window* -
  a half-open UTC interval ``[start, end)``; a candidate belongs to the window containing its
  decision time - it accepts exactly as many candidates as the reference filter accepted in that
  window, namely those with the smallest ``sha256(salt + candidate_id)`` (ties by candidate ID),
  and rejects the rest. Its universe is the set of candidates the reference filter *saw*. The
  engine shows a filter every in-window candidate that is not stale, before any portfolio check,
  so that set does not depend on any filter's decisions and a control run sees exactly the same
  candidates. Matching is on the number of accepted candidates, the quantity a filter controls;
  realised trades and USD turnover can still differ because pauses, entry limits and busy
  instruments apply after the filter. A candidate outside the universe is refused.
* :class:`ModelThresholdFilter` accepts when the scorer's calibrated probability is at least the
  threshold (compared exactly: ``Decimal(p) >= threshold``).
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from typing import Protocol

from trader.domain.candidates import TradeCandidate
from trader.domain.types import canon_decimal, iso_utc
from trader.features.candidate_features import (
    FEATURE_VERSION,
    FeatureVersionError,
    compute,
    verify_snapshot,
)

SCORE_QUANTUM = Decimal("0.000001")
_TWO_64 = 1 << 64


# ----------------------------------------------------------------------------- decisions


@dataclass(frozen=True, slots=True)
class FilterDecision:
    accept: bool
    score: Decimal | None  # quantized to 6 dp, never a float (it is hashed)
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.accept, bool):
            raise TypeError("a filter decision's accept flag must be a bool")
        if self.score is not None:
            if not isinstance(self.score, Decimal) or not self.score.is_finite():
                raise TypeError("a filter score must be a finite Decimal (floats are not hashed)")
            if self.score != self.score.quantize(SCORE_QUANTUM):
                raise ValueError("a filter score must be quantised to 6 decimal places")
        if not self.reason:
            raise ValueError("a filter decision states its reason")


def quantize_score(probability: float) -> Decimal:
    """A probability as a 6-dp Decimal (exact binary value, then half-even rounding)."""
    if isinstance(probability, bool) or not isinstance(probability, float | int):
        raise TypeError("a probability is a float")
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability {probability!r} is outside [0, 1]")
    return Decimal(probability).quantize(SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class FilterRecord:
    """One filter decision as recorded in ``EngineResult.filter_log`` (hash-safe)."""

    candidate_id: str
    instrument_id: str
    decision_time: datetime
    filter_id: str
    accept: bool
    score: str  # canonical Decimal string, "" when the filter gave no score
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "instrument_id": self.instrument_id,
            "decision_time": iso_utc(self.decision_time),
            "filter_id": self.filter_id,
            "accept": self.accept,
            "score": self.score,
            "reason": self.reason,
        }


def record(filter_id: str, candidate: TradeCandidate, decision: FilterDecision) -> FilterRecord:
    return FilterRecord(
        candidate_id=candidate.candidate_id,
        instrument_id=candidate.instrument_id,
        decision_time=candidate.decision_time,
        filter_id=filter_id,
        accept=decision.accept,
        score="" if decision.score is None else canon_decimal(decision.score),
        reason=decision.reason,
    )


# ----------------------------------------------------------------------------- protocols


class CandidateFilter(Protocol):
    @property
    def filter_id(self) -> str: ...

    def decide(
        self, candidate: TradeCandidate, features: Mapping[str, float]
    ) -> FilterDecision: ...


class Scorer(Protocol):
    @property
    def bundle_id(self) -> str: ...

    def predict_proba(self, features: Mapping[str, float]) -> float: ...


class SnapshotSource(Protocol):
    """A strategy that exposes the snapshot behind its most recent candidate per instrument."""

    @property
    def snapshot_version(self) -> str: ...

    def features(self, instrument_id: str) -> Mapping[str, Decimal]: ...


def candidate_feature_fn(
    strategy: SnapshotSource, family: str
) -> Callable[[TradeCandidate], dict[str, float]]:
    """The engine's ``feature_fn`` for a strategy: verified snapshot -> candidate features.

    The engine calls the filter right after ``strategy.on_bar`` returned the candidate, so the
    strategy's snapshot for that instrument is the one behind it; the snapshot hash is checked
    anyway, and a mismatch raises instead of scoring the wrong values.
    """

    def feature_fn(candidate: TradeCandidate) -> dict[str, float]:
        snapshot = strategy.features(candidate.instrument_id)
        verify_snapshot(snapshot, candidate, strategy.snapshot_version)
        return compute(snapshot, candidate, family=family)

    return feature_fn


# ----------------------------------------------------------------------------- filters


class AcceptAll:
    """Accepts every candidate; a run with it must equal a run without a filter."""

    filter_id = "accept-all"

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        del candidate, features
        return FilterDecision(accept=True, score=None, reason="accept_all")


def _hash_draw(salt: str, candidate_id: str) -> int:
    digest = hashlib.sha256((salt + candidate_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _draw_score(draw: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return (Decimal(draw) / Decimal(_TWO_64)).quantize(SCORE_QUANTUM, rounding=ROUND_FLOOR)


def _require_rate(rate: Decimal) -> Decimal:
    if not isinstance(rate, Decimal) or not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("a rejection rate is a Decimal in [0, 1]")
    return rate


def _draw_below(draw: int, rate: Decimal) -> bool:
    """``draw / 2**64 < rate``, exactly (no rounding at any precision of ``rate``)."""
    with localcontext() as ctx:
        ctx.prec = 100
        return Decimal(draw) < rate * _TWO_64


class HashRandomReject:
    """Random control: reject each candidate with probability ``rate`` (hash-based, seeded)."""

    def __init__(self, rate: Decimal, salt: str) -> None:
        self.rate = _require_rate(rate)
        if not salt:
            raise ValueError("a random control needs a salt")
        self.salt = salt
        self.filter_id = f"hash-random-reject:{canon_decimal(rate)}:{salt}"

    def rejects(self, candidate_id: str) -> bool:
        return _draw_below(_hash_draw(self.salt, candidate_id), self.rate)

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        del features
        draw = _hash_draw(self.salt, candidate.candidate_id)
        reject = _draw_below(draw, self.rate)
        return FilterDecision(
            accept=not reject,
            score=_draw_score(draw),
            reason="random_reject" if reject else "random_accept",
        )


class TurnoverMatchedReject:
    """Equal-turnover control: per window, accept as many candidates as the reference did.

    ``windows`` are non-overlapping half-open UTC intervals in time order; ``universe`` holds
    every candidate the reference filter saw (ID and decision time); ``accept_counts[i]`` is the
    number to accept in ``windows[i]``. The accepted candidates are the ``accept_counts[i]`` with
    the smallest ``sha256(salt + candidate_id)`` in that window.
    """

    def __init__(
        self,
        *,
        windows: Sequence[tuple[datetime, datetime]],
        universe: Sequence[tuple[str, datetime]],
        accept_counts: Sequence[int],
        salt: str,
    ) -> None:
        if not salt:
            raise ValueError("a random control needs a salt")
        if len(accept_counts) != len(windows):
            raise ValueError("one accept count per evaluation window")
        for start, end in windows:
            if start.tzinfo is None or end.tzinfo is None or not start < end:
                raise ValueError("evaluation windows are tz-aware with start < end")
        for (_, end), (start, _) in itertools.pairwise(windows):
            if start < end:
                raise ValueError("evaluation windows must be in time order and not overlap")
        members: dict[int, list[tuple[bytes, str]]] = {i: [] for i in range(len(windows))}
        self._window_of: dict[str, int] = {}
        for candidate_id, decision_time in universe:
            if candidate_id in self._window_of:
                raise ValueError(f"candidate {candidate_id} appears twice in the universe")
            index = _window_index(windows, decision_time)
            if index is None:
                raise ValueError(f"candidate {candidate_id} lies outside every evaluation window")
            self._window_of[candidate_id] = index
            key = hashlib.sha256((salt + candidate_id).encode("utf-8")).digest()
            members[index].append((key, candidate_id))
        accepted: set[str] = set()
        for index, count in enumerate(accept_counts):
            if count < 0 or count > len(members[index]):
                raise ValueError(
                    f"window {index} accepts {count} of {len(members[index])} candidates"
                )
            ranked = sorted(members[index])
            accepted.update(candidate_id for _, candidate_id in ranked[:count])
        self.salt = salt
        self.windows = tuple(windows)
        self.accept_counts = tuple(accept_counts)
        self._accepted = frozenset(accepted)
        self.filter_id = f"turnover-matched:{salt}"

    @classmethod
    def matching(
        cls,
        log: Sequence[FilterRecord],
        *,
        windows: Sequence[tuple[datetime, datetime]],
        salt: str,
    ) -> TurnoverMatchedReject:
        """The control for a reference filter's log (every candidate it saw, and its accepts)."""
        counts = [0] * len(windows)
        for entry in log:
            index = _window_index(windows, entry.decision_time)
            if index is not None and entry.accept:
                counts[index] += 1
        return cls(
            windows=windows,
            universe=[(e.candidate_id, e.decision_time) for e in log],
            accept_counts=counts,
            salt=salt,
        )

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        del features
        if candidate.candidate_id not in self._window_of:
            raise ValueError(
                f"candidate {candidate.candidate_id} was not seen by the reference filter; the "
                "equal-turnover control must run on exactly the same candidates"
            )
        accept = candidate.candidate_id in self._accepted
        return FilterDecision(
            accept=accept,
            score=_draw_score(_hash_draw(self.salt, candidate.candidate_id)),
            reason="turnover_matched_accept" if accept else "turnover_matched_reject",
        )


def _window_index(windows: Sequence[tuple[datetime, datetime]], ts: datetime) -> int | None:
    for index, (start, end) in enumerate(windows):
        if start <= ts < end:
            return index
    return None


class ModelThresholdFilter:
    """Accept when the scorer's calibrated probability is at least ``threshold``."""

    def __init__(self, scorer: Scorer, threshold: Decimal) -> None:
        if not isinstance(threshold, Decimal) or not threshold.is_finite():
            raise TypeError("a threshold is a finite Decimal")
        if not 0 <= threshold <= 1:
            raise ValueError("a probability threshold lies in [0, 1]")
        version = getattr(scorer, "feature_version", FEATURE_VERSION)
        if version != FEATURE_VERSION:
            raise FeatureVersionError(
                f"scorer {scorer.bundle_id} uses features {version!r}, not {FEATURE_VERSION}"
            )
        self.scorer = scorer
        self.threshold = threshold
        self.filter_id = f"model:{scorer.bundle_id}:{canon_decimal(threshold)}"

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        del candidate
        if not features:
            raise ValueError(
                "a model filter needs candidate features; pass the engine a feature_fn"
            )
        probability = self.scorer.predict_proba(features)
        score = quantize_score(probability)
        accept = Decimal(probability) >= self.threshold
        return FilterDecision(
            accept=accept,
            score=score,
            reason="probability_at_or_above_threshold" if accept else "probability_below_threshold",
        )
