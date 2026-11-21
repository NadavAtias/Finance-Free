"""Indicators computed only from the history slice they are given."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, n: int) -> float:
    return float(close.iloc[-n:].mean())


def total_return(close: pd.Series, n: int) -> float:
    return float(close.iloc[-1] / close.iloc[-n - 1] - 1)


def realized_vol(close: pd.Series, n: int) -> float:
    rets = np.log(close.iloc[-n - 1 :]).diff().dropna()
    return float(rets.std(ddof=1) * np.sqrt(252))


def atr(bars: pd.DataFrame, n: int) -> float:
    h, lo, c = bars["high"], bars["low"], bars["close"]
    prev = c.shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-n:].mean())
