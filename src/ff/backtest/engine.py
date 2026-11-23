"""Event-driven daily backtester.

Per session ``t`` the loop is:

1. settle cash whose settlement date has arrived (T+1)
2. fill market-on-open orders created at ``t-1``'s close, at ``t``'s open
3. trigger protective stops against ``t``'s range
4. mark to market at ``t``'s close; update risk state (drawdown, daily loss)
5. kill switch check
6. strategy sees data up to ``t``'s close only and returns intents
7. risk engine approves / resizes; orders are queued for ``t+1``'s open

The same loop, with a live clock and feed instead of historical replay, is the
paper-trading loop in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ff.core.audit import AuditLog
from ff.core.types import Intent, Order, OrderType, Rationale, Side
from ff.data.view import MarketData
from ff.execution.broker import SimBroker
from ff.execution.portfolio import ClosedTrade, Portfolio
from ff.markets.us import USMarket
from ff.risk.engine import RiskEngine, RiskState
from ff.signals.base import SignalStore
from ff.strategies.base import Context, Strategy


@dataclass
class BacktestResult:
    strategy_id: str
    equity: pd.Series
    gross_exposure: pd.Series
    closed_trades: list[ClosedTrade]
    recommendations: list[Intent]
    recommendation_date: pd.Timestamp | None
    final_weights: dict[str, float]
    risk_state: RiskState
    commissions: float
    turnover_annual: float
    good_faith_violations: int
    audit: AuditLog
    interest_earned: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class StepOutput:
    """What one session produced: marks, and (on rebalance days) the approved
    target book plus the next-open orders it created."""

    date: pd.Timestamp
    equity: float
    gross_exposure: float
    approved: list[Intent] | None = None
    orders: list[Order] = field(default_factory=list)
    kill_switch: bool = False


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        data: MarketData,
        market: USMarket,
        risk: RiskEngine,
        initial_capital: float,
        audit: AuditLog,
        reference_symbol: str = "SPY",
        min_trade_weight: float = 0.005,
        cash_rate: pd.Series | None = None,
        signals: SignalStore | None = None,
    ) -> None:
        """``cash_rate``: annualized yield on idle cash (decimal), indexed by date.

        Interest for the night between sessions t-1 and t uses the rate known at
        t-1's close, so it carries no look-ahead.
        """
        self.strategy = strategy
        self.data = data
        self.market = market
        self.risk = risk
        self.audit = audit
        self.portfolio = Portfolio(cash=initial_capital)
        self.broker = SimBroker(market, self.portfolio, data, audit)
        self.reference = reference_symbol
        self.min_trade_weight = min_trade_weight
        self._last_px: dict[str, float] = {}
        self.cash_rate = cash_rate.sort_index() if cash_rate is not None else None
        self.interest_earned = 0.0
        self._was_halted = False
        self.signals = signals

    def _accrue_interest(self, prev: pd.Timestamp, today: pd.Timestamp) -> None:
        if self.cash_rate is None or self.portfolio.cash <= 0:
            return
        known = self.cash_rate.loc[:prev]
        if known.empty:
            return
        rate = max(float(known.iloc[-1]), 0.0)
        interest = self.portfolio.cash * rate * (today - prev).days / 365.0
        self.portfolio.cash += interest
        self.interest_earned += interest

    def _prices(self, date: pd.Timestamp) -> dict[str, float]:
        for s in self.data.symbols:
            bar = self.data.bar(s, date)
            if bar is not None:
                self._last_px[s] = float(bar["close"])
        return dict(self._last_px)

    def step(
        self,
        d: pd.Timestamp,
        i: int,
        prev: pd.Timestamp | None,
        is_month_end: bool,
        is_first_session: bool,
    ) -> StepOutput:
        """Process one session ``d`` (session index ``i``). Used by ``run`` and by
        the paper-trading loop, which calls it once per new session."""
        if prev is not None:
            self._accrue_interest(prev, d)
        self.portfolio.settle(i)
        self.broker.process_open(d, i)
        self.broker.process_stops(d, i)

        px = self._prices(d)
        eq = self.portfolio.equity(px)
        weights = self.portfolio.weights(px)
        out = StepOutput(d, eq, sum(weights.values()))
        self.risk.on_close(d, i, eq)

        if self.risk.kill_switch_active():
            self.broker.cancel_all()
            self.audit.write("kill_switch", d, action="all orders cancelled, no decisions")
            out.kill_switch = True
            return out

        ctx = Context(
            date=d,
            data=self.data.view(d),
            equity=eq,
            weights=weights,
            is_month_end=is_month_end,
            is_first_session=is_first_session,
            signals=self.signals.view(d) if self.signals is not None else None,
        )
        intents = self.strategy.on_bar(ctx)

        halted = self.risk.state.halted
        if halted and not self._was_halted:
            # Breaker just tripped: flatten everything at next open.
            intents = [
                Intent(s, 0.0, 1.0, None, Rationale("Drawdown breaker: flatten")) for s in weights
            ]
        self._was_halted = halted
        if intents is None:
            return out

        approved = self.risk.apply(d, intents, weights, px)
        for it in approved:
            self.audit.write(
                "decision",
                d,
                strategy=self.strategy.id,
                symbol=it.symbol,
                target_weight=round(it.target_weight, 5),
                stop=it.stop_price,
                rationale=it.rationale.as_text(),
            )
        self._rebalance(d, approved, weights, px, eq)
        out.approved = approved
        out.orders = [
            o
            for o in self.broker.open_orders()
            if o.type is OrderType.MARKET_ON_OPEN and o.created == d
        ]
        return out

    def run(self, start: pd.Timestamp, end: pd.Timestamp) -> BacktestResult:
        sessions = self.data.sessions(self.reference)
        sessions = sessions[(sessions >= start) & (sessions <= end)]
        if len(sessions) < 2:
            raise ValueError("not enough sessions in the requested range")
        equity_pts: dict[pd.Timestamp, float] = {}
        gross_pts: dict[pd.Timestamp, float] = {}
        recs: list[Intent] = []
        rec_date: pd.Timestamp | None = None

        for i, d in enumerate(sessions):
            is_last = i == len(sessions) - 1
            out = self.step(
                d,
                i,
                sessions[i - 1] if i > 0 else None,
                is_month_end=not is_last and sessions[i + 1].month != d.month,
                is_first_session=i == 0,
            )
            equity_pts[d] = out.equity
            gross_pts[d] = out.gross_exposure
            if out.approved is not None:
                recs, rec_date = out.approved, d

        last = sessions[-1]
        years = max((last - sessions[0]).days / 365.25, 1e-9)
        avg_eq = pd.Series(equity_pts).mean()
        return BacktestResult(
            strategy_id=self.strategy.id,
            equity=pd.Series(equity_pts, name=self.strategy.id),
            gross_exposure=pd.Series(gross_pts, name=self.strategy.id),
            closed_trades=self.portfolio.closed_trades,
            recommendations=recs,
            recommendation_date=rec_date,
            final_weights=self.portfolio.weights(self._last_px),
            risk_state=self.risk.state,
            commissions=self.portfolio.commissions,
            turnover_annual=self.portfolio.traded_notional / avg_eq / years / 2,
            good_faith_violations=self.portfolio.good_faith_violations,
            audit=self.audit,
            interest_earned=self.interest_earned,
        )

    def _rebalance(
        self,
        d: pd.Timestamp,
        approved: list[Intent],
        weights: dict[str, float],
        px: dict[str, float],
        equity: float,
    ) -> None:
        self.broker.cancel_all(OrderType.MARKET_ON_OPEN)
        targets = {it.symbol: it for it in approved}
        # Anything held but absent from the target book is exited.
        for sym in weights:
            if sym not in targets:
                targets[sym] = Intent(sym, 0.0, 1.0, None, Rationale("Not in target book"))
        for sym, it in targets.items():
            price = px.get(sym)
            if price is None:
                continue
            held = self.portfolio.quantity(sym)
            tgt_qty = int(it.target_weight * equity // price)
            delta = tgt_qty - held
            small = abs(delta) * price < self.min_trade_weight * equity
            if delta != 0 and not (small and tgt_qty > 0):
                side = Side.BUY if delta > 0 else Side.SELL
                self.broker.submit(
                    Order(
                        sym,
                        side,
                        abs(delta),
                        OrderType.MARKET_ON_OPEN,
                        d,
                        reason=it.rationale.summary,
                    )
                )
            existing = self.broker.stop_for(sym)
            if existing:
                self.broker.cancel(existing.id)
            if tgt_qty > 0 and it.stop_price is not None:
                self.broker.submit(
                    Order(
                        sym,
                        Side.SELL,
                        tgt_qty,
                        OrderType.STOP,
                        d,
                        stop_price=it.stop_price,
                        reason="protective stop",
                    )
                )
