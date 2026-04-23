"""Layered quality-gate runner."""

from __future__ import annotations

import locale
import subprocess
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
    "src/stock_analyzer/data/market_warehouse.py",
    "src/stock_analyzer/runtime/service.py",
    "src/stock_analyzer/runtime/services",
    "src/stock_analyzer/ops",
    "tests/test_release_preflight.py",
    "tests/test_release_smoke.py",
    "tests/test_release_snapshot.py",
    "tests/test_staging_rehearsal.py",
)

_MYPY_BLOCKING_TARGETS = (
    "src/stock_analyzer/data/market_warehouse.py",
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
)

_MYPY_INFORMATIONAL_TARGETS = (
    "src/stock_analyzer/runtime/services/acceptance_service.py",
    "src/stock_analyzer/runtime/services/evolution_release_service.py",
    "src/stock_analyzer/runtime/services/idle_queue_storage_service.py",
    "src/stock_analyzer/runtime/services/training_service.py",
)

_SMOKE_TEST_NODES = (
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
    "tests/test_service_scheduler.py::test_tdx_sync_job_runs_at_configured_time",
    "tests/test_service_scheduler.py::test_market_warehouse_sync_job_runs_at_configured_time",
    "tests/test_service_scheduler.py::test_close_reconcile_job_reports_missing_snapshot",
    "tests/test_service_scheduler.py::test_close_reconcile_job_reports_position_mismatch",
    "tests/test_service_evolution_scheduler.py::test_evolution_offhours_refreshes_tdx_before_run_when_enabled",
    "tests/test_service_evolution_scheduler.py::test_evolution_offhours_refreshes_market_warehouse_before_run_when_enabled",
)

_SLOW_TEST_FILES = (
    "tests/test_service_closed_loop_flow.py",
    "tests/test_service_market_warehouse.py",
    "tests/test_service_runtime_state_persistence.py",
    "tests/test_service_week5.py",
    "tests/test_service_week6.py",
    "tests/test_service_week6_execution.py",
    "tests/test_service_week6_data_quality.py",
    "tests/test_main_week5.py",
    "tests/test_main_week6.py",
    "tests/test_main_news_preview.py",
    "tests/test_service_news_preview.py",
    "tests/test_intraday_factors.py",
)


def build_stage_specs(stage: str) -> list[QualityCommandSpec]:
    """Build the command list for a quality gate stage."""
    normalized = stage.strip().lower()
    if normalized == "clean-scope":
        specs = [
            QualityCommandSpec(
                name="ruff_clean_scope",
                command=("python", "-m", "ruff", "check", *_RUFF_TARGETS),
                log_name="ruff_clean_scope.log",
            )
        ]
        specs.extend(
            QualityCommandSpec(
                name=f"mypy_{Path(target).stem}",
                command=("python", "-m", "mypy", target, "--follow-imports", "skip"),
                log_name=f"mypy_{Path(target).stem}.log",
            )
            for target in _MYPY_BLOCKING_TARGETS
        )
        specs.extend(
            QualityCommandSpec(
                name=f"mypy_{Path(target).stem}",
                command=("python", "-m", "mypy", target, "--follow-imports", "skip"),
                log_name=f"mypy_{Path(target).stem}.log",
                blocking=False,
            )
            for target in _MYPY_INFORMATIONAL_TARGETS
        )
        return specs
    if normalized == "smoke":
        return [
            QualityCommandSpec(
                name="pytest_smoke",
                command=("python", "-m", "pytest", *_SMOKE_TEST_NODES, "-q"),
                log_name="pytest_smoke.log",
            )
        ]
    if normalized == "integration":
        return [
            QualityCommandSpec(
                name="pytest_integration",
                command=("python", "-m", "pytest", *_INTEGRATION_TEST_NODES, "-q"),
                log_name="pytest_integration.log",
            )
        ]
    if normalized == "slow-report":
        return [
            QualityCommandSpec(
                name="pytest_slow_report",
                command=("python", "-m", "pytest", *_SLOW_TEST_FILES, "--durations=20", "-q"),
                log_name="pytest_slow_report.log",
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
