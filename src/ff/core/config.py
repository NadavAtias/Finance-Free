"""Typed run configuration loaded from YAML.

Secrets never live here: provider keys are read from environment variables
(see ``.env.example``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CostConfig:
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    half_spread_bps: float = 1.0  # paid on every fill, ETFs are typically 0.5-2 bps
    slippage_bps: float = 2.0  # extra adverse move for a manual next-open fill
    max_participation: float = 0.01  # max fraction of a day's volume we can fill


@dataclass
class RiskConfig:
    max_risk_per_trade: float = 0.01  # equity fraction lost if stop is hit
    max_position_weight: float = 0.25
    max_gross_exposure: float = 1.0  # cash account: no leverage
    daily_loss_limit: float = 0.02  # block new entries next session
    max_drawdown: float = 0.15  # flatten and halt until manual reset
    kill_switch_file: str = "KILL_SWITCH"


@dataclass
class StrategyConfig:
    id: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunConfig:
    name: str
    start: str
    end: str | None
    initial_capital: float
    provider: str
    universe: list[str]
    strategy: StrategyConfig
    benchmarks: list[StrategyConfig] = field(default_factory=list)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    data_dir: str = "data"
    runs_dir: str = "runs"
    settlement_days: int = 1  # US T+1 since May 2024

    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def _strategy(d: dict[str, Any]) -> StrategyConfig:
    return StrategyConfig(id=d["id"], kind=d["kind"], params=d.get("params", {}))


def load_config(path: str | Path) -> RunConfig:
    raw = yaml.safe_load(Path(path).read_text())
    return RunConfig(
        name=raw["name"],
        start=str(raw["start"]),
        end=str(raw["end"]) if raw.get("end") else None,
        initial_capital=float(raw.get("initial_capital", 100_000)),
        provider=raw.get("provider", "stooq"),
        universe=list(raw["universe"]),
        strategy=_strategy(raw["strategy"]),
        benchmarks=[_strategy(b) for b in raw.get("benchmarks", [])],
        costs=CostConfig(**raw.get("costs", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        data_dir=raw.get("data_dir", "data"),
        runs_dir=raw.get("runs_dir", "runs"),
        settlement_days=int(raw.get("settlement_days", 1)),
    )
