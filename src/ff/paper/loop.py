"""Daily paper-trading loop.

``run_paper`` is idempotent: it processes every *final* session (bar complete,
close at least 30 minutes old) after the last one it recorded, then stops. Run it
once after each US close (the daemon does), or by hand any time.

Per sleeve, the same ``BacktestEngine.step`` used in backtests advances one
session at a time; its full state (ledger, open orders, risk state, strategy
memory) is pickled into SQLite between runs, without the market data, which is
re-attached each run.

Prices are split and dividend adjusted (Yahoo). When a dividend or split goes
ex, the provider rescales past prices; the loop detects that on held positions
by comparing the close it recorded with the freshly downloaded one, credits the
dividend in cash (or rescales shares for a split) and moves stops by the same
factor. Nothing already in the journal is rewritten.
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from typing import Any

import numpy as np
import pandas as pd

from ff.backtest.engine import BacktestEngine, StepOutput
from ff.backtest.runner import Prepared, prepare
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig
from ff.core.types import Intent, OrderType, Side, reserve_order_ids
from ff.markets.calendar import ET, is_month_end, last_final_session
from ff.markets.us import CostModel, USMarket
from ff.notify import telegram
from ff.paper.config import PaperConfig, PortfolioConfig, SleeveConfig
from ff.paper.db import PaperDB
from ff.risk.engine import RiskEngine
from ff.strategies.base import build_strategy

log = logging.getLogger(__name__)

DEFAULT_ALERT_SETTINGS: dict[str, Any] = {
    "telegram": {"enabled": True, "chat_id": "", "bot_configured": False},
    "email": {"enabled": False, "address": ""},
    "events": {
        "recommendations": True,
        "fills": False,
        "stop_near": True,
        "risk_limit": True,
        "circuit_breaker": True,
        "kill_switch": True,
        "strategy_status": True,
        "daily_summary": True,
    },
    "min_severity": "warning",
    "quiet_hours": {"enabled": False, "from": "22:00", "to": "07:00"},
}
STOP_NEAR = 0.02  # alert when the close is within 2% of a stop


def ts_et(d: pd.Timestamp, hh: int, mm: int) -> str:
    """ISO UTC timestamp for a wall-clock time in New York on session ``d``."""
    local = datetime.combine(pd.Timestamp(d).date(), time(hh, mm), tzinfo=ET)
    return pd.Timestamp(local).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SleeveRun:
    portfolio: str
    sleeve: str
    sessions: list[pd.Timestamp] = field(default_factory=list)
    new_recs: list[dict[str, Any]] = field(default_factory=list)
    stops_hit: list[str] = field(default_factory=list)
    equity: float | None = None


@dataclass
class PaperRunResult:
    last_session: pd.Timestamp | None
    sleeves: list[SleeveRun]
    summary: str = ""
    telegram: str = ""

    @property
    def new_sessions(self) -> int:
        return max((len(s.sessions) for s in self.sleeves), default=0)


# --- engine construction and persistence -----------------------------------------
def _new_engine(cfg: PaperConfig, sl: SleeveConfig, prep: Prepared) -> BacktestEngine:
    st = sl.strategy
    params = dict(st.params)
    params.pop("symbols", None)
    strat = build_strategy(st.kind, sl.id, sl.universe, params)
    audit = AuditLog()
    ks = str(cfg.kill_switch_path)
    if sl.benchmark_rules or strat.is_benchmark:
        risk = RiskEngine(
            RiskConfig(max_position_weight=1.0, kill_switch_file=ks),
            audit,
            passthrough=True,
            reset_after_sessions=None,
        )
    else:
        # Live: the drawdown breaker is reset by hand only.
        risk = RiskEngine(replace(cfg.risk, kill_switch_file=ks), audit, reset_after_sessions=None)
    market = USMarket(CostModel(cfg.costs), cfg.settlement_days)
    return BacktestEngine(
        strat, prep.data, market, risk, sl.capital, audit, cash_rate=prep.cash_rate
    )


def _detach(engine: BacktestEngine) -> bytes:
    data, cash_rate = engine.data, engine.cash_rate
    records = engine.audit.records
    engine.data = engine.broker.data = None  # type: ignore[assignment]
    engine.cash_rate = None
    engine.audit.records = []
    try:
        return pickle.dumps(engine, protocol=pickle.HIGHEST_PROTOCOL)
    finally:
        engine.data = engine.broker.data = data
        engine.cash_rate = cash_rate
        engine.audit.records = records


def _attach(blob: bytes, prep: Prepared, cfg: PaperConfig) -> BacktestEngine:
    engine: BacktestEngine = pickle.loads(blob)
    engine.data = engine.broker.data = prep.data
    engine.cash_rate = prep.cash_rate.sort_index()
    engine.risk.cfg = replace(engine.risk.cfg, kill_switch_file=str(cfg.kill_switch_path))
    return engine


def _max_order_id(engine: BacktestEngine) -> int:
    return max((o.id for o in engine.broker.open_orders()), default=0)


# --- corporate actions -------------------------------------------------------------
def _apply_adjustments(
    engine: BacktestEngine, prep: Prepared, last: pd.Timestamp, db: PaperDB, sl: SleeveConfig
) -> None:
    for sym, recorded in list(engine._last_px.items()):
        bar = prep.data.bar(sym, last)
        if bar is None or recorded <= 0:
            continue
        fresh = float(bar["close"])
        r = fresh / recorded
        engine._last_px[sym] = fresh
        if abs(r - 1) < 1e-4:
            continue
        pos = engine.portfolio.positions.get(sym)
        qty = pos.quantity if pos else 0
        for o in engine.broker.open_orders():
            if o.symbol == sym and o.stop_price is not None:
                o.stop_price *= r
        if not qty or pos is None:
            continue
        ts = ts_et(last, 16, 1)
        if r < 0.8:  # split
            new_qty = round(qty / r)
            pos.quantity, pos.peak_qty = new_qty, max(pos.peak_qty, new_qty)
            pos.avg_cost *= r
            for o in engine.broker.open_orders():
                if o.symbol == sym and o.type is OrderType.STOP:
                    o.quantity = new_qty
            db.event(ts, "system", f"Split detected: {qty} shares became {new_qty}", sym, sl.id)
        elif r < 1:
            cash = qty * recorded * (1 - r)
            engine.portfolio.cash += cash
            db.event(
                ts,
                "fill",
                f"Dividend credited: ${cash:,.2f} on {qty} shares "
                f"({(1 - r):.2%} of price); stops moved by the same factor",
                sym,
                sl.id,
                qty=0,
                price=round(cash, 2),
            )
        else:
            db.event(
                ts,
                "system",
                f"Price history revised up {r - 1:.3%} by the data source; no cash change",
                sym,
                sl.id,
            )


# --- journal -----------------------------------------------------------------------
def _journal(db: PaperDB, recs: list[dict[str, Any]], sleeve: str, run: SleeveRun) -> None:
    for r in recs:
        k, d = r["kind"], r["date"]
        sym = str(r.get("symbol", "—"))
        if k == "decision":
            w = float(r.get("target_weight", 0.0))
            db.event(
                ts_et(d, 16, 5),
                "decision",
                f"Target {w:.1%} of sleeve. {r.get('rationale', '')}",
                sym,
                sleeve,
            )
        elif k == "risk":
            verdict = "BLOCKED" if not r.get("after") else "RESIZED"
            db.event(
                ts_et(d, 16, 5),
                "risk",
                f"{verdict} by {r['rule']}: {float(r['before']):.1%} → "
                f"{float(r['after']):.1%}. {r.get('reason', '')}",
                sym,
                sleeve,
            )
        elif k == "order":
            side = getattr(r["side"], "value", r["side"])
            if getattr(r["type"], "value", r["type"]) == OrderType.STOP.value:
                db.event(
                    ts_et(d, 16, 6),
                    "order",
                    f"Protective stop for {r['qty']} shares at {float(r['stop']):.2f}",
                    sym,
                    sleeve,
                )
            else:
                db.event(
                    ts_et(d, 16, 6),
                    "order",
                    f"Order to {str(side).lower()} {r['qty']} at the next open "
                    f"({r.get('reason', '')})",
                    sym,
                    sleeve,
                )
        elif k == "fill":
            side = str(getattr(r["side"], "value", r["side"]))
            qty = int(r["qty"])
            price = float(r["price"])
            reason = str(r.get("reason", ""))
            stop = reason.startswith("stop hit")
            when = ts_et(d, 9, 30) if not stop else ts_et(d, 12, 0)
            verb = "Bought" if side == Side.BUY.value else "Sold"
            db.event(
                when,
                "fill",
                f"{verb} {qty} at {price:.2f}" + (f" ({reason})" if stop else ""),
                sym,
                sleeve,
                qty=qty if side == Side.BUY.value else -qty,
                price=price,
            )
            if stop:
                run.stops_hit.append(f"{sym} sold {qty} at {price:.2f}")
        elif k in ("reject", "partial_cancel"):
            db.event(ts_et(d, 9, 31), "risk", f"{k.replace('_', ' ')}: {r}", sym, sleeve)
        elif k == "kill_switch":
            db.event(ts_et(d, 16, 5), "system", "Kill switch on: orders cancelled, no decisions")
        elif k in ("risk_halt", "risk_block", "risk_reset"):
            text = {
                "risk_halt": f"Drawdown breaker tripped ({float(r.get('drawdown', 0)):.1%}). "
                "Positions are sold at the next open; reset by hand",
                "risk_block": f"Daily loss limit hit ({float(r.get('day_return', 0)):.1%}): "
                "no new risk next session",
                "risk_reset": "Drawdown breaker reset",
            }[k]
            db.event(ts_et(d, 16, 2), "risk", text, "—", sleeve)
            sev = "critical" if k == "risk_halt" else "warning"
            db.alert(ts_et(d, 16, 2), sev, text.split(":")[0].split(" (")[0], text)


def _tickets(
    db: PaperDB, out: StepOutput, pf: PortfolioConfig, sl: SleeveConfig, px: dict[str, float]
) -> list[dict[str, Any]]:
    intents: dict[str, Intent] = {i.symbol: i for i in (out.approved or [])}
    made: list[dict[str, Any]] = []
    d = out.date
    for o in out.orders:
        it = intents.get(o.symbol)
        buy = o.side is Side.BUY
        sig = ""
        if it is not None:
            sig = it.rationale.summary
            uniq = {s.name: s for s in it.rationale.signals}.values()
            if uniq:
                sig += ". Numbers: " + "; ".join(
                    f"{s.note} {s.value:+.1%}" if abs(s.value) < 5 else f"{s.note} {s.value:.2f}"
                    for s in uniq
                )
        rec = {
            "id": f"rec_{d:%Y%m%d}_{sl.id}_{o.symbol}",
            "sleeve": sl.id,
            "portfolio": pf.id,
            "session": f"{d.date()}",
            "created": ts_et(d, 16, 10),
            "ticker": o.symbol,
            "direction": "BUY" if buy else "SELL",
            "shares": int(o.quantity),
            "entry": round(px.get(o.symbol, 0.0), 2),
            "stop": round(it.stop_price, 2) if (buy and it and it.stop_price) else None,
            "take_profit": it.take_profit if it else None,
            "confidence": round((it.confidence if it else 0.5) * 100),
            "horizon": "Hold for weeks to months; reviewed each month end"
            if buy
            else "Sell at the next open",
            "signals": sig or (o.reason or ""),
            "risk": (it.rationale.risk if it and it.rationale.risk else "")
            or "Price can gap through the stop overnight.",
            "invalidation": (it.rationale.invalidation if it else "") or "",
        }
        db.execute(
            "INSERT OR REPLACE INTO recommendations(id, sleeve, portfolio, session, created, "
            "ticker, direction, shares, entry, stop, take_profit, confidence, horizon, signals, "
            "risk, invalidation, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            tuple(rec[k] for k in list(rec)),
        )
        made.append(rec)
    return made


# --- snapshots for the API -----------------------------------------------------------
def _save_positions(
    db: PaperDB, engine: BacktestEngine, pf: str, sl: str, prev_px: dict[str, float]
) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM positions WHERE sleeve = ?", (sl,))
        for sym, p in engine.portfolio.positions.items():
            if not p.quantity:
                continue
            stop = engine.broker.stop_for(sym)
            c.execute(
                "INSERT INTO positions(sleeve, portfolio, ticker, shares, entry, last, prev, stop, "
                "opened) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sl,
                    pf,
                    sym,
                    p.quantity,
                    p.avg_cost,
                    engine._last_px.get(sym, p.avg_cost),
                    prev_px.get(sym),
                    stop.stop_price if stop else None,
                    f"{p.opened.date()}" if p.opened is not None else None,
                ),
            )


def _benchmark_growth(cfg: PaperConfig, prep: Prepared, last: pd.Timestamp) -> dict[str, Any]:
    start = pd.Timestamp(cfg.start)
    out: dict[str, Any] = {}
    for b in cfg.benchmarks:
        closes = pd.DataFrame({s: prep.data._upto(s, last)["close"] for s in b.weights}).dropna()
        closes = closes[closes.index >= start]
        if closes.empty:
            continue
        rets = closes.pct_change().fillna(0.0)
        w = pd.Series(b.weights) / sum(b.weights.values())
        val, hold, g = 1.0, w.copy(), []
        prev_month = None
        for dt, r in rets.iterrows():
            d = pd.Timestamp(dt)  # type: ignore[arg-type]
            if prev_month is not None and d.month != prev_month:
                hold = w * float(hold.sum())  # monthly rebalance to target
            hold = hold * (1 + r[w.index])
            val = float(hold.sum())
            g.append(val)
            prev_month = d.month
        out[b.id] = {
            "name": b.name,
            "dates": [f"{d.date()}" for d in closes.index],
            "growth": g,
        }
    return out


def _correlation(prep: Prepared, tickers: list[str], last: pd.Timestamp) -> dict[str, Any]:
    if len(tickers) < 2:
        return {
            "tickers": tickers,
            "matrix": [[1.0]] if tickers else [],
            "window": "60 trading days",
        }
    px = pd.DataFrame({t: prep.data._upto(t, last)["close"].iloc[-61:] for t in tickers}).dropna()
    c = np.corrcoef(np.diff(np.log(px.to_numpy()), axis=0), rowvar=False)
    return {
        "tickers": tickers,
        "matrix": [[round(float(x), 3) for x in row] for row in c],
        "window": "60 trading days",
    }


# --- main entry ----------------------------------------------------------------------
def run_paper(
    cfg: PaperConfig,
    now: datetime | None = None,
    refresh: bool = True,
    prep: Prepared | None = None,
    notify: bool = True,
) -> PaperRunResult:
    db = PaperDB(cfg.db_path)
    if db.get_json("alert_settings") is None:
        db.set_json("alert_settings", DEFAULT_ALERT_SETTINGS)
    prep = prep or prepare(cfg.as_run_config(), refresh=refresh)
    final = last_final_session(now)
    sessions = prep.data.sessions("SPY")
    sessions = sessions[(sessions >= pd.Timestamp(cfg.start)) & (sessions <= final)]
    runs: list[SleeveRun] = []
    last_done: pd.Timestamp | None = None

    for pf, sl in cfg.sleeves:
        run = SleeveRun(pf.id, sl.id)
        runs.append(run)
        row = db.rows("SELECT * FROM sleeve_state WHERE sleeve = ?", (sl.id,))
        if row:
            engine = _attach(row[0]["blob"], prep, cfg)
            last = pd.Timestamp(row[0]["last_session"])
            idx = int(row[0]["session_index"])
            reserve_order_ids(int(row[0]["max_order_id"]))
            _apply_adjustments(engine, prep, last, db, sl)
        else:
            engine = _new_engine(cfg, sl, prep)
            last, idx = None, -1
            db.event(
                ts_et(sessions[0], 9, 0) if len(sessions) else ts_et(pd.Timestamp(cfg.start), 9, 0),
                "system",
                f"Paper trading started with ${sl.capital:,.0f}",
                "—",
                sl.id,
            )
        todo = [d for d in sessions if last is None or d > last]
        n_trades = len(engine.portfolio.closed_trades)
        prev_px: dict[str, float] = dict(engine._last_px)
        for d in todo:
            db.execute(
                "UPDATE recommendations SET status = 'expired' "
                "WHERE sleeve = ? AND status = 'open' AND session < ?",
                (sl.id, f"{d.date()}"),
            )
            prev_px = dict(engine._last_px)
            idx += 1
            out = engine.step(d, idx, last, is_month_end(d), is_first_session=last is None)
            px = dict(engine._last_px)
            _journal(db, engine.audit.records, sl.id, run)
            engine.audit.records = []
            if out.orders:
                run.new_recs += _tickets(db, out, pf, sl, px)
            db.execute(
                "INSERT OR REPLACE INTO equity(sleeve, portfolio, date, equity, cash, gross) "
                "VALUES(?,?,?,?,?,?)",
                (
                    sl.id,
                    pf.id,
                    f"{d.date()}",
                    out.equity,
                    engine.portfolio.cash,
                    out.gross_exposure,
                ),
            )
            run.sessions.append(d)
            run.equity = out.equity
            last = d
        for t in engine.portfolio.closed_trades[n_trades:]:
            db.execute(
                "INSERT INTO trades(sleeve, portfolio, ticker, entry_date, exit_date, quantity, "
                "entry_price, exit_price, pnl, reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    sl.id,
                    pf.id,
                    t.symbol,
                    f"{t.entry_date.date()}" if t.entry_date is not None else None,
                    f"{t.exit_date.date()}",
                    t.quantity,
                    t.entry_price,
                    t.exit_price,
                    t.pnl,
                    t.reason,
                ),
            )
        if last is None:
            continue
        _save_positions(db, engine, pf.id, sl.id, prev_px)
        with db.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO sleeve_state(sleeve, portfolio, last_session, "
                "session_index, max_order_id, blob, updated) VALUES(?,?,?,?,?,?,?)",
                (
                    sl.id,
                    pf.id,
                    f"{last.date()}",
                    idx,
                    _max_order_id(engine),
                    _detach(engine),
                    pd.Timestamp.now(tz="UTC").isoformat(),
                ),
            )
        db.set_json(
            f"risk_state:{sl.id}",
            {
                "halted": engine.risk.state.halted,
                "block_new_risk": engine.risk.state.block_new_risk,
                "peak_equity": engine.risk.state.peak_equity,
                "passthrough": engine.risk.passthrough,
            },
        )
        last_done = last if last_done is None else max(last_done, last)

    result = PaperRunResult(last_done, runs)
    if last_done is None:
        result.summary = "No final session to process yet."
        return result

    held = sorted({r["ticker"] for r in db.rows("SELECT ticker FROM positions")})
    db.set_json("benchmarks", _benchmark_growth(cfg, prep, last_done))
    db.set_json("correlation", _correlation(prep, held, last_done))
    db.set_json(
        "meta",
        {
            "last_session": f"{last_done.date()}",
            "updated": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "provider": prep.provider.name,
            "data_issues": len(prep.issues),
        },
    )
    if result.new_sessions:
        _alerts_and_summary(db, cfg, prep, result, notify)
    return result


def _alerts_and_summary(
    db: PaperDB, cfg: PaperConfig, prep: Prepared, result: PaperRunResult, notify: bool
) -> None:
    assert result.last_session is not None
    d = result.last_session
    ts = ts_et(d, 16, 15)
    recs = [r for s in result.sleeves for r in s.new_recs if r["session"] == f"{d.date()}"]
    if recs:
        names = ", ".join(f"{r['direction']} {r['shares']} {r['ticker']}" for r in recs[:8])
        db.alert(
            ts,
            "info",
            f"{len(recs)} new trade idea{'s' if len(recs) > 1 else ''}",
            f"{names}{' …' if len(recs) > 8 else ''}. They are meant for the next open.",
        )
    for s in result.sleeves:
        for hit in s.stops_hit:
            db.alert(ts, "warning", "Stop-loss hit", f"{s.sleeve}: {hit}")
    near = []
    for p in db.rows("SELECT * FROM positions WHERE stop IS NOT NULL"):
        if p["last"] and (p["last"] - p["stop"]) / p["last"] < STOP_NEAR:
            near.append(f"{p['ticker']} {p['last']:.2f} vs stop {p['stop']:.2f}")
    if near:
        db.alert(ts, "warning", "Close to a stop-loss", "; ".join(near))

    # Daily summary (journal + Telegram)
    eq_rows = db.rows(
        "SELECT date, SUM(equity) AS eq FROM equity GROUP BY date ORDER BY date DESC LIMIT 2"
    )
    total = float(eq_rows[0]["eq"])
    prev = float(eq_rows[1]["eq"]) if len(eq_rows) > 1 else cfg.start_capital
    bm = db.get_json("benchmarks", {})
    spy = bm.get("spy", {}).get("growth", [1.0])[-1] - 1 if bm.get("spy") else None
    lines = [
        f"Finance Free · close of {d:%a %d %b %Y}",
        f"Paper equity ${total:,.0f} ({total / prev - 1:+.2%} today, "
        f"{total / cfg.start_capital - 1:+.2%} since start)"
        + (f"; SPY {spy:+.2%} since start" if spy is not None else ""),
    ]
    if recs:
        lines.append(f"{len(recs)} trade idea(s) for the next open:")
        lines += [
            f"  {r['direction']} {r['shares']} {r['ticker']} ~{r['entry']:.2f}"
            + (f", stop {r['stop']:.2f}" if r["stop"] else "")
            for r in recs[:10]
        ]
    else:
        lines.append("No trades for the next open.")
    for s in result.sleeves:
        lines += [f"Stop hit: {h}" for h in s.stops_hit]
    if near:
        lines.append("Near a stop: " + "; ".join(near))
    if cfg.kill_switch_path.exists():
        lines.append("KILL SWITCH IS ON: no decisions are being made.")
    lines.append("Practice money only. Past results do not promise future ones.")
    result.summary = "\n".join(lines)
    db.event(ts, "system", f"End of day: total paper money {total:,.0f} USD")

    settings = db.get_json("alert_settings", DEFAULT_ALERT_SETTINGS)
    tg = settings.get("telegram", {})
    if notify and tg.get("enabled") and settings.get("events", {}).get("daily_summary", True):
        ok, detail = telegram.send(result.summary, tg.get("chat_id") or None)
        result.telegram = detail
        if not ok:
            log.info("telegram summary not sent: %s", detail)


def reset_breaker(cfg: PaperConfig, sleeve: str) -> str:
    """Manual reset of a tripped drawdown breaker (live trading never auto-resets).
    The new high-water mark is the sleeve's current equity."""
    db = PaperDB(cfg.db_path)
    row = db.rows("SELECT * FROM sleeve_state WHERE sleeve = ?", (sleeve,))
    if not row:
        raise KeyError(f"no paper state for sleeve {sleeve!r}")
    engine: BacktestEngine = pickle.loads(row[0]["blob"])
    st = engine.risk.state
    was = st.halted
    st.halted, st.halted_at = False, None
    st.peak_equity = st.last_equity or st.peak_equity
    engine._was_halted = False
    db.execute(
        "UPDATE sleeve_state SET blob = ? WHERE sleeve = ?",
        (pickle.dumps(engine, protocol=pickle.HIGHEST_PROTOCOL), sleeve),
    )
    now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"Drawdown breaker reset by hand for {sleeve} (was {'on' if was else 'off'})"
    db.event(now, "risk", msg, "—", sleeve)
    return msg
