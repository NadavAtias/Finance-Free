"""Signal snapshots for the dashboard, written after each paper run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ff.backtest.runner import Prepared
from ff.core.universe import load_universe
from ff.paper.config import PaperConfig
from ff.paper.db import PaperDB
from ff.signals import calendar
from ff.signals.base import MARKET
from ff.strategies.base import Context, build_strategy


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), n)


def scoreboard(prep: Prepared, last: pd.Timestamp) -> list[dict[str, Any]]:
    store = prep.signals
    assert store is not None
    view = store.view(last)
    # the same month-start snapshot the strategy trades on, so both pages agree
    scores = view.scores()
    pick_total = sum(sc.weight for s, sc in scores.items() if store.sources[s].role == "pick")
    out = []
    for sid, info in store.sources.items():
        sc = scores.get(sid)
        st = store.status.get(sid, {})
        share = sc.weight / pick_total if (sc and info.role == "pick" and pick_total > 0) else 0.0
        out.append(
            {
                "id": sid,
                "name": info.name,
                "role": {
                    "pick": "Picking stocks",
                    "exposure": "How much to invest",
                    "warning": "Warnings only",
                }[info.role],
                "status": (sc.status if sc else "insufficient")
                if st.get("ok", True) or st.get("records")
                else "unavailable",
                "horizon": info.horizon,
                "hit_90d": _r(sc.hit_90d) if sc else None,
                "hit_all": _r(sc.hit) if sc else None,
                "n": sc.n_obs if sc else 0,
                "months": sc.n_months if sc else 0,
                "ic": _r(sc.ic) if sc else None,
                "t": _r(sc.t, 2) if sc else None,
                "edge": _r(sc.mean, 5) if sc else None,
                "trust": round(share, 4)
                if info.role == "pick"
                else round(sc.weight, 4)
                if sc
                else 0,
                "by_horizon": {
                    str(h): {k: _r(v, 5) for k, v in d.items()}
                    for h, d in (sc.by_horizon if sc else {}).items()
                },
                "history": [round(x, 3) for x in (sc.history if sc else [])],
                "note": info.note,
                "evidence": info.evidence,
                "data": {
                    "ok": bool(st.get("ok", True)),
                    "updated": st.get("updated"),
                    "problem": st.get("problem", ""),
                    "records": int(st.get("records", 0)),
                },
            }
        )
    return out


def recent_events(prep: Prepared, last: pd.Timestamp, sessions: int = 10) -> list[dict[str, Any]]:
    store = prep.signals
    assert store is not None
    v = store.view(last)
    df = v.known()
    df = df[df["known_idx"] > v.idx - sessions]
    keep = []
    for (sid, _), g in df.groupby(["source", "known_date"]):
        if sid == "price_momentum":  # 100 rows a month: show the extremes only
            g = pd.concat([g.nlargest(3, "value"), g.nsmallest(3, "value")])
        elif sid == "macro_regime":
            g = g.tail(1)
        keep.append(g)
    if not keep:
        return []
    df = pd.concat(keep).sort_values(["known_date", "source"], ascending=False).head(60)
    return [
        {
            "ts": f"{r['known_date'].date()}T21:00:00Z",
            "source": store.sources[r["source"]].name,
            "source_id": r["source"],
            "ticker": "Market" if r["symbol"] == MARKET else r["symbol"],
            "text": r["detail"],
            "score": round(float(r["value"]), 3),
        }
        for _, r in df.iterrows()
    ]


def conviction_board(
    cfg: PaperConfig, prep: Prepared, last: pd.Timestamp, held: set[str]
) -> dict[str, Any] | None:
    store = prep.signals
    assert store is not None
    sleeve = next((s for _, s in cfg.sleeves if s.strategy.kind == "system_conviction"), None)
    if sleeve is None:
        return None
    params = dict(sleeve.strategy.params)
    strat = build_strategy("system_conviction", sleeve.id, sleeve.universe, params)
    ctx = Context(last, prep.data.view(last), 0.0, {}, False, False, signals=store.view(last))
    b = strat.board(ctx)  # type: ignore[attr-defined]
    meta = load_universe(sleeve.universe_file) if sleeve.universe_file else {}
    ranked = sorted(b["scores"].items(), key=lambda kv: -kv[1])
    top = ranked[:12] + [kv for kv in ranked[-5:] if kv[1] < 0 and kv not in ranked[:12]]
    return {
        "as_of": f"{last.date()}",
        "entry_score": getattr(strat, "entry_score", 0.3),
        "exit_score": getattr(strat, "exit_score", 0.0),
        "regime": round(b["regime"], 3),
        "regime_text": b["regime_text"],
        "scale": round(b["scale"], 3),
        "trust": round(b["trust"], 3),
        "weights": {k: round(v, 4) for k, v in b["weights"].items()},
        "names": {k: store.sources[k].name for k in store.sources},
        "stocks": [
            {
                "ticker": t,
                "name": meta.get(t, (t, ""))[0],
                "sector": meta.get(t, ("", "Other"))[1],
                "score": round(s, 3),
                "held": t in held,
                "parts": [
                    {"source": src, "contrib": round(c, 3), "detail": d}
                    for src, (c, d) in sorted(
                        b["parts"].get(t, {}).items(), key=lambda kv: -abs(kv[1][0])
                    )
                ],
            }
            for t, s in top
        ],
        "calendar": calendar.upcoming(Path(cfg.data_dir) / "signals", last),
    }


def save_signal_snapshots(
    db: PaperDB, cfg: PaperConfig, prep: Prepared, last: pd.Timestamp
) -> None:
    held = {r["ticker"] for r in db.rows("SELECT ticker FROM positions")}
    db.set_json("signals_scoreboard", scoreboard(prep, last))
    db.set_json("signals_events", recent_events(prep, last))
    board = conviction_board(cfg, prep, last, held)
    if board is not None:
        db.set_json("conviction_board", board)
