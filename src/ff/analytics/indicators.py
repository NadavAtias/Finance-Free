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
    """Average true range over the last ``n`` sessions (simple mean)."""
    tail = bars.iloc[-(n + 1) :]
    h = tail["high"].to_numpy()[1:]
    lo = tail["low"].to_numpy()[1:]
    prev = tail["close"].to_numpy()[:-1]
    tr = np.maximum(np.maximum(h - lo, np.abs(h - prev)), np.abs(lo - prev))
    return float(tr.mean())
