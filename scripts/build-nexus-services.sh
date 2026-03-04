#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="/etc/systemd/system"

services=(
  nexus-telegram.service
  nexus-discord.service
  nexus-processor.service
  nexus-webhook.service
  nexus-health.service
)

echo "Syncing Nexus systemd unit files to ${UNIT_DIR}..."
for svc in "${services[@]}"; do
  src="${ROOT_DIR}/${svc}"
  dst="${UNIT_DIR}/${svc}"

  if [[ ! -f "${src}" ]]; then
    echo "Missing source unit file: ${src}" >&2
    exit 1
  fi

  sudo cp "${src}" "${dst}"
  echo "Installed ${svc}"
done

echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Enabling Nexus services..."
sudo systemctl enable "${services[@]}"

echo "Restarting Nexus services..."
sudo systemctl restart "${services[@]}"

echo "Service status:"
for svc in "${services[@]}"; do
  state="$(systemctl is-active "${svc}" || true)"
  echo "${svc}: ${state}"
done

echo "Done."
