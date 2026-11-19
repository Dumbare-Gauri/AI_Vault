#!/usr/bin/env bash
# Restores one environment's database from a backup.sh dump. The app is
# stopped first and started again afterwards; the database is replaced, not
# merged. The dump's .env must be the one this environment runs with (the
# same CONNECTOR_ENCRYPTION_KEY), or stored Drive tokens can't be read.
#
#   restore.sh <staging|production> <dump-file>
set -euo pipefail

ENV_NAME="${1:?usage: restore.sh <staging|production> <dump-file>}"
DUMP="${2:?usage: restore.sh <staging|production> <dump-file>}"
case "$ENV_NAME" in staging|production) ;; *) echo "Unknown environment: $ENV_NAME" >&2; exit 2 ;; esac
[ -f "$DUMP" ] || { echo "No such file: $DUMP" >&2; exit 1; }
if [ -f "$DUMP.sha256" ]; then
  (cd "$(dirname "$DUMP")" && sha256sum --check --quiet "$(basename "$DUMP").sha256")
fi

ROOT="${AIVAULT_ROOT:-/opt/aivault}"
BASE="$ROOT/$ENV_NAME"
PROJECT="${AIVAULT_PROJECT:-aivault-$ENV_NAME}"
SRC="${AIVAULT_SRC:-$BASE/src}"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$BASE/.env" -f "$SRC/infrastructure/single-server/compose.yml")

if [ "${CONFIRM:-}" != "replace-$ENV_NAME" ]; then
  read -r -p "This replaces the $ENV_NAME database with $(basename "$DUMP"). Type 'replace-$ENV_NAME': " answer
  [ "$answer" = "replace-$ENV_NAME" ] || { echo "Cancelled; nothing changed."; exit 1; }
fi

echo "==> Stopping the $ENV_NAME app"
"${COMPOSE[@]}" stop web backend worker scheduler

echo "==> Restoring"
"${COMPOSE[@]}" exec -T postgres sh -c \
  'dropdb -U aivault --if-exists aivault && createdb -U aivault aivault'
"${COMPOSE[@]}" exec -T postgres pg_restore -U aivault -d aivault --no-owner --exit-on-error < "$DUMP"

echo "==> Starting the $ENV_NAME app (migrations run first)"
"${COMPOSE[@]}" up -d --wait --wait-timeout 600
echo "==> Restored $ENV_NAME from $(basename "$DUMP")"
