#!/usr/bin/env bash
# Install the bot as a systemd service (auto start on boot, auto restart on crash).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-overwallbot}"
SVC_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "==> Preparing environment (deploy.sh)..."
  bash "$DIR/deploy.sh"
fi

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "==> Writing service to $SVC_PATH ..."
$SUDO tee "$SVC_PATH" >/dev/null <<EOF
[Unit]
Description=OverWallVpn Telegram Bot
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/main.py
Restart=always
RestartSec=5
# To run as a specific user, uncomment and set:
# User=youruser

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE_NAME"
echo "OK. Installed."
echo "Status:    $SUDO systemctl status $SERVICE_NAME --no-pager"
echo "Live logs: $SUDO journalctl -u $SERVICE_NAME -f"
echo "Restart:   $SUDO systemctl restart $SERVICE_NAME"
