# ════════════════════════════════════════════════════════════════════════════
# MCP Agent — Developer Makefile
# ════════════════════════════════════════════════════════════════════════════
.DEFAULT_GOAL := help
PYTHON        := python3
VENV          := .venv
PIP           := $(VENV)/bin/pip
APP           := $(VENV)/bin/python -m uvicorn app.main:app
PYTEST        := $(VENV)/bin/python -m pytest
RUFF          := $(VENV)/bin/ruff
BLACK         := $(VENV)/bin/black

.PHONY: help venv install dev test test-cov lint fmt clean run docker-up docker-down

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  MCP Agent — available targets"
	@echo ""
	@echo "  Setup"
	@echo "    make venv       Create virtual environment"
	@echo "    make install    Install all dependencies into venv"
	@echo "    make env        Copy .env.example → .env.local (edit before running)"
	@echo ""
	@echo "  Development"
	@echo "    make dev        Run dev server with hot-reload"
	@echo "    make run        Run production server"
	@echo ""
	@echo "  Testing"
	@echo "    make test       Run test suite"
	@echo "    make test-cov   Run tests with HTML coverage report"
	@echo ""
	@echo "  Code quality"
	@echo "    make lint       Lint with ruff"
	@echo "    make fmt        Format with black"
	@echo ""
	@echo "  Docker"
	@echo "    make docker-up   Start services (agent + redis)"
	@echo "    make docker-down Stop services"
	@echo ""
	@echo "  Maintenance"
	@echo "    make clean      Remove venv, caches, build artefacts"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────
venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip wheel

install: venv
	$(PIP) install -r requirements.txt

env:
	@test -f .env.local && echo ".env.local already exists, skipping" || \
		(cp .env.example .env.local && echo "Created .env.local — fill in ANTHROPIC_API_KEY and MCP_BASE_URL")

# ── Run ───────────────────────────────────────────────────────────────────────
dev: install
	@test -f .env.local || $(MAKE) env
	$(APP) --host 0.0.0.0 --port 8080 --reload

run: install
	$(APP) --host 0.0.0.0 --port 8080 --workers 2

# ── Test ──────────────────────────────────────────────────────────────────────
test: install
	TESTING=1 ENV=test AUTH_ENABLED=false RBAC_ENABLED=false \
		REDIS_ENABLED=false LOG_LEVEL=WARNING \
		MCP_BASE_URL=http://localhost:19999 \
		ANTHROPIC_API_KEY=test-key JWT_SECRET=test-secret \
		TOOL_RISK_CONFIRMATION=false \
		$(PYTEST) tests/ -v

test-cov: install
	TESTING=1 ENV=test AUTH_ENABLED=false RBAC_ENABLED=false \
		REDIS_ENABLED=false LOG_LEVEL=WARNING \
		MCP_BASE_URL=http://localhost:19999 \
		ANTHROPIC_API_KEY=test-key JWT_SECRET=test-secret \
		TOOL_RISK_CONFIRMATION=false \
		$(PYTEST) tests/ -v --cov=app --cov-report=html --cov-report=term-missing
	@echo "Coverage report: htmlcov/index.html"

# ── Code quality ───────────────────────────────────────────────────────────────
lint:
	$(RUFF) check app/ tests/

fmt:
	$(BLACK) app/ tests/

# ── Docker ────────────────────────────────────────────────────────────────────
docker-up:
	docker-compose up --build -d
	@echo "Agent running at http://localhost:8080"
	@echo "Docs at         http://localhost:8080/docs"

docker-down:
	docker-compose down

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	rm -rf $(VENV) __pycache__ .pytest_cache htmlcov .coverage .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned."
