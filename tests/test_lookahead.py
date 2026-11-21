"""Look-ahead guard: decisions must not depend on data after the decision date."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ff.backtest.engine import BacktestEngine
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig
from ff.data.view import MarketData
from ff.markets.us import USMarket
from ff.risk.engine import RiskEngine
from ff.strategies.base import build_strategy
from tests.conftest import END, START


def _run(
    bars: dict[str, pd.DataFrame], market: USMarket, risk_cfg: RiskConfig
) -> tuple[pd.Series, list[dict[str, object]]]:
    audit = AuditLog()
    strat = build_strategy("trend_etf", "t", list(bars), {})
    eng = BacktestEngine(
        strat, MarketData(bars), market, RiskEngine(risk_cfg, audit), 100_000, audit
    )
    res = eng.run(START + pd.Timedelta(days=500), END)
    return res.equity, audit.of_kind("decision")


def test_view_never_returns_future_rows(synthetic_bars: dict[str, pd.DataFrame]) -> None:
    data = MarketData(synthetic_bars)
    asof = synthetic_bars["SPY"].index[800]
    view = data.view(asof)
    for s in data.symbols:
        assert view.history(s).index.max() <= asof
        assert view.history(s, 50).index.max() <= asof


def test_corrupting_future_does_not_change_past_decisions(
    synthetic_bars: dict[str, pd.DataFrame], market: USMarket, risk_cfg: RiskConfig
) -> None:
    eq_clean, dec_clean = _run(synthetic_bars, market, risk_cfg)
    cutoff = synthetic_bars["SPY"].index[1100]
    rng = np.random.default_rng(0)
    corrupted = {}
    for s, df in synthetic_bars.items():
        df = df.copy()
        future = df.index > cutoff
        noise = rng.uniform(0.3, 3.0, future.sum())
        for col in ("open", "high", "low", "close"):
            df.loc[future, col] = df.loc[future, col] * noise
        corrupted[s] = df
    eq_bad, dec_bad = _run(corrupted, market, risk_cfg)

    before = [d for d in dec_clean if d["date"] <= cutoff]
    assert before, "test needs decisions before the cutoff"
    assert before == [d for d in dec_bad if d["date"] <= cutoff]
    pd.testing.assert_series_equal(eq_clean[:cutoff], eq_bad[:cutoff])
    assert not eq_clean.equals(eq_bad)  # sanity: corruption did matter afterwards
