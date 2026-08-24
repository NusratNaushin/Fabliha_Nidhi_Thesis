# Convenience targets. Everything here is a thin wrapper over a command that is
# also documented in the README -- nothing is hidden behind make.
#
# On Windows use the PowerShell equivalents in the README; this file targets
# macOS and Linux, where `make` is already present.

VENV    ?= .venv
PY      ?= $(VENV)/bin/python
PYTEST  ?= $(PY) -m pytest

.DEFAULT_GOAL := help
.PHONY: help bootstrap install migrate api demo test test-all test-slow \
        test-integration test-validation cov lint clean db-up db-down \
        snowstorm-up snowstorm-down mimic audit check-db

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------
bootstrap:  ## Full first-time setup (venv, deps, .env, Snowstorm clone, migrations)
	./scripts/bootstrap.sh

install:  ## Create the venv and install dependencies only
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip --quiet
	$(PY) -m pip install -r requirements.txt --quiet

migrate:  ## Apply database migrations
	$(PY) -m alembic upgrade head

check-db:  ## Verify the configured database is reachable and the schema is current
	$(PY) scripts/check_database.py

check-rules:  ## Refuse hard-coded terminology release identifiers (Hard Rules 1-3)
	$(PY) scripts/check_no_hardcoded_versions.py

# --- services --------------------------------------------------------------
db-up:  ## Start PostgreSQL
	docker compose up -d

db-down:  ## Stop PostgreSQL (data volume is kept)
	docker compose down

snowstorm-up:  ## Start Snowstorm + Elasticsearch (needs ~8 GB free RAM)
	cd infra/snowstorm && docker compose up -d

snowstorm-down:  ## Stop Snowstorm + Elasticsearch
	cd infra/snowstorm && docker compose down

api:  ## Run the API on http://localhost:8000/docs
	$(PY) -m uvicorn backend.app.main:app --reload

# --- running things --------------------------------------------------------
demo:  ## End-to-end demo on synthetic releases (no licensed files needed)
	$(PY) scripts/demo_end_to_end.py

mimic:  ## Download the open-access MIMIC-III demo and audit it against current LOINC
	$(PY) scripts/fetch_mimic_demo.py
	$(PY) scripts/import_mimic_labitems.py --file data/raw/validation/D_LABITEMS.csv
	$(PY) scripts/audit_mappings.py --source-dataset MIMIC_III --report-name mimic_loinc_audit.csv

audit:  ## Audit every stored mapping against the current releases
	$(PY) scripts/audit_mappings.py

validate:  ## Run the full validation experiment on the archives in data/raw/validation
	$(PY) scripts/validate_releases.py

review-export:  ## Write the pending decisions to data/reports/review_queue.csv
	$(PY) scripts/review_queue.py export --latest

review-apply:  ## Apply an edited review CSV (REVIEWER="dr name" required)
	@test -n "$(REVIEWER)" || (echo 'usage: make review-apply REVIEWER="dr name"'; exit 2)
	$(PY) scripts/review_queue.py apply --file data/reports/review_queue.csv --reviewer "$(REVIEWER)"

loinc-to-snowstorm:  ## Load a LOINC release into Snowstorm (LOINC=<path to zip>)
	@test -n "$(LOINC)" || (echo 'usage: make loinc-to-snowstorm LOINC=data/raw/loinc/<file>.zip'; exit 2)
	$(PY) scripts/upload_loinc_to_snowstorm.py --file "$(LOINC)" --download-cli

# --- tests -----------------------------------------------------------------
test:  ## Default suite (unit + API); needs nothing running
	$(PYTEST) -q

test-all:  ## Every suite, including the ones that skip without their inputs
	$(PYTEST) -q -m ""

test-slow:  ## Performance suite (10,000-mapping audit, N+1 guard)
	$(PYTEST) -q -m slow -s

test-integration:  ## Snowstorm integration suite (needs a running Snowstorm)
	$(PYTEST) -q -m integration -v

test-validation:  ## Official-release validation (needs real archives in data/raw/validation)
	$(PYTEST) -m validation -v -s

cov:  ## Full suite with coverage, HTML report in htmlcov/
	$(PYTEST) --cov=backend/app --cov-report=term-missing --cov-report=html --cov-report=xml
	$(PY) scripts/check_coverage.py --min-overall 85 --min-core 95

test-postgres:  ## Run the suite against a THROWAWAY database you name (DB_URL=...)
	@test -n "$(DB_URL)" || (echo 'usage: make test-postgres DB_URL=postgresql+psycopg://user:pw@host:port/throwaway_db'; echo 'The fixtures DROP every table. Never point this at a database you care about.'; exit 2)
	VAS_TEST_DATABASE_URL="$(DB_URL)" $(PYTEST) -q

# --- housekeeping ----------------------------------------------------------
clean:  ## Remove caches and generated coverage output
	rm -rf .pytest_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
