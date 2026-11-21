from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ff.core.audit import AuditLog
from ff.core.config import CostConfig, RiskConfig
from ff.data.providers.synthetic import SyntheticProvider
from ff.data.view import MarketData
from ff.markets.us import CostModel, USMarket

START = pd.Timestamp("2015-01-01")
END = pd.Timestamp("2020-12-31")


def make_bars(
    closes: list[float] | np.ndarray,
    start: str = "2020-01-01",
    volume: float = 1_000_000.0,
    spread: float = 0.01,
) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * (1 + spread),
            "low": np.minimum(opens, closes) * (1 - spread),
            "close": closes,
            "volume": volume,
        },
        index=idx,
    )


@pytest.fixture
def synthetic_bars() -> dict[str, pd.DataFrame]:
    p = SyntheticProvider(seed=3)
    return {s: p.daily_bars(s, START, END) for s in ["SPY", "TLT", "GLD", "EFA"]}


@pytest.fixture
def market() -> USMarket:
    return USMarket(
        CostModel(CostConfig(half_spread_bps=1, slippage_bps=2, max_participation=0.5)),
        settlement_days=1,
    )


@pytest.fixture
def zero_cost_market() -> USMarket:
    return USMarket(
        CostModel(CostConfig(half_spread_bps=0, slippage_bps=0, max_participation=1.0)),
        settlement_days=1,
    )


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def risk_cfg(tmp_path: object) -> RiskConfig:
    return RiskConfig(kill_switch_file=str(tmp_path) + "/KILL_SWITCH")


def data_of(bars: dict[str, pd.DataFrame]) -> MarketData:
    return MarketData(bars)
