"""Deterministic synthetic bars for tests and offline demos. Never use for research."""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from ff.data.providers.base import ProviderCaps, normalize


class SyntheticProvider:
    caps = ProviderCaps(
        "synthetic",
        needs_api_key=False,
        adjusted=True,
        includes_delisted=False,
        notes="Geometric Brownian motion with regime shifts; for tests only.",
    )

    def __init__(self, seed: int = 7, annual_drift: float = 0.06, annual_vol: float = 0.18) -> None:
        self.seed = seed
        self.mu = annual_drift
        self.sigma = annual_vol

    def daily_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        dates = pd.bdate_range(start, end)
        rng = np.random.default_rng(self.seed + zlib.crc32(symbol.encode()))
        n = len(dates)
        # Slow regime switch so trend strategies have something to find.
        regime = np.sign(np.sin(np.arange(n) / rng.uniform(150, 300) + rng.uniform(0, 6)))
        drift = (self.mu + 0.10 * regime) / 252
        vol = self.sigma / np.sqrt(252)
        rets = drift + vol * rng.standard_normal(n)
        close = 100 * np.exp(np.cumsum(rets))
        gap = vol * 0.3 * rng.standard_normal(n)
        open_ = np.r_[close[0], close[:-1]] * np.exp(gap)
        hi = np.maximum(open_, close) * np.exp(np.abs(vol * 0.5 * rng.standard_normal(n)))
        lo = np.minimum(open_, close) * np.exp(-np.abs(vol * 0.5 * rng.standard_normal(n)))
        volume = rng.integers(2_000_000, 8_000_000, n).astype(float)
        df = pd.DataFrame(
            {"open": open_, "high": hi, "low": lo, "close": close, "volume": volume}, index=dates
        )
        return normalize(df)
