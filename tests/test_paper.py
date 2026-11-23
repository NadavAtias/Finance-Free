from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ff.api.app import create_app
from ff.backtest.runner import prepare
from ff.markets.calendar import ET, is_month_end, is_session, last_final_session, next_session
from ff.paper.config import PaperConfig, load_paper_config
from ff.paper.db import PaperDB
from ff.paper.loop import reset_breaker, run_paper

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp: Path, sub: str = "s") -> PaperConfig:
    c = load_paper_config(ROOT / "config" / "paper.yaml")
    c.provider = "synthetic"
    c.start = "2020-01-02"
    c.cash_rate_fixed = 0.02
    c.state_dir = str(tmp / sub / "state")
    c.data_dir = str(tmp / "data")
    c.runs_dir = str(tmp / "runs")
    return c


def _at(y: int, m: int, d: int, hh: int = 18) -> datetime:
    return datetime(y, m, d, hh, 0, tzinfo=ET)


# --- calendar ---------------------------------------------------------------------
def test_nyse_calendar_holidays_and_month_end() -> None:
    assert not is_session(pd.Timestamp("2026-11-26"))  # Thanksgiving
    assert not is_session(pd.Timestamp("2026-04-03"))  # Good Friday
    assert not is_session(pd.Timestamp("2026-07-03"))  # July 4 observed
    assert is_session(pd.Timestamp("2026-11-27"))
    assert next_session(pd.Timestamp("2026-11-25")) == pd.Timestamp("2026-11-27")
    assert is_month_end(pd.Timestamp("2026-11-30"))
    assert not is_month_end(pd.Timestamp("2026-11-27"))


def test_todays_bar_counts_only_after_the_close() -> None:
    assert last_final_session(_at(2026, 11, 23, 15)) == pd.Timestamp("2026-11-20")
    assert last_final_session(_at(2026, 11, 23, 17)) == pd.Timestamp("2026-11-23")
    assert last_final_session(_at(2026, 11, 28, 12)) == pd.Timestamp("2026-11-27")


# --- loop ---------------------------------------------------------------------------
def test_incremental_runs_match_one_shot_and_rerun_is_a_noop(tmp_path: Path) -> None:
    a = _cfg(tmp_path, "a")
    r1 = run_paper(a, now=_at(2020, 3, 16), notify=False)
    r2 = run_paper(a, now=_at(2020, 6, 30), notify=False)
    r3 = run_paper(a, now=_at(2020, 6, 30), notify=False)
    b = _cfg(tmp_path, "b")
    one = run_paper(b, now=_at(2020, 6, 30), notify=False)
    assert r1.new_sessions > 0 and r2.new_sessions > 0 and r3.new_sessions == 0
    assert [s.equity for s in r2.sleeves] == pytest.approx([s.equity for s in one.sleeves])
    da, db_ = PaperDB(a.db_path), PaperDB(b.db_path)
    q = "SELECT sleeve, date, equity FROM equity ORDER BY sleeve, date"
    assert da.rows(q) == db_.rows(q)
    q = "SELECT id, ticker, direction, shares FROM recommendations ORDER BY id"
    assert da.rows(q) == db_.rows(q)


def test_first_session_creates_trade_tickets_with_stops(tmp_path: Path) -> None:
    c = _cfg(tmp_path)
    run_paper(c, now=_at(2020, 1, 2), notify=False)
    recs = PaperDB(c.db_path).rows("SELECT * FROM recommendations")
    assert recs, "first session should invest"
    sys_buys = [r for r in recs if r["sleeve"] == "track_a_systematic"]
    assert sys_buys and all(r["direction"] == "BUY" and r["stop"] for r in sys_buys)
    core = {r["ticker"] for r in recs if r["sleeve"] == "core_60_40"}
    assert core == {"SPY", "IEF"}
    # the next session fills them, and the tickets expire
    run_paper(c, now=_at(2020, 1, 3), notify=False)
    db = PaperDB(c.db_path)
    assert db.rows("SELECT COUNT(*) AS n FROM positions")[0]["n"] > 0
    left = db.rows("SELECT status FROM recommendations WHERE session = '2020-01-02'")
    assert {r["status"] for r in left} == {"expired"}


def test_kill_switch_blocks_decisions(tmp_path: Path) -> None:
    c = _cfg(tmp_path)
    c.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    c.kill_switch_path.write_text("on")
    run_paper(c, now=_at(2020, 2, 3), notify=False)
    db = PaperDB(c.db_path)
    assert db.rows("SELECT COUNT(*) AS n FROM recommendations")[0]["n"] == 0
    assert db.rows("SELECT COUNT(*) AS n FROM positions")[0]["n"] == 0
    assert any("Kill switch" in e["detail"] for e in db.rows("SELECT detail FROM events"))


def test_dividend_adjustment_credits_cash(tmp_path: Path) -> None:
    c = _cfg(tmp_path)
    prep = prepare(c.as_run_config(), refresh=False)
    run_paper(c, now=_at(2020, 1, 10), prep=prep, notify=False)
    db = PaperDB(c.db_path)
    held = db.rows("SELECT * FROM positions WHERE sleeve = 'core_60_40' AND ticker = 'IEF'")[0]
    last = pd.Timestamp(db.get_json("meta")["last_session"])
    eq_before = db.rows(
        "SELECT equity FROM equity WHERE sleeve = 'core_60_40' ORDER BY date DESC LIMIT 1"
    )[0]["equity"]
    # The data source pays a 1% dividend tomorrow: history up to `last` is scaled by 0.99.
    bars = prep.data._bars["IEF"] if hasattr(prep.data, "_bars") else None
    assert bars is not None
    adj = bars.copy()
    cols = ["open", "high", "low", "close"]
    adj.loc[adj.index <= last, cols] *= 0.99
    prep.data._bars["IEF"] = adj
    prep.data.__init__(prep.data._bars)  # rebuild lookups
    run_paper(c, now=_at(2020, 1, 13), prep=prep, notify=False)
    ev = db.rows(
        "SELECT * FROM events WHERE detail LIKE 'Dividend credited%' AND strategy = 'core_60_40'"
    )
    assert len(ev) == 1
    credit = json.loads(ev[0]["extra"])["price"]
    assert credit == pytest.approx(held["shares"] * held["last"] * 0.01, rel=1e-3)
    assert eq_before > 0


def test_breaker_reset_by_hand(tmp_path: Path) -> None:
    c = _cfg(tmp_path)
    run_paper(c, now=_at(2020, 1, 10), notify=False)
    assert "reset by hand" in reset_breaker(c, "track_a_systematic")
    with pytest.raises(KeyError):
        reset_breaker(c, "nope")


# --- API ----------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    c = _cfg(tmp_path)
    run_paper(c, now=_at(2020, 4, 30), notify=False)
    return TestClient(create_app(c, ROOT / "dashboard"))


def _keys_match(mock: Any, live: Any, where: str) -> list[str]:
    """Every key the UI's mock defines must exist in the live response."""
    errs: list[str] = []
    if isinstance(mock, dict):
        if not isinstance(live, dict):
            return [f"{where}: expected object"]
        for k, v in mock.items():
            if k not in live:
                errs.append(f"{where}.{k} missing")
            elif live[k] is not None:
                errs += _keys_match(v, live[k], f"{where}.{k}")
    elif isinstance(mock, list) and mock and isinstance(live, list) and live:
        errs += _keys_match(mock[0], live[0], f"{where}[0]")
    return errs


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_api_matches_dashboard_contract(client: TestClient) -> None:
    shapes = json.loads(
        subprocess.run(
            ["node", str(ROOT / "tests" / "js" / "mock_shapes.js")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    errs: list[str] = []
    for path, shape in shapes.items():
        if path == "/api/signals":
            continue  # Phase 4
        r = client.get(path)
        assert r.status_code == 200, path
        errs += _keys_match(shape, r.json(), path)
    assert not errs, errs


def test_api_actions(client: TestClient) -> None:
    recs = client.get("/api/recommendations").json()
    assert recs
    rid = recs[0]["id"]
    assert client.post(f"/api/recommendations/{rid}/taken").json() == {"ok": True}
    assert rid not in {r["id"] for r in client.get("/api/recommendations").json()}
    assert client.get("/api/journal").json()[0]["type"] == "manual"
    assert client.post("/api/recommendations/nope/skip").status_code == 404

    assert client.post("/api/kill-switch", json={"engaged": True}).json() == {"engaged": True}
    assert client.get("/api/summary").json()["kill_switch"] is True
    assert client.post("/api/kill-switch", json={"engaged": False}).json() == {"engaged": False}

    alerts = client.get("/api/alerts").json()
    assert alerts
    client.post(f"/api/alerts/{alerts[0]['id']}/read")
    assert client.get("/api/alerts").json()[0]["read"] is True

    s = client.get("/api/alerts/settings").json()
    s["telegram"]["chat_id"] = "123"
    client.put("/api/alerts/settings", json=s)
    assert client.get("/api/alerts/settings").json()["telegram"]["chat_id"] == "123"


def test_dashboard_is_served_in_live_mode(client: TestClient) -> None:
    html = client.get("/").text
    assert "window.FF_MODE = 'live'" in html
    assert client.get("/js/api.js").status_code == 200


def test_overview_series_start_at_zero(client: TestClient) -> None:
    ov = client.get("/api/overview").json()
    for s in ov["series"]:
        assert len(s["values"]) == len(ov["dates"])
        assert s["values"][0] == pytest.approx(0.0, abs=1e-9)
    ids = [p["id"] for p in ov["portfolios"]]
    assert ids == ["track_a", "track_b", "conviction"]
