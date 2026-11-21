from __future__ import annotations

import pandas as pd
import pytest

from ff.core.audit import AuditLog
from ff.core.config import CostConfig
from ff.core.types import Fill, Order, OrderType, Side
from ff.data.view import MarketData
from ff.execution.broker import SimBroker
from ff.execution.portfolio import Portfolio
from ff.markets.us import CostModel, USMarket
from tests.conftest import make_bars


def test_cost_model_is_always_adverse() -> None:
    cm = CostModel(CostConfig(half_spread_bps=1, slippage_bps=2))
    assert cm.fill_price(Side.BUY, 100) == pytest.approx(100.03)
    assert cm.fill_price(Side.SELL, 100) == pytest.approx(99.97)


def _broker(
    bars: pd.DataFrame, market: USMarket, cash: float = 10_000
) -> tuple[SimBroker, Portfolio]:
    pf = Portfolio(cash=cash)
    return SimBroker(market, pf, MarketData({"X": bars}), AuditLog()), pf


def test_market_on_open_fills_at_next_open(zero_cost_market: USMarket) -> None:
    bars = make_bars([100, 110, 120])
    br, pf = _broker(bars, zero_cost_market)
    d0, d1 = bars.index[0], bars.index[1]
    br.submit(Order("X", Side.BUY, 10, OrderType.MARKET_ON_OPEN, d0))
    br.process_open(d1, 1)
    assert pf.quantity("X") == 10
    assert br.fills[0].price == pytest.approx(bars.loc[d1, "open"])  # 100, not 110 close


def test_participation_cap_gives_partial_fill() -> None:
    market = USMarket(
        CostModel(CostConfig(half_spread_bps=0, slippage_bps=0, max_participation=0.01))
    )
    bars = make_bars([10, 10, 10], volume=1_000)  # cap = 10 shares
    br, pf = _broker(bars, market)
    br.submit(Order("X", Side.BUY, 50, OrderType.MARKET_ON_OPEN, bars.index[0]))
    br.process_open(bars.index[1], 1)
    assert pf.quantity("X") == 10
    assert br.open_orders() == []  # remainder cancelled
    assert br.audit.of_kind("partial_cancel")


def test_buy_limited_by_cash(zero_cost_market: USMarket) -> None:
    bars = make_bars([100, 100, 100])
    br, pf = _broker(bars, zero_cost_market, cash=1_050)
    br.submit(Order("X", Side.BUY, 50, OrderType.MARKET_ON_OPEN, bars.index[0]))
    br.process_open(bars.index[1], 1)
    assert pf.quantity("X") == 10
    assert pf.cash >= 0


def test_stop_gap_fills_at_open_and_touch_fills_at_stop(zero_cost_market: USMarket) -> None:
    bars = make_bars([100, 100, 100, 100])
    bars.loc[bars.index[2], ["open", "high", "low", "close"]] = [90, 92, 88, 91]  # gap below
    br, pf = _broker(bars, zero_cost_market)
    pf.apply(Fill(0, "X", Side.BUY, 10, 100, 0, bars.index[0]), 0, 1)
    br.submit(Order("X", Side.SELL, 10, OrderType.STOP, bars.index[0], stop_price=95))
    br.process_stops(bars.index[1], 1)
    assert pf.quantity("X") == 10  # low 99 never touched 95
    br.process_stops(bars.index[2], 2)
    assert pf.quantity("X") == 0
    assert br.fills[-1].price == pytest.approx(90)  # gap: filled at open, worse than stop

    bars2 = make_bars([100, 100, 100])
    bars2.loc[bars2.index[1], ["open", "high", "low", "close"]] = [99, 100, 94, 98]
    br2, pf2 = _broker(bars2, zero_cost_market)
    pf2.apply(Fill(0, "X", Side.BUY, 10, 100, 0, bars2.index[0]), 0, 1)
    br2.submit(Order("X", Side.SELL, 10, OrderType.STOP, bars2.index[0], stop_price=95))
    br2.process_stops(bars2.index[1], 1)
    assert br2.fills[-1].price == pytest.approx(95)


def test_settlement_t_plus_one_and_round_trip_pnl() -> None:
    pf = Portfolio(cash=1_000)
    d = pd.bdate_range("2024-06-03", periods=5)
    pf.apply(Fill(1, "X", Side.BUY, 10, 50, 1.0, d[0]), 0, 1)
    pf.apply(Fill(2, "X", Side.SELL, 5, 60, 1.0, d[1]), 1, 1)
    assert pf.unsettled_cash == pytest.approx(299)
    pf.settle(1)
    assert pf.unsettled_cash == pytest.approx(299)  # settles on session 2
    pf.settle(2)
    assert pf.unsettled_cash == 0
    assert pf.closed_trades == []  # still holding 5
    pf.apply(Fill(3, "X", Side.SELL, 5, 40, 1.0, d[2]), 2, 1)
    (t,) = pf.closed_trades
    assert t.pnl == pytest.approx(-1 + (60 - 50) * 5 - 1 + (40 - 50) * 5 - 1)
    assert t.quantity == 10


def test_good_faith_violation_detected() -> None:
    pf = Portfolio(cash=0)
    d = pd.bdate_range("2024-06-03", periods=3)
    pf.positions.clear()
    pf.cash = 1_000
    pf.unsettled.append((1, 1_000.0))  # all cash unsettled until session 1
    pf.apply(Fill(1, "Y", Side.BUY, 10, 100, 0, d[0]), 0, 1)
    pf.apply(Fill(2, "Y", Side.SELL, 10, 100, 0, d[0]), 0, 1)  # sold same session
    assert pf.good_faith_violations == 1
