"""Command line entry point: ``ff backtest`` and ``ff fetch``."""

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
    f = sub.add_parser("fetch", help="download and cache bars for a config's symbols")
    f.add_argument("config", type=Path)
    f.add_argument("--provider")
    args = p.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
