from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.secret_key = "test-secret"
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = True
    config.command_channel.history_archive_dir = str(tmp_path / "runtime_history")
    config.command_channel.history_archive_retention_days = 10
    config.command_channel.history_archive_max_records = 500
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.evolution.auto_run = False
    return config


def test_runtime_history_archive_writes_file_and_purges_expired(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    archive_dir = Path(config.command_channel.history_archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    expired = archive_dir / "runtime_history_20260220.json"
    retained = archive_dir / "runtime_history_20260302.json"
    expired.write_text("{}", encoding="utf-8")
    retained.write_text("{}", encoding="utf-8")

    report = service.archive_runtime_history(
        now=datetime.fromisoformat("2026-03-05T15:30:00"),
        force=True,
    )
    assert report["archived"] is True
    assert _as_int(report["purged_count"]) >= 1
    assert str(expired) in _as_text_list(report["purged_files"])
    assert not expired.exists()
    assert retained.exists()

    output_path = Path(str(report["path"]))
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["archive_version"] == 1
    assert "summary" in payload
    assert "portfolio" in payload


def test_runtime_history_archive_keeps_pipeline_portfolio_executions(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._record_audit_event(  # noqa: SLF001
        event_type="pipeline_run",
        trace_id="trace-runtime-execution",
        payload={
            "portfolio_update": {
                "opened": 0,
                "skipped_max_holdings": 1,
                "status": "simulated_auto_applied",
                "executions": [
                    {
                        "trade_id": "SKIP-trace-000001-rejected_max_holdings",
                        "symbol": "000001",
                        "side": "buy",
                        "status": "rejected_max_holdings",
                        "quantity": 0,
                        "price": 10.1,
                    }
                ],
            }
        },
    )

    report = service.archive_runtime_history(
        now=datetime.fromisoformat("2026-03-05T15:30:00"),
        force=True,
    )
    payload = json.loads(Path(str(report["path"])).read_text(encoding="utf-8"))
    audit_events = payload["runtime"]["audit_events"]
    pipeline_event = next(
        item for item in audit_events if item["event_type"] == "pipeline_run"
    )
    portfolio_update = pipeline_event["payload"]["portfolio_update"]

    assert portfolio_update["skipped_max_holdings"] == 1
    assert portfolio_update["executions"][0]["status"] == "rejected_max_holdings"


def test_runtime_history_archive_if_needed_dedups_same_day(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    now = datetime.fromisoformat("2026-03-05T16:00:00")
    first = service.archive_runtime_history_if_needed(now=now)
    second = service.archive_runtime_history_if_needed(now=now)
    assert first["archived"] is True
    assert second["archived"] is False
    assert second["reason"] == "dedup"


def test_close_reconcile_job_contains_runtime_archive_payload(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    result = service._job_close_reconcile()  # noqa: SLF001
    assert "runtime_archive" in result
    archive_report = result["runtime_archive"]
    assert isinstance(archive_report, dict)
    if archive_report.get("archived") is True:
        archive_path = Path(str(archive_report.get("path", "")))
        assert archive_path.exists()
