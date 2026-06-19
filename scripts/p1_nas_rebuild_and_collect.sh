#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[p1-nas] %s\n' "$*"
}

branch="${BRANCH:-codex/p1-shadow-calibration-data-quality}"
api_base="${API_BASE:-http://127.0.0.1:18001}"
output_dir="${OUTPUT_DIR:-artifacts/research/p1_advisory_collection_quick_rerun}"
symbols="${SYMBOLS:-600000,000001}"
runs="${RUNS:-2}"
interval_sec="${INTERVAL_SEC:-60}"
health_attempts="${HEALTH_ATTEMPTS:-30}"
health_sleep_sec="${HEALTH_SLEEP_SEC:-2}"

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f docker-compose.yml -f docker-compose.advisory.yml "$@"
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f docker-compose.yml -f docker-compose.advisory.yml "$@"
    return
  fi
  log "docker compose is not available."
  exit 1
}

check_repo() {
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "current directory is not a Git worktree; run from /vol1/docker/StockAnalyzer_repo."
    exit 1
  fi
}

checkout_branch() {
  log "fetching latest origin refs"
  git fetch origin
  log "checking out origin/${branch}"
  git checkout -B "${branch}" "origin/${branch}"
  log "current tip:"
  git log --oneline -5
}

rebuild_services() {
  log "rebuilding api and scheduler with docker-compose.advisory.yml"
  compose up -d --build api scheduler
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
  log "capturing NAS environment evidence"
  python scripts/p1_capture_nas_environment.py \
    --api-base "$api_base" \
    --output-dir "$output_dir" \
    --expected-branch "$branch"
  log "running advisory-only collection"
  python scripts/p1_run_nas_advisory_collection.py \
    --api-base "$api_base" \
    --output-dir "$output_dir" \
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
rebuild_services
wait_for_safe_health
run_collection
