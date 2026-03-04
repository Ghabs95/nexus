#!/usr/bin/env bash

set -euo pipefail

NEXUS_DIR="/home/ubuntu/git/ghabs/nexus"
ENV_FILE="$NEXUS_DIR/.env"
SYSTEMD_UNITS=("nexus-telegram" "nexus-processor" "nexus-webhook" "nexus-health")
COMPOSE_SERVICES=("bot" "processor" "webhook" "health")

ACTION="up"
QUIET=false
ACTION_SET=false

CLI_DEPLOY_TYPE=""
CLI_NEXUS_RUNTIME_DIR=""
CLI_NEXUS_CORE_STORAGE_DIR=""
CLI_LOGS_DIR=""

usage() {
  cat <<EOF
Usage: $0 [up|down|restart|status|logs] [options]

Options:
  --quiet                             Only valid with status
  --deploy-type <compose|systemd>
  --nexus-runtime-dir <path>
  --nexus-core-storage-dir <path>
  --logs-dir <path>
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
  running="$(docker compose ps --services --filter status=running 2>/dev/null || true)"
  for service in "${COMPOSE_SERVICES[@]}"; do
    if grep -Fxq "$service" <<< "$running"; then
      echo "$service: active"
    else
      echo "$service: inactive"
    fi
  done
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
      if [[ "$QUIET" == "true" ]]; then
        compose_quiet_status
      else
        docker compose ps
      fi
      ;;
    logs)
      docker compose logs -f bot processor webhook health
      ;;
  esac
}

run_systemd() {
  ensure_runtime_dirs
  case "$ACTION" in
    up)
      sudo systemctl daemon-reload
      sudo systemctl enable --now "${SYSTEMD_UNITS[@]}"
      ;;
    down)
      sudo systemctl disable --now "${SYSTEMD_UNITS[@]}" || true
      ;;
    restart)
      sudo systemctl restart "${SYSTEMD_UNITS[@]}"
      ;;
    status)
      if [[ "$QUIET" == "true" ]]; then
        for unit in "${SYSTEMD_UNITS[@]}"; do
          if sudo systemctl is-active --quiet "$unit"; then
            echo "$unit: active"
          else
            echo "$unit: inactive"
          fi
        done
      else
        sudo systemctl status "${SYSTEMD_UNITS[@]}" --no-pager
      fi
      ;;
    logs)
      sudo journalctl -u nexus-telegram -u nexus-processor -u nexus-webhook -u nexus-health -f
      ;;
  esac
}

parse_args "$@"

[[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE (create from $NEXUS_DIR/.env.example)"

DOTENV_DEPLOY_TYPE="$(read_env_var "DEPLOY_TYPE")"
DOTENV_NEXUS_RUNTIME_DIR="$(read_env_var "NEXUS_RUNTIME_DIR")"
DOTENV_NEXUS_CORE_STORAGE_DIR="$(read_env_var "NEXUS_CORE_STORAGE_DIR")"
DOTENV_LOGS_DIR="$(read_env_var "LOGS_DIR")"

DEPLOY_TYPE="$(resolve_value "DEPLOY_TYPE" "$CLI_DEPLOY_TYPE" "$DOTENV_DEPLOY_TYPE" "compose")"
NEXUS_RUNTIME_DIR="$(resolve_value "NEXUS_RUNTIME_DIR" "$CLI_NEXUS_RUNTIME_DIR" "$DOTENV_NEXUS_RUNTIME_DIR" "/var/lib/nexus")"
NEXUS_CORE_STORAGE_DIR="$(resolve_value "NEXUS_CORE_STORAGE_DIR" "$CLI_NEXUS_CORE_STORAGE_DIR" "$DOTENV_NEXUS_CORE_STORAGE_DIR" "$NEXUS_RUNTIME_DIR/nexus-arc")"
LOGS_DIR="$(resolve_value "LOGS_DIR" "$CLI_LOGS_DIR" "$DOTENV_LOGS_DIR" "/var/log/nexus")"
NEXUS_STATE_DIR="$NEXUS_RUNTIME_DIR/state"

case "$DEPLOY_TYPE" in
  compose)
    run_compose
    ;;
  systemd)
    run_systemd
    ;;
  *)
    die "Invalid DEPLOY_TYPE=$DEPLOY_TYPE (expected compose or systemd)"
    ;;
esac
