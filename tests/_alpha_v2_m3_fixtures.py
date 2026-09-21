"""M3 测试共享夹具：冻结清单 + epoch 开启的标准链条。

修复轮起，任何写快照 / 成熟 / 挂账 missing 的调用都要求磁盘上存在
与 epoch 锚定一致的 freeze manifest，且 epoch identity 覆盖全部 8 个
冻结键（含 ``execution_price_mode``）。本模块集中这套最小可信配置，
供全部 M3 测试文件复用——避免各自搭一份"绕开闸门的夹具"又悄悄回到旧路径。

R3 起：写入日由**真实墙钟**决定，生产清单默认不授权确定性时钟。测试要复现
"某天当天写入"，必须用 :func:`capture_at` 把墙上时钟固定到那一天，并让夹具清单
带上 ``deterministic_clock=True``（生产 CLI 恒为 false，所以这只是测试能力）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path

from stock_analyzer.alpha_v2.validation.epoch import EpochRecord, open_epoch
from stock_analyzer.alpha_v2.validation.freeze import (
    build_validation_freeze,
    write_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import frozen_wall_clock

M3_MODEL_BLOCK = {
    "model_id": "alpha_v2_shadow_epoch_001",
    "artifact_hash": "h" * 64,
    "artifact_created_at": "2026-09-18T12:00:00+08:00",
    "artifact_path": "unused",
    "status": "frozen",
}

M3_FEATURE_COLUMNS = ["ret_1d", "ma5"]

# 测试用的固定写入时刻（本地时区 21:45，与生产 capture 窗口同量级）。
CAPTURE_HOUR = 21
CAPTURE_MINUTE = 45


@contextmanager
def capture_at(day: date):
    """把墙上时钟固定到 ``day`` 当天（build_shadow_rows + write 都必须包在里面）。"""
    zone = datetime.now().astimezone().tzinfo
    with frozen_wall_clock(datetime.combine(day, time(CAPTURE_HOUR, CAPTURE_MINUTE), tzinfo=zone)):
        yield day


def write_freeze_manifest(
    root: str | Path,
    *,
    validation_epoch_id: str = "alpha_v2_epoch_001",
    execution_price_mode: str = "raw",
    validation_mode: str = "test",
    validation_start_date: str | None = "2026-09-01",
    deterministic_clock: bool = True,
    model: dict[str, object] | None = None,
    feature_columns: list[str] | None = None,
    **overrides: object,
) -> dict[str, object]:
    """写一份非空 schema 的生产 profile 清单（默认值对应"可开 clean OOS"）。

    ``validation_mode`` 默认 ``"test"``：与 production 同语义（可进 clean OOS），
    但允许用 :func:`capture_at` 把写入日固定到用例需要的那一天——**production 模式
    永久不接受固定时钟**（见 ``shadow_capture.capture_clock_policy``）。freeze CLI
    只产 production / rehearsal，因此这个模式在生产上不可达。
    """
    manifest = build_validation_freeze(
        validation_epoch_id=validation_epoch_id,
        code_commit=str(overrides.get("code_commit", "c" * 40)),
        git_branch="test",
        config_hash=str(overrides.get("config_hash", "cfg")),
        config_hash_scope="test",
        model=dict(model) if model is not None else M3_MODEL_BLOCK,
        feature_columns=list(feature_columns) if feature_columns is not None else M3_FEATURE_COLUMNS,
        feature_group_ids=["price_volume_technical"],
        selection_contract={"selection_contract_id": "night_alpha_v2_v1"},
        execution_price_mode=str(overrides.get("execution_price_mode", execution_price_mode)),
        feature_price_mode="qfq",
        validation_start_date=validation_start_date,
        created_at="2026-09-18T22:00:00+08:00",
        validation_mode=str(overrides.get("validation_mode", validation_mode)),
        deterministic_clock=bool(
            overrides.get("deterministic_clock", deterministic_clock)
        ),
        deterministic_clock_source="test_fixture",
    )
    write_validation_freeze(manifest, root=root)
    return manifest


def open_epoch_for_manifest(
    root: str | Path,
    manifest: dict[str, object],
    *,
    epoch_id: str = "alpha_v2_epoch_001",
    opened_on_date: str = "2026-09-01",
    extra_identity: dict[str, object] | None = None,
) -> EpochRecord:
    """按冻结清单内容派生 epoch identity（清单/epoch 从根上就一致）。"""
    model = dict(manifest.get("model", {}) or {})
    identity: dict[str, object] = {
        "code_commit": manifest["code_commit"],
        "config_hash": manifest["config_hash"],
        "model_id": model.get("model_id", ""),
        "model_artifact_hash": model.get("artifact_hash", ""),
        "feature_schema_hash": manifest["feature_schema_hash"],
        "label_policy_hash": manifest["label_policy_hash"],
        "selection_contract_id": manifest["selection_contract_id"],
        "execution_price_mode": manifest["execution_price_mode"],
    }
    if extra_identity:
        identity.update(extra_identity)
    return open_epoch(
        root=root,
        epoch_id=epoch_id,
        freeze_manifest_hash=str(manifest["freeze_manifest_hash"]),
        identity=identity,
        opened_on_date=opened_on_date,
    )


def shadow_row_identity(epoch: EpochRecord) -> dict[str, object]:
    """写入快照行需要的身份全集（含行级 7 键 + 快照数据点标识）。"""
    ident = dict(epoch.identity)
    ident.setdefault("universe_snapshot_id", "pit-test")
    ident.setdefault("data_snapshot_id", "db-test")
    ident.setdefault("model_created_at", "2026-09-18T12:00:00+08:00")
    return ident
