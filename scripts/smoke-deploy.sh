#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy.sh"

FAILED=0

run_check() {
  local name="$1"
  shift
  if "$@" >/tmp/nexus-smoke.out 2>&1; then
    echo "$name: pass"
  else
    echo "$name: fail"
    tail -n 20 /tmp/nexus-smoke.out || true
    FAILED=1
  fi
}

run_check "status" "$DEPLOY_SCRIPT" status
run_check "status-quiet" "$DEPLOY_SCRIPT" status --quiet

if [[ "$FAILED" -ne 0 ]]; then
  echo "Smoke deploy failed"
  exit 1
fi

echo "Smoke deploy passed"
