#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"
DEPLOY_SCRIPT="${SCRIPT_DIR}/deploy.sh"
UNIT_DIR="/etc/systemd/system"
LOGGING_COMPOSE_FILE="/home/ubuntu/git/ghabs/vsc-server-infra/logging/docker-compose.yml"

SYSTEMD_SERVICES=(
  nexus-telegram.service
  nexus-discord.service
  nexus-processor.service
  nexus-webhook.service
  nexus-health.service
)

DEPLOY_TYPE="${DEPLOY_TYPE:-compose}"
COMPOSE_PROFILE="${COMPOSE_PROFILE:-local}"
OBSERVABILITY="${OBSERVABILITY_ENABLED:-false}"
INFRA="${INFRA_ENABLED:-false}"
START_AFTER_BUILD="true"

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --docker                    Use Docker Compose deployment
  --systemd                   Use systemd deployment
  --compose-profile <profile> Compose profile (local|prod), default: local
  --observability / --no-observability Include Loki/Promtail/Grafana stack (compose only)
  --infra                    Include Postgres/Redis stack (compose only)
  --build-only                Build only (compose) / sync units only (systemd)
  --up                        Build then start/restart services (default)
  -h, --help
USAGE
}

while (($# > 0)); do
  case "$1" in
    --docker)
      DEPLOY_TYPE="compose"
      shift
      ;;
    --systemd)
      DEPLOY_TYPE="systemd"
      shift
      ;;
    --compose-profile|--profile)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }
      COMPOSE_PROFILE="$2"
      shift 2
      ;;
    --compose-profile=*|--profile=*)
      COMPOSE_PROFILE="${1#*=}"
      shift
      ;;
    --observability)
      OBSERVABILITY="true"
      shift
      ;;
    --no-observability)
      OBSERVABILITY="false"
      shift
      ;;
    --infra)
      INFRA="true"
      shift
      ;;
    --build-only)
      START_AFTER_BUILD="false"
      shift
      ;;
    --up)
      START_AFTER_BUILD="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$DEPLOY_TYPE" == "compose" ]]; then
  cd "$ROOT_DIR"
  COMPOSE_CMD=(docker compose -f "$ROOT_DIR/docker-compose.yml")
  case "$COMPOSE_PROFILE" in
    local)
      COMPOSE_CMD+=(-f "$ROOT_DIR/docker-compose.local.yml")
      ;;
    prod)
      COMPOSE_CMD+=(-f "$ROOT_DIR/docker-compose.prod.yml")
      ;;
    *)
      echo "Invalid compose profile: $COMPOSE_PROFILE (expected local or prod)" >&2
      exit 1
      ;;
  esac
  OBS_ARGS=()
  if [[ "$OBSERVABILITY" == "true" ]]; then
    [[ -f "$LOGGING_COMPOSE_FILE" ]] || { echo "Missing logging compose file: $LOGGING_COMPOSE_FILE" >&2; exit 1; }
    COMPOSE_CMD+=(-f "$LOGGING_COMPOSE_FILE")
    OBS_ARGS+=(--observability)
  else
    OBS_ARGS+=(--no-observability)
  fi
  INFRA_ARGS=()
  if [[ "$INFRA" == "true" ]]; then
    INFRA_ARGS+=(--infra)
  fi
  echo "Building Docker services for profile '$COMPOSE_PROFILE'..."
  "${COMPOSE_CMD[@]}" build

  if [[ "$START_AFTER_BUILD" == "true" ]]; then
    echo "Restarting Docker services..."
    exec "$DEPLOY_SCRIPT" restart --docker --compose-profile "$COMPOSE_PROFILE" "${OBS_ARGS[@]}" "${INFRA_ARGS[@]}" --no-build
  fi

  echo "Build complete (no restart requested)."
  exit 0
fi

echo "Syncing Nexus systemd unit files to ${UNIT_DIR}..."
for svc in "${SYSTEMD_SERVICES[@]}"; do
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

if [[ "$START_AFTER_BUILD" == "true" ]]; then
  echo "Enabling Nexus services..."
  sudo systemctl enable "${SYSTEMD_SERVICES[@]}"

  echo "Restarting Nexus services..."
  sudo systemctl restart "${SYSTEMD_SERVICES[@]}"

  echo "Service status:"
  for svc in "${SYSTEMD_SERVICES[@]}"; do
    state="$(systemctl is-active "${svc}" || true)"
    echo "${svc}: ${state}"
  done
else
  echo "Unit files synced (no restart requested)."
fi

echo "Done."
