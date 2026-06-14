#!/usr/bin/env bash
# Run the bot in the foreground. Stop with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

if [ ! -f .env ]; then
  echo "ERROR: .env not found. First run: cp .env.example .env && nano .env"
  exit 1
fi

exec python main.py
