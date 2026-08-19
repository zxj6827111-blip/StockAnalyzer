from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (REPO_ROOT / "scripts" / "p1_nas_rebuild_and_collect.sh").read_text(
        encoding="utf-8",
    )


def test_p1_nas_rebuild_wrapper_forces_advisory_compose_override() -> None:
    script = _script()

    assert "docker-compose.advisory.yml" in script
    assert "docker-compose.runtime.yml" in script
    assert "docker-compose.vendor-overlay.yml" in script
    assert "docker-compose.runtime.localvol.yml" not in script
    assert "SKIP_GIT_FETCH" in script
    assert "required_head" in script
    assert "git merge-base --is-ancestor" in script
    assert "rsync -av --delete" in script
    assert "git rev-parse HEAD > .build_commit" in script
    assert "STOCK_ANALYZER_BUILD_COMMIT" in script
    assert "STOCK_ANALYZER_BUILD_SHORT_COMMIT" in script
    assert "STOCK_ANALYZER_BUILD_DIRTY=false" in script
    assert "migrate_runtime_state_v9.py" in script
    assert "runtime_state_rollback_path" in script
    assert 'cp -p "$runtime_state" "$runtime_state_rollback_path"' in script
    assert "--dry-run" in script
    assert "runtime_state_backups" not in script  # migration helper owns backup naming/checksums
    assert 'build.get("trusted") is not True' in script
    assert "api_image" in script and "critical_image" in script and "heavy_image" in script
    assert 'f"scheduler_{group}_heartbeat.json"' in script
    assert 'for group in ("critical", "heavy")' in script
    assert "--mode host" in script
    assert ".rollback_image" in script
    assert "stock_analyzer_runtime_artifacts" in script
    assert '--runtime-state "$runtime_state"' in script
    assert '--model-artifact "$model_artifact"' in script
    assert "compose build api" in script
    assert "build_vendor_intraday_summary.py" in script
    assert 'summary_candidate="${summary_current}.candidate-${deploy_id}"' in script
    assert "promote_candidate_summary" in script
    assert "SA__DATA_SOURCE__INTRADAY_RUNTIME_MODE" in script
    assert "SA__DATA_SOURCE__INTRADAY_ZIP_FALLBACK_ENABLED" in script
    cutover_index = script.index("cutover_services()")
    assert script.index("build_vendor_intraday_summary.py") < script.index(
        "backup_runtime_containers", cutover_index
    )
    promote_index = script.index(
        "promote_candidate_summary", script.index("cutover_services()")
    )
    assert promote_index < script.index(
        "compose up -d --no-build --force-recreate api scheduler-critical scheduler-heavy"
    )
    assert (
        "compose up -d --no-build --force-recreate api scheduler-critical scheduler-heavy" in script
    )
    assert "p1_capture_nas_environment.py" in script
    assert "p1_run_nas_advisory_collection.py" in script
    assert "p1_accept_nas_advisory_collection.py" in script
    assert "p1_audit_goal_completion.py" in script
    assert "--confirm-run" in script
    assert 'runtime.get("advisory_only") is not True' in script
    assert 'runtime.get("training_enabled") is not False' in script
    assert "collection will not start" in script
    assert "snapshot_runtime_containers" in script
    assert "restore_runtime_containers" in script
    assert "docker rename" in script
    assert "on_exit" in script and "rollback_runtime" in script
    assert "backup_scheduler_heartbeats" in script
    assert "restore_scheduler_heartbeats" in script
    assert 'backup="${path}.rollback-${deploy_id}"' in script
    assert "heartbeat_path.stat().st_mtime" not in script


def test_p1_nas_rebuild_tracks_partial_container_backups_during_rollback() -> None:
    script = _script()

    assert "runtime_container_backed_up=()" in script
    assert 'runtime_container_backed_up[index]="1"' in script
    assert 'if [ "${runtime_container_backed_up[index]:-0}" = "1" ]' in script
    assert "prepare_runtime_rollback()" in script
    assert "rollback: failed to quiesce unrenamed container" in script
    assert "rollback: original container disappeared before backup" in script


def test_p1_nas_rebuild_requires_fresh_scheduler_heartbeats() -> None:
    script = _script()

    assert "scheduler_heartbeat_backup_started=0" in script
    assert "scheduler_heartbeat_backup_started=1" in script
    assert "scheduler_heartbeat_backed_up=()" in script
    assert 'scheduler_heartbeat_backed_up[index]="1"' in script
    assert 'if ! mv "$path" "$backup"; then' in script
    assert "backup_scheduler_heartbeats" in script
    assert "restore_scheduler_heartbeats" in script
    assert "fresh scheduler heartbeat identity gate did not pass before timeout" in script
    assert "heartbeat_path.stat().st_mtime" not in script


def test_p1_nas_rebuild_surfaces_incomplete_automatic_rollback() -> None:
    script = _script()
    rollback_body = script[
        script.index("rollback_runtime()") : script.index("on_exit()")
    ]

    assert "if ! restore_previous_summary; then" in rollback_body
    assert "if ! restore_runtime_state; then" in rollback_body
    assert "&& ! restore_scheduler_heartbeats; then" in rollback_body
    assert "&& ! restore_runtime_containers; then" in rollback_body
    assert "rollback: failed to restore stock-analyzer:latest image tag" in rollback_body
    assert (
        "FATAL: automatic rollback was incomplete; manual recovery is required"
        in rollback_body
    )
    assert "return 1" in rollback_body
