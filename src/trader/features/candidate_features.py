"""Scale-free candidate features for the M3 filter (feature version ``candidate-v1``).

A model may only *filter* candidates that a frozen rule proposed (strategy_and_profitability.md
§6-§7, docs/ml_research_guide.md §1). Its inputs are derived here from two things that exist at
the decision time and nothing else:

* the rule's feature snapshot - exactly the values hashed into ``candidate.snapshot_hash``
  (``streaming-v1`` for the research rules, ``momentum-v1`` for the runtime Candidate A); and
* the candidate's own fields (reference price, stop, target, decision time, track).

Every value is a float (features may be floats; money, prices and quantities stay Decimal and
are only *divided* here). Ratios are computed in Decimal and converted once at the end, so the
same snapshot always yields the same bits.

``FEATURE_NAMES`` is one fixed tuple for both rule families, so a model's column layout never
depends on which rule produced a row. Inputs a snapshot does not carry get a documented default
and a group indicator, so every value is finite:

=====================  ==========================================  ==================  =======
name                   definition                                  source              default
=====================  ==========================================  ==================  =======
``ref_distance_atr``   (close - reference) / ATR                   streaming-v1        0.0
``atr_to_close``       ATR / close                                 both                -
``atr_ratio``          ATR / long ATR                              streaming-v1        1.0
``efficiency_ratio``   efficiency ratio                            streaming-v1        0.0
``context_return``     return over the completed context bars      streaming-v1        0.0
``breakout_atr``       (close - prior high) / ATR                  both                -
``stop_distance_atr``  (reference price - stop) / ATR              candidate           -
``reward_risk``        (target - reference) / (reference - stop)   candidate           0.0 [1]
``hour_sin``           sin(2 pi * time of day / 24 h)              decision time [2]   -
``hour_cos``           cos(2 pi * time of day / 24 h)              decision time [2]   -
``weekday``            0 = Monday ... 6 = Sunday                   decision time [2]   -
``vwap_distance_atr``  (close - session VWAP) / ATR                momentum-v1         0.0
``trailing_return_3``  close / close three bars earlier - 1        momentum-v1         0.0
``streaming_missing``  1.0 when the streaming-v1 inputs are absent indicator           -
``session_missing``    1.0 when the momentum-v1 inputs are absent  indicator           -
=====================  ==========================================  ==================  =======

[1] Only for a candidate without a target, which no frozen rule emits and the engine rejects;
the default lies outside the feature's otherwise strictly positive range, so it is its own
indicator. [2] Exchange-local (America/New_York) time for the stock track, whose instruments all
trade on the XNYS calendar; UTC for every other track.

Fail closed: an unknown snapshot schema, a snapshot whose close differs from the candidate's
reference price, a short candidate, a non-positive close, ATR or long ATR, or any non-finite
input raises :class:`FeatureError`. A consumer built for another feature version is refused with
:class:`FeatureVersionError` (see :func:`require_feature_version`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.types import Direction, Track
from trader.features.momentum import FEATURE_VERSION as MOMENTUM_VERSION
from trader.features.streaming import FEATURE_VERSION as STREAMING_VERSION

FEATURE_VERSION = "candidate-v1"

FEATURE_NAMES: tuple[str, ...] = (
    "ref_distance_atr",
    "atr_to_close",
    "atr_ratio",
    "efficiency_ratio",
    "context_return",
    "breakout_atr",
    "stop_distance_atr",
    "reward_risk",
    "hour_sin",
    "hour_cos",
    "weekday",
    "vwap_distance_atr",
    "trailing_return_3",
    "streaming_missing",
    "session_missing",
)

FAMILIES: tuple[str, ...] = ("CANDIDATE_A", "CANDIDATE_B")

#: The exact key set of each supported snapshot schema.
SNAPSHOT_KEYS: dict[str, frozenset[str]] = {
    STREAMING_VERSION: frozenset(
        {
            "close",
            "reference",
            "atr",
            "long_atr",
            "efficiency_ratio",
            "prior_high",
            "context_return",
        }
    ),
    MOMENTUM_VERSION: frozenset(
        {"close", "prior_high", "session_vwap", "atr", "trailing_return_3"}
    ),
}

_EXCHANGE_TIME = ZoneInfo("America/New_York")
_SECONDS_PER_DAY = 86400.0


class FeatureError(ValueError):
    """A snapshot or candidate cannot produce valid candidate features (fail closed)."""


class FeatureVersionError(FeatureError):
    """Features, labels or a model were produced under a different feature version."""


def require_feature_version(version: str) -> None:
    """Refuse anything computed under another candidate feature version."""
    if version != FEATURE_VERSION:
        raise FeatureVersionError(
            f"feature version {version!r} does not match this implementation ({FEATURE_VERSION})"
        )


def snapshot_version(snapshot: Mapping[str, Decimal]) -> str:
    """Identify a snapshot's schema by its exact key set."""
    keys = frozenset(snapshot)
    for version, expected in SNAPSHOT_KEYS.items():
        if keys == expected:
            return version
    raise FeatureError(f"unknown feature snapshot schema with keys {sorted(keys)}")


def snapshot_hash(snapshot: Mapping[str, Decimal], version: str) -> str:
    """The digest a rule records in ``TradeCandidate.snapshot_hash`` for this snapshot."""
    return content_hash({"version": version, "features": dict(snapshot)})


def verify_snapshot(
    snapshot: Mapping[str, Decimal], candidate: TradeCandidate, version: str
) -> None:
    """Refuse a snapshot that is not exactly the one behind ``candidate``."""
    if snapshot_version(snapshot) != version:
        raise FeatureError(f"snapshot schema does not match its declared version {version}")
    if snapshot_hash(snapshot, version) != candidate.snapshot_hash:
        raise FeatureError(
            f"snapshot does not reproduce the snapshot hash of candidate {candidate.candidate_id}"
        )


def _finite(snapshot: Mapping[str, Decimal]) -> None:
    for key, value in snapshot.items():
        if not isinstance(value, Decimal) or not value.is_finite():
            raise FeatureError(f"snapshot value {key} is not a finite Decimal")


def _positive(name: str, value: Decimal) -> Decimal:
    if value <= 0:
        raise FeatureError(f"{name} must be positive, got {value}")
    return value


def _time_features(candidate: TradeCandidate) -> tuple[float, float, float]:
    zone = _EXCHANGE_TIME if candidate.track is Track.STOCKS else UTC
    local = candidate.decision_time.astimezone(zone)
    seconds = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    angle = 2.0 * math.pi * seconds / _SECONDS_PER_DAY
    return math.sin(angle), math.cos(angle), float(local.weekday())


def compute(
    snapshot: Mapping[str, Decimal], candidate: TradeCandidate, *, family: str
) -> dict[str, float]:
    """Candidate features (``FEATURE_NAMES`` order) from a snapshot and its candidate."""
    if family not in FAMILIES:
        raise FeatureError(f"unknown rule family {family!r}")
    if candidate.direction is not Direction.LONG:
        raise FeatureError("candidate-v1 features are defined for long candidates only")
    version = snapshot_version(snapshot)
    if version == MOMENTUM_VERSION and family != "CANDIDATE_A":
        raise FeatureError("a momentum-v1 snapshot belongs to the runtime Candidate A")
    _finite(snapshot)
    close = _positive("close", snapshot["close"])
    atr = _positive("ATR", snapshot["atr"])
    if close != candidate.reference_price:
        raise FeatureError("snapshot close differs from the candidate's reference price")
    reference_price = candidate.reference_price
    risk = reference_price - candidate.stop_price
    target = candidate.target_price
    hour_sin, hour_cos, weekday = _time_features(candidate)
    out: dict[str, Decimal | float] = {
        "atr_to_close": atr / close,
        "breakout_atr": (close - snapshot["prior_high"]) / atr,
        "stop_distance_atr": risk / atr,
        "reward_risk": (target - reference_price) / risk if target is not None else 0.0,
        "hour_sin": hour_sin,
        "hour_cos": hour_cos,
        "weekday": weekday,
    }
    if version == STREAMING_VERSION:
        long_atr = _positive("long ATR", snapshot["long_atr"])
        out |= {
            "ref_distance_atr": (close - snapshot["reference"]) / atr,
            "atr_ratio": atr / long_atr,
            "efficiency_ratio": snapshot["efficiency_ratio"],
            "context_return": snapshot["context_return"],
            "streaming_missing": 0.0,
            "vwap_distance_atr": 0.0,
            "trailing_return_3": 0.0,
            "session_missing": 1.0,
        }
    else:
        out |= {
            "ref_distance_atr": 0.0,
            "atr_ratio": 1.0,
            "efficiency_ratio": 0.0,
            "context_return": 0.0,
            "streaming_missing": 1.0,
            "vwap_distance_atr": (close - snapshot["session_vwap"]) / atr,
            "trailing_return_3": snapshot["trailing_return_3"],
            "session_missing": 0.0,
        }
    values = {name: float(out[name]) for name in FEATURE_NAMES}
    for name, value in values.items():
        if not math.isfinite(value):
            raise FeatureError(f"feature {name} is not finite")
    return values
