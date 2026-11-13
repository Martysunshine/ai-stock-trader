"""Walk-forward training of candidate filters (docs/ml_research_guide.md §1, §5;
risk_and_validation.md §5; strategy_and_profitability.md §6-§7).

* purge/embargo removes every training label that reaches into the embargo before the
  training end;
* validation and test rows cannot change the scaler or the model coefficients; calibration
  and the threshold are fitted on validation only;
* every grid point and threshold candidate is logged; training is seeded and reproducible;
* anything that would leak or mismatch data fails closed.

All data here is synthetic (no real market data is evaluated).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from trader.domain.types import DomainError
from trader.models.bundle import ModelBundle
from trader.models.calibration import isotonic_spec, platt_spec, sigmoid
from trader.research.training import (
    DEFAULT_LOGISTIC_GRID,
    FoldData,
    TrainingError,
    TrainResult,
    fit_isotonic,
    fit_platt,
    float_at_least,
    fold_data_from_labelset,
    purge,
    to_datetime64,
    train_fold,
)

NAMES = ("ret_1", "atr_pct", "range_pos", "volume_z")
FAMILY = "CANDIDATE_A"
T0 = datetime(2024, 1, 1, tzinfo=UTC)
TRAIN_END = datetime(2025, 1, 1, tzinfo=UTC)
VAL_END = datetime(2025, 4, 1, tzinfo=UTC)
TEST_END = datetime(2025, 7, 1, tzinfo=UTC)
EMBARGO = 360
THRESHOLDS = tuple(Decimal(n) / 100 for n in range(30, 81, 5))
ONE_POINT = ({"C": "1", "class_weight": "none"},)
LGBM_GRID = (
    {"num_leaves": "7", "min_child_samples": "20", "learning_rate": "0.1", "n_estimators": "30"},
    {"num_leaves": "4", "min_child_samples": "40", "learning_rate": "0.1", "n_estimators": "40"},
)


# ----------------------------------------------------------------------------- fixtures


@dataclasses.dataclass(frozen=True)
class Row:
    """Shape of research.labels.LabelRow (task C), duck-typed here."""

    candidate_id: str
    family: str
    instrument_id: str
    decision_time: datetime
    exit_time: datetime | None
    status: str
    net_usd: Decimal
    net_return: float
    win: bool
    features: Mapping[str, float]


@dataclasses.dataclass(frozen=True)
class Labels:
    """Shape of research.labels.LabelSet (task C), duck-typed here."""

    rows: tuple[Row, ...]
    feature_names: tuple[str, ...] = NAMES
    feature_version: str = "candidate-v1"
    source: Mapping[str, str] = dataclasses.field(
        default_factory=lambda: {"dataset": "synthetic", "content_sha256": "ab" * 32}
    )

    def between(self, start: datetime, end: datetime) -> Labels:
        rows = tuple(r for r in self.rows if start <= r.decision_time < end)
        return dataclasses.replace(self, rows=rows)

    def matrix(self) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
        x = np.asarray(
            [[r.features[n] for n in self.feature_names] for r in self.rows], dtype=np.float64
        ).reshape(len(self.rows), len(self.feature_names))
        return x, np.asarray([r.win for r in self.rows], dtype=np.int64)


def make_labels(seed: int = 5, *, families: tuple[str, ...] = (FAMILY,)) -> Labels:
    rng = np.random.default_rng(seed)
    rows = []
    t = T0
    i = 0
    while t < TEST_END:
        x = rng.normal(size=len(NAMES))
        latent = 1.2 * x[0] - 0.8 * x[2] + 0.6 * rng.normal()
        filled = bool(rng.random() > 0.1)
        net_return = 0.004 * latent + 0.001 * rng.normal() - 0.002 if filled else 0.0
        hold = timedelta(minutes=int(rng.integers(30, 360)))
        rows.append(
            Row(
                candidate_id=f"c{i:05d}",
                family=families[i % len(families)],
                instrument_id="SYN:AAA",
                decision_time=t,
                exit_time=t + hold if filled else None,
                status="FILLED" if filled else "UNFILLED",
                net_usd=Decimal(f"{50 * net_return:.6f}"),
                net_return=net_return,
                win=net_return > 0,
                features=dict(zip(NAMES, (float(v) for v in x), strict=True)),
            )
        )
        t += timedelta(hours=6)
        i += 1
    return Labels(rows=tuple(rows))


def windows(labels: Labels) -> tuple[FoldData, FoldData]:
    train = purge(
        fold_data_from_labelset(labels.between(T0, TRAIN_END)),
        train_end=TRAIN_END,
        embargo_minutes=EMBARGO,
    )
    val = fold_data_from_labelset(labels.between(TRAIN_END, VAL_END - timedelta(minutes=EMBARGO)))
    return train, val


def run(train: FoldData, val: FoldData, **overrides: Any) -> TrainResult:
    kwargs: dict[str, Any] = {
        "model_type": "logistic",
        "grid": ONE_POINT,
        "seed": 7,
        "fold_index": 1,
        "training_start": T0,
        "training_cutoff": TRAIN_END,
        "threshold_grid": THRESHOLDS,
        "min_accepts": 10,
    }
    kwargs.update(overrides)
    return train_fold(train, val, **kwargs)


@pytest.fixture(scope="module")
def labels() -> Labels:
    return make_labels()


@pytest.fixture(scope="module")
def folds(labels: Labels) -> tuple[FoldData, FoldData]:
    return windows(labels)


def ms(text: str) -> np.datetime64:
    return np.datetime64(text, "ms")


def hand_fold(rows: list[tuple[str, str]]) -> FoldData:
    n = len(rows)
    return FoldData(
        X=np.arange(2 * n, dtype=np.float64).reshape(n, 2),
        y=np.asarray([i % 2 for i in range(n)], dtype=np.int64),
        decision_time=np.asarray([ms(d) for d, _ in rows]),
        exit_time=np.asarray([ms(e) for _, e in rows]),
        net_return=np.zeros(n),
        feature_names=("a", "b"),
        feature_version="v",
        family=FAMILY,
    )


# ----------------------------------------------------------------------------- purge


def test_purge_drops_labels_whose_decision_or_exit_reaches_the_embargo() -> None:
    fold = hand_fold(
        [
            ("2024-12-31T20:00", "2024-12-31T22:59"),  # 0 keep: exit before 23:00
            ("2024-12-31T21:00", "2024-12-31T22:00"),  # 1 keep
            ("2024-12-31T21:30", "2024-12-31T23:00"),  # 2 drop: exit at the embargo start
            ("2024-12-31T22:00", "NaT"),  # 3 keep: never filled
            ("2024-12-31T22:59", "NaT"),  # 4 keep
            ("2024-12-31T23:00", "NaT"),  # 5 drop: decision inside the embargo
            ("2024-12-31T23:30", "2024-12-31T23:45"),  # 6 drop
            ("2024-12-31T23:59", "2025-01-01T02:00"),  # 7 drop: exit after the training end
        ]
    )
    kept = purge(fold, train_end=TRAIN_END, embargo_minutes=60)
    assert kept.X[:, 0].tolist() == [0.0, 2.0, 6.0, 8.0]  # rows 0, 1, 3, 4
    no_embargo = purge(fold, train_end=TRAIN_END, embargo_minutes=0)
    assert len(no_embargo) == 7  # only the row whose exit passes the training end goes
    assert len(purge(fold, train_end=TRAIN_END, embargo_minutes=24 * 60)) == 0
    with pytest.raises(TrainingError, match="negative"):
        purge(fold, train_end=TRAIN_END, embargo_minutes=-1)
    with pytest.raises(DomainError, match="naive"):
        purge(fold, train_end=datetime(2025, 1, 1), embargo_minutes=0)  # noqa: DTZ001


def test_training_refuses_unpurged_labels(folds: tuple[FoldData, FoldData]) -> None:
    train, val = folds
    late = train.exit_time.copy()
    late[-1] = to_datetime64(TRAIN_END) + np.timedelta64(30, "m")
    leaking = dataclasses.replace(train, exit_time=late)
    with pytest.raises(TrainingError, match="purge"):
        run(leaking, val)


# ----------------------------------------------------------------------------- isolation


def test_validation_rows_cannot_change_the_scaler_or_coefficients(
    folds: tuple[FoldData, FoldData],
) -> None:
    train, val = folds
    garbage = dataclasses.replace(
        val, X=val.X[::-1] * 5.0 + 3.0, y=1 - val.y, net_return=-val.net_return
    )
    for model_type, grid in (("logistic", ONE_POINT), ("lightgbm", LGBM_GRID[:1])):
        base = run(train, val, model_type=model_type, grid=grid, min_accepts=1)
        other = run(train, garbage, model_type=model_type, grid=grid, min_accepts=1)
        assert other.bundle.preprocessing == base.bundle.preprocessing
        assert other.bundle.parameters == base.bundle.parameters
        assert other.bundle.calibration != base.bundle.calibration  # calibration is validation's


def test_the_selected_model_equals_a_fit_on_training_rows_alone(
    folds: tuple[FoldData, FoldData],
) -> None:
    train, val = folds
    full = run(train, val, grid=DEFAULT_LOGISTIC_GRID)
    selected = int(full.val_metrics["selected_grid_index"])
    alone = run(train, val, grid=(DEFAULT_LOGISTIC_GRID[selected],))
    assert full.bundle.preprocessing == alone.bundle.preprocessing
    assert full.bundle.parameters == alone.bundle.parameters


def test_test_window_rows_cannot_change_the_bundle(labels: Labels) -> None:
    cut = VAL_END - timedelta(minutes=EMBARGO)
    corrupted = dataclasses.replace(
        labels,
        rows=tuple(
            r
            if r.decision_time < cut
            else dataclasses.replace(
                r,
                win=not r.win,
                net_return=-r.net_return,
                features=dict.fromkeys(NAMES, 99.0),
            )
            for r in labels.rows
        ),
    )
    assert corrupted.rows != labels.rows
    for model_type, grid in (("logistic", DEFAULT_LOGISTIC_GRID[:4]), ("lightgbm", LGBM_GRID)):
        a = run(*windows(labels), model_type=model_type, grid=grid)
        b = run(*windows(corrupted), model_type=model_type, grid=grid)
        assert a.bundle.checksum == b.bundle.checksum
        assert a.selection_log == b.selection_log


def test_calibration_and_threshold_are_fitted_on_validation_only(
    folds: tuple[FoldData, FoldData],
) -> None:
    train, val = folds
    result = run(train, val)
    bundle = result.bundle
    z_val = bundle.raw_scores(val.X)
    assert bundle.calibration == platt_spec(*fit_platt(z_val, val.y))

    # training net returns are never read: calibration and threshold do not move
    shuffled = dataclasses.replace(train, net_return=train.net_return[::-1] * -3.0)
    again = run(shuffled, val)
    assert again.bundle.calibration == bundle.calibration
    assert again.bundle.threshold == bundle.threshold
    assert again.bundle.parameters == bundle.parameters

    # the threshold maximises the mean validation net return among eligible thresholds
    p_val = bundle.predict_proba_matrix(val.X)
    best: tuple[Decimal, Decimal] | None = None
    for t in sorted(THRESHOLDS):
        accepted = p_val >= float_at_least(t)
        if accepted.sum() >= 10:
            objective = Decimal(f"{float(val.net_return[accepted].mean()):.12f}")
            if best is None or objective > best[1]:
                best = (t, objective)
    assert best is not None and bundle.threshold == best[0]
    assert result.val_metrics["objective"] == format(best[1].normalize(), "f")

    isotonic = run(train, val, calibration="isotonic").bundle
    assert isotonic.calibration == isotonic_spec(*fit_isotonic(sigmoid(z_val), val.y))
    assert run(train, val, calibration="none").bundle.calibration["method"] == "none"


def test_threshold_objective_callback_and_minimum_accepts(
    folds: tuple[FoldData, FoldData],
) -> None:
    train, val = folds
    seen: list[ModelBundle] = []

    def objective(candidate: ModelBundle) -> Decimal:
        seen.append(candidate)
        return Decimal(1) - abs(candidate.threshold - Decimal("0.45"))

    result = run(train, val, threshold_objective=objective, min_accepts=1)
    assert result.bundle.threshold == Decimal("0.45")
    assert result.bundle in seen
    assert len({b.threshold for b in seen}) == len(seen)
    assert {b.parameters == result.bundle.parameters for b in seen} == {True}
    assert {b.calibration == result.bundle.calibration for b in seen} == {True}
    assert result.val_metrics["objective_source"] == "callback"
    eligible = [e for e in result.selection_log if e.get("eligible") == "true"]
    assert len(eligible) == len(seen)

    with pytest.raises(TrainingError, match="no threshold accepts"):
        run(train, val, min_accepts=len(val) + 1)
    with pytest.raises(TrainingError, match="finite Decimal"):
        run(train, val, threshold_objective=lambda b: 0.5, min_accepts=1)


# ----------------------------------------------------------------------------- logging


def test_selection_log_records_every_grid_point_and_threshold(
    folds: tuple[FoldData, FoldData],
) -> None:
    train, val = folds
    for model_type, grid in (("logistic", DEFAULT_LOGISTIC_GRID), ("lightgbm", LGBM_GRID)):
        result = run(train, val, model_type=model_type, grid=grid)
        log = result.selection_log
        assert all(isinstance(k, str) and isinstance(v, str) for e in log for k, v in e.items())
        grid_rows = [e for e in log if e["stage"] == "grid"]
        assert len(grid_rows) == len(grid)
        losses = [float(e["val_log_loss"]) for e in grid_rows]
        selected = next(e for e in log if e["stage"] == "selected")
        assert int(selected["index"]) == losses.index(min(losses))
        assert [e["stage"] for e in log].count("threshold") == len(THRESHOLDS)
        assert [e["stage"] for e in log].count("calibration") == 1
        assert all(isinstance(v, str) for v in result.val_metrics.values())
        bundle = result.bundle
        assert bundle.model_type == model_type and bundle.family == FAMILY
        assert bundle.training_cutoff == TRAIN_END and bundle.fold_index == 1
        assert bundle.feature_names == NAMES and bundle.feature_version == "candidate-v1"
        assert "scikit-learn" in bundle.library_versions
        assert ("lightgbm" in bundle.library_versions) == (model_type == "lightgbm")
        assert bundle.data_hashes["source.dataset"] == "synthetic"
        assert bundle.data_hashes["train_rows_sha256"] == train.digest()
        assert bundle.data_hashes["validation_rows_sha256"] == val.digest()
        for key, value in grid[int(selected["index"])].items():
            assert bundle.hyperparameters[key] == value


def test_training_is_seeded_and_reproducible(labels: Labels) -> None:
    for model_type, grid in (("logistic", DEFAULT_LOGISTIC_GRID), ("lightgbm", LGBM_GRID)):
        first = run(*windows(labels), model_type=model_type, grid=grid)
        second = run(*windows(labels), model_type=model_type, grid=grid)
        assert first.bundle.checksum == second.bundle.checksum
        assert first.bundle.to_json() == second.bundle.to_json()
        assert first.selection_log == second.selection_log
        assert first.val_metrics == second.val_metrics
        assert first.bundle.bundle_id.startswith(f"candidate_a-{model_type}-f01-")


# ----------------------------------------------------------------------------- fail closed


def test_invalid_training_requests_fail_closed(folds: tuple[FoldData, FoldData]) -> None:
    train, val = folds
    renamed = dataclasses.replace(val, feature_names=("a", "b", "c", "d"))
    one_class = dataclasses.replace(train, y=np.zeros(len(train), dtype=np.int64))
    cases: list[tuple[tuple[FoldData, FoldData], dict[str, Any], str]] = [
        ((train, renamed), {}, "differ in features"),
        ((train, val), {"training_cutoff": datetime(2024, 6, 1, tzinfo=UTC)}, "outside"),
        ((train, val), {"training_start": datetime(2024, 6, 1, tzinfo=UTC)}, "outside"),
        ((train, train), {}, "precede the training cutoff"),
        ((one_class, val), {}, "both profitable"),
        ((train, val), {"model_type": "svm"}, "model_type"),
        ((train, val), {"calibration": "beta"}, "calibration"),
        ((train, val), {"grid": ()}, "grid is empty"),
        ((train, val), {"grid": ({"C": "1", "penalty": "l1"},)}, "unknown logistic"),
        ((train, val), {"grid": ({"C": 0.5},)}, "binary floating point"),
        ((train, val), {"grid": ({"C": "-1"},)}, "positive"),
        ((train, val), {"threshold_grid": (0.5,)}, "Decimal"),
        ((train, val), {"threshold_grid": (Decimal("1.5"),)}, "Decimal"),
        ((train, val), {"threshold_grid": (Decimal("0.5"), Decimal("0.50"))}, "duplicates"),
        ((train, val), {"seed": -1}, "negative"),
        (
            (train, val),
            {"model_type": "lightgbm", "grid": ({"num_leaves": "1.5"},)},
            "integer",
        ),
    ]
    for (tr, va), overrides, message in cases:
        with pytest.raises(TrainingError, match=message):
            run(tr, va, **overrides)


def test_fold_data_validates_its_arrays() -> None:
    good = hand_fold([("2024-01-01T00:00", "2024-01-01T01:00"), ("2024-01-01T02:00", "NaT")])
    for change, message in (
        ({"X": np.array([[np.nan, 1.0], [1.0, 2.0]])}, "finite"),
        ({"y": np.array([0, 2])}, "0/1"),
        ({"decision_time": good.decision_time[::-1]}, "sorted"),
        ({"exit_time": np.asarray([ms("2023-12-31T00:00"), ms("NaT")])}, "precede"),
        ({"decision_time": np.array([1, 2])}, "datetime64"),
        ({"net_return": np.array([0.0])}, "net_return"),
        ({"feature_names": ("a", "a")}, "unique"),
        ({"X": np.zeros((2, 3))}, "n x 2"),
    ):
        with pytest.raises(TrainingError, match=message):
            dataclasses.replace(good, **change)
    assert not good.X.flags.writeable
    assert good.select(np.array([True, False])).X.tolist() == [[0.0, 1.0]]


def test_fold_data_from_a_label_set() -> None:
    mixed = make_labels(9, families=("CANDIDATE_A", "CANDIDATE_B"))
    with pytest.raises(TrainingError, match="pass family="):
        fold_data_from_labelset(mixed)
    b_only = fold_data_from_labelset(mixed, family="CANDIDATE_B")
    expected = [r for r in mixed.rows if r.family == "CANDIDATE_B"]
    assert len(b_only) == len(expected) and b_only.family == "CANDIDATE_B"
    assert b_only.net_return.tolist() == [r.net_return for r in expected]
    unfilled = [i for i, r in enumerate(expected) if r.exit_time is None]
    assert unfilled and bool(np.isnat(b_only.exit_time[unfilled]).all())
    assert dict(b_only.data_hashes) == dict(mixed.source)
    empty = fold_data_from_labelset(mixed.between(TEST_END, TEST_END), family=FAMILY)
    assert len(empty) == 0 and empty.X.shape == (0, len(NAMES))

    class Flipped(Labels):
        def matrix(self) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
            x, y = super().matrix()
            return x, 1 - y

    with pytest.raises(TrainingError, match="win flags"):
        fold_data_from_labelset(Flipped(rows=make_labels(3).rows[:20]))


# ----------------------------------------------------------------------------- calibration


def test_platt_fit_satisfies_its_optimality_conditions() -> None:
    rng = np.random.default_rng(17)
    z = rng.normal(size=20_000) * 2.0
    y = (rng.random(20_000) < sigmoid(1.5 * z - 0.5)).astype(np.int64)
    a, b = fit_platt(z, y)
    assert abs(a - 1.5) < 0.1 and abs(b + 0.5) < 0.1
    n_pos, n_neg = int(y.sum()), int(len(y) - y.sum())
    target = np.where(y == 1, (n_pos + 1) / (n_pos + 2), 1 / (n_neg + 2))
    residual = sigmoid(a * z + b) - target
    # the gradient of the smoothed cross-entropy vanishes (relative to the sample size)
    assert abs(residual.sum()) / len(z) < 1e-9 and abs(np.dot(residual, z)) / len(z) < 1e-9
    # one-sided or tiny validation windows still give finite coefficients
    for labels_ in (np.ones(30, dtype=np.int64), np.zeros(30, dtype=np.int64)):
        a1, b1 = fit_platt(z[:30], labels_)
        assert np.isfinite([a1, b1]).all()
    assert fit_platt(z[:500], y[:500]) == fit_platt(z[:500], y[:500])


def test_exact_threshold_comparison() -> None:
    for text in ("0.1", "0.55", "0.3", "1", "0"):
        t = Decimal(text)
        f = float_at_least(t)
        assert Decimal(f) >= t
        assert Decimal(np.nextafter(f, -np.inf)) < t or f == 0.0
