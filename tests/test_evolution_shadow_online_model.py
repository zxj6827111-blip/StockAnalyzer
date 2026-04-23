from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.evolution.modules.shadow_online_model import run_shadow_online_model
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


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


def _make_orchestrator(tmp_path: Path) -> OffhoursEvolutionOrchestrator:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.evolution.code_commit_id = "git:test"
    config.evolution.strict_dependency_check = False
    return OffhoursEvolutionOrchestrator(
        config=config.evolution,
        project_root=tmp_path,
    )


def _write_shadow_online_labels(tmp_path: Path) -> Path:
    label_path = tmp_path / "artifacts" / "evolution" / "m5_shadow_online_labels.jsonl"
    label_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.write_text(
        "\n".join(
            [
                '{"symbol":"600000.SH","trade_date":"2026-03-01","label_mature_time":"2026-03-02T15:00:00","open":10.0,"high":10.3,"low":9.9,"close":10.2,"volume":1500000,"label":1,"p_meta":0.45}',
                '{"symbol":"000001.SZ","trade_date":"2026-03-01","label_mature_time":"2026-03-02T15:00:00","open":10.0,"high":10.1,"low":9.6,"close":9.7,"volume":1200000,"label":0,"p_meta":0.60}',
                '{"symbol":"300001.SZ","trade_date":"2026-03-02","label_mature_time":"2026-03-03T15:00:00","open":9.8,"high":10.2,"low":9.7,"close":10.1,"volume":980000,"label":1,"p_meta":0.48}',
                '{"symbol":"002594.SZ","trade_date":"2026-03-02","label_mature_time":"2026-03-03T15:00:00","open":11.0,"high":11.1,"low":10.5,"close":10.6,"volume":1050000,"label":0,"p_meta":0.58}',
                '{"symbol":"688981.SH","trade_date":"2026-03-03","label_mature_time":"2026-03-04T15:00:00","open":12.0,"high":12.4,"low":11.9,"close":12.2,"volume":880000,"label":1,"p_meta":0.47}',
            ]
        ),
        encoding="utf-8",
    )
    return label_path


def test_shadow_online_model_updates_with_matured_samples() -> None:
    records = [
        {
            "symbol": "600000.SH",
            "trade_date": "2026-03-01",
            "label_mature_time": "2026-03-02T15:00:00",
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.2,
            "volume": 1_500_000,
            "label": 1,
            "p_meta": 0.45,
        },
        {
            "symbol": "000001.SZ",
            "trade_date": "2026-03-01",
            "label_mature_time": "2026-03-02T15:00:00",
            "open": 10.0,
            "high": 10.1,
            "low": 9.6,
            "close": 9.7,
            "volume": 1_200_000,
            "label": 0,
            "p_meta": 0.60,
        },
        {
            "symbol": "300001.SZ",
            "trade_date": "2026-03-02",
            "label_mature_time": "2026-03-03T15:00:00",
            "open": 9.8,
            "high": 10.2,
            "low": 9.7,
            "close": 10.1,
            "volume": 980_000,
            "label": 1,
            "p_meta": 0.48,
        },
        {
            "symbol": "002594.SZ",
            "trade_date": "2026-03-02",
            "label_mature_time": "2026-03-03T15:00:00",
            "open": 11.0,
            "high": 11.1,
            "low": 10.5,
            "close": 10.6,
            "volume": 1_050_000,
            "label": 0,
            "p_meta": 0.58,
        },
    ]

    result = run_shadow_online_model(
        records=records,
        now=datetime(2026, 3, 4, 20, 40, tzinfo=UTC),
        previous_state=None,
        max_samples=10,
        min_samples=3,
        learning_rate=0.2,
        preview_limit=2,
    )

    assert result.status == "updated"
    assert result.shadow_mode is True
    assert result.affects_main_model is False
    assert result.metrics.valid_samples == 4
    assert result.metrics.updates_applied == 4
    assert len(result.preview) == 2
    assert any(str(reason).startswith("comparison:") for reason in result.reasons)


def test_shadow_online_model_skips_unmatured_samples() -> None:
    records = [
        {
            "symbol": "600000.SH",
            "trade_date": "2026-03-03",
            "label_mature_time": "2026-03-06T15:00:00",
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "volume": 1000,
            "label": 1,
        },
        {
            "symbol": "000001.SZ",
            "trade_date": "2026-03-01",
            "label_mature_time": "2026-03-02T15:00:00",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1200,
            "label": 1,
        },
    ]

    result = run_shadow_online_model(
        records=records,
        now=datetime(2026, 3, 4, 20, 40, tzinfo=UTC),
        previous_state=None,
        max_samples=10,
        min_samples=1,
        learning_rate=0.2,
        preview_limit=5,
    )

    assert result.status == "updated"
    assert result.samples_considered == 1
    assert result.samples_used == 1
    assert result.preview[0]["symbol"] == "000001.SZ"


def test_orchestrator_includes_shadow_online_model_report(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    label_path = _write_shadow_online_labels(tmp_path)
    orchestrator._config.m5_label_records_path = str(label_path.relative_to(tmp_path))

    report = orchestrator.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
                "p_meta": 0.51,
            }
        ],
        now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="shadow-online-test",
    )

    modules = _as_mapping(report["modules"])
    shadow = _as_mapping(modules["shadow_online_model"])
    shadow_v2 = _as_mapping(modules["shadow_online_model_v2"])
    assert shadow["shadow_mode"] is True
    assert shadow["affects_main_model"] is False
    assert shadow["status"] == "updated"
    assert str(shadow["artifact_uri"]).startswith("suggestions/shadow_online/")
    assert (tmp_path / str(shadow["artifact_uri"])).exists() is True
    shadow_online_report = _as_mapping(report["shadow_online_report"])
    assert shadow_online_report["artifact_uri"] == shadow["artifact_uri"]
    assert shadow_v2["shadow_mode"] is True
    assert shadow_v2["affects_main_model"] is False
    assert shadow_v2["status"] == "updated"
    assert str(shadow_v2["artifact_uri"]).startswith("suggestions/shadow_online/shadow_online_v2_")
    assert (tmp_path / str(shadow_v2["artifact_uri"])).exists() is True
    shadow_online_v2_report = _as_mapping(report["shadow_online_v2_report"])
    assert shadow_online_v2_report["artifact_uri"] == shadow_v2["artifact_uri"]

    shadow_payload = json.loads(
        (tmp_path / str(shadow["artifact_uri"])).read_text(encoding="utf-8")
    )
    report_payload = _as_mapping(shadow_payload["report"])
    assert report_payload["shadow_mode"] is True
    assert report_payload["affects_main_model"] is False
    metrics = _as_mapping(report_payload["metrics"])
    assert _as_int(metrics["valid_samples"]) >= 5

    shadow_v2_payload = json.loads(
        (tmp_path / str(shadow_v2["artifact_uri"])).read_text(encoding="utf-8")
    )
    report_v2_payload = _as_mapping(shadow_v2_payload["report"])
    assert report_v2_payload["shadow_mode"] is True
    assert report_v2_payload["affects_main_model"] is False
    metrics_v2 = _as_mapping(report_v2_payload["metrics"])
    assert _as_int(metrics_v2["valid_samples"]) >= 5
    assert "avg_execution_fill_ratio" in metrics_v2
    assert "signal_divergence_ratio" in metrics_v2

    v2_state_path = orchestrator._shadow_online_v2_state_path
    v2_metrics_path = orchestrator._shadow_online_v2_metrics_path
    assert v2_state_path.exists() is True
    assert v2_metrics_path.exists() is True

    v2_state_payload = json.loads(v2_state_path.read_text(encoding="utf-8"))
    assert v2_state_payload["engine"] == "protocol_shadow_online_v2_lr"
    assert _as_mapping(v2_state_payload["state"]) != {}

    v2_metric_records = [
        json.loads(line)
        for line in v2_metrics_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(v2_metric_records) >= 1
    assert v2_metric_records[-1]["engine"] == "protocol_shadow_online_v2_lr"
    assert v2_metric_records[-1]["status"] == "updated"


def test_orchestrator_shadow_online_v2_reuses_state_across_runs(tmp_path: Path) -> None:
    label_path = _write_shadow_online_labels(tmp_path)

    first = _make_orchestrator(tmp_path)
    first._config.m5_label_records_path = str(label_path.relative_to(tmp_path))
    first.run(
        records=[
            {
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "close": 10.1,
                "volume": 2_000_000,
                "p_meta": 0.51,
            }
        ],
        now=datetime(2026, 3, 5, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="shadow-online-run-1",
    )
    first_state_payload = json.loads(
        first._shadow_online_v2_state_path.read_text(encoding="utf-8")
    )
    first_updates = _as_int(_as_mapping(first_state_payload["state"])["cumulative_updates"])

    second = _make_orchestrator(tmp_path)
    second._config.m5_label_records_path = str(label_path.relative_to(tmp_path))
    report = second.run(
        records=[
            {
                "symbol": "000001.SZ",
                "open": 10.1,
                "high": 10.4,
                "low": 10.0,
                "close": 10.2,
                "volume": 1_800_000,
                "p_meta": 0.49,
            }
        ],
        now=datetime(2026, 3, 6, 20, 45, tzinfo=UTC),
        dry_run=True,
        source_trace_id="shadow-online-run-2",
    )

    modules = _as_mapping(report["modules"])
    assert _as_mapping(modules["shadow_online_model"])["status"] == "updated"
    assert _as_mapping(modules["shadow_online_model_v2"])["status"] == "updated"

    second_state_payload = json.loads(
        second._shadow_online_v2_state_path.read_text(encoding="utf-8")
    )
    second_updates = _as_int(_as_mapping(second_state_payload["state"])["cumulative_updates"])
    assert second_updates >= first_updates

    v2_metric_records = [
        json.loads(line)
        for line in second._shadow_online_v2_metrics_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(v2_metric_records) >= 2
    assert v2_metric_records[-1]["metadata"]["source_trace_id"] == "shadow-online-run-2"
