"""Naive risk parity with a volatility target (always invested, no timing).

Rule, at each month-end close: every asset gets a weight proportional to
1 / its trailing volatility, so each contributes roughly equal risk, and the
book is scaled to ``target_vol``. A cash account cannot lever up, so the risk
engine caps the book at 100% invested and each asset at its position cap.

Evidence: Asness, Frazzini & Pedersen (2012), "Leverage Aversion and Risk
Parity"; Maillard, Roncalli & Teiletche (2010) on equal risk contribution.
Without leverage it tends to be bond-heavy, and it is hurt when stocks and
bonds fall together (as in 2022).
"""

from __future__ import annotations

from dataclasses import replace

from ff.analytics.indicators import atr, realized_vol
from ff.core.types import Intent, Rationale, SignalContribution
from ff.strategies.base import Context, Strategy, register
from ff.strategies.common import vol_scale


@register("risk_parity")
class RiskParity(Strategy):
    tags = frozenset({"track:A", "risk_parity", "allocation"})

    def __init__(
        self,
        id: str,
        universe: list[str],
        vol_days: int = 60,
        target_vol: float = 0.08,
        atr_days: int = 20,
        atr_stop_mult: float = 4.0,
    ) -> None:
        super().__init__(
            id,
            universe,
            vol_days=vol_days,
            target_vol=target_vol,
            atr_days=atr_days,
            atr_stop_mult=atr_stop_mult,
        )
        self.vol_days = vol_days
        self.target_vol = target_vol
        self.atr_days = atr_days
        self.atr_stop_mult = atr_stop_mult
        self.warmup = max(vol_days + 1, atr_days + 1)

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        if not (ctx.is_month_end or ctx.is_first_session):
            return None
        raw: dict[str, float] = {}
        intents: list[Intent] = []
        for sym in self.universe:
            hist = ctx.data.history(sym, self.warmup + 1)
            if len(hist) < self.warmup or ctx.data.close(sym) is None:
                continue
            px = float(hist["close"].iloc[-1])
            vol = max(realized_vol(hist["close"], self.vol_days), 1e-4)
            stop = px - self.atr_stop_mult * atr(hist, self.atr_days)
            raw[sym] = 1.0 / vol
            intents.append(
                Intent(
                    sym,
                    0.0,
                    0.5,
                    stop,
                    Rationale(
                        summary=f"Hold {sym} at equal risk share (vol {vol:.0%})",
                        signals=(
                            SignalContribution("vol_annual", vol, f"{self.vol_days}d realized vol"),
                        ),
                        risk=f"stop {stop:.2f} ({stop / px - 1:+.1%}); "
                        "stocks and bonds can fall together",
                        invalidation="monthly rebalance only; exits on stop",
                    ),
                )
            )
        scale = vol_scale(ctx, raw, self.vol_days, self.target_vol)
        return [replace(i, target_weight=raw[i.symbol] * scale) for i in intents]
