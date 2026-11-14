# syntax=docker/dockerfile:1.7
# Hold Short visualizer API (read-only over the tower's MongoDB records).
#   docker build -f deploy/viz-api.Dockerfile -t holdshort-viz-api .
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PORT=8100

WORKDIR /app
COPY visualizer/api/pyproject.toml visualizer/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY visualizer/api/ ./
RUN rm -rf tests

RUN useradd --create-home --uid 10001 viz && chown -R viz /app
USER viz

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8100}/viz/health" || exit 1
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8100}"]
