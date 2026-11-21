"""Statistics that discount a backtest for luck and for the number of things tried.

- Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR):
  Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting for
  Selection Bias, Backtest Overfitting and Non-Normality", J. Portfolio Mgmt.
- Probability of Backtest Overfitting (PBO) via combinatorially symmetric
  cross-validation (CSCV): Bailey, Borwein, López de Prado & Zhu (2017),
  "The Probability of Backtest Overfitting", J. Computational Finance.

All Sharpe ratios here are per period (daily), not annualized, as the formulas
require. Only the standard library's NormalDist is used (no SciPy dependency).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class SharpeStats:
    sharpe: float  # per period
    n: int
    skew: float
    kurtosis: float  # non-excess (normal = 3)

    @property
    def annualized(self) -> float:
        return self.sharpe * math.sqrt(252)


def sharpe_stats(returns: pd.Series | np.ndarray) -> SharpeStats:
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    sd = r.std(ddof=1)
    sr = float(r.mean() / sd) if sd > 0 else 0.0
    z = (r - r.mean()) / sd if sd > 0 else np.zeros_like(r)
    return SharpeStats(sr, len(r), float((z**3).mean()), float((z**4).mean()))


def probabilistic_sharpe(stats: SharpeStats, benchmark_sr: float = 0.0) -> float:
    """Probability that the true Sharpe exceeds ``benchmark_sr`` given this sample."""
    sr = stats.sharpe
    denom = 1 - stats.skew * sr + (stats.kurtosis - 1) / 4 * sr**2
    if stats.n < 2 or denom <= 0:
        return float("nan")
    return _N.cdf((sr - benchmark_sr) * math.sqrt(stats.n - 1) / math.sqrt(denom))


def expected_max_sharpe(trial_sharpes: list[float], n_trials: int | None = None) -> float:
    """Sharpe the best of N unskilled trials would show by luck (per period)."""
    n = n_trials or len(trial_sharpes)
    if n < 2 or len(trial_sharpes) < 2:
        return 0.0
    sd = float(np.std(trial_sharpes, ddof=1))
    return sd * (
        (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n * math.e))
    )


def deflated_sharpe(
    stats: SharpeStats, trial_sharpes: list[float], n_trials: int | None = None
) -> tuple[float, float]:
    """Return (DSR, SR0): the probability the Sharpe beats the luck threshold SR0."""
    sr0 = expected_max_sharpe(trial_sharpes, n_trials)
    return probabilistic_sharpe(stats, sr0), sr0


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    logits: np.ndarray
    n_splits: int
    n_strategies: int


def pbo_cscv(returns: pd.DataFrame, n_blocks: int = 16) -> PBOResult:
    """PBO of picking the best in-sample column of ``returns`` (T x N trials).

    Rows are split into ``n_blocks`` contiguous blocks; every half of the blocks
    is used once as in-sample and its complement as out-of-sample. PBO is the
    share of splits where the in-sample winner ranks in the bottom half
    out-of-sample.
    """
    m = returns.dropna().to_numpy(dtype=float)
    t, n = m.shape
    if n < 2:
        raise ValueError("PBO needs at least two trials")
    if n_blocks % 2 or n_blocks < 2:
        raise ValueError("n_blocks must be an even number >= 2")
    edges = np.linspace(0, t, n_blocks + 1).astype(int)
    blocks = [m[edges[i] : edges[i + 1]] for i in range(n_blocks)]
    sums = np.array([b.sum(axis=0) for b in blocks])
    sumsq = np.array([(b**2).sum(axis=0) for b in blocks])
    counts = np.array([len(b) for b in blocks], dtype=float)

    def sharpe(mask: np.ndarray) -> np.ndarray:
        c = counts[mask].sum()
        s, q = sums[mask].sum(axis=0), sumsq[mask].sum(axis=0)
        mean = s / c
        var = (q - c * mean**2) / (c - 1)
        return np.where(var > 0, mean / np.sqrt(np.maximum(var, 1e-30)), 0.0)

    logits = []
    for combo in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_mask = np.zeros(n_blocks, dtype=bool)
        is_mask[list(combo)] = True
        best = int(np.argmax(sharpe(is_mask)))
        oos = sharpe(~is_mask)
        rank = float((oos < oos[best]).sum() + 1 + 0.5 * ((oos == oos[best]).sum() - 1))
        omega = rank / (n + 1)
        logits.append(math.log(omega / (1 - omega)))
    arr = np.array(logits)
    return PBOResult(float((arr <= 0).mean()), arr, len(arr), n)
