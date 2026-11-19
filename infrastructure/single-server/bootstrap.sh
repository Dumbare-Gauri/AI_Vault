#!/usr/bin/env bash
# Prepares a fresh Ubuntu 24.04 EC2 instance to run one AI Vault environment.
# Run once, as root, from that environment's checkout (via an SSM session —
# the instance has no SSH):
#
#   git clone https://github.com/Dumbare-Gauri/AI_Vault.git /opt/aivault/<env>/src
#   /opt/aivault/<env>/src/infrastructure/single-server/bootstrap.sh <staging|production>
#
# Safe to run again. It never overwrites an existing .env.
set -euo pipefail

ENV_NAME="${1:?usage: bootstrap.sh <staging|production>}"
case "$ENV_NAME" in
  staging) OTHER=production; WEB_PORT=8081 ;;
  production) OTHER=staging; WEB_PORT=8080 ;;
  *) echo "Unknown environment: $ENV_NAME" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "Run as root" >&2; exit 1; }

BASE="/opt/aivault/$ENV_NAME"
SRC="$BASE/src"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$HERE" = "$SRC/infrastructure/single-server" ] \
  || { echo "Run this from $SRC (the $ENV_NAME checkout), not $HERE" >&2; exit 1; }
# Staging and production never share a machine: a staging deploy runs as root.
[ ! -e "/opt/aivault/$OTHER" ] \
  || { echo "/opt/aivault/$OTHER exists: this instance belongs to $OTHER" >&2; exit 1; }

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git curl unattended-upgrades
command -v aws >/dev/null || snap install aws-cli --classic
systemctl enable --now docker

echo "==> Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo "==> 2 GB swap"
if ! swapon --show=NAME --noheadings | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-aivault.conf
sysctl -q --system

echo "==> $BASE/.env"
if [ ! -e "$BASE/.env" ]; then
  install -m 600 /dev/null "$BASE/.env"
  sed -e "s|^ENVIRONMENT_NAME=.*|ENVIRONMENT_NAME=$ENV_NAME|" \
      -e "s|^APP_ENV_FILE=.*|APP_ENV_FILE=$BASE/.env|" \
      -e "s|^WEB_PORT=.*|WEB_PORT=$WEB_PORT|" \
      -e "s|aivault-staging-|aivault-$ENV_NAME-|g" \
      "$HERE/env.example" > "$BASE/.env"
  echo "    created from env.example — fill in every value before the first deploy"
else
  echo "    exists, left unchanged"
fi
chmod 600 "$BASE/.env"

echo "==> Scheduled jobs"
install -m 644 "$HERE"/systemd/aivault-backup@.service "$HERE"/systemd/aivault-backup@.timer /etc/systemd/system/
if [ "$ENV_NAME" = staging ]; then
  install -m 644 "$HERE"/systemd/aivault-autostop.service "$HERE"/systemd/aivault-autostop.timer /etc/systemd/system/
fi
systemctl daemon-reload
systemctl enable --now "aivault-backup@$ENV_NAME.timer"
[ "$ENV_NAME" != staging ] || systemctl enable --now aivault-autostop.timer

cat <<EOF

Done. Next:
  1. Fill in $BASE/.env (every value; secrets with \`openssl rand -hex 32\`).
  2. Point PUBLIC_HOST's DNS A record at this instance's Elastic IP.
  3. Deploy: merge to main (staging) / approve a release (production), or by hand:
     $HERE/deploy.sh $ENV_NAME <commit-sha>
EOF
