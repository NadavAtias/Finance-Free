from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ff.analytics.overfitting import (
    deflated_sharpe,
    expected_max_sharpe,
    pbo_cscv,
    probabilistic_sharpe,
    sharpe_stats,
)


def test_psr_is_half_when_sharpe_equals_benchmark() -> None:
    rng = np.random.default_rng(0)
    st = sharpe_stats(rng.normal(0.001, 0.01, 2000))
    assert probabilistic_sharpe(st, st.sharpe) == pytest.approx(0.5)


def test_psr_grows_with_sample_length() -> None:
    rng = np.random.default_rng(1)
    short = sharpe_stats(rng.normal(0.0005, 0.01, 250))
    long = sharpe_stats(rng.normal(0.0005, 0.01, 5000))
    assert probabilistic_sharpe(long) > probabilistic_sharpe(short)


def test_expected_max_sharpe_increases_with_trials() -> None:
    srs = list(np.random.default_rng(2).normal(0, 0.02, 50))
    assert expected_max_sharpe(srs, 10) < expected_max_sharpe(srs, 1000)
    assert expected_max_sharpe([0.03], 1) == 0.0


def test_deflation_penalizes_many_trials() -> None:
    rng = np.random.default_rng(3)
    st = sharpe_stats(rng.normal(0.0006, 0.01, 3000))
    trials = list(rng.normal(0.0, 0.015, 30))
    dsr_few, _ = deflated_sharpe(st, trials, n_trials=2)
    dsr_many, sr0 = deflated_sharpe(st, trials, n_trials=500)
    assert dsr_many < dsr_few
    assert sr0 > 0


def test_pbo_high_for_pure_noise_low_for_real_skill() -> None:
    # One noise sample can look skilled by chance, so average over many samples:
    # with no real differences, the in-sample winner is a coin flip out of sample.
    pbos = [
        pbo_cscv(pd.DataFrame(np.random.default_rng(s).normal(0, 0.01, (1000, 8))), 8).pbo
        for s in range(30)
    ]
    assert 0.35 < float(np.mean(pbos)) < 0.65

    noise = pd.DataFrame(np.random.default_rng(4).normal(0, 0.01, (2000, 12)))
    skill = noise.copy()
    skill[0] += 0.002  # one trial is genuinely better everywhere
    assert pbo_cscv(skill, n_blocks=10).pbo < 0.05
