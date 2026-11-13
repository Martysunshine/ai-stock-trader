"""Calibration diagnostics against hand-computed values (docs/ml_research_guide.md §5;
strategy_and_profitability.md §6)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from trader.research.calibration_stats import (
    EPS,
    brier,
    expected_calibration_error,
    log_loss,
    reliability_bins,
)

Y = [1, 0, 1, 1, 0]
P = [0.9, 0.2, 0.6, 0.4, 0.1]


def test_brier_score_by_hand() -> None:
    # squared errors 0.01, 0.04, 0.16, 0.36, 0.01 -> 0.58 / 5
    assert brier(Y, P) == pytest.approx(0.116, abs=1e-12)
    assert brier([1, 0], [1.0, 0.0]) == 0.0
    assert brier([1, 0], [0.0, 1.0]) == 1.0


def test_log_loss_by_hand_and_against_scikit_learn() -> None:
    # -(ln .9 + ln .8 + ln .6 + ln .4 + ln .9) / 5
    assert log_loss(Y, P) == pytest.approx(0.3721961876540016, abs=1e-12)
    assert log_loss([1, 0], [0.5, 0.5]) == pytest.approx(math.log(2), abs=1e-12)
    from sklearn.metrics import log_loss as sk_log_loss

    rng = np.random.default_rng(3)
    y = rng.integers(0, 2, 200)
    p = rng.random(200)
    assert log_loss(y, p) == pytest.approx(sk_log_loss(y, p), abs=1e-12)


def test_log_loss_clips_certain_wrong_predictions() -> None:
    assert log_loss([1], [0.0]) == pytest.approx(-math.log(EPS), abs=1e-9)
    assert log_loss([0], [0.0]) == pytest.approx(-math.log1p(-EPS), abs=1e-20)
    assert math.isfinite(log_loss([0, 1], [1.0, 0.0]))


def test_reliability_bins_by_hand() -> None:
    low, high = reliability_bins(Y, P, n_bins=2)
    # [0, 0.5): p = 0.2, 0.4, 0.1 with outcomes 0, 1, 0
    assert (low.lower, low.upper, low.count) == (0.0, 0.5, 3)
    assert low.mean_predicted == pytest.approx(0.7 / 3, abs=1e-12)
    assert low.observed_rate == pytest.approx(1 / 3, abs=1e-12)
    # [0.5, 1]: p = 0.9, 0.6 with outcomes 1, 1
    assert (high.lower, high.upper, high.count) == (0.5, 1.0, 2)
    assert high.mean_predicted == pytest.approx(0.75, abs=1e-12)
    assert high.observed_rate == 1.0


def test_reliability_bins_report_empty_bins_and_put_one_in_the_last_bin() -> None:
    bins = reliability_bins([0, 1, 1], [0.0, 1.0, 0.75], n_bins=4)
    assert [b.count for b in bins] == [1, 0, 0, 2]
    assert bins[1].mean_predicted is None and bins[1].observed_rate is None
    assert bins[1].as_dict()["mean_predicted"] == ""
    assert bins[3].mean_predicted == pytest.approx(0.875, abs=1e-12)
    assert bins[3].observed_rate == 1.0
    assert bins[0].as_dict() == {
        "lower": "0.0",
        "upper": "0.25",
        "count": "1",
        "mean_predicted": "0.0",
        "observed_rate": "0.0",
    }


def test_expected_calibration_error_by_hand() -> None:
    # 3/5 * |1/3 - 0.7/3| + 2/5 * |1 - 0.75| = 0.06 + 0.10
    assert expected_calibration_error(Y, P, n_bins=2) == pytest.approx(0.16, abs=1e-12)
    # 1/3 * 0 + 2/3 * |1 - 0.875|
    ece = expected_calibration_error([0, 1, 1], [0.0, 1.0, 0.75], n_bins=4)
    assert ece == pytest.approx(0.25 / 3, abs=1e-12)
    assert expected_calibration_error([0, 1, 0, 1], [0.5] * 4) == 0.0


@pytest.mark.parametrize(
    ("y", "p", "message"),
    [
        ([1, 2], [0.5, 0.5], "0 or 1"),
        ([1, 0], [0.5, 1.5], "within"),
        ([1, 0], [0.5, float("nan")], "finite"),
        ([1, 0], [0.5], "equally long"),
        ([], [], "at least one"),
    ],
)
def test_invalid_inputs_fail_loudly(y: list[float], p: list[float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        brier(y, p)
    with pytest.raises(ValueError, match=message):
        reliability_bins(y, p)


def test_bin_count_and_eps_are_validated() -> None:
    with pytest.raises(ValueError, match="n_bins"):
        reliability_bins(Y, P, n_bins=0)
    with pytest.raises(ValueError, match="eps"):
        log_loss(Y, P, eps=0.0)
