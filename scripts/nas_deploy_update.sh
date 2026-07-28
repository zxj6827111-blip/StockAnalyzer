#!/usr/bin/env bash
# Single-directory NAS update: git pull -> build -> recreate api/scheduler.
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
)
# Optional nightly train-after-sync overlay
if [[ -f "${ROOT}/docker-compose.learning.yml" ]] && [[ "${ENABLE_LEARNING:-1}" = "1" ]]; then
  COMPOSE+=(-f docker-compose.learning.yml)
  echo "learning overlay: enabled (ENABLE_LEARNING=1)"
fi

echo "[2/4] build api image"
"${COMPOSE[@]}" build api

if [[ "${DO_RECREATE}" -eq 1 ]]; then
  echo "[3/4] recreate api scheduler"
  "${COMPOSE[@]}" up -d --no-build --force-recreate api scheduler
else
  echo "[3/4] skip recreate"
fi

echo "[4/4] health"
"${COMPOSE[@]}" ps
PORT="$(grep -E '^SA_API_HOST_PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
PORT="${PORT:-18001}"
sleep 3
curl -fsS "http://127.0.0.1:${PORT}/health" || true
echo
echo "OK. commit=${SHORT}"
echo "If capital_curve:freeze still active, run: bash scripts/nas_reset_sim_account.sh"
