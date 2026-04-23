.PHONY: install dev api test lint typecheck format docker-build docker-up docker-down evolution-drill evolution-preflight evolution-window-report quality-evolution release-preflight release-smoke staging-rehearsal quality-clean-scope quality-smoke quality-integration quality-slow-report quality-gate

install:
	python -m pip install -e .[dev]

dev: api

api:
	uvicorn stock_analyzer.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest

lint:
	ruff check src tests

typecheck:
	mypy src

format:
	ruff format src tests

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

evolution-drill:
	python -m stock_analyzer.cli evolution-drill --now "2026-03-02T20:41:00"

evolution-preflight:
	python -m stock_analyzer.cli evolution-preflight --fail-on-not-ready true

evolution-window-report:
	python -m stock_analyzer.cli evolution-window-report --days 10 --min-runs 5

quality-evolution:
	ruff check src tests
	mypy src
	pytest tests -k evolution --cov=src/stock_analyzer/evolution --cov-fail-under=80

release-preflight:
	python scripts/run_release_preflight.py --fail-on-not-ready

release-smoke:
	python scripts/run_release_smoke.py --fail-on-failure

staging-rehearsal:
	python scripts/run_staging_rehearsal.py --fail-on-blocked

quality-clean-scope:
	python scripts/run_quality_gate.py --stage clean-scope --fail-on-error

quality-smoke:
	python scripts/run_quality_gate.py --stage smoke --fail-on-error

quality-integration:
	python scripts/run_quality_gate.py --stage integration --fail-on-error

quality-slow-report:
	python scripts/run_quality_gate.py --stage slow-report --fail-on-error

quality-gate:
	python scripts/run_quality_gate.py --stage all --fail-on-error
