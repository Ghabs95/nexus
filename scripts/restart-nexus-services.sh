#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="${SCRIPT_DIR}/deploy.sh"

DEPLOY_TYPE="${DEPLOY_TYPE:-compose}"
COMPOSE_PROFILE="${COMPOSE_PROFILE:-local}"
OBSERVABILITY="${OBSERVABILITY_ENABLED:-false}"
INFRA="${INFRA_ENABLED:-false}"
COMPOSE_BUILD="true"
TARGET_SERVICES=""

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --docker                    Use Docker Compose deployment
  --systemd                   Use systemd deployment
  --compose-profile <profile> Compose profile (local|prod), default: local
  --observability / --no-observability Include Loki/Promtail/Grafana stack (compose only)
  --infra                    Include Postgres/Redis stack (compose only)
  --build                     Build images on restart (compose only, default)
  --no-build                  Do not build images on restart (compose only)
  --services <list>           Comma-separated subset (e.g. "webhook,processor")
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
    --build)
      COMPOSE_BUILD="true"
      shift
      ;;
    --no-build)
      COMPOSE_BUILD="false"
      shift
      ;;
    --services)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }
      TARGET_SERVICES="$2"
      shift 2
      ;;
    --services=*)
      TARGET_SERVICES="${1#*=}"
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

if [[ ! -x "$DEPLOY_SCRIPT" ]]; then
  echo "Missing deploy script: $DEPLOY_SCRIPT" >&2
  exit 1
fi

if [[ "$DEPLOY_TYPE" == "compose" ]]; then
  OBS_ARGS=()
  if [[ "$OBSERVABILITY" == "true" ]]; then
    OBS_ARGS+=(--observability)
  else
    OBS_ARGS+=(--no-observability)
  fi
  INFRA_ARGS=()
  if [[ "$INFRA" == "true" ]]; then
    INFRA_ARGS+=(--infra)
  fi
  SERVICE_ARGS=()
  if [[ -n "$TARGET_SERVICES" ]]; then
    SERVICE_ARGS+=(--services "$TARGET_SERVICES")
  fi
  if [[ "$COMPOSE_BUILD" == "true" ]]; then
    exec "$DEPLOY_SCRIPT" restart --docker --compose-profile "$COMPOSE_PROFILE" "${OBS_ARGS[@]}" "${INFRA_ARGS[@]}" "${SERVICE_ARGS[@]}" --build
  fi
  exec "$DEPLOY_SCRIPT" restart --docker --compose-profile "$COMPOSE_PROFILE" "${OBS_ARGS[@]}" "${INFRA_ARGS[@]}" "${SERVICE_ARGS[@]}" --no-build
fi

if [[ -n "$TARGET_SERVICES" ]]; then
  exec "$DEPLOY_SCRIPT" restart --systemd --services "$TARGET_SERVICES"
fi
exec "$DEPLOY_SCRIPT" restart --systemd
