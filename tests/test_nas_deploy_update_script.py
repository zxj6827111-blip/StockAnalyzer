from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stock_analyzer.ops.quality_gate import build_stage_specs

REPO_ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (REPO_ROOT / "scripts" / "nas_deploy_update.sh").read_text(
        encoding="utf-8",
    )


def test_nas_deploy_update_uses_bounded_health_readiness_polling() -> None:
    script = _script()

    assert 'HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-30}"' in script
    assert 'HEALTH_SLEEP_SEC="${HEALTH_SLEEP_SEC:-2}"' in script
    assert 'while [[ "${attempt}" -le "${HEALTH_ATTEMPTS}" ]]' in script
    assert "--connect-timeout 3 --max-time 10" in script
    assert 'sleep "${HEALTH_SLEEP_SEC}"' in script
    assert "health not ready" in script
    assert "sleep 3" not in script


@pytest.mark.skipif(os.name == "nt", reason="Bash syntax is verified on Linux CI")
def test_nas_deploy_update_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "scripts" / "nas_deploy_update.sh")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_nas_deploy_update_health_gate_remains_fail_closed() -> None:
    script = _script()

    assert 'h.get("status") != "ok"' in script
    assert 'build.get("commit") != expected' in script
    assert 'build.get("short_commit") != expected[:7]' in script
    assert 'build.get("trusted") is not True' in script
    assert 'runtime.get("advisory_only") is not True' in script
    assert 'runtime.get("training_enabled") is not False' in script
    assert "identity or safety validation failed" in script


def test_nas_deploy_update_starts_split_schedulers_by_default() -> None:
    script = _script()

    assert "START_SCHEDULERS=1" in script
    assert "--no-start-scheduler|--no-start-schedulers" in script
    assert "--start-scheduler|--start-schedulers" in script
    assert "scheduler-critical scheduler-heavy" in script
    assert "schedulers: explicitly disabled by --no-start-schedulers" in script
    assert "validate_scheduler_heartbeat" in script
    assert "HEALTH_GATE_STARTED_EPOCH" in script


def test_nas_deploy_update_builds_candidate_summary_before_runtime_recreate() -> None:
    script = _script()

    assert "build_vendor_intraday_summary.py" in script
    assert 'volume_definitions = payload.get("volumes") or {}' in script
    assert "def resolve_mount_source(item: dict[str, object]) -> str:" in script
    assert 'return str(definition.get("name") or source)' in script
    assert 'SUMMARY_CANDIDATE="${SUMMARY_CURRENT}.candidate-${DEPLOY_ID}"' in script
    assert 'SUMMARY_CANDIDATE_MANIFEST="${SUMMARY_CANDIDATE}.manifest.json"' in script
    assert "promote_candidate_summary()" in script
    assert "INTRADAY_SUMMARY_KEEP_DAYS:-480" in script
    assert "INTRADAY_FRESHNESS_ARGS=()" in script
    assert "INTRADAY_SUMMARY_REQUIRED_LATEST_DATE:-" in script
    assert '"${INTRADAY_FRESHNESS_ARGS[@]}"' in script
    assert "runtime candidate sync is authoritative" in script
    assert "cannot resolve intraday summary freshness floor" not in script
    assert "/data/vendor_history:ro" in script
    assert "/data/intraday_summary" in script
    assert script.index("build api image with commit metadata") < script.index(
        "build_vendor_intraday_summary.py"
    )
    assert script.index("build_vendor_intraday_summary.py") < script.index(
        "if ! snapshot_runtime_containers"
    )
    cutover_index = script.index("if ! snapshot_runtime_containers")
    assert cutover_index < script.index("--force-recreate api", cutover_index)


def test_nas_deploy_update_reuses_valid_intraday_summary_by_default() -> None:
    script = _script()

    assert "FORCE_REBUILD_INTRADAY_SUMMARY=0" in script
    assert "--rebuild-intraday-summary" in script
    assert "--check-reusable" in script
    assert "${SUMMARY_HOST_ROOT}:/data/intraday_summary:ro" in script
    assert "existing intraday summary is valid and will be reused" in script
    assert 'if [[ "${SUMMARY_REUSED}" -eq 0 ]]; then' in script
    assert "existing intraday summary remains in place" in script
    reusable_check = script.index("--check-reusable")
    candidate_build = script.index(
        '--output "/data/intraday_summary/$(basename "${SUMMARY_CANDIDATE}")"',
        reusable_check,
    )
    assert reusable_check < candidate_build
    assert candidate_build < script.index("if ! snapshot_runtime_containers")


def test_nas_deploy_update_has_automatic_runtime_rollback() -> None:
    script = _script()

    assert "rollback_runtime()" in script
    assert "prepare_runtime_rollback()" in script
    assert "restore_previous_summary()" in script
    assert "restore_runtime_containers()" in script
    assert "docker rename" in script
    assert "'{{.State.Running}}'" in script
    assert "stock-analyzer:rollback-pre-" in script
    assert 'SUMMARY_ROLLBACK="${SUMMARY_CURRENT}.rollback-${DEPLOY_ID}"' in script
    assert "candidate intraday summary or manifest is missing" in script
    assert '"${RUNTIME_CONTAINER_RUNNING[index]}" == "true"' in script
    assert '"${RUNTIME_CONTAINER_RUNNING[index]:-false}" == "true"' in script
    assert "RUNTIME_CONTAINER_BACKED_UP=()" in script
    assert 'RUNTIME_CONTAINER_BACKED_UP[index]="1"' in script
    assert '"${RUNTIME_CONTAINER_BACKED_UP[index]:-0}" == "1"' in script
    assert "rollback: failed to quiesce unrenamed container" in script
    assert "rollback: original container disappeared before backup" in script
    assert "FATAL: automatic rollback was incomplete; manual recovery is required." in script
    rollback_body = script[
        script.index("rollback_runtime()") : script.index(
            'echo "[3/6] build api image with commit metadata'
        )
    ]
    assert "if ! restore_previous_summary; then" in rollback_body
    assert "&& ! restore_runtime_containers; then" in rollback_body
    assert "failed=1" in rollback_body
    assert '"${COMPOSE[@]}" up -d' not in rollback_body
    assert script.count("rollback_runtime") >= 4


def test_nas_deploy_update_no_recreate_is_image_build_only() -> None:
    script = _script()

    marker = 'if [[ "${DO_RECREATE}" -eq 0 ]]; then'
    assert marker in script
    assert "image build complete; runtime was not recreated" in script
    assert script.index(marker) < script.index('PORT="$(grep -E')


def test_nas_deploy_update_requires_duckdb_runtime_without_zip_fallback() -> None:
    script = _script()

    assert '"SA__DATA_SOURCE__INTRADAY_RUNTIME_MODE":"duckdb_required"' in script
    assert '"SA__DATA_SOURCE__INTRADAY_ZIP_FALLBACK_ENABLED":"false"' in script
    assert "intraday_summary is not read-only" in script
    assert "/data/intraday_summary/vendor_intraday_summary.duckdb" in script


def test_nas_deploy_update_resolves_host_python_interpreter() -> None:
    script = _script()

    assert 'HOST_PYTHON="${HOST_PYTHON:-}"' in script
    assert 'command -v "${HOST_PYTHON}"' in script
    assert "command -v python3" in script
    assert "command -v python" in script
    assert script.index("command -v python3 >") < script.index("command -v python >")
    assert "using python interpreter: ${HOST_PYTHON}" in script


def test_nas_deploy_update_uses_host_python_for_json_steps() -> None:
    script = _script()

    assert '"${HOST_PYTHON}" - "${RENDERED}"' in script
    assert '"${HOST_PYTHON}" - "${HEALTH}" "${COMMIT}"' in script
    assert "python - " not in script
    assert "python3 - " not in script


def test_nas_deploy_update_fails_closed_without_python() -> None:
    script = _script()

    assert "neither python3 nor python found on PATH" in script
    assert 'echo "ERROR: HOST_PYTHON=${HOST_PYTHON} not found on PATH." >&2' in script
    assert "exit 127" in script
    assert "import json, sys" in script


def test_nas_deploy_update_checks_run_in_quality_gate() -> None:
    clean_scope = next(
        spec for spec in build_stage_specs("clean-scope") if spec.name == "ruff_clean_scope"
    )
    smoke = next(spec for spec in build_stage_specs("smoke") if spec.name == "pytest_smoke")

    assert "tests/test_nas_deploy_update_script.py" in clean_scope.command
    assert "tests/test_nas_deploy_update_script.py" in smoke.command


# ---------------------------------------------------------------------------
# 受管 stock_updater 安装（2026-08-20 版本错位事故的回归防线）
# ---------------------------------------------------------------------------
def test_nas_deploy_update_installs_managed_updater_with_flag_gate() -> None:
    script = _script()

    # 只在新镜像真正支持新参数后才安装，杜绝脚本/镜像再次错位。
    assert "scripts/nas_stock_updater.sh" in script
    assert 'grep -q -- "--require-readiness"' in script
    assert (
        "image does not support --require-readiness; refusing to install "
        "the managed updater" in script
    )
    # 安装前校验受管脚本语法与关键运维能力。
    assert "bash -n" in script
    for capability in ("flock", "update_checkpoint.json", "sleep 1800", "--sync-vendor-delta"):
        assert f'"{capability}"' in script
    # 生产路径可覆盖，默认指向 NAS 现网位置；替换前先备份、原子落位。
    assert (
        'PROD_UPDATER="${STOCK_ANALYZER_PROD_UPDATER:-'
        '/vol1/docker/tools/stock_updater.sh}"' in script
    )
    assert "UPDATER_BACKUP=" in script
    assert ".stock_updater.staged-" in script
    # cron 指向校验为警告而非阻塞（cron 归 NAS 管理员所有）。
    assert "WARNING: crontab does not reference" in script


def test_nas_deploy_update_rollback_restores_previous_updater() -> None:
    """镜像回滚必须同步恢复旧 updater：回滚后的镜像可能不认新参数。"""
    script = _script()

    assert (
        "# The rolled-back image may lack the new updater flags, so the previous"
        in script
    )
    assert 'cp -p "${UPDATER_BACKUP}" "${PROD_UPDATER}"' in script


def test_nas_deploy_update_skips_updater_install_on_no_recreate() -> None:
    """--no-recreate 保持仅构建镜像语义：安装段整体位于 DO_RECREATE=1 守卫内。"""
    script = _script()

    installer_at = script.find('if [[ ! -f "${MANAGED_UPDATER_SRC}" ]]; then')
    assert installer_at != -1
    guard = 'if [[ "${DO_RECREATE}" -eq 1 ]]; then'
    guard_before_installer = script.rfind(guard, 0, installer_at)
    assert guard_before_installer != -1, (
        "managed updater install must be guarded by DO_RECREATE=1"
    )
    # 守卫与其闭合 fi 之间不得出现提前退出的 no-recreate 分支。
    closing_fi = script.find("\nfi\n", guard_before_installer)
    assert closing_fi != -1
    assert "runtime was not recreated" not in script[guard_before_installer:closing_fi]
