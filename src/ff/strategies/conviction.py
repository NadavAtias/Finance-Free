"""System Conviction: the system's own stock picks from all signal sources.

Every session it computes one score per stock:

    score = sum over pick sources of  weight_s x value_s x decay

- ``value_s`` is the source's latest active record for the stock, in [-1, 1].
- ``decay`` falls linearly from 1 to 0 over the source's time-to-live.
- ``weight_s`` comes from the source's own scored track record, measured only
  on outcomes already known (``SignalView.scores``), shrunk toward zero and
  normalized to sum to 1. A source without proven value gets zero, so with no
  evidence at all the system makes no picks. When the summed evidence is weak
  (below ``FULL_TRUST``), every score is scaled down with it. Nothing here is
  hand-weighted.

Rules (long only; a cash account with no shorting):
- Buy when the score reaches ``entry_score``, best scores first, up to
  ``max_positions`` and ``max_per_sector``; not within ``cooldown`` sessions
  of leaving the same stock.
- Each buy has an ATR stop (``atr_stop_mult`` x ATR) and a take-profit at
  ``reward_risk`` x the stop distance; the risk engine sizes it so the stop
  costs at most 1% of the book.
- Sell when the score drops below ``exit_score``, the close reaches the
  take-profit, or ``max_hold`` sessions pass (the sources' edge fades).
- The market mood (macro source) shrinks new positions in risk-off regimes,
  down to ``regime_floor`` of full size, in proportion to how much its own
  record has earned trust.

Defaults are round numbers set before any backtest, not fitted.
"""

from __future__ import annotations

from typing import Any

from ff.analytics.indicators import atr
from ff.core.types import Intent, Rationale
from ff.core.universe import load_universe
from ff.strategies.base import Context, Strategy, register

#: summed source weight at which scores are used at full strength. A monthly
#: information ratio of 0.2 (about 0.7 a year) with long history is a strong,
#: realistic signal; weaker total evidence scales every score down.
FULL_TRUST = 0.2


@register("system_conviction")
class SystemConviction(Strategy):
    tags = frozenset({"conviction"})
    needs_signals = True

    def __init__(
        self,
        id: str,
        universe: list[str],
        max_positions: int = 10,
        entry_score: float = 0.30,
        exit_score: float = 0.0,
        atr_days: int = 20,
        atr_stop_mult: float = 2.5,
        reward_risk: float = 2.0,
        max_hold: int = 63,
        max_per_sector: int = 3,
        cooldown: int = 21,
        regime_floor: float = 0.5,
        universe_file: str | None = None,
    ) -> None:
        super().__init__(
            id,
            universe,
            max_positions=max_positions,
            entry_score=entry_score,
            exit_score=exit_score,
            atr_days=atr_days,
            atr_stop_mult=atr_stop_mult,
            reward_risk=reward_risk,
            max_hold=max_hold,
            max_per_sector=max_per_sector,
            cooldown=cooldown,
            regime_floor=regime_floor,
            universe_file=universe_file,
        )
        self.max_positions = max_positions
        self.entry_score = entry_score
        self.exit_score = exit_score
        self.atr_days = atr_days
        self.atr_stop_mult = atr_stop_mult
        self.reward_risk = reward_risk
        self.max_hold = max_hold
        self.max_per_sector = max_per_sector
        self.cooldown = cooldown
        self.regime_floor = regime_floor
        meta = load_universe(universe_file) if universe_file else {}
        self.sector = {s: meta[s][1] if s in meta else "Other" for s in universe}
        self.warmup = atr_days + 1
        self._held: dict[str, dict[str, Any]] = {}
        self._cool: dict[str, int] = {}  # symbol -> session index it may be bought again
        self.last_board: dict[str, Any] = {}

    # ------------------------------------------------------------------------------
    def board(self, ctx: Context) -> dict[str, Any]:
        """Scores of every stock, the parts behind them and the source weights."""
        sv = ctx.signals
        assert sv is not None
        scores = sv.scores()
        pick = {s: sc for s, sc in scores.items() if sv.sources[s].role == "pick"}
        total = sum(sc.weight for sc in pick.values())
        weights = {s: (sc.weight / total if total > 0 else 0.0) for s, sc in pick.items()}
        # Weak total evidence shrinks every score, so fewer stocks clear the bar.
        trust = min(1.0, total / FULL_TRUST)
        parts: dict[str, dict[str, tuple[float, str]]] = {}
        for src, w in weights.items():
            if w <= 0:
                continue
            act = sv.active(src)
            act = act[act["symbol"].isin(self.universe)]
            for _, r in act.drop_duplicates("symbol", keep="last").iterrows():
                parts.setdefault(r["symbol"], {})[src] = (
                    trust * w * float(r["value"]) * float(r["decay"]),
                    str(r["detail"]),
                )
        comp = {sym: sum(c for c, _ in p.values()) for sym, p in parts.items()}
        regime, regime_text, scale = 0.0, "", 1.0
        for src, info in sv.sources.items():
            if info.role != "exposure":
                continue
            rec = sv.latest_market(src)
            sc = scores.get(src)
            if rec is None or sc is None:
                continue
            regime, regime_text = float(rec["value"]), str(rec["detail"])
            trust = {"active": 1.0, "probation": 0.5}.get(sc.status, 0.0)
            trust *= sc.n_months / (sc.n_months + 24) if sc.n_months else 0.0
            scale = 1.0 - (1.0 - self.regime_floor) * trust * max(0.0, -regime)
        return {
            "scores": comp,
            "parts": parts,
            "weights": weights,
            "trust": trust,
            "status": {s: sc.status for s, sc in scores.items()},
            "regime": regime,
            "regime_text": regime_text,
            "scale": scale,
        }

    def _why(self, sym: str, b: dict[str, Any], ctx: Context) -> str:
        sv = ctx.signals
        assert sv is not None
        bits = [
            f"{sv.sources[src].name} ({b['weights'][src]:.0%} of the score, adds {c:+.2f}): {d}"
            for src, (c, d) in sorted(b["parts"].get(sym, {}).items(), key=lambda kv: -kv[1][0])
        ]
        return ". ".join(bits)

    def on_bar(self, ctx: Context) -> list[Intent] | None:
        if ctx.signals is None:
            raise ValueError("system_conviction needs signals: add a signals block to the config")
        idx = ctx.signals.idx
        b = self.board(ctx)
        self.last_board = b
        comp: dict[str, float] = b["scores"]

        # Forget positions that are gone (stop hit, or the buy never filled).
        for sym in list(self._held):
            if ctx.weights.get(sym, 0.0) <= 0 and self._held[sym]["idx"] < idx:
                self._held.pop(sym)
                self._cool[sym] = idx + self.cooldown

        changed = False
        exits: dict[str, str] = {}
        for sym, h in self._held.items():
            px = ctx.data.close(sym)
            s = comp.get(sym, 0.0)
            if s < self.exit_score:
                exits[sym] = f"system score fell to {s:+.2f} (exit below {self.exit_score:+.2f})"
            elif px is not None and h["tp"] is not None and px >= h["tp"]:
                exits[sym] = f"take-profit reached: close {px:.2f} >= {h['tp']:.2f}"
            elif idx - h["idx"] >= self.max_hold:
                exits[sym] = f"held {self.max_hold} sessions, the signals' edge has faded"
        for sym in exits:
            self._held.pop(sym)
            self._cool[sym] = idx + self.cooldown
            changed = True

        per_sector: dict[str, int] = {}
        for sym in self._held:
            per_sector[self.sector.get(sym, "Other")] = (
                per_sector.get(self.sector.get(sym, "Other"), 0) + 1
            )
        entries: list[str] = []
        ranked = sorted(comp.items(), key=lambda kv: -kv[1])
        for sym, s in ranked:
            if s < self.entry_score or len(self._held) + len(entries) >= self.max_positions:
                break
            sec = self.sector.get(sym, "Other")
            if sym in self._held or self._cool.get(sym, -1) > idx or ctx.data.close(sym) is None:
                continue
            if per_sector.get(sec, 0) >= self.max_per_sector:
                continue
            hist = ctx.data.history(sym, self.atr_days + 1)
            if len(hist) < self.atr_days + 1:
                continue
            per_sector[sec] = per_sector.get(sec, 0) + 1
            entries.append(sym)

        if not entries and not changed:
            return None

        book: list[Intent] = []
        for sym, h in self._held.items():
            book.append(
                Intent(
                    sym,
                    ctx.weights.get(sym, h["weight"]),
                    h["conf"],
                    h["stop"],
                    h["rationale"],
                    take_profit=h["tp"],
                )
            )
        weight = b["scale"] / self.max_positions
        regime_note = (
            f" Market mood {b['regime']:+.2f} ({b['regime_text']}); new positions sized at "
            f"{b['scale']:.0%}."
            if b["regime_text"]
            else ""
        )
        for sym in entries:
            px = float(ctx.data.close(sym) or 0.0)
            a = atr(ctx.data.history(sym, self.atr_days + 1), self.atr_days)
            stop = px - self.atr_stop_mult * a
            tp = px + self.reward_risk * (px - stop)
            s = comp[sym]
            conf = min(0.95, 0.5 + 0.5 * min(1.0, s))
            rat = Rationale(
                summary=f"System Conviction buy {sym}: score {s:+.2f} (buys from "
                f"{self.entry_score:+.2f}). {self._why(sym, b, ctx)}.{regime_note}",
                risk=f"stop {stop:.2f} ({stop / px - 1:+.1%}, {self.atr_stop_mult:g}x ATR); "
                f"take-profit {tp:.2f} ({tp / px - 1:+.1%}); a single stock can gap through "
                "its stop on news",
                invalidation=f"score falls below {self.exit_score:+.2f}, close under {stop:.2f}, "
                f"or {self.max_hold} sessions pass without reaching {tp:.2f}",
            )
            self._held[sym] = {
                "idx": idx,
                "stop": stop,
                "tp": tp,
                "weight": weight,
                "conf": conf,
                "rationale": rat,
            }
            book.append(Intent(sym, weight, conf, stop, rat, take_profit=round(tp, 2)))
        for sym, why in exits.items():
            book.append(
                Intent(sym, 0.0, 0.6, None, Rationale(f"System Conviction sell {sym}: {why}"))
            )
        return book
