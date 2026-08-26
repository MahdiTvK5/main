#!/usr/bin/env bash
# Install the bot as a systemd service (auto start on boot, auto restart on crash).
#
# SERVICE_NAME can be overridden to install branch copies side by side with the
# production instance, e.g.:
#   SERVICE_NAME=overwallbot-mybranch bash install_service.sh
# deploy.sh does exactly that for you when installing a branch.
set -euo pipefail
# SERVICE_NAME and INSTALL_DIR can be overridden so that isolated branch copies
# (created by deploy.sh) get their own systemd unit next to the production one:
#   SERVICE_NAME=overwallbot-mybranch INSTALL_DIR=/path/to/copy bash install_service.sh
DIR="${INSTALL_DIR:-$(cd "$(dirname "$0")" && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-overwallbot}"
SVC_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "ERROR: virtual environment not found in $DIR."
  echo "First run:  bash deploy.sh   (it builds .venv, installs deps and installs this service)"
  exit 1
fi

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "==> Writing service to $SVC_PATH ..."
$SUDO tee "$SVC_PATH" >/dev/null <<EOF
[Unit]
Description=OverWallVpn Telegram Bot (${SERVICE_NAME})
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
echo "OK. Installed as ${SERVICE_NAME}."
echo "Status:    ${SUDO} systemctl status ${SERVICE_NAME} --no-pager"
echo "Live logs: ${SUDO} journalctl -u ${SERVICE_NAME} -f"
echo "Restart:   ${SUDO} systemctl restart ${SERVICE_NAME}"
