"""learning_protocol 损坏恢复的误判防线（2026-09-12 生产事故回归）。

事故：backfill 持有 duckdb 写锁期间，并发训练请求的 IOException（"Could
not set lock ..."）被当作库损坏，恢复流程把 1.27GB 的 learning_protocol
.duckdb 改名重建。本文件钉死两条行为：
1. 锁冲突（IOException lock 文本）绝不触发恢复；
2. 真损坏文本仍触发恢复（原有 kill-9 恢复能力保留）。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from stock_analyzer.runtime.service import (
    StockAnalyzerService,
    _is_likely_learning_protocol_corruption,
)
from tests.test_service_learning_governance import (
    _load_test_config,
    _new_service,
)

_LOCK_TEXT = (
    'IO Error: Could not set lock on file "learning_protocol.duckdb": '
    "Conflicting lock is held in another process"
)
_CORRUPT_TEXT = 'IO Error: Corrupted database file: "learning_protocol.duckdb"'


@pytest.mark.parametrize(
    ("error_text", "expected"),
    [
        (_LOCK_TEXT, False),
        ("IO Error: database is locked", False),
        ("FATAL: could not set lock on file", False),
        (_CORRUPT_TEXT, True),
        ("Invalid Input Error: no magic bytes found", True),
        ("checksum mismatch in block 3", True),
        # 版本不匹配不自动重建——重建只会静默丢数据。
        (
            "Trying to read a database file with version number 64, "
            "but we can only read version 51",
            False,
        ),
    ],
)
def test_corruption_classifier(error_text: str, expected: bool) -> None:
    assert _is_likely_learning_protocol_corruption(error_text) is expected


def _prepare(tmp_path: Path) -> StockAnalyzerService:
    config = _load_test_config(tmp_path)
    return _new_service(config)


def _corrupt_backup_count(tmp_path: Path) -> int:
    bootstrap_state = Path(str(_load_test_config(tmp_path).training.bootstrap_state_path))
    return len(list(bootstrap_state.parent.glob("learning_protocol.corrupt.*")))


def test_lock_conflict_never_triggers_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _prepare(tmp_path)
    backups_before = _corrupt_backup_count(tmp_path)

    def _raise_lock(*args: object, **kwargs: object) -> list[object]:
        raise duckdb.IOException(_LOCK_TEXT)

    monkeypatch.setattr(service._sample_store, "list_snapshots", _raise_lock)
    result = service._try_train_models_from_learning_protocol(
        trainer=service._build_model_trainer(),
        symbols=["600000"],
        lookback_days=30,
        artifact_path=str(tmp_path / "model.json"),
    )
    assert result["ok"] is False
    assert result.get("db_recovered") is None
    assert "learning_protocol_db_corrupted_recovered" != str(result.get("fallback_reason"))
    assert _corrupt_backup_count(tmp_path) == backups_before


def test_real_corruption_still_triggers_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _prepare(tmp_path)
    db_path = Path(str(service._config.training.bootstrap_state_path)).parent / (
        "learning_protocol.duckdb"
    )
    # 造一个真实存在的库文件，供恢复流程改名。
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.close()

    def _raise_corrupt(*args: object, **kwargs: object) -> list[object]:
        raise duckdb.IOException(_CORRUPT_TEXT)

    monkeypatch.setattr(service._sample_store, "list_snapshots", _raise_corrupt)
    result = service._try_train_models_from_learning_protocol(
        trainer=service._build_model_trainer(),
        symbols=["600000"],
        lookback_days=30,
        artifact_path=str(tmp_path / "model.json"),
    )
    assert result["ok"] is False
    assert result.get("db_recovered") is True
    assert result.get("fallback_reason") == "learning_protocol_db_corrupted_recovered"
    # 原库被改名进备份，新库文件已重建。
    backups = list(db_path.parent.glob("learning_protocol.corrupt.*"))
    assert backups, "real corruption should leave a renamed backup"
    assert db_path.exists()
