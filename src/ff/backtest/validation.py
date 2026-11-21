"""Phase 2 validation: is a backtest result real, or luck and tuning?

Steps, all net of costs and on the same engine as the plain backtest:

1. Run the strategy as configured (literature defaults) and the benchmarks.
2. Parameter sweep over a small grid. Every parameter set is logged in the trial
   registry, which feeds the Deflated Sharpe Ratio with the honest trial count.
3. Probability of Backtest Overfitting (CSCV) across the grid.
4. Walk-forward: each year, pick the grid member with the best Sharpe over the
   previous ``train_years`` and use only its next-year returns. The stitched
   out-of-sample curve is what a tuner would really have earned.
5. Cost stress: the configured strategy with costs multiplied.
6. Fair comparisons: the strategy run at the same volatility as 60/40, and a
   monthly-rebalanced blend of the strategy with 60/40.
7. Locked hold-out: sessions from ``holdout_start`` are excluded unless opened
   explicitly, and each opening is recorded.

Walk-forward note: returns are stitched from full-period runs of each grid
member, so a test year starts from that member's positions rather than from
cash. With monthly rebalancing this changes little and is standard practice.
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ff.analytics.metrics import daily_rf, performance
from ff.analytics.overfitting import PBOResult, deflated_sharpe, pbo_cscv, sharpe_stats
from ff.backtest.engine import BacktestResult
from ff.backtest.registry import TrialRegistry
from ff.backtest.runner import (
    Prepared,
    _git_commit,
    data_warning,
    prepare,
    run_id_for,
    run_strategy,
)
from ff.core.config import RunConfig, StrategyConfig

log = logging.getLogger(__name__)


@dataclass
class ValidationSettings:
    grid: dict[str, list[Any]] = field(default_factory=dict)
    train_years: int = 5
    test_years: int = 1
    pbo_blocks: int = 16
    cost_stress: float = 2.0
    blend_with: str = "sixty_forty"
    blend_weight: float = 0.5
    # The 1% per-trade risk cap usually binds before a higher vol target does, so the
    # volatility-matched variant may relax it (the brief allows 1-2%).
    vol_matched_max_risk_per_trade: float = 0.02
    min_dsr: float = 0.95
    max_pbo: float = 0.5

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ValidationSettings:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class ValidationResult:
    cfg: RunConfig
    settings: ValidationSettings
    period: tuple[pd.Timestamp, pd.Timestamp]
    holdout: str
    base: BacktestResult
    benchmarks: dict[str, BacktestResult]
    grid: dict[str, dict[str, Any]]  # trial id -> params
    grid_returns: pd.DataFrame  # excess daily returns per trial
    grid_metrics: dict[str, dict[str, float]]
    walk_forward: pd.Series  # stitched OOS daily returns (total, not excess)
    wf_choices: list[dict[str, Any]]
    wf_benchmarks: dict[str, pd.Series]  # benchmark equity over the OOS span
    cost_stressed: BacktestResult
    vol_matched: BacktestResult | None
    vol_matched_target: float | None
    blend: pd.Series | None
    metrics: dict[str, dict[str, float]]
    dsr: float
    sr0_annual: float
    n_trials: int
    pbo: PBOResult | None
    checks: list[Check]
    cash_rate: pd.Series
    meta: dict[str, Any]


def _grid(base: dict[str, Any], grid: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    if not grid:
        return {"base": dict(base)}
    keys = sorted(grid)
    out: dict[str, dict[str, Any]] = {}
    for values in itertools.product(*(grid[k] for k in keys)):
        p = dict(base)
        p.update(dict(zip(keys, values, strict=True)))
        out["_".join(f"{k}={v}" for k, v in zip(keys, values, strict=True))] = p
    return out


def blend_equity(a: pd.Series, b: pd.Series, weight_a: float) -> pd.Series:
    """Monthly-rebalanced mix of two equity curves (no extra trading costs)."""
    idx = a.index.intersection(b.index)
    ra, rb = a.reindex(idx).pct_change().fillna(0), b.reindex(idx).pct_change().fillna(0)
    va, vb = weight_a, 1 - weight_a
    out = []
    for i, d in enumerate(idx):
        if i > 0 and d.month != idx[i - 1].month:
            total = va + vb
            va, vb = total * weight_a, total * (1 - weight_a)
        va *= 1 + ra.iloc[i]
        vb *= 1 + rb.iloc[i]
        out.append(va + vb)
    blended: pd.Series = pd.Series(out, index=idx) * float(a.iloc[0])
    return blended


def walk_forward(
    total_returns: pd.DataFrame, excess: pd.DataFrame, train_years: int, test_years: int
) -> tuple[pd.Series, list[dict[str, Any]]]:
    idx = total_returns.index
    first_test = idx[0] + pd.DateOffset(years=train_years)
    pieces: list[pd.Series] = []
    choices: list[dict[str, Any]] = []
    t0 = first_test
    while t0 <= idx[-1]:
        t1 = t0 + pd.DateOffset(years=test_years)
        train = excess[(idx >= t0 - pd.DateOffset(years=train_years)) & (idx < t0)]
        test_mask = (idx >= t0) & (idx < t1)
        if not test_mask.any():
            break
        sr = train.mean() / train.std(ddof=1)
        best = str(sr.idxmax())
        pieces.append(total_returns.loc[test_mask, best])
        choices.append(
            {
                "test_start": t0.date(),
                "test_end": idx[test_mask][-1].date(),
                "chosen": best,
                "train_sharpe": float(sr[best] * np.sqrt(252)),
            }
        )
        t0 = t1
    return (pd.concat(pieces) if pieces else pd.Series(dtype=float)), choices


def _excess(r: pd.Series, rate: pd.Series) -> pd.Series:
    out: pd.Series = r - daily_rf(rate, r.index)
    return out


def run_validation(
    cfg: RunConfig, open_holdout: bool = False, refresh: bool = False, out_root: Path | None = None
) -> tuple[Path, ValidationResult]:
    vs = ValidationSettings.from_dict(cfg.validation)
    prep: Prepared = prepare(cfg, refresh)
    registry = TrialRegistry(Path(cfg.runs_dir) / "trial_registry.jsonl")
    kind = cfg.strategy.kind
    start, end = prep.start, prep.end
    holdout_note = "none configured"
    if cfg.holdout_start:
        h = pd.Timestamp(cfg.holdout_start)
        if open_holdout:
            prior = registry.holdout_openings(kind)
            registry.record_holdout_opened(kind, cfg.strategy.params, cfg.holdout_start)
            holdout_note = (
                f"OPENED (included from {h.date()}); "
                f"opened {len(prior)} time(s) before for this strategy"
            )
        else:
            end = min(end, h - pd.Timedelta(days=1))
            holdout_note = f"locked: sessions from {h.date()} excluded"

    benchmarks = {b.id: run_strategy(b, cfg, prep, start, end) for b in cfg.benchmarks}
    base = run_strategy(cfg.strategy, cfg, prep, start, end)

    grid = _grid(cfg.strategy.params, vs.grid)
    total: dict[str, pd.Series] = {}
    grid_metrics: dict[str, dict[str, float]] = {}
    for tid, params in grid.items():
        res = (
            base
            if params == cfg.strategy.params
            else run_strategy(StrategyConfig(tid, kind, params), cfg, prep, start, end)
        )
        r = res.equity.pct_change().dropna()
        total[tid] = r
        st = sharpe_stats(_excess(r, prep.cash_rate))
        grid_metrics[tid] = performance(
            res.equity, res.gross_exposure, res.closed_trades, rf_annual=prep.cash_rate
        )
        registry.record_trial(
            kind,
            params,
            sharpe_daily=st.sharpe,
            n=st.n,
            period=f"{start.date()}..{end.date()}",
            provider=prep.provider.name,
            git_commit=_git_commit(),
        )
        log.info("trial %s: Sharpe %.2f", tid, grid_metrics[tid]["sharpe"])
    total_df = pd.DataFrame(total).dropna()
    excess_df = total_df.sub(daily_rf(prep.cash_rate, total_df.index), axis=0)

    base_stats = sharpe_stats(_excess(base.equity.pct_change().dropna(), prep.cash_rate))
    n_trials = max(registry.n_trials(kind), len(grid))
    dsr, sr0 = deflated_sharpe(base_stats, registry.trial_sharpes(kind), n_trials)
    pbo = pbo_cscv(excess_df, vs.pbo_blocks) if excess_df.shape[1] >= 2 else None

    wf, choices = walk_forward(total_df, excess_df, vs.train_years, vs.test_years)
    wf_bench: dict[str, pd.Series] = {}
    if len(wf):
        for bid, b in benchmarks.items():
            eq = b.equity.loc[wf.index[0] - pd.Timedelta(days=7) : wf.index[-1]]
            wf_bench[bid] = eq / eq.iloc[0]

    stressed = run_strategy(
        StrategyConfig(f"{cfg.strategy.id}_cost_x{vs.cost_stress:g}", kind, cfg.strategy.params),
        cfg,
        prep,
        start,
        end,
        cost_multiplier=vs.cost_stress,
    )

    vol_matched = None
    vm_target: float | None = None
    blend: pd.Series | None = None
    partner = benchmarks.get(vs.blend_with)
    if partner is not None:
        vm_target = round(float(partner.equity.pct_change().std() * np.sqrt(252)), 3)
        if "target_vol" in cfg.strategy.params or kind == "trend_etf":
            p = dict(cfg.strategy.params, target_vol=vm_target)
            mr = vs.vol_matched_max_risk_per_trade
            vol_matched = run_strategy(
                StrategyConfig(f"{cfg.strategy.id}_vol{vm_target:.0%}_risk{mr:.0%}", kind, p),
                cfg,
                prep,
                start,
                end,
                risk_override={"max_risk_per_trade": mr},
            )
        blend = blend_equity(base.equity, partner.equity, vs.blend_weight)

    spy = next((b.equity for k, b in benchmarks.items() if k.lower().startswith("spy")), None)

    def perf(eq: pd.Series, res: BacktestResult | None = None) -> dict[str, float]:
        return performance(
            eq,
            res.gross_exposure if res else None,
            res.closed_trades if res else (),
            benchmark=spy,
            rf_annual=prep.cash_rate,
        )

    metrics: dict[str, dict[str, float]] = {cfg.strategy.id: perf(base.equity, base)}
    metrics.update({k: perf(b.equity, b) for k, b in benchmarks.items()})
    metrics[stressed.strategy_id] = perf(stressed.equity, stressed)
    if vol_matched is not None:
        metrics[vol_matched.strategy_id] = perf(vol_matched.equity, vol_matched)
    if blend is not None:
        metrics[f"blend_{vs.blend_weight:.0%}_{vs.blend_with}"] = perf(blend)
    if len(wf):
        wf_eq = (1 + wf).cumprod() * cfg.initial_capital
        metrics["walk_forward_oos"] = perf(wf_eq)
        for k, eq in wf_bench.items():
            metrics[f"{k}_same_oos_period"] = perf(eq)

    checks = _checks(cfg, vs, metrics, dsr, pbo, blend is not None, len(wf) > 0)
    run_id = run_id_for(cfg, "validate")
    meta = {
        "run_id": run_id,
        "period": f"{start.date()} to {end.date()}",
        "holdout": holdout_note,
        "provider": prep.provider.name,
        "git_commit": _git_commit(),
        "config_fingerprint": cfg.fingerprint(),
        "grid_size": len(grid),
        "trials_in_registry": n_trials,
        "data_warning": data_warning(prep.provider),
        "settings": json.dumps(asdict(vs)),
    }
    result = ValidationResult(
        cfg,
        vs,
        (start, end),
        holdout_note,
        base,
        benchmarks,
        grid,
        excess_df,
        grid_metrics,
        wf,
        choices,
        wf_bench,
        stressed,
        vol_matched,
        vm_target,
        blend,
        metrics,
        dsr,
        sr0 * np.sqrt(252),
        n_trials,
        pbo,
        checks,
        prep.cash_rate,
        meta,
    )
    out = (out_root or Path(cfg.runs_dir)) / run_id
    out.mkdir(parents=True, exist_ok=True)
    from ff.analytics.validation_report import render_validation

    (out / "validation.html").write_text(render_validation(result), encoding="utf-8")
    (out / "validation.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "metrics": metrics,
                "grid_metrics": grid_metrics,
                "deflated_sharpe": dsr,
                "sr0_annual": sr0 * np.sqrt(252),
                "n_trials": n_trials,
                "pbo": pbo.pbo if pbo else None,
                "walk_forward_choices": choices,
                "checks": [asdict(c) for c in checks],
            },
            indent=2,
            default=str,
        )
    )
    return out, result


def _checks(
    cfg: RunConfig,
    vs: ValidationSettings,
    m: dict[str, dict[str, float]],
    dsr: float,
    pbo: PBOResult | None,
    has_blend: bool,
    has_wf: bool,
) -> list[Check]:
    sid = cfg.strategy.id
    out: list[Check] = []
    out.append(
        Check(
            "Deflated Sharpe",
            bool(dsr >= vs.min_dsr),
            f"{dsr:.2f} (need ≥ {vs.min_dsr:.2f}): chance the Sharpe is real after "
            "counting every parameter set tried",
        )
    )
    if pbo is not None:
        out.append(
            Check(
                "Overfitting (PBO)",
                pbo.pbo < vs.max_pbo,
                f"{pbo.pbo:.0%} (need < {vs.max_pbo:.0%}): how often the best in-sample "
                "setting lands in the bottom half out of sample",
            )
        )
    if has_wf:
        wf = m["walk_forward_oos"]
        out.append(
            Check(
                "Walk-forward out of sample",
                wf["sharpe"] > 0,
                f"Sharpe {wf['sharpe']:.2f}, CAGR {wf['cagr']:.1%} when settings are "
                "re-picked each year using only past data",
            )
        )
    stressed = next(k for k in m if k.startswith(f"{sid}_cost_x"))
    out.append(
        Check(
            f"Survives {vs.cost_stress:g}x costs",
            m[stressed]["sharpe"] > 0,
            f"Sharpe {m[stressed]['sharpe']:.2f} vs {m[sid]['sharpe']:.2f} at base cost",
        )
    )
    if has_blend and vs.blend_with in m:
        bk = f"blend_{vs.blend_weight:.0%}_{vs.blend_with}"
        better = m[bk]["sharpe"] > m[vs.blend_with]["sharpe"]
        out.append(
            Check(
                f"Improves {vs.blend_with} as a {vs.blend_weight:.0%} blend",
                better,
                f"blend Sharpe {m[bk]['sharpe']:.2f} vs {m[vs.blend_with]['sharpe']:.2f};"
                f" max drawdown {m[bk]['max_drawdown']:.1%} vs "
                f"{m[vs.blend_with]['max_drawdown']:.1%}",
            )
        )
    return out
