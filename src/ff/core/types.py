"""Core domain types shared by every layer.

All timestamps are session dates (``pd.Timestamp``, tz-naive, normalized) in the
MVP's daily-bar world. Intraday work will add tz-aware timestamps in UTC.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum

import pandas as pd

Symbol = str


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET_ON_OPEN = "market_on_open"  # filled at the next session's open
    STOP = "stop"  # sell stop: triggers when price trades at or below stop_price


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SignalContribution:
    """One input that drove a decision, with its value and plain-English meaning."""

    name: str
    value: float
    note: str


@dataclass(frozen=True)
class Rationale:
    """Human-readable reasoning attached to every intent and recommendation."""

    summary: str
    signals: tuple[SignalContribution, ...] = ()
    risk: str = ""
    invalidation: str = ""

    def as_text(self) -> str:
        parts = [self.summary]
        if self.signals:
            parts.append(
                "Signals: " + "; ".join(f"{s.name}={s.value:.4g} ({s.note})" for s in self.signals)
            )
        if self.risk:
            parts.append(f"Risk: {self.risk}")
        if self.invalidation:
            parts.append(f"Invalidated if: {self.invalidation}")
        return " | ".join(parts)


@dataclass(frozen=True)
class Intent:
    """What a strategy wants, before risk management.

    The MVP expresses intents as target portfolio weights (fraction of the
    strategy's equity). ``target_weight == 0`` means exit.
    """

    symbol: Symbol
    target_weight: float
    confidence: float
    stop_price: float | None
    rationale: Rationale
    take_profit: float | None = None


_order_ids = itertools.count(1)


def next_order_id() -> int:
    return next(_order_ids)


def reserve_order_ids(above: int) -> None:
    """Make new order ids start after ``above`` (paper state restored from disk)."""
    global _order_ids
    peek = next(_order_ids)
    _order_ids = itertools.count(max(peek, above + 1))


@dataclass
class Order:
    symbol: Symbol
    side: Side
    quantity: int
    type: OrderType
    created: pd.Timestamp
    stop_price: float | None = None
    reason: str = ""
    id: int = field(default_factory=next_order_id)
    filled_qty: int = 0
    status: OrderStatus = OrderStatus.PENDING

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_qty


@dataclass(frozen=True)
class Fill:
    order_id: int
    symbol: Symbol
    side: Side
    quantity: int
    price: float  # execution price incl. spread and slippage
    commission: float
    date: pd.Timestamp
    reason: str = ""

    @property
    def signed_qty(self) -> int:
        return self.quantity if self.side is Side.BUY else -self.quantity
