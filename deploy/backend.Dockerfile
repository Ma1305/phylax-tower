# syntax=docker/dockerfile:1.7
# Phylax Tower tower: one image, `python -m holdshort.serve` runs every process
# (api + workers). Build from the repo root:
#   docker build -f deploy/backend.Dockerfile -t holdshort-tower .
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PORT=8000

WORKDIR /app

# Dependencies first (cached unless pyproject.toml / uv.lock change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY holdshort/ holdshort/
COPY scripts/ scripts/
COPY docs/v2/world_lga.json docs/v2/world_lga.json
# tools/ is optional (the simulator task may not be merged yet); copy it when present.
RUN --mount=type=bind,target=/ctx \
    if [ -d /ctx/tools ]; then cp -r /ctx/tools /app/tools; else echo "note: no tools/ in build context"; fi

RUN useradd --create-home --uid 10001 holdshort && chown -R holdshort /app
USER holdshort

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1
CMD ["sh", "-c", "exec python -m holdshort.serve --port ${PORT:-8000}"]
