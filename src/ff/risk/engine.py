"""Risk engine: a chain of independent rules that approve, shrink or reject intents.

Order of evaluation:
1. Portfolio-level gates (kill switch, drawdown breaker, daily loss limit)
2. Per-intent caps (max risk per trade via stop distance, max position weight)
3. Book-level cap (gross exposure, no leverage in a cash account)

Every change is recorded with the rule's name and reason in the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from ff.core.audit import AuditLog
from ff.core.config import RiskConfig
from ff.core.types import Intent


@dataclass(frozen=True)
class RiskDecision:
    rule: str
    symbol: str
    before: float
    after: float
    reason: str


@dataclass
class RiskState:
    peak_equity: float = 0.0
    last_equity: float | None = None
    halted: bool = False
    halted_at: int | None = None
    block_new_risk: bool = False
    breaker_trips: int = 0
    daily_limit_hits: int = 0
    decisions: list[RiskDecision] = field(default_factory=list)


class RiskEngine:
    def __init__(
        self,
        cfg: RiskConfig,
        audit: AuditLog,
        passthrough: bool = False,
        reset_after_sessions: int | None = 21,
    ) -> None:
        """``passthrough`` skips per-trade caps (benchmarks).

        ``reset_after_sessions``: in backtests the drawdown breaker re-arms after
        this many sessions to emulate a human reviewing and resetting it. Live,
        the reset is manual only (``None``).
        """
        self.cfg = cfg
        self.audit = audit
        self.passthrough = passthrough
        self.reset_after = reset_after_sessions
        self.state = RiskState()

    # --- portfolio-level -----------------------------------------------------
    def kill_switch_active(self) -> bool:
        return Path(self.cfg.kill_switch_file).exists()

    def on_close(self, date: pd.Timestamp, idx: int, equity: float) -> None:
        """Update drawdown and daily-loss state after each session's close."""
        st = self.state
        if (
            st.halted
            and self.reset_after is not None
            and st.halted_at is not None
            and idx - st.halted_at >= self.reset_after
        ):
            st.halted = False
            st.peak_equity = equity
            self.audit.write("risk_reset", date, rule="drawdown_breaker", equity=equity)
        st.peak_equity = max(st.peak_equity, equity)
        dd = 1 - equity / st.peak_equity if st.peak_equity > 0 else 0.0
        if not self.passthrough and not st.halted and dd >= self.cfg.max_drawdown:
            st.halted, st.halted_at = True, idx
            st.breaker_trips += 1
            self.audit.write("risk_halt", date, rule="drawdown_breaker", drawdown=dd)
        st.block_new_risk = False
        if st.last_equity and not self.passthrough:
            day_ret = equity / st.last_equity - 1
            if day_ret <= -self.cfg.daily_loss_limit:
                st.block_new_risk = True
                st.daily_limit_hits += 1
                self.audit.write("risk_block", date, rule="daily_loss_limit", day_return=day_ret)
        st.last_equity = equity

    # --- intent-level ----------------------------------------------------------
    def _log(self, date: pd.Timestamp, d: RiskDecision) -> None:
        self.state.decisions.append(d)
        self.audit.write(
            "risk",
            date,
            rule=d.rule,
            symbol=d.symbol,
            before=d.before,
            after=d.after,
            reason=d.reason,
        )

    def apply(
        self,
        date: pd.Timestamp,
        intents: list[Intent],
        current: dict[str, float],
        prices: dict[str, float],
    ) -> list[Intent]:
        """Return approved intents (target weights) for a full target book."""
        out: list[Intent] = []
        for it in intents:
            w = it.target_weight
            if self.state.halted:
                if w:
                    self._log(
                        date,
                        RiskDecision(
                            "drawdown_breaker",
                            it.symbol,
                            w,
                            0.0,
                            "trading halted after max drawdown",
                        ),
                    )
                w = 0.0
            if not self.passthrough and w > 0:
                w = self._per_trade(date, it, w, prices)
            if self.state.block_new_risk and w > current.get(it.symbol, 0.0):
                held = current.get(it.symbol, 0.0)
                self._log(
                    date,
                    RiskDecision(
                        "daily_loss_limit", it.symbol, w, held, "no new risk after daily loss limit"
                    ),
                )
                w = held
            out.append(replace(it, target_weight=w))
        gross = sum(i.target_weight for i in out)
        cap = self.cfg.max_gross_exposure
        if gross > cap:
            scale = cap / gross
            self._log(
                date, RiskDecision("gross_exposure", "*", gross, cap, f"scaled book by {scale:.3f}")
            )
            out = [replace(i, target_weight=i.target_weight * scale) for i in out]
        return out

    def _per_trade(
        self, date: pd.Timestamp, it: Intent, w: float, prices: dict[str, float]
    ) -> float:
        px = prices.get(it.symbol)
        if it.stop_price is None or px is None or it.stop_price >= px:
            self._log(
                date,
                RiskDecision(
                    "requires_stop",
                    it.symbol,
                    w,
                    0.0,
                    "long intent without a valid stop below price",
                ),
            )
            return 0.0
        stop_pct = (px - it.stop_price) / px
        max_w_risk = self.cfg.max_risk_per_trade / stop_pct
        if w > max_w_risk:
            self._log(
                date,
                RiskDecision(
                    "max_risk_per_trade",
                    it.symbol,
                    w,
                    max_w_risk,
                    f"stop {stop_pct:.1%} away caps loss at "
                    f"{self.cfg.max_risk_per_trade:.1%} of equity",
                ),
            )
            w = max_w_risk
        if w > self.cfg.max_position_weight:
            self._log(
                date,
                RiskDecision(
                    "max_position_weight",
                    it.symbol,
                    w,
                    self.cfg.max_position_weight,
                    "position cap",
                ),
            )
            w = self.cfg.max_position_weight
        return w
