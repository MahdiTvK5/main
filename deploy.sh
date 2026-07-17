#!/usr/bin/env bash
# Pull latest code and install dependencies on the server.
set -euo pipefail
cd "$(dirname "$0")"

# --- Git pull (only when this really is a git checkout) ---
if [ -d .git ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "==> Pulling latest from git..."
  git pull --ff-only || git pull || true
else
  echo "==> Skipping git pull (this folder is not a git repository)."
  echo "    For auto-update next time, install with:"
  echo "      git clone <repo-url> overwallbot && cd overwallbot"
fi

# --- Python venv (robust: recreate if missing OR broken) ---
echo "==> Setting up Python venv..."
if [ ! -x .venv/bin/python ]; then
  # A previous failed run can leave a partial .venv with no python/activate.
  rm -rf .venv
  if ! python3 -m venv .venv; then
    echo "ERROR: Could not create a Python virtual environment."
    echo "Install the required packages first, then re-run this script:"
    echo "  sudo apt update && sudo apt install -y python3-venv python3-pip"
    exit 1
  fi
fi

VENV_PY=".venv/bin/python"

echo "==> Installing dependencies..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r requirements.txt

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
