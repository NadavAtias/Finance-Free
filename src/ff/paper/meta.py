"""Display names and asset classes for the dashboard. Unknown tickers fall back
to the ticker itself and "Other"."""

from __future__ import annotations

from pathlib import Path

from ff.core.universe import load_universe

INSTRUMENTS: dict[str, tuple[str, str]] = {
    "SPY": ("SPDR S&P 500", "US equity"),
    "QQQ": ("Invesco QQQ (Nasdaq-100)", "US equity"),
    "IWM": ("iShares Russell 2000", "US equity"),
    "EFA": ("iShares MSCI EAFE", "Intl equity"),
    "EEM": ("iShares MSCI Emerging Markets", "Intl equity"),
    "TLT": ("iShares 20+ Year Treasury", "Fixed income"),
    "IEF": ("iShares 7-10 Year Treasury", "Fixed income"),
    "VNQ": ("Vanguard Real Estate", "Real estate"),
    "DBC": ("Invesco DB Commodity", "Commodities"),
    "GLD": ("SPDR Gold Shares", "Commodities"),
}


_STOCKS = Path(__file__).resolve().parents[3] / "config" / "universes" / "us_large_cap.yaml"


def _lookup(ticker: str) -> tuple[str, str]:
    if ticker in INSTRUMENTS:
        return INSTRUMENTS[ticker]
    if _STOCKS.exists():
        return load_universe(str(_STOCKS)).get(ticker, (ticker, "Other"))
    return (ticker, "Other")


def name_of(ticker: str) -> str:
    return _lookup(ticker)[0]


def sector_of(ticker: str) -> str:
    return _lookup(ticker)[1]


STRATEGY_INFO: dict[str, tuple[str, str]] = {
    "trend_etf": (
        "Trend following",
        "Holds ETFs above their 200-day average with a positive 12-month return",
    ),
    "risk_parity": ("Risk parity", "Spreads risk equally across all ETFs, always invested"),
    "xs_momentum": (
        "Momentum rotation",
        "Holds the 3 ETFs that rose most over 12 months, if above cash",
    ),
    "fixed_mix": ("60/40 core", "60% SPY, 40% IEF, rebalanced monthly"),
    "system_conviction": (
        "Conviction picks",
        "Buys large US stocks where the proven signal sources agree; each pick has a stop, "
        "a take-profit and a 3-month limit",
    ),
}
