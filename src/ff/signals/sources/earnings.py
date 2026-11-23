"""Earnings reaction: the stock's 3-day move around its earnings release,
net of SPY, as the "surprise".

Earnings dates come free from SEC EDGAR: companies file an 8-K with Item 2.02
("Results of Operations") when they release results. Using the market's
reaction instead of analyst consensus needs no paid estimates data, and
Brandt, Kishore, Santa-Clara & Venkatachalam (2008) show the drift after big
earnings-announcement returns is at least as strong as after classic
earnings surprises (post-earnings announcement drift, Bernard & Thomas 1989).

Honest prior: the drift has shrunk a lot since the 2000s and is now weak in
large, liquid stocks (Chordia, Goyal, Sadka, Sadka & Shivakumar 2009;
Martineau 2022). Expect a small or zero measured edge in a large-cap universe.

Look-ahead: the window is close of the session before the filing date to the
close of the session after it, so it covers releases before the open, during
the day, and after the close. The record is known at the close of the last
day of the window and can only be traded at the next open.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ff.signals.base import COLUMNS, BuildContext, SignalSource, SourceInfo
from ff.signals.http import FetchError
from ff.signals.sources.edgar import company_filings, ticker_ciks

log = logging.getLogger(__name__)
FULL_SCALE = 0.06  # a 6% move beyond SPY maps to a full +1 / -1


def earnings_dates(filings: pd.DataFrame) -> pd.Series:
    m = (filings["form"] == "8-K") & filings["items"].str.contains(r"\b2\.02\b", regex=True)
    dates = filings.loc[m, "filingDate"].sort_values().drop_duplicates()
    keep, last = [], None
    for d in dates:
        if last is None or (d - last).days > 20:  # amendments / repeats of one release
            keep.append(d)
            last = d
    return pd.Series(keep, dtype="datetime64[ns]")


class EarningsReaction(SignalSource):
    info = SourceInfo(
        id="earnings_reaction",
        name="Earnings reaction",
        role="pick",
        horizon=63,
        ttl=63,
        note="How the stock moved vs SPY in the 3 days around its earnings report",
        evidence="Bernard & Thomas 1989; Brandt et al. 2008; Martineau 2022 (it has faded)",
    )

    def build(self, ctx: BuildContext) -> pd.DataFrame:
        events: dict[str, pd.Series] = {}
        problems: list[str] = []
        try:
            ciks = ticker_ciks(ctx.cache_dir, ctx.refresh)
        except FetchError as exc:
            self.status = {"ok": False, "updated": None, "problem": str(exc)}
            log.warning("earnings source unavailable: %s", exc)
            return pd.DataFrame(columns=COLUMNS)
        for sym in ctx.symbols:
            cik = ciks.get(sym.upper())
            if cik is None:
                problems.append(f"no CIK for {sym}")
                continue
            try:
                f = company_filings(cik, ctx.cache_dir, ctx.refresh, since=ctx.start)
            except (FetchError, ValueError) as exc:
                problems.append(f"{sym}: {exc}")
                continue
            events[sym] = earnings_dates(f)
        self.status = {
            "ok": not problems,
            "updated": f"{pd.Timestamp.now().date()}",
            "problem": "; ".join(problems[:5]) + (" …" if len(problems) > 5 else ""),
        }
        return self.from_dates(events, ctx)

    def from_dates(self, events: dict[str, pd.Series], ctx: BuildContext) -> pd.DataFrame:
        s = ctx.sessions
        spy = ctx.data._bars["SPY"]["close"].reindex(s).to_numpy(dtype=float)
        rows = []
        for sym, dates in events.items():
            if sym not in ctx.data.symbols:
                continue
            c = ctx.data._bars[sym]["close"].reindex(s).to_numpy(dtype=float)
            for d in dates:
                if d < ctx.start - pd.Timedelta(days=120):
                    continue
                i = int(np.searchsorted(s.values, np.datetime64(d), side="left"))
                if i < 1 or i + 1 >= len(s):
                    continue  # window not complete yet
                pre, post = i - 1, i + 1
                if not (np.isfinite(c[pre]) and np.isfinite(c[post])):
                    continue
                ear = (c[post] / c[pre] - 1) - (spy[post] / spy[pre] - 1)
                v = float(np.clip(ear / FULL_SCALE, -1, 1))
                rows.append(
                    (
                        self.info.id,
                        sym,
                        pd.Timestamp(d),
                        s[post],
                        v,
                        f"moved {ear:+.1%} vs SPY in the 3 days around its {d:%d %b %Y} "
                        "earnings report",
                    )
                )
        df = pd.DataFrame(rows, columns=COLUMNS)
        df["value"] = df["value"].astype(np.float64)
        return df
