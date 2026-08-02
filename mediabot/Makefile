# Developer shortcuts. Everything here also works inside the container image.
.DEFAULT_GOAL := help
PY ?= python
COMPOSE ?= docker compose

.PHONY: help install dev-install lint format typecheck test test-unit test-cov \
        migrate migration run-bot run-api run-worker run-scheduler \
        up down logs ps build restart clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install the package
	$(PY) -m pip install -e .

dev-install: ## Install with development extras
	$(PY) -m pip install -e ".[dev]"

lint: ## Run ruff
	ruff check src tests

format: ## Format the code base
	ruff format src tests
	ruff check --fix src tests

typecheck: ## Run mypy in strict mode
	mypy src

test: ## Run the whole test-suite
	pytest

test-unit: ## Run the fast, pure-domain tests
	pytest -m unit

test-cov: ## Run the tests with coverage
	pytest --cov --cov-report=term-missing --cov-report=html

migrate: ## Apply database migrations
	alembic upgrade head

migration: ## Create a migration: make migration m="add table x"
	alembic revision --autogenerate -m "$(m)"

run-bot: ## Run the Telegram bot
	$(PY) -m mediabot bot

run-api: ## Run the REST API and admin panel
	$(PY) -m mediabot api

run-scheduler: ## Run the scheduler
	$(PY) -m mediabot scheduler

run-worker: ## Run a Celery worker
	celery -A mediabot.infrastructure.queue worker -l info

build: ## Build the Docker image
	$(COMPOSE) build

up: ## Start the whole stack
	$(COMPOSE) up -d

down: ## Stop the stack
	$(COMPOSE) down

restart: ## Restart the application services
	$(COMPOSE) restart bot api worker beat scheduler

logs: ## Follow the logs
	$(COMPOSE) logs -f --tail=100

ps: ## Show service status
	$(COMPOSE) ps

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
