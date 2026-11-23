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
