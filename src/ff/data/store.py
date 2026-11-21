"""Local Parquet bar cache plus provider registry and validation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ff.data.providers.base import DataProvider, ProviderError
from ff.data.providers.synthetic import SyntheticProvider
from ff.data.providers.web import StooqProvider, TiingoProvider, YahooProvider

log = logging.getLogger(__name__)


def make_provider(name: str) -> DataProvider:
    registry: dict[str, Callable[[], DataProvider]] = {
        "stooq": StooqProvider,
        "yahoo": YahooProvider,
        "tiingo": TiingoProvider,
        "synthetic": SyntheticProvider,
    }
    if name not in registry:
        raise ProviderError(f"unknown provider {name!r}; choose from {sorted(registry)}")
    provider: DataProvider = registry[name]()
    return provider


@dataclass(frozen=True)
class DataIssue:
    symbol: str
    date: pd.Timestamp | None
    kind: str
    detail: str


def validate_bars(
    symbol: str, df: pd.DataFrame, max_gap_days: int = 7, jump_threshold: float = 0.25
) -> list[DataIssue]:
    """Flag suspicious bars. Never silently modifies data."""
    issues: list[DataIssue] = []
    if df.empty:
        return [DataIssue(symbol, None, "empty", "no bars")]
    bad_px = df[(df[["open", "high", "low", "close"]] <= 0).any(axis=1)]
    issues += [DataIssue(symbol, d, "non_positive_price", "") for d in bad_px.index]
    tol = 1e-6
    bad_hl = df[
        (df["high"] < df[["open", "close"]].max(axis=1) * (1 - tol))
        | (df["low"] > df[["open", "close"]].min(axis=1) * (1 + tol))
    ]
    issues += [DataIssue(symbol, d, "inconsistent_ohlc", "") for d in bad_hl.index]
    gaps = df.index.to_series().diff().dt.days
    issues += [
        DataIssue(symbol, d, "gap", f"{int(g)} calendar days")
        for d, g in zip(gaps.index[gaps > max_gap_days], gaps[gaps > max_gap_days], strict=True)
    ]
    rets = df["close"].pct_change().abs()
    issues += [
        DataIssue(symbol, d, "jump", f"{r:.1%} close-to-close")
        for d, r in zip(rets.index[rets > jump_threshold], rets[rets > jump_threshold], strict=True)
    ]
    return issues


class BarStore:
    """Caches each symbol's full history as Parquet under ``<data_dir>/bars/<provider>/``."""

    def __init__(self, provider: DataProvider, data_dir: str | Path) -> None:
        self.provider = provider
        self.root = Path(data_dir) / "bars" / provider.caps.name
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.parquet"

    def get(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp, refresh: bool = False
    ) -> pd.DataFrame:
        path = self._path(symbol)
        cached: pd.DataFrame | None = None
        if path.exists() and not refresh:
            cached = pd.read_parquet(path)
            covers_end = cached.index.max() >= end - pd.Timedelta(days=4)
            if cached.index.min() <= start + pd.Timedelta(days=7) and covers_end:
                return cached.loc[start:end]
        fresh = self.provider.daily_bars(symbol, start - pd.Timedelta(days=10), end)
        if cached is not None:
            fresh = pd.concat([cached, fresh])
            fresh = fresh[~fresh.index.duplicated(keep="last")].sort_index()
        fresh.to_parquet(path)
        log.info(
            "cached %s: %d bars %s..%s",
            symbol,
            len(fresh),
            fresh.index.min().date(),
            fresh.index.max().date(),
        )
        return fresh.loc[start:end]

    def load(
        self, symbols: list[str], start: pd.Timestamp, end: pd.Timestamp, refresh: bool = False
    ) -> tuple[dict[str, pd.DataFrame], list[DataIssue]]:
        bars: dict[str, pd.DataFrame] = {}
        issues: list[DataIssue] = []
        for s in symbols:
            df = self.get(s, start, end, refresh=refresh)
            issues += validate_bars(s, df)
            bars[s] = df
        return bars, issues
