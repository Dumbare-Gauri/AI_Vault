#!/usr/bin/env bash
# Backs up one environment's database:
#   1. pg_dump (custom format) into $AIVAULT_ROOT/<env>/backups, newest kept;
#   2. copied to that environment's own S3 backup bucket, if configured.
#
#   backup.sh <staging|production>
#
# What a full recovery needs, and where it lives:
#   - the database (users, connections, encrypted Drive tokens, indexes,
#     conversations, archives' records, schedules) ........ this dump
#   - the environment's .env, above all CONNECTOR_ENCRYPTION_KEY — without
#     it the stored Drive tokens in any dump are unreadable .... kept apart
#     from the dumps (see README "Backups"), never in the same bucket
# Not backed up, because nothing is lost without it: Redis (queued jobs and
# rate-limit counters), the archive cache bucket (copies; the archives
# themselves are in each person's own storage), the embedding model
# (downloaded again) and archive temp files.
set -euo pipefail

ENV_NAME="${1:?usage: backup.sh <staging|production>}"
case "$ENV_NAME" in staging|production) ;; *) echo "Unknown environment: $ENV_NAME" >&2; exit 2 ;; esac

ROOT="${AIVAULT_ROOT:-/opt/aivault}"
BASE="$ROOT/$ENV_NAME"
ENV_FILE="$BASE/.env"
PROJECT="${AIVAULT_PROJECT:-aivault-$ENV_NAME}"
SRC="${AIVAULT_SRC:-$BASE/src}"
OUT_DIR="$BASE/backups"
KEEP="${BACKUP_KEEP_LOCAL:-7}"

setting() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

umask 077
mkdir -p "$OUT_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$OUT_DIR/aivault-$ENV_NAME-$stamp.dump"

echo "==> Dumping the $ENV_NAME database"
docker compose -p "$PROJECT" --env-file "$ENV_FILE" \
  -f "$SRC/infrastructure/single-server/compose.yml" \
  exec -T postgres pg_dump -U aivault -d aivault --format=custom > "$dump.partial"
# A dump that can't be listed is not a backup.
docker compose -p "$PROJECT" --env-file "$ENV_FILE" \
  -f "$SRC/infrastructure/single-server/compose.yml" \
  exec -T postgres pg_restore --list < "$dump.partial" > /dev/null
mv "$dump.partial" "$dump"
sha256sum "$dump" > "$dump.sha256"
echo "==> Wrote $dump ($(du -h "$dump" | cut -f1))"

bucket="$(setting BACKUP_BUCKET)"
if [ -n "$bucket" ]; then
  # This environment's own upload-only credentials: they can add backups to
  # its bucket and nothing else — not read them back, not delete them, and
  # not touch the other environment's.
  AWS_ACCESS_KEY_ID="$(setting BACKUP_AWS_ACCESS_KEY_ID)" \
  AWS_SECRET_ACCESS_KEY="$(setting BACKUP_AWS_SECRET_ACCESS_KEY)" \
  AWS_DEFAULT_REGION="$(setting AWS_REGION)" \
    aws s3 cp --only-show-errors --sse AES256 "$dump" "s3://$bucket/$ENV_NAME/$(basename "$dump")"
  AWS_ACCESS_KEY_ID="$(setting BACKUP_AWS_ACCESS_KEY_ID)" \
  AWS_SECRET_ACCESS_KEY="$(setting BACKUP_AWS_SECRET_ACCESS_KEY)" \
  AWS_DEFAULT_REGION="$(setting AWS_REGION)" \
    aws s3 cp --only-show-errors --sse AES256 "$dump.sha256" "s3://$bucket/$ENV_NAME/$(basename "$dump").sha256"
  echo "==> Copied to s3://$bucket/$ENV_NAME/"
else
  echo "==> BACKUP_BUCKET is not set: this backup exists on this server only" >&2
fi

ls -1t "$OUT_DIR"/aivault-"$ENV_NAME"-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
  rm -f "$old" "$old.sha256"
done
