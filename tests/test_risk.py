from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ff.backtest.engine import BacktestEngine
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig
from ff.core.types import Intent, Rationale
from ff.data.view import MarketData
from ff.markets.us import USMarket
from ff.risk.engine import RiskEngine
from ff.strategies.base import Context, Strategy
from tests.conftest import make_bars

D = pd.Timestamp("2024-01-02")


def _intent(sym: str, w: float, stop: float | None) -> Intent:
    return Intent(sym, w, 0.8, stop, Rationale("test"))


def test_max_risk_per_trade_caps_weight(risk_cfg: RiskConfig) -> None:
    eng = RiskEngine(risk_cfg, AuditLog())
    # stop 10% below -> 1% risk allows at most 10% weight
    (out,) = eng.apply(D, [_intent("X", 0.20, 90.0)], {}, {"X": 100.0})
    assert out.target_weight == pytest.approx(0.10)
    assert eng.state.decisions[0].rule == "max_risk_per_trade"


def test_position_cap(risk_cfg: RiskConfig) -> None:
    eng = RiskEngine(risk_cfg, AuditLog())
    (out,) = eng.apply(D, [_intent("X", 0.50, 99.0)], {}, {"X": 100.0})
    assert out.target_weight == pytest.approx(risk_cfg.max_position_weight)


def test_long_without_stop_is_rejected(risk_cfg: RiskConfig) -> None:
    eng = RiskEngine(risk_cfg, AuditLog())
    (out,) = eng.apply(D, [_intent("X", 0.10, None)], {}, {"X": 100.0})
    assert out.target_weight == 0


def test_gross_exposure_scaled(risk_cfg: RiskConfig) -> None:
    eng = RiskEngine(risk_cfg, AuditLog())
    intents = [_intent(s, 0.25, 99.0) for s in "ABCDEF"]
    out = eng.apply(D, intents, {}, dict.fromkeys("ABCDEF", 100.0))
    assert sum(i.target_weight for i in out) == pytest.approx(1.0)


def test_daily_loss_blocks_increases_but_allows_cuts(risk_cfg: RiskConfig) -> None:
    eng = RiskEngine(risk_cfg, AuditLog())
    eng.on_close(D, 0, 100_000)
    eng.on_close(D + pd.Timedelta(days=1), 1, 97_000)  # -3% day
    assert eng.state.block_new_risk
    up, down = eng.apply(
        D,
        [_intent("X", 0.2, 99.0), _intent("Y", 0.05, 99.0)],
        {"X": 0.1, "Y": 0.15},
        {"X": 100.0, "Y": 100.0},
    )
    assert up.target_weight == pytest.approx(0.1)
    assert down.target_weight == pytest.approx(0.05)


def test_drawdown_breaker_halts_and_rearms(risk_cfg: RiskConfig) -> None:
    eng = RiskEngine(risk_cfg, AuditLog(), reset_after_sessions=5)
    eng.on_close(D, 0, 100_000)
    eng.on_close(D, 1, 84_000)
    assert eng.state.halted
    (out,) = eng.apply(D, [_intent("X", 0.1, 99.0)], {}, {"X": 100.0})
    assert out.target_weight == 0
    eng.on_close(D, 6, 84_000)
    assert not eng.state.halted


class _AlwaysLong(Strategy):
    def on_bar(self, ctx: Context) -> list[Intent] | None:
        px = ctx.data.close("X")
        assert px is not None
        return [_intent("X", 0.2, px * 0.95)]


def test_kill_switch_stops_all_orders(risk_cfg: RiskConfig, zero_cost_market: USMarket) -> None:
    Path(risk_cfg.kill_switch_file).write_text("halt")
    bars = make_bars([100 + i for i in range(30)])
    audit = AuditLog()
    eng = BacktestEngine(
        _AlwaysLong("a", ["X"]),
        MarketData({"X": bars}),
        zero_cost_market,
        RiskEngine(risk_cfg, audit),
        10_000,
        audit,
        reference_symbol="X",
    )
    res = eng.run(bars.index[0], bars.index[-1])
    assert not audit.of_kind("order")
    assert res.equity.iloc[-1] == pytest.approx(10_000)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.floats(0, 2), st.floats(0.5, 99.9)), min_size=1, max_size=12))
def test_property_no_intent_exceeds_caps(pairs: list[tuple[float, float]]) -> None:
    cfg = RiskConfig(kill_switch_file="/nonexistent/KILL")
    eng = RiskEngine(cfg, AuditLog())
    syms = [f"S{i}" for i in range(len(pairs))]
    intents = [_intent(s, w, stop) for s, (w, stop) in zip(syms, pairs, strict=True)]
    out = eng.apply(D, intents, {}, dict.fromkeys(syms, 100.0))
    assert sum(i.target_weight for i in out) <= cfg.max_gross_exposure + 1e-9
    for i in out:
        assert i.target_weight <= cfg.max_position_weight + 1e-9
        assert i.stop_price is not None
        stop_pct = (100.0 - i.stop_price) / 100.0
        assert i.target_weight * stop_pct <= cfg.max_risk_per_trade + 1e-9
