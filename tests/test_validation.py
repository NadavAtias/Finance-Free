from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ff.backtest.engine import BacktestEngine
from ff.backtest.registry import TrialRegistry
from ff.backtest.validation import blend_equity, run_validation, walk_forward
from ff.core.audit import AuditLog
from ff.core.config import load_config
from ff.data.view import MarketData
from ff.markets.us import USMarket
from ff.risk.engine import RiskEngine
from ff.strategies.base import build_strategy
from tests.conftest import make_bars

ROOT = Path(__file__).resolve().parents[1]


def test_walk_forward_picks_best_past_trial_and_uses_only_next_year() -> None:
    idx = pd.bdate_range("2010-01-01", "2016-12-31")
    rng = np.random.default_rng(0)
    a = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
    b = a.copy()
    early = idx < pd.Timestamp("2015-01-01")
    b[early] += 0.002  # b is better during training only
    total = pd.DataFrame({"a": a, "b": b})
    oos, choices = walk_forward(total, total, train_years=5, test_years=1)
    assert choices[0]["chosen"] == "b"
    assert oos.index.min() >= pd.Timestamp("2015-01-01")
    first_year = oos[oos.index.year == 2015]
    pd.testing.assert_series_equal(first_year, b[b.index.year == 2015], check_names=False)


def test_blend_of_identical_curves_is_the_curve() -> None:
    idx = pd.bdate_range("2020-01-01", periods=300)
    eq = pd.Series(100 * np.exp(np.cumsum(np.full(300, 0.0005))), index=idx)
    pd.testing.assert_series_equal(blend_equity(eq, eq, 0.5), eq, check_names=False)


def test_blend_rebalances_monthly() -> None:
    idx = pd.bdate_range("2020-01-01", periods=120)
    up = pd.Series(np.linspace(100, 200, 120), index=idx)
    flat = pd.Series(100.0, index=idx)
    mix = blend_equity(up, flat, 0.5)
    # monthly rebalancing keeps adding to the winner's loser leg, so it lands between
    assert flat.iloc[-1] < mix.iloc[-1] < up.iloc[-1]


def test_registry_counts_distinct_trials(tmp_path: Path) -> None:
    reg = TrialRegistry(tmp_path / "r.jsonl")
    reg.record_trial("k", {"a": 1}, sharpe_daily=0.01)
    reg.record_trial("k", {"a": 1}, sharpe_daily=0.02)  # same params again
    reg.record_trial("k", {"a": 2}, sharpe_daily=0.03)
    reg.record_trial("other", {"a": 1}, sharpe_daily=0.0)
    assert reg.n_trials("k") == 2
    assert sorted(reg.trial_sharpes("k")) == [0.02, 0.03]


def test_cash_earns_interest_without_lookahead(zero_cost_market: USMarket, tmp_path: Path) -> None:
    from ff.core.config import RiskConfig
    from ff.core.types import Intent

    class Flat:
        id = "flat"
        is_benchmark = True
        warmup = 0

        def on_bar(self, ctx: object) -> list[Intent] | None:
            return None

    bars = make_bars([100.0] * 260)
    rate = pd.Series(0.0, index=bars.index)
    rate.iloc[100:] = 0.05  # rate rises on session 100
    audit = AuditLog()
    eng = BacktestEngine(
        Flat(),  # type: ignore[arg-type]
        MarketData({"X": bars}),
        zero_cost_market,
        RiskEngine(RiskConfig(kill_switch_file=str(tmp_path / "K")), audit),
        100_000,
        audit,
        reference_symbol="X",
        cash_rate=rate,
    )
    res = eng.run(bars.index[0], bars.index[-1])
    assert res.equity.iloc[100] == pytest.approx(100_000)  # 5% only known after close 100
    assert res.equity.iloc[101] > 100_000
    days = (bars.index[-1] - bars.index[100]).days
    assert res.equity.iloc[-1] == pytest.approx(100_000 * (1 + 0.05 * days / 365), rel=2e-3)
    assert res.interest_earned > 0


def test_validation_end_to_end_on_synthetic(tmp_path: Path) -> None:
    cfg = load_config(ROOT / "config" / "validate_trend.yaml")
    cfg.provider = "synthetic"
    cfg.start, cfg.end = "2012-01-01", "2020-12-31"
    cfg.holdout_start = "2019-01-01"
    cfg.data_dir = str(tmp_path / "data")
    cfg.runs_dir = str(tmp_path / "runs")
    cfg.validation["grid"] = {"sma_days": [150, 200], "target_vol": [0.08, 0.10]}
    cfg.validation["train_years"] = 3
    cfg.validation["pbo_blocks"] = 6
    out, res = run_validation(cfg)
    assert (out / "validation.html").exists()
    assert res.base.equity.index.max() < pd.Timestamp("2019-01-01")  # hold-out locked
    assert res.n_trials == 4
    assert 0 <= res.dsr <= 1
    assert res.pbo is not None and 0 <= res.pbo.pbo <= 1
    assert {c.name for c in res.checks} >= {"Deflated Sharpe", "Overfitting (PBO)"}

    _, opened = run_validation(cfg, open_holdout=True)
    assert opened.base.equity.index.max() > pd.Timestamp("2019-01-01")
    assert "OPENED" in opened.holdout
    reg = TrialRegistry(Path(cfg.runs_dir) / "trial_registry.jsonl")
    assert len(reg.holdout_openings("trend_etf")) == 1


def test_build_strategy_unknown_kind() -> None:
    with pytest.raises(KeyError):
        build_strategy("nope", "x", ["SPY"], {})
