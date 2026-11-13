"""Model bundles (architecture.md §3 ``ModelBundle``; ADR-0027; docs/ml_research_guide.md §1).

* numpy-only inference equals scikit-learn and LightGBM ``predict_proba`` (and the same
  calibration) within 1e-9, including LightGBM's missing-value rules;
* every number is an exact decimal string, the JSON round trip is lossless and deterministic;
* the checksum detects tampering, and a sealed bundle cannot be modified;
* ``trader.models`` never loads a training library.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import lightgbm
import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import ValidationError
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from trader.domain.hashing import content_hash
from trader.models.bundle import (
    BundleInputError,
    BundleIntegrityError,
    ModelBundle,
)
from trader.models.calibration import (
    CalibrationError,
    compile_calibration,
    isotonic_spec,
    none_spec,
    platt_spec,
    sigmoid,
)
from trader.models.codec import EncodingError, decode_number, encode_float
from trader.research.training import export_lightgbm, export_logistic

NAMES = ("ret_1", "atr_pct", "range_pos", "volume_z")
TOL = 1e-9


def _data(seed: int, n: int) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, len(NAMES))) * [1.0, 3.0, 0.1, 10.0] + [0.0, 5.0, -1.0, 100.0]
    latent = x[:, 0] + 0.3 * (x[:, 1] - 5.0) - 4.0 * (x[:, 2] + 1.0) + rng.normal(size=n)
    return x, (latent > 0.4).astype(np.int64)


def _fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "bundle_id": "test-bundle",
        "feature_version": "candidate-v1",
        "feature_names": NAMES,
        "family": "CANDIDATE_A",
        "training_start": datetime(2024, 1, 1, tzinfo=UTC),
        "training_cutoff": datetime(2025, 1, 1, tzinfo=UTC),
        "fold_index": 1,
        "calibration": none_spec(),
        "threshold": Decimal("0.55"),
        "hyperparameters": {"C": "1"},
        "seed": 7,
        "library_versions": {"numpy": np.__version__},
        "data_hashes": {"train_rows_sha256": "0" * 64},
    }
    fields.update(overrides)
    return fields


def _logistic(**overrides: Any) -> tuple[ModelBundle, StandardScaler, LogisticRegression]:
    x, y = _data(1, 500)
    scaler = StandardScaler().fit(x)
    model = LogisticRegression(C=0.5, max_iter=1000).fit(scaler.transform(x), y)
    preprocessing, parameters = export_logistic(scaler, model)
    bundle = ModelBundle.seal(
        model_type="logistic",
        preprocessing=preprocessing,
        parameters=parameters,
        **_fields(**overrides),
    )
    return bundle, scaler, model


def _lightgbm(
    x: NDArray[np.float64], y: NDArray[np.int64], **params: Any
) -> tuple[ModelBundle, lightgbm.LGBMClassifier]:
    settings: dict[str, Any] = {
        "n_estimators": 40,
        "num_leaves": 7,
        "min_child_samples": 10,
        "n_jobs": 1,
        "deterministic": True,
        "force_col_wise": True,
        "random_state": 3,
        "verbosity": -1,
    }
    model = lightgbm.LGBMClassifier(**(settings | params)).fit(x, y)
    bundle = ModelBundle.seal(
        model_type="lightgbm", preprocessing={}, parameters=export_lightgbm(model), **_fields()
    )
    return bundle, model


def _missing_types(bundle: ModelBundle) -> set[str]:
    found: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        if "split_feature" in node:
            found.add(node["missing_type"])
            walk(node["left_child"])
            walk(node["right_child"])

    for tree in bundle.parameters["trees"]["tree_info"]:
        walk(tree["tree_structure"])
    return found


def _probe(seed: int, n: int = 400) -> NDArray[np.float64]:
    x, _ = _data(seed, n)
    rng = np.random.default_rng(seed + 100)
    x[rng.random(n) < 0.2, 0] = np.nan
    x[rng.random(n) < 0.2, 1] = 0.0
    x[rng.random(n) < 0.1, 2] = np.nan
    x[rng.random(n) < 0.05, 3] = -0.0
    return x


# ----------------------------------------------------------------------------- equivalence


def test_logistic_numpy_inference_equals_scikit_learn() -> None:
    bundle, scaler, model = _logistic()
    x, _ = _data(2, 300)
    expected = model.predict_proba(scaler.transform(x))[:, 1]
    assert np.abs(bundle.uncalibrated_proba(x) - expected).max() < TOL
    decision = model.decision_function(scaler.transform(x))
    assert np.abs(bundle.raw_scores(x) - decision).max() < TOL
    # calibration "none" is the uncalibrated probability
    assert np.abs(bundle.predict_proba_matrix(x) - expected).max() < TOL


def test_platt_and_isotonic_calibration_match_their_reference_maps() -> None:
    plain, scaler, model = _logistic()
    x_val, y_val = _data(4, 300)
    decision = model.decision_function(scaler.transform(x_val))
    proba = model.predict_proba(scaler.transform(x_val))[:, 1]

    a, b = 0.8, -0.3
    platt, _, _ = _logistic(calibration=platt_spec(a, b))
    expected = 1.0 / (1.0 + np.exp(-(a * decision + b)))
    assert np.abs(platt.predict_proba_matrix(x_val) - expected).max() < TOL

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(proba, y_val)
    isotonic, _, _ = _logistic(calibration=isotonic_spec(iso.X_thresholds_, iso.y_thresholds_))
    x_new, _ = _data(5, 300)
    reference = iso.predict(model.predict_proba(scaler.transform(x_new))[:, 1])
    assert np.abs(isotonic.predict_proba_matrix(x_new) - reference).max() < TOL
    assert plain.checksum not in (platt.checksum, isotonic.checksum)


@pytest.mark.parametrize(
    ("nan_in_training", "zero_as_missing", "expected_types"),
    [
        (True, False, {"NaN"}),  # NaN seen in training: NaN goes the learned default way
        (False, False, {"None"}),  # no missing values: NaN is read as 0.0
        (False, True, {"Zero"}),  # zero_as_missing: 0 and NaN go the default way
    ],
)
def test_lightgbm_numpy_inference_equals_lightgbm_with_missing_values(
    nan_in_training: bool, zero_as_missing: bool, expected_types: set[str]
) -> None:
    x, y = _data(11, 800)
    if nan_in_training:
        x[np.random.default_rng(12).random(800) < 0.15, 0] = np.nan
    bundle, model = _lightgbm(x, y, zero_as_missing=zero_as_missing)
    assert expected_types <= _missing_types(bundle)
    probe = _probe(13)
    assert np.isnan(probe).any() and (probe == 0.0).any()
    expected = model.predict_proba(probe)[:, 1]
    assert np.abs(bundle.uncalibrated_proba(probe) - expected).max() < TOL
    raw = model.predict(probe, raw_score=True)
    assert np.abs(bundle.raw_scores(probe) - raw).max() < TOL

    a, b = 1.3, 0.2
    calibrated = ModelBundle.seal(
        model_type="lightgbm",
        preprocessing={},
        parameters=bundle.parameters,
        **_fields(calibration=platt_spec(a, b)),
    )
    assert np.abs(calibrated.predict_proba_matrix(probe) - sigmoid(a * raw + b)).max() < TOL


def test_lightgbm_single_leaf_trees_are_supported() -> None:
    x, y = _data(21, 60)
    bundle, model = _lightgbm(x, y, min_child_samples=1000)  # no split is possible
    probe = _probe(22, 50)
    assert np.abs(bundle.uncalibrated_proba(probe) - model.predict_proba(probe)[:, 1]).max() < TOL


# ----------------------------------------------------------------------------- inputs


def test_predict_proba_on_a_feature_mapping() -> None:
    bundle, scaler, model = _logistic()
    x, _ = _data(3, 5)
    for row in x:
        features = dict(zip(NAMES, (float(v) for v in row), strict=True))
        expected = model.predict_proba(scaler.transform(row[None, :]))[0, 1]
        assert abs(bundle.predict_proba(features) - expected) < TOL
        assert bundle.predict_proba({**features, "unused": 1.0}) == bundle.predict_proba(features)
    with pytest.raises(BundleInputError, match="missing features"):
        bundle.predict_proba({"ret_1": 0.1})
    with pytest.raises(BundleInputError, match="NaN"):
        bundle.predict_proba({**dict.fromkeys(NAMES, 0.0), "ret_1": math.nan})
    with pytest.raises(BundleInputError, match="feature columns"):
        bundle.predict_proba_matrix(np.zeros((2, 3)))


def test_lightgbm_bundle_scores_a_missing_value_like_lightgbm() -> None:
    x, y = _data(31, 600)
    x[np.random.default_rng(32).random(600) < 0.2, 0] = np.nan
    bundle, model = _lightgbm(x, y)
    features = {"ret_1": math.nan, "atr_pct": 5.0, "range_pos": -1.0, "volume_z": 100.0}
    row = np.asarray([[math.nan, 5.0, -1.0, 100.0]])
    assert abs(bundle.predict_proba(features) - model.predict_proba(row)[0, 1]) < TOL


# ----------------------------------------------------------------------------- persistence


def test_json_round_trip_is_exact_and_deterministic(tmp_path: Path) -> None:
    x, y = _data(41, 600)
    for bundle in (_logistic()[0], _lightgbm(x, y)[0]):
        first, second = tmp_path / "a" / "bundle.json", tmp_path / "b.json"
        bundle.save(first)
        bundle.save(second)
        assert first.read_bytes() == second.read_bytes()
        loaded = ModelBundle.load(first)
        assert loaded == bundle and loaded.checksum == bundle.checksum
        assert loaded.threshold == Decimal("0.55")
        assert loaded.training_cutoff == datetime(2025, 1, 1, tzinfo=UTC)
        probe, _ = _data(42, 300)
        assert np.array_equal(
            loaded.predict_proba_matrix(probe), bundle.predict_proba_matrix(probe)
        )
        # the checksum is the canonical hash of everything except itself
        payload = json.loads(first.read_text(encoding="utf-8"))
        assert payload.pop("checksum") == content_hash(payload) == bundle.checksum

        def no_floats(text: str) -> float:
            raise AssertionError(f"binary float {text} in a bundle file")

        json.loads(first.read_text(encoding="utf-8"), parse_float=no_floats)


def test_tampering_is_detected_by_the_checksum(tmp_path: Path) -> None:
    bundle, _, _ = _logistic()
    path = tmp_path / "bundle.json"
    bundle.save(path)
    original = json.loads(path.read_text(encoding="utf-8"))

    for mutate in (
        lambda p: p["parameters"]["coef"].__setitem__(0, "0.5"),
        lambda p: p.__setitem__("threshold", "0.4"),
        lambda p: p["preprocessing"]["means"].__setitem__(1, "4.9"),
        lambda p: p["data_hashes"].__setitem__("train_rows_sha256", "1" * 64),
        lambda p: p.__setitem__("checksum", "f" * 64),
    ):
        payload = json.loads(json.dumps(original))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(BundleIntegrityError, match="checksum"):
            ModelBundle.load(path)

    with pytest.raises(BundleIntegrityError, match="cannot be modified"):
        bundle.model_copy(update={"threshold": Decimal("0.1")})
    assert bundle.model_copy() == bundle
    bundle.parameters["coef"][0] = "9"  # mutating nested content in place
    with pytest.raises(BundleIntegrityError, match="no longer matches"):
        bundle.save(tmp_path / "mutated.json")


def test_binary_floats_and_malformed_content_are_refused() -> None:
    bundle, _, _ = _logistic()
    good = {"preprocessing": bundle.preprocessing, "parameters": bundle.parameters}
    with pytest.raises(BundleIntegrityError, match="computes the checksum"):
        ModelBundle.seal(model_type="logistic", **good, **_fields(), checksum="0" * 64)
    bad_cases: list[dict[str, Any]] = [
        {"parameters": {**bundle.parameters, "intercept": 0.25}},  # binary float
        {"parameters": {**bundle.parameters, "coef": bundle.parameters["coef"][:2]}},
        {"preprocessing": {**bundle.preprocessing, "scales": ["0"] * len(NAMES)}},
        {"threshold": 0.5},
        {"threshold": Decimal("1.5")},
        {"feature_names": ("a", "a", "b", "c")},
        {"training_start": datetime(2026, 1, 1, tzinfo=UTC)},
        {"training_cutoff": datetime(2025, 1, 1)},  # naive  # noqa: DTZ001
        {"calibration": {"method": "beta", "params": {}}},
        {"calibration": {"method": "platt", "params": {"a": "1"}}},
    ]
    for override in bad_cases:
        fields = {**good, **_fields()}
        fields.update(override)
        with pytest.raises(ValidationError):
            ModelBundle.seal(model_type="logistic", **fields)


def test_unsupported_lightgbm_structures_are_refused() -> None:
    x, y = _data(51, 400)
    bundle, _ = _lightgbm(x, y)
    trees = json.loads(json.dumps(bundle.parameters["trees"]))

    def with_change(change: Any) -> dict[str, Any]:
        copy = json.loads(json.dumps(trees))
        change(copy)
        return {"trees": copy}

    root = "tree_structure"
    for change, message in (
        (lambda t: t.__setitem__("objective", "regression"), "binary objective"),
        (lambda t: t.__setitem__("average_output", True), "averaged"),
        (lambda t: t.__setitem__("num_class", 3), "single-output"),
        (lambda t: t["tree_info"][0][root].__setitem__("decision_type", "=="), "numerical"),
        (lambda t: t["tree_info"][0][root].__setitem__("split_feature", 9), "split_feature"),
        (lambda t: t["tree_info"][0][root].__setitem__("missing_type", "Other"), "missing_type"),
        (lambda t: t["tree_info"][0][root].__setitem__("threshold", "nan"), "NaN"),
        (lambda t: t.__setitem__("max_feature_idx", 7), "features"),
    ):
        with pytest.raises(ValidationError, match=message):
            ModelBundle.seal(
                model_type="lightgbm", preprocessing={}, parameters=with_change(change), **_fields()
            )


# ----------------------------------------------------------------------------- codec


def test_float_encoding_round_trips_exactly() -> None:
    rng = np.random.default_rng(61)
    values = [
        *rng.normal(size=200) * 10.0 ** rng.integers(-30, 30, size=200),
        1e-35,
        1.0000000180025095e-35,
        5e-324,
        1.7976931348623157e308,
        -0.0,
        0.1,
        1 / 3,
    ]
    for value in values:
        text = encode_float(float(value))
        assert "e" not in text.lower()
        assert float(text) == float(value)
        assert decode_number(text, where="x") == float(value)
    for bad in (math.nan, math.inf):
        with pytest.raises(EncodingError):
            encode_float(bad)
    for raw in (0.5, True, None, "abc", "Infinity"):
        with pytest.raises(EncodingError):
            decode_number(raw, where="x")
    assert decode_number(3, where="x") == 3.0


def test_calibration_maps_are_validated() -> None:
    assert compile_calibration(none_spec()).method == "none"
    for spec in (
        {"method": "none", "params": {"a": "1"}},
        {"method": "isotonic", "params": {"x": ["0.2", "0.1"], "y": ["0", "1"]}},
        {"method": "isotonic", "params": {"x": ["0.1", "0.2"], "y": ["0.9", "0.1"]}},
        {"method": "isotonic", "params": {"x": ["0.1", "1.2"], "y": ["0", "1"]}},
        {"method": "isotonic", "params": {"x": [], "y": []}},
        {"method": "isotonic", "params": {"x": ["0.1"], "y": ["0.1", "0.2"]}},
        {"method": "platt", "params": {"a": 1.0, "b": "0"}},
        {"method": "platt"},
    ):
        with pytest.raises(CalibrationError):
            compile_calibration(spec)
    iso = compile_calibration(isotonic_spec(np.array([0.2, 0.6]), np.array([0.1, 0.9])))
    z = np.log(np.array([0.1, 0.4, 0.9]) / (1 - np.array([0.1, 0.4, 0.9])))
    assert np.allclose(iso.apply(z), [0.1, 0.5, 0.9], atol=1e-12)  # clipped, interpolated


def test_the_models_package_never_loads_a_training_library() -> None:
    code = (
        "import sys, trader.models.bundle, trader.models.calibration; "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('sklearn', 'lightgbm', 'scipy') or m.startswith('trader.research')); "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
