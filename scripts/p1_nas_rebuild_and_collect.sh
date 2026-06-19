#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[p1-nas] %s\n' "$*"
}

branch="${BRANCH:-codex/p1-shadow-calibration-data-quality}"
required_head="${REQUIRED_HEAD:-6e5bd3b}"
repo_dir="${REPO_DIR:-/vol1/docker/StockAnalyzer_repo}"
runtime_dir="${RUNTIME_DIR:-/vol1/docker/StockAnalyzer}"
runtime_artifacts_dir="${RUNTIME_ARTIFACTS_DIR:-/vol1/docker/volumes/stock_analyzer_runtime_artifacts/_data}"
api_base="${API_BASE:-http://127.0.0.1:18001}"
output_dir="${OUTPUT_DIR:-${runtime_artifacts_dir}/research/p1_advisory_collection_quick_rerun}"
runtime_state="${RUNTIME_STATE:-${runtime_artifacts_dir}/runtime/runtime_state.json}"
model_artifact="${MODEL_ARTIFACT:-${runtime_artifacts_dir}/model_v1.json}"
symbols="${SYMBOLS:-600000,000001}"
runs="${RUNS:-2}"
interval_sec="${INTERVAL_SEC:-60}"
health_attempts="${HEALTH_ATTEMPTS:-30}"
health_sleep_sec="${HEALTH_SLEEP_SEC:-2}"
compose_files=(
  -f docker-compose.yml
  -f docker-compose.runtime.yml
  -f docker-compose.runtime.localvol.yml
  -f docker-compose.advisory.yml
)

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose --env-file "${runtime_dir}/.env" "${compose_files[@]}" "$@"
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose --env-file "${runtime_dir}/.env" "${compose_files[@]}" "$@"
    return
  fi
  log "docker compose is not available."
  exit 1
}

check_repo() {
  cd "$repo_dir"
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "${repo_dir} is not a Git worktree."
    exit 1
  fi
}

checkout_branch() {
  cd "$repo_dir"
  if [ "${SKIP_GIT_FETCH:-0}" = "1" ]; then
    log "SKIP_GIT_FETCH=1; using local ${branch} without contacting origin"
    git checkout "${branch}"
  else
    log "fetching latest origin refs"
    git fetch origin
    log "checking out origin/${branch}"
    git checkout -B "${branch}" "origin/${branch}"
  fi
  current_head="$(git rev-parse HEAD)"
  if ! git merge-base --is-ancestor "$required_head" "$current_head"; then
    log "required commit ${required_head} is not an ancestor of current HEAD ${current_head}"
    exit 1
  fi
  git rev-parse HEAD > .build_commit
  log "current tip:"
  git log --oneline -5
}

sync_runtime_dir() {
  log "syncing repo to runtime dir: ${runtime_dir}"
  rsync -av --delete \
    --exclude '.git' \
    --exclude '.env' \
    --exclude 'artifacts/' \
    --exclude 'suggestions/' \
    --exclude 'tdx_empty/' \
    --exclude '.venv/' \
    --exclude '.vscode/' \
    --exclude 'tests/' \
    "${repo_dir}/" "${runtime_dir}/"
}

rebuild_services() {
  cd "$runtime_dir"
  export STOCK_ANALYZER_BUILD_COMMIT
  STOCK_ANALYZER_BUILD_COMMIT="$(cat "${runtime_dir}/.build_commit")"
  log "building api image from runtime dir with advisory compose override"
  compose build api
  log "recreating api and scheduler with existing rebuilt image"
  compose up -d --no-build --force-recreate api scheduler
}

wait_for_safe_health() {
  attempt=1
  while [ "$attempt" -le "$health_attempts" ]; do
    set +e
    python - "$api_base" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
try:
    with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
        health = json.load(response)
except Exception as exc:  # pragma: no cover - executed on NAS
    print(f"health_request_failed: {exc}")
    sys.exit(2)

runtime = health.get("runtime") or {}
print(json.dumps({"mode": health.get("mode"), "runtime": runtime}, ensure_ascii=False))
if runtime.get("advisory_only") is not True:
    sys.exit(3)
if runtime.get("training_enabled") is not False:
    sys.exit(4)
PY
    code=$?
    set -e

    if [ "$code" -eq 0 ]; then
      log "health gate passed: advisory_only=true and training_enabled=false"
      return 0
    fi
    if [ "$code" -eq 3 ] || [ "$code" -eq 4 ]; then
      log "unsafe runtime detected after rebuild; collection will not start"
      exit "$code"
    fi
    log "health not ready yet, retry ${attempt}/${health_attempts}"
    attempt=$((attempt + 1))
    sleep "$health_sleep_sec"
  done

  log "health gate did not pass before timeout; collection will not start"
  exit 1
}

run_collection() {
  cd "$runtime_dir"
  log "capturing NAS environment evidence"
  python scripts/p1_capture_nas_environment.py \
    --api-base "$api_base" \
    --output-dir "$output_dir" \
    --expected-branch "$branch" \
    --repo-dir "$repo_dir" \
    --runtime-dir "$runtime_dir"
  log "running advisory-only collection"
  python scripts/p1_run_nas_advisory_collection.py \
    --api-base "$api_base" \
    --output-dir "$output_dir" \
    --runtime-state "$runtime_state" \
    --config "${runtime_dir}/config/default.yaml" \
    --model-artifact "$model_artifact" \
    --symbols "$symbols" \
    --runs "$runs" \
    --interval-sec "$interval_sec" \
    --confirm-run
  log "building collection acceptance report"
  python scripts/p1_accept_nas_advisory_collection.py \
    --collection-dir "$output_dir" \
    --min-completed-runs "$runs"
  log "building goal completion audit"
  python scripts/p1_audit_goal_completion.py \
    --collection-dir "$output_dir" \
    --min-completed-runs "$runs"
  log "collection report:"
  log "${output_dir}/p1_nas_environment.json"
  log "${output_dir}/p1_advisory_collection_report.md"
  log "${output_dir}/p1_advisory_collection_report.json"
  log "${output_dir}/p1_advisory_collection_acceptance.md"
  log "${output_dir}/p1_advisory_collection_acceptance.json"
  log "${output_dir}/p1_goal_completion_audit.md"
  log "${output_dir}/p1_goal_completion_audit.json"
}

check_repo
checkout_branch
sync_runtime_dir
rebuild_services
wait_for_safe_health
run_collection
