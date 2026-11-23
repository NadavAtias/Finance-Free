"""Point-in-time data access: the look-ahead guard.

Strategies only ever receive a ``DataView`` bound to the current session. Every
accessor slices to rows with index <= ``asof``; there is no method that can
return later data. Tests corrupt future rows and assert decisions don't change.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class MarketData:
    """Holds full histories. Only the engine touches this object directly."""

    def __init__(self, bars: dict[str, pd.DataFrame]) -> None:
        self._bars = bars
        self._index = {s: df.index.values for s, df in bars.items()}
        # Fast per-session lookup used by the broker and mark-to-market.
        self._rows: dict[str, dict[pd.Timestamp, dict[str, float]]] = {
            s: dict(zip(df.index, df.to_dict("records"), strict=True))  # type: ignore[arg-type]
            for s, df in bars.items()
        }

    @property
    def symbols(self) -> list[str]:
        return list(self._bars)

    def bar(self, symbol: str, date: pd.Timestamp) -> dict[str, float] | None:
        """The session's bar as a plain dict (open/high/low/close/volume), or None."""
        rows = self._rows.get(symbol)
        return None if rows is None else rows.get(date)

    def sessions(self, reference: str) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self._bars[reference].index)

    def view(self, asof: pd.Timestamp) -> DataView:
        return DataView(self, asof)

    def _upto(self, symbol: str, asof: pd.Timestamp) -> pd.DataFrame:
        df = self._bars[symbol]
        end = int(np.searchsorted(self._index[symbol], np.datetime64(asof), side="right"))
        return df.iloc[:end]


class DataView:
    def __init__(self, data: MarketData, asof: pd.Timestamp) -> None:
        self._data = data
        self.asof = asof

    @property
    def symbols(self) -> list[str]:
        return self._data.symbols

    def history(self, symbol: str, lookback: int | None = None) -> pd.DataFrame:
        """Bars up to and including ``asof``; the last ``lookback`` rows if given."""
        df = self._data._upto(symbol, self.asof)
        return df if lookback is None else df.iloc[-lookback:]

    def close(self, symbol: str) -> float | None:
        df = self.history(symbol, 1)
        if df.empty or df.index[-1] != self.asof:
            return None  # no bar today: don't trade on stale prices
        return float(df["close"].iloc[-1])
