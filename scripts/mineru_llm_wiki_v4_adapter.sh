#!/usr/bin/env bash
set -euo pipefail

MINERU_REPO="${MINERU_REPO:-/root/MinerU}"
MINERU_API_HOST="${MINERU_API_HOST:-127.0.0.1}"
MINERU_API_PORT="${MINERU_API_PORT:-18000}"
MINERU_V4_ADAPTER_HOST="${MINERU_V4_ADAPTER_HOST:-127.0.0.1}"
MINERU_V4_ADAPTER_PORT="${MINERU_V4_ADAPTER_PORT:-18888}"
MINERU_V4_ADAPTER_TOKEN="${MINERU_V4_ADAPTER_TOKEN:-local-mineru-token}"
MINERU_V4_ADAPTER_LANG="${MINERU_V4_ADAPTER_LANG:-ch}"
MINERU_V4_ADAPTER_FORMULA_ENABLE="${MINERU_V4_ADAPTER_FORMULA_ENABLE:-true}"
MINERU_V4_ADAPTER_TABLE_ENABLE="${MINERU_V4_ADAPTER_TABLE_ENABLE:-true}"
MINERU_V4_ADAPTER_ALLOW_URL_FETCH="${MINERU_V4_ADAPTER_ALLOW_URL_FETCH:-0}"
MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS="${MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS:-86400}"
MINERU_USE_LOCAL_MODELS="${MINERU_USE_LOCAL_MODELS:-1}"
MINERU_RUN_DIR="${MINERU_RUN_DIR:-/tmp/mineru_llm_wiki_v4_adapter}"
MINERU_LOG_DIR="${MINERU_LOG_DIR:-${MINERU_RUN_DIR}/logs}"
MINERU_API_PID_FILE="${MINERU_API_PID_FILE:-${MINERU_RUN_DIR}/mineru-api.pid}"
MINERU_V4_ADAPTER_PID_FILE="${MINERU_V4_ADAPTER_PID_FILE:-${MINERU_RUN_DIR}/mineru-v4-adapter.pid}"

api_url="http://${MINERU_API_HOST}:${MINERU_API_PORT}"
adapter_url="http://${MINERU_V4_ADAPTER_HOST}:${MINERU_V4_ADAPTER_PORT}"
api_base="${adapter_url}/api/v4"

usage() {
  cat <<USAGE
Usage: $0 {start|stop|restart|status|logs|tail|health}

Environment overrides:
  MINERU_REPO                              default: /root/MinerU
  MINERU_API_HOST                          default: 127.0.0.1
  MINERU_API_PORT                          default: 8000
  MINERU_V4_ADAPTER_HOST                   default: 127.0.0.1
  MINERU_V4_ADAPTER_PORT                   default: 8888
  MINERU_V4_ADAPTER_TOKEN                  default: local-mineru-token
  MINERU_V4_ADAPTER_LANG                   default: ch
  MINERU_V4_ADAPTER_FORMULA_ENABLE         default: true
  MINERU_V4_ADAPTER_TABLE_ENABLE           default: true
  MINERU_V4_ADAPTER_ALLOW_URL_FETCH        default: 0
  MINERU_USE_LOCAL_MODELS                  default: 1
  MINERU_RUN_DIR                           default: /tmp/mineru_llm_wiki_v4_adapter

LLM Wiki API_BASE:
  ${api_base}
USAGE
}

prepare_dirs() {
  mkdir -p "${MINERU_RUN_DIR}" "${MINERU_LOG_DIR}"
}

export_model_env() {
  if [[ "${MINERU_USE_LOCAL_MODELS}" == "1" ]]; then
    export MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-local}"
    export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
    export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
  fi
}

is_running() {
  local pid_file="$1"
  [[ -f "${pid_file}" ]] || return 1
  local pid
  pid="$(cat "${pid_file}")"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

pid_value() {
  local pid_file="$1"
  if [[ -f "${pid_file}" ]]; then
    cat "${pid_file}"
  fi
}

wait_http() {
  local url="$1"
  local name="$2"
  local retries="${3:-120}"
  local delay="${4:-2}"
  local i
  for ((i = 1; i <= retries; i++)); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      echo "${name} is ready: ${url}"
      return 0
    fi
    sleep "${delay}"
  done
  echo "ERROR: timed out waiting for ${name}: ${url}" >&2
  return 1
}

start_api() {
  if is_running "${MINERU_API_PID_FILE}"; then
    echo "mineru-api already running, pid=$(pid_value "${MINERU_API_PID_FILE}")"
    return
  fi

  prepare_dirs
  export_model_env

  cd "${MINERU_REPO}"
  nohup mineru-api \
    --host "${MINERU_API_HOST}" \
    --port "${MINERU_API_PORT}" \
    >"${MINERU_LOG_DIR}/mineru-api.log" 2>&1 &
  echo "$!" >"${MINERU_API_PID_FILE}"
  echo "started mineru-api, pid=$(pid_value "${MINERU_API_PID_FILE}"), log=${MINERU_LOG_DIR}/mineru-api.log"
  wait_http "${api_url}/health" "mineru-api"
}

start_adapter() {
  if is_running "${MINERU_V4_ADAPTER_PID_FILE}"; then
    echo "mineru-v4-adapter already running, pid=$(pid_value "${MINERU_V4_ADAPTER_PID_FILE}")"
    return
  fi

  prepare_dirs

  local allow_flag="--no-allow-url-fetch"
  if [[ "${MINERU_V4_ADAPTER_ALLOW_URL_FETCH}" == "1" || "${MINERU_V4_ADAPTER_ALLOW_URL_FETCH}" == "true" ]]; then
    allow_flag="--allow-url-fetch"
  fi

  local formula_flag="--no-formula-enable"
  if [[ "${MINERU_V4_ADAPTER_FORMULA_ENABLE}" == "1" || "${MINERU_V4_ADAPTER_FORMULA_ENABLE}" == "true" || "${MINERU_V4_ADAPTER_FORMULA_ENABLE}" == "yes" || "${MINERU_V4_ADAPTER_FORMULA_ENABLE}" == "on" ]]; then
    formula_flag="--formula-enable"
  fi

  local table_flag="--no-table-enable"
  if [[ "${MINERU_V4_ADAPTER_TABLE_ENABLE}" == "1" || "${MINERU_V4_ADAPTER_TABLE_ENABLE}" == "true" || "${MINERU_V4_ADAPTER_TABLE_ENABLE}" == "yes" || "${MINERU_V4_ADAPTER_TABLE_ENABLE}" == "on" ]]; then
    table_flag="--table-enable"
  fi

  cd "${MINERU_REPO}"
  nohup python -m mineru.cli.v4_adapter \
    --host "${MINERU_V4_ADAPTER_HOST}" \
    --port "${MINERU_V4_ADAPTER_PORT}" \
    --upstream-url "${api_url}" \
    --token "${MINERU_V4_ADAPTER_TOKEN}" \
    --lang "${MINERU_V4_ADAPTER_LANG}" \
    "${formula_flag}" \
    "${table_flag}" \
    "${allow_flag}" \
    --task-retention-seconds "${MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS}" \
    >"${MINERU_LOG_DIR}/mineru-v4-adapter.log" 2>&1 &
  echo "$!" >"${MINERU_V4_ADAPTER_PID_FILE}"
  echo "started mineru-v4-adapter, pid=$(pid_value "${MINERU_V4_ADAPTER_PID_FILE}"), log=${MINERU_LOG_DIR}/mineru-v4-adapter.log"
  wait_http "${adapter_url}/health" "mineru-v4-adapter"
}

start_all() {
  start_api
  start_adapter
  cat <<INFO

Started MinerU for LLM Wiki.

LLM Wiki API_BASE:
  ${api_base}

LLM Wiki mineruConfig:
  "mineruConfig": {
    "enabled": true,
    "token": "${MINERU_V4_ADAPTER_TOKEN}",
    "modelVersion": "pipeline"
  }

Logs:
  ${MINERU_LOG_DIR}/mineru-api.log
  ${MINERU_LOG_DIR}/mineru-v4-adapter.log
INFO
}

stop_one() {
  local name="$1"
  local pid_file="$2"
  if ! is_running "${pid_file}"; then
    rm -f "${pid_file}"
    echo "${name} is not running"
    return
  fi

  local pid
  pid="$(cat "${pid_file}")"
  echo "stopping ${name}, pid=${pid}"
  kill "${pid}" 2>/dev/null || true

  local i
  for ((i = 1; i <= 30; i++)); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${pid_file}"
      echo "${name} stopped"
      return
    fi
    sleep 1
  done

  echo "${name} did not stop gracefully; sending SIGKILL"
  kill -9 "${pid}" 2>/dev/null || true
  rm -f "${pid_file}"
}

stop_all() {
  stop_one "mineru-v4-adapter" "${MINERU_V4_ADAPTER_PID_FILE}"
  stop_one "mineru-api" "${MINERU_API_PID_FILE}"
}

status_one() {
  local name="$1"
  local pid_file="$2"
  if is_running "${pid_file}"; then
    echo "${name}: running, pid=$(pid_value "${pid_file}")"
  else
    echo "${name}: stopped"
  fi
}

status_all() {
  status_one "mineru-api" "${MINERU_API_PID_FILE}"
  status_one "mineru-v4-adapter" "${MINERU_V4_ADAPTER_PID_FILE}"
  echo "mineru-api health: ${api_url}/health"
  echo "adapter health:    ${adapter_url}/health"
  echo "LLM Wiki API_BASE: ${api_base}"
}

logs_all() {
  echo "== ${MINERU_LOG_DIR}/mineru-api.log =="
  tail -n 80 "${MINERU_LOG_DIR}/mineru-api.log" 2>/dev/null || true
  echo
  echo "== ${MINERU_LOG_DIR}/mineru-v4-adapter.log =="
  tail -n 80 "${MINERU_LOG_DIR}/mineru-v4-adapter.log" 2>/dev/null || true
}

tail_logs() {
  touch "${MINERU_LOG_DIR}/mineru-api.log" "${MINERU_LOG_DIR}/mineru-v4-adapter.log"
  tail -f "${MINERU_LOG_DIR}/mineru-api.log" "${MINERU_LOG_DIR}/mineru-v4-adapter.log"
}

health_all() {
  echo "mineru-api:"
  curl -fsS "${api_url}/health"
  echo
  echo "mineru-v4-adapter:"
  curl -fsS "${adapter_url}/health"
  echo
}

cmd="${1:-}"
case "${cmd}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    start_all
    ;;
  status)
    status_all
    ;;
  logs)
    logs_all
    ;;
  tail)
    prepare_dirs
    tail_logs
    ;;
  health)
    health_all
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "ERROR: unknown command: ${cmd}" >&2
    usage
    exit 2
    ;;
esac
