"""Run a lightweight scheduler soak against the runtime service."""
# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.config import load_config, StockAnalyzerConfig
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.scheduler import _due_interval_slot
from stock_analyzer.runtime.service import StockAnalyzerService


_TIMELINE = (
    "08:50:00",
    "09:00:00",
    "09:25:00",
    "09:30:00",
    "09:30:30",
    "09:31:00",
    "12:00:00",
    "15:30:00",
    "20:35:00",
    "20:40:00",
)


def _build_config(temp_root: Path) -> StockAnalyzerConfig:
    config = load_config(ROOT / "config" / "default.yaml")
    config.command_channel.secret_key = "scheduler-soak-secret"
    config.command_channel.state_persist_enabled = False

    config.scheduler.premarket_time = "09:00"
    config.scheduler.midday_news_time = "12:00"
    config.scheduler.auction_report_time = "09:25"
    config.scheduler.close_reconcile_time = "15:30"
    config.scheduler.week4_acceptance_time = "20:35"
    config.scheduler.week6_daily_time = "08:50"

    config.acceptance.enabled = True
    config.acceptance.auto_run = True
    config.acceptance.export_enabled = False
    config.acceptance.auto_notify = False
    config.acceptance.notify_on_pass = False

    config.week5.enabled = True
    config.week5.auto_run = True
    config.week5.auto_notify = False
    config.week5.first_board_window_intervals = []
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.first_board_interval_min = 1

    config.week6.enabled = True
    config.week6.auto_run = True
    config.week6.auto_notify = False
    config.week6.run_time = "08:50"
    config.week6.data_prewarm_enabled = False

    config.market_warehouse.enabled = False
    config.market_warehouse.auto_run = False
    config.tdx_sync.enabled = False
    config.tdx_sync.auto_run = False
    config.cloud_backup.enabled = False
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False

    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False

    config.evolution.enabled = True
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:40"
    config.evolution.m3_maintenance_interval_min = 20
    config.evolution.release_confirmation_watchdog_interval_min = 15
    config.evolution.strict_dependency_check = False
    config.evolution.auto_generate_loader_inputs = False
    config.evolution.code_commit_id = "git:scheduler-soak"

    offline_root = temp_root / "offline_package"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    config.training.artifact_path = str(temp_root / "test_model_scheduler_soak.json")
    config.training.bootstrap_state_path = str(
        temp_root / "test_bootstrap_state_scheduler_soak.json"
    )
    config.evolution.suggestions_dir = str((temp_root / "suggestions").relative_to(temp_root))
    config.evolution.manifest_path = str(
        (temp_root / "artifacts" / "evolution" / "run_manifest.json").relative_to(temp_root)
    )
    config.evolution.compliance_db_path = str(
        (temp_root / "artifacts" / "evolution" / "compliance.duckdb").relative_to(temp_root)
    )
    return config


def _new_service(config: StockAnalyzerConfig, temp_root: Path) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2053)
    service._provider = provider
    service._pipeline._provider = provider
    service._realtime_provider = provider
    if service._realtime_pipeline is not None:
        service._realtime_pipeline._provider = provider
    service._evolution_project_root = temp_root

    orchestrator_cls = service._evolution_orchestrator.__class__
    service._evolution_orchestrator = orchestrator_cls(
        config=config.evolution,
        project_root=temp_root,
    )

    # Keep soak focused on scheduler dispatch instead of runtime state I/O.
    service._refresh_runtime_state_from_disk_if_changed = lambda: None
    service._persist_runtime_state_to_disk = lambda: None
    service.state.watchlist = ["600000", "000001"]
    return service


def _install_lightweight_callbacks(service: StockAnalyzerService) -> Counter[str]:
    counts: Counter[str] = Counter()

    def _make_callback(name: str):
        def _callback() -> dict[str, object]:
            counts[name] += 1
            return {
                "job": name,
                "invocation": counts[name],
                "timestamp": datetime.now().isoformat(),
            }

        return _callback

    for job in service._scheduler._jobs.values():
        job.callback = _make_callback(job.name)
    for job in service._scheduler._interval_jobs.values():
        job.callback = _make_callback(job.name)
    return counts


def _simulate(
    *,
    service: StockAnalyzerService,
    days: int,
    start_date: date,
) -> tuple[dict[str, int], list[dict[str, object]], dict[str, object], int]:
    runs_by_job: Counter[str] = Counter()
    failures: list[dict[str, object]] = []
    total_results = 0

    for offset in range(days):
        current_date = start_date + timedelta(days=offset)
        for hhmmss in _TIMELINE:
            now = datetime.fromisoformat(f"{current_date.isoformat()}T{hhmmss}")
            results = service.run_due_jobs(now=now)
            total_results += len(results)
            for result in results:
                if bool(result.get("ran")):
                    job_name = str(result.get("job", "")).strip()
                    runs_by_job[job_name] += 1
                if bool(result.get("ran")) and not bool(result.get("success")):
                    failures.append(
                        {
                            "timestamp": now.isoformat(),
                            "job": str(result.get("job", "")).strip(),
                            "detail": str(result.get("detail", "")).strip(),
                        }
                    )

    return (
        dict(sorted(runs_by_job.items())),
        failures,
        service._scheduler.export_state(),
        total_results,
    )


def _expected_runs(
    *,
    service: StockAnalyzerService,
    days: int,
    start_date: date,
) -> dict[str, int]:
    expected: Counter[str] = Counter()
    for offset in range(days):
        current_date = start_date + timedelta(days=offset)
        daily_done: set[str] = set()
        interval_slots: dict[str, tuple[date, int]] = {}
        for hhmmss in _TIMELINE:
            current = datetime.fromisoformat(f"{current_date.isoformat()}T{hhmmss}")
            for name, job in service._scheduler._jobs.items():
                if name in daily_done:
                    continue
                if job.latest_time is not None and current.time() > job.latest_time:
                    daily_done.add(name)
                    continue
                if current.time() >= job.trigger_time:
                    expected[name] += 1
                    daily_done.add(name)
            for name, job in service._scheduler._interval_jobs.items():
                slot = _due_interval_slot(job=job, current=current.time())
                if slot is None:
                    continue
                marker = (current_date, slot)
                if interval_slots.get(name) == marker:
                    continue
                expected[name] += 1
                interval_slots[name] = marker
    return dict(sorted(expected.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run lightweight scheduler soak")
    parser.add_argument("--days", type=int, default=10, help="Number of simulated days")
    parser.add_argument(
        "--output",
        default=str(ROOT / "artifacts" / "quality" / "scheduler_soak_report.json"),
        help="Output JSON report path",
    )
    args = parser.parse_args()

    if args.days <= 0:
        raise SystemExit("--days must be > 0")

    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_scheduler_soak"
    temp_root.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    config = _build_config(temp_root)
    service = _new_service(config=config, temp_root=temp_root)
    lightweight_invocations = _install_lightweight_callbacks(service)

    started_at = datetime.now().isoformat()
    started_perf = perf_counter()
    runs_by_job, failures, scheduler_state, total_results = _simulate(
        service=service,
        days=args.days,
        start_date=date(2026, 3, 2),
    )
    duration_ms = int((perf_counter() - started_perf) * 1000)
    expected_runs = _expected_runs(
        service=service,
        days=args.days,
        start_date=date(2026, 3, 2),
    )

    mismatches = [
        {
            "job": job,
            "expected": expected,
            "actual": runs_by_job.get(job, 0),
        }
        for job, expected in sorted(expected_runs.items())
        if runs_by_job.get(job, 0) != expected
    ]

    last_run = scheduler_state.get("last_run", {})
    last_interval_slot = scheduler_state.get("last_interval_slot", {})
    state_ok = isinstance(last_run, dict) and isinstance(last_interval_slot, dict)
    if state_ok:
        state_ok = (
            len(last_run) == len(service._scheduler._jobs)
            and len(last_interval_slot) == len(service._scheduler._interval_jobs)
        )

    report = {
        "ok": not failures and not mismatches and state_ok,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "simulated_days": args.days,
        "timeline_points_per_day": len(_TIMELINE),
        "total_run_due_calls": args.days * len(_TIMELINE),
        "total_scheduler_results": total_results,
        "duration_ms": duration_ms,
        "expected_runs": expected_runs,
        "actual_runs": runs_by_job,
        "callback_invocations": dict(sorted(lightweight_invocations.items())),
        "failures": failures,
        "run_count_mismatches": mismatches,
        "scheduler_state_ok": state_ok,
        "scheduler_state": scheduler_state,
        "output_path": str(output_path),
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
