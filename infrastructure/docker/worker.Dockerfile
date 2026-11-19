# Build context is the repo root — see backend.Dockerfile's comment for why
# the relative path layout (packages/shared alongside apps/worker) matters.
FROM python:3.13-slim

WORKDIR /workspace

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY packages/shared ./packages/shared
COPY apps/worker/requirements.txt ./apps/worker/requirements.txt

# pip resolves the `-e ../../packages/shared` line in requirements.txt
# relative to the *current working directory*, not the requirements file's
# location — so this install must run with apps/worker as cwd.
WORKDIR /workspace/apps/worker
# The base image's pip bundles vulnerable msgpack and urllib3 copies.
RUN pip install --no-cache-dir --upgrade "pip>=26.3" \
    && pip install --no-cache-dir -r requirements.txt

WORKDIR /workspace
COPY apps/worker ./apps/worker

# Phase 10 (ADR-022) — see backend.Dockerfile's identical comment. The
# worker also needs write access to $PROMETHEUS_MULTIPROC_DIR (default
# /tmp/prometheus_multiproc_dir — world-writable) if metrics are enabled;
# no extra chown needed for that path specifically.
RUN useradd --create-home --uid 1000 vault_app \
    && mkdir -p /home/vault_app/gensim-data /archive-tmp \
    && chown -R vault_app:vault_app /workspace /home/vault_app/gensim-data /archive-tmp
USER vault_app

WORKDIR /workspace/apps/worker

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD celery -A worker.celery_app inspect ping || exit 1

CMD ["celery", "-A", "worker.celery_app", "worker", "--loglevel=info"]
