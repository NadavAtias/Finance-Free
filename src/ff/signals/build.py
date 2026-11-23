"""Build the signal store for a run: every configured source, cached on disk."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ff.data.view import MarketData
from ff.signals.base import BuildContext, SignalSource, SignalStore, empty_records
from ff.signals.scoring import SourceScorer
from ff.signals.sources.earnings import EarningsReaction
from ff.signals.sources.insider import InsiderBuying
from ff.signals.sources.macro import MacroRegime
from ff.signals.sources.momentum import PriceMomentum

log = logging.getLogger(__name__)

SOURCES: dict[str, type[SignalSource]] = {
    "price_momentum": PriceMomentum,
    "macro_regime": MacroRegime,
    "insider_buying": InsiderBuying,
    "earnings_reaction": EarningsReaction,
}


@dataclass
class SignalsConfig:
    sources: list[str]
    universe: list[str]
    history_start: str = "2007-01-01"
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any], universe: list[str]) -> SignalsConfig:
        return cls(
            sources=list(d.get("sources", list(SOURCES))),
            universe=list(d.get("universe", universe)),
            history_start=str(d.get("history_start", "2007-01-01")),
            params=dict(d.get("params", {})),
        )


def build_store(
    cfg: SignalsConfig,
    data: MarketData,
    data_dir: str | Path,
    end: pd.Timestamp,
    refresh: bool = True,
    sources: dict[str, SignalSource] | None = None,
    offline_only: bool = False,
) -> SignalStore:
    """``sources`` lets tests inject offline sources; ``offline_only`` (synthetic
    data) skips every source that needs the network."""
    sessions = data.sessions("SPY")
    sessions = sessions[sessions <= end]
    ctx = BuildContext(
        data=data,
        sessions=sessions,
        symbols=[s for s in cfg.universe if s in data.symbols],
        start=pd.Timestamp(cfg.history_start),
        end=end,
        cache_dir=Path(data_dir) / "signals",
        refresh=refresh,
        params=cfg.params,
    )
    objs = sources or {sid: SOURCES[sid]() for sid in cfg.sources}
    if offline_only:
        objs = {k: v for k, v in objs.items() if v.offline}
    parts, status = [], {}
    for sid, src in objs.items():
        try:
            recs = src.build(ctx)
        except Exception as exc:  # a broken source must never stop the paper loop
            log.exception("signal source %s failed", sid)
            src.status = {"ok": False, "updated": None, "problem": f"{type(exc).__name__}: {exc}"}
            recs = empty_records()
        log.info("signal source %s: %d records", sid, len(recs))
        status[sid] = {**src.status, "records": len(recs)}
        if len(recs):
            parts.append(recs)
    records = pd.concat(parts, ignore_index=True) if parts else empty_records()
    records["event_date"] = pd.to_datetime(records["event_date"])
    records["known_date"] = pd.to_datetime(records["known_date"])
    records["value"] = records["value"].astype(float)
    infos = {sid: src.info for sid, src in objs.items()}
    scorer = SourceScorer(records, data, sessions, ctx.symbols, infos)
    return SignalStore(records, infos, sessions, scorer, status)
