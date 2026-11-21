"""Run a configured backtest end to end and write a reproducible run folder."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from ff.analytics.metrics import performance
from ff.analytics.report import render
from ff.backtest.engine import BacktestEngine, BacktestResult
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig, RunConfig, StrategyConfig
from ff.data.store import BarStore, make_provider
from ff.data.view import MarketData
from ff.markets.us import CostModel, USMarket
from ff.risk.engine import RiskEngine
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


def _run_one(
    sc: StrategyConfig,
    universe: list[str],
    cfg: RunConfig,
    data: MarketData,
    start: pd.Timestamp,
    end: pd.Timestamp,
    audit_path: Path | None,
) -> BacktestResult:
    params = dict(sc.params)
    uni = params.pop("symbols", universe)
    strat = build_strategy(sc.kind, sc.id, uni, params)
    audit = AuditLog(audit_path)
    risk_cfg = (
        cfg.risk
        if not strat.is_benchmark
        else RiskConfig(max_position_weight=1.0, kill_switch_file=cfg.risk.kill_switch_file)
    )
    risk = RiskEngine(risk_cfg, audit, passthrough=strat.is_benchmark)
    market = USMarket(CostModel(cfg.costs), cfg.settlement_days)
    engine = BacktestEngine(strat, data, market, risk, cfg.initial_capital, audit)
    try:
        return engine.run(start, end)
    finally:
        audit.close()


def run_backtest(cfg: RunConfig, refresh: bool = False, out_root: Path | None = None) -> Path:
    start = pd.Timestamp(cfg.start)
    end = pd.Timestamp(cfg.end) if cfg.end else pd.Timestamp.today().normalize()
    provider = make_provider(cfg.provider)
    store = BarStore(provider, cfg.data_dir)
    bars, issues = store.load(
        _symbols(cfg), start - pd.Timedelta(days=WARMUP_CALENDAR_DAYS), end, refresh=refresh
    )
    data = MarketData(bars)

    run_id = f"{pd.Timestamp.now():%Y%m%d-%H%M%S}_{cfg.name}_{cfg.fingerprint()}"
    out = (out_root or Path(cfg.runs_dir)) / run_id
    out.mkdir(parents=True, exist_ok=True)

    results = [_run_one(cfg.strategy, cfg.universe, cfg, data, start, end, out / "audit.jsonl")]
    results += [_run_one(b, cfg.universe, cfg, data, start, end, None) for b in cfg.benchmarks]

    spy = next((r.equity for r in results[1:] if r.strategy_id.lower().startswith("spy")), None)
    metrics = {
        r.strategy_id: performance(
            r.equity, r.gross_exposure, r.closed_trades, benchmark=spy if r is not None else None
        )
        for r in results
    }
    data_warning = (
        f"Data source: {provider.caps.name}. "
        + provider.caps.notes
        + (" Does not include delisted securities." if not provider.caps.includes_delisted else "")
    )
    meta = {
        "run_id": run_id,
        "period": f"{results[0].equity.index[0].date()} to {results[0].equity.index[-1].date()}",
        "provider": provider.caps.name,
        "git_commit": _git_commit(),
        "config_fingerprint": cfg.fingerprint(),
        "initial_capital": f"${cfg.initial_capital:,.0f}",
        "costs": json.dumps(asdict(cfg.costs)),
        "risk": json.dumps(asdict(cfg.risk)),
        "data_warning": data_warning,
    }
    issue_txt = [
        f"{i.symbol} {i.date.date() if i.date is not None else ''} {i.kind} {i.detail}"
        for i in issues
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
