"""Signal records, the point-in-time signal store, and the source interface.

Every external input becomes rows of one table:

| column       | meaning                                                              |
|--------------|----------------------------------------------------------------------|
| ``source``   | source id (``insider_buying``, ``macro_regime``, ...)                |
| ``symbol``   | ticker, or ``MARKET`` (``"*"``) for market-wide sources              |
| ``event_date`` | when the thing happened (trade date, report date, observation date) |
| ``known_date`` | the session at whose close we could first have acted on it       |
| ``value``    | score in [-1, 1]; positive leans buy                                 |
| ``detail``   | one plain-English sentence for rationales and the dashboard          |

``known_date`` is always set conservatively by the source (filing date plus one
session, data release lags, etc). ``SignalView`` is the look-ahead guard: bound
to one session, it only ever returns rows with ``known_date <= asof``, exactly
like ``DataView`` for prices.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from ff.data.view import MarketData
    from ff.signals.scoring import SourceScore, SourceScorer

MARKET = "*"
COLUMNS = ["source", "symbol", "event_date", "known_date", "value", "detail"]


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    #: "pick" (chooses stocks), "exposure" (how much to invest), "warning" (risk flag only)
    role: str
    #: forward horizon in sessions the source is scored and traded on
    horizon: int
    #: sessions a record stays active after ``known_date`` (linear decay to zero)
    ttl: int
    note: str
    evidence: str


def empty_records() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype=object) for c in COLUMNS})
    df["event_date"] = pd.to_datetime(df["event_date"])
    df["known_date"] = pd.to_datetime(df["known_date"])
    df["value"] = df["value"].astype(float)
    return df


def next_session(sessions: pd.DatetimeIndex, d: pd.Timestamp, strictly_after: bool = True) -> Any:
    """The first session after (or on, if not ``strictly_after``) date ``d``; NaT if none."""
    side: Literal["left", "right"] = "right" if strictly_after else "left"
    i = int(np.searchsorted(sessions.values, np.datetime64(pd.Timestamp(d).normalize()), side=side))
    return sessions[i] if i < len(sessions) else pd.NaT


def next_sessions(sessions: pd.DatetimeIndex, dates: pd.Series, strictly_after: bool = True) -> Any:
    """Vectorized ``next_session``."""
    side: Literal["left", "right"] = "right" if strictly_after else "left"
    vals = pd.to_datetime(dates).dt.normalize().values
    idx = np.searchsorted(sessions.values, vals, side=side)
    out = np.full(len(idx), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    ok = idx < len(sessions)
    out[ok] = sessions.values[idx[ok]]
    return pd.Series(out, index=dates.index)


@dataclass
class BuildContext:
    """What a source needs to build its records."""

    data: MarketData
    sessions: pd.DatetimeIndex  # reference (SPY) sessions
    symbols: list[str]  # the stock universe for pick sources
    start: pd.Timestamp
    end: pd.Timestamp
    cache_dir: Path
    refresh: bool = True
    params: dict[str, Any] = field(default_factory=dict)


class SignalSource(ABC):
    info: SourceInfo
    #: True when the source needs no network (built from prices only)
    offline: bool = False

    @abstractmethod
    def build(self, ctx: BuildContext) -> pd.DataFrame:
        """Return records (``COLUMNS``). Must never raise for a network failure:
        use the cache and log a warning instead."""

    #: set by ``build`` for the dashboard: last successful update, and any problem
    status: dict[str, Any]

    def __init__(self) -> None:
        self.status = {"ok": True, "updated": None, "problem": ""}


class SignalStore:
    """All records of all sources plus the scorer. Bound to sessions with ``view``."""

    def __init__(
        self,
        records: pd.DataFrame,
        sources: dict[str, SourceInfo],
        sessions: pd.DatetimeIndex,
        scorer: SourceScorer | None = None,
        status: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        recs = records.dropna(subset=["known_date"]).copy()
        recs = recs[recs["known_date"] <= sessions[-1]] if len(sessions) else recs
        recs["known_idx"] = np.searchsorted(sessions.values, recs["known_date"].values, side="left")
        self.records = recs.sort_values(["known_idx", "source", "symbol"]).reset_index(drop=True)
        self._known = self.records["known_idx"].to_numpy()
        self.sources = sources
        self.sessions = sessions
        self.scorer = scorer
        self.status = status or {}

    def index_of(self, d: pd.Timestamp) -> int:
        """Index of the last session on or before ``d``."""
        return int(np.searchsorted(self.sessions.values, np.datetime64(d), side="right")) - 1

    def view(self, asof: pd.Timestamp) -> SignalView:
        return SignalView(self, asof)


class SignalView:
    """Signals known at the close of ``asof``. There is no way to reach later rows."""

    def __init__(self, store: SignalStore, asof: pd.Timestamp) -> None:
        self._store = store
        self.asof = asof
        self.idx = store.index_of(asof)
        self._end = int(np.searchsorted(store._known, self.idx, side="right"))

    @property
    def sources(self) -> dict[str, SourceInfo]:
        return self._store.sources

    def known(self) -> pd.DataFrame:
        """Every record known at ``asof``."""
        return self._store.records.iloc[: self._end]

    def active(self, source: str, ttl: int | None = None) -> pd.DataFrame:
        """Records of ``source`` still within their time-to-live, with ``age``
        (sessions since known) and ``decay`` (1 when new, falling linearly to 0)."""
        info = self._store.sources[source]
        ttl = info.ttl if ttl is None else ttl
        lo = int(np.searchsorted(self._store._known, self.idx - ttl + 1, side="left"))
        df = self._store.records.iloc[lo : self._end]
        df = df[df["source"] == source].copy()
        df["age"] = self.idx - df["known_idx"]
        df["decay"] = 1.0 - df["age"] / ttl
        return df

    def latest_market(self, source: str) -> pd.Series | None:
        df = self.active(source)
        df = df[df["symbol"] == MARKET]
        return None if df.empty else df.iloc[-1]

    def scores(self) -> dict[str, SourceScore]:
        """Each source's measured predictive value, using only outcomes already known."""
        if self._store.scorer is None:
            return {}
        return self._store.scorer.scores_asof(self.idx)
