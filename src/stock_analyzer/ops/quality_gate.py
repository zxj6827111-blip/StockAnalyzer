"""Layered quality-gate runner."""

from __future__ import annotations

import locale
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class QualityCommandSpec:
    """One quality gate command."""

    name: str
    command: tuple[str, ...]
    log_name: str
    blocking: bool = True


@dataclass
class QualityCommandResult:
    """Execution result for one quality gate command."""

    name: str
    command: list[str]
    returncode: int
    duration_ms: int
    log_path: str
    blocking: bool

    output_tail: str = ""


@dataclass
class QualityGateReport:
    """Structured report for a quality gate stage."""

    stage: str
    ok: bool
    started_at: str
    finished_at: str
    commands: list[QualityCommandResult] = field(default_factory=list)
    blocking_failures: list[str] = field(default_factory=list)
    non_blocking_failures: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        import json

        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


_RUFF_TARGETS = (
    "src/stock_analyzer/main.py",
    "src/stock_analyzer/api",
    "src/stock_analyzer/data/market_warehouse.py",
    "src/stock_analyzer/data/vendor_zip_overlay.py",
    "src/stock_analyzer/pipeline.py",
    "src/stock_analyzer/backtest/asof_scan.py",
    "src/stock_analyzer/backtest/holding_curve.py",
    "src/stock_analyzer/runtime/services/asof_backtest_service.py",
    "src/stock_analyzer/runtime/service.py",
    "src/stock_analyzer/runtime/scheduler_worker.py",
    "src/stock_analyzer/runtime/scheduler_job_worker.py",
    "src/stock_analyzer/runtime/scheduler_supervisor.py",
    "src/stock_analyzer/runtime/universe_candidate_selector.py",
    "src/stock_analyzer/runtime/services",
    "src/stock_analyzer/ops",
    "src/stock_analyzer/data/financial_pit.py",
    "scripts/probe_universe_quality_selector.py",
    "scripts/backfill_financial_snapshots.py",
    "scripts/update_vendor_daily_from_tushare.py",
    "scripts/build_vendor_intraday_summary.py",
    "scripts/export_support_bundle.py",
    "scripts/watchdog_scheduler.py",
    "tests/test_release_preflight.py",
    "tests/test_release_smoke.py",
    "tests/test_release_snapshot.py",
    "tests/test_staging_rehearsal.py",
    "tests/test_nas_deploy_update_script.py",
    "tests/test_compose_intraday_scheduler.py",
    "tests/test_build_vendor_intraday_summary.py",
    "tests/test_scheduler_job_worker.py",
    "tests/test_scheduler_supervisor.py",
    "tests/test_watchdog_scheduler.py",
    "tests/test_support_bundle.py",
    "tests/test_pipeline.py",
    "tests/test_pipeline_asof.py",
    "tests/test_holding_curve.py",
    "tests/test_api_backtest.py",
    "tests/test_news_daily_archive.py",
    "tests/test_vendor_zip_overlay.py",
    "tests/test_update_vendor_daily_from_tushare.py",
    "tests/test_universe_candidate_selector.py",
    "tests/test_service_week5.py",
    "tests/test_pipeline_parallel.py",
    "tests/test_week5_scan_funnel_policy.py",
    "tests/test_probe_universe_quality_selector.py",
    "tests/test_backfill_financial_snapshots.py",
    "tests/test_financial_pit.py",
    "tests/test_file_lock.py",
    "tests/test_scheduler_worker.py",
    "tests/test_main_scheduler_run_due.py",
    "tests/test_market_warehouse.py",
    "tests/test_week5_automation.py",
)

_MYPY_BLOCKING_TARGETS = (
    "src/stock_analyzer/main.py",
    "src/stock_analyzer/api",
    "src/stock_analyzer/data/market_warehouse.py",
    "src/stock_analyzer/data/vendor_zip_overlay.py",
    "src/stock_analyzer/pipeline.py",
    "src/stock_analyzer/backtest/asof_scan.py",
    "src/stock_analyzer/backtest/holding_curve.py",
    "src/stock_analyzer/runtime/services/asof_backtest_service.py",
    "src/stock_analyzer/runtime/services/market_sync_service.py",
    "src/stock_analyzer/runtime/services/dashboard_service.py",
    "src/stock_analyzer/runtime/services/evolution_core_service.py",
    "src/stock_analyzer/runtime/services/idle_queue_service.py",
    "src/stock_analyzer/runtime/services/idle_queue_orchestration_service.py",
    "src/stock_analyzer/runtime/services/idle_queue_weekend_service.py",
    "src/stock_analyzer/runtime/services/idle_queue_workday_service.py",
    "src/stock_analyzer/runtime/services/runtime_state_service.py",
    "src/stock_analyzer/runtime/services/reconcile_service.py",
    "src/stock_analyzer/runtime/services/week7_sim_broker_service.py",
    "src/stock_analyzer/ops/release_preflight.py",
    "src/stock_analyzer/ops/release_smoke.py",
    "src/stock_analyzer/ops/staging_rehearsal.py",
    "src/stock_analyzer/ops/release_snapshot.py",
    "src/stock_analyzer/ops/file_lock.py",
    "src/stock_analyzer/runtime/scheduler_worker.py",
    "src/stock_analyzer/runtime/scheduler_job_worker.py",
    "src/stock_analyzer/runtime/scheduler_supervisor.py",
    "src/stock_analyzer/runtime/services/week5_automation_service.py",
    "src/stock_analyzer/runtime/services/week5_candidate_state.py",
    "src/stock_analyzer/runtime/services/week5_market_snapshot_service.py",
)

_MYPY_INFORMATIONAL_TARGETS = (
    "src/stock_analyzer/runtime/services/acceptance_service.py",
    "src/stock_analyzer/runtime/services/evolution_release_service.py",
    "src/stock_analyzer/runtime/services/idle_queue_storage_service.py",
    "src/stock_analyzer/runtime/services/training_service.py",
)

_SMOKE_TEST_NODES = (
    "tests/test_nas_deploy_update_script.py",
    "tests/test_release_preflight.py",
    "tests/test_release_smoke.py",
    "tests/test_release_snapshot.py",
    "tests/test_staging_rehearsal.py",
    "tests/test_main_health.py",
    "tests/test_main_dashboard.py",
    "tests/test_main_acceptance.py",
    "tests/test_main_week7.py::test_week7_sim_broker_endpoints",
    "tests/test_service_acceptance.py",
    "tests/test_service_dashboard.py",
    "tests/test_service_portfolio.py::test_service_reconcile_with_broker_snapshot_command",
    "tests/test_service_portfolio.py::test_service_reconcile_requires_snapshot_when_enabled",
    "tests/test_service_portfolio.py::test_service_reconcile_weekly_report_contains_sim_vs_broker_fields",
    "tests/test_service_portfolio.py::test_service_reconcile_detects_quantity_and_account_mismatch",
    "tests/test_service_week7_sim_broker.py",
)

_INTEGRATION_TEST_NODES = _SMOKE_TEST_NODES + (
    "tests/test_service_market_warehouse.py",
    "tests/test_service_runtime_state_merge.py",
    "tests/test_service_runtime_state_persistence.py",
    "tests/test_service_runtime_archive.py",
    "tests/test_service_acceptance_bundle.py",
    "tests/test_acceptance_release_gate.py",
    "tests/test_phase_d_status.py",
    "tests/test_service_v13_acceptance_integration.py",
    "tests/test_vendor_zip_overlay.py",
    "tests/test_universe_candidate_selector.py",
    "tests/test_service_week5.py",
    "tests/test_probe_universe_quality_selector.py",
    "tests/test_backfill_financial_snapshots.py",
    "tests/test_financial_pit.py",
    "tests/test_market_warehouse.py",
    "tests/test_service_scheduler.py::test_tdx_sync_job_runs_at_configured_time",
    "tests/test_service_scheduler.py::test_market_warehouse_sync_job_runs_at_configured_time",
    "tests/test_service_scheduler.py::test_close_reconcile_job_reports_missing_snapshot",
    "tests/test_service_scheduler.py::test_close_reconcile_job_reports_position_mismatch",
    "tests/test_service_evolution_scheduler.py::test_evolution_offhours_refreshes_tdx_before_run_when_enabled",
    "tests/test_service_evolution_scheduler.py::test_evolution_offhours_refreshes_market_warehouse_before_run_when_enabled",
    "tests/test_week5_automation.py",
)

_SLOW_TEST_FILES = (
    "tests/test_service_closed_loop_flow.py",
    "tests/test_service_market_warehouse.py",
    "tests/test_service_runtime_state_persistence.py",
    "tests/test_service_week6.py",
    "tests/test_service_week6_execution.py",
    "tests/test_service_week6_data_quality.py",
    "tests/test_main_week5.py",
    "tests/test_main_week6.py",
    "tests/test_main_news_preview.py",
    "tests/test_service_news_preview.py",
    "tests/test_intraday_factors.py",
    "tests/test_universe_candidate_selector.py",
)

# Full-suite stage: run every test under testpaths (except the slow files that
# are exercised by the non-blocking slow-report stage) in parallel and enforce
# a coverage floor. Threshold is a conservative baseline: a full local run on
# Python 3.11 reports ~79% line coverage after excluding the slow files, so a
# 75% floor leaves headroom for runner-to-runner variance. Workers match the
# GitHub-hosted ubuntu-latest runner (4 vCPU); -n 2 previously left half the
# cores idle (~476s local full suite -> ~4x less with -n 4).
_FULL_COVERAGE_FLOOR = 75
_FULL_PARALLEL_WORKERS = "4"
# 按文件分组分发（而非默认的逐测试 load）：同一文件的测试固定在同一 worker
# 内串行，跨文件状态泄漏的组合面从"每次新增测试文件都会重排全部 worker 序列"
# 缩小到"只有同 worker 的文件邻居变化"。2026-08-29 实锤：新增一个 2 行测试
# 文件改变了 gw0 的测试序列，暴露了 test_week5_snapshot_integration 的顺序
# 敏感 flaky（CI 两次红、加一个无关文件后两次绿）。负载均衡略差于 load，
# 4 worker 下可接受。
_FULL_XDIST_DIST = "loadfile"


def build_stage_specs(stage: str) -> list[QualityCommandSpec]:
    """Build the command list for a quality gate stage."""
    normalized = stage.strip().lower()
    python = sys.executable
    if normalized == "clean-scope":
        specs = [
            QualityCommandSpec(
                name="ruff_clean_scope",
                command=(python, "-m", "ruff", "check", *_RUFF_TARGETS),
                log_name="ruff_clean_scope.log",
            ),
            # mypy 合并为单次调用：按 target 逐个起进程时，每次都要加载解释器
            # 与类型检查器（本地 24 进程串行 ~11s vs 合并单次 ~3s；CI 冷环境
            # 差异更大）。blocking 与 informational 保持两个进程，避免把
            # 非阻塞目标的失败升级为阻塞。
            QualityCommandSpec(
                name="mypy_blocking",
                command=(
                    python,
                    "-m",
                    "mypy",
                    *_MYPY_BLOCKING_TARGETS,
                    "--follow-imports",
                    "skip",
                ),
                log_name="mypy_blocking.log",
            ),
            QualityCommandSpec(
                name="mypy_informational",
                command=(
                    python,
                    "-m",
                    "mypy",
                    *_MYPY_INFORMATIONAL_TARGETS,
                    "--follow-imports",
                    "skip",
                ),
                log_name="mypy_informational.log",
                blocking=False,
            ),
        ]
        return specs
    if normalized == "smoke":
        return [
            QualityCommandSpec(
                name="pytest_smoke",
                command=(python, "-m", "pytest", *_SMOKE_TEST_NODES, "-q"),
                log_name="pytest_smoke.log",
            )
        ]
    if normalized == "integration":
        return [
            QualityCommandSpec(
                name="pytest_integration",
                command=(python, "-m", "pytest", *_INTEGRATION_TEST_NODES, "-q"),
                log_name="pytest_integration.log",
            )
        ]
    if normalized == "slow-report":
        return [
            QualityCommandSpec(
                name="pytest_slow_report",
                command=(
                    python,
                    "-m",
                    "pytest",
                    *_SLOW_TEST_FILES,
                    "-n",
                    _FULL_PARALLEL_WORKERS,
                    "--durations=20",
                    "-q",
                ),
                log_name="pytest_slow_report.log",
            )
        ]
    if normalized == "full":
        return [
            QualityCommandSpec(
                name="pytest_full_suite",
                command=(
                    python,
                    "-m",
                    "pytest",
                    "tests",
                    *(f"--ignore={path}" for path in _SLOW_TEST_FILES),
                    "-n",
                    _FULL_PARALLEL_WORKERS,
                    "--dist",
                    _FULL_XDIST_DIST,
                    "--cov=stock_analyzer",
                    "--cov-report=term",
                    "--cov-report=xml:artifacts/coverage/coverage.xml",
                    f"--cov-fail-under={_FULL_COVERAGE_FLOOR}",
                    "--durations=20",
                    "-q",
                ),
                log_name="pytest_full_suite.log",
            )
        ]
    if normalized == "all":
        return (
            build_stage_specs("clean-scope")
            + build_stage_specs("smoke")
            + build_stage_specs("integration")
        )
    raise ValueError(f"unsupported quality gate stage: {stage}")


def run_quality_gate(
    stage: str,
    *,
    project_root: str | Path | None = None,
) -> QualityGateReport:
    """Run one quality-gate stage and persist per-command logs."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    started_at = datetime.now()
    log_root = root / "artifacts" / "quality"
    log_root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "coverage").mkdir(parents=True, exist_ok=True)

    results: list[QualityCommandResult] = []
    blocking_failures: list[str] = []
    non_blocking_failures: list[str] = []
    for spec in build_stage_specs(stage):
        log_path = log_root / spec.log_name
        command_started = datetime.now()
        completed = subprocess.run(
            list(spec.command),
            cwd=root,
            capture_output=True,
            text=False,
            check=False,
        )
        duration_ms = int((datetime.now() - command_started).total_seconds() * 1000)
        stdout_text = _decode_subprocess_stream(completed.stdout)
        stderr_text = _decode_subprocess_stream(completed.stderr)
        output = stdout_text
        if stdout_text and stderr_text:
            output += "\n"
        output += stderr_text
        log_path.write_text(output, encoding="utf-8")
        result = QualityCommandResult(
            name=spec.name,
            command=list(spec.command),
            returncode=completed.returncode,
            duration_ms=duration_ms,
            log_path=str(log_path),
            blocking=spec.blocking,
            output_tail=_output_tail(output) if completed.returncode != 0 else "",
        )
        results.append(result)
        if completed.returncode != 0:
            if spec.blocking:
                blocking_failures.append(spec.name)
            else:
                non_blocking_failures.append(spec.name)

    finished_at = datetime.now()
    return QualityGateReport(
        stage=stage,
        ok=not blocking_failures,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        commands=results,
        blocking_failures=blocking_failures,
        non_blocking_failures=non_blocking_failures,
    )


def _decode_subprocess_stream(payload: bytes | str | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    candidates = [
        "utf-8",
        locale.getpreferredencoding(False) or "utf-8",
        "gb18030",
    ]
    tried: set[str] = set()
    for encoding in candidates:
        normalized = encoding.strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _output_tail(output: str, *, max_lines: int = 80, max_chars: int = 12_000) -> str:
    lines = output.splitlines()
    return "\n".join(lines[-max_lines:])[-max_chars:]
