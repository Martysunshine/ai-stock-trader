"""Probability calibration applied by a model bundle at inference (architecture.md §3
``ModelBundle``; strategy_and_profitability.md §6-§7; docs/ml_research_guide.md §5).

The map is fitted offline on the *validation* window only (``research/training.py``) and
stored in the bundle as decimal strings::

    {"method": "platt",    "params": {"a": "<str>", "b": "<str>"}}
    {"method": "isotonic", "params": {"x": ["<str>", ...], "y": ["<str>", ...]}}
    {"method": "none",     "params": {}}

* ``platt``: ``p = sigmoid(a * z + b)``, where ``z`` is the model's raw log-odds score
  (the logistic decision value, or LightGBM's ``sigmoid * sum(leaf values)``).
* ``isotonic``: piecewise-linear interpolation through the knots, ``x`` being the
  *uncalibrated* probability ``sigmoid(z)`` and ``y`` the calibrated one, clipped to the first
  and last knot outside their range (scikit-learn ``IsotonicRegression(out_of_bounds="clip")``).
* ``none``: ``p = sigmoid(z)``.

numpy only: this module runs inside the trading process and never imports a training library
or research code (ADR-0027; tests/test_boundaries.py).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from trader.models.codec import EncodingError, decode_number, decode_vector, encode_float

FloatArray = NDArray[np.float64]

CALIBRATION_METHODS = ("platt", "isotonic", "none")


class CalibrationError(ValueError):
    """A stored calibration map is malformed or unsupported."""


def sigmoid(z: FloatArray) -> FloatArray:
    """Numerically stable logistic function (no overflow for large ``|z|``)."""
    z = np.asarray(z, dtype=np.float64)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


@dataclass(frozen=True, slots=True)
class Calibrator:
    """A compiled calibration map: raw log-odds scores in, calibrated probabilities out."""

    method: str
    a: float = 1.0
    b: float = 0.0
    knots_x: tuple[float, ...] = ()
    knots_y: tuple[float, ...] = ()

    def apply(self, scores: FloatArray) -> FloatArray:
        z = np.asarray(scores, dtype=np.float64)
        if self.method == "platt":
            return sigmoid(self.a * z + self.b)
        if self.method == "isotonic":
            return np.interp(
                sigmoid(z),
                np.asarray(self.knots_x, dtype=np.float64),
                np.asarray(self.knots_y, dtype=np.float64),
            )
        return sigmoid(z)


def none_spec() -> dict[str, Any]:
    return {"method": "none", "params": {}}


def platt_spec(a: float, b: float) -> dict[str, Any]:
    return {"method": "platt", "params": {"a": encode_float(a), "b": encode_float(b)}}


def isotonic_spec(x: FloatArray, y: FloatArray) -> dict[str, Any]:
    return {
        "method": "isotonic",
        "params": {
            "x": [encode_float(float(v)) for v in np.asarray(x, dtype=np.float64)],
            "y": [encode_float(float(v)) for v in np.asarray(y, dtype=np.float64)],
        },
    }


def _monotone_unit_interval(values: list[float], name: str) -> None:
    if any(v < 0.0 or v > 1.0 for v in values):
        raise CalibrationError(f"isotonic {name} knots must lie in [0, 1]")
    if any(b < a for a, b in itertools.pairwise(values)):
        raise CalibrationError(f"isotonic {name} knots must be non-decreasing")


def compile_calibration(spec: Mapping[str, Any]) -> Calibrator:
    """Validate a stored calibration map and compile it (fail closed on anything unexpected)."""
    if not isinstance(spec, Mapping) or set(spec) != {"method", "params"}:
        raise CalibrationError('calibration must be {"method": ..., "params": {...}}')
    method = spec["method"]
    params = spec["params"]
    if method not in CALIBRATION_METHODS:
        raise CalibrationError(f"unsupported calibration method {method!r}")
    if not isinstance(params, Mapping):
        raise CalibrationError("calibration params must be a mapping")
    try:
        if method == "none":
            if params:
                raise CalibrationError("calibration 'none' takes no parameters")
            return Calibrator(method="none")
        if method == "platt":
            if set(params) != {"a", "b"}:
                raise CalibrationError("platt calibration needs exactly 'a' and 'b'")
            return Calibrator(
                method="platt",
                a=decode_number(params["a"], where="calibration.a"),
                b=decode_number(params["b"], where="calibration.b"),
            )
        if set(params) != {"x", "y"}:
            raise CalibrationError("isotonic calibration needs exactly 'x' and 'y' knots")
        x = decode_vector(params["x"], where="calibration.x")
        y = decode_vector(params["y"], where="calibration.y", length=len(x))
    except EncodingError as exc:
        raise CalibrationError(str(exc)) from exc
    if not x:
        raise CalibrationError("isotonic calibration needs at least one knot")
    _monotone_unit_interval(x, "x")
    _monotone_unit_interval(y, "y")
    return Calibrator(method="isotonic", knots_x=tuple(x), knots_y=tuple(y))
