#!/usr/bin/env bash

set -euo pipefail

ACTION="${1:-up}"
NEXUS_DIR="/home/ubuntu/git/ghabs/nexus"
ENV_FILE="$NEXUS_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE"
  echo "Create it from $NEXUS_DIR/.env.example"
  exit 1
fi

DEPLOY_TYPE=$(grep -E '^DEPLOY_TYPE=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]')
DEPLOY_TYPE=${DEPLOY_TYPE:-compose}

NEXUS_RUNTIME_DIR=$(grep -E '^NEXUS_RUNTIME_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')
NEXUS_CORE_STORAGE_DIR=$(grep -E '^NEXUS_CORE_STORAGE_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')
LOGS_DIR=$(grep -E '^LOGS_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')
NEXUS_RUNTIME_DIR=${NEXUS_RUNTIME_DIR:-/var/lib/nexus}
NEXUS_CORE_STORAGE_DIR=${NEXUS_CORE_STORAGE_DIR:-$NEXUS_RUNTIME_DIR/nexus-core}
NEXUS_STATE_DIR="$NEXUS_RUNTIME_DIR/state"
LOGS_DIR=${LOGS_DIR:-/var/log/nexus}

ensure_runtime_dirs() {
  sudo mkdir -p "$NEXUS_RUNTIME_DIR" "$NEXUS_STATE_DIR" "$NEXUS_CORE_STORAGE_DIR" "$LOGS_DIR"
  sudo chown -R ubuntu:ubuntu "$NEXUS_RUNTIME_DIR" "$LOGS_DIR"
}

run_compose() {
  cd "$NEXUS_DIR"
  case "$ACTION" in
    up)
      docker compose up -d --build
      ;;
    down)
      docker compose down
      ;;
    restart)
      docker compose down
      docker compose up -d --build
      ;;
    status)
      docker compose ps
      ;;
    logs)
      docker compose logs -f bot processor webhook health
      ;;
    *)
      echo "Unknown action: $ACTION"
      echo "Usage: $0 [up|down|restart|status|logs]"
      exit 1
      ;;
  esac
}

run_systemd() {
  ensure_runtime_dirs
  case "$ACTION" in
    up)
      sudo systemctl daemon-reload
      sudo systemctl enable --now nexus-bot nexus-processor nexus-webhook nexus-health
      ;;
    down)
      sudo systemctl disable --now nexus-bot nexus-processor nexus-webhook nexus-health || true
      ;;
    restart)
      sudo systemctl restart nexus-bot nexus-processor nexus-webhook nexus-health
      ;;
    status)
      sudo systemctl status nexus-bot nexus-processor nexus-webhook nexus-health --no-pager
      ;;
    logs)
      sudo journalctl -u nexus-bot -u nexus-processor -u nexus-webhook -u nexus-health -f
      ;;
    *)
      echo "Unknown action: $ACTION"
      echo "Usage: $0 [up|down|restart|status|logs]"
      exit 1
      ;;
  esac
}

case "$DEPLOY_TYPE" in
  compose)
    run_compose
    ;;
  systemd)
    run_systemd
    ;;
  *)
    echo "Invalid DEPLOY_TYPE=$DEPLOY_TYPE (expected compose or systemd)"
    exit 1
    ;;
esac
