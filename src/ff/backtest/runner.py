"""Run a configured backtest end to end and write a reproducible run folder."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pandas as pd

from ff.analytics.metrics import performance
from ff.analytics.report import render
from ff.backtest.engine import BacktestEngine, BacktestResult
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig, RunConfig, StrategyConfig
from ff.data.providers.base import ProviderCaps
from ff.data.store import BarStore, DataIssue, make_provider
from ff.data.view import MarketData
from ff.markets.us import CostModel, USMarket
from ff.risk.engine import RiskEngine
from ff.signals.base import SignalStore
from ff.strategies.base import build_strategy

log = logging.getLogger(__name__)
WARMUP_CALENDAR_DAYS = 550


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _symbols(cfg: RunConfig) -> list[str]:
    syms = list(dict.fromkeys(["SPY", *cfg.universe]))
    for b in cfg.benchmarks:
        syms += [s for s in b.params.get("weights", {}) if s not in syms]
        syms += [s for s in b.params.get("symbols", []) if s not in syms]
    return syms


@dataclass
class Prepared:
    """Everything a set of runs shares: data, cash rate and provenance."""

    data: MarketData
    cash_rate: pd.Series
    start: pd.Timestamp
    end: pd.Timestamp
    provider: ProviderCaps
    issues: list[DataIssue]
    signals: SignalStore | None = None


def prepare(cfg: RunConfig, refresh: bool = False) -> Prepared:
    start = pd.Timestamp(cfg.start)
    end = pd.Timestamp(cfg.end) if cfg.end else pd.Timestamp.today().normalize()
    provider = make_provider(cfg.provider)
    store = BarStore(provider, cfg.data_dir)
    load_from = start - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
    if cfg.signals:
        # sources are scored on their whole history, so load prices from its start
        load_from = min(
            load_from,
            pd.Timestamp(cfg.signals.get("history_start", "2007-01-01")) - pd.Timedelta(days=400),
        )
    bars, issues = store.load(_symbols(cfg), load_from, end, refresh=refresh)
    data = MarketData(bars)
    sessions = data.sessions("SPY")
    if cfg.cash_rate_fixed is not None or provider.caps.name == "synthetic":
        fixed = cfg.cash_rate_fixed if cfg.cash_rate_fixed is not None else 0.02
        rate = pd.Series(fixed, index=sessions)
    elif cfg.cash_rate_symbol:
        rate = store.get(cfg.cash_rate_symbol, load_from, end, refresh=refresh)["close"] / 100.0
    else:
        rate = pd.Series(0.0, index=sessions)
    signals = None
    if cfg.signals:
        from ff.signals.build import SignalsConfig, build_store

        scfg = SignalsConfig.from_dict(cfg.signals, cfg.universe)
        signals = build_store(
            scfg,
            data,
            cfg.data_dir,
            end,
            refresh=refresh,
            offline_only=provider.caps.name == "synthetic",
        )
    return Prepared(data, rate.astype(float), start, end, provider.caps, issues, signals)


def run_strategy(
    sc: StrategyConfig,
    cfg: RunConfig,
    prep: Prepared,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    audit_path: Path | None = None,
    cost_multiplier: float = 1.0,
    risk_override: dict[str, float] | None = None,
) -> BacktestResult:
    """Run one strategy (or benchmark) through the shared engine, costs and risk."""
    params = dict(sc.params)
    uni = params.pop("symbols", cfg.universe)
    strat = build_strategy(sc.kind, sc.id, uni, params)
    audit = AuditLog(audit_path)
    risk_cfg = (
        cfg.risk
        if not strat.is_benchmark
        else RiskConfig(max_position_weight=1.0, kill_switch_file=cfg.risk.kill_switch_file)
    )
    if risk_override:
        risk_cfg = replace(risk_cfg, **risk_override)  # type: ignore[arg-type]
    risk = RiskEngine(risk_cfg, audit, passthrough=strat.is_benchmark)
    costs = replace(
        cfg.costs,
        half_spread_bps=cfg.costs.half_spread_bps * cost_multiplier,
        slippage_bps=cfg.costs.slippage_bps * cost_multiplier,
        commission_per_share=cfg.costs.commission_per_share * cost_multiplier,
    )
    market = USMarket(CostModel(costs), cfg.settlement_days)
    engine = BacktestEngine(
        strat,
        prep.data,
        market,
        risk,
        cfg.initial_capital,
        audit,
        cash_rate=prep.cash_rate,
        signals=prep.signals,
    )
    try:
        return engine.run(start or prep.start, end or prep.end)
    finally:
        audit.close()


def data_warning(caps: ProviderCaps) -> str:
    return (
        f"Data source: {caps.name}. "
        + caps.notes
        + (" Does not include delisted securities." if not caps.includes_delisted else "")
    )


def run_id_for(cfg: RunConfig, kind: str = "") -> str:
    tag = f"_{kind}" if kind else ""
    return f"{pd.Timestamp.now():%Y%m%d-%H%M%S}_{cfg.name}{tag}_{cfg.fingerprint()}"


def run_backtest(cfg: RunConfig, refresh: bool = False, out_root: Path | None = None) -> Path:
    prep = prepare(cfg, refresh)
    run_id = run_id_for(cfg)
    out = (out_root or Path(cfg.runs_dir)) / run_id
    out.mkdir(parents=True, exist_ok=True)

    results = [run_strategy(cfg.strategy, cfg, prep, audit_path=out / "audit.jsonl")]
    results += [run_strategy(b, cfg, prep) for b in cfg.benchmarks]

    spy = next((r.equity for r in results[1:] if r.strategy_id.lower().startswith("spy")), None)
    metrics = {
        r.strategy_id: performance(
            r.equity, r.gross_exposure, r.closed_trades, benchmark=spy, rf_annual=prep.cash_rate
        )
        for r in results
    }
    meta = {
        "run_id": run_id,
        "period": f"{results[0].equity.index[0].date()} to {results[0].equity.index[-1].date()}",
        "provider": prep.provider.name,
        "git_commit": _git_commit(),
        "config_fingerprint": cfg.fingerprint(),
        "initial_capital": f"${cfg.initial_capital:,.0f}",
        "cash_rate": cfg.cash_rate_symbol if cfg.cash_rate_fixed is None else cfg.cash_rate_fixed,
        "costs": json.dumps(asdict(cfg.costs)),
        "risk": json.dumps(asdict(cfg.risk)),
        "data_warning": data_warning(prep.provider),
    }
    issue_txt = [
        f"{i.symbol} {i.date.date() if i.date is not None else ''} {i.kind} {i.detail}"
        for i in prep.issues
    ]

    (out / "report.html").write_text(render(results, metrics, meta, issue_txt), encoding="utf-8")
    (out / "metrics.json").write_text(json.dumps({"meta": meta, "metrics": metrics}, indent=2))
    pd.DataFrame({r.strategy_id: r.equity for r in results}).to_csv(out / "equity.csv")
    pd.DataFrame([asdict(t) for t in results[0].closed_trades]).to_csv(
        out / "trades.csv", index=False
    )
    pd.DataFrame(
        [
            {
                "symbol": i.symbol,
                "target_weight": i.target_weight,
                "stop": i.stop_price,
                "rationale": i.rationale.as_text(),
            }
            for i in results[0].recommendations
        ]
    ).to_csv(out / "recommendations.csv", index=False)
    log.info("run written to %s", out)
    return out


def copy_config(src: Path, out: Path) -> None:
    shutil.copy(src, out / "config.yaml")
