.PHONY: install install-dev test build check format-check lint format typecheck run stop clean deploy-dev deploy-prod

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

# Build, push, and deploy to Cloud Run. TAG defaults to current git SHA for dev.
# Caller must be authenticated to gcloud (gcloud auth login) and Docker
# (gcloud auth configure-docker).
deploy-dev:
	@TAG="$${TAG:-$$(git rev-parse HEAD)}"; \
	  scripts/deploy.sh dev "$$TAG"

deploy-prod:
	@if [ -z "$$TAG" ]; then \
	  echo "TAG is required: make deploy-prod TAG=v1.2.3"; exit 1; \
	fi
	@scripts/deploy.sh prod "$$TAG"
