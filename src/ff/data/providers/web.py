"""Free web data providers: Stooq, Yahoo chart API and Tiingo.

None of these include delisted securities, so they are fine for a fixed ETF
universe and NOT valid for single-stock backtests (survivorship bias).
"""

from __future__ import annotations

import io
import os
import time

import pandas as pd
import requests

from ff.data.providers.base import ProviderCaps, ProviderError, normalize

_UA = {"User-Agent": "Mozilla/5.0 (finance-free research client)"}


def _get(url: str, params: dict[str, str] | None = None, retries: int = 3) -> requests.Response:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_UA, timeout=30)
            if r.status_code == 429:
                time.sleep(2**attempt * 2)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(2**attempt)
    raise ProviderError(f"GET {url} failed after {retries} attempts: {last}")


class StooqProvider:
    """stooq.com CSV download. No key. US tickers use the ``.us`` suffix.

    As of 2026-11 Stooq serves a JavaScript bot check to scripts, so this
    provider fails until that changes. Kept for when it works again.
    """

    caps = ProviderCaps(
        "stooq",
        needs_api_key=False,
        adjusted=True,
        includes_delisted=False,
        notes="Free, keyless; split and dividend adjusted; occasional gaps.",
    )

    def daily_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        params = {
            "s": f"{symbol.lower()}.us",
            "i": "d",
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
        }
        text = _get("https://stooq.com/q/d/l/", params).text
        if not text.lower().startswith("date"):
            raise ProviderError(f"stooq returned no data for {symbol}: {text[:80]!r}")
        df = pd.read_csv(io.StringIO(text), parse_dates=["Date"], index_col="Date")
        df.columns = [c.lower() for c in df.columns]
        if "volume" not in df:
            df["volume"] = 0.0
        return normalize(df)


class YahooProvider:
    """Yahoo Finance chart API. Unofficial: may break or rate-limit without notice."""

    caps = ProviderCaps(
        "yahoo",
        needs_api_key=False,
        adjusted=True,
        includes_delisted=False,
        notes="Unofficial endpoint; use for research only.",
    )

    def daily_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        params = {
            "period1": str(int(start.timestamp())),
            "period2": str(int(end.timestamp()) + 86400),
            "interval": "1d",
            "events": "div,split",
            "includeAdjustedClose": "true",
        }
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        data = _get(url, params).json()
        try:
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = res["indicators"]["adjclose"][0]["adjclose"]
            idx = pd.to_datetime(res["timestamp"], unit="s")
        except (KeyError, TypeError, IndexError) as exc:
            raise ProviderError(f"yahoo returned unexpected payload for {symbol}") from exc
        raw = pd.DataFrame(
            {
                "open": q["open"],
                "high": q["high"],
                "low": q["low"],
                "close": q["close"],
                "volume": q["volume"],
                "adj": adj,
            },
            index=idx,
        )
        raw = raw.dropna()
        factor = raw["adj"] / raw["close"]
        for col in ("open", "high", "low", "close"):
            raw[col] = raw[col] * factor
        raw["volume"] = raw["volume"] / factor  # keep dollar volume consistent
        return normalize(raw)


class TiingoProvider:
    """Tiingo EOD API. Needs ``TIINGO_API_KEY`` (free tier available)."""

    caps = ProviderCaps(
        "tiingo",
        needs_api_key=True,
        adjusted=True,
        includes_delisted=False,
        notes="Clean EOD data; free tier has hourly/daily request caps.",
    )

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("TIINGO_API_KEY", "")
        if not self.api_key:
            raise ProviderError("TIINGO_API_KEY is not set (put it in .env, never in code)")

    def daily_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        params = {
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "token": self.api_key,
            "format": "json",
        }
        rows = _get(f"https://api.tiingo.com/tiingo/daily/{symbol.lower()}/prices", params).json()
        if not rows:
            raise ProviderError(f"tiingo returned no data for {symbol}")
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["date"])
        df = df.rename(
            columns={
                "adjOpen": "open",
                "adjHigh": "high",
                "adjLow": "low",
                "adjClose": "close",
                "adjVolume": "volume",
            }
        )
        return normalize(df)
