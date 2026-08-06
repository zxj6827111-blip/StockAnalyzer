from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any, cast
from unittest import mock

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.training.artifact_path = str(tmp_path / "model.json")
    config.evolution.auto_run = False
    config.cloud_backup.enabled = False
    config.market_warehouse.auto_run = False
    config.tdx_sync.auto_run = False
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    return config


def _make_service(
    tmp_path: Path,
) -> tuple[StockAnalyzerService, mock.Mock]:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    notify_recorder = mock.Mock()
    _patch_attr(service, "notify", notify_recorder)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    return service, notify_recorder


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _corrupt_text() -> str:
    return "not a duckdb file"


def _learning_protocol_db_path(service: StockAnalyzerService) -> Path:
    return Path(service._training_bootstrap_state_path).parent / "learning_protocol.duckdb"


def _backup_files(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob("learning_protocol.corrupt.*.duckdb"))


def test_recover_corrupt_learning_protocol_renames_and_rebuilds(tmp_path: Path) -> None:
    service, notify_recorder = _make_service(tmp_path)
    db_path = _learning_protocol_db_path(service)
    db_path.write_text(_corrupt_text(), encoding="utf-8")

    result = service._recover_corrupt_learning_protocol(
        error_text="SerializationException: corrupt database"
    )

    assert result["recovered"] is True
    backup_path = Path(cast(str, result["backup_path"]))
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == _corrupt_text()
    assert backup_path.name.startswith("learning_protocol.corrupt.")
    assert backup_path.name.endswith(".duckdb")

    rebuilt = SampleStore(db_path=db_path)
    counts = rebuilt.counts()
    assert counts == {
        "signal_snapshots": 0,
        "outcome_records": 0,
        "dataset_manifests": 0,
    }

    state = dict(service._training_bootstrap_state)
    assert state["completed"] is False
    assert state["last_status"] == "db_recovered"
    assert state["last_error"] == (
        "learning_protocol_recovered:SerializationException: corrupt database"
    )
    assert state["last_recovery_at"]

    persisted = json.loads(
        Path(service._training_bootstrap_state_path).read_text(encoding="utf-8")
    )
    assert persisted["last_status"] == "db_recovered"
    assert persisted["last_recovery_at"] == state["last_recovery_at"]

    assert notify_recorder.call_count == 1
    notify_call = notify_recorder.call_args.kwargs
    assert "learning protocol db recovered" in str(notify_call["title"])
    assert notify_call["level"] == "warn"


def test_recover_corrupt_learning_protocol_backup_collision_adds_suffix(
    tmp_path: Path,
) -> None:
    service, _notify_recorder = _make_service(tmp_path)
    db_path = _learning_protocol_db_path(service)
    db_path.write_text(_corrupt_text(), encoding="utf-8")

    first = service._recover_corrupt_learning_protocol(error_text="SerializationException: 1")
    assert first["recovered"] is True
    first_backup = Path(cast(str, first["backup_path"]))
    first_backup.write_text("already claimed", encoding="utf-8")

    db_path.write_text(_corrupt_text(), encoding="utf-8")
    second = service._recover_corrupt_learning_protocol(error_text="SerializationException: 2")

    assert second["recovered"] is True
    second_backup = Path(cast(str, second["backup_path"]))
    assert second_backup.exists()
    assert second_backup != first_backup
    assert second_backup.read_text(encoding="utf-8") == _corrupt_text()
    assert len(_backup_files(db_path)) == 2


def test_recover_corrupt_learning_protocol_db_missing(tmp_path: Path) -> None:
    service, _notify_recorder = _make_service(tmp_path)
    db_path = _learning_protocol_db_path(service)
    assert not db_path.exists()

    result = service._recover_corrupt_learning_protocol(error_text="SerializationException: x")

    assert result == {"recovered": False, "reason": "db_missing"}
    assert not _backup_files(db_path)


def test_recover_corrupt_learning_protocol_failure_returns_reason(tmp_path: Path) -> None:
    service, _notify_recorder = _make_service(tmp_path)
    db_path = _learning_protocol_db_path(service)
    db_path.write_text(_corrupt_text(), encoding="utf-8")

    with mock.patch(
        "stock_analyzer.runtime.service.shutil.move",
        side_effect=OSError("permission denied"),
    ):
        result = service._recover_corrupt_learning_protocol(error_text="SerializationException: x")

    assert result["recovered"] is False
    assert "permission denied" in str(result["reason"])
    assert db_path.exists()
    assert not _backup_files(db_path)


def test_learning_protocol_exception_recovers_corrupt_db_in_except_block(
    tmp_path: Path,
) -> None:
    service, _notify_recorder = _make_service(tmp_path)
    db_path = _learning_protocol_db_path(service)
    db_path.write_text(_corrupt_text(), encoding="utf-8")

    fake_exception = type("SerializationException", (RuntimeError,), {})(
        "Failed to deserialize the database"
    )
    with mock.patch.object(
        service._label_policy_registry,
        "register_from_config",
        side_effect=fake_exception,
    ):
        result = service._try_train_models_from_learning_protocol(
            trainer=mock.Mock(),
            symbols=[],
            lookback_days=600,
            artifact_path=None,
        )

    assert result["ok"] is False
    assert result["errors"] == ["learning_protocol_failed:SerializationException"]
    assert result["db_recovered"] is True
    assert result["fallback_reason"] == "learning_protocol_db_corrupted_recovered"
    assert len(_backup_files(db_path)) == 1
    assert SampleStore(db_path=db_path).counts()["signal_snapshots"] == 0
    assert service._training_bootstrap_state["last_status"] == "db_recovered"


def test_learning_protocol_exception_non_recoverable_keeps_original_payload(
    tmp_path: Path,
) -> None:
    service, _notify_recorder = _make_service(tmp_path)
    with mock.patch.object(
        service._label_policy_registry,
        "register_from_config",
        side_effect=ValueError("boom"),
    ):
        result = service._try_train_models_from_learning_protocol(
            trainer=mock.Mock(),
            symbols=[],
            lookback_days=600,
            artifact_path=None,
        )

    assert result["ok"] is False
    assert result["errors"] == ["learning_protocol_failed:ValueError"]
    assert "db_recovered" not in result
    assert result["fallback_reason"] == "learning_protocol_exception:boom"
    assert service._training_bootstrap_state["last_status"] != "db_recovered"
