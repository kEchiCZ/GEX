# GEXLens — vývojové příkazy (POSIX shell: Linux/macOS/Git Bash).
# Windows ekvivalenty jsou v README (sekce Vývoj).
.PHONY: test test-python test-frontend run run-api run-frontend

test: test-python test-frontend

test-python:
	uv sync --all-packages
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy engine/src engine/tests api/src api/tests
	uv run pytest

test-frontend:
	cd frontend && npm ci && npm run lint && npm run format && npm test && npm run build

# Dev běh (plný docker compose provoz přijde v M5 — issue Packaging)
run-api:
	uv run --package gexlens-api uvicorn gexlens_api.main:app --reload --port 8000

run-frontend:
	cd frontend && npm run dev

run:
	$(MAKE) run-api & $(MAKE) run-frontend
