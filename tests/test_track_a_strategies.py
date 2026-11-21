from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ff.data.view import MarketData
from ff.strategies.base import Context, build_strategy
from tests.conftest import make_bars


def _ctx(bars: dict[str, pd.DataFrame], i: int, month_end: bool = True) -> Context:
    d = next(iter(bars.values())).index[i]
    return Context(d, MarketData(bars).view(d), 100_000, {}, month_end, False)


def _noisy(trend: float, n: int = 400, seed: int = 1, vol: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(trend + vol * rng.standard_normal(n)))


def _universe() -> dict[str, pd.DataFrame]:
    return {
        "A": make_bars(_noisy(0.004, seed=1)),
        "B": make_bars(_noisy(0.0025, seed=2)),
        "C": make_bars(_noisy(0.0005, seed=3)),
        "D": make_bars(_noisy(-0.002, seed=4)),
    }


def test_xs_momentum_holds_top_ranked_with_positive_momentum() -> None:
    bars = _universe()
    strat = build_strategy("xs_momentum", "m", list(bars), {"top_n": 2})
    out = {i.symbol: i for i in strat.on_bar(_ctx(bars, 399)) or []}
    assert out["A"].target_weight > 0 and out["B"].target_weight > 0
    assert out["C"].target_weight == 0 and out["D"].target_weight == 0
    assert out["A"].stop_price is not None and out["A"].stop_price < bars["A"]["close"].iloc[-1]
    assert "top 2" in out["C"].rationale.summary


def test_xs_momentum_goes_to_cash_when_everything_falls() -> None:
    bars = {s: make_bars(_noisy(-0.002, seed=i)) for i, s in enumerate("XYZ")}
    strat = build_strategy("xs_momentum", "m", list(bars), {"top_n": 2})
    out = strat.on_bar(_ctx(bars, 399)) or []
    assert out and all(i.target_weight == 0 for i in out)
    assert all("below cash" in i.rationale.summary or "outside" in i.rationale.summary for i in out)


def test_xs_momentum_skips_recent_month() -> None:
    # Strong 12-month winner that crashed in the last few days still ranks first:
    # the 12-1 window ignores the most recent ``skip_days``.
    a = _noisy(0.003, seed=1)
    a[-10:] = a[-11] * 0.7
    bars = {"A": make_bars(a), "B": make_bars(_noisy(0.001, seed=2))}
    strat = build_strategy("xs_momentum", "m", list(bars), {"top_n": 1})
    out = {i.symbol: i for i in strat.on_bar(_ctx(bars, 399)) or []}
    assert out["A"].target_weight > 0 and out["B"].target_weight == 0


def test_risk_parity_weights_inverse_to_volatility() -> None:
    bars = {
        "LOW": make_bars(_noisy(0.0, seed=1, vol=0.005)),
        "HIGH": make_bars(_noisy(0.0, seed=2, vol=0.02)),
    }
    strat = build_strategy("risk_parity", "rp", list(bars), {})
    out = {i.symbol: i for i in strat.on_bar(_ctx(bars, 399)) or []}
    ratio = out["LOW"].target_weight / out["HIGH"].target_weight
    assert 3 < ratio < 5  # vol ratio is ~4
    assert all(i.stop_price is not None for i in out.values())


def test_risk_parity_only_rebalances_month_end() -> None:
    bars = _universe()
    strat = build_strategy("risk_parity", "rp", list(bars), {})
    assert strat.on_bar(_ctx(bars, 399, month_end=False)) is None


def test_ensemble_is_weighted_sum_of_members() -> None:
    bars = _universe()
    members = [
        {"kind": "xs_momentum", "weight": 1, "params": {"top_n": 2}},
        {"kind": "risk_parity", "weight": 3},
    ]
    ens = build_strategy("ensemble", "e", list(bars), {"members": members})
    mom = build_strategy("xs_momentum", "m", list(bars), {"top_n": 2})
    rp = build_strategy("risk_parity", "rp", list(bars), {})
    ctx = _ctx(bars, 399)
    got = {i.symbol: i for i in ens.on_bar(ctx) or []}
    wm = {i.symbol: i for i in mom.on_bar(ctx) or []}
    wr = {i.symbol: i for i in rp.on_bar(ctx) or []}
    for s in bars:
        expect = 0.25 * wm[s].target_weight + 0.75 * wr[s].target_weight
        assert got[s].target_weight == pytest.approx(expect)
    # the combined stop is the widest member stop
    stops = [x.stop_price for x in (wm["A"], wr["A"]) if x.stop_price is not None]
    assert got["A"].stop_price == pytest.approx(min(stops))
    assert ens.warmup == max(mom.warmup, rp.warmup)


def test_ensemble_keeps_last_member_book_between_rebalances() -> None:
    bars = _universe()
    members = [{"kind": "risk_parity"}, {"kind": "buy_and_hold", "params": {"symbols": ["A"]}}]
    ens = build_strategy("ensemble", "e", list(bars), {"members": members})
    first = ens.on_bar(_ctx(bars, 398))
    assert first is not None
    assert ens.on_bar(_ctx(bars, 399, month_end=False)) is None


def test_ensemble_requires_members() -> None:
    with pytest.raises(ValueError):
        build_strategy("ensemble", "e", ["A"], {})
