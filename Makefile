.PHONY: install install-dev test build check format-check lint format typecheck run stop clean

install:
	@uv sync

install-dev:
	@uv sync --group dev

test:
	@uv run pytest

build:
	@docker build -t machwave-api .
	@docker build -f Dockerfile.worker -t machwave-worker .

typecheck:
	@uv run pyright

check: format-check lint typecheck

format-check:
	@uv run ruff format --check

lint:
	@uv run ruff check .

format:
	@uv run ruff format
	@uv run ruff check . --fix

run:
	@docker compose up --build

stop:
	@docker compose down

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
