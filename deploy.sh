#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Working directory: $(pwd)"

# --- Verify Git repository ---
if [ ! -d .git ] || ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: This folder is not a git repository."
  exit 1
fi

# --- Protect local environment/config files ---
if [ -f .env ]; then
  cp .env /tmp/overwallbot.env.backup
  echo "==> .env backed up."
fi

# --- Fetch latest code ---
echo "==> Fetching origin..."
git fetch origin

# --- Always deploy origin/main ---
echo "==> Switching to main..."
git checkout main

echo "==> Resetting code to origin/main..."
git reset --hard origin/main

echo "==> Current version:"
git log -1 --oneline --decorate

# --- Restore .env ---
if [ -f /tmp/overwallbot.env.backup ]; then
  cp /tmp/overwallbot.env.backup .env
  rm -f /tmp/overwallbot.env.backup
  echo "==> .env restored."
elif [ ! -f .env ]; then
  cp .env.example .env
  echo "WARNING: .env created. Configure it before running the bot."
fi

# --- Python venv ---
echo "==> Checking Python virtual environment..."

if [ ! -x .venv/bin/python ]; then
  echo "==> Creating virtual environment..."
  rm -rf .venv
  python3 -m venv .venv
fi

VENV_PY=".venv/bin/python"

# --- Dependencies ---
echo "==> Installing dependencies..."
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r requirements.txt

# --- Syntax check ---
echo "==> Checking Python syntax..."
"$VENV_PY" -m py_compile \
  main.py \
  db.py \
  pricing.py \
  rewards.py \
  webpanel.py

# --- Restart service ---
SERVICE_NAME="${SERVICE_NAME:-overwallbot}"

if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then

  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
  fi

  echo "==> Restarting ${SERVICE_NAME}..."
  $SUDO systemctl restart "$SERVICE_NAME"

  sleep 2

  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "======================================"
    echo " UPDATE SUCCESSFUL"
    echo "======================================"
    echo "Version:"
    git log -1 --oneline --decorate
    echo
    echo "Service:"
    $SUDO systemctl status "$SERVICE_NAME" --no-pager -l
  else
    echo "ERROR: Service failed to start."
    echo
    $SUDO journalctl -u "$SERVICE_NAME" -n 50 --no-pager
    exit 1
  fi

else
  echo "==> Service ${SERVICE_NAME} not installed."
  echo "Run:"
  echo "  bash install_service.sh"
fi
