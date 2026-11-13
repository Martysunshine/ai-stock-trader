"""Research statistics (risk_and_validation.md §7, §8).

Binary floating point is acceptable here: these are evidence summaries derived from the exact
ledger, never inputs to accounting or sizing (instructions.md §3). All resampling uses an
explicit seed, so a report is reproducible.

* Session series include untraded zero-return sessions.
* Uncertainty uses the stationary bootstrap (Politis & Romano) over *sessions*, preserving
  autocorrelation and the clustering of trades within a session; per-trade expectancy is a ratio
  of session sums resampled together.
* Selection: a bootstrap max-statistic ("reality check") across all trials of a study gives a
  selection-adjusted lower bound for the best trial, and the deflated Sharpe ratio (Bailey &
  López de Prado) discounts the best Sharpe by the number of registered trials.
* Effective sample size uses Bartlett-weighted autocorrelations; the prospective power analysis
  inflates the required sample by the same variance factor.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from numpy.typing import NDArray

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()

FloatArray = NDArray[np.float64]


def as_array(values: Sequence[float]) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


# ----------------------------------------------------------------------------- bootstrap


def stationary_indices(
    n: int, mean_block: float, replicates: int, rng: np.random.Generator
) -> NDArray[np.int64]:
    """Index matrix (replicates x n) for the stationary bootstrap with geometric blocks."""
    if n <= 0:
        raise ValueError("cannot resample an empty series")
    p = 1.0 / max(mean_block, 1.0)
    out = np.empty((replicates, n), dtype=np.int64)
    starts = rng.integers(0, n, size=(replicates, n))
    new_block = rng.random((replicates, n)) < p
    out[:, 0] = starts[:, 0]
    for t in range(1, n):
        out[:, t] = np.where(new_block[:, t], starts[:, t], (out[:, t - 1] + 1) % n)
    return out


@dataclass(frozen=True, slots=True)
class Interval:
    estimate: float
    lower: float
    upper: float
    one_sided_lower: float
    level: float

    def as_dict(self) -> dict[str, float]:
        return {
            "estimate": self.estimate,
            "lower": self.lower,
            "upper": self.upper,
            "one_sided_lower": self.one_sided_lower,
            "level": self.level,
        }


def bootstrap_mean(
    x: FloatArray, *, mean_block: float, replicates: int, seed: int, level: float = 0.95
) -> Interval:
    rng = np.random.default_rng(seed)
    idx = stationary_indices(len(x), mean_block, replicates, rng)
    means = x[idx].mean(axis=1)
    alpha = 1 - level
    return Interval(
        estimate=float(x.mean()),
        lower=float(np.quantile(means, alpha / 2)),
        upper=float(np.quantile(means, 1 - alpha / 2)),
        one_sided_lower=float(np.quantile(means, alpha)),
        level=level,
    )


def bootstrap_ratio(
    numerator: FloatArray,
    denominator: FloatArray,
    *,
    mean_block: float,
    replicates: int,
    seed: int,
    level: float = 0.95,
) -> Interval | None:
    """Per-trade expectancy: sum(session PnL) / sum(session trades), sessions resampled."""
    if denominator.sum() <= 0:
        return None
    rng = np.random.default_rng(seed)
    idx = stationary_indices(len(numerator), mean_block, replicates, rng)
    num = numerator[idx].sum(axis=1)
    den = denominator[idx].sum(axis=1)
    valid = den > 0
    ratios = num[valid] / den[valid]
    alpha = 1 - level
    return Interval(
        estimate=float(numerator.sum() / denominator.sum()),
        lower=float(np.quantile(ratios, alpha / 2)),
        upper=float(np.quantile(ratios, 1 - alpha / 2)),
        one_sided_lower=float(np.quantile(ratios, alpha)),
        level=level,
    )


def reality_check(
    matrix: FloatArray, *, mean_block: float, replicates: int, seed: int, level: float = 0.95
) -> dict[str, float | int]:
    """Selection-adjusted bound for the best of K strategies (rows) over shared sessions.

    The bootstrap distribution of ``max_k (mean*_k - mean_k)`` (sessions resampled jointly, so
    cross-strategy correlation is kept) gives the critical value ``c``; the best strategy's
    selection-aware one-sided lower bound is ``mean_best - c`` and the p-value of
    "no strategy has a positive mean" is the share of ``max_k (mean*_k - mean_k) >= mean_best``.
    """
    k, n = matrix.shape
    rng = np.random.default_rng(seed)
    idx = stationary_indices(n, mean_block, replicates, rng)
    means = matrix.mean(axis=1)
    boot = matrix[:, idx].mean(axis=2)  # k x replicates
    centred_max = (boot - means[:, None]).max(axis=0)
    best = int(np.argmax(means))
    crit = float(np.quantile(centred_max, level))
    return {
        "best_index": best,
        "best_mean": float(means[best]),
        "critical_value": crit,
        "selection_adjusted_lower_bound": float(means[best] - crit),
        "p_value_no_positive_mean": float((centred_max >= means[best]).mean()),
        "strategies": k,
        "sessions": n,
    }


# ----------------------------------------------------------------------------- Sharpe


def sharpe(x: FloatArray) -> float:
    sd = float(x.std(ddof=1)) if len(x) > 1 else 0.0
    return float(x.mean()) / sd if sd > 0 else 0.0


def skew_kurtosis(x: FloatArray) -> tuple[float, float]:
    if len(x) < 3:
        return 0.0, 3.0
    d = x - x.mean()
    m2 = float((d**2).mean())
    if m2 == 0:
        return 0.0, 3.0
    return float((d**3).mean()) / m2**1.5, float((d**4).mean()) / m2**2


def deflated_sharpe(
    x: FloatArray, *, trial_sharpes: Sequence[float], n_trials: int
) -> dict[str, float]:
    """Probability that the true per-session Sharpe exceeds the expected best-of-N by luck."""
    n = len(x)
    sr = sharpe(x)
    skew, kurt = skew_kurtosis(x)
    if n_trials > 1 and len(trial_sharpes) > 1:
        var = float(np.var(np.asarray(trial_sharpes, dtype=np.float64), ddof=1))
        sr0 = math.sqrt(max(var, 0.0)) * (
            (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
            + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e))
        )
    else:
        sr0 = 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if n < 2 or denom <= 0:
        return {"sharpe": sr, "benchmark_sharpe": sr0, "dsr": float("nan")}
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return {"sharpe": sr, "benchmark_sharpe": sr0, "dsr": _N.cdf(z)}


# ----------------------------------------------------------------------------- dependence


def effective_sample_size(x: FloatArray, max_lag: int | None = None) -> tuple[float, float]:
    """(n_eff, variance inflation factor) from Bartlett-weighted autocorrelations."""
    n = len(x)
    if n < 3 or float(x.var()) == 0:
        return float(n), 1.0
    lags = max_lag if max_lag is not None else max(1, int(4 * (n / 100) ** (2 / 9)))
    d = x - x.mean()
    var = float((d * d).mean())
    vif = 1.0
    for k in range(1, min(lags, n - 1) + 1):
        rho = float((d[k:] * d[:-k]).mean()) / var
        vif += 2 * (1 - k / (lags + 1)) * rho
    vif = max(vif, 1e-6)
    return n / vif, vif


def sessions_for_power(
    sigma: float, effect: float, *, alpha: float = 0.05, power: float = 0.8, vif: float = 1.0
) -> float:
    """Sessions needed for a one-sided test of mean > 0 to detect ``effect`` with ``power``."""
    if effect <= 0 or sigma <= 0:
        return float("inf")
    z = _N.inv_cdf(1 - alpha) + _N.inv_cdf(power)
    return (z * sigma / effect) ** 2 * vif


# ----------------------------------------------------------------------------- paths


def drawdown_stats(equity: FloatArray) -> dict[str, float]:
    if len(equity) == 0:
        return {"max_drawdown_usd": 0.0, "max_drawdown_fraction": 0.0, "longest_underwater": 0}
    peaks = np.maximum.accumulate(equity)
    dd = peaks - equity
    worst = int(np.argmax(dd))
    longest = current = 0
    for underwater in dd > 1e-12:
        current = current + 1 if underwater else 0
        longest = max(longest, current)
    return {
        "max_drawdown_usd": float(dd[worst]),
        "max_drawdown_fraction": float(dd[worst] / peaks[worst]) if peaks[worst] > 0 else 0.0,
        "longest_underwater": float(longest),
    }


def loss_trigger_probabilities(
    session_pnl: FloatArray,
    *,
    capital: float,
    horizon: int,
    daily_limit: float,
    weekly_limit: float,
    drawdown_fraction: float,
    floor: float,
    week_sessions: int,
    mean_block: float,
    replicates: int,
    seed: int,
) -> dict[str, float]:
    """Model-dependent path-bootstrap probabilities of reaching each loss trigger.

    Session sums understate intraday excursions, so the daily figure is a lower bound.
    """
    rng = np.random.default_rng(seed)
    n = len(session_pnl)
    idx = stationary_indices(n, mean_block, replicates, rng)[:, : min(horizon, n)]
    if horizon > n:
        extra = stationary_indices(n, mean_block, replicates, rng)[:, : horizon - n]
        idx = np.concatenate([idx, extra], axis=1)
    paths = session_pnl[idx]
    equity = capital + np.cumsum(paths, axis=1)
    peaks = np.maximum.accumulate(
        np.concatenate([np.full((replicates, 1), capital), equity], 1), 1
    )[:, 1:]
    weekly = np.lib.stride_tricks.sliding_window_view(paths, week_sessions, axis=1).sum(axis=2)
    return {
        "horizon_sessions": float(horizon),
        "daily_limit": float((paths <= -daily_limit).any(axis=1).mean()),
        "weekly_limit": float((weekly <= -weekly_limit).any(axis=1).mean()),
        "high_water_drawdown": float(
            ((peaks - equity) >= drawdown_fraction * peaks).any(axis=1).mean()
        ),
        "capital_floor": float((equity <= floor).any(axis=1).mean()),
    }
