#!/usr/bin/env bash
# Stops the staging server once nobody has used AI Vault for IDLE_MINUTES,
# so it costs only its disk and IP while idle. "Used" means an API request
# (/v1/...) from outside the server — the app makes them whenever someone
# has it open — or a deploy. Started again by the next deploy or the
# "Start staging" workflow. Run every 10 minutes by aivault-idle-stop.timer.
set -euo pipefail

IDLE_MINUTES="${IDLE_MINUTES:-60}"
BASE=/opt/aivault/staging

# Staging only: never stops a production server, even if installed there.
[ -d "$BASE" ] && [ ! -e /opt/aivault/production ] || exit 0

uptime_seconds="$(cut -d. -f1 /proc/uptime)"
[ "$uptime_seconds" -ge $((IDLE_MINUTES * 60)) ] || exit 0
[ -z "$(find "$BASE/deployed_sha" -mmin "-$IDLE_MINUTES" 2>/dev/null)" ] || exit 0

# nginx access log: client address first, request path seventh. Health
# checks come from the container itself or Docker's gateway.
api_requests="$(docker logs --since "${IDLE_MINUTES}m" aivault-staging-web-1 2>/dev/null \
  | awk '$7 ~ /^\/v1\// && $1 != "127.0.0.1" && $1 !~ /^172\.30\./' | wc -l)"
[ "$api_requests" -eq 0 ] || exit 0

echo "No AI Vault use for $IDLE_MINUTES minutes: stopping the staging server"
exec flock -n "$BASE/deploy.lock" systemctl poweroff
