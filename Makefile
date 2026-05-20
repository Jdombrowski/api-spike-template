PYTHON  := python3
VENV    := .venv
PIP     := $(VENV)/bin/pip
PY      := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest

.DEFAULT_GOAL := help

# ── Help ───────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Setup ──────────────────────────────────────────────────────────────────────
.PHONY: venv
venv: ## Create the virtual environment
	$(PYTHON) -m venv $(VENV)

.PHONY: init
init: venv ## First-time setup: dependencies and .env
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@test -f .env && echo ".env already exists — skipping" || (cp .env.example .env && echo "Created .env — fill in your API keys")

# ── Tests ──────────────────────────────────────────────────────────────────────
.PHONY: test
test: ## Run unit tests only (no network calls)
	$(PYTEST) tests/test_core.py

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(PYTEST) tests/ --cov=src --cov-report=term-missing

.PHONY: test-cov-html
test-cov-html: ## Run tests and open HTML coverage report
	$(PYTEST) tests/ --cov=src --cov-report=html
	open htmlcov/index.html

.PHONY: test-polygon
test-polygon: ## Run live Polygon.io integration tests (requires POLYGON_API_KEY)
	$(PYTEST) tests/test_polygon_client.py -v

.PHONY: test-edgar
test-edgar: ## Run live EDGAR integration tests (requires EDGAR_USER_AGENT in .env)
	$(PYTEST) tests/test_edgar_client.py -v

# ── Run ────────────────────────────────────────────────────────────────────────
.PHONY: run
run: ## Run the full investigation pipeline
	$(PY) -m src.pipeline

.PHONY: run-cik
run-cik: ## Run pipeline for a specific CIK  (usage: make run-cik CIK=0001067983)
	$(PY) -m src.pipeline --cik $(CIK)

.PHONY: report
report: ## Print a rich terminal summary of all investigation findings
	$(PY) -m src.report

.PHONY: export
export: ## Export findings to timestamped CSVs in data/exports/
	$(PY) -m src.report --export

# ── Clean ──────────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove byte-compiled files and test artifacts
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
	find . -name '*.pyc' -not -path './.venv/*' -delete
	rm -rf .coverage htmlcov .pytest_cache

.PHONY: clean-all
clean-all: clean ## Remove everything including the virtual environment
	rm -rf $(VENV)
