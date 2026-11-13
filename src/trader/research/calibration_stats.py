"""Probability-calibration diagnostics for candidate-filter models (docs/ml_research_guide.md §5;
strategy_and_profitability.md §6; risk_and_validation.md §7).

Brier score, log-loss, reliability bins and the expected calibration error are *diagnostics*:
models are selected by validation log-loss and thresholds by net economics, never by accuracy
or win rate. Binary floating point is fine here; these numbers are evidence summaries, never
inputs to accounting or sizing (instructions.md §3), and are rendered as strings wherever they
are hashed.

Conventions:

* ``y`` holds 0/1 outcomes ("net PnL after costs > 0"), ``p`` the predicted probability of 1.
* :func:`log_loss` clips ``p`` to ``[eps, 1 - eps]`` with ``eps`` = float64 machine epsilon,
  the same convention as ``sklearn.metrics.log_loss``.
* :func:`reliability_bins` uses ``n_bins`` equal-width bins on ``[0, 1]``; bin ``i`` holds
  ``i/n <= p < (i+1)/n`` (computed as ``floor(p * n)``) and the last bin also holds ``p = 1``.
  Empty bins are reported with ``count = 0`` and no means.
* :func:`expected_calibration_error` is ``sum_b (n_b / N) * |observed_b - mean_predicted_b|``
  over the non-empty bins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]

EPS = float(np.finfo(np.float64).eps)


def _validated(y: npt.ArrayLike, p: npt.ArrayLike) -> tuple[FloatArray, FloatArray]:
    ya = np.asarray(y, dtype=np.float64)
    pa = np.asarray(p, dtype=np.float64)
    if ya.ndim != 1 or pa.ndim != 1 or ya.shape != pa.shape:
        raise ValueError(f"y and p must be 1-D and equally long, got {ya.shape} and {pa.shape}")
    if ya.size == 0:
        raise ValueError("calibration statistics need at least one observation")
    if not np.isin(ya, (0.0, 1.0)).all():
        raise ValueError("outcomes must be 0 or 1")
    if not np.isfinite(pa).all() or (pa < 0.0).any() or (pa > 1.0).any():
        raise ValueError("probabilities must be finite and within [0, 1]")
    return ya, pa


def brier(y: npt.ArrayLike, p: npt.ArrayLike) -> float:
    """Mean squared difference between predicted probability and outcome."""
    ya, pa = _validated(y, p)
    return float(np.mean((pa - ya) ** 2))


def log_loss(y: npt.ArrayLike, p: npt.ArrayLike, *, eps: float = EPS) -> float:
    """Mean negative log-likelihood of the outcomes (natural log)."""
    ya, pa = _validated(y, p)
    if not 0.0 < eps < 0.5:
        raise ValueError("eps must be within (0, 0.5)")
    clipped = np.clip(pa, eps, 1.0 - eps)
    return float(-np.mean(ya * np.log(clipped) + (1.0 - ya) * np.log1p(-clipped)))


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float | None
    observed_rate: float | None

    def as_dict(self) -> dict[str, str]:
        return {
            "lower": repr(self.lower),
            "upper": repr(self.upper),
            "count": str(self.count),
            "mean_predicted": "" if self.mean_predicted is None else repr(self.mean_predicted),
            "observed_rate": "" if self.observed_rate is None else repr(self.observed_rate),
        }


def reliability_bins(
    y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10
) -> tuple[ReliabilityBin, ...]:
    """Equal-width reliability diagram: per bin, the mean prediction and the observed rate."""
    if n_bins < 1:
        raise ValueError("n_bins must be at least 1")
    ya, pa = _validated(y, p)
    index = np.minimum(np.floor(pa * n_bins).astype(np.int64), n_bins - 1)
    out = []
    for b in range(n_bins):
        mask = index == b
        count = int(mask.sum())
        out.append(
            ReliabilityBin(
                lower=b / n_bins,
                upper=(b + 1) / n_bins,
                count=count,
                mean_predicted=float(pa[mask].mean()) if count else None,
                observed_rate=float(ya[mask].mean()) if count else None,
            )
        )
    return tuple(out)


def expected_calibration_error(y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10) -> float:
    """Count-weighted mean absolute gap between observed rate and mean prediction per bin."""
    bins = reliability_bins(y, p, n_bins)
    total = sum(b.count for b in bins)
    ece = math.fsum(
        b.count / total * abs(b.observed_rate - b.mean_predicted)
        for b in bins
        if b.observed_rate is not None and b.mean_predicted is not None
    )
    return float(ece)
