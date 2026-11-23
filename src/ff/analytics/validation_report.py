"""Self-contained HTML report for a validation run."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import PercentFormatter

from ff.analytics.metrics import calendar_year_returns, drawdown
from ff.analytics.report import COLORS, ORDER, STYLE, _fmt, _png

if TYPE_CHECKING:
    from ff.backtest.validation import ValidationResult


def _metrics_table(metrics: dict[str, dict[str, float]], cols: list[str]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    rows = []
    for k in ORDER:
        if any(k in metrics[c] for c in cols):
            cells = "".join(f"<td>{_fmt(k, metrics[c].get(k))}</td>" for c in cols)
            rows.append(f"<tr><th>{k.replace('_', ' ')}</th>{cells}</tr>")
    return f"<div class='wrap'><table><tr><th></th>{head}</tr>{''.join(rows)}</table></div>"


def _curves(series: dict[str, pd.Series], title: str, log: bool = True) -> str:
    fig, ax = plt.subplots(figsize=(10, 4))
    for (name, eq), c in zip(series.items(), COLORS * 3, strict=False):
        (eq / eq.iloc[0]).plot(
            ax=ax, label=name, color=c, lw=1.8 if name == next(iter(series)) else 1.2
        )
    if log:
        ax.set_yscale("log")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8)
    return _png(fig)


def _drawdowns(series: dict[str, pd.Series]) -> str:
    fig, ax = plt.subplots(figsize=(10, 2.6))
    for (name, eq), c in zip(series.items(), COLORS * 3, strict=False):
        drawdown(eq).plot(ax=ax, label=name, color=c, lw=1.1)
    ax.set_title("Drawdown from peak")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8)
    return _png(fig)


def _grid_chart(gm: dict[str, dict[str, float]], base_key: str | None) -> str:
    items = sorted(gm.items(), key=lambda kv: kv[1]["sharpe"])
    fig, ax = plt.subplots(figsize=(10, max(2.5, 0.28 * len(items))))
    names = [k for k, _ in items]
    vals = [v["sharpe"] for _, v in items]
    colors = ["#2563eb" if k == base_key else "#9ca3af" for k in names]
    ax.barh(names, vals, color=colors)
    ax.axvline(0, color="#6b7280", lw=0.8)
    ax.set_title("Sharpe of every parameter set tried (blue = configured default)")
    ax.tick_params(axis="y", labelsize=7)
    ax.grid(alpha=0.3, axis="x")
    return _png(fig)


def _rolling_sharpe(series: dict[str, pd.Series], rate: pd.Series) -> str:
    from ff.analytics.metrics import daily_rf

    fig, ax = plt.subplots(figsize=(10, 2.8))
    for (name, eq), c in zip(series.items(), COLORS * 3, strict=False):
        r = eq.pct_change().dropna()
        ex = r - daily_rf(rate, r.index)
        rs = ex.rolling(756).mean() / ex.rolling(756).std() * np.sqrt(252)
        rs.plot(ax=ax, label=name, color=c, lw=1.1)
    ax.axhline(0, color="#6b7280", lw=0.8)
    ax.set_title("Rolling 3-year Sharpe")
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8)
    return _png(fig)


def render_validation(v: ValidationResult) -> str:
    sid = v.cfg.strategy.id
    blend_key = next((k for k in v.metrics if k.startswith("blend_")), None)
    main_series: dict[str, pd.Series] = {sid: v.base.equity}
    main_series.update({k: b.equity for k, b in v.benchmarks.items()})
    if v.blend is not None and blend_key:
        main_series[blend_key] = v.blend
    if v.vol_matched is not None:
        main_series[v.vol_matched.strategy_id] = v.vol_matched.equity

    passed = sum(c.passed for c in v.checks)
    verdict = (
        "Passes every check. Still only paper evidence: next is live paper trading."
        if passed == len(v.checks)
        else f"Passes {passed} of {len(v.checks)} checks. Not ready to rely on."
    )
    checks = "".join(
        f"<tr><td>{'✅' if c.passed else '❌'}</td><th>{html.escape(c.name)}</th>"
        f"<td style='text-align:left'>{html.escape(c.detail)}</td></tr>"
        for c in v.checks
    )

    main_cols = [
        sid,
        *v.benchmarks,
        *[k for k in v.metrics if k.startswith(f"{sid}_") or k.startswith("blend_")],
    ]
    main_cols = list(dict.fromkeys(c for c in main_cols if c in v.metrics))
    wf_cols = [k for k in v.metrics if k == "walk_forward_oos" or k.endswith("_same_oos_period")]

    years = pd.DataFrame({k: calendar_year_returns(s) for k, s in main_series.items()})
    yhead = "".join(f"<th>{html.escape(k)}</th>" for k in years.columns)
    yrows = "".join(
        f"<tr><th>{y}</th>"
        + "".join(
            f"<td class='{'neg' if val < 0 else ''}'>{val:.1%}</td>"
            if pd.notna(val)
            else "<td>–</td>"
            for val in years.loc[y]
        )
        + "</tr>"
        for y in years.index
    )

    wf_html = ""
    if len(v.walk_forward):
        wf_eq = (1 + v.walk_forward).cumprod()
        wf_series = {"walk_forward_oos": wf_eq, **v.wf_benchmarks}
        wf_png = _curves(wf_series, "Walk-forward vs benchmarks, same period")
        choice_rows = "".join(
            f"<tr><td>{c['test_start']}</td><td>{c['test_end']}</td>"
            f"<td style='text-align:left'>{html.escape(c['chosen'])}</td>"
            f"<td>{c['train_sharpe']:.2f}</td></tr>"
            for c in v.wf_choices
        )
        wf_html = f"""<h2>Walk-forward (out of sample)</h2>
<p class="muted">Each year uses the setting with the best Sharpe over the previous
{v.settings.train_years} years, then is scored only on the following year.</p>
<img alt="walk-forward" src="data:image/png;base64,{wf_png}">
{_metrics_table(v.metrics, wf_cols)}
<details><summary>Setting picked each year</summary><div class="wrap"><table>
<tr><th>Test from</th><th>to</th><th>Chosen setting</th><th>Train Sharpe</th></tr>{choice_rows}
</table></div></details>"""

    gm_rows = "".join(
        f"<tr><td style='text-align:left'>{html.escape(k)}</td><td>{m['cagr']:.1%}</td>"
        f"<td>{m['volatility']:.1%}</td><td>{m['sharpe']:.2f}</td><td>{m['max_drawdown']:.1%}</td>"
        f"</tr>"
        for k, m in sorted(v.grid_metrics.items(), key=lambda kv: -kv[1]["sharpe"])
    )
    base_key = next((k for k, p in v.grid.items() if p == v.cfg.strategy.params), None)
    sharpes = np.array([m["sharpe"] for m in v.grid_metrics.values()])
    plateau = (
        f"{(sharpes > 0).mean():.0%} of settings have a positive Sharpe; range "
        f"{sharpes.min():.2f} to {sharpes.max():.2f}, median {np.median(sharpes):.2f}."
    )
    pbo_txt = (
        f"{v.pbo.pbo:.0%} across {v.pbo.n_splits:,} splits of {v.pbo.n_strategies} settings"
        if v.pbo
        else "not computed (needs 2+ settings)"
    )
    meta_rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td style='text-align:left'>"
        f"{html.escape(str(val))}</td></tr>"
        for k, val in v.meta.items()
    )
    vm_note = (
        f"<p class='muted'>The volatility-matched version targets {v.vol_matched_target:.0%}"
        f" (60/40's realized volatility) and allows "
        f"{v.settings.vol_matched_max_risk_per_trade:.0%} risk per trade instead of "
        f"{v.cfg.risk.max_risk_per_trade:.0%}. A cash account still caps it at 100% "
        "invested, so it may not reach the target.</p>"
        if v.vol_matched is not None
        else ""
    )
    rs = v.base.risk_state
    n_dec = len(v.base.audit.of_kind("decision"))
    caps = sum(d.rule == "max_risk_per_trade" for d in rs.decisions)
    vm_note += (
        f"<p class='muted'>Risk caps on the configured strategy: the per-trade risk cap "
        f"shrank {caps} of {n_dec} position decisions; gross exposure averaged "
        f"{v.base.gross_exposure.mean():.0%}.</p>"
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Validation: {html.escape(sid)}</title><style>{STYLE}</style></head><body>
<h1>Validation: {html.escape(sid)}</h1>
<div class="muted">{html.escape(v.meta["period"])} · net of costs · cash earns T-bills ·
Sharpe is over the T-bill rate · hold-out {html.escape(v.holdout)}</div>
<p class="warn"><b>{html.escape(verdict)}</b><br>Simulation only. Past performance does not
predict future results. {html.escape(v.meta["data_warning"])}</p>
<h2>Checks</h2><div class="wrap"><table>{checks}</table></div>
<p class="muted">Deflated Sharpe uses {v.n_trials} trials from the registry
({html.escape(str(v.meta.get("trials_pooled_from", "")))}); luck threshold
{v.sr0_annual:.2f} annualized Sharpe. PBO: {pbo_txt}.</p>
<h2>Strategy vs benchmarks, blend and variants</h2>
<img alt="equity" src="data:image/png;base64,{_curves(main_series, "Growth of $1 (log scale)")}">
<img alt="drawdowns" src="data:image/png;base64,{_drawdowns(main_series)}">
<img alt="rolling sharpe" src="data:image/png;base64,{_rolling_sharpe(main_series, v.cash_rate)}">
{vm_note}
{_metrics_table(v.metrics, main_cols)}
<h2>Calendar-year returns</h2><div class="wrap"><table><tr><th></th>{yhead}</tr>{yrows}
</table></div>
{wf_html}
<h2>Parameter sweep</h2><p class="muted">{html.escape(plateau)} A robust strategy works across
a plateau of settings, not at one lucky point.</p>
<img alt="sweep" src="data:image/png;base64,{_grid_chart(v.grid_metrics, base_key)}">
<details><summary>All settings</summary><div class="wrap"><table><tr><th>Setting</th>
<th>CAGR</th><th>Vol</th><th>Sharpe</th><th>Max DD</th></tr>{gm_rows}</table></div></details>
<h2>Run details</h2><div class="wrap"><table>{meta_rows}</table></div>
</body></html>"""
