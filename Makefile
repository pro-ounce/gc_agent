# ════════════════════════════════════════════════════════════════════════════
# MCP Agent — Developer Makefile
# Run ALL targets from:  /Users/six7/Git/gc/agent/
# ════════════════════════════════════════════════════════════════════════════
.DEFAULT_GOAL := help
# Requires Python 3.12 — pyenv will auto-select via .python-version
PYTHON        := python3.12
VENV          := .venv
PIP           := $(VENV)/bin/pip
APP           := $(VENV)/bin/python -m uvicorn app.main:app
PYTEST        := $(VENV)/bin/python -m pytest
RUFF          := $(VENV)/bin/ruff
BLACK         := $(VENV)/bin/black

# ── Guard: must run from the project root ─────────────────────────────────────
_check-dir:
	@test -f pyproject.toml || (echo "ERROR: run make from /Users/six7/Git/gc/agent/"; exit 1)

.PHONY: help venv install dev run test test-cov lint fmt clean docker-up docker-down _check-dir

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  MCP Agent — available targets"
	@echo "  (run all commands from /Users/six7/Git/gc/agent/)"
	@echo ""
	@echo "  Setup"
	@echo "    make install    Create .venv and install requirements.txt"
	@echo "    make env        Copy .env.example → .env.local (edit secrets)"
	@echo ""
	@echo "  Development"
	@echo "    make dev        Hot-reload server on :8080  (docs at /docs)"
	@echo "    make run        Production server"
	@echo ""
	@echo "  Testing"
	@echo "    make test       Run 17-test suite (no network, no Redis needed)"
	@echo "    make test-cov   Tests + HTML coverage report"
	@echo ""
	@echo "  Code quality"
	@echo "    make lint       ruff check"
	@echo "    make fmt        black format"
	@echo ""
	@echo "  Docker"
	@echo "    make docker-up   Build + start agent + redis"
	@echo "    make docker-down Stop containers"
	@echo ""
	@echo "  Maintenance"
	@echo "    make clean      Remove .venv, caches, build artefacts"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────
venv: _check-dir
	@$(PYTHON) --version 2>&1 | grep -q "3\.1[12]" \
		|| (echo "ERROR: Python 3.11+ required. Active: $$($(PYTHON) --version 2>&1)"; \
		    echo "       Run: pyenv local 3.12.8"; exit 1)
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip wheel

install: venv
	# Install ONLY from requirements.txt — never runs pip install .
	$(PIP) install --quiet -r requirements.txt
	@echo "✓ Dependencies installed into $(VENV)"

env: _check-dir
	@test -f .env.local \
		&& echo ".env.local already exists — skipping" \
		|| (cp .env.example .env.local \
			&& echo "Created .env.local — set OLLAMA_BASE_URL and MCP_BASE_URL")

# ── Run ───────────────────────────────────────────────────────────────────────
dev: install
	@test -f .env.local || $(MAKE) env
	$(APP) --host 0.0.0.0 --port 8080 --reload

run: install
	$(APP) --host 0.0.0.0 --port 8080 --workers 2

# ── Backup ──────────────────────────────────────────────────────────────────────
# Pre-deploy OpenSearch snapshot (run ON the box, before rsync/restart, so a failed
# backup aborts the deploy). Wire this as the FIRST step of the deploy pipeline.
#   make snapshot LABEL=pre-v1.5
LABEL ?= pre-deploy
snapshot:
	bash deploy/os-snapshot.sh "$(LABEL)"

# ── Test ──────────────────────────────────────────────────────────────────────
TEST_ENV := TESTING=1 ENV=test AUTH_ENABLED=false RBAC_ENABLED=false \
            REDIS_ENABLED=false LOG_LEVEL=WARNING \
            MCP_BASE_URL=http://localhost:19999 \
            JWT_SECRET=test-secret \
            TOOL_RISK_CONFIRMATION=false

test: install
	$(TEST_ENV) $(PYTEST) tests/ -v

test-cov: install
	$(TEST_ENV) $(PYTEST) tests/ -v \
		--cov=app --cov-report=html --cov-report=term-missing
	@echo "Coverage report → htmlcov/index.html"

# ── Code quality ───────────────────────────────────────────────────────────────
lint: install
	$(RUFF) check app/ tests/

fmt: install
	$(BLACK) app/ tests/

# ── Docker ────────────────────────────────────────────────────────────────────
docker-up: _check-dir
	docker-compose up --build -d
	@echo "Agent  → http://localhost:8080"
	@echo "Docs   → http://localhost:8080/docs"

docker-down: _check-dir
	docker-compose down

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	rm -rf $(VENV) .pytest_cache htmlcov .coverage .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned."
