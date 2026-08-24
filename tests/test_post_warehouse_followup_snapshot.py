"""Tests for the phase-D scheduling closure in scripts/run_post_warehouse_followup.py.

Covers the new step wiring: incremental feature snapshot before the data
preflight, preflight gating of the week5 scan, and the non-fatal semantics of
snapshot build failures.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
_FOLLOWUP_PATH = ROOT / "scripts" / "run_post_warehouse_followup.py"


def _load_followup() -> object:
    module_name = "run_post_warehouse_followup"
    spec = importlib.util.spec_from_file_location(module_name, _FOLLOWUP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def followup() -> object:
    return _load_followup()


class _FakeService:
    """Records calls so the main() orchestration is observable."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.train_kwargs: dict[str, object] = {}

    def run_week5_scan(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("run_week5_scan")
        return {"ok": True, "signals": [{"symbol": "600519"}]}

    def repair_learning_backfill(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("repair_learning_backfill")
        return {"ok": True, "repaired_snapshot_count": 0, "promoted_label_matured": 0,
                "promoted_fully_matured": 0}

    def build_learning_trainable_manifest(self) -> dict[str, object]:
        self.calls.append("build_learning_trainable_manifest")
        return {"ok": True, "dataset_manifest_id": "manifest-1",
                "included_snapshot_count": 1, "included_outcome_count": 1}

    def train_learning_manifest(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("train_learning_manifest")
        self.train_kwargs = dict(kwargs)
        return {"ok": True, "artifact_path": "/tmp/model.bin", "predictor_loaded": False,
                "model_registry": {"registered": True, "model_id": "model-1"}}

    def build_phase_d_tabular_deep_report(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("build_phase_d_tabular_deep_report")
        return {"ok": True, "output_path": "/tmp/tabular_deep.json"}


@pytest.fixture()
def fake_main_env(monkeypatch: pytest.MonkeyPatch, followup: object) -> _FakeService:
    """Patch the followup module so main() runs without config/artifacts side effects."""
    fake = _FakeService()
    monkeypatch.setattr(followup, "_write_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(followup, "_write_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(followup, "_pending_snapshot_ids", lambda service: [])
    monkeypatch.setattr(followup, "get_config", lambda: SimpleNamespace(data_source=object()))
    monkeypatch.setattr(followup, "StockAnalyzerService", lambda config: fake)
    return fake


def test_main_step_order_feature_snapshot_preflight_then_scan(
    followup: object,
    fake_main_env: _FakeService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_service = fake_main_env
    monkeypatch.setattr(
        followup,
        "_run_feature_snapshot_step",
        lambda service, config: {"ok": True, "skipped": True, "status": "skipped",
                                 "data_snapshot_id": "snap_x", "symbol_count": 0,
                                 "errors": []},
    )
    monkeypatch.setattr(
        followup,
        "_run_data_preflight",
        lambda service, now: {"status": "ok", "reasons": [],
                              "data_snapshot_id": "snap_x", "snapshot_current": True},
    )
    assert followup.main() == 0
    steps = _steps_from_json(capsys.readouterr().out)
    step_keys = list(steps)
    assert step_keys.index("build_feature_snapshot") < step_keys.index("data_preflight")
    assert step_keys.index("data_preflight") < step_keys.index("week5_scan")
    assert fake_service.calls[0] == "run_week5_scan"
    assert fake_service.train_kwargs["load_predictor"] is False


def test_main_preflight_blocked_skips_scan(
    followup: object,
    fake_main_env: _FakeService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        followup,
        "_run_feature_snapshot_step",
        lambda service, config: {"ok": True, "skipped": False, "status": "completed",
                                 "data_snapshot_id": "snap_new", "symbol_count": 3,
                                 "errors": []},
    )
    monkeypatch.setattr(
        followup,
        "_run_data_preflight",
        lambda service, now: {"status": "blocked", "reasons": ["feature_snapshot_stale",
                                                               "provider_synthetic"],
                              "data_snapshot_id": "snap_new", "snapshot_current": False},
    )
    assert followup.main() == 0
    assert "run_week5_scan" not in fake_main_env.calls
    out = capsys.readouterr().out
    steps = _steps_from_json(out)
    scan_step = steps["week5_scan"]
    assert scan_step["skipped"] is True
    assert scan_step["reason"] == "skipped: data_preflight_blocked"
    assert scan_step["data_preflight_reasons"] == ["feature_snapshot_stale",
                                                   "provider_synthetic"]
    step_keys = list(steps)
    assert step_keys.index("build_feature_snapshot") < step_keys.index("data_preflight")
    assert step_keys.index("data_preflight") < step_keys.index("week5_scan")


def _steps_from_json(output: str) -> dict[str, object]:
    import json

    payload = json.loads(output)
    return dict(payload["steps"])


def test_feature_snapshot_step_no_rows_marks_skipped_warning(
    followup: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Mock()
    provider.list_symbols.side_effect = AttributeError("no list_symbols")
    monkeypatch.setattr(followup, "build_runtime_provider", lambda config: provider)
    monkeypatch.setattr(
        followup,
        "build_feature_snapshot",
        lambda *args, **kwargs: {"ok": False, "skipped": False, "errors": ["no_rows"]},
    )
    payload = followup._run_feature_snapshot_step(Mock(), Mock())
    assert payload["ok"] is False
    assert payload["status"] == "skipped_warning"
    assert payload["errors"] == ["no_rows"]
    assert payload["data_snapshot_id"] == ""
    assert payload["symbol_count"] == 0


def test_feature_snapshot_step_exception_is_non_fatal(
    followup: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Mock()
    monkeypatch.setattr(followup, "build_runtime_provider", lambda config: provider)

    def _boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("warehouse locked")

    monkeypatch.setattr(followup, "build_feature_snapshot", _boom)
    payload = followup._run_feature_snapshot_step(Mock(), Mock())
    assert payload["ok"] is False
    assert payload["status"] == "fail"
    assert payload["error"]["message"] == "warehouse locked"


def test_data_preflight_maps_gate_status_and_kwargs(
    followup: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    config = Mock()
    service = Mock()
    service._config = config
    monkeypatch.setattr(followup, "load_feature_snapshot", lambda cfg: (None, None))
    monkeypatch.setattr(followup, "snapshot_is_current", lambda *args: False)
    gate = Mock()
    gate.get.side_effect = lambda key, default=None: {
        "status": "blocked",
        "reasons": ["feature_snapshot_stale", "data_stale:5d"],
    }.get(key, default)
    service._build_data_gate = Mock(return_value=gate)

    payload = followup._run_data_preflight(service, now)
    assert payload["status"] == "blocked"
    assert payload["reasons"] == ["feature_snapshot_stale", "data_stale:5d"]
    assert payload["snapshot_current"] is False
    service._build_data_gate.assert_called_once_with(
        snapshot_manifest=None,
        snapshot_current=False,
        latest_trade_date="",
        now=now,
    )


def test_data_preflight_gate_error_blocks(
    followup: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Mock()
    service = Mock()
    service._config = config
    monkeypatch.setattr(followup, "load_feature_snapshot", lambda cfg: (None, None))
    monkeypatch.setattr(followup, "snapshot_is_current", lambda *args: False)

    def _boom(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("gate exploded")

    service._build_data_gate = Mock(side_effect=_boom)
    payload = followup._run_data_preflight(service, datetime.now(UTC))
    assert payload["status"] == "blocked"
    assert payload["reasons"] == ["data_preflight_error"]
