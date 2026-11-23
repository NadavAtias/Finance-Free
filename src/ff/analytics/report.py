"""Self-contained HTML backtest report (charts embedded as PNG)."""

from __future__ import annotations

import base64
import html
import io
import math
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from ff.analytics.metrics import calendar_year_returns, drawdown
from ff.backtest.engine import BacktestResult

PCT = {
    "total_return",
    "cagr",
    "volatility",
    "max_drawdown",
    "best_month",
    "worst_month",
    "avg_exposure",
    "win_rate",
}
ORDER = [
    "total_return",
    "cagr",
    "volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_dd_duration_days",
    "calmar",
    "best_month",
    "worst_month",
    "avg_exposure",
    "beta_vs_benchmark",
    "correlation_vs_benchmark",
    "closed_trades",
    "win_rate",
    "profit_factor",
    "avg_win",
    "avg_loss",
]
COLORS = ["#2563eb", "#9ca3af", "#d97706", "#059669", "#7c3aed"]

STYLE = """
:root{--bg:#fff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--neg:#b91c1c;--card:#f9fafb}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f17;--fg:#e5e7eb;--muted:#9ca3af;
--line:#1f2937;--neg:#f87171;--card:#111827} img{filter:invert(.92) hue-rotate(180deg)}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;
padding:24px 16px;max-width:1100px;margin-inline:auto}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted)} .warn{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 14px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right;vertical-align:top}
th:first-child,td:first-child{text-align:left} .neg{color:var(--neg)}
.wrap{overflow-x:auto} img{max-width:100%} td:last-child{text-align:left}
"""


def _fmt(key: str, v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if key in PCT:
        return f"{v:.1%}"
    if key in {"closed_trades", "max_dd_duration_days"}:
        return f"{v:,.0f}"
    if key in {"avg_win", "avg_loss"}:
        return f"${v:,.0f}"
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}"


def _png(fig: Any) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _charts(results: list[BacktestResult]) -> tuple[str, str]:
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for r, c in zip(results, COLORS, strict=False):
        (r.equity / r.equity.iloc[0]).plot(
            ax=ax, label=r.strategy_id, color=c, lw=2 if r is results[0] else 1.3
        )
    ax.set_yscale("log")
    ax.set_title("Growth of $1 (log scale, net of costs)")
    ax.grid(alpha=0.3)
    ax.legend(frameon=False)
    eq_png = _png(fig)
    fig, ax = plt.subplots(figsize=(10, 2.8))
    for r, c in zip(results, COLORS, strict=False):
        drawdown(r.equity).plot(ax=ax, label=r.strategy_id, color=c, lw=1.2)
    ax.set_title("Drawdown from peak")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(alpha=0.3)
    ax.legend(frameon=False)
    return eq_png, _png(fig)


def render(
    results: list[BacktestResult],
    metrics: dict[str, dict[str, float]],
    meta: dict[str, Any],
    data_issues: list[str],
) -> str:
    eq_png, dd_png = _charts(results)
    main = results[0]
    head = "".join(f"<th>{html.escape(r.strategy_id)}</th>" for r in results)
    rows = []
    for k in ORDER:
        if any(k in metrics[r.strategy_id] for r in results):
            cells = "".join(f"<td>{_fmt(k, metrics[r.strategy_id].get(k))}</td>" for r in results)
            rows.append(f"<tr><th>{k.replace('_', ' ')}</th>{cells}</tr>")
    years = pd.DataFrame({r.strategy_id: calendar_year_returns(r.equity) for r in results})
    yrows = "".join(
        f"<tr><th>{y}</th>"
        + "".join(f"<td class='{'neg' if v < 0 else ''}'>{v:.1%}</td>" for v in years.loc[y])
        + "</tr>"
        for y in years.index
    )
    recs = "".join(
        f"<tr><td>{html.escape(i.symbol)}</td><td>{i.target_weight:.1%}</td>"
        f"<td>{'' if i.stop_price is None else f'{i.stop_price:.2f}'}</td>"
        f"<td>{html.escape(i.rationale.as_text())}</td></tr>"
        for i in sorted(main.recommendations, key=lambda x: -x.target_weight)
    )
    trades = "".join(
        f"<tr><td>{html.escape(t.symbol)}</td><td>{t.entry_date.date() if t.entry_date else ''}"
        f"</td><td>{t.exit_date.date()}</td><td>{t.quantity}</td><td>{t.entry_price:.2f}</td>"
        f"<td>{t.exit_price:.2f}</td><td class='{'neg' if t.pnl < 0 else ''}'>{t.pnl:,.0f}</td>"
        f"<td>{html.escape(t.reason)}</td></tr>"
        for t in main.closed_trades[-60:][::-1]
    )
    rs = main.risk_state
    risk_items = [
        f"Drawdown breaker trips: {rs.breaker_trips}",
        f"Daily loss limit hits: {rs.daily_limit_hits}",
        f"Risk resizes/rejections: {len(rs.decisions)}",
        f"Good-faith violations (cash account): {main.good_faith_violations}",
        f"Commissions paid: ${main.commissions:,.0f}",
        f"Annual turnover: {main.turnover_annual:.0%}",
        f"Interest earned on idle cash: ${main.interest_earned:,.0f}",
    ]
    meta_rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in meta.items()
    )
    issues = "".join(f"<li>{html.escape(x)}</li>" for x in data_issues[:50]) or "<li>None</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest: {html.escape(main.strategy_id)}</title>
<style>{STYLE}</style></head><body>
<h1>{html.escape(main.strategy_id)}</h1>
<div class="muted">{html.escape(str(meta.get("period", "")))} · net of costs ·
run {html.escape(str(meta.get("run_id", "")))}</div>
<p class="warn">Simulation only. Past performance does not predict future results.
{html.escape(str(meta.get("data_warning", "")))}</p>
<h2>Equity and drawdown</h2>
<img alt="equity curves" src="data:image/png;base64,{eq_png}">
<img alt="drawdowns" src="data:image/png;base64,{dd_png}">
<h2>Metrics</h2><div class="wrap"><table><tr><th></th>{head}</tr>{"".join(rows)}</table></div>
<h2>Calendar-year returns</h2><div class="wrap"><table><tr><th></th>{head}</tr>{yrows}
</table></div>
<h2>Latest recommendations ({
        main.recommendation_date.date() if main.recommendation_date else "–"
    })</h2>
<p class="muted">Target weights to place at the next session's open.</p>
<div class="wrap"><table><tr><th>Symbol</th><th>Target</th><th>Stop</th><th>Rationale</th></tr>
{recs}</table></div>
<h2>Risk and costs</h2><ul>{"".join(f"<li>{x}</li>" for x in risk_items)}</ul>
<h2>Recent closed trades (exits)</h2><div class="wrap"><table><tr><th>Symbol</th>
<th>Entry</th><th>Exit</th><th>Qty</th><th>Avg cost</th><th>Exit px</th><th>P&amp;L</th>
<th>Reason</th></tr>{trades}</table></div>
<h2>Data quality flags</h2><ul>{issues}</ul>
<h2>Run details</h2><div class="wrap"><table>{meta_rows}</table></div>
</body></html>"""
