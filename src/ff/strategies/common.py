"""Helpers shared by strategies. Every function reads only the point-in-time view."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ff.strategies.base import Context


def vol_scale(ctx: Context, raw: dict[str, float], vol_days: int, target_vol: float) -> float:
    """Multiplier that brings a raw weight vector to ``target_vol`` annualized volatility,
    estimated from the trailing ``vol_days`` covariance of log returns."""
    if not raw:
        return 0.0
    syms = list(raw)
    px = pd.DataFrame({s: ctx.data.history(s, vol_days + 1)["close"] for s in syms}).dropna()
    if len(px) < 3:
        return 0.0
    log_rets = np.diff(np.log(px.to_numpy()), axis=0)
    cov = np.atleast_2d(np.cov(log_rets, rowvar=False)) * 252
    w = np.array([raw[s] for s in syms])
    port_vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    return target_vol / port_vol if port_vol > 0 else 0.0
