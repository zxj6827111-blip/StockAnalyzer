from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from scripts.p1_run_nas_advisory_collection import run_collection

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_p1_advisory_collection_writes_dry_plan_without_pipeline_run(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        _ = payload
        calls.append((method, path))
        raise AssertionError("dry plan must not call the API")

    report = run_collection(
        api_base="http://127.0.0.1:18001",
        api_token="",
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        runs=2,
        interval_sec=0.0,
        output_dir=tmp_path / "collection",
        runtime_state_path=tmp_path / "runtime_state.json",
        config_path=REPO_ROOT / "config" / "default.yaml",
        model_artifact_path=tmp_path / "model_v1.json",
        confirm_run=False,
        http_request=_fake_request,
    )

    assert report["status"] == "dry_plan_no_pipeline_run"
    assert calls == []
    assert (tmp_path / "collection" / "p1_advisory_collection_report.md").exists()


def test_p1_advisory_collection_runs_multiple_advisory_probes(
    tmp_path: Path,
) -> None:
    runtime_state = tmp_path / "runtime_state.json"
    runtime_state.write_text(
        json.dumps(
            {
                "latest_signals": {
                    "trace_id": "trace-collection",
                    "timestamp": "2026-06-19T09:31:00",
                    "source": "pipeline_run",
                    "signals": [
                        {
                            "symbol": "600000",
                            "action": "watch",
                            "probabilities": {"lgbm": 0.26, "xgb": 0.25, "meta": 0.24},
                            "score": 24.0,
                            "decision_trace": {
                                "financial_gate": {
                                    "allowed": True,
                                    "roe": 0.08,
                                    "debt_ratio": 0.45,
                                    "financial_data_complete": True,
                                    "financial_missing_fields": "",
                                    "financial_source": "unit_test_financials",
                                    "financial_report_date": "2026-03-31",
                                },
                                "cross_review_gate": {"passed": False},
                            },
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    model_artifact = tmp_path / "model_v1.json"
    model_artifact.write_text(
        json.dumps(
            {
                "training_metrics": {"positive_rate": 0.1, "test_samples": 20},
                "metadata": {"test_samples": 20},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, str, Mapping[str, object] | None]] = []
    trace_counter = {"value": 0}

    def _fake_request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        calls.append((method, path, payload))
        if path == "/dashboard/ops/state":
            return {"advisory_only": True, "execution_mode": "advisory_only"}
        if path == "/run/pipeline":
            trace_counter["value"] += 1
            return {
                "trace_id": f"trace-collection-{trace_counter['value']}",
                "execution_mode": "advisory_only",
                "portfolio_update": {
                    "status": "skipped_advisory_only",
                    "executions": [],
                },
            }
        if path == "/signals/latest":
            return {
                "trace_id": "trace-collection",
                "source": "pipeline_run",
                "storage_source": "runtime_state",
                "signals": [
                    {
                        "symbol": "600000",
                        "action": "watch",
                        "probabilities": {"lgbm": 0.26, "xgb": 0.25, "meta": 0.24},
                        "score": 24.0,
                        "decision_trace": {
                            "financial_gate": {
                                "allowed": True,
                                "roe": 0.08,
                                "debt_ratio": 0.45,
                                "financial_data_complete": True,
                                "financial_missing_fields": "",
                                "financial_source": "unit_test_financials",
                                "financial_report_date": "2026-03-31",
                            },
                            "cross_review_gate": {"passed": False},
                        },
                    }
                ],
            }
        if path.startswith("/audit/events?"):
            return {
                "events": [
                    {
                        "event_id": "AUD-00000001",
                        "timestamp": "2026-06-19T09:31:00",
                        "event_type": "pipeline_run",
                        "trace_id": "trace-collection",
                        "payload": {
                            "execution_mode": "advisory_only",
                            "portfolio_update": {
                                "execution_attempts": {},
                                "advisory_attempts": {"signals": 1, "buy_signals": 0},
                                "executions": [],
                            },
                        },
                    }
                ]
            }
        if path == "/research/signal-quality/run":
            return {
                "status": "ok",
                "signal_source": "pipeline_run",
                "signal_storage_source": "runtime_state",
                "source_signal_count": 1,
                "signal_loss_funnel": {
                    "execution_attempts": {},
                    "advisory_attempts": {"signals": 1, "buy_signals": 0},
                    "dry_run_attempts": {},
                    "execution_stages": {},
                    "data_gaps": [],
                },
            }
        raise AssertionError(f"unexpected call: {method} {path}")

    report = run_collection(
        api_base="http://127.0.0.1:18001",
        api_token="",
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        runs=2,
        interval_sec=0.0,
        output_dir=tmp_path / "collection",
        runtime_state_path=runtime_state,
        config_path=REPO_ROOT / "config" / "default.yaml",
        model_artifact_path=model_artifact,
        confirm_run=True,
        http_request=_fake_request,
        sleep=lambda seconds: None,
    )

    assert report["status"] == "pass"
    assert report["completed_runs"] == 2
    assert report["summary"]["passed_runs"] == 2
    assert report["summary"]["financial_raw_fields_observed_runs"] == 2
    assert (tmp_path / "collection" / "run_001" / "nas_validation_report.md").exists()
    assert (tmp_path / "collection" / "run_002" / "nas_validation_report.json").exists()
    assert (tmp_path / "collection" / "p1_advisory_collection_report.json").exists()
    pipeline_calls = [item for item in calls if item[1] == "/run/pipeline"]
    assert len(pipeline_calls) == 2
