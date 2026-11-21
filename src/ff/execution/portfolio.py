"""Cash-account ledger: positions, settled vs unsettled cash, realized trades."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ff.core.types import Fill, Side


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0
    opened: pd.Timestamp | None = None
    realized: float = 0.0  # P&L banked by partial exits, net of commissions
    peak_qty: int = 0


@dataclass(frozen=True)
class ClosedTrade:
    """One round trip: from flat to flat, including any trims and add-ons."""

    symbol: str
    entry_date: pd.Timestamp | None
    exit_date: pd.Timestamp
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    reason: str


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    # (session index when the cash settles, amount)
    unsettled: list[tuple[int, float]] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    commissions: float = 0.0
    traded_notional: float = 0.0
    good_faith_violations: int = 0
    # symbol -> session index before which a sale is a good-faith violation
    _gfv_until: dict[str, int] = field(default_factory=dict)

    @property
    def unsettled_cash(self) -> float:
        return sum(a for _, a in self.unsettled)

    @property
    def settled_cash(self) -> float:
        return self.cash - self.unsettled_cash

    def settle(self, session_idx: int) -> None:
        self.unsettled = [(i, a) for i, a in self.unsettled if i > session_idx]

    def quantity(self, symbol: str) -> int:
        p = self.positions.get(symbol)
        return p.quantity if p else 0

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.quantity * prices[s] for s, p in self.positions.items() if p.quantity)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def weights(self, prices: dict[str, float]) -> dict[str, float]:
        eq = self.equity(prices)
        if eq <= 0:
            return {}
        return {s: p.quantity * prices[s] / eq for s, p in self.positions.items() if p.quantity}

    def apply(self, fill: Fill, session_idx: int, settlement_days: int) -> None:
        pos = self.positions.setdefault(fill.symbol, Position(fill.symbol))
        notional = fill.quantity * fill.price
        self.commissions += fill.commission
        self.traded_notional += notional
        if fill.side is Side.BUY:
            if notional + fill.commission > self.settled_cash + 1e-6:
                # Paid with unsettled proceeds: selling before they settle is a GFV.
                self._gfv_until[fill.symbol] = session_idx + settlement_days
            new_qty = pos.quantity + fill.quantity
            pos.avg_cost = (pos.avg_cost * pos.quantity + notional) / new_qty
            if pos.quantity == 0:
                pos.opened = fill.date
                pos.realized = 0.0
            pos.quantity = new_qty
            pos.peak_qty = max(pos.peak_qty, new_qty)
            pos.realized -= fill.commission
            self.cash -= notional + fill.commission
        else:
            if session_idx < self._gfv_until.get(fill.symbol, -1):
                self.good_faith_violations += 1
            qty = min(fill.quantity, pos.quantity)
            pos.realized += (fill.price - pos.avg_cost) * qty - fill.commission
            pos.quantity -= qty
            proceeds = qty * fill.price - fill.commission
            self.cash += proceeds
            self.unsettled.append((session_idx + settlement_days, proceeds))
            if pos.quantity == 0:
                self.closed_trades.append(
                    ClosedTrade(
                        fill.symbol,
                        pos.opened,
                        fill.date,
                        pos.peak_qty,
                        pos.avg_cost,
                        fill.price,
                        pos.realized,
                        fill.reason,
                    )
                )
                pos.avg_cost, pos.opened, pos.realized, pos.peak_qty = 0.0, None, 0.0, 0
