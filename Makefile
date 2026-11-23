.PHONY: setup test lint backtest validate validate-a demo paper serve
setup:
	uv sync
test:
	uv run pytest
lint:
	uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
backtest:
	uv run ff backtest config/mvp_trend.yaml
validate:
	uv run ff validate config/validate_trend.yaml

validate-a: # each Track A strategy, then the combined book (pools their trials)
	uv run ff validate config/validate_trend.yaml
	uv run ff validate config/validate_momentum.yaml
	uv run ff validate config/validate_risk_parity.yaml
	uv run ff validate config/validate_track_a.yaml
demo:
	uv run ff backtest config/mvp_trend.yaml --provider synthetic
paper:
	uv run ff paper config/paper.yaml
serve:
	uv run ff serve config/paper.yaml
