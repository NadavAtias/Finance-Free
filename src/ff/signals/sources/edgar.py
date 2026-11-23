"""SEC EDGAR helpers: ticker to CIK, and a company's full filing index."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from ff.signals.http import sec_http

log = logging.getLogger(__name__)
FILING_FIELDS = [
    "accessionNumber",
    "form",
    "filingDate",
    "acceptanceDateTime",
    "items",
    "primaryDocument",
]


def ticker_ciks(cache_dir: Path, refresh: bool = True) -> dict[str, int]:
    body = sec_http().cached(
        "https://www.sec.gov/files/company_tickers.json",
        cache_dir / "edgar" / "company_tickers.json",
        max_age_hours=24 * 7,
        refresh=refresh,
    )
    return parse_company_tickers(body)


def parse_company_tickers(body: bytes) -> dict[str, int]:
    raw = json.loads(body)
    out: dict[str, int] = {}
    for row in raw.values():
        t = str(row["ticker"]).upper().replace(".", "-")
        out.setdefault(t, int(row["cik_str"]))
    return out


def _columns(block: dict[str, Any]) -> pd.DataFrame:
    n = len(block.get("accessionNumber", []))
    return pd.DataFrame({f: block.get(f, [""] * n) for f in FILING_FIELDS})


def parse_submissions(main: dict[str, Any], pages: list[dict[str, Any]]) -> pd.DataFrame:
    """Combine ``filings.recent`` and the older pages into one filing table."""
    filings: dict[str, Any] = main.get("filings", {})
    parts = [_columns(filings.get("recent", {}))] + [_columns(p) for p in pages]
    df = pd.concat(parts, ignore_index=True)
    df["filingDate"] = pd.to_datetime(df["filingDate"], errors="coerce")
    df["items"] = df["items"].fillna("").astype(str)
    return df.dropna(subset=["filingDate"]).drop_duplicates("accessionNumber")


def company_filings(
    cik: int, cache_dir: Path, refresh: bool = True, since: pd.Timestamp | None = None
) -> pd.DataFrame:
    """All filings of one company. The main file is refreshed daily; older pages
    never change, so they are cached forever. Pages that end before ``since``
    are skipped."""
    http = sec_http()
    d = cache_dir / "edgar" / "submissions"
    main = json.loads(
        http.cached(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
            d / f"CIK{cik:010d}.json",
            max_age_hours=20,
            refresh=refresh,
        )
    )
    pages = []
    for f in main.get("filings", {}).get("files", []):
        if since is not None and pd.Timestamp(f.get("filingTo", "2100-01-01")) < since:
            continue
        pages.append(
            json.loads(
                http.cached(
                    f"https://data.sec.gov/submissions/{f['name']}",
                    d / f["name"],
                    max_age_hours=None,
                    refresh=refresh,
                )
            )
        )
    return parse_submissions(main, pages)
