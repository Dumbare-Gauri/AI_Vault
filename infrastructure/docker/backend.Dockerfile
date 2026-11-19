# Build context is the repo root (see infrastructure/docker/docker-compose.yml)
# so this Dockerfile can preserve the monorepo's relative path layout —
# apps/backend/requirements.txt references packages/shared as `-e ../../packages/shared`,
# and that only resolves correctly if the same relative structure exists in the image.
FROM python:3.13-slim

WORKDIR /workspace

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY packages/shared ./packages/shared
COPY apps/backend/requirements.txt ./apps/backend/requirements.txt

# pip resolves the `-e ../../packages/shared` line in requirements.txt
# relative to the *current working directory*, not the requirements file's
# location — so this install must run with apps/backend as cwd.
WORKDIR /workspace/apps/backend
# The base image's pip bundles vulnerable msgpack and urllib3 copies.
RUN pip install --no-cache-dir --upgrade "pip>=26.3" \
    && pip install --no-cache-dir -r requirements.txt

WORKDIR /workspace
COPY apps/backend ./apps/backend

# Phase 10 (ADR-022) — a container process running as root is a
# security-review finding regardless of whether anything's exploited it
# yet; least-privilege applies to containers the same as it does to Drive
# OAuth scopes (Handbook §13). `/workspace` must be writable by this user
# for local dev's bind-mounted hot-reload (docker-compose.yml) to work.
RUN useradd --create-home --uid 1000 vault_app \
    && mkdir -p /home/vault_app/gensim-data \
    && chown -R vault_app:vault_app /workspace /home/vault_app/gensim-data
USER vault_app

WORKDIR /workspace/apps/backend

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD curl -f http://localhost:8000/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
