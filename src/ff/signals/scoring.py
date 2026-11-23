"""Measure each signal source's predictive value, point in time.

For every record we compute what happened next, as if we had acted on it:
buy at the open of the session after ``known_date``, sell at the close ``h``
sessions after ``known_date``. Stock-picking sources are measured against the
equal-weight average of the same universe over the same window (so the drift
that every survivor in a fixed universe has is netted out); market-wide sources
against SPY itself.

An outcome only counts once it is fully known: ``resolved_idx = known_idx + h``.
``scores_asof(i)`` uses outcomes with ``resolved_idx <= i`` and nothing else, so
a backtest can let the model learn its source weights as it goes without
peeking.

Statistics per source and horizon
- monthly series ``m_t``: mean of ``value x abnormal return`` over the records
  known in month ``t`` (a long/short "factor return" of the source)
- ``ir`` = mean / std of ``m_t``; ``t`` = ``ir * sqrt(months)``, divided by
  ``sqrt(h / 21)`` because overlapping holding windows inflate it
- ``hit``: share of records where the sign of the value matched the outcome
- ``ic``: Spearman rank correlation of value with outcome (when values vary)
- market sources use the time-series rank IC of month-end value vs next return

Weights ("how much we listen"): ``max(0, ir) * n / (n + 24)``, halved while
``1 <= t < 2`` (probation), zero when ``t < 1`` or under 12 months of history.
A t of 1 is a low bar on purpose: the sources are known effects from the
literature, not new discoveries, and probation halves the weight.
With no evidence a source gets nothing; this is how the system learns which
sources to trust without hand-tuning.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ff.signals.base import MARKET, SourceInfo

if TYPE_CHECKING:
    from ff.data.view import MarketData

HORIZONS = (5, 21, 63)
SHRINK_MONTHS = 24
MIN_MONTHS = 12


@dataclass
class SourceScore:
    source: str
    horizon: int
    n_obs: int = 0
    n_months: int = 0
    mean: float | None = None  # monthly mean of value x abnormal return
    ir: float | None = None
    t: float | None = None
    hit: float | None = None
    hit_90d: float | None = None
    ic: float | None = None
    status: str = "insufficient"
    weight: float = 0.0
    by_horizon: dict[int, dict[str, float | None]] = field(default_factory=dict)
    history: list[float] = field(default_factory=list)  # monthly hit rate, last 12 months

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def spearman(x: pd.Series, y: pd.Series) -> float:
    """Rank correlation without scipy."""
    return float(x.rank().corr(y.rank()))


def _shift_up(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    if k < len(a):
        out[: len(a) - k] = a[k:]
    return out


class SourceScorer:
    def __init__(
        self,
        records: pd.DataFrame,
        data: MarketData,
        sessions: pd.DatetimeIndex,
        universe: list[str],
        sources: dict[str, SourceInfo],
        reference: str = "SPY",
    ) -> None:
        self.sources = sources
        self.sessions = sessions
        syms = [s for s in universe if s in data.symbols]
        n = len(sessions)

        def mat(col: str, symbols: list[str]) -> np.ndarray:
            return (
                np.column_stack(
                    [data._bars[s][col].reindex(sessions).to_numpy(dtype=float) for s in symbols]
                )
                if symbols
                else np.empty((n, 0))
            )

        o, c = mat("open", syms), mat("close", syms)
        ref_o, ref_c = mat("open", [reference]), mat("close", [reference])
        col = {s: i for i, s in enumerate(syms)}
        obs = records.copy()
        obs["known_idx"] = np.searchsorted(sessions.values, obs["known_date"].values, side="left")
        obs = obs[obs["known_idx"] < n]
        k = obs["known_idx"].to_numpy()
        is_mkt = (obs["symbol"] == MARKET).to_numpy()
        ci = np.array([col.get(s, -1) for s in obs["symbol"]], dtype=int)
        for h in HORIZONS:
            entry = _shift_up(o, 1)  # open of the next session
            exit_ = _shift_up(c, h)  # close h sessions later
            with np.errstate(invalid="ignore", divide="ignore"):
                fwd = exit_ / entry - 1
                ok = np.isfinite(fwd)
                cnt = ok.sum(axis=1)
                tot = np.where(ok, fwd, 0.0).sum(axis=1)
                bench = np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)
                ref = (_shift_up(ref_c, h) / _shift_up(ref_o, 1) - 1)[:, 0]
            out = np.full(len(obs), np.nan)
            stock = (~is_mkt) & (ci >= 0)
            out[stock] = fwd[k[stock], ci[stock]] - bench[k[stock]]
            out[is_mkt] = ref[k[is_mkt]]
            obs[f"fwd_{h}"] = out
            obs[f"res_{h}"] = k + h
        obs["month"] = obs["known_date"].dt.to_period("M")
        self.obs = obs.reset_index(drop=True)
        self._cache: dict[int, dict[str, SourceScore]] = {}

    # ------------------------------------------------------------------------------
    def _month_key(self, idx: int) -> int:
        """Weights used during a month are those known at the previous month's last
        session, so results do not depend on when in the month they are asked for."""
        if idx < 0:
            return -1
        d = self.sessions[idx]
        first = int(np.searchsorted(self.sessions.values, np.datetime64(d.replace(day=1)), "left"))
        return first - 1

    def scores_asof(self, idx: int, exact: bool = False) -> dict[str, SourceScore]:
        key = idx if exact else self._month_key(idx)
        if key not in self._cache:
            self._cache[key] = {s: self._score(s, key) for s in self.sources}
        return self._cache[key]

    def _stats(self, df: pd.DataFrame, h: int, market: bool) -> dict[str, float | None]:
        y = df[f"fwd_{h}"]
        x = df["value"]
        out: dict[str, float | None] = {
            "n_obs": float(len(df)),
            "mean": None,
            "ir": None,
            "t": None,
            "hit": None,
            "ic": None,
            "n_months": 0.0,
        }
        if df.empty:
            return out
        nz = x != 0
        out["hit"] = float(((np.sign(x[nz]) * y[nz]) > 0).mean()) if nz.any() else None
        overlap = np.sqrt(max(1.0, h / 21))
        if market:
            m = df.groupby("month").last()
            out["n_months"] = float(len(m))
            if len(m) >= 3 and m["value"].nunique() > 1:
                ic = spearman(m["value"], m[f"fwd_{h}"])
                out["ic"] = ic
                out["ir"] = ic
                if abs(ic) < 1:
                    out["t"] = ic * np.sqrt(len(m) - 2) / np.sqrt(1 - ic**2) / overlap
            return out
        per = (x * y).groupby(df["month"]).mean()
        out["n_months"] = float(len(per))
        out["mean"] = float(per.mean())
        if len(per) >= 3 and per.std(ddof=1) > 0:
            ir = float(per.mean() / per.std(ddof=1))
            out["ir"] = ir
            out["t"] = ir * np.sqrt(len(per)) / overlap
        if x.nunique() > 2:
            out["ic"] = spearman(x, y)
        return out

    def _score(self, source: str, idx: int) -> SourceScore:
        info = self.sources[source]
        h = info.horizon
        market = info.role == "exposure"
        sc = SourceScore(source, h)
        base = self.obs[self.obs["source"] == source]
        for hh in HORIZONS:
            done = base[(base[f"res_{hh}"] <= idx) & base[f"fwd_{hh}"].notna()]
            st = self._stats(done, hh, market)
            sc.by_horizon[hh] = {"t": st["t"], "mean": st["mean"], "ic": st["ic"], "hit": st["hit"]}
            if hh != h:
                continue
            sc.n_obs = int(st["n_obs"] or 0)
            sc.n_months = int(st["n_months"] or 0)
            sc.mean, sc.ir, sc.t, sc.hit, sc.ic = st["mean"], st["ir"], st["t"], st["hit"], st["ic"]
            if idx >= 0 and not done.empty:
                cut = self.sessions[idx] - pd.Timedelta(days=90)
                recent = done[done["known_date"] >= cut - pd.Timedelta(days=int(h * 1.5))]
                nz = recent[recent["value"] != 0]
                if len(nz):
                    sc.hit_90d = float(((np.sign(nz["value"]) * nz[f"fwd_{h}"]) > 0).mean())
                hits = done[done["value"] != 0].assign(
                    ok=lambda d: (np.sign(d["value"]) * d[f"fwd_{h}"]) > 0
                )
                sc.history = [float(v) for v in hits.groupby("month")["ok"].mean().iloc[-12:]]
        if sc.n_months < MIN_MONTHS or sc.t is None or sc.ir is None:
            sc.status, sc.weight = "insufficient", 0.0
        elif sc.t < 1.0:
            sc.status, sc.weight = "off", 0.0
        else:
            shrink = sc.n_months / (sc.n_months + SHRINK_MONTHS)
            sc.status = "active" if sc.t >= 2 else "probation"  # 1 <= t < 2
            sc.weight = max(0.0, sc.ir) * shrink * (1.0 if sc.t >= 2 else 0.5)
        if info.role == "warning":
            sc.weight = 0.0
        return sc
