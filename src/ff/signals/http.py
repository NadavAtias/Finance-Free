"""Polite HTTP for free public data: disk cache, rate limit, retries.

SEC EDGAR requires a User-Agent naming you and a contact email ("Jane Doe
jane@example.com"); requests without one are blocked. Set ``SEC_USER_AGENT`` in
``.env``. It is read from the environment only and never stored in code or logs.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


class Http:
    def __init__(self, min_interval: float = 0.0, user_agent: str | None = None) -> None:
        self.min_interval = min_interval
        self.user_agent = user_agent or "Mozilla/5.0 (finance-free research client)"
        self._last = 0.0
        self.session = requests.Session()

    def get(self, url: str, params: dict[str, str] | None = None, retries: int = 4) -> bytes:
        last: Exception | None = None
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(
                    url,
                    params=params,
                    headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
                    timeout=60,
                )
                if r.status_code in (429, 503):
                    time.sleep(2**attempt * 2)
                    last = FetchError(f"HTTP {r.status_code}")
                    continue
                if r.status_code == 404:
                    raise FetchError(f"404 for {url}")
                r.raise_for_status()
                return r.content
            except FetchError:
                raise
            except requests.RequestException as exc:
                last = exc
                time.sleep(2**attempt)
        raise FetchError(f"GET {url} failed after {retries} attempts: {last}")

    def cached(
        self,
        url: str,
        path: Path,
        max_age_hours: float | None,
        refresh: bool = True,
        params: dict[str, str] | None = None,
    ) -> bytes:
        """Return the cached file if fresh enough (``None`` = forever), else download.
        Falls back to a stale cache when the download fails."""
        if path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600
            if not refresh or max_age_hours is None or age_h < max_age_hours:
                return path.read_bytes()
        try:
            body = self.get(url, params)
        except FetchError:
            if path.exists():
                log.warning("using stale cache for %s", url)
                return path.read_bytes()
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(path)
        return body


_sec: Http | None = None


def sec_http() -> Http:
    """Shared EDGAR client: 8 requests/second (SEC asks for at most 10)."""
    global _sec
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise FetchError(
            "SEC EDGAR needs SEC_USER_AGENT in .env, e.g. SEC_USER_AGENT=Your Name you@example.com"
        )
    if _sec is None or _sec.user_agent != ua:
        _sec = Http(min_interval=0.125, user_agent=ua)
    return _sec
