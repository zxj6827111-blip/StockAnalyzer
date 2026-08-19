from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from stock_analyzer.runtime.scheduler_job_worker import run_job_once
from stock_analyzer.runtime.service import StockAnalyzerService


class _FakeService:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = results
        self.only_jobs: list[str] | None = None

    def run_due_jobs(
        self,
        now: datetime | None = None,
        only_jobs: list[str] | None = None,
    ) -> list[dict[str, object]]:
        assert now is not None
        self.only_jobs = only_jobs
        return self.results


def _service(fake: _FakeService) -> StockAnalyzerService:
    return cast(StockAnalyzerService, cast(Any, fake))


def test_run_job_once_executes_exact_job() -> None:
    fake = _FakeService(
        [
            {
                "job": "premarket_scan",
                "ran": True,
                "success": True,
                "detail": "ok",
                "payload": {"signals": 3},
            }
        ]
    )

    payload = run_job_once(
        service=_service(fake),
        job="premarket_scan",
        now=datetime.fromisoformat("2026-08-18T08:30:00"),
        run_id="run-1",
    )

    assert fake.only_jobs == ["premarket_scan"]
    assert payload["status"] == "success"
    assert payload["success"] is True
    assert payload["ran"] is True


def test_run_job_once_reports_bootstrap_gate_failure() -> None:
    fake = _FakeService(
        [
            {
                "job": "bootstrap_gate",
                "ran": False,
                "success": False,
                "detail": "blocked_bootstrap_required",
                "payload": {},
            }
        ]
    )

    payload = run_job_once(
        service=_service(fake),
        job="evolution_offhours",
        now=datetime.fromisoformat("2026-08-18T21:45:00"),
        run_id="run-2",
    )

    assert payload["status"] == "failed"
    assert payload["success"] is False
    assert payload["detail"] == "blocked_bootstrap_required"
