#!/usr/bin/env bash
# Single-directory NAS update: git pull -> build -> validate -> recreate api.
# Run from the runtime project root (recommended: /vol1/docker/StockAnalyzer).
#
# Prerequisites:
# - This directory is a git clone of StockAnalyzer
# - .env exists here and is NOT committed
# - runtime data lives in named volumes (docker-compose.runtime.localvol.yml)
#   OR in ./artifacts (bind mount). Both are safe with git pull because
#   artifacts/ and .env are gitignored.
#
# Usage:
#   cd /vol1/docker/StockAnalyzer
#   bash scripts/nas_deploy_update.sh
#   bash scripts/nas_deploy_update.sh --branch main
#   bash scripts/nas_deploy_update.sh --no-recreate   # build only
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="main"
DO_RECREATE=1
DO_PULL=1
START_SCHEDULER=0
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-30}"
HEALTH_SLEEP_SEC="${HEALTH_SLEEP_SEC:-2}"

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
    --start-scheduler)
      START_SCHEDULER=1
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

if [[ "${DO_PULL}" -eq 1 ]]; then
  echo "[1/4] git fetch/pull origin/${BRANCH}"
  git fetch origin "${BRANCH}"
  git checkout "${BRANCH}"
  git pull --ff-only origin "${BRANCH}"
else
  echo "[1/4] skip git pull"
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
  -f docker-compose.runtime.localvol.yml
  -f docker-compose.advisory.yml
  -f docker-compose.vendor-overlay.yml
)
if [[ "${ENABLE_LEARNING:-0}" = "1" ]]; then
  echo "ERROR: ENABLE_LEARNING=1 is forbidden for advisory vendor-overlay deployment" >&2
  exit 1
fi

echo "[2/4] render and fail-closed validate advisory vendor-overlay config"
RENDERED="$(mktemp)"
trap 'rm -f "${RENDERED}"' EXIT
"${COMPOSE[@]}" config --format json > "${RENDERED}"
python - "${RENDERED}" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text())
for name, service in d.get("services", {}).items():
    if name not in {"api", "scheduler"}:
        continue
    env = service.get("environment") or {}
    if isinstance(env, list): env = dict(item.split("=", 1) for item in env if "=" in item)
    expected = {"SA__APP__ADVISORY_ONLY":"true", "SA__TRAINING__ENABLED":"false", "SA__AUTO_PROMOTION__ENABLED":"false", "SA__DATA_SOURCE__PRIMARY":"vendor_zip_overlay"}
    for key, value in expected.items():
        if str(env.get(key, "")).lower() != value: raise SystemExit(f"fail-closed: {name} {key}={env.get(key)!r}, expected {value!r}")
    mounts = service.get("volumes") or []
    vendor_mounts = [item for item in mounts if isinstance(item, dict) and item.get("target") == "/data/vendor_history"]
    if len(vendor_mounts) != 1 or vendor_mounts[0].get("read_only") is not True:
        raise SystemExit(f"fail-closed: {name} vendor_history is not read-only")
print("rendered config safety checks passed")
PY
echo "[3/4] build api image with commit metadata"
"${COMPOSE[@]}" build --build-arg STOCK_ANALYZER_BUILD_COMMIT="${COMMIT}" --build-arg STOCK_ANALYZER_BUILD_SHORT_COMMIT="${SHORT}" --build-arg STOCK_ANALYZER_BUILD_DIRTY=0 api

if [[ "${DO_RECREATE}" -eq 1 ]]; then
  echo "[4/4] recreate api only; scheduler requires explicit --start-scheduler"
  "${COMPOSE[@]}" up -d --no-build --force-recreate api
  if [[ "${START_SCHEDULER}" -eq 1 ]]; then
    "${COMPOSE[@]}" up -d --no-build --force-recreate scheduler
  else
    echo "scheduler: not started (vendor compatibility gate required)"
  fi
else
  echo "[4/4] skip recreate"
fi

echo "[4/4] health and image identity"
"${COMPOSE[@]}" ps
LABEL_COMMIT="$(docker image inspect stock-analyzer:latest --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ "${LABEL_COMMIT}" != "${COMMIT}" ]]; then
  echo "ERROR: image label commit ${LABEL_COMMIT} != source ${COMMIT}" >&2
  exit 1
fi
PORT="$(grep -E '^SA_API_HOST_PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
PORT="${PORT:-18001}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
attempt=1
while [[ "${attempt}" -le "${HEALTH_ATTEMPTS}" ]]; do
  HEALTH_ERROR="$(mktemp)"
  if HEALTH="$(curl -fsS --connect-timeout 3 --max-time 10 "${HEALTH_URL}" 2>"${HEALTH_ERROR}")"; then
    rm -f "${HEALTH_ERROR}"
    if python - "${HEALTH}" "${COMMIT}" <<'PY'
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
      echo
      echo "OK. commit=${SHORT}"
      echo "If capital_curve:freeze still active, run: bash scripts/nas_reset_sim_account.sh"
      exit 0
    fi
    echo "ERROR: health endpoint responded but identity or safety validation failed." >&2
    exit 1
  else
    CURL_CODE=$?
  fi

  HEALTH_MESSAGE="$(tr '\n' ' ' < "${HEALTH_ERROR}")"
  rm -f "${HEALTH_ERROR}"
  echo "health not ready (${attempt}/${HEALTH_ATTEMPTS}, curl=${CURL_CODE}): ${HEALTH_MESSAGE}" >&2
  if [[ "${attempt}" -lt "${HEALTH_ATTEMPTS}" ]]; then
    sleep "${HEALTH_SLEEP_SEC}"
  fi
  attempt=$((attempt + 1))
done

echo "ERROR: health did not become ready after ${HEALTH_ATTEMPTS} attempts: ${HEALTH_URL}" >&2
docker logs stock-analyzer-api --tail 100 >&2 || true
exit 1
