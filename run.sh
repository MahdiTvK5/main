#!/usr/bin/env bash
# Run the bot in the foreground. Stop with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"

# Create the venv if missing or broken, then install deps.
if [ ! -x .venv/bin/python ]; then
  rm -rf .venv
  if ! python3 -m venv .venv; then
    echo "ERROR: Could not create a Python virtual environment."
    echo "  sudo apt update && sudo apt install -y python3-venv python3-pip"
    exit 1
  fi
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "ERROR: .env not found. First run: cp .env.example .env && nano .env"
  exit 1
fi

exec .venv/bin/python main.py
