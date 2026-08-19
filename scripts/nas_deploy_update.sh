#!/usr/bin/env bash
# Single-directory NAS update: git pull -> build -> validate -> recreate api.
# Run from the runtime project root (recommended: /vol1/docker/StockAnalyzer).
#
# Prerequisites:
# - This directory is a git clone of StockAnalyzer
# - .env exists here and is NOT committed
# - runtime data lives in named volumes (docker-compose.runtime.yml)
#   OR in ./artifacts (bind mount). Both are safe with git pull because
#   artifacts/ and .env are gitignored.
#
# Usage:
#   cd /vol1/docker/StockAnalyzer
#   bash scripts/nas_deploy_update.sh
#   bash scripts/nas_deploy_update.sh --branch main
#   bash scripts/nas_deploy_update.sh --no-recreate   # image build only
#   bash scripts/nas_deploy_update.sh --no-start-schedulers  # emergency API-only cutover
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="main"
DO_RECREATE=1
DO_PULL=1
START_SCHEDULERS=1
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-30}"
HEALTH_SLEEP_SEC="${HEALTH_SLEEP_SEC:-2}"
HOST_PYTHON="${HOST_PYTHON:-}"
DEPLOY_ID="$(date -u +%Y%m%d%H%M%S)-$$"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      BRANCH="${2:-main}"
      shift 2
      ;;
    --no-recreate)
      DO_RECREATE=0
      shift
      ;;
    --no-pull)
      DO_PULL=0
      shift
      ;;
    --start-scheduler|--start-schedulers)
      START_SCHEDULERS=1
      shift
      ;;
    --no-start-scheduler|--no-start-schedulers)
      START_SCHEDULERS=0
      shift
      ;;
    -h|--help)
      sed -n '1,20p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

cd "${ROOT}"

if ! [[ "${HEALTH_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: HEALTH_ATTEMPTS must be a positive integer." >&2
  exit 2
fi
if ! [[ "${HEALTH_SLEEP_SEC}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: HEALTH_SLEEP_SEC must be a positive integer." >&2
  exit 2
fi

if [[ ! -f .env ]]; then
  echo "ERROR: ${ROOT}/.env missing. Keep secrets only in runtime .env." >&2
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: ${ROOT} is not a git worktree." >&2
  echo "One-time migrate:" >&2
  echo "  cd /vol1/docker && mv StockAnalyzer StockAnalyzer_data_bak" >&2
  echo "  git clone <your-repo-url> StockAnalyzer" >&2
  echo "  cp StockAnalyzer_data_bak/.env StockAnalyzer/.env" >&2
  echo "  # artifacts are in named volume stock_analyzer_runtime_artifacts — no copy needed" >&2
  exit 1
fi

# Refuse dirty tracked files so pull/build is reproducible.
if [[ -n "$(git status --porcelain --untracked-files=no 2>/dev/null || true)" ]]; then
  echo "ERROR: tracked local modifications present. Commit/stash them or reset before deploy." >&2
  git status --short --untracked-files=no
  exit 1
fi

# Resolve the host Python interpreter before any pull/build/recreate step.
if [[ -n "${HOST_PYTHON}" ]]; then
  if ! command -v "${HOST_PYTHON}" >/dev/null 2>&1; then
    echo "ERROR: HOST_PYTHON=${HOST_PYTHON} not found on PATH." >&2
    exit 127
  fi
elif command -v python3 >/dev/null 2>&1; then
  HOST_PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  HOST_PYTHON="$(command -v python)"
else
  echo "ERROR: neither python3 nor python found on PATH; install Python or set HOST_PYTHON." >&2
  exit 127
fi
echo "using python interpreter: ${HOST_PYTHON}"

if [[ "${DO_PULL}" -eq 1 ]]; then
  echo "[1/6] git fetch/pull origin/${BRANCH}"
  git fetch origin "${BRANCH}"
  git checkout "${BRANCH}"
  git pull --ff-only origin "${BRANCH}"
else
  echo "[1/6] skip git pull"
fi

COMMIT="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
echo "${COMMIT}" > "${ROOT}/.build_commit"
export STOCK_ANALYZER_BUILD_COMMIT="${COMMIT}"
export STOCK_ANALYZER_BUILD_SHORT_COMMIT="${SHORT}"
export STOCK_ANALYZER_BUILD_DIRTY="0"
export STOCK_ANALYZER_BUILD_TIME_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "build_commit=${COMMIT}"

COMPOSE=(docker compose --env-file "${ROOT}/.env"
  -f docker-compose.yml
  -f docker-compose.runtime.yml
  -f docker-compose.advisory.yml
  -f docker-compose.vendor-overlay.yml
)
if [[ "${ENABLE_LEARNING:-0}" = "1" ]]; then
  echo "ERROR: ENABLE_LEARNING=1 is forbidden for advisory vendor-overlay deployment" >&2
  exit 1
fi

echo "[2/6] render and fail-closed validate advisory vendor-overlay config"
RENDERED="$(mktemp)"
trap 'rm -f "${RENDERED}"' EXIT
"${COMPOSE[@]}" config --format json > "${RENDERED}"
"${HOST_PYTHON}" - "${RENDERED}" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text())
for name, service in d.get("services", {}).items():
    if name not in {"api", "scheduler-critical", "scheduler-heavy"}:
        continue
    env = service.get("environment") or {}
    if isinstance(env, list): env = dict(item.split("=", 1) for item in env if "=" in item)
    expected = {"SA__APP__ADVISORY_ONLY":"true", "SA__TRAINING__ENABLED":"false", "SA__AUTO_PROMOTION__ENABLED":"false", "SA__DATA_SOURCE__PRIMARY":"vendor_zip_overlay", "SA__DATA_SOURCE__INTRADAY_RUNTIME_MODE":"duckdb_required", "SA__DATA_SOURCE__INTRADAY_ZIP_FALLBACK_ENABLED":"false"}
    for key, value in expected.items():
        if str(env.get(key, "")).lower() != value: raise SystemExit(f"fail-closed: {name} {key}={env.get(key)!r}, expected {value!r}")
    mounts = service.get("volumes") or []
    vendor_mounts = [item for item in mounts if isinstance(item, dict) and item.get("target") == "/data/vendor_history"]
    if len(vendor_mounts) != 1 or vendor_mounts[0].get("read_only") is not True:
        raise SystemExit(f"fail-closed: {name} vendor_history is not read-only")
    summary_mounts = [item for item in mounts if isinstance(item, dict) and item.get("target") == "/data/intraday_summary"]
    if len(summary_mounts) != 1 or summary_mounts[0].get("read_only") is not True:
        raise SystemExit(f"fail-closed: {name} intraday_summary is not read-only")
    expected_summary = "/data/intraday_summary/vendor_intraday_summary.duckdb"
    if env.get("SA__DATA_SOURCE__INTRADAY_SUMMARY_PATH") != expected_summary:
        raise SystemExit(f"fail-closed: {name} intraday summary path is invalid")
print("rendered config safety checks passed")
PY
PREVIOUS_IMAGE_ID="$(docker image inspect stock-analyzer:latest --format '{{.Id}}' 2>/dev/null || true)"
ROLLBACK_TAG=""
if [[ "${DO_RECREATE}" -eq 1 && -n "${PREVIOUS_IMAGE_ID}" ]]; then
  ROLLBACK_TAG="stock-analyzer:rollback-pre-$(date -u +%Y%m%d%H%M%S)"
  docker tag "${PREVIOUS_IMAGE_ID}" "${ROLLBACK_TAG}"
  printf '%s\n' "${ROLLBACK_TAG}" > "${ROOT}/.rollback_image"
fi

RUNTIME_CONTAINER_NAMES=(
  stock-analyzer-api
  stock-analyzer-scheduler
  stock-analyzer-scheduler-critical
  stock-analyzer-scheduler-heavy
)
RUNTIME_CONTAINER_BACKUPS=()
RUNTIME_CONTAINER_EXISTED=()
RUNTIME_CONTAINER_RUNNING=()
RUNTIME_CONTAINER_BACKED_UP=()
RUNTIME_BACKUP_STARTED=0
ROLLBACK_COMPLETED=0
SUMMARY_HOST_ROOT=""
SUMMARY_CURRENT=""
SUMMARY_CURRENT_MANIFEST=""
SUMMARY_CANDIDATE=""
SUMMARY_CANDIDATE_MANIFEST=""
SUMMARY_ROLLBACK=""
SUMMARY_ROLLBACK_MANIFEST=""
SUMMARY_OLD_DB_BACKED_UP=0
SUMMARY_OLD_MANIFEST_BACKED_UP=0
SUMMARY_NEW_DB_PROMOTED=0
SUMMARY_NEW_MANIFEST_PROMOTED=0

snapshot_runtime_containers() {
  local name
  local backup
  local running
  RUNTIME_CONTAINER_BACKUPS=()
  RUNTIME_CONTAINER_EXISTED=()
  RUNTIME_CONTAINER_RUNNING=()
  RUNTIME_CONTAINER_BACKED_UP=()
  for name in "${RUNTIME_CONTAINER_NAMES[@]}"; do
    backup="${name}.rollback-${DEPLOY_ID}"
    if docker inspect "${backup}" >/dev/null 2>&1; then
      echo "ERROR: rollback container already exists: ${backup}" >&2
      return 1
    fi
    RUNTIME_CONTAINER_BACKUPS+=("${backup}")
    RUNTIME_CONTAINER_BACKED_UP+=("0")
    if docker inspect "${name}" >/dev/null 2>&1; then
      if ! running="$(docker inspect --format '{{.State.Running}}' "${name}")"; then
        return 1
      fi
      RUNTIME_CONTAINER_EXISTED+=("1")
      RUNTIME_CONTAINER_RUNNING+=("${running}")
    else
      RUNTIME_CONTAINER_EXISTED+=("0")
      RUNTIME_CONTAINER_RUNNING+=("false")
    fi
  done
}

backup_runtime_containers() {
  local index
  local name
  local backup
  RUNTIME_BACKUP_STARTED=1
  for index in "${!RUNTIME_CONTAINER_NAMES[@]}"; do
    if [[ "${RUNTIME_CONTAINER_EXISTED[index]}" != "1" ]]; then
      continue
    fi
    name="${RUNTIME_CONTAINER_NAMES[index]}"
    backup="${RUNTIME_CONTAINER_BACKUPS[index]}"
    if [[ "${RUNTIME_CONTAINER_RUNNING[index]}" == "true" ]]; then
      if ! docker stop "${name}" >/dev/null; then
        return 1
      fi
    fi
    if ! docker rename "${name}" "${backup}"; then
      return 1
    fi
    RUNTIME_CONTAINER_BACKED_UP[index]="1"
  done
}

prepare_runtime_rollback() {
  local index
  local name
  local failed=0
  for index in "${!RUNTIME_CONTAINER_NAMES[@]}"; do
    name="${RUNTIME_CONTAINER_NAMES[index]}"
    if [[ "${RUNTIME_CONTAINER_BACKED_UP[index]:-0}" == "1" \
      || "${RUNTIME_CONTAINER_EXISTED[index]:-0}" == "0" ]]; then
      if docker inspect "${name}" >/dev/null 2>&1 \
        && ! docker rm -f "${name}" >/dev/null; then
        echo "rollback: failed to stop replacement container ${name}" >&2
        failed=1
      fi
      continue
    fi
    if docker inspect "${name}" >/dev/null 2>&1; then
      if ! docker stop "${name}" >/dev/null 2>&1; then
        echo "rollback: failed to quiesce unrenamed container ${name}" >&2
        failed=1
      fi
    else
      echo "rollback: original container disappeared before backup: ${name}" >&2
      failed=1
    fi
  done
  return "${failed}"
}

restore_runtime_containers() {
  local index
  local name
  local backup
  local failed=0
  for index in "${!RUNTIME_CONTAINER_NAMES[@]}"; do
    name="${RUNTIME_CONTAINER_NAMES[index]}"
    backup="${RUNTIME_CONTAINER_BACKUPS[index]:-}"
    if [[ "${RUNTIME_CONTAINER_BACKED_UP[index]:-0}" == "1" ]]; then
      if ! docker inspect "${backup}" >/dev/null 2>&1; then
        echo "rollback: backup container missing: ${backup}" >&2
        failed=1
        continue
      fi
      if ! docker rename "${backup}" "${name}" >/dev/null; then
        echo "rollback: failed to restore container name ${name}" >&2
        failed=1
        continue
      fi
    elif [[ "${RUNTIME_CONTAINER_EXISTED[index]:-0}" == "1" ]] \
      && ! docker inspect "${name}" >/dev/null 2>&1; then
      echo "rollback: unrenamed original container is missing: ${name}" >&2
      failed=1
      continue
    fi

    if [[ "${RUNTIME_CONTAINER_EXISTED[index]:-0}" == "1" \
      && "${RUNTIME_CONTAINER_RUNNING[index]:-false}" == "true" ]] \
      && ! docker start "${name}" >/dev/null; then
      echo "rollback: failed to restart container ${name}" >&2
      failed=1
    fi
  done
  return "${failed}"
}

cleanup_runtime_backups() {
  local backup
  for backup in "${RUNTIME_CONTAINER_BACKUPS[@]}"; do
    docker rm -f "${backup}" >/dev/null 2>&1 || true
  done
  RUNTIME_BACKUP_STARTED=0
}

cleanup_summary_candidates() {
  if [[ -z "${SUMMARY_CANDIDATE}" ]]; then
    return 0
  fi
  rm -f \
    "${SUMMARY_CANDIDATE}" \
    "${SUMMARY_CANDIDATE_MANIFEST}" \
    "${SUMMARY_CANDIDATE}.next" \
    "${SUMMARY_CANDIDATE}.next.manifest.json" \
    "${SUMMARY_CANDIDATE}.previous" \
    "${SUMMARY_CANDIDATE}.previous.manifest.json"
}

promote_candidate_summary() {
  if [[ ! -s "${SUMMARY_CANDIDATE}" || ! -s "${SUMMARY_CANDIDATE_MANIFEST}" ]]; then
    echo "ERROR: candidate intraday summary or manifest is missing." >&2
    return 1
  fi
  rm -f "${SUMMARY_ROLLBACK}" "${SUMMARY_ROLLBACK_MANIFEST}"
  if [[ -e "${SUMMARY_CURRENT}" ]]; then
    if ! mv "${SUMMARY_CURRENT}" "${SUMMARY_ROLLBACK}"; then
      return 1
    fi
    SUMMARY_OLD_DB_BACKED_UP=1
  fi
  if [[ -e "${SUMMARY_CURRENT_MANIFEST}" ]]; then
    if ! mv "${SUMMARY_CURRENT_MANIFEST}" "${SUMMARY_ROLLBACK_MANIFEST}"; then
      return 1
    fi
    SUMMARY_OLD_MANIFEST_BACKED_UP=1
  fi
  if ! mv "${SUMMARY_CANDIDATE}" "${SUMMARY_CURRENT}"; then
    return 1
  fi
  SUMMARY_NEW_DB_PROMOTED=1
  if ! mv "${SUMMARY_CANDIDATE_MANIFEST}" "${SUMMARY_CURRENT_MANIFEST}"; then
    return 1
  fi
  SUMMARY_NEW_MANIFEST_PROMOTED=1
  if [[ ! -s "${SUMMARY_CURRENT}" || ! -s "${SUMMARY_CURRENT_MANIFEST}" ]]; then
    echo "ERROR: promoted intraday summary validation failed." >&2
    return 1
  fi
}

restore_previous_summary() {
  local failed=0
  if [[ -z "${SUMMARY_CURRENT}" ]]; then
    return 0
  fi
  if [[ "${SUMMARY_NEW_DB_PROMOTED}" -eq 1 \
    || ( "${SUMMARY_OLD_DB_BACKED_UP}" -eq 1 && -e "${SUMMARY_CURRENT}" ) ]]; then
    if ! rm -f "${SUMMARY_CURRENT}"; then
      echo "rollback: failed to remove promoted intraday summary" >&2
      failed=1
    fi
  fi
  if [[ "${SUMMARY_NEW_MANIFEST_PROMOTED}" -eq 1 \
    || ( "${SUMMARY_OLD_MANIFEST_BACKED_UP}" -eq 1 \
      && -e "${SUMMARY_CURRENT_MANIFEST}" ) ]]; then
    if ! rm -f "${SUMMARY_CURRENT_MANIFEST}"; then
      echo "rollback: failed to remove promoted intraday manifest" >&2
      failed=1
    fi
  fi
  if [[ "${SUMMARY_OLD_DB_BACKED_UP}" -eq 1 ]]; then
    if [[ ! -e "${SUMMARY_ROLLBACK}" ]] \
      || ! mv "${SUMMARY_ROLLBACK}" "${SUMMARY_CURRENT}"; then
      echo "rollback: failed to restore previous intraday summary" >&2
      failed=1
    fi
  fi
  if [[ "${SUMMARY_OLD_MANIFEST_BACKED_UP}" -eq 1 ]]; then
    if [[ ! -e "${SUMMARY_ROLLBACK_MANIFEST}" ]] \
      || ! mv "${SUMMARY_ROLLBACK_MANIFEST}" "${SUMMARY_CURRENT_MANIFEST}"; then
      echo "rollback: failed to restore previous intraday manifest" >&2
      failed=1
    fi
  fi
  if ! cleanup_summary_candidates; then
    echo "rollback: failed to clean candidate intraday summary files" >&2
    failed=1
  fi
  if [[ "${failed}" -eq 0 ]]; then
    echo "rollback: restored the previous intraday summary state" >&2
  fi
  return "${failed}"
}

cleanup_summary_rollback() {
  rm -f "${SUMMARY_ROLLBACK}" "${SUMMARY_ROLLBACK_MANIFEST}"
  cleanup_summary_candidates
}

rollback_runtime() {
  local failed=0
  if [[ "${ROLLBACK_COMPLETED}" -eq 1 ]]; then
    return 0
  fi
  echo "rollback: restoring the previous runtime" >&2
  set +e
  if [[ "${RUNTIME_BACKUP_STARTED}" -eq 1 ]] && ! prepare_runtime_rollback; then
    failed=1
  fi
  if ! restore_previous_summary; then
    failed=1
  fi
  if [[ "${RUNTIME_BACKUP_STARTED}" -eq 1 ]] && ! restore_runtime_containers; then
    failed=1
  fi
  if [[ -n "${PREVIOUS_IMAGE_ID}" ]] \
    && ! docker tag "${PREVIOUS_IMAGE_ID}" stock-analyzer:latest; then
    echo "rollback: failed to restore stock-analyzer:latest image tag" >&2
    failed=1
  fi
  RUNTIME_BACKUP_STARTED=0
  ROLLBACK_COMPLETED=1
  set -e
  if [[ "${failed}" -ne 0 ]]; then
    echo "FATAL: automatic rollback was incomplete; manual recovery is required." >&2
    return 1
  fi
  return 0
}

on_exit() {
  local status=$?
  trap - EXIT
  set +e
  if [[ "${status}" -ne 0 && "${ROLLBACK_COMPLETED}" -eq 0 ]]; then
    rollback_runtime
  fi
  rm -f "${RENDERED}"
  exit "${status}"
}

trap on_exit EXIT

echo "[3/6] build api image with commit metadata while the current runtime remains available"
"${COMPOSE[@]}" build --build-arg STOCK_ANALYZER_BUILD_COMMIT="${COMMIT}" --build-arg STOCK_ANALYZER_BUILD_SHORT_COMMIT="${SHORT}" --build-arg STOCK_ANALYZER_BUILD_DIRTY=0 api

LABEL_COMMIT="$(docker image inspect stock-analyzer:latest --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ "${LABEL_COMMIT}" != "${COMMIT}" ]]; then
  echo "ERROR: image label commit ${LABEL_COMMIT} != source ${COMMIT}" >&2
  exit 1
fi
if [[ "${DO_RECREATE}" -eq 0 ]]; then
  echo "[4/6] skip intraday summary replacement"
  echo "[5/6] skip runtime stop"
  echo "[6/6] image build complete; runtime was not recreated"
  exit 0
fi

echo "[4/6] build candidate intraday summary DuckDB while the current runtime remains available"
mapfile -t SUMMARY_MOUNTS < <("${HOST_PYTHON}" - "${RENDERED}" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
volumes = (payload.get("services", {}).get("api", {}).get("volumes") or [])
by_target = {
    item.get("target"): item.get("source")
    for item in volumes
    if isinstance(item, dict) and item.get("target") and item.get("source")
}
for target in ("/data/vendor_history", "/data/intraday_summary", "/app/artifacts"):
    source = by_target.get(target)
    if not source:
        raise SystemExit(f"missing rendered mount source for {target}")
    print(source)
PY
)
if [[ "${#SUMMARY_MOUNTS[@]}" -ne 3 ]]; then
  echo "ERROR: failed to resolve vendor, intraday summary, and artifacts mounts." >&2
  if [[ -n "${PREVIOUS_IMAGE_ID}" ]]; then
    docker tag "${PREVIOUS_IMAGE_ID}" stock-analyzer:latest
  fi
  exit 1
fi
VENDOR_HOST_ROOT="${SUMMARY_MOUNTS[0]}"
SUMMARY_HOST_ROOT="${SUMMARY_MOUNTS[1]}"
ARTIFACTS_MOUNT_SOURCE="${SUMMARY_MOUNTS[2]}"
mkdir -p "${SUMMARY_HOST_ROOT}"
SUMMARY_CURRENT="${SUMMARY_HOST_ROOT}/vendor_intraday_summary.duckdb"
SUMMARY_CURRENT_MANIFEST="${SUMMARY_CURRENT}.manifest.json"
SUMMARY_CANDIDATE="${SUMMARY_CURRENT}.candidate-${DEPLOY_ID}"
SUMMARY_CANDIDATE_MANIFEST="${SUMMARY_CANDIDATE}.manifest.json"
SUMMARY_ROLLBACK="${SUMMARY_CURRENT}.rollback-${DEPLOY_ID}"
SUMMARY_ROLLBACK_MANIFEST="${SUMMARY_ROLLBACK}.manifest.json"
cleanup_summary_candidates
INTRADAY_REQUIRED_LATEST_DATE="${INTRADAY_SUMMARY_REQUIRED_LATEST_DATE:-}"
if [[ -z "${INTRADAY_REQUIRED_LATEST_DATE}" ]]; then
  INTRADAY_REQUIRED_LATEST_DATE="$(docker run --rm \
    -v "${ARTIFACTS_MOUNT_SOURCE}:/app/artifacts:ro" \
    stock-analyzer:latest \
    python -c 'import json; from pathlib import Path; p=Path("/app/artifacts/vendor_overlay/daily_index.json"); d=json.loads(p.read_text(encoding="utf-8")); dates=[str(v.get("latest_date", "")) for v in (d.get("symbols") or {}).values() if isinstance(v, dict) and str(v.get("latest_date", ""))]; print(max(dates, default=""))')"
fi
if [[ -z "${INTRADAY_REQUIRED_LATEST_DATE}" ]]; then
  echo "ERROR: cannot resolve intraday summary freshness floor from daily_index.json." >&2
  exit 1
fi
echo "intraday summary required latest date: ${INTRADAY_REQUIRED_LATEST_DATE}"
if ! docker run --rm \
  -v "${VENDOR_HOST_ROOT}:/data/vendor_history:ro" \
  -v "${SUMMARY_HOST_ROOT}:/data/intraday_summary" \
  stock-analyzer:latest \
  python /app/scripts/build_vendor_intraday_summary.py \
    --root /data/vendor_history \
    --output "/data/intraday_summary/$(basename "${SUMMARY_CANDIDATE}")" \
    --keep-days "${INTRADAY_SUMMARY_KEEP_DAYS:-480}" \
    --require-latest-date "${INTRADAY_REQUIRED_LATEST_DATE}"; then
  cleanup_summary_candidates
  if [[ -n "${PREVIOUS_IMAGE_ID}" ]]; then
    docker tag "${PREVIOUS_IMAGE_ID}" stock-analyzer:latest
  fi
  exit 1
fi
if [[ ! -s "${SUMMARY_CANDIDATE}" || ! -s "${SUMMARY_CANDIDATE_MANIFEST}" ]]; then
  echo "ERROR: candidate intraday summary or manifest is missing." >&2
  cleanup_summary_candidates
  if [[ -n "${PREVIOUS_IMAGE_ID}" ]]; then
    docker tag "${PREVIOUS_IMAGE_ID}" stock-analyzer:latest
  fi
  exit 1
fi

echo "[5/6] preserve old containers and atomically promote the candidate summary"
if ! snapshot_runtime_containers || ! backup_runtime_containers; then
  rollback_runtime
  exit 1
fi
if ! promote_candidate_summary; then
  rollback_runtime
  exit 1
fi

echo "[6/6] recreate api and split schedulers"
HEALTH_GATE_STARTED_EPOCH="$(date +%s)"
if ! "${COMPOSE[@]}" up -d --no-build --force-recreate api; then
  rollback_runtime
  exit 1
fi
if [[ "${START_SCHEDULERS}" -eq 1 ]]; then
  if ! "${COMPOSE[@]}" up -d --no-build --force-recreate scheduler-critical scheduler-heavy; then
    rollback_runtime
    exit 1
  fi
else
  echo "schedulers: explicitly disabled by --no-start-schedulers"
fi

echo "[6/6] health and image identity"
"${COMPOSE[@]}" ps
PORT="$(grep -E '^SA_API_HOST_PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
PORT="${PORT:-18001}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

validate_scheduler_heartbeat() {
  local group="$1"
  local container="stock-analyzer-scheduler-${group}"
  local path="/app/artifacts/runtime/scheduler_${group}_heartbeat.json"
  local running
  local heartbeat_mtime
  local heartbeat
  running="$(docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null || true)"
  if [[ "${running}" != "true" ]]; then
    return 1
  fi
  heartbeat_mtime="$(docker exec "${container}" stat -c %Y "${path}" 2>/dev/null || true)"
  if ! [[ "${heartbeat_mtime}" =~ ^[0-9]+$ ]] || [[ "${heartbeat_mtime}" -lt "${HEALTH_GATE_STARTED_EPOCH}" ]]; then
    return 1
  fi
  heartbeat="$(docker exec "${container}" cat "${path}" 2>/dev/null || true)"
  if [[ -z "${heartbeat}" ]]; then
    return 1
  fi
  "${HOST_PYTHON}" - "${heartbeat}" "${COMMIT}" "${group}" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
expected_commit = sys.argv[2]
expected_group = sys.argv[3]
if payload.get("status") != "ok" or payload.get("group") != expected_group:
    raise SystemExit(f"scheduler heartbeat mismatch: {payload}")
build = payload.get("build") or {}
if (
    build.get("commit") != expected_commit
    or build.get("short_commit") != expected_commit[:7]
    or build.get("trusted") is not True
):
    raise SystemExit(f"scheduler build mismatch: {build}")
if not str(payload.get("timestamp", "")).strip():
    raise SystemExit("scheduler heartbeat timestamp missing")
PY
}

attempt=1
while [[ "${attempt}" -le "${HEALTH_ATTEMPTS}" ]]; do
  HEALTH_ERROR="$(mktemp)"
  if HEALTH="$(curl -fsS --connect-timeout 3 --max-time 10 "${HEALTH_URL}" 2>"${HEALTH_ERROR}")"; then
    rm -f "${HEALTH_ERROR}"
    if ! "${HOST_PYTHON}" - "${HEALTH}" "${COMMIT}" <<'PY'
import json, sys
h = json.loads(sys.argv[1])
expected = sys.argv[2]
if h.get("status") != "ok":
    raise SystemExit(f"health status mismatch: {h.get('status')!r}")
build = h.get("build") or {}
if (
    build.get("commit") != expected
    or build.get("short_commit") != expected[:7]
    or build.get("trusted") is not True
):
    raise SystemExit(f"health commit mismatch: {build}")
runtime = h.get("runtime") or {}
if runtime.get("advisory_only") is not True or runtime.get("training_enabled") is not False:
    raise SystemExit(f"health safety mismatch: {runtime}")
print(json.dumps({"build_commit": build.get("commit"), "advisory_only": runtime.get("advisory_only"), "training_enabled": runtime.get("training_enabled")}, separators=(",", ":")))
PY
    then
      echo "ERROR: health endpoint responded but identity or safety validation failed." >&2
      rollback_runtime
      exit 1
    fi
    SCHEDULERS_READY=1
    if [[ "${START_SCHEDULERS}" -eq 1 ]]; then
      if ! validate_scheduler_heartbeat critical || ! validate_scheduler_heartbeat heavy; then
        SCHEDULERS_READY=0
      fi
    fi
    if [[ "${SCHEDULERS_READY}" -eq 1 ]]; then
      echo
      echo "OK. commit=${SHORT}"
      echo "If capital_curve:freeze still active, run: bash scripts/nas_reset_sim_account.sh"
      ROLLBACK_COMPLETED=1
      cleanup_runtime_backups || echo "WARNING: stale rollback containers require manual cleanup" >&2
      cleanup_summary_rollback || echo "WARNING: stale summary rollback files require manual cleanup" >&2
      exit 0
    fi
    echo "health not ready (${attempt}/${HEALTH_ATTEMPTS}): waiting for fresh critical/heavy scheduler heartbeats" >&2
  else
    CURL_CODE=$?
    HEALTH_MESSAGE="$(tr '\n' ' ' < "${HEALTH_ERROR}")"
    rm -f "${HEALTH_ERROR}"
    echo "health not ready (${attempt}/${HEALTH_ATTEMPTS}, curl=${CURL_CODE}): ${HEALTH_MESSAGE}" >&2
  fi

  if [[ "${attempt}" -lt "${HEALTH_ATTEMPTS}" ]]; then
    sleep "${HEALTH_SLEEP_SEC}"
  fi
  attempt=$((attempt + 1))
done

echo "ERROR: health did not become ready after ${HEALTH_ATTEMPTS} attempts: ${HEALTH_URL}" >&2
docker logs stock-analyzer-api --tail 100 >&2 || true
if [[ "${START_SCHEDULERS}" -eq 1 ]]; then
  docker logs stock-analyzer-scheduler-critical --tail 100 >&2 || true
  docker logs stock-analyzer-scheduler-heavy --tail 100 >&2 || true
fi
rollback_runtime
exit 1
