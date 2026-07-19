.PHONY: help install dev lint format typecheck test check docker-build docker-run docker-doctor install-alpine clean webui ci-local

VENV ?= .venv
PY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
PIP := $(if $(wildcard $(VENV)/bin/pip),$(VENV)/bin/pip,pip3)
RUFF := $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)
MYPY := $(if $(wildcard $(VENV)/bin/mypy),$(VENV)/bin/mypy,mypy)
PYTEST := $(if $(wildcard $(VENV)/bin/pytest),$(VENV)/bin/pytest,pytest)

.DEFAULT_GOAL := help

help: ## Show this help
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create .venv (if missing) and install dev + notifications extras
	@if [ ! -d "$(VENV)" ]; then \
		echo "Creating virtualenv at $(VENV)..."; \
		python3 -m venv $(VENV); \
	fi
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev,notifications]"

dev: install ## Alias for install

lint: ## Run ruff check + format --check (mirrors CI)
	$(RUFF) check hivepilot tests
	$(RUFF) format --check hivepilot tests

format: ## Auto-format code with ruff
	$(RUFF) format hivepilot tests

typecheck: ## Run mypy (mirrors CI)
	$(MYPY) hivepilot tests

test: ## Run the test suite
	$(PYTEST) -q

check: lint typecheck test ## Full CI-equivalent gate: lint + typecheck + test

docker-build: ## Build the production Alpine image via docker compose
	docker compose build hivepilot

docker-run: ## Start the production API + scheduler services (detached)
	docker compose up -d hivepilot scheduler

docker-doctor: ## Run `hivepilot doctor` inside the docker compose service
	docker compose run --rm dev hivepilot doctor

install-alpine: ## Bare-metal install on a fresh Alpine host (apk + venv + pip)
	sh scripts/install-alpine.sh

clean: ## Remove caches/build artifacts (keeps .venv, .env, state.db)
	rm -rf .mypy_cache .pytest_cache .ruff_cache
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf *.egg-info

webui: ## Build the Mirador web UI and fail if committed static/ is stale (mirrors CI)
	cd web && npm ci && npm run build
	git diff --exit-code hivepilot/webui/static

ci-local: ## Reproduce the full CI gate locally: lint + typecheck + tests + web build/staleness
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) hivepilot tests
	$(PYTEST) -q
	$(MAKE) webui
