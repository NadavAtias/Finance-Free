"""Cross-sectional momentum with an absolute-momentum filter (ETF rotation).

Rule, evaluated at each month-end close:
- Rank the universe by the "12-1" return: the return over ``lookback_days``
  skipping the most recent ``skip_days`` (Jegadeesh & Titman 1993; the skip
  avoids the short-term reversal effect).
- Hold the ``top_n`` best ranked, but only those whose own 12-1 return beats
  cash (absolute momentum, Antonacci 2014 "Dual Momentum"; Moskowitz, Ooi &
  Pedersen 2012). Anything else goes to cash.
- Holdings get inverse-volatility weights, and the book is scaled to
  ``target_vol`` (Moreira & Muir 2017). Stops sit ``atr_stop_mult`` x ATR below.

Evidence: Asness, Moskowitz & Pedersen (2013) find momentum across asset
classes, not only single stocks. Momentum suffers sharp "crashes" at market
rebounds (Daniel & Moskowitz 2016), which the absolute filter only partly helps.
"""

from __future__ import annotations

from ff.analytics.indicators import atr, realized_vol
from ff.core.types import Intent, Rationale, SignalContribution
from ff.strategies.base import Context, Strategy, register
from ff.strategies.common import vol_scale


@register("xs_momentum")
class CrossSectionalMomentum(Strategy):
    tags = frozenset({"track:A", "momentum", "rotation"})

    def __init__(
        self,
        id: str,
        universe: list[str],
        lookback_days: int = 252,
        skip_days: int = 21,
        top_n: int = 3,
        vol_days: int = 60,
        target_vol: float = 0.10,
        atr_days: int = 20,
        atr_stop_mult: float = 3.0,
    ) -> None:
        super().__init__(
            id,
            universe,
            lookback_days=lookback_days,
            skip_days=skip_days,
            top_n=top_n,
            vol_days=vol_days,
            target_vol=target_vol,
            atr_days=atr_days,
            atr_stop_mult=atr_stop_mult,
        )
        self.lookback_days = lookback_days
        self.skip_days = skip_days
        self.top_n = top_n
        self.vol_days = vol_days
        self.target_vol = target_vol
        self.atr_days = atr_days
        self.atr_stop_mult = atr_stop_mult
        self.warmup = max(lookback_days + 1, vol_days + 1, atr_days + 1)

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        if not (ctx.is_month_end or ctx.is_first_session):
            return None
        scores: dict[str, float] = {}
        for sym in self.universe:
            hist = ctx.data.history(sym, self.warmup + 1)
            if len(hist) < self.warmup or ctx.data.close(sym) is None:
                continue
            c = hist["close"]
            scores[sym] = float(c.iloc[-1 - self.skip_days] / c.iloc[-1 - self.lookback_days] - 1)
        ranked = sorted(scores, key=lambda s: -scores[s])
        hurdle = self._cash_hurdle(ctx)
        picks = [s for s in ranked[: self.top_n] if scores[s] > hurdle]

        raw: dict[str, float] = {}
        intents: dict[str, Intent] = {}
        for rank, sym in enumerate(ranked, start=1):
            hist = ctx.data.history(sym, self.warmup + 1)
            px = float(hist["close"].iloc[-1])
            vol = max(realized_vol(hist["close"], self.vol_days), 1e-4)
            signals = (
                SignalContribution(
                    "momentum_12_1",
                    scores[sym],
                    f"{self.lookback_days}d return skipping {self.skip_days}d",
                ),
                SignalContribution("rank", float(rank), f"of {len(ranked)}"),
                SignalContribution("vol_annual", vol, f"{self.vol_days}d realized vol"),
            )
            if sym in picks:
                stop = px - self.atr_stop_mult * atr(hist, self.atr_days)
                raw[sym] = 1.0 / vol
                intents[sym] = Intent(
                    sym,
                    0.0,
                    min(1.0, 0.5 + 0.1 * (self.top_n - rank + 1)),
                    stop,
                    Rationale(
                        summary=f"Long {sym}: rank {rank} of {len(ranked)} on 12-1 momentum "
                        f"({scores[sym]:+.1%})",
                        signals=signals,
                        risk=f"stop {stop:.2f} ({stop / px - 1:+.1%}); "
                        "momentum can reverse sharply "
                        "at market turning points",
                        invalidation=f"drops out of the top {self.top_n}, 12-1 return falls below "
                        "cash, or stop hit",
                    ),
                )
            else:
                why = (
                    f"rank {rank}, outside top {self.top_n}"
                    if rank > self.top_n
                    else f"12-1 return {scores[sym]:+.1%} below cash"
                )
                intents[sym] = Intent(
                    sym, 0.0, 0.5, None, Rationale(f"Flat {sym}: {why}", signals=signals)
                )
        scale = vol_scale(ctx, raw, self.vol_days, self.target_vol)
        for sym, w in raw.items():
            intents[sym] = _with_weight(intents[sym], w * scale)
        return list(intents.values())

    def _cash_hurdle(self, ctx: Context) -> float:
        """Momentum must beat cash. Approximated as 0: the engine's T-bill series
        is not part of the strategy's data view in the MVP."""
        return 0.0


def _with_weight(it: Intent, w: float) -> Intent:
    from dataclasses import replace

    return replace(it, target_weight=w)
