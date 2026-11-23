"""Price momentum: 12-month return skipping the last month, ranked across the universe.

Evidence: Jegadeesh & Titman (1993); Asness, Moskowitz & Pedersen (2013) find it
in every major asset class. Caveats: it crashes after sharp market rebounds
(Daniel & Moskowitz 2016, e.g. 2009), and in a universe of today's large caps
its backtest is flattered by survivorship. It is here mainly as the baseline
every other source has to add to.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ff.signals.base import COLUMNS, BuildContext, SignalSource, SourceInfo


class PriceMomentum(SignalSource):
    offline = True
    info = SourceInfo(
        id="price_momentum",
        name="Price momentum",
        role="pick",
        horizon=21,
        ttl=25,
        note="12-month return, skipping the last month, ranked against the other stocks",
        evidence="Jegadeesh & Titman 1993; Asness, Moskowitz & Pedersen 2013",
    )

    def build(self, ctx: BuildContext) -> pd.DataFrame:
        s = ctx.sessions
        syms = [x for x in ctx.symbols if x in ctx.data.symbols]
        closes = pd.DataFrame({x: ctx.data._bars[x]["close"].reindex(s) for x in syms})
        mom = closes.shift(21) / closes.shift(252) - 1
        month_end = pd.Series(s.month, index=s) != pd.Series(s.month, index=s).shift(-1)
        month_end.iloc[-1] = False  # the current month is not over yet
        rows = []
        for d in s[month_end.to_numpy() & (s >= ctx.start)]:
            m = mom.loc[d].dropna()
            if len(m) < 5:
                continue
            rk = m.rank(method="average")
            val = (rk - 1) / (len(m) - 1) * 2 - 1
            for sym, ret, r, v in zip(
                m.index, m.to_numpy(), rk.to_numpy(), val.to_numpy(), strict=True
            ):
                rows.append(
                    (
                        self.info.id,
                        sym,
                        d,
                        d,
                        float(v),
                        f"12-1 month return {ret:+.0%}, rank {int(r)} of {len(m)}",
                    )
                )
        self.status = {"ok": True, "updated": f"{s[-1].date()}", "problem": ""}
        df = pd.DataFrame(rows, columns=COLUMNS)
        df["value"] = df["value"].astype(float).clip(-1, 1)
        return df if len(df) else df.astype({"value": np.float64})
