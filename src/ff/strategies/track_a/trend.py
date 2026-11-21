"""Time-series trend on a diversified ETF basket (Track A, MVP strategy).

Rule, evaluated at each month-end close:
- An asset is "in trend" when its close is above its N-day simple moving
  average (Faber 2007) AND its trailing 12-month return is positive
  (time-series momentum, Moskowitz, Ooi & Pedersen 2012). Requiring both
  reduces whipsaw at the cost of slightly later entries.
- In-trend assets get inverse-volatility weights, then the whole book is scaled
  so its estimated volatility (trailing covariance) equals ``target_vol``
  (volatility targeting, Moreira & Muir 2017). Gross exposure and per-position
  caps are enforced by the risk engine. Out-of-trend assets go to cash.
- Each position carries a protective stop ``atr_stop_mult`` x ATR below the
  close, which also defines its risk for position sizing.

Parameters are literature defaults, not optimized.
"""

from __future__ import annotations

from dataclasses import replace

from ff.analytics.indicators import atr, realized_vol, sma, total_return
from ff.core.types import Intent, Rationale, SignalContribution
from ff.strategies.base import Context, Strategy, register
from ff.strategies.common import vol_scale


@register("trend_etf")
class TrendETF(Strategy):
    tags = frozenset({"track:A", "trend", "tsmom"})

    def __init__(
        self,
        id: str,
        universe: list[str],
        sma_days: int = 200,
        mom_days: int = 252,
        vol_days: int = 60,
        target_vol: float = 0.10,
        atr_days: int = 20,
        atr_stop_mult: float = 3.0,
    ) -> None:
        super().__init__(
            id,
            universe,
            sma_days=sma_days,
            mom_days=mom_days,
            vol_days=vol_days,
            target_vol=target_vol,
            atr_days=atr_days,
            atr_stop_mult=atr_stop_mult,
        )
        self.sma_days = sma_days
        self.mom_days = mom_days
        self.vol_days = vol_days
        self.target_vol = target_vol
        self.atr_days = atr_days
        self.atr_stop_mult = atr_stop_mult
        self.warmup = max(sma_days, mom_days + 1, vol_days + 1, atr_days + 1)

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        if not (ctx.is_month_end or ctx.is_first_session):
            return None
        intents: list[Intent] = []
        longs: dict[str, float] = {}  # symbol -> inverse-vol raw weight
        for sym in self.universe:
            hist = ctx.data.history(sym, self.warmup + 1)
            if len(hist) < self.warmup or ctx.data.close(sym) is None:
                continue  # not enough history yet (e.g. ETF launched later)
            close = hist["close"]
            px = float(close.iloc[-1])
            ma = sma(close, self.sma_days)
            mom = total_return(close, self.mom_days)
            vol = max(realized_vol(close, self.vol_days), 1e-4)
            a = atr(hist, self.atr_days)
            stop = px - self.atr_stop_mult * a
            above = px / ma - 1
            signals = (
                SignalContribution("price_vs_sma", above, f"close vs {self.sma_days}d SMA"),
                SignalContribution("momentum_12m", mom, f"{self.mom_days}d total return"),
                SignalContribution("vol_annual", vol, f"{self.vol_days}d realized vol"),
            )
            if above > 0 and mom > 0:
                longs[sym] = 1.0 / vol
                conf = min(1.0, 0.5 + 0.5 * min(abs(above) / 0.05, 1.0))
                intents.append(
                    Intent(
                        symbol=sym,
                        target_weight=0.0,
                        confidence=conf,
                        stop_price=stop,
                        rationale=Rationale(
                            summary=f"Long {sym}: {above:+.1%} vs {self.sma_days}d SMA, "
                            f"12m return {mom:+.1%}",
                            signals=signals,
                            risk=f"stop {stop:.2f} ({stop / px - 1:+.1%}); asset vol {vol:.0%}, "
                            f"book scaled to {self.target_vol:.0%} portfolio vol",
                            invalidation=f"month-end close below SMA ({ma:.2f}), 12m return "
                            f"turns negative, or stop hit",
                        ),
                    )
                )
            else:
                intents.append(
                    Intent(
                        symbol=sym,
                        target_weight=0.0,
                        confidence=0.5,
                        stop_price=None,
                        rationale=Rationale(
                            summary=f"Flat {sym}: trend filter off ({above:+.1%} vs SMA, "
                            f"12m {mom:+.1%})",
                            signals=signals,
                        ),
                    )
                )
        scale = vol_scale(ctx, longs, self.vol_days, self.target_vol)
        return [
            replace(i, target_weight=longs[i.symbol] * scale) if i.symbol in longs else i
            for i in intents
        ]
