"""Combine several strategies into one book with fixed capital weights.

Each member keeps its own signals and schedule; the ensemble remembers each
member's latest target book and, whenever any member rebalances, publishes
``sum(weight_i * target_i)``. A combined position's stop is the widest (lowest)
member stop, so the risk engine sizes against the most conservative distance.
Weights are fixed here; Phase 6 replaces them with performance-based allocation.
"""

from __future__ import annotations

from typing import Any

from ff.core.types import Intent, Rationale, SignalContribution
from ff.strategies.base import Context, Strategy, build_strategy, register

_LABEL = {
    "trend_etf": "Trend",
    "risk_parity": "Risk parity",
    "xs_momentum": "Momentum",
    "fixed_mix": "Fixed mix",
}


@register("ensemble")
class Ensemble(Strategy):
    tags = frozenset({"track:A", "ensemble"})

    def __init__(
        self,
        id: str,
        universe: list[str],
        members: list[dict[str, Any]] | None = None,
        weights: list[float] | None = None,
    ) -> None:
        super().__init__(id, universe, members=members, weights=weights)
        if not members:
            raise ValueError("ensemble needs at least one member")
        if weights is not None:
            # lets a validation grid vary the mix without repeating the member list
            if len(weights) != len(members):
                raise ValueError("weights must match members")
            members = [dict(m, weight=w) for m, w in zip(members, weights, strict=True)]
        total = sum(float(m.get("weight", 1.0)) for m in members)
        self.members: list[tuple[float, Strategy]] = []
        for i, m in enumerate(members):
            params = dict(m.get("params", {}))
            uni = params.pop("symbols", universe)
            strat = build_strategy(m["kind"], m.get("id", f"{id}_{m['kind']}_{i}"), uni, params)
            self.members.append((float(m.get("weight", 1.0)) / total, strat))
        self.warmup = max(s.warmup for _, s in self.members)
        self._books: dict[str, list[Intent]] = {}

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        changed = False
        for _, s in self.members:
            book = s.on_bar(ctx)
            if book is not None:
                self._books[s.id] = book
                changed = True
        if not changed:
            return None
        weight: dict[str, float] = {}
        stop: dict[str, float] = {}
        parts: dict[str, list[str]] = {}
        sigs: dict[str, list[SignalContribution]] = {}
        for w, s in self.members:
            for it in self._books.get(s.id, []):
                weight[it.symbol] = weight.get(it.symbol, 0.0) + w * it.target_weight
                if it.target_weight > 0 and it.stop_price is not None:
                    stop[it.symbol] = min(stop.get(it.symbol, it.stop_price), it.stop_price)
                if it.target_weight > 0:
                    parts.setdefault(it.symbol, []).append(
                        f"{_LABEL.get(s.kind, s.kind)} ({w:.0%} of book, wants "
                        f"{it.target_weight:.1%}): {it.rationale.summary}"
                    )
                    sigs.setdefault(it.symbol, []).extend(it.rationale.signals)
        out: list[Intent] = []
        for sym, w in weight.items():
            if w > 0:
                out.append(
                    Intent(
                        sym,
                        w,
                        0.6,
                        stop.get(sym),
                        Rationale(
                            summary=f"Long {sym}, {w:.1%} of the book. " + ". ".join(parts[sym]),
                            signals=tuple(sigs[sym]),
                            risk=f"combined stop {stop[sym]:.2f} (widest member stop)"
                            if sym in stop
                            else "",
                            invalidation="members drop it at their next rebalance, or stop hit",
                        ),
                    )
                )
            else:
                out.append(
                    Intent(sym, 0.0, 0.5, None, Rationale(f"Flat {sym}: no member holds it"))
                )
        return out
