"""Week5 Phase 0 整改修复的回归测试。

覆盖范围（对应 docs/week5_phase0_audit_20260831.md 的 B1/B2/B3/B5/B6 与差异报告）：
- next_tradable_open 入场语义（soup 路径标签 + outcome 指标）；
- LabelsConfig 默认值（soft_label + next_tradable_open）与 default.yaml 一致性；
- OutcomeRecord.conflict_flag 持久化 + 旧库迁移 + MFE/MAE 冲突判定；
- model_id v3 内容绑定（uri 无关）+ champion 内容身份强制 + hash 物化；
- build_label_policy_diff_report 新旧标签差异统计。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml

from stock_analyzer.config import LabelsConfig
from stock_analyzer.labels.soup import build_soup_labels
from stock_analyzer.learning.backfill import _compute_outcome_metrics_for_row
from stock_analyzer.learning.label_policy_registry import build_label_policy_record
from stock_analyzer.learning.sample_schema import MaturityStatus, OutcomeRecord
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.bundle import compute_artifact_file_hash
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRegistry,
    ModelRole,
    build_model_registry_record_from_artifact,
)
from stock_analyzer.models.trainer import build_label_policy_diff_report

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# B1：next_tradable_open 入场语义
# ---------------------------------------------------------------------------


def _bars(rows: list[dict[str, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [datetime(2026, 8, 3 + offset) for offset in range(len(rows))], name="date"
    )
    return pd.DataFrame(rows, index=index)


def test_soup_next_tradable_open_entry_semantics() -> None:
    # T 日决策；T+1 开盘 10.0 入场；入场日高点命中 TP（+5%）→ 标签 1。
    bars = _bars(
        [
            {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0},
            {"open": 10.0, "high": 10.6, "low": 9.9, "close": 10.4},
            {"open": 10.4, "high": 10.8, "low": 10.1, "close": 10.6},
        ]
    )
    labels = build_soup_labels(
        bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
        price_basis="next_tradable_open",
        conflict_policy="soft_label",
    )
    assert labels.iloc[0] == 1.0


def test_soup_next_tradable_open_conflict_soft_label() -> None:
    # 入场日同时命中 TP 与 SL → 显式 soft label 0.5（v2 契约），非分支顺序判定。
    bars = _bars(
        [
            {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0},
            {"open": 10.0, "high": 10.7, "low": 9.3, "close": 10.1},
            {"open": 10.1, "high": 10.4, "low": 9.9, "close": 10.2},
        ]
    )
    labels = build_soup_labels(
        bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
        price_basis="next_tradable_open",
        conflict_policy="soft_label",
        conflict_soft_label_value=0.5,
    )
    assert labels.iloc[0] == 0.5


def test_soup_next_tradable_open_skips_suspended() -> None:
    # T+1 停牌 → 顺延到 T+2 以其开盘价入场。
    bars = _bars(
        [
            {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "suspended": False},
            {
                "open": 11.0,
                "high": 11.5,
                "low": 10.9,
                "close": 11.2,
                "suspended": True,
            },
            {"open": 11.0, "high": 11.9, "low": 10.9, "close": 11.6, "suspended": False},
            {"open": 11.6, "high": 12.0, "low": 11.4, "close": 11.8, "suspended": False},
        ]
    )
    labels = build_soup_labels(
        bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
        price_basis="next_tradable_open",
        exclude_untradable=True,
        conflict_policy="soft_label",
    )
    # 入场价 11.0，TP=11.55，入场日 high=11.9 命中 → 1。
    assert labels.iloc[0] == 1.0


def test_labels_config_defaults_phase0_contract() -> None:
    config = LabelsConfig()
    assert config.pnl_price_basis == "next_tradable_open"
    assert config.conflict_policy == "soft_label"
    assert config.conflict_soft_label_value == 0.5

    with (REPO_ROOT / "config" / "default.yaml").open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    labels = payload["labels"]
    assert labels["pnl_price_basis"] == "next_tradable_open"
    assert labels["conflict_policy"] == "soft_label"
    assert float(labels["conflict_soft_label_value"]) == 0.5


# ---------------------------------------------------------------------------
# B3：conflict_flag 持久化 + 旧库迁移 + 冲突判定
# ---------------------------------------------------------------------------


def _policy_record(price_basis: str = "next_tradable_open"):
    return build_label_policy_record(
        label_name="soup_10d_tp8_before_sl5",
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
        price_basis=price_basis,
        exclude_untradable=True,
        conflict_policy="soft_label",
        conflict_soft_label_value=0.5,
    )


def test_outcome_metrics_conflict_derived_from_mfe_mae() -> None:
    policy = _policy_record()
    # 入场价 = T+1 开盘 10.0；窗口内 TP(+5%) 与 SL(-5%) 同时命中 → conflict。
    bars = _bars(
        [
            {"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0},
            {"open": 10.0, "high": 10.6, "low": 9.4, "close": 10.1},
            {"open": 10.1, "high": 10.3, "low": 9.3, "close": 9.8},
        ]
    )
    metrics = _compute_outcome_metrics_for_row(bars=bars, row_position=0, label_policy=policy)
    assert metrics is not None
    assert metrics.max_favorable_excursion >= 0.05
    assert metrics.max_adverse_excursion <= -0.05
    assert metrics.conflict is True

    # 仅命中 TP → 非 conflict。
    bars_clean = _bars(
        [
            {"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0},
            {"open": 10.0, "high": 10.6, "low": 9.9, "close": 10.4},
            {"open": 10.4, "high": 10.7, "low": 10.2, "close": 10.6},
        ]
    )
    metrics_clean = _compute_outcome_metrics_for_row(
        bars=bars_clean, row_position=0, label_policy=policy
    )
    assert metrics_clean is not None
    assert metrics_clean.conflict is False


_LEGACY_OUTCOME_DDL = (
    "CREATE TABLE outcome_records ("
    "snapshot_id VARCHAR PRIMARY KEY, "
    "maturity_status VARCHAR NOT NULL, "
    "label_mature_time VARCHAR, "
    "realized_return DOUBLE, "
    "max_favorable_excursion DOUBLE, "
    "max_adverse_excursion DOUBLE, "
    "execution_fill_ratio DOUBLE, "
    "realized_slippage_bp DOUBLE, "
    "reconcile_status VARCHAR NOT NULL, "
    "sim_vs_broker_diff DOUBLE, "
    "outcome_updated_at VARCHAR NOT NULL, "
    "last_backfill_at VARCHAR, "
    "backfill_fidelity_tier VARCHAR, "
    "backfill_source VARCHAR NOT NULL, "
    "recomputed_feature_schema_id VARCHAR NOT NULL"
    ")"
)


def test_outcome_conflict_flag_roundtrip_and_legacy_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_store.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_LEGACY_OUTCOME_DDL)
    conn.execute(
        "INSERT INTO outcome_records VALUES "
        "('snap-legacy', 'label_matured', '2026-03-31T15:00:00+00:00', 0.07, 0.09, "
        "-0.03, NULL, NULL, '', NULL, '2026-03-31T15:00:00+00:00', NULL, 'gold', "
        "'runtime_observed', '')"
    )
    conn.close()

    store = SampleStore(db_path=db_path)
    legacy = store.get_outcome("snap-legacy")
    assert legacy is not None
    # 旧数据未计算冲突标记 → None（不冒充 False）。
    assert legacy.conflict_flag is None

    outcome = OutcomeRecord(
        snapshot_id="snap-new",
        maturity_status=MaturityStatus.LABEL_MATURED,
        label_mature_time=datetime(2026, 4, 10, 15, 0, tzinfo=UTC),
        realized_return=0.02,
        max_favorable_excursion=0.09,
        max_adverse_excursion=-0.06,
        conflict_flag=True,
        backfill_source="phase0_backfill",
    )
    store.upsert_outcome(outcome)
    loaded = store.get_outcome("snap-new")
    assert loaded is not None
    assert loaded.conflict_flag is True
    assert legacy.conflict_flag is None


# ---------------------------------------------------------------------------
# B5/B6：model_id v3 内容绑定 + champion 内容身份强制
# ---------------------------------------------------------------------------


def _artifact() -> ModelArtifact:
    return ModelArtifact.create(
        feature_columns=["f"],
        lgbm_model={},
        xgb_model={},
        lgbm_calibrator={},
        xgb_calibrator={},
        training_metrics={},
        dataset_manifest_id="dm1",
        feature_schema_id="fs1",
        feature_schema_hash="fsh1",
        label_policy_id="lp1",
        label_policy_hash="lph1",
    )


def test_model_id_v3_is_content_bound_and_uri_independent(tmp_path: Path) -> None:
    artifact = _artifact()  # 复用同一 artifact：created_at 属身份 payload，须保持一致。
    artifact_file_a = tmp_path / "a" / "model.json"
    artifact_file_b = tmp_path / "b" / "model.json"
    for path in (artifact_file_a, artifact_file_b):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"payload": "same"}), encoding="utf-8")

    record_a = build_model_registry_record_from_artifact(
        artifact=artifact, artifact_uri=str(artifact_file_a)
    )
    record_b = build_model_registry_record_from_artifact(
        artifact=artifact, artifact_uri=str(artifact_file_b)
    )
    # 同内容、不同路径/文件 → 身份一致（不再绑定 artifact_uri）。
    assert record_a.model_id.startswith("model_v3_")
    assert record_a.model_id == record_b.model_id

    # 内容不同 → 身份不同。
    artifact_file_c = tmp_path / "c" / "model.json"
    artifact_file_c.parent.mkdir(parents=True, exist_ok=True)
    artifact_file_c.write_text(json.dumps({"payload": "different"}), encoding="utf-8")
    record_c = build_model_registry_record_from_artifact(
        artifact=_artifact(), artifact_uri=str(artifact_file_c)
    )
    assert record_c.model_id != record_a.model_id

    # 显式 content hash 优先于文件 hash。
    record_hashed = build_model_registry_record_from_artifact(
        artifact=_artifact(),
        artifact_uri=str(artifact_file_a),
        artifact_content_hash="a" * 64,
    )
    assert record_hashed.model_id != record_a.model_id


def test_champion_requires_content_hash_and_materializes_from_file(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")

    # 无 hash + 不可读 artifact → champion 校验拒绝（fail-closed）。
    orphan = build_model_registry_record_from_artifact(
        artifact=_artifact(),
        artifact_uri=str(tmp_path / "missing" / "model.json"),
        role=ModelRole.CHAMPION,
        lifecycle_state=ModelLifecycleState.APPROVED,
        model_id="champ_orphan",
    )
    with pytest.raises(ValueError, match="champion record requires content identity"):
        registry.register(orphan)

    # 真实文件 + 空 hash → update_role 物化内容 hash 后晋升成功。
    artifact_file = tmp_path / "bundle" / "model.json"
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_file.write_text("{}", encoding="utf-8")
    record = build_model_registry_record_from_artifact(
        artifact=_artifact(),
        artifact_uri=str(artifact_file),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.APPROVED,
        model_id="chall_ok",
    )
    registry.register(record)
    promoted = registry.update_role(model_id="chall_ok", role=ModelRole.CHAMPION)
    assert promoted.artifact_content_hash == compute_artifact_file_hash(artifact_file)
    champion = registry.active_champion()
    assert champion is not None
    assert champion.model_id == "chall_ok"


# ---------------------------------------------------------------------------
# B4（代码侧）：新旧标签策略差异报告
# ---------------------------------------------------------------------------


def test_label_policy_diff_report_counts() -> None:
    old_policy = build_label_policy_record(
        label_name="soup_10d_tp8_before_sl5",
        take_profit_pct=0.08,
        stop_loss_pct=0.05,
        horizon_days=10,
        price_basis="next_tradable_open",
        exclude_untradable=True,
        conflict_policy="bar_shape_heuristic",
        conflict_soft_label_value=0.5,
        schema_version="1",
    )
    new_policy = build_label_policy_record(
        label_name="soup_10d_tp8_before_sl5",
        take_profit_pct=0.08,
        stop_loss_pct=0.05,
        horizon_days=10,
        price_basis="next_tradable_open",
        exclude_untradable=True,
        conflict_policy="soft_label",
        conflict_soft_label_value=0.5,
        schema_version="2",
    )

    outcomes = [
        # 冲突样本：v1 判 0.0，v2 判 0.5 → 变更行。
        OutcomeRecord(
            snapshot_id="s1",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 4, 1, 15, 0, tzinfo=UTC),
            realized_return=0.01,
            max_favorable_excursion=0.09,
            max_adverse_excursion=-0.06,
            conflict_flag=True,
        ),
        # 纯 TP 样本：两侧一致 1.0。
        OutcomeRecord(
            snapshot_id="s2",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 5, 2, 15, 0, tzinfo=UTC),
            realized_return=0.10,
            max_favorable_excursion=0.12,
            max_adverse_excursion=-0.01,
            conflict_flag=False,
        ),
        # 纯 SL 样本：两侧一致 0.0。
        OutcomeRecord(
            snapshot_id="s3",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 5, 20, 15, 0, tzinfo=UTC),
            realized_return=-0.07,
            max_favorable_excursion=0.02,
            max_adverse_excursion=-0.09,
            conflict_flag=False,
        ),
    ]

    report = build_label_policy_diff_report(
        outcomes=outcomes, old_policy=old_policy, new_policy=new_policy
    )
    assert report["total_outcomes"] == 3
    assert report["changed_label_rows"] == 1
    assert report["old_conflict_count"] == 1
    assert report["new_conflict_count"] == 1
    assert report["old_positive_ratio"] == pytest.approx(1 / 3, abs=1e-6)
    # 正样本按 label >= 0.5 计数：s1(0.5) 与 s2(1.0) 为正 → 2/3。
    assert report["new_positive_ratio"] == pytest.approx(2 / 3, abs=1e-6)
    assert report["mature_time_distribution"] == {"2026-04": 1, "2026-05": 2}
    assert report["old_policy_id"] != report["new_policy_id"]
    assert report["new_policy_hash"] == new_policy.label_policy_hash


# ---------------------------------------------------------------------------
# 符合性检查响应：label_anchor_time / source_data_cutoff 持久化
# ---------------------------------------------------------------------------


def test_outcome_anchor_and_cutoff_roundtrip_and_legacy_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_store2.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_LEGACY_OUTCOME_DDL)
    conn.execute(
        "INSERT INTO outcome_records VALUES "
        "('snap-legacy2', 'label_matured', '2026-03-31T15:00:00+00:00', 0.07, 0.09, "
        "-0.03, NULL, NULL, '', NULL, '2026-03-31T15:00:00+00:00', NULL, 'gold', "
        "'runtime_observed', '')"
    )
    conn.close()

    store = SampleStore(db_path=db_path)
    legacy = store.get_outcome("snap-legacy2")
    assert legacy is not None
    # 旧数据无锚点/截止 → None，不冒充已计算。
    assert legacy.label_anchor_time is None
    assert legacy.source_data_cutoff is None

    anchor = datetime(2026, 4, 1, 14, 50, tzinfo=UTC)
    cutoff = datetime(2026, 4, 15, 15, 0, tzinfo=UTC)
    outcome = OutcomeRecord(
        snapshot_id="snap-new2",
        maturity_status=MaturityStatus.LABEL_MATURED,
        label_mature_time=datetime(2026, 4, 11, 15, 0, tzinfo=UTC),
        realized_return=0.03,
        label_anchor_time=anchor,
        source_data_cutoff=cutoff,
        conflict_flag=False,
        backfill_source="phase0_backfill",
    )
    store.upsert_outcome(outcome)
    loaded = store.get_outcome("snap-new2")
    assert loaded is not None
    assert loaded.label_anchor_time == anchor
    assert loaded.source_data_cutoff == cutoff


def test_reload_gate_schema_ring_blocks_mismatched_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """schema 第三环：artifact 声明的 schema 与 registry 记录不一致 → 拒绝。"""

    from stock_analyzer.models.registry import (
        ModelLifecycleState,
        ModelRole,
        build_model_registry_record_from_artifact,
    )
    from stock_analyzer.runtime.service import StockAnalyzerService
    from tests.test_service_model_registry import _load_test_config

    service = StockAnalyzerService(config=_load_test_config(tmp_path))
    monkeypatch.setattr(service._pipeline, "reload_predictor", lambda artifact_path=None: True)

    # 现实链路：artifact 落盘（schema 绑定随文件走）→ 从同一文件构建注册记录。
    artifact_file = tmp_path / "bundle" / "model.json"
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact = _artifact()
    artifact.save(artifact_file)
    record = build_model_registry_record_from_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_file),
        role=ModelRole.CHAMPION,
        lifecycle_state=ModelLifecycleState.APPROVED,
        model_id="gate_schema_champ",
    )
    service._model_registry.register(record)

    # 同一 artifact（schema 绑定一致）→ 放行。
    assert service._validated_predictor_reload(str(artifact_file), source="t_ok") is True

    # 篡改 artifact 的 schema hash → 与 registry 记录不一致 → 拒绝。
    payload = json.loads(artifact_file.read_text(encoding="utf-8"))
    payload["feature_schema_hash"] = "tampered_hash"
    artifact_file.write_text(json.dumps(payload), encoding="utf-8")
    assert service._validated_predictor_reload(str(artifact_file), source="t_bad") is False
