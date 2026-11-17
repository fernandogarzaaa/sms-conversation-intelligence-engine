PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: help install test cov lint fmt demo seed serve worker eval docker clean

help:
	@echo "install  create .venv and install with dev extras"
	@echo "test     run the test suite"
	@echo "cov      run tests with coverage"
	@echo "lint     ruff check"
	@echo "fmt      ruff format + autofix"
	@echo "demo     console walk-through of scenarios A-G"
	@echo "seed     write a demo database for the dashboard"
	@echo "serve    run the API + dashboard on :8099"
	@echo "worker   run the send-queue / follow-up worker"
	@echo "eval     score the configured provider on the labelled set"

.venv:
	python3 -m venv .venv

install: .venv
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

cov:
	$(PY) -m pytest --cov --cov-report=term

lint:
	$(PY) -m ruff check src tests scripts

fmt:
	$(PY) -m ruff check --fix src tests scripts

demo:
	$(PY) -m sms_engine.cli demo

seed:
	$(PY) -m sms_engine.cli seed-db

serve:
	$(PY) -m sms_engine.cli serve --reload

worker:
	$(PY) -m sms_engine.cli worker

eval:
	$(PY) -m sms_engine.cli eval --mismatches

docker:
	docker compose up --build

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist src/*.egg-info
