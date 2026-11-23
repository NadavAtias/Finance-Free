"""Command line entry point: ``ff backtest``, ``ff validate``, ``ff fetch``,
``ff paper`` (one paper-trading pass), ``ff daemon`` (always-on scheduler) and
``ff serve`` (dashboard + API)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from ff.backtest.runner import _symbols, copy_config, run_backtest
from ff.core.config import load_config
from ff.data.store import BarStore, make_provider


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so keys stay out of code and shell history."""
    import os

    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _load_dotenv()
    p = argparse.ArgumentParser(prog="ff", description="Finance Free research platform")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest", help="run a backtest from a YAML config")
    b.add_argument("config", type=Path)
    b.add_argument("--provider", help="override the config's data provider")
    b.add_argument("--refresh", action="store_true", help="re-download data")
    v = sub.add_parser("validate", help="walk-forward, sweep, Deflated Sharpe, PBO, blends")
    v.add_argument("config", type=Path)
    v.add_argument("--provider", help="override the config's data provider")
    v.add_argument("--refresh", action="store_true", help="re-download data")
    v.add_argument(
        "--open-holdout",
        action="store_true",
        help="include the locked hold-out period (recorded in the trial registry)",
    )
    f = sub.add_parser("fetch", help="download and cache bars for a config's symbols")
    f.add_argument("config", type=Path)
    f.add_argument("--provider")
    pp = sub.add_parser("paper", help="process new sessions in the paper-trading ledger")
    pp.add_argument("config", type=Path)
    pp.add_argument("--no-refresh", action="store_true", help="use cached data only")
    pp.add_argument("--no-notify", action="store_true", help="don't send Telegram")
    pp.add_argument("--reset-breaker", metavar="SLEEVE", help="reset a tripped drawdown breaker")
    sg = sub.add_parser(
        "signals", help="download every signal source and print how much each one has earned"
    )
    sg.add_argument("config", type=Path)
    sg.add_argument("--no-refresh", action="store_true", help="use cached data only")
    dm = sub.add_parser("daemon", help="run the paper loop after every US close, forever")
    dm.add_argument("config", type=Path)
    sv = sub.add_parser("serve", help="serve the dashboard and its API")
    sv.add_argument("config", type=Path)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--dashboard", type=Path, default=Path("dashboard"))
    args = p.parse_args(argv)

    if args.cmd in ("paper", "daemon", "serve", "signals"):
        return _paper_commands(args)

    cfg = load_config(args.config)
    if args.provider:
        cfg.provider = args.provider
    if args.cmd == "fetch":
        store = BarStore(make_provider(cfg.provider), cfg.data_dir)
        end = pd.Timestamp(cfg.end) if cfg.end else pd.Timestamp.today().normalize()
        bars, issues = store.load(
            _symbols(cfg), pd.Timestamp(cfg.start) - pd.Timedelta(days=550), end, refresh=True
        )
        for s, df in bars.items():
            print(f"{s:6s} {len(df):6d} bars  {df.index.min().date()} .. {df.index.max().date()}")
        print(f"{len(issues)} data-quality flags")
        return 0
    if args.cmd == "validate":
        from ff.backtest.validation import run_validation

        vout, res = run_validation(cfg, open_holdout=args.open_holdout, refresh=args.refresh)
        copy_config(args.config, vout)
        for c in res.checks:
            print(f"{'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}")
        for sid in res.metrics:
            m = res.metrics[sid]
            print(
                f"{sid:34s} CAGR {m['cagr']:6.1%}  vol {m['volatility']:5.1%}  "
                f"Sharpe {m['sharpe']:5.2f}  maxDD {m['max_drawdown']:6.1%}"
            )
        print(f"Report: {vout / 'validation.html'}")
        return 0
    out = run_backtest(cfg, refresh=args.refresh)
    copy_config(args.config, out)
    summary = json.loads((out / "metrics.json").read_text())["metrics"]
    for sid, m in summary.items():
        print(
            f"{sid:28s} CAGR {m['cagr']:6.1%}  vol {m['volatility']:5.1%}  "
            f"Sharpe {m['sharpe']:5.2f}  maxDD {m['max_drawdown']:6.1%}"
        )
    print(f"Report: {out / 'report.html'}")
    return 0


def _paper_commands(args: argparse.Namespace) -> int:
    from ff.paper.config import load_paper_config

    pcfg = load_paper_config(args.config)
    if args.cmd == "serve":
        import uvicorn

        from ff.api.app import create_app

        uvicorn.run(create_app(pcfg, args.dashboard), host=args.host, port=args.port)
        return 0
    if args.cmd == "signals":
        return _signals_report(pcfg, refresh=not args.no_refresh)
    if args.cmd == "daemon":
        from ff.paper.daemon import serve_forever

        serve_forever(pcfg)
        return 0
    from ff.paper.loop import reset_breaker, run_paper

    if args.reset_breaker:
        print(reset_breaker(pcfg, args.reset_breaker))
        return 0
    res = run_paper(pcfg, refresh=not args.no_refresh, notify=not args.no_notify)
    print(f"Processed {res.new_sessions} new session(s); last recorded: {res.last_session}")
    if res.summary:
        print(res.summary)
    if res.telegram:
        print(f"Telegram: {res.telegram}")
    return 0


def _signals_report(pcfg: object, refresh: bool) -> int:
    from ff.backtest.runner import prepare
    from ff.paper.config import PaperConfig
    from ff.paper.signals import conviction_board, scoreboard

    assert isinstance(pcfg, PaperConfig)
    if not pcfg.signals:
        print("No signals block in this config.")
        return 1
    prep = prepare(pcfg.as_run_config(), refresh=refresh)
    assert prep.signals is not None
    last = prep.signals.sessions[-1]
    print(f"Signal sources as of {last.date()} (outcomes known by then only)\n")
    print(
        f"{'source':20s} {'status':13s} {'months':>6s} {'calls':>7s} {'right':>6s} "
        f"{'t':>5s} {'listen':>7s}  data"
    )
    for s in scoreboard(prep, last):
        hit = f"{s['hit_all']:.0%}" if s["hit_all"] is not None else "-"
        t = f"{s['t']:.1f}" if s["t"] is not None else "-"
        d = s["data"]
        data = "ok" if d["ok"] else f"PROBLEM: {d['problem']}"
        print(
            f"{s['name']:20s} {s['status']:13s} {s['months']:6d} {s['n']:7d} {hit:>6s} "
            f"{t:>5s} {s['trust']:7.0%}  {data} ({d['records']} records)"
        )
    board = conviction_board(pcfg, prep, last, set())
    if board:
        print(f"\nMarket mood {board['regime']:+.2f}: {board['regime_text'] or 'no data'}")
        print(f"Top System Conviction scores (buys from {board['entry_score']:+.2f}):")
        for st in board["stocks"][:10]:
            print(
                f"  {st['ticker']:6s} {st['score']:+.2f}  "
                + "; ".join(f"{p['source']} {p['contrib']:+.2f}" for p in st["parts"])
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
