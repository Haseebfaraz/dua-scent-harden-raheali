# Reproducible commands. CI (.github/workflows/ci.yml) runs exactly these targets.
# Requires Python 3.12 and a PostgreSQL 16 reachable as DATABASE_URL (see `make db-local`).
PY ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin
export OPENAI_API_KEY ?= test-placeholder-not-a-key
export OPENAI_MODEL ?= test-model

.PHONY: venv install install-dev lock audit lint test test-security test-reference migrations db-local db-stop startup-check

venv:
	$(PY) -m venv $(VENV) && $(BIN)/pip install --quiet --upgrade pip

install: venv          ## production dependency set, hash-checked
	$(BIN)/pip install --quiet --require-hashes -r requirements.txt && $(BIN)/pip install --quiet --no-deps .

install-dev: venv      ## production + test/lint/audit tooling, hash-checked
	$(BIN)/pip install --quiet --require-hashes -r requirements-dev.txt && $(BIN)/pip install --quiet --no-deps -e .

lock:                  ## re-resolve both lock files from pyproject.toml (commit the result)
	$(BIN)/pip install --quiet pip-tools
	$(BIN)/pip-compile --quiet --generate-hashes --strip-extras --resolver=backtracking -o requirements.txt pyproject.toml
	$(BIN)/pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe --resolver=backtracking --extra dev -o requirements-dev.txt pyproject.toml

audit:                 ## dependency vulnerability audit of BOTH locked sets (public advisory data only)
	$(BIN)/pip-audit -r requirements.txt --require-hashes --progress-spinner off --desc on
	$(BIN)/pip-audit -r requirements-dev.txt --require-hashes --progress-spinner off --desc on

lint:
	$(BIN)/ruff check app tests scripts/data_retention.py

migrations:            ## apply migrations 0001-0004 to DATABASE_URL twice and verify the schema (disposable databases only)
	$(BIN)/python -m scripts.verify_migrations

test:                  ## the complete deterministic suite (live_ai and reference_data are deselected by pyproject)
	$(BIN)/python -m pytest -q -p no:cacheprovider -rN

test-security:
	$(BIN)/python -m pytest -q -p no:cacheprovider tests/security

test-reference:        ## the 13 tests that need the production reference catalog (not runnable in CI)
	$(BIN)/python -m pytest -q -p no:cacheprovider -m reference_data -o addopts=""

startup-check:         ## import the app and build the ASGI object with fake settings; no network, no migration
	$(BIN)/python -m scripts.startup_check

db-local:              ## start an embedded disposable PostgreSQL 16 (pgserver) for local runs
	$(BIN)/pip install --quiet pgserver && $(BIN)/python -m scripts.local_db start

db-stop:
	$(BIN)/python -m scripts.local_db stop
