"""Reference portfolios: buy-and-hold and fixed-mix. They run through the same
engine, costs and fills as real strategies, so comparisons are apples to apples."""

from __future__ import annotations

from typing import Any

from ff.core.types import Intent, Rationale
from ff.strategies.base import Context, Strategy, register


@register("buy_and_hold")
class BuyAndHold(Strategy):
    is_benchmark = True
    tags = frozenset({"benchmark"})

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        if not ctx.is_first_session:
            return None
        w = 1.0 / len(self.universe)
        return [Intent(s, w, 1.0, None, Rationale(f"Buy and hold {s}")) for s in self.universe]


@register("fixed_mix")
class FixedMix(Strategy):
    """Monthly-rebalanced fixed weights, e.g. 60/40 SPY/IEF."""

    is_benchmark = True
    tags = frozenset({"benchmark"})

    def __init__(
        self, id: str, universe: list[str], weights: dict[str, float] | None = None, **params: Any
    ) -> None:
        weights = weights or {s: 1.0 / len(universe) for s in universe}
        super().__init__(id, list(weights), weights=weights, **params)
        self.weights = weights

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        if not (ctx.is_first_session or ctx.is_month_end):
            return None
        return [
            Intent(s, w, 1.0, None, Rationale(f"Rebalance {s} to {w:.0%}"))
            for s, w in self.weights.items()
        ]
