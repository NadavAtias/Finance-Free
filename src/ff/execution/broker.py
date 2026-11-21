"""Broker interface and the simulated broker used by backtests and paper trading.

Fill rules (daily bars, manual-execution assumptions):
- MARKET_ON_OPEN orders created after session t's close fill at session t+1's
  open, moved against us by half-spread + slippage.
- Fill size is capped at ``max_participation`` of the session's volume; any
  remainder is cancelled and logged (a partial fill).
- Buys are capped by available cash (cash account, no margin).
- Sell STOP orders: if the session opens at or below the stop, fill at the open
  (gap risk); otherwise if the low touches the stop, fill at the stop. Both
  then pay spread + slippage.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from ff.core.audit import AuditLog
from ff.core.types import Fill, Order, OrderStatus, OrderType, Side
from ff.data.view import MarketData
from ff.execution.portfolio import Portfolio
from ff.markets.us import USMarket


class Broker(Protocol):
    def submit(self, order: Order) -> int: ...
    def cancel(self, order_id: int) -> None: ...
    def open_orders(self) -> list[Order]: ...


class SimBroker:
    def __init__(
        self, market: USMarket, portfolio: Portfolio, data: MarketData, audit: AuditLog
    ) -> None:
        self.market = market
        self.portfolio = portfolio
        self.data = data
        self.audit = audit
        self._orders: dict[int, Order] = {}
        self.fills: list[Fill] = []

    # --- Broker interface -------------------------------------------------
    def submit(self, order: Order) -> int:
        self._orders[order.id] = order
        self.audit.write(
            "order",
            order.created,
            id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=order.quantity,
            type=order.type,
            stop=order.stop_price,
            reason=order.reason,
        )
        return order.id

    def cancel(self, order_id: int) -> None:
        o = self._orders.pop(order_id, None)
        if o and o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            o.status = OrderStatus.CANCELLED

    def open_orders(self) -> list[Order]:
        return list(self._orders.values())

    def stop_for(self, symbol: str) -> Order | None:
        for o in self._orders.values():
            if o.symbol == symbol and o.type is OrderType.STOP:
                return o
        return None

    def cancel_all(self, order_type: OrderType | None = None) -> None:
        for oid in [i for i, o in self._orders.items() if order_type in (None, o.type)]:
            self.cancel(oid)

    # --- Simulation ---------------------------------------------------------
    def _fill(self, order: Order, ref_price: float, qty: int, date: pd.Timestamp, idx: int) -> None:
        costs = self.market.costs
        price = costs.fill_price(order.side, ref_price)
        if order.side is Side.BUY:
            affordable = int((self.portfolio.cash - costs.commission(qty)) // price)
            qty = min(qty, max(affordable, 0))
        else:
            qty = min(qty, self.portfolio.quantity(order.symbol))
        if qty <= 0:
            order.status = OrderStatus.REJECTED
            self.audit.write(
                "reject",
                date,
                id=order.id,
                symbol=order.symbol,
                reason="no cash" if order.side is Side.BUY else "no position",
            )
            return
        fill = Fill(
            order.id,
            order.symbol,
            order.side,
            qty,
            price,
            costs.commission(qty),
            date,
            order.reason,
        )
        self.portfolio.apply(fill, idx, self.market.settlement_days)
        self.fills.append(fill)
        order.filled_qty += qty
        order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIAL
        self.audit.write(
            "fill",
            date,
            id=order.id,
            symbol=fill.symbol,
            side=fill.side,
            qty=qty,
            price=round(price, 4),
            commission=fill.commission,
            reason=order.reason,
        )

    def process_open(self, date: pd.Timestamp, idx: int) -> None:
        moo = [o for o in self._orders.values() if o.type is OrderType.MARKET_ON_OPEN]
        # Sells first so their proceeds can fund buys.
        moo.sort(key=lambda o: 0 if o.side is Side.SELL else 1)
        for o in moo:
            bar = self.data.bar(o.symbol, date)
            if bar is None:
                continue  # no session for this symbol today; keep order for next open
            cap = self.market.costs.max_fill_qty(float(bar["volume"]))
            qty = min(o.remaining, cap) if cap > 0 else o.remaining
            self._fill(o, float(bar["open"]), qty, date, idx)
            if o.remaining > 0 and o.status is OrderStatus.PARTIAL:
                self.audit.write(
                    "partial_cancel", date, id=o.id, symbol=o.symbol, unfilled=o.remaining
                )
            self._orders.pop(o.id, None)

    def process_stops(self, date: pd.Timestamp, idx: int) -> None:
        for o in [o for o in self._orders.values() if o.type is OrderType.STOP]:
            held = self.portfolio.quantity(o.symbol)
            if held <= 0:
                self._orders.pop(o.id, None)
                continue
            bar = self.data.bar(o.symbol, date)
            if bar is None or o.stop_price is None:
                continue
            if float(bar["open"]) <= o.stop_price:
                ref = float(bar["open"])
            elif float(bar["low"]) <= o.stop_price:
                ref = o.stop_price
            else:
                continue
            o.quantity = held
            o.reason = f"stop hit at {o.stop_price:.2f}"
            self._fill(o, ref, held, date, idx)
            self._orders.pop(o.id, None)
