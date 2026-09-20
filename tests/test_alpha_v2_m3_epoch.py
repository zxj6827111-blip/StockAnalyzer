"""M3 Validation Epoch 注册表测试（单开/关闭纪律/身份对账）。"""

from __future__ import annotations

import pytest

from stock_analyzer.alpha_v2.validation.epoch import (
    EpochRegistryError,
    active_epoch,
    assert_epoch_open,
    close_epoch,
    epoch_identity_matches,
    get_epoch,
    load_epoch_registry,
    open_epoch,
    update_epoch_days,
)

_IDENTITY = {
    "code_commit": "abc",
    "config_hash": "cfg",
    "model_id": "m1",
    "model_artifact_hash": "h1",
    "feature_schema_hash": "fs",
    "label_policy_hash": "lp",
    "selection_contract_id": "night_alpha_v2_v1",
    "execution_price_mode": "raw",
}


def _open(tmp_path, epoch_id="alpha_v2_epoch_001"):
    return open_epoch(
        root=tmp_path,
        epoch_id=epoch_id,
        freeze_manifest_hash="f" * 64,
        identity=_IDENTITY,
        opened_on_date="2026-09-18",
        opened_at="2026-09-18T21:50:00+08:00",
    )


def test_open_epoch_persists_and_recovers(tmp_path):
    record = _open(tmp_path)
    assert record.status == "open"
    epochs, history = load_epoch_registry(tmp_path)
    assert record.epoch_id in epochs
    assert any(item["event"] == "epoch_opened" for item in history)
    assert active_epoch(tmp_path).epoch_id == record.epoch_id


def test_second_epoch_rejected_while_one_open(tmp_path):
    _open(tmp_path)
    with pytest.raises(EpochRegistryError, match="已有开放 epoch"):
        _open(tmp_path, epoch_id="alpha_v2_epoch_002")


def test_invalid_epoch_id_rejected(tmp_path):
    with pytest.raises(EpochRegistryError):
        _open(tmp_path, epoch_id="epoch1")


def test_close_requires_reason_and_single_use(tmp_path):
    _open(tmp_path)
    with pytest.raises(EpochRegistryError):
        close_epoch(root=tmp_path, epoch_id="alpha_v2_epoch_001", reason="")
    closed = close_epoch(
        root=tmp_path,
        epoch_id="alpha_v2_epoch_001",
        reason="B3 类缺陷修复完成后重新验收",
        closed_at="2026-10-01T00:00:00+08:00",
    )
    assert closed.status == "closed"
    with pytest.raises(EpochRegistryError, match="已关闭"):
        close_epoch(root=tmp_path, epoch_id="alpha_v2_epoch_001", reason="again")
    with pytest.raises(EpochRegistryError):
        assert_epoch_open(closed)


def test_reopen_same_id_rejected(tmp_path):
    _open(tmp_path)
    close_epoch(root=tmp_path, epoch_id="alpha_v2_epoch_001", reason="迁移到 alpha_v2_epoch_002")
    with pytest.raises(EpochRegistryError, match="复用"):
        _open(tmp_path)


def test_epoch_identity_reconciliation(tmp_path):
    record = _open(tmp_path)
    assert epoch_identity_matches(record, _IDENTITY) == []
    drift = dict(_IDENTITY, model_artifact_hash="different")
    violations = epoch_identity_matches(record, drift)
    assert any("model_artifact_hash" in item for item in violations)


def test_update_epoch_days(tmp_path):
    _open(tmp_path)
    updated = update_epoch_days(
        root=tmp_path, epoch_id="alpha_v2_epoch_001", days={"mature_dates_5d": 3}
    )
    assert updated.days["mature_dates_5d"] == 3
    fetched = get_epoch(tmp_path, "alpha_v2_epoch_001")
    assert fetched is not None and fetched.days["mature_dates_5d"] == 3
