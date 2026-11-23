"""Macro regime from FRED: VIX, the yield curve and credit spreads.

Each daily value is an average of three parts in [-1, 1]:
- VIX vs its 1-year median (high fear leans risk-off)
- 10-year minus 3-month Treasury yield (inverted curve leans risk-off;
  Estrella & Mishkin 1998 show it predicts recessions)
- change in Moody's Baa minus 10-year Treasury spread over 3 months (widening
  credit spreads lean risk-off; Gilchrist & Zakrajsek 2012)

It only scales how much of the System Conviction book is invested; it never
picks stocks. Honest prior: macro variables have predicted the stock market
poorly out of sample (Welch & Goyal 2008), so its weight starts at zero and
grows only if its own scored record earns it.

Look-ahead: FRED posts day D's value on the next business day, so a value
dated D is first used at the close of the session after D.
"""

from __future__ import annotations

import io
import logging

import numpy as np
import pandas as pd

from ff.signals.base import COLUMNS, MARKET, BuildContext, SignalSource, SourceInfo, next_sessions
from ff.signals.http import FetchError, Http

log = logging.getLogger(__name__)
SERIES = {"VIXCLS": "VIX", "T10Y3M": "10y-3m curve", "BAA10Y": "Baa credit spread"}


def parse_fredgraph(text: str) -> pd.Series:
    """Parse fredgraph.csv (``observation_date`` or ``DATE`` header; ``.`` = missing)."""
    df = pd.read_csv(io.StringIO(text))
    date_col = df.columns[0]
    val_col = df.columns[1]
    out = pd.to_numeric(df[val_col], errors="coerce")
    out.index = pd.to_datetime(df[date_col])
    return out.dropna().sort_index()


class MacroRegime(SignalSource):
    info = SourceInfo(
        id="macro_regime",
        name="Economy",
        role="exposure",
        horizon=21,
        ttl=10,
        note="Fear index (VIX), yield curve and credit spreads",
        evidence="Estrella & Mishkin 1998; Gilchrist & Zakrajsek 2012; "
        "Welch & Goyal 2008 (skeptic)",
    )

    def fetch(self, ctx: BuildContext) -> dict[str, pd.Series]:
        http = Http(min_interval=0.5)
        out: dict[str, pd.Series] = {}
        problems = []
        for sid in SERIES:
            path = ctx.cache_dir / "fred" / f"{sid}.csv"
            try:
                body = http.cached(
                    "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    path,
                    max_age_hours=12,
                    refresh=ctx.refresh,
                    params={"id": sid},
                )
                out[sid] = parse_fredgraph(body.decode())
            except (FetchError, ValueError, IndexError) as exc:
                problems.append(f"{sid}: {exc}")
                log.warning("FRED %s unavailable: %s", sid, exc)
        self.status = {
            "ok": not problems,
            "updated": max((f"{s.index[-1].date()}" for s in out.values()), default=None),
            "problem": "; ".join(problems),
        }
        return out

    def build(self, ctx: BuildContext) -> pd.DataFrame:
        return self.from_series(self.fetch(ctx), ctx)

    def from_series(self, raw: dict[str, pd.Series], ctx: BuildContext) -> pd.DataFrame:
        if len(raw) < len(SERIES):
            return pd.DataFrame(columns=COLUMNS)
        days = pd.bdate_range(min(s.index[0] for s in raw.values()), ctx.end)
        f = pd.DataFrame({k: v.reindex(days).ffill(limit=5) for k, v in raw.items()}).dropna()
        vix_med = f["VIXCLS"].rolling(252, min_periods=126).median()
        p_vix = -((f["VIXCLS"] / vix_med - 1) / 0.5).clip(-1, 1)
        p_curve = (f["T10Y3M"] / 1.0).clip(-1, 1)
        p_credit = -(f["BAA10Y"].diff(63) / 0.5).clip(-1, 1)
        parts = pd.DataFrame({"vix": p_vix, "curve": p_curve, "credit": p_credit}).dropna()
        parts = parts[parts.index >= ctx.start - pd.Timedelta(days=40)]
        value = parts.mean(axis=1)
        known = next_sessions(ctx.sessions, pd.Series(parts.index, index=parts.index))
        rows = []
        for d in parts.index:
            k = known[d]
            if pd.isna(k):
                continue
            v = f.loc[d]
            mood = "risk-on" if value[d] > 0.15 else "risk-off" if value[d] < -0.15 else "neutral"
            rows.append(
                (
                    self.info.id,
                    MARKET,
                    d,
                    k,
                    float(value[d]),
                    f"{mood}: VIX {v['VIXCLS']:.1f} (1y median {vix_med[d]:.1f}), "
                    f"10y-3m {v['T10Y3M']:+.2f}%, Baa spread {v['BAA10Y']:.2f}% "
                    f"({f['BAA10Y'].diff(63)[d]:+.2f} in 3 months)",
                )
            )
        df = pd.DataFrame(rows, columns=COLUMNS)
        # one record per known session (the latest observation known then)
        df = df.sort_values("event_date").drop_duplicates("known_date", keep="last")
        df["value"] = df["value"].astype(np.float64)
        return df.reset_index(drop=True)
