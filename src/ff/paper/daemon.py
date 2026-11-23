"""Always-on scheduler: runs the paper loop after every US close.

Runs once at start (catches up on any missed sessions), then sleeps until 16:45
New York time on each trading day. If the data source has not published the
session's bar yet, it retries every 30 minutes, up to 4 times. Any error is
logged, written to the alert feed and sent to Telegram, and the daemon keeps
going: a bad day of data must not stop tomorrow's run.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta

import pandas as pd

from ff.markets.calendar import ET, is_session, last_final_session, now_et
from ff.notify import telegram
from ff.paper.config import PaperConfig
from ff.paper.db import PaperDB
from ff.paper.loop import run_paper

log = logging.getLogger(__name__)
RUN_AT = (16, 45)
RETRIES, RETRY_MINUTES = 4, 30


def next_run(now: datetime) -> datetime:
    d = now.astimezone(ET).date()
    for _ in range(10):
        t = datetime(d.year, d.month, d.day, *RUN_AT, tzinfo=ET)
        if is_session(pd.Timestamp(d)) and t > now:
            return t
        d += timedelta(days=1)
    raise RuntimeError("no trading session in the next 10 days")


def run_once(cfg: PaperConfig) -> bool:
    """One attempt. Returns True when the latest final session is recorded."""
    db = PaperDB(cfg.db_path)
    try:
        res = run_paper(cfg)
    except Exception as e:
        log.exception("paper run failed")
        now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = f"The daily paper run failed: {type(e).__name__}: {e}"
        db.alert(now, "critical", "Daily run failed", msg)
        db.event(now, "system", msg)
        telegram.send(f"Finance Free: {msg}")
        return False
    log.info("processed %d session(s); last %s", res.new_sessions, res.last_session)
    if res.new_sessions:
        log.info("backup written to %s", db.backup())
    expected = last_final_session()
    return res.last_session is not None and res.last_session >= expected


def serve_forever(cfg: PaperConfig) -> None:
    run_once(cfg)
    while True:
        t = next_run(now_et())
        log.info("next paper run at %s", t.isoformat())
        _time.sleep(max(0.0, (t - now_et()).total_seconds()))
        for attempt in range(RETRIES + 1):
            if run_once(cfg):
                break
            if attempt < RETRIES:
                log.info("session bar not available yet; retrying in %d min", RETRY_MINUTES)
                _time.sleep(RETRY_MINUTES * 60)
