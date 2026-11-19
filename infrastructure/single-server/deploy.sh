#!/usr/bin/env bash
# Deploys one commit to one environment on this server, and rolls back to
# the last working commit if the new one doesn't come up healthy.
#
#   sudo /opt/aivault/<env>/src/infrastructure/single-server/deploy.sh <staging|production> <commit-sha>
#
# Run by the GitHub deploy workflow through AWS Systems Manager; can also be
# run by hand on the server. Each environment has its own checkout, .env,
# Compose project and state — this script never touches the other one.
set -euo pipefail

ENV_NAME="${1:?usage: deploy.sh <staging|production> <commit-sha>}"
SHA="${2:?usage: deploy.sh <staging|production> <commit-sha>}"
case "$ENV_NAME" in staging|production) ;; *) echo "Unknown environment: $ENV_NAME" >&2; exit 2 ;; esac
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "Expected a full 40-character commit SHA" >&2; exit 2; }

BASE="/opt/aivault/$ENV_NAME"
SRC="$BASE/src"
ENV_FILE="$BASE/.env"
PROJECT="aivault-$ENV_NAME"
STATE="$BASE/deployed_sha"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$SRC/infrastructure/single-server/compose.yml")

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE" >&2; exit 1; }
[ "$(stat -c %a "$ENV_FILE")" = "600" ] || { echo "$ENV_FILE must be chmod 600" >&2; exit 1; }
grep -q "^ENVIRONMENT_NAME=$ENV_NAME\b" "$ENV_FILE" \
  || { echo "$ENV_FILE is not the $ENV_NAME environment's file" >&2; exit 1; }

exec 9>"$BASE/deploy.lock"
flock -n 9 || { echo "Another $ENV_NAME deployment is running" >&2; exit 1; }

previous="$(cat "$STATE" 2>/dev/null || true)"

web_port="$(grep -E '^WEB_PORT=' "$ENV_FILE" | cut -d= -f2)"
origin_secret="$(grep -E '^ORIGIN_VERIFY_SECRET=' "$ENV_FILE" | cut -d= -f2-)"

release() {
  local sha="$1"
  git -C "$SRC" fetch --quiet origin "$sha"
  git -C "$SRC" checkout --quiet --detach "$sha"
  sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$sha/" "$ENV_FILE"
  "${COMPOSE[@]}" up -d --build --wait --wait-timeout 600 --remove-orphans
}

healthy() {
  # Through nginx, exactly as CloudFront reaches it.
  for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null -H "X-Origin-Verify: $origin_secret" \
        "http://127.0.0.1:$web_port/health/live"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

if [ -n "$previous" ]; then
  echo "==> Backing up the $ENV_NAME database before migrating"
  "$SRC/infrastructure/single-server/backup.sh" "$ENV_NAME"
fi

echo "==> Deploying $SHA to $ENV_NAME (was: ${previous:-nothing})"
if release "$SHA" && healthy; then
  echo "$SHA" > "$STATE"
  docker image prune -f --filter "until=168h" >/dev/null
  echo "==> $ENV_NAME is running $SHA"
  exit 0
fi

echo "==> $SHA did not come up healthy" >&2
if [ -n "$previous" ]; then
  # Code only: a migration that already ran is not undone automatically —
  # restore the pre-deploy backup by hand if the old code can't read it.
  echo "==> Rolling $ENV_NAME back to $previous" >&2
  release "$previous" && healthy && echo "==> Rolled back to $previous" >&2
fi
exit 1
