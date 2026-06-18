from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts.p0_run_nas_advisory_probe import ProbeError, run_probe

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_nas_advisory_probe_refuses_non_advisory_state(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        _ = payload
        calls.append((method, path))
        return {"advisory_only": False, "execution_mode": "portfolio_auto_apply"}

    with pytest.raises(ProbeError, match="advisory_only is not true"):
        run_probe(
            api_base="http://127.0.0.1:18001",
            api_token="",
            symbols=["600000"],
            strategy="trend",
            current_equity=1.0,
            output_dir=tmp_path / "out",
            runtime_state_path=tmp_path / "runtime_state.json",
            confirm_run=True,
            http_request=_fake_request,
        )

    assert calls == [("GET", "/dashboard/ops/state")]


def test_run_nas_advisory_probe_captures_and_validates_evidence(tmp_path: Path) -> None:
    runtime_state = tmp_path / "runtime_state.json"
    runtime_state.write_text(
        json.dumps(
            {
                "latest_signals": {
                    "trace_id": "trace-advisory",
                    "timestamp": "2026-06-19T09:31:00",
                    "source": "pipeline_run",
                    "signals": [{"symbol": "600000", "action": "watch"}],
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

    def _fake_request(
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        calls.append((method, path, payload))
        if path == "/dashboard/ops/state":
            return {"advisory_only": True, "execution_mode": "advisory_only"}
        if path == "/run/pipeline":
            return {"trace_id": "trace-advisory", "execution_mode": "advisory_only"}
        if path == "/signals/latest":
            return {
                "trace_id": "trace-advisory",
                "source": "pipeline_run",
                "storage_source": "runtime_state",
                "signals": [{"symbol": "600000", "action": "watch"}],
            }
        if path.startswith("/audit/events?"):
            return {
                "events": [
                    {
                        "event_id": "AUD-00000001",
                        "timestamp": "2026-06-19T09:31:00",
                        "event_type": "pipeline_run",
                        "trace_id": "trace-advisory",
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

    result = run_probe(
        api_base="http://127.0.0.1:18001",
        api_token="",
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        output_dir=tmp_path / "out",
        runtime_state_path=runtime_state,
        confirm_run=True,
        config_path=REPO_ROOT / "config" / "default.yaml",
        model_artifact_path=model_artifact,
        http_request=_fake_request,
    )

    assert result["status"] == "pass"
    assert (tmp_path / "out" / "commands" / "pipeline_advisory.json").exists()
    assert (tmp_path / "out" / "nas_advisory_validation_report.md").exists()
    analysis = result["analysis"]
    assert isinstance(analysis, dict)
    assert analysis["status"] == "generated"
    assert analysis["remaining_expected_inputs"] == []
    analysis_dir = tmp_path / "out" / "analysis"
    assert (analysis_dir / "final_report_v3.json").exists()
    assert (analysis_dir / "p4_feature_family_ablation_v1.json").exists()
    assert (analysis_dir / "p5_position" / "position_framework_analysis.json").exists()
    assert (analysis_dir / "p0_shadow_experiment_plan_v1.json").exists()
    assert result["goal_completion_status"] == "complete"
    assert (tmp_path / "out" / "p0_goal_completion_audit.json").exists()
    assert (tmp_path / "out" / "p0_goal_completion_audit.md").exists()
    assert str(result["goal_completion_markdown"]).endswith("p0_goal_completion_audit.md")
    pipeline_call = [item for item in calls if item[1] == "/run/pipeline"][0]
    assert pipeline_call[2] == {
        "symbols": ["600000"],
        "strategy": "trend",
        "current_equity": 1.0,
        "use_live_runtime": False,
        "dry_run_execution": False,
        "notify_enabled": False,
    }
