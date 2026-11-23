# Finance Free

A personal research and paper-trading platform for US stocks and ETFs. It backtests strategies with realistic costs and risk rules, and produces recommendations with the reasoning behind them. The owner places any real trades by hand; there is no broker connection.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, the data provider plan, bias prevention, and the roadmap.

> Simulation only. Backtest results do not predict future performance.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

macOS note: on a Mac, run commands as `./ff ...` (for example `./ff serve config/paper.yaml`) or through `make`, or add `export UV_NO_EDITABLE=1` to `~/.zshrc` once so plain `uv run ff ...` works too. Otherwise `uv run ff` can fail with `No module named 'ff'`: macOS sometimes hides the editable-install `.pth` file, and Python 3.13+ skips hidden `.pth` files. The launcher installs the package as a normal (non-editable) copy that is rebuilt whenever the code changes, so it never depends on that file.

```bash
uv sync                                   # install dependencies
make test                                 # unit tests
make demo                                 # offline run on synthetic data (checks the pipeline)
make backtest                             # real run: downloads free daily data from Yahoo
make validate                             # Phase 2: walk-forward, sweep, Deflated Sharpe, PBO
make paper                                # Phase 3: process new sessions in the paper ledger
make serve                                # dashboard + API at http://127.0.0.1:8000
./ff signals config/paper.yaml            # Phase 4: signal sources and their scoreboard
```

`ff validate` keeps the last years (``holdout_start``) locked. Open them once, when you are done changing the strategy, with `uv run ff validate config/validate_trend.yaml --open-holdout`; every opening is recorded in `runs/trial_registry.jsonl`, together with every parameter set ever tried.

Stooq currently serves a JavaScript bot check, so it fails for automated use; Yahoo is the default. For Tiingo, copy `.env.example` to `.env` and set `TIINGO_API_KEY` (never commit `.env`).

Each run writes a folder under `runs/` containing:

| File | Contents |
|---|---|
| `report.html` | Equity and drawdown charts, metrics vs SPY and 60/40, yearly returns, latest recommendations with rationale, trades, data-quality flags |
| `metrics.json` | All metrics plus run metadata (git commit, config fingerprint) |
| `recommendations.csv` | Latest target weights, stops and rationale |
| `trades.csv`, `equity.csv` | Round-trip trades and daily equity |
| `audit.jsonl` | Every decision, risk verdict, order and fill |
| `config.yaml` | The exact config used |

## Paper trading (Phase 3)

`config/paper.yaml` defines the paper book: Track A with $50k in trend + risk parity and $50k in a 60/40 core, starting 2026-11-23. Each run of `ff paper config/paper.yaml`:

1. downloads the latest daily bars and processes every finished session since the last run (safe to run any number of times; a session counts once its close is 30 minutes old),
2. fills the previous trade tickets at the open with costs, triggers stops, credits dividends,
3. records equity, trades, every decision, risk verdict, order and fill in `state/paper.sqlite`,
4. writes new trade tickets for the next open and alerts, and sends the daily summary to Telegram if configured.

You place real trades yourself, if any, and mark each ticket as taken or skipped on the dashboard. The paper book fills every ticket either way, so it stays a clean record of the system.

`ff serve config/paper.yaml` serves the dashboard in `dashboard/` with live data (opening `dashboard/index.html` directly still shows the mock data). `ff daemon config/paper.yaml` runs the loop after every US close forever, retries if the data is late, backs up the ledger nightly to `state/backups/`, and reports failures as alerts. On a server, `docker compose -f docker/compose.yaml up -d --build` runs both; [docs/DEPLOY.md](docs/DEPLOY.md) walks through a Hetzner setup with one install script.

The drawdown breaker never resets itself in paper trading. After reviewing, reset it with `ff paper config/paper.yaml --reset-breaker track_a_systematic`.

Telegram: create a bot with @BotFather, put `TELEGRAM_BOT_TOKEN` (and optionally `TELEGRAM_CHAT_ID`) in `.env`, or set the chat id on the dashboard's Alerts page and press Send test.

## Signals and System Conviction (Phase 4)

The paper book now has a third portfolio, **System Conviction** ($100k of practice money): the system's own picks among 100 large US stocks (`config/universes/us_large_cap.yaml`), built from four free signal sources:

| Source | What it measures | Data | Research |
|---|---|---|---|
| Price momentum | 12-month return skipping the last month, ranked | prices | Jegadeesh & Titman 1993 |
| Insider buying | Open-market buys by officers and directors; routine buyers removed; clusters and CEO/CFO buys score higher | SEC Form 4 | Cohen, Malloy & Pomorski 2012 |
| Earnings reaction | Move vs SPY in the 3 days around the earnings 8-K | SEC 8-K Item 2.02 + prices | Bernard & Thomas 1989; Brandt et al. 2008 |
| Economy | VIX vs its 1-year median, 10y-3m curve, Baa credit spread trend; only sizes new positions | FRED | Estrella & Mishkin 1998; Gilchrist & Zakrajsek 2012 |

How it decides (`src/ff/strategies/conviction.py`, `src/ff/signals/scoring.py`):

1. Every record carries the session it was first knowable (a Form 4 counts from the session after its filing date, a FRED value from the session after its date). `SignalView` never returns anything later, just like prices.
2. Each source is scored on what happened after each of its calls: bought at the next open, measured against the universe's average over its holding time (1 week, 1 month, 3 months). Only outcomes already finished count. Its weight is `max(0, monthly information ratio) x months / (months + 24)`, halved while its t-stat is between 1 and 2, zero below 1. Nothing is hand-weighted: a source with no record gets nothing.
3. Each stock's score is the weighted sum of the sources' latest values. It buys from +0.30 (best first, 10 positions, 3 per sector), with a 2.5 ATR stop, a 2R take-profit and a 63-session limit; it sells when the score drops below 0. A nervous economy shrinks new positions, by as much as its own record allows.
4. Every ticket explains which sources drove it, the risk, and what would invalidate it.

Setup: put `SEC_USER_AGENT=Your Name you@example.com` in `.env` (SEC blocks requests without it). Then:

```bash
./ff signals config/paper.yaml        # downloads the history (first time ~20-30 min), prints the scoreboard
./ff validate config/validate_conviction.yaml   # walk-forward, Deflated Sharpe, PBO; 2023+ stays locked
```

What to expect, honestly: these are documented effects, but momentum crashes after sharp rebounds, the earnings drift has largely faded in large caps since the 2000s (Martineau 2022), and insider buying is strongest in small companies. In a large-cap universe a realistic outcome is a small edge or none. The scoreboard exists to tell you which, with numbers, instead of anyone's opinion. The universe is today's large caps, so backtests before 2026 are flattered by survivorship; paper results from the start date on are not.

## Kill switch

The dashboard's kill switch creates `state/KILL_SWITCH` (for backtests, `KILL_SWITCH` in the working directory). While it exists, no decisions are made and waiting orders are cancelled at the next run. Delete it, or turn the switch off, to resume.

## Layout

```
src/ff/
  core/        types, config, audit log
  data/        providers (stooq, yahoo, tiingo, synthetic), Parquet cache, point-in-time view
  markets/     US cost model and settlement
  execution/   simulated broker and cash-account ledger
  risk/        risk engine and rules
  strategies/  plug-in interface, Track A strategies, benchmarks
  backtest/    event-driven engine and run orchestration
  analytics/   indicators, metrics, HTML report
  signals/     Phase 4 signal store, source scoring, sources (FRED, SEC EDGAR, prices)
  paper/       paper-trading loop, SQLite ledger, daemon
  api/         FastAPI backend for the dashboard
  notify/      Telegram alerts
  app/         CLI
dashboard/     static UI (vanilla JS); live via `ff serve`
config/        run configs (YAML)
tests/         unit and property tests
docker/        Dockerfile and compose
deploy/        install.sh (fresh Ubuntu server) and update.sh
```

## Adding a strategy

Subclass `ff.strategies.base.Strategy`, decorate it with `@register("my_kind")`, import it from a strategies package, and reference `kind: my_kind` in a YAML config. The engine, risk rules, costs and reports apply automatically.
