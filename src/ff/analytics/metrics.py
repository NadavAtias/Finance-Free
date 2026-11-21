"""Performance metrics. Risk-free rate is 0 in the MVP (cash also earns 0 in the
sim), so Sharpe here is return/vol; T-bill yield modeling comes with Phase 2."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from ff.execution.portfolio import ClosedTrade

TRADING_DAYS = 252


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def max_drawdown_duration(equity: pd.Series) -> int:
    """Longest run of sessions spent below a previous peak."""
    under = (equity < equity.cummax()).astype(int).to_numpy()
    longest = run = 0
    for u in under:
        run = run + 1 if u else 0
        longest = max(longest, run)
    return longest


def performance(
    equity: pd.Series,
    gross: pd.Series | None = None,
    trades: Sequence[ClosedTrade] = (),
    benchmark: pd.Series | None = None,
) -> dict[str, float]:
    eq = equity.dropna()
    rets = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else float("nan")
    vol = rets.std(ddof=1) * np.sqrt(TRADING_DAYS)
    downside = rets[rets < 0].std(ddof=1) * np.sqrt(TRADING_DAYS)
    mdd = drawdown(eq).min()
    m: dict[str, float] = {
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": rets.mean() / rets.std(ddof=1) * np.sqrt(TRADING_DAYS) if vol > 0 else np.nan,
        "sortino": rets.mean() * TRADING_DAYS / downside if downside > 0 else np.nan,
        "max_drawdown": mdd,
        "max_dd_duration_days": float(max_drawdown_duration(eq)),
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "best_month": float("nan"),
        "worst_month": float("nan"),
    }
    monthly = eq.resample("ME").last().pct_change().dropna()
    if len(monthly):
        m["best_month"], m["worst_month"] = monthly.max(), monthly.min()
    if gross is not None and len(gross):
        m["avg_exposure"] = float(gross.mean())
    if trades:
        pnl = np.array([t.pnl for t in trades])
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        m["closed_trades"] = float(len(pnl))
        m["win_rate"] = float(len(wins)) / float(len(pnl))
        m["profit_factor"] = float(wins.sum()) / float(-losses.sum()) if len(losses) else np.inf
        m["avg_win"] = wins.mean() if len(wins) else 0.0
        m["avg_loss"] = losses.mean() if len(losses) else 0.0
    if benchmark is not None:
        b = benchmark.reindex(eq.index).pct_change().dropna()
        r = rets.reindex(b.index).dropna()
        b = b.reindex(r.index)
        if len(r) > 2 and b.var() > 0:
            m["beta_vs_benchmark"] = float(np.cov(r, b)[0, 1] / b.var())
            m["correlation_vs_benchmark"] = float(r.corr(b))
    return {k: float(v) for k, v in m.items()}


def calendar_year_returns(equity: pd.Series) -> pd.Series:
    yearly = equity.resample("YE").last()
    first = pd.Series([equity.iloc[0]], index=[equity.index[0] - pd.Timedelta(days=1)])
    out = pd.concat([first, yearly]).pct_change().dropna()
    out.index = pd.DatetimeIndex(out.index).year
    return out
