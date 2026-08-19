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
deploy_id="$(date -u +%Y%m%d%H%M%S)-$$"
previous_image=""
rollback_tag=""
runtime_state_rollback_path=""
runtime_container_names=(
  stock-analyzer-api
  stock-analyzer-scheduler
  stock-analyzer-scheduler-critical
  stock-analyzer-scheduler-heavy
)
runtime_container_backups=()
runtime_container_existed=()
runtime_container_running=()
runtime_container_backed_up=()
runtime_backup_started=0
scheduler_heartbeat_paths=(
  "${runtime_artifacts_dir}/runtime/scheduler_critical_heartbeat.json"
  "${runtime_artifacts_dir}/runtime/scheduler_heavy_heartbeat.json"
)
scheduler_heartbeat_backups=()
scheduler_heartbeat_existed=()
scheduler_heartbeat_backed_up=()
scheduler_heartbeat_backup_started=0
summary_current=""
summary_current_manifest=""
summary_candidate=""
summary_candidate_manifest=""
summary_rollback=""
summary_rollback_manifest=""
summary_old_db_backed_up=0
summary_old_manifest_backed_up=0
summary_new_db_promoted=0
summary_new_manifest_promoted=0
compose_files=(
  -f docker-compose.yml
  -f docker-compose.runtime.yml
  -f docker-compose.advisory.yml
  -f docker-compose.vendor-overlay.yml
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


snapshot_runtime_containers() {
  local name
  local backup
  local running
  runtime_container_backups=()
  runtime_container_existed=()
  runtime_container_running=()
  runtime_container_backed_up=()
  for name in "${runtime_container_names[@]}"; do
    backup="${name}.rollback-${deploy_id}"
    if docker inspect "$backup" >/dev/null 2>&1; then
      log "rollback container already exists: ${backup}"
      return 1
    fi
    runtime_container_backups+=("$backup")
    runtime_container_backed_up+=("0")
    if docker inspect "$name" >/dev/null 2>&1; then
      if ! running="$(docker inspect --format '{{.State.Running}}' "$name")"; then
        return 1
      fi
      runtime_container_existed+=("1")
      runtime_container_running+=("$running")
    else
      runtime_container_existed+=("0")
      runtime_container_running+=("false")
    fi
  done
}

backup_runtime_containers() {
  local index
  local name
  local backup
  runtime_backup_started=1
  for index in "${!runtime_container_names[@]}"; do
    if [ "${runtime_container_existed[index]}" != "1" ]; then
      continue
    fi
    name="${runtime_container_names[index]}"
    backup="${runtime_container_backups[index]}"
    if [ "${runtime_container_running[index]}" = "true" ]; then
      if ! docker stop "$name" >/dev/null; then
        return 1
      fi
    fi
    if ! docker rename "$name" "$backup"; then
      return 1
    fi
    runtime_container_backed_up[index]="1"
  done
}

prepare_runtime_rollback() {
  local index
  local name
  local failed=0
  for index in "${!runtime_container_names[@]}"; do
    name="${runtime_container_names[index]}"
    if [ "${runtime_container_backed_up[index]:-0}" = "1" ] \
      || [ "${runtime_container_existed[index]:-0}" = "0" ]; then
      if docker inspect "$name" >/dev/null 2>&1 \
        && ! docker rm -f "$name" >/dev/null; then
        log "rollback: failed to stop replacement container ${name}"
        failed=1
      fi
      continue
    fi
    if docker inspect "$name" >/dev/null 2>&1; then
      if ! docker stop "$name" >/dev/null 2>&1; then
        log "rollback: failed to quiesce unrenamed container ${name}"
        failed=1
      fi
    else
      log "rollback: original container disappeared before backup: ${name}"
      failed=1
    fi
  done
  return "$failed"
}

restore_runtime_containers() {
  local index
  local name
  local backup
  local failed=0
  for index in "${!runtime_container_names[@]}"; do
    name="${runtime_container_names[index]}"
    backup="${runtime_container_backups[index]:-}"
    if [ "${runtime_container_backed_up[index]:-0}" = "1" ]; then
      if ! docker inspect "$backup" >/dev/null 2>&1; then
        log "rollback: backup container missing: ${backup}"
        failed=1
        continue
      fi
      if ! docker rename "$backup" "$name" >/dev/null; then
        log "rollback: failed to restore container name ${name}"
        failed=1
        continue
      fi
    elif [ "${runtime_container_existed[index]:-0}" = "1" ] \
      && ! docker inspect "$name" >/dev/null 2>&1; then
      log "rollback: unrenamed original container is missing: ${name}"
      failed=1
      continue
    fi

    if [ "${runtime_container_existed[index]:-0}" = "1" ] \
      && [ "${runtime_container_running[index]:-false}" = "true" ] \
      && ! docker start "$name" >/dev/null; then
      log "rollback: failed to restart container ${name}"
      failed=1
    fi
  done
  return "$failed"
}

cleanup_runtime_backups() {
  local backup
  for backup in "${runtime_container_backups[@]}"; do
    docker rm -f "$backup" >/dev/null 2>&1 || true
  done
  runtime_backup_started=0
}


backup_scheduler_heartbeats() {
  local index
  local path
  local backup
  scheduler_heartbeat_backups=()
  scheduler_heartbeat_existed=()
  scheduler_heartbeat_backed_up=()
  for path in "${scheduler_heartbeat_paths[@]}"; do
    backup="${path}.rollback-${deploy_id}"
    scheduler_heartbeat_backups+=("$backup")
    scheduler_heartbeat_backed_up+=("0")
    if [ -e "$backup" ]; then
      log "heartbeat rollback file already exists: ${backup}"
      return 1
    fi
    if [ -e "$path" ]; then
      scheduler_heartbeat_existed+=("1")
    else
      scheduler_heartbeat_existed+=("0")
    fi
  done

  scheduler_heartbeat_backup_started=1
  for index in "${!scheduler_heartbeat_paths[@]}"; do
    if [ "${scheduler_heartbeat_existed[index]}" != "1" ]; then
      continue
    fi
    path="${scheduler_heartbeat_paths[index]}"
    backup="${scheduler_heartbeat_backups[index]}"
    if ! mv "$path" "$backup"; then
      return 1
    fi
    scheduler_heartbeat_backed_up[index]="1"
  done
}

restore_scheduler_heartbeats() {
  local index
  local path
  local backup
  local failed=0
  for index in "${!scheduler_heartbeat_paths[@]}"; do
    path="${scheduler_heartbeat_paths[index]}"
    backup="${scheduler_heartbeat_backups[index]:-}"
    if [ "${scheduler_heartbeat_backed_up[index]:-0}" = "1" ]; then
      if [ -e "$path" ] && ! rm -f "$path"; then
        log "rollback: failed to remove new scheduler heartbeat ${path}"
        failed=1
        continue
      fi
      if [ ! -e "$backup" ] || ! mv "$backup" "$path"; then
        log "rollback: failed to restore scheduler heartbeat ${path}"
        failed=1
      fi
    elif [ "${scheduler_heartbeat_existed[index]:-0}" = "0" ] \
      && [ -e "$path" ] && ! rm -f "$path"; then
      log "rollback: failed to remove new scheduler heartbeat ${path}"
      failed=1
    fi
  done
  return "$failed"
}

cleanup_scheduler_heartbeat_backups() {
  local backup
  for backup in "${scheduler_heartbeat_backups[@]}"; do
    rm -f "$backup"
  done
}

cleanup_summary_candidates() {
  if [ -z "$summary_candidate" ]; then
    return 0
  fi
  rm -f \
    "$summary_candidate" \
    "$summary_candidate_manifest" \
    "${summary_candidate}.next" \
    "${summary_candidate}.next.manifest.json" \
    "${summary_candidate}.previous" \
    "${summary_candidate}.previous.manifest.json"
}

promote_candidate_summary() {
  if [ ! -s "$summary_candidate" ] || [ ! -s "$summary_candidate_manifest" ]; then
    log "candidate intraday summary or manifest is missing"
    return 1
  fi
  rm -f "$summary_rollback" "$summary_rollback_manifest"
  if [ -e "$summary_current" ]; then
    if ! mv "$summary_current" "$summary_rollback"; then
      return 1
    fi
    summary_old_db_backed_up=1
  fi
  if [ -e "$summary_current_manifest" ]; then
    if ! mv "$summary_current_manifest" "$summary_rollback_manifest"; then
      return 1
    fi
    summary_old_manifest_backed_up=1
  fi
  if ! mv "$summary_candidate" "$summary_current"; then
    return 1
  fi
  summary_new_db_promoted=1
  if ! mv "$summary_candidate_manifest" "$summary_current_manifest"; then
    return 1
  fi
  summary_new_manifest_promoted=1
  test -s "$summary_current"
  test -s "$summary_current_manifest"
}

restore_previous_summary() {
  local failed=0
  if [ -z "$summary_current" ]; then
    return 0
  fi
  if [ "$summary_new_db_promoted" -eq 1 ] \
    || { [ "$summary_old_db_backed_up" -eq 1 ] && [ -e "$summary_current" ]; }; then
    if ! rm -f "$summary_current"; then
      log "rollback: failed to remove promoted intraday summary"
      failed=1
    fi
  fi
  if [ "$summary_new_manifest_promoted" -eq 1 ] \
    || { [ "$summary_old_manifest_backed_up" -eq 1 ] \
      && [ -e "$summary_current_manifest" ]; }; then
    if ! rm -f "$summary_current_manifest"; then
      log "rollback: failed to remove promoted intraday manifest"
      failed=1
    fi
  fi
  if [ "$summary_old_db_backed_up" -eq 1 ]; then
    if [ ! -e "$summary_rollback" ] || ! mv "$summary_rollback" "$summary_current"; then
      log "rollback: failed to restore previous intraday summary"
      failed=1
    fi
  fi
  if [ "$summary_old_manifest_backed_up" -eq 1 ]; then
    if [ ! -e "$summary_rollback_manifest" ] \
      || ! mv "$summary_rollback_manifest" "$summary_current_manifest"; then
      log "rollback: failed to restore previous intraday manifest"
      failed=1
    fi
  fi
  if ! cleanup_summary_candidates; then
    log "rollback: failed to clean candidate intraday summary files"
    failed=1
  fi
  return "$failed"
}

cleanup_summary_rollback() {
  rm -f "$summary_rollback" "$summary_rollback_manifest"
  cleanup_summary_candidates
}

restore_runtime_state() {
  if [ -z "$runtime_state_rollback_path" ] || [ ! -s "$runtime_state_rollback_path" ]; then
    return 0
  fi
  local restore_tmp="${runtime_state}.restore-${deploy_id}"
  if ! cp -p "$runtime_state_rollback_path" "$restore_tmp" \
    || ! mv "$restore_tmp" "$runtime_state"; then
    log "rollback: failed to restore runtime state"
    rm -f "$restore_tmp"
    return 1
  fi
}

rollback_runtime() {
  local failed=0
  log "rollback: restoring previous summary, runtime state, heartbeats, image, and containers"
  set +e
  if [ "$runtime_backup_started" -eq 1 ] && ! prepare_runtime_rollback; then
    failed=1
  fi
  if ! restore_previous_summary; then
    failed=1
  fi
  if ! restore_runtime_state; then
    failed=1
  fi
  if [ "$scheduler_heartbeat_backup_started" -eq 1 ] \
    && ! restore_scheduler_heartbeats; then
    failed=1
  fi
  if [ "$runtime_backup_started" -eq 1 ] && ! restore_runtime_containers; then
    failed=1
  fi
  if [ -n "$previous_image" ] && ! docker tag "$previous_image" stock-analyzer:latest; then
    log "rollback: failed to restore stock-analyzer:latest image tag"
    failed=1
  fi
  runtime_backup_started=0
  scheduler_heartbeat_backup_started=0
  set -e
  if [ "$failed" -ne 0 ]; then
    log "FATAL: automatic rollback was incomplete; manual recovery is required"
    return 1
  fi
  return 0
}

on_exit() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ]; then
    rollback_runtime
  else
    cleanup_runtime_backups || log "warning: stale rollback containers require cleanup"
    cleanup_summary_rollback || log "warning: stale summary rollback files require cleanup"
    cleanup_scheduler_heartbeat_backups || log "warning: stale heartbeat rollback files require cleanup"
    scheduler_heartbeat_backup_started=0
    if [ -n "$runtime_state_rollback_path" ]; then
      rm -f "$runtime_state_rollback_path" || log "warning: stale runtime-state rollback file requires cleanup"
    fi
  fi
  exit "$status"
}

trap on_exit EXIT

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

build_intraday_summary() {
  cd "$runtime_dir"
  local rendered
  rendered="$(mktemp)"
  compose config --format json > "$rendered"
  mapfile -t summary_mounts < <(python - "$rendered" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
services = payload.get("services") or {}
expected = {
    "SA__DATA_SOURCE__PRIMARY": "vendor_zip_overlay",
    "SA__DATA_SOURCE__INTRADAY_RUNTIME_MODE": "duckdb_required",
    "SA__DATA_SOURCE__INTRADAY_ZIP_FALLBACK_ENABLED": "false",
}
for name in ("api", "scheduler-critical", "scheduler-heavy"):
    service = services.get(name) or {}
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env if "=" in item)
    for key, value in expected.items():
        if str(env.get(key, "")).lower() != value:
            raise SystemExit(f"fail-closed: {name} {key}={env.get(key)!r}")
volumes = (services.get("api") or {}).get("volumes") or []
by_target = {
    item.get("target"): item.get("source")
    for item in volumes
    if isinstance(item, dict) and item.get("target") and item.get("source")
}
for target in ("/data/vendor_history", "/data/intraday_summary"):
    source = by_target.get(target)
    if not source:
        raise SystemExit(f"missing rendered mount source for {target}")
    print(source)
PY
  )
  rm -f "$rendered"
  if [ "${#summary_mounts[@]}" -ne 2 ]; then
    log "failed to resolve vendor and intraday summary host mounts"
    return 1
  fi
  local vendor_host_root="${summary_mounts[0]}"
  local summary_host_root="${summary_mounts[1]}"
  mkdir -p "$summary_host_root"
  summary_current="${summary_host_root}/vendor_intraday_summary.duckdb"
  summary_current_manifest="${summary_current}.manifest.json"
  summary_candidate="${summary_current}.candidate-${deploy_id}"
  summary_candidate_manifest="${summary_candidate}.manifest.json"
  summary_rollback="${summary_current}.rollback-${deploy_id}"
  summary_rollback_manifest="${summary_rollback}.manifest.json"
  cleanup_summary_candidates
  log "building candidate intraday summary DuckDB"
  docker run --rm \
    -v "${vendor_host_root}:/data/vendor_history:ro" \
    -v "${summary_host_root}:/data/intraday_summary" \
    stock-analyzer:latest \
    python /app/scripts/build_vendor_intraday_summary.py \
      --root /data/vendor_history \
      --output "/data/intraday_summary/$(basename "$summary_candidate")" \
      --keep-days "${INTRADAY_SUMMARY_KEEP_DAYS:-480}"
  test -s "$summary_candidate"
  test -s "$summary_candidate_manifest"
}

build_release_artifacts() {
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
  build_intraday_summary
}

migrate_runtime_state() {
  cd "$runtime_dir"
  if [ ! -f "$runtime_state" ]; then
    log "runtime state is missing: ${runtime_state}"
    return 1
  fi
  runtime_state_rollback_path="${runtime_state}.rollback-${deploy_id}"
  cp -p "$runtime_state" "$runtime_state_rollback_path"
  PYTHONPATH="${runtime_dir}/src" python scripts/migrate_runtime_state_v9.py \
    "$runtime_state" --dry-run > "${runtime_state}.v9-dry-run.json"
  PYTHONPATH="${runtime_dir}/src" python scripts/migrate_runtime_state_v9.py \
    "$runtime_state" > "${runtime_state}.v9-migration.json"
  log "runtime state migrated; exact pre-cutover copy retained for rollback"
}

cutover_services() {
  cd "$runtime_dir"
  log "preserving old containers before cutover"
  snapshot_runtime_containers
  backup_runtime_containers
  backup_scheduler_heartbeats
  migrate_runtime_state
  promote_candidate_summary
  log "starting api and split schedulers with the rebuilt image"
  compose up -d --no-build --force-recreate api scheduler-critical scheduler-heavy
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
  critical_image="$(docker inspect --format '{{.Image}}' stock-analyzer-scheduler-critical)"
  heavy_image="$(docker inspect --format '{{.Image}}' stock-analyzer-scheduler-heavy)"
  if [ -z "$api_image" ] || [ "$api_image" != "$critical_image" ] || [ "$api_image" != "$heavy_image" ]; then
    log "runtime image digest mismatch: api=${api_image} critical=${critical_image} heavy=${heavy_image}"
    return 1
  fi
  attempt=1
  while [ "$attempt" -le "$health_attempts" ]; do
    if python - "$runtime_dir" "$runtime_artifacts_dir" "$api_image" <<'PY'
import json
import pathlib
import sys

runtime_dir = pathlib.Path(sys.argv[1])
artifacts = pathlib.Path(sys.argv[2])
digest = sys.argv[3]
expected = (runtime_dir / ".build_commit").read_text(encoding="utf-8").strip()
commits = {}
for group in ("critical", "heavy"):
    heartbeat_path = artifacts / "runtime" / f"scheduler_{group}_heartbeat.json"
    if not heartbeat_path.is_file():
        raise SystemExit(1)
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    scheduler_commit = ((heartbeat.get("build") or {}).get("commit") or "").strip()
    if not expected or expected == "unknown" or scheduler_commit != expected:
        raise SystemExit(1)
    commits[group] = scheduler_commit
report = {
    "repo_head": expected,
    "scheduler_commits": commits,
    "image_digest": digest,
}
(artifacts / "runtime" / "build_identity_gate.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False))
PY
    then
      return 0
    fi
    log "waiting for fresh scheduler heartbeats, retry ${attempt}/${health_attempts}"
    sleep "$health_sleep_sec"
    attempt=$((attempt + 1))
  done
  log "fresh scheduler heartbeat identity gate did not pass before timeout"
  return 1
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
build_release_artifacts
cutover_services
wait_for_safe_health
verify_build_identity
run_collection
