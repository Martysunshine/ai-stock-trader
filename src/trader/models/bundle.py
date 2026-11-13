"""Approved inference bundles for candidate-filter models (architecture.md §3 ``ModelBundle``;
strategy_and_profitability.md §6-§7; instructions.md §3, §5; docs/ml_research_guide.md §1, §5).

A ``ModelBundle`` is the frozen, self-describing artifact a trained filter is exported to: the
feature version and names, the rule family, the training window and cutoff, the preprocessing,
the model parameters, the calibration map, the acceptance threshold, the hyperparameters and
seed, the library versions used for training, the data hashes, and a SHA-256 ``checksum`` of
the canonical JSON of everything else. A bundle with a wrong checksum cannot be constructed or
loaded (:class:`BundleIntegrityError`), and a sealed bundle cannot be modified.

Every number is stored as an exact decimal string (``models/codec.py``), so the canonical hash
never sees a binary float and inference reproduces the trained model exactly.

Inference uses numpy alone (ADR-0027): this package never imports scikit-learn, LightGBM or
``trader.research``, so the trading process never loads a training library.

* ``logistic``: ``z = ((x - means) / scales) . coef + intercept`` (StandardScaler followed by
  LogisticRegression), ``p = sigmoid(z)``.
* ``lightgbm``: the trees of ``booster.dump_model()`` are evaluated directly - numerical splits
  (``decision_type "<="``), ``default_left`` and LightGBM's ``missing_type`` rules (``None``:
  NaN is read as 0; ``Zero``: |x| <= kZeroThreshold goes the default way; ``NaN``: NaN goes the
  default way) - the leaf values are summed in tree order and ``z = sigmoid * sum``.
* The calibration map (``models/calibration.py``) then turns ``z`` into the probability that
  the candidate is profitable after all costs.

A model only *filters* candidates: it can never size an order, change a limit or reach a
broker (tests/test_boundaries.py).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import numpy as np
from numpy.typing import NDArray
from pydantic import Field, PrivateAttr, field_validator, model_validator

from trader.domain.base import ConfigModel
from trader.domain.hashing import canonical_json, content_hash
from trader.domain.types import Fraction, UtcDatetime
from trader.models.calibration import Calibrator, compile_calibration, sigmoid
from trader.models.codec import EncodingError, decode_number, decode_vector, frozen_json

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]

ModelType = Literal["logistic", "lightgbm"]

#: LightGBM's ``kZeroThreshold`` (declared ``1e-35f``, i.e. this value as a double).
K_ZERO_THRESHOLD = 1.0000000180025095e-35
_MISSING_TYPES = {"None": 0, "Zero": 1, "NaN": 2}


class BundleIntegrityError(RuntimeError):
    """The checksum does not match the content, or a sealed bundle was modified."""


class BundleInputError(ValueError):
    """Feature input that the bundle cannot score (missing feature, wrong shape, non-finite)."""


# ----------------------------------------------------------------------------- scorers


@dataclass(frozen=True, slots=True, eq=False)
class LogisticScorer:
    """Standardisation followed by a linear log-odds score."""

    means: FloatArray
    scales: FloatArray
    coef: FloatArray
    intercept: float

    def raw_scores(self, x: FloatArray) -> FloatArray:
        if not np.isfinite(x).all():
            raise BundleInputError("a logistic bundle cannot score NaN or infinite features")
        standardised = (x - self.means) / self.scales
        return np.asarray(standardised @ self.coef + self.intercept, dtype=np.float64)


@dataclass(frozen=True, slots=True, eq=False)
class _Tree:
    """One LightGBM tree as flat arrays. Child ids ``>= 0`` are split nodes, ``< 0`` leaves
    (leaf ``i`` is stored as ``~i``)."""

    feature: IntArray
    threshold: FloatArray
    default_left: BoolArray
    missing_type: IntArray
    left: IntArray
    right: IntArray
    leaf_value: FloatArray
    depth: int

    def values(self, x: FloatArray) -> FloatArray:
        n = x.shape[0]
        if self.feature.size == 0:
            return np.full(n, self.leaf_value[0], dtype=np.float64)
        node = np.zeros(n, dtype=np.int64)
        for _ in range(self.depth):
            rows = np.flatnonzero(node >= 0)
            if rows.size == 0:
                break
            current = node[rows]
            fval = x[rows, self.feature[current]]
            mtype = self.missing_type[current]
            is_nan = np.isnan(fval)
            # LightGBM NumericalDecision: NaN is read as 0.0 unless the split's missing type is NaN.
            fval = np.where(is_nan & (mtype != _MISSING_TYPES["NaN"]), 0.0, fval)
            missing = ((mtype == _MISSING_TYPES["Zero"]) & (np.abs(fval) <= K_ZERO_THRESHOLD)) | (
                (mtype == _MISSING_TYPES["NaN"]) & is_nan
            )
            go_left = np.where(missing, self.default_left[current], fval <= self.threshold[current])
            node[rows] = np.where(go_left, self.left[current], self.right[current])
        if (node >= 0).any():
            raise BundleIntegrityError("tree evaluation did not reach a leaf")
        return self.leaf_value[~node]


@dataclass(frozen=True, slots=True, eq=False)
class LightGBMScorer:
    """Sum of tree outputs in tree order, scaled by the binary objective's sigmoid parameter."""

    trees: tuple[_Tree, ...]
    sigmoid: float

    def raw_scores(self, x: FloatArray) -> FloatArray:
        total = np.zeros(x.shape[0], dtype=np.float64)
        for tree in self.trees:
            total += tree.values(x)
        return self.sigmoid * total


NumpyScorer = LogisticScorer | LightGBMScorer


def _parse_tree(structure: Any, n_features: int, where: str) -> _Tree:
    feature: list[int] = []
    threshold: list[float] = []
    default_left: list[bool] = []
    missing_type: list[int] = []
    left: list[int] = []
    right: list[int] = []
    leaf_value: list[float] = []
    max_depth = 0

    def visit(node: Any, depth: int, path: str) -> int:
        nonlocal max_depth
        if not isinstance(node, Mapping):
            raise EncodingError(f"{path}: tree node must be a mapping")
        if "split_feature" not in node:
            if "leaf_value" not in node:
                raise EncodingError(f"{path}: node is neither a split nor a leaf")
            leaf_value.append(decode_number(node["leaf_value"], where=f"{path}.leaf_value"))
            max_depth = max(max_depth, depth)
            return ~(len(leaf_value) - 1)
        if node.get("decision_type") != "<=":
            raise EncodingError(
                f"{path}: only numerical '<=' splits are supported "
                f"(got {node.get('decision_type')!r}; categorical features are not allowed)"
            )
        f = node["split_feature"]
        if isinstance(f, bool) or not isinstance(f, int) or not 0 <= f < n_features:
            raise EncodingError(f"{path}: split_feature {f!r} outside 0..{n_features - 1}")
        mtype = node.get("missing_type")
        if mtype not in _MISSING_TYPES:
            raise EncodingError(f"{path}: unsupported missing_type {mtype!r}")
        dleft = node.get("default_left")
        if not isinstance(dleft, bool):
            raise EncodingError(f"{path}: default_left must be a boolean")
        index = len(feature)
        feature.append(f)
        threshold.append(decode_number(node.get("threshold"), where=f"{path}.threshold"))
        default_left.append(dleft)
        missing_type.append(_MISSING_TYPES[mtype])
        left.append(0)
        right.append(0)
        left[index] = visit(node.get("left_child"), depth + 1, f"{path}.left_child")
        right[index] = visit(node.get("right_child"), depth + 1, f"{path}.right_child")
        return index

    root = visit(structure, 0, where)
    if feature and root != 0:
        raise EncodingError(f"{where}: the root must be a split node")
    return _Tree(
        feature=np.asarray(feature, dtype=np.int64),
        threshold=np.asarray(threshold, dtype=np.float64),
        default_left=np.asarray(default_left, dtype=np.bool_),
        missing_type=np.asarray(missing_type, dtype=np.int64),
        left=np.asarray(left, dtype=np.int64),
        right=np.asarray(right, dtype=np.int64),
        leaf_value=np.asarray(leaf_value, dtype=np.float64),
        depth=max_depth,
    )


def _compile_lightgbm(dump: Any, n_features: int) -> LightGBMScorer:
    if not isinstance(dump, Mapping):
        raise EncodingError("parameters.trees must be the booster.dump_model() mapping")
    objective = dump.get("objective")
    tokens = objective.split() if isinstance(objective, str) else []
    if not tokens or tokens[0] != "binary":
        raise EncodingError(f"only the binary objective is supported, got {objective!r}")
    sigmoid_param = 1.0
    for token in tokens[1:]:
        if token.startswith("sigmoid:"):
            sigmoid_param = decode_number(token.split(":", 1)[1], where="objective sigmoid")
    if dump.get("num_class") != 1 or dump.get("num_tree_per_iteration") != 1:
        raise EncodingError("only single-output binary LightGBM models are supported")
    if dump.get("average_output") is not False:
        raise EncodingError("averaged (random-forest) LightGBM output is not supported")
    if dump.get("max_feature_idx") != n_features - 1:
        raise EncodingError(
            f"model was trained on {dump.get('max_feature_idx')!r}+1 features, "
            f"bundle declares {n_features}"
        )
    tree_info = dump.get("tree_info")
    if not isinstance(tree_info, list) or not tree_info:
        raise EncodingError("parameters.trees.tree_info must be a non-empty list")
    trees = []
    for i, info in enumerate(tree_info):
        if not isinstance(info, Mapping) or info.get("num_cat", 0) != 0:
            raise EncodingError(f"tree {i}: categorical splits are not supported")
        trees.append(_parse_tree(info.get("tree_structure"), n_features, f"tree[{i}]"))
    return LightGBMScorer(trees=tuple(trees), sigmoid=sigmoid_param)


def _compile_logistic(
    preprocessing: Mapping[str, Any], parameters: Mapping[str, Any], n_features: int
) -> LogisticScorer:
    if set(preprocessing) != {"means", "scales"}:
        raise EncodingError("logistic preprocessing needs exactly 'means' and 'scales'")
    if set(parameters) != {"coef", "intercept"}:
        raise EncodingError("logistic parameters need exactly 'coef' and 'intercept'")
    means = decode_vector(preprocessing["means"], where="means", length=n_features)
    scales = decode_vector(preprocessing["scales"], where="scales", length=n_features)
    if any(s <= 0.0 for s in scales):
        raise EncodingError("standardisation scales must be positive")
    coef = decode_vector(parameters["coef"], where="coef", length=n_features)
    return LogisticScorer(
        means=np.asarray(means, dtype=np.float64),
        scales=np.asarray(scales, dtype=np.float64),
        coef=np.asarray(coef, dtype=np.float64),
        intercept=decode_number(parameters["intercept"], where="intercept"),
    )


def compile_scorer(
    model_type: str,
    preprocessing: Mapping[str, Any],
    parameters: Mapping[str, Any],
    n_features: int,
) -> NumpyScorer:
    """Compile stored model parameters into a numpy scorer (raw log-odds ``z``).

    Raises :class:`~trader.models.codec.EncodingError` for anything malformed or unsupported.
    """
    if model_type == "logistic":
        return _compile_logistic(preprocessing, parameters, n_features)
    if model_type == "lightgbm":
        if preprocessing:
            raise EncodingError("lightgbm bundles take no preprocessing")
        if set(parameters) != {"trees"}:
            raise EncodingError("lightgbm parameters need exactly 'trees'")
        return _compile_lightgbm(parameters["trees"], n_features)
    raise EncodingError(f"unsupported model type {model_type!r}")


# ----------------------------------------------------------------------------- bundle


class BundleContent(ConfigModel):
    """Everything a bundle records except its checksum (validated, float-free)."""

    bundle_id: str = Field(min_length=1)
    model_type: ModelType
    feature_version: str = Field(min_length=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    family: str = Field(min_length=1)
    training_start: UtcDatetime
    training_cutoff: UtcDatetime
    fold_index: int = Field(ge=0)
    preprocessing: dict[str, Any]
    parameters: dict[str, Any]
    calibration: dict[str, Any]
    threshold: Fraction
    hyperparameters: dict[str, str]
    seed: int = Field(ge=0)
    library_versions: dict[str, str]
    data_hashes: dict[str, str]

    @field_validator("preprocessing", "parameters", "calibration", mode="after")
    @classmethod
    def _float_free(cls, value: dict[str, Any]) -> dict[str, Any]:
        frozen = frozen_json(value, where="bundle content")
        if not isinstance(frozen, dict):  # pragma: no cover - a dict stays a dict
            raise ValueError("expected a mapping")
        return frozen

    @model_validator(mode="after")
    def _coherent(self) -> BundleContent:
        if len(set(self.feature_names)) != len(self.feature_names) or not all(self.feature_names):
            raise ValueError("feature names must be non-empty and unique")
        if not self.training_start < self.training_cutoff:
            raise ValueError("training_start must precede training_cutoff")
        compile_scorer(
            self.model_type, self.preprocessing, self.parameters, len(self.feature_names)
        )
        compile_calibration(self.calibration)
        return self

    def content_checksum(self) -> str:
        """SHA-256 of the canonical JSON of every field except ``checksum``."""
        return content_hash(self.model_dump(mode="python", exclude={"checksum"}))


@dataclass(frozen=True, slots=True, eq=False)
class _Compiled:
    scorer: NumpyScorer
    calibrator: Calibrator


class ModelBundle(BundleContent):
    """A sealed, checksummed candidate-filter model (architecture.md §3).

    Build one with :meth:`seal`; :meth:`load` and direct construction verify the checksum.
    Implements the research ``Scorer`` protocol (``bundle_id`` + ``predict_proba``).
    """

    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    _compiled: _Compiled | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _sealed(self) -> ModelBundle:
        expected = self.content_checksum()
        if expected != self.checksum:
            raise BundleIntegrityError(
                f"bundle {self.bundle_id!r}: checksum {self.checksum} does not match its "
                f"content ({expected}); the bundle was modified or corrupted"
            )
        return self

    def model_post_init(self, context: Any, /) -> None:
        self._compiled = _Compiled(
            scorer=compile_scorer(
                self.model_type, self.preprocessing, self.parameters, len(self.feature_names)
            ),
            calibrator=compile_calibration(self.calibration),
        )

    @classmethod
    def seal(cls, **fields: Any) -> ModelBundle:
        """Validate the content, compute its checksum and return the sealed bundle."""
        if "checksum" in fields:
            raise BundleIntegrityError("seal() computes the checksum; do not pass one")
        content = BundleContent.model_validate(fields)
        payload = content.model_dump(mode="python")
        return cls.model_validate({**payload, "checksum": content.content_checksum()})

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        if update:
            raise BundleIntegrityError(
                "a sealed ModelBundle cannot be modified; build a new one with ModelBundle.seal"
            )
        return super().model_copy(deep=deep)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModelBundle):
            return NotImplemented
        return self.checksum == other.checksum and self.model_dump() == other.model_dump()

    def __hash__(self) -> int:
        return hash(self.checksum)

    def verify(self) -> None:
        """Re-check the checksum against the current content (fails if a nested dict mutated)."""
        expected = self.content_checksum()
        if expected != self.checksum:
            raise BundleIntegrityError(
                f"bundle {self.bundle_id!r} content no longer matches its checksum"
            )

    # ------------------------------------------------------------------ inference

    @property
    def _engine(self) -> _Compiled:
        if self._compiled is None:  # pragma: no cover - model_post_init always sets it
            raise BundleIntegrityError("bundle was not compiled")
        return self._compiled

    def _matrix(self, x: FloatArray) -> FloatArray:
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != len(self.feature_names):
            raise BundleInputError(
                f"expected a matrix with {len(self.feature_names)} feature columns "
                f"{self.feature_names}, got shape {arr.shape}"
            )
        return arr

    def raw_scores(self, x: FloatArray) -> FloatArray:
        """Raw log-odds ``z`` for each row (columns in ``feature_names`` order)."""
        return self._engine.scorer.raw_scores(self._matrix(x))

    def uncalibrated_proba(self, x: FloatArray) -> FloatArray:
        """``sigmoid(z)``: what the training library's ``predict_proba[:, 1]`` returns."""
        return sigmoid(self.raw_scores(x))

    def predict_proba_matrix(self, x: FloatArray) -> FloatArray:
        """Calibrated probability that each row's candidate is profitable after costs."""
        return self._engine.calibrator.apply(self.raw_scores(x))

    def predict_proba(self, features: Mapping[str, float]) -> float:
        """Calibrated probability for one candidate's feature snapshot.

        Every name in ``feature_names`` must be present (extra keys are ignored). Logistic
        bundles refuse non-finite values; LightGBM bundles apply LightGBM's missing-value rules.
        """
        missing = [name for name in self.feature_names if name not in features]
        if missing:
            raise BundleInputError(
                f"bundle {self.bundle_id!r} (feature version {self.feature_version}) "
                f"is missing features {missing}"
            )
        row = np.asarray([[float(features[name]) for name in self.feature_names]])
        return float(self.predict_proba_matrix(row)[0])

    # ------------------------------------------------------------------ persistence

    def to_json(self) -> str:
        """Deterministic, human-readable canonical JSON (sorted keys, numbers as strings)."""
        return json.dumps(json.loads(canonical_json(self)), indent=2, sort_keys=True) + "\n"

    def save(self, path: Path) -> None:
        self.verify()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> ModelBundle:
        """Load and verify a bundle; raises :class:`BundleIntegrityError` on a bad checksum."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
