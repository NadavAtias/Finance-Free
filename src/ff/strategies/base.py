"""Strategy plug-in interface and registry.

A strategy sees only a ``Context`` (point-in-time data + read-only portfolio
state) and returns intents. Returning ``None`` means "no rebalance today": the
engine keeps current positions and stops. Returning a list means "this is the
complete target book"; symbols not in it are exited.

To add a strategy: subclass ``Strategy``, decorate with ``@register("kind")``,
and reference ``kind`` from a YAML config. Nothing else changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import pandas as pd

from ff.core.types import Intent
from ff.data.view import DataView

if TYPE_CHECKING:
    from ff.signals.base import SignalView


@dataclass(frozen=True)
class Context:
    date: pd.Timestamp
    data: DataView
    equity: float
    weights: dict[str, float]
    is_month_end: bool
    is_first_session: bool
    #: point-in-time market-intelligence signals (None unless the run configures them)
    signals: SignalView | None = None


class Strategy(ABC):
    kind: ClassVar[str] = ""
    tags: ClassVar[frozenset[str]] = frozenset()
    #: sessions of history needed before the first decision
    warmup: int = 0
    #: benchmarks bypass per-trade risk caps (they are reference portfolios)
    is_benchmark: ClassVar[bool] = False
    #: strategies that read ``ctx.signals`` (the runner then builds the signal store)
    needs_signals: ClassVar[bool] = False

    def __init__(self, id: str, universe: list[str], **params: Any) -> None:
        self.id = id
        self.universe = universe
        self.params = params

    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Intent] | None: ...


_REGISTRY: dict[str, type[Strategy]] = {}


def register(kind: str) -> Callable[[type[Strategy]], type[Strategy]]:
    def deco(cls: type[Strategy]) -> type[Strategy]:
        cls.kind = kind
        _REGISTRY[kind] = cls
        return cls

    return deco


def strategy_class(kind: str) -> type[Strategy] | None:
    # Import modules so their @register decorators run.
    from ff.strategies import benchmarks, conviction, ensemble, track_a  # noqa: F401

    return _REGISTRY.get(kind)


def build_strategy(kind: str, id: str, universe: list[str], params: dict[str, Any]) -> Strategy:
    if strategy_class(kind) is None:
        raise KeyError(f"unknown strategy kind {kind!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[kind](id=id, universe=universe, **params)
