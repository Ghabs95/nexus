#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="${SCRIPT_DIR}/deploy.sh"

DEPLOY_TYPE="${DEPLOY_TYPE:-compose}"
COMPOSE_PROFILE="${COMPOSE_PROFILE:-local}"
OBSERVABILITY="${OBSERVABILITY_ENABLED:-false}"
INFRA="${INFRA_ENABLED:-false}"
QUIET="false"

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --docker                    Use Docker Compose deployment
  --systemd                   Use systemd deployment
  --compose-profile <profile> Compose profile (local|prod), default: local
  --observability / --no-observability Include Loki/Promtail/Grafana stack (compose only)
  --infra                    Include Postgres/Redis stack (compose only)
  --quiet                     Compact status output
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
    --quiet)
      QUIET="true"
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
  if [[ "$QUIET" == "true" ]]; then
    exec "$DEPLOY_SCRIPT" status --docker --compose-profile "$COMPOSE_PROFILE" "${OBS_ARGS[@]}" "${INFRA_ARGS[@]}" --quiet
  fi
  exec "$DEPLOY_SCRIPT" status --docker --compose-profile "$COMPOSE_PROFILE" "${OBS_ARGS[@]}" "${INFRA_ARGS[@]}"
fi

if [[ "$QUIET" == "true" ]]; then
  exec "$DEPLOY_SCRIPT" status --systemd --quiet
fi
exec "$DEPLOY_SCRIPT" status --systemd
