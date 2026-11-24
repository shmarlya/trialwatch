# syntax=docker/dockerfile:1

# --- build: resolve deps and install the project into /app/.venv ---
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime: just Python + the venv, no uv, non-root ---
FROM python:3.13-slim
RUN useradd --create-home --uid 1000 app
COPY --from=build --chown=app:app /app/.venv /app/.venv
# The gold datasets (agreement published with each judgement); compose's ./data mount may replace them
COPY --chown=app:app data/gold /app/data/gold
# The migrations and dbmate (a static binary), so a deploy migrates from this image alone
# (compose.coolify.yaml: `dbmate --wait up` before web and worker start). Lock: two starts at once wait
# for each other; no schema dump: nowhere to write db/schema.sql, and pg_dump isn't here.
COPY --from=ghcr.io/amacneil/dbmate:2.37.0 /usr/local/bin/dbmate /usr/local/bin/dbmate
COPY --chown=app:app db/migrations /app/db/migrations
RUN ["dbmate", "--version"]
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 TRIALWATCH_GOLD_DIR=/app/data/gold \
    DBMATE_NO_DUMP_SCHEMA=true DBMATE_MIGRATION_LOCK=true
USER app
WORKDIR /app
ENTRYPOINT ["trialwatch"]
# The web interface, without a worker of its own (compose runs one); `docker compose run` takes other commands
EXPOSE 8010
CMD ["web", "--host", "0.0.0.0", "--no-worker"]
# Healthy while /healthz answers (the worker's compose service has a healthcheck of its own)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=3)"]
