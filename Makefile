.PHONY: setup test lint backtest demo
setup:
	uv sync
test:
	uv run pytest
lint:
	uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
backtest:
	uv run ff backtest config/mvp_trend.yaml
demo:
	uv run ff backtest config/mvp_trend.yaml --provider synthetic
