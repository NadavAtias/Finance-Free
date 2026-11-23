"""Economic calendar: scheduled market-moving releases.

Used for context in rationales and on the dashboard (an FOMC decision or a CPI
print inside a trade's first days adds gap risk). It is not a scored signal:
the best-known calendar effect, the pre-FOMC drift (Lucca & Moench 2015), has
been much weaker since it was published.

FOMC dates are the Fed's published schedule (federalreserve.gov). CPI and jobs
report dates come from the free FRED API when ``FRED_API_KEY`` is set
(release ids 10 = CPI, 50 = Employment Situation).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd

from ff.signals.http import FetchError, Http

log = logging.getLogger(__name__)

# Second day of each scheduled two-day meeting (the decision and statement day).
FOMC = [
    "2024-01-31",
    "2024-03-20",
    "2024-05-01",
    "2024-06-12",
    "2024-07-31",
    "2024-09-18",
    "2024-11-07",
    "2024-12-18",
    "2025-01-29",
    "2025-03-19",
    "2025-05-07",
    "2025-06-18",
    "2025-07-30",
    "2025-09-17",
    "2025-10-29",
    "2025-12-10",
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]
RELEASES = {10: "CPI inflation report", 50: "Jobs report"}


def upcoming(
    cache_dir: Path, today: pd.Timestamp, days: int = 45, refresh: bool = True
) -> list[dict[str, str]]:
    out = [
        {"date": d, "event": "Fed rate decision (FOMC)"}
        for d in FOMC
        if today <= pd.Timestamp(d) <= today + pd.Timedelta(days=days)
    ]
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        http = Http(min_interval=0.5)
        for rid, name in RELEASES.items():
            try:
                body = http.cached(
                    "https://api.stlouisfed.org/fred/release/dates",
                    cache_dir / "fred" / f"release_{rid}.json",
                    max_age_hours=24,
                    refresh=refresh,
                    params={
                        "release_id": str(rid),
                        "api_key": key,
                        "file_type": "json",
                        "include_release_dates_with_no_data": "true",
                        "realtime_start": f"{today.date()}",
                        "sort_order": "asc",
                    },
                )
                for r in json.loads(body).get("release_dates", []):
                    d = pd.Timestamp(r["date"])
                    if today <= d <= today + pd.Timedelta(days=days):
                        out.append({"date": f"{d.date()}", "event": name})
            except (FetchError, ValueError, KeyError) as exc:
                log.warning("FRED release calendar %s unavailable: %s", rid, exc)
    return sorted(out, key=lambda e: e["date"])
