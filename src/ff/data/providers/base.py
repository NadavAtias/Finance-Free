"""Data provider interface.

A provider returns daily bars for one symbol as a DataFrame indexed by session
date with columns ``open, high, low, close, volume``. Prices are adjusted for
splits and dividends (total-return series) so a buy-and-hold position's value
matches what an investor who reinvested dividends would see.

MVP simplification (documented in docs/ARCHITECTURE.md): we trade adjusted
prices instead of raw prices plus dividend events. This is accurate for returns
and slightly off for share counts and commissions per share. Raw + corporate
action handling arrives with the paid data upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class ProviderCaps:
    name: str
    needs_api_key: bool
    adjusted: bool
    includes_delisted: bool
    notes: str = ""


class DataProvider(Protocol):
    caps: ProviderCaps

    def daily_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...


class ProviderError(RuntimeError):
    pass


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return bars with the standard columns, sorted, de-duplicated, float typed."""
    out = df[BAR_COLUMNS].astype(float).copy()
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).tz_localize(None).normalize()
    out.index.name = "date"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna(subset=["open", "high", "low", "close"])
