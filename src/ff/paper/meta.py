"""Display names and asset classes for the dashboard. Unknown tickers fall back
to the ticker itself and "Other"."""

from __future__ import annotations

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


def name_of(ticker: str) -> str:
    return INSTRUMENTS.get(ticker, (ticker, "Other"))[0]


def sector_of(ticker: str) -> str:
    return INSTRUMENTS.get(ticker, (ticker, "Other"))[1]


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
}
