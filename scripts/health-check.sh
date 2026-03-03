#!/usr/bin/env bash

set -euo pipefail

UNITS=("nexus-bot" "nexus-processor" "nexus-webhook" "nexus-health")
FAILED=0

check_unit() {
  local unit="$1"
  if sudo systemctl is-active --quiet "$unit"; then
    echo "$unit: active"
  else
    echo "$unit: inactive"
    FAILED=1
  fi
}

check_http() {
  local name="$1"
  local url="$2"
  if curl -fsS --max-time 5 "$url" >/dev/null; then
    echo "$name: healthy"
  else
    echo "$name: unhealthy"
    FAILED=1
  fi
}

for unit in "${UNITS[@]}"; do
  check_unit "$unit"
done

check_http "health-endpoint" "http://localhost:8080/health"
check_http "webhook-endpoint" "http://localhost:8081/health"

if [[ "$FAILED" -ne 0 ]]; then
  echo "Health check failed"
  exit 1
fi

echo "Health check passed"
