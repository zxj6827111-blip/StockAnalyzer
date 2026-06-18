from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts.p0_run_nas_advisory_probe import ProbeError, run_probe


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
        http_request=_fake_request,
    )

    assert result["status"] == "pass"
    assert (tmp_path / "out" / "commands" / "pipeline_advisory.json").exists()
    assert (tmp_path / "out" / "nas_advisory_validation_report.md").exists()
    pipeline_call = [item for item in calls if item[1] == "/run/pipeline"][0]
    assert pipeline_call[2] == {
        "symbols": ["600000"],
        "strategy": "trend",
        "current_equity": 1.0,
        "use_live_runtime": False,
        "dry_run_execution": False,
        "notify_enabled": False,
    }
