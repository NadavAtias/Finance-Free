"""Paper-trading configuration (``config/paper.yaml``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ff.core.config import CostConfig, RiskConfig, RunConfig, StrategyConfig


@dataclass
class SleeveConfig:
    id: str
    capital: float
    strategy: StrategyConfig
    universe: list[str]
    benchmark_rules: bool = False
    universe_file: str | None = None


@dataclass
class PortfolioConfig:
    id: str
    name: str
    sleeves: list[SleeveConfig]

    @property
    def capital(self) -> float:
        return sum(s.capital for s in self.sleeves)


@dataclass
class BenchmarkConfig:
    id: str
    name: str
    weights: dict[str, float]


@dataclass
class PaperConfig:
    name: str
    start: str
    provider: str
    portfolios: list[PortfolioConfig]
    benchmarks: list[BenchmarkConfig]
    placeholders: list[dict[str, str]] = field(default_factory=list)
    research: list[dict[str, str]] = field(default_factory=list)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    state_dir: str = "state"
    data_dir: str = "data"
    runs_dir: str = "runs"
    settlement_days: int = 1
    cash_rate_symbol: str | None = "^IRX"
    cash_rate_fixed: float | None = None
    #: Phase 4 signal sources (``ff.signals.build.SignalsConfig``)
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def sleeves(self) -> list[tuple[PortfolioConfig, SleeveConfig]]:
        return [(p, s) for p in self.portfolios for s in p.sleeves]

    @property
    def start_capital(self) -> float:
        return sum(p.capital for p in self.portfolios)

    @property
    def kill_switch_path(self) -> Path:
        p = Path(self.risk.kill_switch_file)
        return p if p.is_absolute() else Path(self.state_dir) / p

    @property
    def db_path(self) -> Path:
        return Path(self.state_dir) / "paper.sqlite"

    def symbols(self) -> list[str]:
        syms: list[str] = ["SPY"]
        for _, s in self.sleeves:
            syms += s.universe
        for b in self.benchmarks:
            syms += list(b.weights)
        return list(dict.fromkeys(syms))

    def as_run_config(self) -> RunConfig:
        """A RunConfig view so the backtest data preparation can be reused."""
        return RunConfig(
            name=self.name,
            start=self.start,
            end=None,
            initial_capital=self.start_capital,
            provider=self.provider,
            universe=self.symbols(),
            strategy=StrategyConfig("paper", "buy_and_hold"),
            costs=self.costs,
            risk=self.risk,
            data_dir=self.data_dir,
            runs_dir=self.runs_dir,
            settlement_days=self.settlement_days,
            cash_rate_symbol=self.cash_rate_symbol,
            cash_rate_fixed=self.cash_rate_fixed,
            signals=self.signals_config(),
        )

    def signals_config(self) -> dict[str, Any]:
        """The signals block with its universe: every sleeve that reads signals."""
        if not self.signals:
            return {}
        from ff.strategies.base import strategy_class

        uni: list[str] = list(self.signals.get("universe", []))
        for _, s in self.sleeves:
            cls = strategy_class(s.strategy.kind)
            if cls is not None and cls.needs_signals:
                uni += [t for t in s.universe if t not in uni]
        return {**self.signals, "universe": uni}


def _sleeve(d: dict[str, Any]) -> SleeveConfig:
    from ff.core.universe import tickers

    st = d["strategy"]
    params = dict(st.get("params", {}))
    universe = list(d.get("universe") or params.get("weights", {}) or params.get("symbols", []))
    if d.get("universe_file"):
        universe += [t for t in tickers(d["universe_file"]) if t not in universe]
        params.setdefault("universe_file", d["universe_file"])
    return SleeveConfig(
        id=d["id"],
        capital=float(d["capital"]),
        strategy=StrategyConfig(d["id"], st["kind"], params),
        universe=universe,
        benchmark_rules=bool(d.get("benchmark_rules", False)),
        universe_file=d.get("universe_file"),
    )


def load_paper_config(path: str | Path) -> PaperConfig:
    raw = yaml.safe_load(Path(path).read_text())
    return PaperConfig(
        name=raw.get("name", "paper"),
        start=str(raw["start"]),
        # FF_PROVIDER in .env switches data source on a server without editing the config
        provider=os.environ.get("FF_PROVIDER") or raw.get("provider", "yahoo"),
        portfolios=[
            PortfolioConfig(p["id"], p.get("name", p["id"]), [_sleeve(s) for s in p["sleeves"]])
            for p in raw["portfolios"]
        ],
        benchmarks=[
            BenchmarkConfig(b["id"], b.get("name", b["id"]), dict(b["weights"]))
            for b in raw.get("benchmarks", [])
        ],
        placeholders=list(raw.get("placeholders", [])),
        research=list(raw.get("research", [])),
        costs=CostConfig(**raw.get("costs", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        state_dir=raw.get("state_dir", "state"),
        data_dir=raw.get("data_dir", "data"),
        runs_dir=raw.get("runs_dir", "runs"),
        settlement_days=int(raw.get("settlement_days", 1)),
        cash_rate_symbol=raw.get("cash_rate_symbol", "^IRX"),
        cash_rate_fixed=raw.get("cash_rate_fixed"),
        signals=dict(raw.get("signals", {})),
    )
