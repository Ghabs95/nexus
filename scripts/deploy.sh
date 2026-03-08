#!/usr/bin/env bash

set -euo pipefail

NEXUS_DIR="/home/ubuntu/git/ghabs/nexus"
ENV_FILE="$NEXUS_DIR/.env"
LOGGING_COMPOSE_FILE="/home/ubuntu/git/ghabs/vsc-server-infra/logging/docker-compose.yml"
SYSTEMD_UNITS=("nexus-telegram" "nexus-discord" "nexus-processor" "nexus-webhook" "nexus-health")
COMPOSE_SERVICES=("bot" "processor" "webhook" "health")
COMPOSE_PROFILE="${COMPOSE_PROFILE:-local}"
COMPOSE_BUILD=true
OBSERVABILITY_ENABLED="${OBSERVABILITY_ENABLED:-false}"
INFRA_ENABLED="${INFRA_ENABLED:-false}"

ACTION="up"
QUIET=false
ACTION_SET=false

CLI_DEPLOY_TYPE=""
CLI_COMPOSE_PROFILE=""
CLI_COMPOSE_BUILD=""
CLI_OBSERVABILITY=""
CLI_INFRA=""
CLI_NEXUS_RUNTIME_DIR=""
CLI_NEXUS_CORE_STORAGE_DIR=""
CLI_LOGS_DIR=""
CLI_SERVICES=""

usage() {
  cat <<EOF
Usage: $0 [up|down|restart|status|logs] [options]

Options:
  --quiet                             Only valid with status
  --deploy-type <compose|systemd>
  --docker                            Alias for --deploy-type compose
  --systemd                           Alias for --deploy-type systemd
  --compose-profile <local|prod>      Compose profile (default: local)
  --observability / --no-observability Include Loki/Promtail/Grafana compose stack
  --infra                             Include Postgres/Redis services
  --build / --no-build                Compose up/restart image build behavior
  --nexus-runtime-dir <path>
  --nexus-core-storage-dir <path>
  --logs-dir <path>
  --services <list>                   Comma-separated subset (e.g. "webhook,processor")
  -h, --help
EOF
}

die() {
  echo "$1" >&2
  exit 1
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf "%s" "$value"
}

strip_inline_comment() {
  local input="$1"
  local out=""
  local in_single=0
  local in_double=0
  local escaped=0
  local i

  for ((i = 0; i < ${#input}; i++)); do
    local ch="${input:i:1}"
    if ((escaped)); then
      out+="$ch"
      escaped=0
      continue
    fi
    if [[ "$ch" == "\\" && $in_single -eq 0 ]]; then
      out+="$ch"
      escaped=1
      continue
    fi
    if [[ "$ch" == "'" && $in_double -eq 0 ]]; then
      ((in_single = 1 - in_single))
      out+="$ch"
      continue
    fi
    if [[ "$ch" == '"' && $in_single -eq 0 ]]; then
      ((in_double = 1 - in_double))
      out+="$ch"
      continue
    fi
    if [[ "$ch" == "#" && $in_single -eq 0 && $in_double -eq 0 ]]; then
      break
    fi
    out+="$ch"
  done
  printf "%s" "$out"
}

parse_env_value() {
  local rhs
  rhs="$(trim "$(strip_inline_comment "$1")")"
  if [[ "$rhs" =~ ^"(.*)"$ ]]; then
    printf "%s" "${BASH_REMATCH[1]}"
    return 0
  fi
  if [[ "$rhs" =~ ^\'(.*)\'$ ]]; then
    printf "%s" "${BASH_REMATCH[1]}"
    return 0
  fi
  printf "%s" "$rhs"
}

read_env_var() {
  local key="$1"
  local value=""
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$(trim "$line")" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      local name="${BASH_REMATCH[2]}"
      local rhs="${BASH_REMATCH[3]}"
      if [[ "$name" == "$key" ]]; then
        value="$(parse_env_value "$rhs")"
      fi
    fi
  done < "$ENV_FILE"
  printf "%s" "$value"
}

resolve_value() {
  local var_name="$1"
  local cli_value="$2"
  local dotenv_value="$3"
  local default_value="$4"
  local shell_value="${!var_name-}"

  if [[ -n "$cli_value" ]]; then
    printf "%s" "$cli_value"
  elif [[ -n "$shell_value" ]]; then
    printf "%s" "$shell_value"
  elif [[ -n "$dotenv_value" ]]; then
    printf "%s" "$dotenv_value"
  else
    printf "%s" "$default_value"
  fi
}

resolve_compose_app_service() {
  local token
  token="$(trim "$1")"
  case "$token" in
    bot|telegram|discord)
      if [[ "$COMPOSE_PROFILE" == "prod" ]]; then
        echo "bot-prod"
      else
        echo "bot"
      fi
      ;;
    processor)
      if [[ "$COMPOSE_PROFILE" == "prod" ]]; then
        echo "processor-prod"
      else
        echo "processor"
      fi
      ;;
    webhook)
      if [[ "$COMPOSE_PROFILE" == "prod" ]]; then
        echo "webhook-prod"
      else
        echo "webhook"
      fi
      ;;
    health)
      if [[ "$COMPOSE_PROFILE" == "prod" ]]; then
        echo "health-prod"
      else
        echo "health"
      fi
      ;;
    bot-prod|processor-prod|webhook-prod|health-prod|bot)
      echo "$token"
      ;;
    *)
      echo ""
      ;;
  esac
}

resolve_systemd_unit() {
  local token
  token="$(trim "$1")"
  token="${token%.service}"
  case "$token" in
    telegram|nexus-telegram)
      echo "nexus-telegram"
      ;;
    discord|nexus-discord)
      echo "nexus-discord"
      ;;
    processor|nexus-processor)
      echo "nexus-processor"
      ;;
    webhook|nexus-webhook)
      echo "nexus-webhook"
      ;;
    health|nexus-health)
      echo "nexus-health"
      ;;
    *)
      echo ""
      ;;
  esac
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
      up|down|restart|status|logs)
        [[ "$ACTION_SET" == "false" ]] || die "Multiple actions provided"
        ACTION="$1"
        ACTION_SET=true
        shift
        ;;
      --quiet)
        QUIET=true
        shift
        ;;
      --deploy-type)
        [[ $# -ge 2 ]] || die "Missing value for --deploy-type"
        CLI_DEPLOY_TYPE="$2"
        shift 2
        ;;
      --deploy-type=*)
        CLI_DEPLOY_TYPE="${1#*=}"
        shift
        ;;
      --docker)
        CLI_DEPLOY_TYPE="compose"
        shift
        ;;
      --systemd)
        CLI_DEPLOY_TYPE="systemd"
        shift
        ;;
      --compose-profile|--profile)
        [[ $# -ge 2 ]] || die "Missing value for --compose-profile"
        CLI_COMPOSE_PROFILE="$2"
        shift 2
        ;;
      --compose-profile=*|--profile=*)
        CLI_COMPOSE_PROFILE="${1#*=}"
        shift
        ;;
      --build)
        CLI_COMPOSE_BUILD="true"
        shift
        ;;
      --no-build)
        CLI_COMPOSE_BUILD="false"
        shift
        ;;
      --observability)
        CLI_OBSERVABILITY="true"
        shift
        ;;
      --no-observability)
        CLI_OBSERVABILITY="false"
        shift
        ;;
      --infra)
        CLI_INFRA="true"
        shift
        ;;
      --nexus-runtime-dir)
        [[ $# -ge 2 ]] || die "Missing value for --nexus-runtime-dir"
        CLI_NEXUS_RUNTIME_DIR="$2"
        shift 2
        ;;
      --nexus-runtime-dir=*)
        CLI_NEXUS_RUNTIME_DIR="${1#*=}"
        shift
        ;;
      --nexus-core-storage-dir)
        [[ $# -ge 2 ]] || die "Missing value for --nexus-core-storage-dir"
        CLI_NEXUS_CORE_STORAGE_DIR="$2"
        shift 2
        ;;
      --nexus-core-storage-dir=*)
        CLI_NEXUS_CORE_STORAGE_DIR="${1#*=}"
        shift
        ;;
      --logs-dir)
        [[ $# -ge 2 ]] || die "Missing value for --logs-dir"
        CLI_LOGS_DIR="$2"
        shift 2
        ;;
      --logs-dir=*)
        CLI_LOGS_DIR="${1#*=}"
        shift
        ;;
      --services)
        [[ $# -ge 2 ]] || die "Missing value for --services"
        CLI_SERVICES="$2"
        shift 2
        ;;
      --services=*)
        CLI_SERVICES="${1#*=}"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --*)
        die "Unknown flag: $1"
        ;;
      *)
        die "Unexpected argument: $1"
        ;;
    esac
  done

  if [[ "$QUIET" == "true" && "$ACTION" != "status" ]]; then
    die "--quiet is only valid with status"
  fi
}

ensure_runtime_dirs() {
  sudo mkdir -p "$NEXUS_RUNTIME_DIR" "$NEXUS_STATE_DIR" "$NEXUS_CORE_STORAGE_DIR" "$LOGS_DIR"
  sudo chown -R ubuntu:ubuntu "$NEXUS_RUNTIME_DIR" "$LOGS_DIR"
}

compose_quiet_status() {
  local running
  running="$("${COMPOSE_CMD[@]}" ps --services --filter status=running 2>/dev/null || true)"
  for service in "${COMPOSE_SERVICES[@]}"; do
    if grep -Fxq "$service" <<< "$running"; then
      echo "$service: active"
    else
      echo "$service: inactive"
    fi
  done
}

run_compose() {
  local up_args=(-d)
  local app_services=()
  local target_services=()
  local filtered_services=()
  local token
  local resolved
  [[ "$COMPOSE_BUILD" == "true" ]] && up_args+=(--build)

  COMPOSE_CMD=(docker compose -f "$NEXUS_DIR/docker-compose.yml")
  if [[ "$OBSERVABILITY_ENABLED" == "true" ]]; then
    [[ -f "$LOGGING_COMPOSE_FILE" ]] || die "Missing logging compose file: $LOGGING_COMPOSE_FILE"
    COMPOSE_CMD+=(-f "$LOGGING_COMPOSE_FILE")
  fi
  COMPOSE_CMD+=(--profile "$COMPOSE_PROFILE")

  if [[ "$COMPOSE_PROFILE" == "prod" ]]; then
    app_services=("bot-prod" "processor-prod" "webhook-prod" "health-prod")
  else
    app_services=("bot" "processor" "webhook" "health")
  fi

  target_services=("${app_services[@]}")
  if [[ "$INFRA_ENABLED" == "true" ]]; then
    target_services+=("postgres" "redis")
  fi
  if [[ "$OBSERVABILITY_ENABLED" == "true" ]]; then
    target_services+=("loki" "promtail" "grafana")
  fi

  if [[ -n "$CLI_SERVICES" ]]; then
    IFS=',' read -r -a requested <<< "$CLI_SERVICES"
    for token in "${requested[@]}"; do
      token="$(trim "$token")"
      [[ -n "$token" ]] || continue

      resolved="$(resolve_compose_app_service "$token")"
      if [[ -z "$resolved" ]]; then
        case "$token" in
          postgres|redis|loki|promtail|grafana)
            resolved="$token"
            ;;
          *)
            die "Unknown compose service token: $token"
            ;;
        esac
      fi

      if ! printf '%s\n' "${target_services[@]}" | grep -Fxq "$resolved"; then
        die "Service '$resolved' is not enabled for current profile/options"
      fi

      if ! printf '%s\n' "${filtered_services[@]}" | grep -Fxq "$resolved"; then
        filtered_services+=("$resolved")
      fi
    done
    [[ ${#filtered_services[@]} -gt 0 ]] || die "--services resolved to an empty service set"
    target_services=("${filtered_services[@]}")
  fi

  COMPOSE_SERVICES=("${target_services[@]}")
  case "$ACTION" in
    up)
      "${COMPOSE_CMD[@]}" up "${up_args[@]}" "${target_services[@]}"
      ;;
    down)
      "${COMPOSE_CMD[@]}" stop "${target_services[@]}" || true
      "${COMPOSE_CMD[@]}" rm -f "${target_services[@]}" || true
      ;;
    restart)
      "${COMPOSE_CMD[@]}" up "${up_args[@]}" --force-recreate "${target_services[@]}"
      ;;
    status)
      if [[ "$QUIET" == "true" ]]; then
        compose_quiet_status
      else
        "${COMPOSE_CMD[@]}" ps
      fi
      ;;
    logs)
      "${COMPOSE_CMD[@]}" logs -f "${COMPOSE_SERVICES[@]}"
      ;;
  esac
}

run_systemd() {
  ensure_runtime_dirs
  local target_units=("${SYSTEMD_UNITS[@]}")
  local filtered_units=()
  local token
  local resolved

  if [[ -n "$CLI_SERVICES" ]]; then
    IFS=',' read -r -a requested <<< "$CLI_SERVICES"
    for token in "${requested[@]}"; do
      token="$(trim "$token")"
      [[ -n "$token" ]] || continue
      resolved="$(resolve_systemd_unit "$token")"
      [[ -n "$resolved" ]] || die "Unknown systemd service token: $token"
      if ! printf '%s\n' "${target_units[@]}" | grep -Fxq "$resolved"; then
        die "Service '$resolved' is not managed by this deploy script"
      fi
      if ! printf '%s\n' "${filtered_units[@]}" | grep -Fxq "$resolved"; then
        filtered_units+=("$resolved")
      fi
    done
    [[ ${#filtered_units[@]} -gt 0 ]] || die "--services resolved to an empty unit set"
    target_units=("${filtered_units[@]}")
  fi

  case "$ACTION" in
    up)
      sudo systemctl daemon-reload
      sudo systemctl enable --now "${target_units[@]}"
      ;;
    down)
      sudo systemctl disable --now "${target_units[@]}" || true
      ;;
    restart)
      sudo systemctl restart "${target_units[@]}"
      ;;
    status)
      if [[ "$QUIET" == "true" ]]; then
        for unit in "${target_units[@]}"; do
          if sudo systemctl is-active --quiet "$unit"; then
            echo "$unit: active"
          else
            echo "$unit: inactive"
          fi
        done
      else
        sudo systemctl status "${target_units[@]}" --no-pager
      fi
      ;;
    logs)
      local journal_units=()
      for unit in "${target_units[@]}"; do
        journal_units+=(-u "$unit")
      done
      sudo journalctl "${journal_units[@]}" -f
      ;;
  esac
}

parse_args "$@"

[[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE (create from $NEXUS_DIR/.env.example)"

DOTENV_DEPLOY_TYPE="$(read_env_var "DEPLOY_TYPE")"
DOTENV_COMPOSE_PROFILE="$(read_env_var "COMPOSE_PROFILE")"
DOTENV_OBSERVABILITY="$(read_env_var "OBSERVABILITY_ENABLED")"
DOTENV_INFRA="$(read_env_var "INFRA_ENABLED")"
DOTENV_NEXUS_RUNTIME_DIR="$(read_env_var "NEXUS_RUNTIME_DIR")"
DOTENV_NEXUS_CORE_STORAGE_DIR="$(read_env_var "NEXUS_CORE_STORAGE_DIR")"
DOTENV_LOGS_DIR="$(read_env_var "LOGS_DIR")"

DEPLOY_TYPE="$(resolve_value "DEPLOY_TYPE" "$CLI_DEPLOY_TYPE" "$DOTENV_DEPLOY_TYPE" "compose")"
COMPOSE_PROFILE="$(resolve_value "COMPOSE_PROFILE" "$CLI_COMPOSE_PROFILE" "$DOTENV_COMPOSE_PROFILE" "$COMPOSE_PROFILE")"
OBSERVABILITY_ENABLED="$(resolve_value "OBSERVABILITY_ENABLED" "$CLI_OBSERVABILITY" "$DOTENV_OBSERVABILITY" "$OBSERVABILITY_ENABLED")"
INFRA_ENABLED="$(resolve_value "INFRA_ENABLED" "$CLI_INFRA" "$DOTENV_INFRA" "$INFRA_ENABLED")"
if [[ -n "$CLI_COMPOSE_BUILD" ]]; then
  COMPOSE_BUILD="$CLI_COMPOSE_BUILD"
fi
NEXUS_RUNTIME_DIR="$(resolve_value "NEXUS_RUNTIME_DIR" "$CLI_NEXUS_RUNTIME_DIR" "$DOTENV_NEXUS_RUNTIME_DIR" "/var/lib/nexus")"
NEXUS_CORE_STORAGE_DIR="$(resolve_value "NEXUS_CORE_STORAGE_DIR" "$CLI_NEXUS_CORE_STORAGE_DIR" "$DOTENV_NEXUS_CORE_STORAGE_DIR" "$NEXUS_RUNTIME_DIR/nexus-arc")"
LOGS_DIR="$(resolve_value "LOGS_DIR" "$CLI_LOGS_DIR" "$DOTENV_LOGS_DIR" "/var/log/nexus")"
NEXUS_STATE_DIR="$NEXUS_RUNTIME_DIR/state"

case "$DEPLOY_TYPE" in
  compose)
    case "$COMPOSE_PROFILE" in
      local|prod) ;;
      *)
        die "Invalid COMPOSE_PROFILE=$COMPOSE_PROFILE (expected local or prod)"
        ;;
    esac
    case "$OBSERVABILITY_ENABLED" in
      true|false) ;;
      *)
        die "Invalid OBSERVABILITY_ENABLED=$OBSERVABILITY_ENABLED (expected true or false)"
        ;;
    esac
    case "$INFRA_ENABLED" in
      true|false) ;;
      *)
        die "Invalid INFRA_ENABLED=$INFRA_ENABLED (expected true or false)"
        ;;
    esac
    run_compose
    ;;
  systemd)
    run_systemd
    ;;
  *)
    die "Invalid DEPLOY_TYPE=$DEPLOY_TYPE (expected compose or systemd)"
    ;;
esac
