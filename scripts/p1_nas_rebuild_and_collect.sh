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
release_stage="${RELEASE_STAGE:-stage-a-consistency}"
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
  export STOCK_ANALYZER_BUILD_SHORT_COMMIT
  STOCK_ANALYZER_BUILD_SHORT_COMMIT="$(printf '%s' "$STOCK_ANALYZER_BUILD_COMMIT" | cut -c1-12)"
  export STOCK_ANALYZER_BUILD_DIRTY=false
  export STOCK_ANALYZER_BUILD_TIME_UTC
  STOCK_ANALYZER_BUILD_TIME_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  previous_image="$(docker image inspect stock-analyzer:latest --format '{{.Id}}' 2>/dev/null || true)"
  if [ -n "$previous_image" ]; then
    rollback_tag="stock-analyzer:rollback-pre-$(date -u +%Y%m%d%H%M%S)"
    docker tag "$previous_image" "$rollback_tag"
    printf '%s\n' "$rollback_tag" > "${runtime_dir}/.rollback_image"
    log "previous image preserved as ${rollback_tag}"
  fi
  log "building api image from runtime dir with advisory compose override"
  compose build api
  stage_tag="stock-analyzer:${release_stage}-${STOCK_ANALYZER_BUILD_SHORT_COMMIT}"
  docker tag stock-analyzer:latest "$stage_tag"
  printf '%s\n' "$stage_tag" > "${runtime_dir}/.release_image"
  log "recreating api and scheduler with existing rebuilt image"
  compose up -d --no-build --force-recreate api scheduler
}

migrate_runtime_state() {
  cd "$runtime_dir"
  log "stopping scheduler writes before runtime-state migration"
  compose stop scheduler >/dev/null 2>&1 || true
  if [ ! -f "$runtime_state" ]; then
    log "runtime state is missing: ${runtime_state}"
    exit 1
  fi
  PYTHONPATH="${runtime_dir}/src" python scripts/migrate_runtime_state_v9.py \
    "$runtime_state" --dry-run > "${runtime_state}.v9-dry-run.json"
  PYTHONPATH="${runtime_dir}/src" python scripts/migrate_runtime_state_v9.py \
    "$runtime_state" > "${runtime_state}.v9-migration.json"
  log "runtime state migrated; backup checksum and sidecar counts captured"
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
build = health.get("build") or {}
expected = open(".build_commit", encoding="utf-8").read().strip()
print(json.dumps({"mode": health.get("mode"), "runtime": runtime, "build": build}, ensure_ascii=False))
if runtime.get("advisory_only") is not True:
    sys.exit(3)
if runtime.get("training_enabled") is not False:
    sys.exit(4)
if build.get("commit") in {None, "", "unknown"} or build.get("trusted") is not True:
    sys.exit(5)
if build.get("commit") != expected:
    sys.exit(6)
PY
    code=$?
    set -e

    if [ "$code" -eq 0 ]; then
      log "health gate passed: advisory_only=true and training_enabled=false"
      return 0
    fi
    if [ "$code" -ge 3 ] && [ "$code" -le 6 ]; then
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

verify_build_identity() {
  cd "$runtime_dir"
  api_image="$(docker inspect --format '{{.Image}}' stock-analyzer-api)"
  scheduler_image="$(docker inspect --format '{{.Image}}' stock-analyzer-scheduler)"
  if [ -z "$api_image" ] || [ "$api_image" != "$scheduler_image" ]; then
    log "api/scheduler image digest mismatch: api=${api_image} scheduler=${scheduler_image}"
    exit 1
  fi
  attempt=1
  while [ "$attempt" -le "$health_attempts" ]; do
    if [ -s "${runtime_artifacts_dir}/runtime/scheduler_heartbeat.json" ]; then
      break
    fi
    sleep "$health_sleep_sec"
    attempt=$((attempt + 1))
  done
  python - "$runtime_dir" "$runtime_artifacts_dir" "$api_image" <<'PY'
import json
import pathlib
import sys

runtime_dir = pathlib.Path(sys.argv[1])
artifacts = pathlib.Path(sys.argv[2])
digest = sys.argv[3]
expected = (runtime_dir / ".build_commit").read_text(encoding="utf-8").strip()
heartbeat = json.loads((artifacts / "runtime" / "scheduler_heartbeat.json").read_text(encoding="utf-8"))
scheduler_commit = ((heartbeat.get("build") or {}).get("commit") or "").strip()
if not expected or expected == "unknown" or scheduler_commit != expected:
    raise SystemExit("scheduler build identity mismatch")
report = {"repo_head": expected, "scheduler_commit": scheduler_commit, "image_digest": digest}
(artifacts / "runtime" / "build_identity_gate.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False))
PY
}

run_collection() {
  cd "$runtime_dir"
  log "capturing NAS environment evidence"
  python scripts/export_support_bundle.py --mode host \
    --base-url "$api_base" \
    --output "${output_dir}/nas_support_bundle_after.json"
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
migrate_runtime_state
rebuild_services
wait_for_safe_health
verify_build_identity
run_collection
