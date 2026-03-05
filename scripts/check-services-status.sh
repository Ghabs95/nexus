#!/usr/bin/env bash

set -euo pipefail

SERVICES=(
  nexus-telegram.service
  nexus-discord.service
  nexus-processor.service
  nexus-webhook.service
  nexus-health.service
)

if ! command -v systemctl >/dev/null 2>&1; then
  echo "Error: systemctl is not available on this host."
  exit 2
fi

printf "%-28s %-10s %-10s %s\n" "SERVICE" "ACTIVE" "ENABLED" "DETAIL"
printf "%-28s %-10s %-10s %s\n" "-------" "------" "-------" "------"

for svc in "${SERVICES[@]}"; do
  active="$(systemctl is-active "$svc" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$svc" 2>/dev/null || true)"
  detail="$(systemctl show "$svc" --property=SubState --property=Result --no-pager 2>/dev/null || true)"
  detail="$(echo "$detail" | tr '\n' ' ' | sed 's/ $//')"

  [[ -z "$active" ]] && active="unknown"
  [[ -z "$enabled" ]] && enabled="unknown"
  [[ -z "$detail" ]] && detail="not found"

  printf "%-28s %-10s %-10s %s\n" "$svc" "$active" "$enabled" "$detail"
done
