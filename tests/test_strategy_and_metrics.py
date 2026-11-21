from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ff.analytics.metrics import drawdown, max_drawdown_duration, performance
from ff.backtest.engine import BacktestEngine
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig
from ff.data.view import MarketData
from ff.markets.us import USMarket
from ff.risk.engine import RiskEngine
from ff.strategies.base import Context, build_strategy
from tests.conftest import make_bars


def _ctx(bars: dict[str, pd.DataFrame], i: int, month_end: bool = True) -> Context:
    d = next(iter(bars.values())).index[i]
    return Context(d, MarketData(bars).view(d), 100_000, {}, month_end, False)


def _noisy(trend: float, n: int = 400, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(trend + 0.01 * rng.standard_normal(n)))


def test_trend_goes_long_uptrend_with_stop_below_price() -> None:
    bars = {"UP": make_bars(_noisy(0.002)), "DN": make_bars(_noisy(-0.002, seed=2))}
    strat = build_strategy("trend_etf", "t", ["UP", "DN"], {})
    out = {i.symbol: i for i in strat.on_bar(_ctx(bars, 399)) or []}
    px = bars["UP"]["close"].iloc[-1]
    assert out["UP"].target_weight > 0
    assert out["UP"].stop_price is not None and out["UP"].stop_price < px
    assert out["DN"].target_weight == 0
    assert "SMA" in out["UP"].rationale.invalidation


def test_trend_only_decides_at_month_end() -> None:
    bars = {"UP": make_bars(_noisy(0.002))}
    strat = build_strategy("trend_etf", "t", ["UP"], {})
    assert strat.on_bar(_ctx(bars, 399, month_end=False)) is None


def test_trend_skips_symbols_without_enough_history() -> None:
    bars = {"UP": make_bars(_noisy(0.002, n=100))}
    strat = build_strategy("trend_etf", "t", ["UP"], {})
    assert strat.on_bar(_ctx(bars, 99)) == []


def test_buy_and_hold_tracks_asset(zero_cost_market: USMarket) -> None:
    closes = _noisy(0.001, n=300)
    bars = make_bars(closes)
    audit = AuditLog()
    strat = build_strategy("buy_and_hold", "bh", ["X"], {})
    risk = RiskEngine(
        RiskConfig(max_position_weight=1.0, kill_switch_file="/none/K"), audit, passthrough=True
    )
    res = BacktestEngine(
        strat, MarketData({"X": bars}), zero_cost_market, risk, 100_000, audit, reference_symbol="X"
    ).run(bars.index[0], bars.index[-1])
    asset_ret = bars["close"].iloc[-1] / bars["open"].iloc[1] - 1
    strat_ret = res.equity.iloc[-1] / res.equity.iloc[0] - 1
    assert strat_ret == pytest.approx(asset_ret, abs=0.01)  # cash drag from whole shares


def test_metrics_on_known_series() -> None:
    idx = pd.bdate_range("2020-01-01", periods=5)
    eq = pd.Series([100, 110, 99, 120, 108], index=idx, dtype=float)
    assert drawdown(eq).min() == pytest.approx(99 / 110 - 1)
    assert max_drawdown_duration(eq) == 1
    m = performance(eq)
    assert m["total_return"] == pytest.approx(0.08)
    assert m["max_drawdown"] == pytest.approx(-0.1)


def test_metrics_cagr_for_steady_growth() -> None:
    idx = pd.date_range("2010-01-01", "2020-01-01", freq="D")
    years = (idx[-1] - idx[0]).days / 365.25
    eq = pd.Series(np.exp(np.linspace(0, np.log(2), len(idx))), index=idx)
    assert performance(eq)["cagr"] == pytest.approx(2 ** (1 / years) - 1, rel=1e-6)
