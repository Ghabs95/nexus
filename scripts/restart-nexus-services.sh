#!/usr/bin/env bash
set -euo pipefail

services=(
  nexus-bot.service
  nexus-processor.service
  nexus-webhook.service
  nexus-health.service
)

echo "Restarting Nexus services..."
sudo systemctl restart "${services[@]}"

echo "Checking status..."
for svc in "${services[@]}"; do
  state=$(systemctl is-active "$svc" || true)
  echo "$svc: $state"
done

echo "Done."
