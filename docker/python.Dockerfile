# Sdílený image pro engine, API i news-engine (uv workspace, Python 3.12).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

COPY pyproject.toml uv.lock ./
COPY engine/pyproject.toml engine/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY news-engine/pyproject.toml news-engine/pyproject.toml
COPY engine/src engine/src
COPY api/src api/src
COPY news-engine/src news-engine/src

RUN uv sync --all-packages --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
