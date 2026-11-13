"""Walk-forward training of candidate-filter models (docs/ml_research_guide.md §1, §5;
strategy_and_profitability.md §6-§7; risk_and_validation.md §5; architecture.md §3 ``ModelBundle``).

A model is a *candidate filter*: it estimates the probability that a frozen rule's candidate is
profitable after all costs and may veto it. One walk-forward fold is trained as follows:

1. :func:`purge` drops training labels whose decision **or exit** falls within the embargo
   before the training end, so no training label overlaps the validation window.
2. Every grid point is fitted on the training rows only - the scaler as well as the model
   (logistic: ``StandardScaler`` + L2 ``LogisticRegression(lbfgs, max_iter=1000)``;
   challenger: ``LGBMClassifier(n_jobs=1, deterministic=True, force_col_wise=True)``).
3. The grid point with the lowest validation log-loss of its *uncalibrated* probability wins
   (ties: the first grid point). Predictions come from the exported numpy bundle code, i.e.
   exactly what the trading process would run.
4. The calibration map (Platt by default, isotonic optional) is fitted on the selected model's
   validation scores only.
5. The acceptance threshold is chosen on validation only: by ``threshold_objective`` (an
   engine replay of the validation window) or, without one, by the mean net return of the
   accepted validation candidates. A threshold must accept at least ``min_accepts`` (and at
   least one) validation candidates; ties go to the lower threshold.
6. Every grid point, the calibration and every threshold candidate are recorded as strings in
   ``selection_log``; the bundle is sealed with a checksum.

Training is deterministic: two runs on the same data, grid and seed produce the identical
bundle checksum. The test window is never an input here. Training libraries are imported
lazily, so importing this module does not require the ``ml`` dependency group (ADR-0027).
"""

from __future__ import annotations

import hashlib
import math
import platform
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from trader.domain.hashing import content_hash
from trader.domain.types import DomainError, canon_decimal, require_utc, to_decimal
from trader.models.bundle import ModelBundle, compile_scorer
from trader.models.calibration import (
    compile_calibration,
    isotonic_spec,
    none_spec,
    platt_spec,
    sigmoid,
)
from trader.models.codec import encode_float, encode_floats
from trader.research.calibration_stats import brier, expected_calibration_error, log_loss

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]
TimeArray = NDArray[np.datetime64]

MODEL_TYPES = ("logistic", "lightgbm")
CALIBRATION_METHODS = ("platt", "isotonic", "none")
LOGISTIC_MAX_ITER = 1000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NAT = np.datetime64("NaT", "ms")

#: Small default grids; a registered trial states its own grid before it runs (registry).
DEFAULT_LOGISTIC_GRID: tuple[dict[str, str], ...] = tuple(
    {"C": c, "class_weight": w} for c in ("0.01", "0.1", "1", "10") for w in ("none", "balanced")
)
DEFAULT_LIGHTGBM_GRID: tuple[dict[str, str], ...] = tuple(
    {"num_leaves": leaves, "min_child_samples": mcs, "learning_rate": "0.05", "n_estimators": n}
    for leaves in ("7", "15")
    for mcs in ("20", "50")
    for n in ("100", "200")
)
DEFAULT_THRESHOLD_GRID: tuple[Decimal, ...] = tuple(Decimal(n) / 100 for n in range(30, 81, 5))


class TrainingError(RuntimeError):
    """A fold cannot be trained honestly (leakage, mismatched data, no eligible threshold).

    ``selection_log`` holds the tuning history recorded before the failure (grid points,
    calibration and threshold candidates), so a study can report the full history of a fold
    that could not be thresholded; it is empty when the fold failed before any fit.
    """

    def __init__(self, message: str, *, selection_log: Sequence[Mapping[str, str]] = ()) -> None:
        super().__init__(message)
        self.selection_log: tuple[dict[str, str], ...] = tuple(dict(e) for e in selection_log)


# ----------------------------------------------------------------------------- data


def to_datetime64(value: datetime) -> np.datetime64:
    """A timezone-aware time as UTC ``datetime64[ms]`` (naive times are refused)."""
    utc = require_utc(value)
    return np.datetime64((utc - _EPOCH) // timedelta(milliseconds=1), "ms")


def _frozen(value: Any, dtype: Any) -> NDArray[Any]:
    arr = np.array(value, dtype=dtype, copy=True)
    arr.flags.writeable = False
    return arr


def _times(value: Any, name: str) -> TimeArray:
    arr = np.asarray(value)
    if not np.issubdtype(arr.dtype, np.datetime64):
        raise TrainingError(f"{name} must be a datetime64 array (UTC), got {arr.dtype}")
    return _frozen(arr.astype("datetime64[ms]"), "datetime64[ms]")


@dataclass(frozen=True, slots=True)
class FoldData:
    """Labelled candidates of one window as arrays (rows in decision-time order).

    ``X`` holds the feature snapshot (columns = ``feature_names``, all finite), ``y`` the win
    flag (net USD after all costs > 0), ``exit_time`` is NaT for a candidate that never filled.
    Arrays are copied and made read-only.
    """

    X: FloatArray
    y: IntArray
    decision_time: TimeArray
    exit_time: TimeArray
    net_return: FloatArray
    feature_names: tuple[str, ...]
    feature_version: str
    family: str
    data_hashes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = tuple(self.feature_names)
        if not names or len(set(names)) != len(names) or not all(names):
            raise TrainingError("feature names must be non-empty and unique")
        x = np.asarray(self.X, dtype=np.float64)
        if x.size == 0:
            x = x.reshape(0, len(names))
        if x.ndim != 2 or x.shape[1] != len(names):
            raise TrainingError(f"X must be n x {len(names)}, got shape {x.shape}")
        if not np.isfinite(x).all():
            raise TrainingError("features must be finite")
        n = x.shape[0]
        y = np.asarray(self.y)
        if y.shape != (n,) or not np.isin(y, (0, 1)).all():
            raise TrainingError("y must hold one 0/1 outcome per row")
        decision = _times(self.decision_time, "decision_time")
        exit_ = _times(self.exit_time, "exit_time")
        if decision.shape != (n,) or exit_.shape != (n,):
            raise TrainingError("decision_time and exit_time need one value per row")
        if np.isnat(decision).any():
            raise TrainingError("every row needs a decision time")
        if n > 1 and (np.diff(decision.astype(np.int64)) < 0).any():
            raise TrainingError("rows must be sorted by decision time")
        filled = ~np.isnat(exit_)
        if (exit_[filled] < decision[filled]).any():
            raise TrainingError("an exit cannot precede its decision")
        net = np.asarray(self.net_return, dtype=np.float64)
        if net.shape != (n,) or not np.isfinite(net).all():
            raise TrainingError("net_return must be finite, one value per row")
        hashes = {str(k): str(v) for k, v in self.data_hashes.items()}
        object.__setattr__(self, "X", _frozen(x, np.float64))
        object.__setattr__(self, "y", _frozen(y, np.int64))
        object.__setattr__(self, "decision_time", decision)
        object.__setattr__(self, "exit_time", exit_)
        object.__setattr__(self, "net_return", _frozen(net, np.float64))
        object.__setattr__(self, "feature_names", names)
        object.__setattr__(self, "data_hashes", hashes)

    def __len__(self) -> int:
        return int(self.X.shape[0])

    def select(self, mask: BoolArray) -> FoldData:
        keep = np.asarray(mask, dtype=np.bool_)
        if keep.shape != (len(self),):
            raise TrainingError("row mask must match the number of rows")
        return FoldData(
            X=self.X[keep],
            y=self.y[keep],
            decision_time=self.decision_time[keep],
            exit_time=self.exit_time[keep],
            net_return=self.net_return[keep],
            feature_names=self.feature_names,
            feature_version=self.feature_version,
            family=self.family,
            data_hashes=self.data_hashes,
        )

    def digest(self) -> str:
        """SHA-256 of every row value and the feature contract (little-endian bytes)."""
        h = hashlib.sha256()
        header = content_hash(
            {
                "feature_names": self.feature_names,
                "feature_version": self.feature_version,
                "family": self.family,
                "rows": len(self),
            }
        )
        h.update(header.encode("ascii"))
        for arr, dtype in (
            (self.X, "<f8"),
            (self.y, "<i8"),
            (self.decision_time.astype(np.int64), "<i8"),
            (self.exit_time.astype(np.int64), "<i8"),
            (self.net_return, "<f8"),
        ):
            h.update(np.ascontiguousarray(arr, dtype=dtype).tobytes())
        return h.hexdigest()


class LabelRowLike(Protocol):
    """The parts of ``research.labels.LabelRow`` that training reads (duck-typed)."""

    @property
    def family(self) -> str: ...
    @property
    def decision_time(self) -> datetime: ...
    @property
    def exit_time(self) -> datetime | None: ...
    @property
    def net_return(self) -> float: ...
    @property
    def win(self) -> bool: ...


class LabelSetLike(Protocol):
    """The parts of ``research.labels.LabelSet`` that training reads (duck-typed)."""

    @property
    def rows(self) -> Sequence[LabelRowLike]: ...
    @property
    def feature_names(self) -> Sequence[str]: ...
    @property
    def feature_version(self) -> str: ...
    @property
    def source(self) -> Mapping[str, str]: ...
    def matrix(self) -> tuple[NDArray[Any], NDArray[Any]]: ...


def fold_data_from_labelset(labels: LabelSetLike, *, family: str | None = None) -> FoldData:
    """Convert a label set (one rule family) into :class:`FoldData`.

    ``family`` selects one rule family when the set mixes several; otherwise the set must hold
    exactly one family. ``X`` and ``y`` come from ``labels.matrix()`` and are cross-checked
    against the rows; ``data_hashes`` is the label set's ``source`` provenance.
    """
    rows = tuple(labels.rows)
    names = tuple(labels.feature_names)
    x_raw, y_raw = labels.matrix()
    x = np.asarray(x_raw, dtype=np.float64)
    if x.size == 0:
        x = x.reshape(0, len(names))
    y = np.asarray(y_raw).astype(np.int64)
    if x.shape != (len(rows), len(names)) or y.shape != (len(rows),):
        raise TrainingError("labels.matrix() does not match the label rows and feature names")
    if any(int(y[i]) != int(bool(row.win)) for i, row in enumerate(rows)):
        raise TrainingError("labels.matrix() outcomes disagree with the rows' win flags")
    families = sorted({row.family for row in rows})
    if family is None:
        if len(families) != 1:
            raise TrainingError(
                f"label set holds families {families}; pass family= to train one rule family"
            )
        family = families[0]
    keep = np.asarray([row.family == family for row in rows], dtype=np.bool_)
    decision = np.asarray(
        [to_datetime64(row.decision_time) for row in rows], dtype="datetime64[ms]"
    )
    exits = np.asarray(
        [_NAT if row.exit_time is None else to_datetime64(row.exit_time) for row in rows],
        dtype="datetime64[ms]",
    )
    net = np.asarray([float(row.net_return) for row in rows], dtype=np.float64)
    return FoldData(
        X=x[keep],
        y=y[keep],
        decision_time=decision[keep],
        exit_time=exits[keep],
        net_return=net[keep],
        feature_names=names,
        feature_version=labels.feature_version,
        family=family,
        data_hashes=dict(labels.source),
    )


def purge(train: FoldData, *, train_end: datetime, embargo_minutes: int) -> FoldData:
    """Drop training rows whose decision or exit is at or after ``train_end - embargo``.

    Rows that never filled (``exit_time`` NaT) are kept when their decision is early enough.
    """
    if embargo_minutes < 0:
        raise TrainingError("embargo_minutes must not be negative")
    cutoff = to_datetime64(train_end) - np.timedelta64(embargo_minutes, "m")
    exit_ok = np.isnat(train.exit_time) | (train.exit_time < cutoff)
    return train.select((train.decision_time < cutoff) & exit_ok)


# ----------------------------------------------------------------------------- grids


def _grid_number(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    try:
        return to_decimal(raw.get(key, default))
    except DomainError as exc:
        raise TrainingError(f"grid value {key}={raw.get(key)!r}: {exc}") from exc


def _grid_int(raw: Mapping[str, Any], key: str, default: str, minimum: int) -> str:
    value = _grid_number(raw, key, default)
    if value != value.to_integral_value() or value < minimum:
        raise TrainingError(f"grid value {key} must be an integer >= {minimum}")
    return str(int(value))


def _grid_point(model_type: str, raw: Mapping[str, Any]) -> dict[str, str]:
    """Canonical string form of one grid point (unknown keys are refused, defaults filled)."""
    if model_type == "logistic":
        unknown = set(raw) - {"C", "class_weight"}
        if unknown:
            raise TrainingError(f"unknown logistic grid keys {sorted(unknown)}")
        c = _grid_number(raw, "C", "1")
        if c <= 0:
            raise TrainingError("logistic C must be positive")
        weight = str(raw.get("class_weight", "none")).lower()
        if weight not in ("none", "balanced"):
            raise TrainingError("class_weight must be 'none' or 'balanced'")
        return {"C": canon_decimal(c), "class_weight": weight}
    unknown = set(raw) - {"num_leaves", "min_child_samples", "learning_rate", "n_estimators"}
    if unknown:
        raise TrainingError(f"unknown lightgbm grid keys {sorted(unknown)}")
    rate = _grid_number(raw, "learning_rate", "0.05")
    if rate <= 0:
        raise TrainingError("learning_rate must be positive")
    return {
        "num_leaves": _grid_int(raw, "num_leaves", "15", 2),
        "min_child_samples": _grid_int(raw, "min_child_samples", "20", 1),
        "learning_rate": canon_decimal(rate),
        "n_estimators": _grid_int(raw, "n_estimators", "100", 1),
    }


# ----------------------------------------------------------------------------- export


def export_logistic(scaler: Any, model: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bundle ``preprocessing`` and ``parameters`` of a fitted StandardScaler + LogisticRegression."""
    if [int(c) for c in model.classes_] != [0, 1]:
        raise TrainingError("the logistic model must be fitted on outcomes {0, 1}")
    preprocessing = {
        "means": [encode_float(float(v)) for v in scaler.mean_],
        "scales": [encode_float(float(v)) for v in scaler.scale_],
    }
    parameters = {
        "coef": [encode_float(float(v)) for v in np.asarray(model.coef_)[0]],
        "intercept": encode_float(float(np.asarray(model.intercept_)[0])),
    }
    return preprocessing, parameters


def export_lightgbm(model: Any) -> dict[str, Any]:
    """Bundle ``parameters`` of a fitted LGBMClassifier: its ``dump_model()`` trees as strings."""
    if [int(c) for c in model.classes_] != [0, 1]:
        raise TrainingError("the LightGBM model must be fitted on outcomes {0, 1}")
    return {"trees": encode_floats(model.booster_.dump_model())}


def _fit_logistic(
    train: FoldData, point: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(train.X)
    model = LogisticRegression(
        C=float(Decimal(point["C"])),
        l1_ratio=0.0,
        solver="lbfgs",
        max_iter=LOGISTIC_MAX_ITER,
        class_weight=None if point["class_weight"] == "none" else "balanced",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        model.fit(scaler.transform(train.X), train.y)
    iterations = int(np.asarray(model.n_iter_).max())
    preprocessing, parameters = export_logistic(scaler, model)
    info = {
        "iterations": str(iterations),
        "converged": "true" if iterations < LOGISTIC_MAX_ITER else "false",
    }
    return preprocessing, parameters, info


def _fit_lightgbm(
    train: FoldData, point: Mapping[str, str], seed: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    import lightgbm

    model = lightgbm.LGBMClassifier(
        boosting_type="gbdt",
        objective="binary",
        num_leaves=int(point["num_leaves"]),
        min_child_samples=int(point["min_child_samples"]),
        learning_rate=float(Decimal(point["learning_rate"])),
        n_estimators=int(point["n_estimators"]),
        n_jobs=1,
        deterministic=True,
        force_col_wise=True,
        random_state=seed,
        verbosity=-1,
    )
    model.fit(train.X, train.y)
    parameters = export_lightgbm(model)
    return {}, parameters, {"trees": str(len(parameters["trees"]["tree_info"]))}


def _fixed_hyperparameters(model_type: str) -> dict[str, str]:
    if model_type == "logistic":
        return {
            "model": "StandardScaler+LogisticRegression",
            "penalty": "l2",
            "solver": "lbfgs",
            "max_iter": str(LOGISTIC_MAX_ITER),
        }
    return {
        "model": "LGBMClassifier",
        "boosting_type": "gbdt",
        "objective": "binary",
        "n_jobs": "1",
        "deterministic": "true",
        "force_col_wise": "true",
    }


def library_versions(model_type: str) -> dict[str, str]:
    import sklearn

    versions = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit-learn": sklearn.__version__,
    }
    if model_type == "lightgbm":
        import lightgbm

        versions["lightgbm"] = lightgbm.__version__
    return versions


# ----------------------------------------------------------------------------- calibration


def fit_platt(scores: FloatArray, y: IntArray, *, max_iter: int = 100) -> tuple[float, float]:
    """Platt scaling ``p = sigmoid(a * z + b)`` by Newton's method with backtracking.

    Uses Platt's smoothed targets ((N+ + 1) / (N+ + 2) and 1 / (N- + 2)) and the safeguards of
    Lin, Lin & Weng (2007), so a small or one-sided validation window cannot produce infinite
    coefficients. Deterministic: plain numpy, fixed start and iteration rules.
    """
    z = np.asarray(scores, dtype=np.float64)
    labels = np.asarray(y, dtype=np.int64)
    if z.shape != labels.shape or z.size == 0 or not np.isfinite(z).all():
        raise TrainingError("Platt scaling needs one finite score per outcome")
    n_pos = int(labels.sum())
    n_neg = int(labels.size - n_pos)
    target = np.where(labels == 1, (n_pos + 1.0) / (n_pos + 2.0), 1.0 / (n_neg + 2.0))

    def objective(a: float, b: float) -> float:
        f = a * z + b
        return float(np.sum(np.logaddexp(0.0, f) - target * f))

    a, b = 0.0, math.log((n_pos + 1.0) / (n_neg + 1.0))
    value = objective(a, b)
    for _ in range(max_iter):
        p = sigmoid(a * z + b)
        d = p - target
        g_a, g_b = float(np.dot(d, z)), float(d.sum())
        if abs(g_a) < 1e-10 and abs(g_b) < 1e-10:
            break
        w = p * (1.0 - p)
        h_aa = float(np.dot(w, z * z)) + 1e-12
        h_bb = float(w.sum()) + 1e-12
        h_ab = float(np.dot(w, z))
        det = h_aa * h_bb - h_ab * h_ab
        step_a = -(h_bb * g_a - h_ab * g_b) / det
        step_b = -(h_aa * g_b - h_ab * g_a) / det
        slope = g_a * step_a + g_b * step_b
        size = 1.0
        while size >= 1e-10:
            na, nb = a + size * step_a, b + size * step_b
            new_value = objective(na, nb)
            if new_value < value + 1e-4 * size * slope:
                a, b, value = na, nb, new_value
                break
            size /= 2.0
        else:
            break
    return a, b


def fit_isotonic(proba: FloatArray, y: IntArray) -> tuple[FloatArray, FloatArray]:
    """Isotonic knots (x = uncalibrated probability, y = calibrated) via scikit-learn."""
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray(proba, dtype=np.float64), np.asarray(y, dtype=np.float64))
    return (
        np.asarray(iso.X_thresholds_, dtype=np.float64),
        np.asarray(iso.y_thresholds_, dtype=np.float64),
    )


def fit_calibration(method: str, scores: FloatArray, y: IntArray) -> dict[str, Any]:
    """Fit the stored calibration map on (validation) raw scores ``z``."""
    if method == "platt":
        return platt_spec(*fit_platt(scores, y))
    if method == "isotonic":
        return isotonic_spec(*fit_isotonic(sigmoid(scores), y))
    if method == "none":
        return none_spec()
    raise TrainingError(f"unsupported calibration {method!r}; use one of {CALIBRATION_METHODS}")


# ----------------------------------------------------------------------------- training


@dataclass(frozen=True, slots=True)
class TrainResult:
    """The sealed bundle, the full tuning history and validation diagnostics (all strings).

    ``val_metrics`` are measured on the validation window, which also fitted the calibration
    and chose the threshold: they are diagnostics, not out-of-sample evidence.
    """

    bundle: ModelBundle
    selection_log: tuple[dict[str, str], ...]
    val_metrics: dict[str, str]


def float_at_least(threshold: Decimal) -> float:
    """Smallest double ``>= threshold``: ``p >= float_at_least(t)`` iff ``p >= t`` exactly."""
    f = float(threshold)
    if Decimal(f) < threshold:
        f = math.nextafter(f, math.inf)
    return f


def _check_fold(
    train: FoldData, val: FoldData, training_start: datetime, training_cutoff: datetime
) -> None:
    if (train.feature_names, train.feature_version, train.family) != (
        val.feature_names,
        val.feature_version,
        val.family,
    ):
        raise TrainingError("training and validation differ in features, version or family")
    start, cutoff = to_datetime64(training_start), to_datetime64(training_cutoff)
    if not start < cutoff:
        raise TrainingError("training_start must precede training_cutoff")
    if len(train) == 0 or len(val) == 0:
        raise TrainingError("training and validation windows must both hold candidates")
    if (train.decision_time < start).any() or (train.decision_time >= cutoff).any():
        raise TrainingError("training rows lie outside [training_start, training_cutoff)")
    exits = train.exit_time[~np.isnat(train.exit_time)]
    if (exits >= cutoff).any():
        raise TrainingError(
            "training labels end at or after the training cutoff; purge() them first"
        )
    if (val.decision_time < cutoff).any():
        raise TrainingError("validation rows precede the training cutoff")
    if set(np.unique(train.y).tolist()) != {0, 1}:
        raise TrainingError("training needs both profitable and unprofitable candidates")


def train_fold(
    train: FoldData,
    val: FoldData,
    *,
    model_type: str,
    grid: Sequence[Mapping[str, str]],
    seed: int,
    fold_index: int,
    training_start: datetime,
    training_cutoff: datetime,
    calibration: str = "platt",
    threshold_grid: Sequence[Decimal],
    threshold_objective: Callable[[ModelBundle], Decimal] | None = None,
    min_accepts: int = 20,
) -> TrainResult:
    """Tune on validation log-loss, calibrate and threshold on validation, seal the bundle."""
    if model_type not in MODEL_TYPES:
        raise TrainingError(f"model_type must be one of {MODEL_TYPES}, got {model_type!r}")
    if calibration not in CALIBRATION_METHODS:
        raise TrainingError(f"calibration must be one of {CALIBRATION_METHODS}")
    if not grid:
        raise TrainingError("the hyperparameter grid is empty")
    if seed < 0 or fold_index < 0 or min_accepts < 0:
        raise TrainingError("seed, fold_index and min_accepts must not be negative")
    thresholds = list(threshold_grid)
    if not thresholds or any(
        not isinstance(t, Decimal) or not t.is_finite() or not 0 <= t <= 1 for t in thresholds
    ):
        raise TrainingError("threshold_grid must hold Decimal values within [0, 1]")
    if len(set(thresholds)) != len(thresholds):
        raise TrainingError("threshold_grid holds duplicates")
    _check_fold(train, val, training_start, training_cutoff)
    if calibration != "none" and set(np.unique(val.y).tolist()) != {0, 1}:
        raise TrainingError("validation needs both outcomes to fit a calibration map")
    points = [_grid_point(model_type, raw) for raw in grid]
    n_features = len(train.feature_names)
    log: list[dict[str, str]] = []

    # -- 1. grid search: fit on train only, score validation with the exported numpy code
    best: tuple[float, int, dict[str, Any], dict[str, Any], FloatArray] | None = None
    for index, point in enumerate(points):
        if model_type == "logistic":
            preprocessing, parameters, info = _fit_logistic(train, point)
        else:
            preprocessing, parameters, info = _fit_lightgbm(train, point, seed)
        scorer = compile_scorer(model_type, preprocessing, parameters, n_features)
        z_val = scorer.raw_scores(val.X)
        loss = log_loss(val.y, sigmoid(z_val))
        log.append(
            {
                "stage": "grid",
                "index": str(index),
                **{f"param.{k}": v for k, v in point.items()},
                **info,
                "val_log_loss": encode_float(loss),
            }
        )
        if best is None or loss < best[0]:
            best = (loss, index, preprocessing, parameters, z_val)
    assert best is not None
    best_loss, best_index, preprocessing, parameters, z_val = best
    log.append(
        {"stage": "selected", "index": str(best_index), "val_log_loss": encode_float(best_loss)}
    )

    # -- 2. calibration: validation scores of the selected model only
    calibration_map = fit_calibration(calibration, z_val, val.y)
    calibrator = compile_calibration(calibration_map)
    p_raw = sigmoid(z_val)
    p_cal = calibrator.apply(z_val)
    cal_entry = {"stage": "calibration", "method": calibration}
    if calibration == "platt":
        cal_entry |= dict(calibration_map["params"])
    elif calibration == "isotonic":
        cal_entry["knots"] = str(len(calibration_map["params"]["x"]))
    log.append(cal_entry)

    # -- 3. bundle content shared by every threshold candidate
    hyperparameters = {
        **_fixed_hyperparameters(model_type),
        **points[best_index],
        "grid_points": str(len(points)),
        "selection": "min validation log-loss (uncalibrated)",
        "calibration": calibration,
        "threshold_rule": (
            "objective callback" if threshold_objective is not None else "mean net return"
        ),
        "min_accepts": str(min_accepts),
    }
    data_hashes = {
        "train_rows_sha256": train.digest(),
        "validation_rows_sha256": val.digest(),
    }
    if dict(train.data_hashes) == dict(val.data_hashes):
        data_hashes |= {f"source.{k}": v for k, v in train.data_hashes.items()}
    else:
        data_hashes |= {f"train.{k}": v for k, v in train.data_hashes.items()}
        data_hashes |= {f"validation.{k}": v for k, v in val.data_hashes.items()}
    content: dict[str, Any] = {
        "model_type": model_type,
        "feature_version": train.feature_version,
        "feature_names": train.feature_names,
        "family": train.family,
        "training_start": require_utc(training_start),
        "training_cutoff": require_utc(training_cutoff),
        "fold_index": fold_index,
        "preprocessing": preprocessing,
        "parameters": parameters,
        "calibration": calibration_map,
        "hyperparameters": hyperparameters,
        "seed": seed,
        "library_versions": library_versions(model_type),
        "data_hashes": data_hashes,
    }
    built: dict[Decimal, ModelBundle] = {}

    def bundle_for(threshold: Decimal) -> ModelBundle:
        if threshold not in built:
            fields = {**content, "threshold": threshold}
            digest = content_hash(fields)
            bundle_id = f"{train.family.lower()}-{model_type}-f{fold_index:02d}-{digest[:12]}"
            built[threshold] = ModelBundle.seal(bundle_id=bundle_id, **fields)
        return built[threshold]

    # -- 4. threshold: validation only, by the objective among thresholds with enough accepts
    chosen: tuple[Decimal, Decimal, int] | None = None
    for threshold in sorted(thresholds):
        accepted = p_cal >= float_at_least(threshold)
        accepts = int(accepted.sum())
        entry = {
            "stage": "threshold",
            "threshold": canon_decimal(threshold),
            "accepts": str(accepts),
            "validation_rows": str(len(val)),
        }
        if accepts < max(min_accepts, 1):
            entry |= {"eligible": "false", "objective": "", "reason": "too few accepts"}
            log.append(entry)
            continue
        if threshold_objective is None:
            objective = Decimal(f"{float(np.mean(val.net_return[accepted])):.12f}")
        else:
            objective = threshold_objective(bundle_for(threshold))
            if not isinstance(objective, Decimal) or not objective.is_finite():
                raise TrainingError("threshold_objective must return a finite Decimal")
        entry |= {"eligible": "true", "objective": canon_decimal(objective)}
        log.append(entry)
        if chosen is None or objective > chosen[1]:
            chosen = (threshold, objective, accepts)
    if chosen is None:
        raise TrainingError(
            f"no threshold accepts at least {max(min_accepts, 1)} validation candidates",
            selection_log=log,
        )
    threshold, objective, accepts = chosen
    bundle = bundle_for(threshold)
    log.append(
        {
            "stage": "threshold_selected",
            "threshold": canon_decimal(threshold),
            "objective": canon_decimal(objective),
            "accepts": str(accepts),
        }
    )
    val_metrics = {
        "train_rows": str(len(train)),
        "train_positives": str(int(train.y.sum())),
        "validation_rows": str(len(val)),
        "validation_positives": str(int(val.y.sum())),
        "selected_grid_index": str(best_index),
        "log_loss_raw": encode_float(log_loss(val.y, p_raw)),
        "log_loss_calibrated": encode_float(log_loss(val.y, p_cal)),
        "brier_raw": encode_float(brier(val.y, p_raw)),
        "brier_calibrated": encode_float(brier(val.y, p_cal)),
        "ece_raw": encode_float(expected_calibration_error(val.y, p_raw)),
        "ece_calibrated": encode_float(expected_calibration_error(val.y, p_cal)),
        "threshold": canon_decimal(threshold),
        "accepts": str(accepts),
        "objective": canon_decimal(objective),
        "objective_source": "callback" if threshold_objective is not None else "mean_net_return",
    }
    return TrainResult(bundle=bundle, selection_log=tuple(log), val_metrics=val_metrics)
