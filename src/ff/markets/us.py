"""US equities market model: costs and settlement.

The trading calendar comes from the data itself (sessions present in the
reference symbol), which is exact for historical replay. The live loop in
Phase 3 adds an exchange calendar library for future sessions and half days.
"""

from __future__ import annotations

from dataclasses import dataclass

from ff.core.config import CostConfig
from ff.core.types import Side


@dataclass(frozen=True)
class CostModel:
    cfg: CostConfig

    def fill_price(self, side: Side, reference: float) -> float:
        """Reference price moved against us by half the spread plus slippage."""
        adverse = (self.cfg.half_spread_bps + self.cfg.slippage_bps) / 10_000
        return reference * (1 + adverse) if side is Side.BUY else reference * (1 - adverse)

    def commission(self, quantity: int) -> float:
        if quantity <= 0:
            return 0.0
        return max(self.cfg.commission_min, self.cfg.commission_per_share * quantity)

    def max_fill_qty(self, day_volume: float) -> int:
        if day_volume <= 0:
            return 0
        return int(day_volume * self.cfg.max_participation)


@dataclass(frozen=True)
class USMarket:
    costs: CostModel
    settlement_days: int = 1
    currency: str = "USD"
    name: str = "US"
