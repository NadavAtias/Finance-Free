"""Read-mostly FastAPI backend for the dashboard (``dashboard/README.md`` lists the
contract; ``dashboard/js/mock.js`` defines every response shape).

Everything is read from the paper ledger (SQLite) written by the daily loop. The
only writes are user actions: marking a trade idea taken or skipped, reading
alerts, alert settings, and the kill switch (a file the loop checks).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ff.markets.calendar import market_status
from ff.notify import telegram
from ff.paper.config import PaperConfig
from ff.paper.db import PaperDB
from ff.paper.loop import DEFAULT_ALERT_SETTINGS
from ff.paper.meta import STRATEGY_INFO, name_of, sector_of

MIN_SESSIONS_FOR_RATIOS = 20  # Sharpe etc. on fewer sessions is noise; shown as "—"
MIN_SESSIONS_FOR_ANNUAL = 60


def _clean(x: Any) -> Any:
    """JSON-safe: NaN/inf become null, numpy scalars become floats."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _now_iso() -> str:
    return pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def curve_metrics(eq: pd.Series, capital: float) -> dict[str, float | None]:
    """Metric names as the dashboard expects them. ``eq`` is daily equity."""
    if eq.empty or capital <= 0:
        return dict.fromkeys(
            [
                "total_return",
                "annualized_return",
                "sharpe",
                "sortino",
                "calmar",
                "max_drawdown",
                "volatility",
            ]
        )
    g = eq / capital
    rets = g.pct_change().dropna()
    total = float(g.iloc[-1] - 1)
    mdd = float((g / g.cummax() - 1).min())
    n = len(rets)
    out: dict[str, float | None] = {
        "total_return": total,
        "max_drawdown": mdd,
        "annualized_return": None,
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "volatility": None,
    }
    if n >= MIN_SESSIONS_FOR_RATIOS:
        sd = float(rets.std(ddof=1))
        down = float(np.sqrt((np.minimum(rets, 0) ** 2).mean()))
        out["volatility"] = sd * np.sqrt(252)
        out["sharpe"] = float(rets.mean() / sd * np.sqrt(252)) if sd > 0 else None
        out["sortino"] = float(rets.mean() / down * np.sqrt(252)) if down > 0 else None
    if n >= MIN_SESSIONS_FOR_ANNUAL:
        cagr = (1 + total) ** (252 / n) - 1
        out["annualized_return"] = cagr
        out["calmar"] = cagr / abs(mdd) if mdd < 0 else None
    return out


class Backend:
    def __init__(self, cfg: PaperConfig) -> None:
        self.cfg = cfg
        self.db = PaperDB(cfg.db_path)

    # --- shared pieces ------------------------------------------------------------
    def equity_frame(self) -> pd.DataFrame:
        rows = self.db.rows("SELECT portfolio, sleeve, date, equity, gross FROM equity")
        if not rows:
            return pd.DataFrame(columns=["portfolio", "sleeve", "date", "equity", "gross"])
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df

    def portfolio_equity(self, pid: str) -> pd.Series:
        df = self.equity_frame()
        df = df[df["portfolio"] == pid]
        return df.groupby("date")["equity"].sum().sort_index()

    def total_equity(self) -> pd.Series:
        df = self.equity_frame()
        return df.groupby("date")["equity"].sum().sort_index()

    def positions(self) -> list[dict[str, Any]]:
        rows = self.db.rows("SELECT * FROM positions ORDER BY portfolio, ticker")
        total = self.equity_now()
        last_session = self.db.get_json("meta", {}).get("last_session")
        out = []
        for p in rows:
            value = p["shares"] * p["last"]
            risk = max(0.0, (p["last"] - p["stop"]) * p["shares"]) if p["stop"] else None
            opened = p["opened"]
            out.append(
                {
                    "ticker": p["ticker"],
                    "name": name_of(p["ticker"]),
                    "portfolio": p["portfolio"],
                    "strategy": p["sleeve"],
                    "sector": sector_of(p["ticker"]),
                    "shares": p["shares"],
                    "entry": p["entry"],
                    "last": p["last"],
                    "prev": p["prev"],
                    "stop": p["stop"],
                    "opened": opened,
                    "market_value": value,
                    "unrealized_pnl": (p["last"] - p["entry"]) * p["shares"],
                    "unrealized_pct": p["last"] / p["entry"] - 1 if p["entry"] else None,
                    "day_pnl": (p["last"] - p["prev"]) * p["shares"] if p["prev"] else None,
                    "risk_usd": risk,
                    "risk_pct_equity": risk / total if (risk is not None and total) else None,
                    "stop_distance_pct": (p["last"] - p["stop"]) / p["last"] if p["stop"] else None,
                    "weight": value / total if total else None,
                    "days_held": (pd.Timestamp(last_session) - pd.Timestamp(opened)).days
                    if (opened and last_session)
                    else None,
                }
            )
        return out

    def equity_now(self) -> float:
        tot = self.total_equity()
        return float(tot.iloc[-1]) if len(tot) else self.cfg.start_capital

    def trade_stats(self, pid: str) -> dict[str, float | None]:
        trades = self.db.rows("SELECT pnl FROM trades WHERE portfolio = ?", (pid,))
        pnl = [t["pnl"] for t in trades]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x < 0]
        df = self.equity_frame()
        df = df[df["portfolio"] == pid]
        exposure = None
        if len(df):
            w = df.assign(v=df["gross"] * df["equity"]).groupby("date")[["v", "equity"]].sum()
            exposure = float((w["v"] / w["equity"]).mean())
        return {
            "win_rate": len(wins) / len(pnl) if pnl else None,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
            "exposure": exposure,
            "trades": len(pnl),
        }

    # --- endpoints ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        tot = self.total_equity()
        eq = float(tot.iloc[-1]) if len(tot) else self.cfg.start_capital
        prev = float(tot.iloc[-2]) if len(tot) > 1 else self.cfg.start_capital
        peak = float(max(tot.max(), self.cfg.start_capital)) if len(tot) else eq
        unread = self.db.rows("SELECT COUNT(*) AS n FROM alerts WHERE read = 0")[0]["n"]
        recs = self.db.rows("SELECT COUNT(*) AS n FROM recommendations WHERE status = 'open'")
        return {
            "as_of": self.db.get_json("meta", {}).get("updated", _now_iso()),
            "equity": eq,
            "start_capital": self.cfg.start_capital,
            "pnl_today": eq - prev,
            "pnl_today_pct": eq / prev - 1 if prev else 0.0,
            "peak_equity": peak,
            "drawdown_pct": eq / peak - 1 if peak else 0.0,
            "market": market_status(),
            "kill_switch": self.cfg.kill_switch_path.exists(),
            "unread_alerts": unread,
            "open_recommendations": recs[0]["n"],
            "last_session": self.db.get_json("meta", {}).get("last_session"),
            "status": self.status(),
        }

    def overview(self) -> dict[str, Any]:
        bm = self.db.get_json("benchmarks", {})
        dates_set: set[str] = set()
        curves: dict[str, pd.Series] = {}
        portfolios = []
        for p in self.cfg.portfolios:
            eq = self.portfolio_equity(p.id)
            curves[p.id] = eq
            dates_set |= {f"{d.date()}" for d in eq.index}
            pnl_today = float(eq.iloc[-1] - (eq.iloc[-2] if len(eq) > 1 else p.capital))
            portfolios.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "start_capital": p.capital,
                    "equity": float(eq.iloc[-1]) if len(eq) else p.capital,
                    "pnl_today": pnl_today if len(eq) else 0.0,
                    "metrics": {**curve_metrics(eq, p.capital), **self.trade_stats(p.id)},
                }
            )
        for ph in self.cfg.placeholders:
            portfolios.append(
                {
                    "id": ph["id"],
                    "name": ph.get("name", ph["id"]),
                    "start_capital": 0.0,
                    "equity": 0.0,
                    "pnl_today": 0.0,
                    "metrics": {
                        **curve_metrics(pd.Series(dtype=float), 0.0),
                        "win_rate": None,
                        "profit_factor": None,
                        "exposure": None,
                        "trades": 0,
                    },
                }
            )
        for b in bm.values():
            dates_set |= set(b["dates"])
        dates = sorted(dates_set)
        series = []
        for p in self.cfg.portfolios:
            s = curves[p.id]
            s.index = [f"{d.date()}" for d in s.index]
            vals = (s.reindex(dates).ffill().fillna(p.capital) / p.capital - 1).tolist()
            series.append({"id": p.id, "name": p.name, "kind": "portfolio", "values": vals})
        benchmarks = []
        for bid, b in bm.items():
            g = pd.Series(b["growth"], index=b["dates"]).reindex(dates).ffill().fillna(1.0)
            series.append(
                {"id": bid, "name": b["name"], "kind": "benchmark", "values": (g - 1).tolist()}
            )
            gs = pd.Series(b["growth"], index=pd.to_datetime(b["dates"]))
            benchmarks.append({"id": bid, "name": b["name"], "metrics": curve_metrics(gs, 1.0)})
        return {
            "as_of": self.db.get_json("meta", {}).get("updated", _now_iso()),
            "dates": dates,
            "series": series,
            "portfolios": portfolios,
            "benchmarks": benchmarks,
        }

    def recommendations(self) -> list[dict[str, Any]]:
        eq = self.equity_now()
        out = []
        for r in self.db.rows(
            "SELECT * FROM recommendations WHERE status = 'open' ORDER BY created, ticker"
        ):
            per = (r["entry"] - r["stop"]) if r["stop"] else None
            risk = per * r["shares"] if per is not None else None
            out.append(
                {
                    "id": r["id"],
                    "ticker": r["ticker"],
                    "direction": r["direction"],
                    "confidence": r["confidence"],
                    "portfolio": r["portfolio"],
                    "strategy": r["sleeve"],
                    "entry": r["entry"],
                    "stop": r["stop"],
                    "take_profit": r["take_profit"],
                    "shares": r["shares"],
                    "created": r["created"],
                    "horizon": r["horizon"],
                    "signals": r["signals"],
                    "risk": r["risk"],
                    "invalidation": r["invalidation"],
                    "position_value": r["entry"] * r["shares"],
                    "risk_usd": risk,
                    "risk_pct_equity": risk / eq if (risk is not None and eq) else None,
                    "reward_risk": abs(r["take_profit"] - r["entry"]) / per
                    if (r["take_profit"] and per)
                    else None,
                }
            )
        return out

    def handle_recommendation(self, rid: str, action: str) -> dict[str, Any]:
        rows = self.db.rows("SELECT * FROM recommendations WHERE id = ?", (rid,))
        if not rows:
            raise HTTPException(404, f"no trade idea {rid}")
        r = rows[0]
        status = "taken" if action == "taken" else "skipped"
        self.db.execute(
            "UPDATE recommendations SET status = ?, handled_at = ? WHERE id = ?",
            (status, _now_iso(), rid),
        )
        verb = "marked this as taken" if status == "taken" else "skipped this"
        self.db.event(
            _now_iso(),
            "manual",
            f"You {verb}: {r['direction'].lower()} {r['shares']} at {r['entry']:.2f}",
            r["ticker"],
            r["sleeve"],
        )
        return {"ok": True}

    def _why(self) -> dict[tuple[str, str], str]:
        """Latest non-zero target decision per (sleeve, ticker): the reason it is held."""
        out: dict[tuple[str, str], str] = {}
        for e in self.db.rows(
            "SELECT strategy, ticker, detail FROM events WHERE type = 'decision' "
            "AND detail NOT LIKE 'Holds 0.0%' AND detail NOT LIKE 'Target 0.0%' "
            "ORDER BY ts DESC, id DESC"
        ):
            out.setdefault((e["strategy"], e["ticker"]), e["detail"])
        return out

    def _history(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        out: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for e in self.db.rows(
            "SELECT ts, strategy, ticker, type, detail FROM events "
            "WHERE type IN ('fill', 'manual') ORDER BY ts DESC, id DESC"
        ):
            out.setdefault((e["strategy"], e["ticker"]), []).append(
                {"ts": e["ts"], "type": e["type"], "detail": e["detail"]}
            )
        return out

    def sleeves(self) -> list[dict[str, Any]]:
        """Each strategy sleeve: money, cash, and what it holds."""
        pos = self.positions()
        last = {
            r["sleeve"]: r
            for r in self.db.rows(
                "SELECT e.* FROM equity e JOIN (SELECT sleeve, MAX(date) AS d FROM equity "
                "GROUP BY sleeve) m ON e.sleeve = m.sleeve AND e.date = m.d"
            )
        }
        out = []
        for p, sl in self.cfg.sleeves:
            row = last.get(sl.id)
            eq = float(row["equity"]) if row else sl.capital
            cash = float(row["cash"]) if row else sl.capital
            held = [x for x in pos if x["strategy"] == sl.id]
            kinds = (
                [m["kind"] for m in sl.strategy.params.get("members", [])]
                if sl.strategy.kind == "ensemble"
                else [sl.strategy.kind]
            )
            out.append(
                {
                    "id": sl.id,
                    "portfolio": p.id,
                    "name": " + ".join(STRATEGY_INFO.get(k, (k, ""))[0] for k in kinds),
                    "rules": "; ".join(STRATEGY_INFO.get(k, (k, k))[1] for k in kinds),
                    "start_capital": sl.capital,
                    "equity": eq,
                    "cash": cash,
                    "invested_pct": (eq - cash) / eq if eq else 0.0,
                    "return_pct": eq / sl.capital - 1 if sl.capital else 0.0,
                    "holdings": sorted(
                        (
                            {
                                "key": x["ticker"],
                                "value": x["market_value"],
                                "pct": x["market_value"] / eq,
                            }
                            for x in held
                        ),
                        key=lambda r: -r["value"],
                    ),
                }
            )
        return out

    def status(self) -> dict[str, Any]:
        meta = self.db.get_json("meta", {})
        from ff.markets.calendar import ET, next_session

        last = meta.get("last_session")
        nxt = next_session(pd.Timestamp(last)) if last else None
        return {
            "last_session": last,
            "updated": meta.get("updated"),
            "provider": meta.get("provider"),
            "data_issues": meta.get("data_issues", 0),
            "cadence": "End of day: one update after each US close (daily prices, not real-time)",
            "next_update_et": f"{nxt.date()} 16:45 ET" if nxt is not None else None,
            "tz": str(ET),
        }

    def trades(self) -> list[dict[str, Any]]:
        return [
            {
                "sleeve": t["sleeve"],
                "portfolio": t["portfolio"],
                "ticker": t["ticker"],
                "opened": t["entry_date"],
                "closed": t["exit_date"],
                "shares": t["quantity"],
                "entry": t["entry_price"],
                "exit": t["exit_price"],
                "pnl": t["pnl"],
                "pnl_pct": t["exit_price"] / t["entry_price"] - 1 if t["entry_price"] else None,
                "reason": t["reason"],
            }
            for t in self.db.rows("SELECT * FROM trades ORDER BY exit_date DESC, id DESC")
        ]

    def positions_payload(self) -> dict[str, Any]:
        pos = self.positions()
        why, hist = self._why(), self._history()
        for p in pos:
            key = (p["strategy"], p["ticker"])
            p["why"] = why.get(key)
            p["history"] = hist.get(key, [])[:20]
        return {
            "as_of": self.db.get_json("meta", {}).get("updated", _now_iso()),
            "equity": self.equity_now(),
            "positions": pos,
            "sleeves": self.sleeves(),
            "flat_portfolios": [],
        }

    def _validation_metrics(self, kind: str) -> dict[str, Any]:
        """Latest validation.json for this strategy kind (Phase 2 runs), if any."""
        empty = {"cagr": None, "sharpe": None, "max_dd": None, "win_rate": None}
        best: tuple[float, dict[str, Any]] | None = None
        prefix = {"fixed_mix": "sixty_forty"}.get(kind, kind)
        root = Path(self.cfg.runs_dir)
        for f in root.glob("*_validate_*/validation.json") if root.exists() else []:
            try:
                doc = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            keys = [k for k in doc.get("metrics", {}) if k.startswith(prefix)]
            base = doc["metrics"][min(keys, key=len)] if keys else None
            if base and (best is None or f.stat().st_mtime > best[0]):
                best = (f.stat().st_mtime, base)
        if best is None:
            return empty
        m = best[1]
        return {
            "cagr": m.get("cagr"),
            "sharpe": m.get("sharpe"),
            "max_dd": m.get("max_drawdown"),
            "win_rate": m.get("win_rate"),
        }

    def strategies(self) -> list[dict[str, Any]]:
        total = self.cfg.start_capital
        df = self.equity_frame()
        out = []
        for p, sl in self.cfg.sleeves:
            eq = df[df["sleeve"] == sl.id].set_index("date")["equity"].sort_index()
            m = curve_metrics(eq, sl.capital)
            n_trades = self.db.rows("SELECT COUNT(*) AS n FROM trades WHERE sleeve = ?", (sl.id,))[
                0
            ]["n"]
            fills = self.db.rows(
                "SELECT COUNT(*) AS n FROM events WHERE strategy = ? AND type = 'fill'", (sl.id,)
            )[0]["n"]
            kinds = (
                [m_["kind"] for m_ in sl.strategy.params.get("members", [])]
                if sl.strategy.kind == "ensemble"
                else [sl.strategy.kind]
            )
            names = [STRATEGY_INFO.get(k, (k, ""))[0] for k in kinds]
            bt = (
                self._validation_metrics(kinds[0])
                if len(kinds) == 1
                else {"cagr": None, "sharpe": None, "max_dd": None, "win_rate": None}
            )
            out.append(
                {
                    "id": sl.id,
                    "name": " + ".join(names),
                    "desc": "; ".join(STRATEGY_INFO.get(k, (k, k))[1] for k in kinds),
                    "portfolio": p.id,
                    "status": "paper",
                    "allocation": sl.capital / total if total else 0.0,
                    "since": self.cfg.start,
                    "live_trades": fills or n_trades,
                    "backtest": bt,
                    "live": {
                        "cagr": m["annualized_return"],
                        "sharpe": m["sharpe"],
                        "max_dd": m["max_drawdown"],
                        "win_rate": self.trade_stats(p.id)["win_rate"],
                    }
                    if len(eq)
                    else None,
                }
            )
            if len(kinds) > 1:
                for k in kinds:
                    name, desc = STRATEGY_INFO.get(k, (k, ""))
                    out.append(
                        {
                            "id": k,
                            "name": name,
                            "desc": f"{desc}. Part of the {' + '.join(names)} sleeve; "
                            "live results are tracked for the sleeve",
                            "portfolio": p.id,
                            "status": "paper",
                            "allocation": sl.capital / total / len(kinds) if total else 0.0,
                            "since": self.cfg.start,
                            "live_trades": 0,
                            "backtest": self._validation_metrics(k),
                            "live": None,
                        }
                    )
        for r in self.cfg.research:
            name, desc = STRATEGY_INFO.get(r["kind"], (r.get("name", r["kind"]), ""))
            out.append(
                {
                    "id": r["id"],
                    "name": r.get("name", name),
                    "desc": desc + ". Failed the overfitting checks, so research only",
                    "portfolio": "track_a",
                    "status": "research",
                    "allocation": 0.0,
                    "since": None,
                    "live_trades": 0,
                    "backtest": self._validation_metrics(r["kind"]),
                    "live": None,
                }
            )
        return out

    def risk(self) -> dict[str, Any]:
        pos = self.positions()
        eq = self.equity_now()
        tot = self.total_equity()
        peak = float(max(tot.max(), self.cfg.start_capital)) if len(tot) else eq
        prev = float(tot.iloc[-2]) if len(tot) > 1 else self.cfg.start_capital

        def by(key: str) -> list[dict[str, Any]]:
            m: dict[str, float] = {}
            for p in pos:
                m[p[key]] = m.get(p[key], 0.0) + p["market_value"]
            return sorted(
                ({"key": k, "value": v, "pct": v / eq} for k, v in m.items()),
                key=lambda r: -r["value"],
            )

        risky = [p for p in pos if p["risk_pct_equity"] is not None]
        top = max(risky, key=lambda p: p["risk_pct_equity"], default=None)
        open_risk = sum(p["risk_usd"] or 0.0 for p in pos)
        day = eq - prev
        corr = self.db.get_json("correlation", {"tickers": [], "matrix": [], "window": ""})
        return {
            "equity": eq,
            "limits": {
                "risk_per_trade": self.cfg.risk.max_risk_per_trade,
                "daily_loss": self.cfg.risk.daily_loss_limit,
                "circuit_breaker": self.cfg.risk.max_drawdown,
                "sector": None,  # no asset-class cap is enforced yet
                "gross_exposure": self.cfg.risk.max_gross_exposure,
            },
            "risk_per_trade": {
                "value": top["risk_pct_equity"] if top else 0.0,
                "ticker": top["ticker"] if top else "—",
            },
            "open_risk": {"usd": open_risk, "pct": open_risk / eq if eq else 0.0},
            "daily_loss": {"value": min(0.0, day) / prev if prev else 0.0, "pnl": day},
            "drawdown": {"value": eq / peak - 1 if peak else 0.0, "peak": peak},
            "gross_exposure": sum(p["market_value"] for p in pos) / eq if eq else 0.0,
            "exposure": {
                "ticker": by("ticker"),
                "sector": by("sector"),
                "strategy": by("strategy"),
            },
            "correlation": corr,
            "risk_state": {
                sl.id: self.db.get_json(f"risk_state:{sl.id}") for _, sl in self.cfg.sleeves
            },
        }

    def signal_events(self) -> list[dict[str, Any]]:
        """Phase 4 adds the market-intelligence sources; until then the events are the
        strategies' own price signals behind the latest trade ideas."""
        out = []
        for r in self.db.rows(
            "SELECT * FROM recommendations ORDER BY created DESC, ticker LIMIT 30"
        ):
            out.append(
                {
                    "ts": r["created"],
                    "source": "Price",
                    "ticker": r["ticker"],
                    "text": r["signals"][:240],
                    "score": (r["confidence"] - 50) / 50 * (1 if r["direction"] == "BUY" else -1),
                }
            )
        return out

    def journal(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.rows("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            e = {
                "id": f"ev_{r['id']:05d}",
                "ts": r["ts"],
                "type": r["type"],
                "ticker": r["ticker"],
                "strategy": r["strategy"],
                "detail": r["detail"],
            }
            if r["extra"]:
                e.update(json.loads(r["extra"]))
            out.append(e)
        return out

    def alerts(self) -> list[dict[str, Any]]:
        return [
            {
                "id": f"al_{r['id']}",
                "ts": r["ts"],
                "severity": r["severity"],
                "title": r["title"],
                "body": r["body"],
                "read": bool(r["read"]),
            }
            for r in self.db.rows("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT 200")
        ]

    def alert_settings(self) -> dict[str, Any]:
        s = self.db.get_json("alert_settings", DEFAULT_ALERT_SETTINGS)
        s.setdefault("telegram", {})["bot_configured"] = telegram.bot_token() is not None
        return dict(s)

    def kill_switch(self, engaged: bool) -> dict[str, Any]:
        path = self.cfg.kill_switch_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if engaged:
            path.write_text(f"engaged from the dashboard at {_now_iso()}\n")
        elif path.exists():
            path.unlink()
        on = path.exists()
        text = "on. Trading stopped" if on else "off. Trading is back on"
        self.db.event(_now_iso(), "system", f"You turned the kill switch {text}")
        self.db.alert(
            _now_iso(),
            "critical" if on else "info",
            f"Kill switch turned {'on' if on else 'off'}",
            "No decisions are made and waiting orders are cancelled at the next run."
            if on
            else "Strategies run again after the next close. Cancelled orders are not re-sent.",
        )
        settings = self.alert_settings()
        if settings.get("events", {}).get("kill_switch") and settings["telegram"].get("enabled"):
            telegram.send(
                f"Finance Free: kill switch turned {'ON' if on else 'off'}",
                settings["telegram"].get("chat_id") or None,
            )
        return {"engaged": on}


def _alert_id(aid: str) -> int:
    try:
        return int(aid.removeprefix("al_"))
    except ValueError as e:
        raise HTTPException(404, f"no alert {aid}") from e


def create_app(cfg: PaperConfig, dashboard_dir: Path | None = None) -> FastAPI:
    be = Backend(cfg)
    app = FastAPI(title="Finance Free", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.backend = be

    def j(x: Any) -> Any:
        return _clean(x)

    @app.get("/api/health")
    def health() -> Any:
        return j({"ok": True, **be.db.get_json("meta", {})})

    @app.get("/api/summary")
    def summary() -> Any:
        return j(be.summary())

    @app.get("/api/overview")
    def overview() -> Any:
        return j(be.overview())

    @app.get("/api/recommendations")
    def recommendations() -> Any:
        return j(be.recommendations())

    @app.post("/api/recommendations/{rid}/taken")
    def taken(rid: str) -> Any:
        return be.handle_recommendation(rid, "taken")

    @app.post("/api/recommendations/{rid}/skip")
    def skip(rid: str) -> Any:
        return be.handle_recommendation(rid, "skip")

    @app.get("/api/positions")
    def positions() -> Any:
        return j(be.positions_payload())

    @app.get("/api/strategies")
    def strategies() -> Any:
        return j(be.strategies())

    @app.get("/api/risk")
    def risk() -> Any:
        return j(be.risk())

    @app.get("/api/signals")
    def signals() -> Any:
        return []  # Phase 4: market-intelligence sources and their scoreboard

    @app.get("/api/signals/events")
    def signal_events() -> Any:
        return j(be.signal_events())

    @app.get("/api/status")
    def status() -> Any:
        return j(be.status())

    @app.get("/api/trades")
    def trades() -> Any:
        return j(be.trades())

    @app.get("/api/journal")
    def journal() -> Any:
        return j(be.journal())

    @app.get("/api/alerts")
    def alerts() -> Any:
        return be.alerts()

    @app.post("/api/alerts/{aid}/read")
    def read_alert(aid: str) -> Any:
        be.db.execute("UPDATE alerts SET read = 1 WHERE id = ?", (_alert_id(aid),))
        return {"ok": True}

    @app.get("/api/alerts/settings")
    def get_settings() -> Any:
        return be.alert_settings()

    @app.put("/api/alerts/settings")
    def put_settings(body: dict[str, Any] = Body(...)) -> Any:  # noqa: B008
        cur = be.alert_settings()
        cur.update(body)
        cur.get("telegram", {}).pop("bot_configured", None)
        be.db.set_json("alert_settings", cur)
        return {"ok": True}

    @app.post("/api/alerts/test")
    def test_alert(body: dict[str, Any] | None = Body(None)) -> Any:  # noqa: B008
        channel = (body or {}).get("channel", "telegram")
        if channel != "telegram":
            raise HTTPException(400, "only Telegram is set up; email comes later")
        chat = be.alert_settings().get("telegram", {}).get("chat_id") or None
        ok, detail = telegram.send("Finance Free test alert: Telegram is working.", chat)
        if not ok:
            raise HTTPException(502, detail)
        return {"ok": True, "channel": channel}

    @app.post("/api/kill-switch")
    def kill(body: dict[str, Any] = Body(...)) -> Any:  # noqa: B008
        return be.kill_switch(bool(body.get("engaged")))

    if dashboard_dir and dashboard_dir.exists():
        index = (dashboard_dir / "index.html").read_text(encoding="utf-8")
        live = index.replace(
            '<script src="js/mock.js"></script>',
            "<script>window.FF_MODE = 'live';</script>\n  <script src=\"js/mock.js\"></script>",
        )

        @app.get("/", response_class=HTMLResponse)
        @app.get("/index.html", response_class=HTMLResponse)
        def home() -> str:
            return live

        app.mount("/", StaticFiles(directory=dashboard_dir), name="dashboard")
    return app
