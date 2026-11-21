# Finance Free

A personal research and paper-trading platform for US stocks and ETFs. It backtests strategies with realistic costs and risk rules, and produces recommendations with the reasoning behind them. The owner places any real trades by hand; there is no broker connection.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, the data provider plan, bias prevention, and the roadmap.

> Simulation only. Backtest results do not predict future performance.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # install dependencies
make test                                 # unit tests
make demo                                 # offline run on synthetic data (checks the pipeline)
make backtest                             # real run: downloads free daily data from Yahoo
```

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

## Kill switch

Create a file named `KILL_SWITCH` in the working directory to stop all orders and decisions immediately. Delete it to resume.

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
  app/         CLI
config/        run configs (YAML)
tests/         unit and property tests
docker/        Dockerfile and compose
```

## Adding a strategy

Subclass `ff.strategies.base.Strategy`, decorate it with `@register("my_kind")`, import it from a strategies package, and reference `kind: my_kind` in a YAML config. The engine, risk rules, costs and reports apply automatically.
