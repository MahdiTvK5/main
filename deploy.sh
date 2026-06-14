#!/usr/bin/env bash
# Pull latest code and install dependencies on the server.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Pulling latest from git..."
git pull --ff-only || git pull || true

echo "==> Setting up Python venv..."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> Installing dependencies..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "WARNING: .env created. Fill it now: nano .env"
fi

SERVICE_NAME="${SERVICE_NAME:-overwallbot}"
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  echo "==> Restarting service ${SERVICE_NAME}..."
  $SUDO systemctl restart "$SERVICE_NAME" || true
  echo "OK. Live logs: $SUDO journalctl -u $SERVICE_NAME -f"
else
  echo "==> Ready."
  echo "Run manually:        bash run.sh"
  echo "Run as service:      bash install_service.sh"
fi
