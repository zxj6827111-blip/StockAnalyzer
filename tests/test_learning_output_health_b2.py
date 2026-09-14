"""B2：输出健康门 + 批准/发布/热载统一（学习链整改 v2 批次 B）。

判据（v2 §B2）：

- 确定性失败（非有限值 / 常数输出 / 契约不匹配）hard-block；
- 经验阈值（unique<20、raw→cal AUC 落差、spread 相对塌缩）只 advisory；
- **不设** `positive_rate ∈ [0.30,0.70]` 通用硬门；不跨分数尺度复用 spread 绝对阈值；
- 保留 `evaluate_promotion_validity` 的全部既有阻断项（本 PR 只增不减）；
- 热载不得只认"任意 role 的 hash 命中"：blocked/revoked 记录不得构成放行凭证，
  训练入口 load_predictor 分支同样受输出健康门约束。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.learning.output_health import (
    MIN_SCORED_FOR_CONSTANT_BLOCK,
    evaluate_output_health,
)
from stock_analyzer.learning.slot_occupied_nav import evaluate_promotion_validity
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRole,
    build_model_registry_record_from_artifact,
)


def _healthy_metrics() -> dict[str, float]:
    """一个"健康但经验指标普通"的输出：可排序、非有限值 0、价值唯一。"""

    return {
        "auc": 0.58,
        "auc_valid": 1.0,
        "hard_label_count": 60.0,
        "hard_positive_count": 30.0,
        "hard_negative_count": 30.0,
        "scored_samples_raw_blend": 120.0,
        "unique_values_raw_blend": 120.0,
        "non_finite_count_raw_blend": 0.0,
        "tie_fraction_raw_blend": 0.0,
        "auc_raw_blend": 0.6,
        "mean_prob_spread_raw_blend": 0.12,
        "scored_samples_calibrated_blend": 120.0,
        "unique_values_calibrated_blend": 90.0,
        "non_finite_count_calibrated_blend": 0.0,
        "tie_fraction_calibrated_blend": 0.25,
        "auc_calibrated_blend": 0.56,
        "mean_prob_spread_calibrated_blend": 0.09,
    }


# ---------------------------------------------------------------------------
# 输出健康门（单输出语义检查）
# ---------------------------------------------------------------------------


def test_mis_kill_counterexample_is_not_rejected() -> None:
    """审核反例：分数全在 0.51~0.59、预测正率 100%、AUC=1、Precision@K=1。

    排序完全正确，**不得**被新门拒绝——这正是不设 `positive_rate ∈ [0.30,0.70]`
    通用硬门的理由。
    """

    metrics = {
        "predicted_positive_rate": 1.0,
        "positive_rate_calibrated_blend": 1.0,
        "precision_at_k_calibrated_blend": 1.0,
        "auc_calibrated_blend": 1.0,
        "auc_raw_blend": 1.0,
        "scored_samples_calibrated_blend": 100.0,
        # 0.51~0.59 的连续区间 → 9 个以上不同取值，不是常数输出。
        "unique_values_calibrated_blend": 9.0,
        "non_finite_count_calibrated_blend": 0.0,
        "tie_fraction_calibrated_blend": 0.91,
        "mean_prob_spread_calibrated_blend": 0.07,
        "scored_samples_raw_blend": 100.0,
        "unique_values_raw_blend": 9.0,
        "non_finite_count_raw_blend": 0.0,
        "mean_prob_spread_raw_blend": 0.07,
    }

    report = evaluate_output_health(metrics)

    assert report.valid is True
    assert report.blocking_reasons == []


def test_healthy_output_passes_without_advisories() -> None:
    report = evaluate_output_health(_healthy_metrics())

    assert report.valid is True
    assert report.blocking_reasons == []
    assert report.warnings == []
    assert report.checks["evaluable"] is True


def test_constant_calibrated_output_hard_blocks() -> None:
    """9/13 工件形态：校准后塌成常数（唯一取值 1）→ 确定性失败，必须阻断。"""

    metrics = _healthy_metrics()
    metrics["unique_values_calibrated_blend"] = 1.0
    metrics["mean_prob_spread_calibrated_blend"] = 0.0003

    report = evaluate_output_health(metrics)

    assert report.valid is False
    assert "output_health_constant_output:calibrated" in report.blocking_reasons


def test_non_finite_output_hard_blocks() -> None:
    metrics = _healthy_metrics()
    metrics["non_finite_count_calibrated_blend"] = 3.0

    report = evaluate_output_health(metrics)

    assert report.valid is False
    assert "output_health_non_finite_values:calibrated" in report.blocking_reasons


def test_no_scored_samples_hard_blocks() -> None:
    metrics = _healthy_metrics()
    metrics["scored_samples_calibrated_blend"] = 0.0

    report = evaluate_output_health(metrics)

    assert report.valid is False
    assert "output_health_no_scored_samples:calibrated" in report.blocking_reasons


def test_partially_declared_output_semantics_hard_blocks() -> None:
    """契约不匹配：声明了输出语义却缺该尺度的关键字段（自相矛盾）。"""

    metrics = _healthy_metrics()
    metrics.pop("unique_values_calibrated_blend")

    report = evaluate_output_health(metrics)

    assert report.valid is False
    assert "output_health_metrics_missing:calibrated" in report.blocking_reasons


def test_legacy_metrics_without_output_semantics_is_advisory_only() -> None:
    """完全没有输出语义字段 → 无法判定，advisory 放行（不能用新门挡旧工件）。"""

    report = evaluate_output_health({"auc": 0.33, "auc_valid": 1.0, "hard_label_count": 50.0})

    assert report.valid is True
    assert report.blocking_reasons == []
    assert report.checks["evaluable"] is False
    # 覆盖缺口不进 warnings（避免每个旧工件都带噪音告警），但留痕在 checks 里。
    assert report.warnings == []


def test_empirical_thresholds_are_advisory_not_blocking() -> None:
    """unique<20、AUC 落差>0.30、spread 相对塌缩：只 warning，绝不阻断。"""

    metrics = _healthy_metrics()
    metrics["unique_values_calibrated_blend"] = 5.0
    metrics["unique_values_raw_blend"] = 5.0
    metrics["auc_raw_blend"] = 0.9
    metrics["auc_calibrated_blend"] = 0.5
    metrics["mean_prob_spread_calibrated_blend"] = 0.001

    report = evaluate_output_health(metrics)

    assert report.valid is True
    assert report.blocking_reasons == []
    assert "output_health_low_unique_values_advisory:calibrated" in report.warnings
    assert "output_health_low_unique_values_advisory:raw" in report.warnings
    assert "output_health_auc_drop_advisory" in report.warnings
    assert "output_health_calibration_spread_collapse_advisory" in report.warnings
    assert report.checks["auc_drop"] == pytest.approx(0.4)
    assert float(report.checks["spread_retention_ratio"]) < 0.2


# ---------------------------------------------------------------------------
# 批准路径（evaluate_promotion_validity 集成）
# ---------------------------------------------------------------------------


def _full_test_stats() -> dict[str, float]:
    return {
        "unique_trade_dates": 40.0,
        "unique_logical_samples": 200.0,
        "hard_positive_count": 50.0,
        "hard_negative_count": 50.0,
    }


def test_promotion_gate_blocks_degenerate_output_and_keeps_existing_items() -> None:
    """退化工件在批准路径被拒，理由可查；既有阻断项一个不少。"""

    metrics = _healthy_metrics()
    metrics["unique_values_calibrated_blend"] = 1.0

    report = evaluate_promotion_validity(
        metrics_summary=metrics,
        # v1 manifest 无法自证无重复 → 既有阻断项，与输出健康门并存。
        manifest_schema_version="1",
        require_full_gates=True,
        min_test_trade_dates=20,
        min_hard_class_samples=30,
        test_stats=_full_test_stats(),
    )

    assert report.valid is False
    assert "output_health_constant_output:calibrated" in report.blocking_reasons
    # 既有阻断项保持并存（本改动只增不减）。
    assert "manifest_not_v2" in report.blocking_reasons
    assert "output_health" in report.checks


def test_promotion_gate_does_not_reject_mis_kill_counterexample() -> None:
    """误杀反例在**完整门**下也不被新层拒绝（其余既有门按健康值构造为通过）。"""

    metrics = _healthy_metrics()
    metrics.update(
        {
            "auc_calibrated_blend": 1.0,
            "auc_raw_blend": 1.0,
            "precision_at_k_calibrated_blend": 1.0,
            "positive_rate_calibrated_blend": 1.0,
            "unique_values_calibrated_blend": 9.0,
            "unique_values_raw_blend": 9.0,
        }
    )

    report = evaluate_promotion_validity(
        metrics_summary=metrics,
        manifest_schema_version="2",
        require_full_gates=True,
        min_test_trade_dates=20,
        min_hard_class_samples=30,
        test_stats=_full_test_stats(),
    )

    assert report.valid is True
    assert report.blocking_reasons == []
    # 经验阈值只以 warning 出现（unique=9 < 20）。
    assert any("low_unique_values_advisory" in item for item in report.warnings)


# ---------------------------------------------------------------------------
# 热载路径（_validated_predictor_reload）
# ---------------------------------------------------------------------------


def _reload_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    from stock_analyzer.runtime.service import StockAnalyzerService
    from tests.test_service_model_registry import _load_test_config

    service = StockAnalyzerService(config=_load_test_config(tmp_path))
    monkeypatch.setattr(service._pipeline, "reload_predictor", lambda artifact_path=None: True)
    return service


def _saved_artifact(tmp_path: Path, metrics: dict[str, float], name: str) -> Path:
    artifact_file = tmp_path / name
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    ModelArtifact.create(
        feature_columns=["f"],
        lgbm_model={},
        xgb_model={},
        lgbm_calibrator={},
        xgb_calibrator={},
        training_metrics=metrics,
        dataset_manifest_id="dm1",
        feature_schema_id="fs1",
        feature_schema_hash="fsh1",
        label_policy_id="lp1",
        label_policy_hash="lph1",
    ).save(artifact_file)
    return artifact_file


def test_reload_allows_healthy_approved_champion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _reload_service(tmp_path, monkeypatch)
    artifact_file = _saved_artifact(tmp_path, _healthy_metrics(), "bundle/model.json")
    record = build_model_registry_record_from_artifact(
        artifact=ModelArtifact.load(artifact_file),
        artifact_uri=str(artifact_file),
        role=ModelRole.CHAMPION,
        lifecycle_state=ModelLifecycleState.APPROVED,
        model_id="b2_healthy_champ",
    )
    service._model_registry.register(record)

    assert service._validated_predictor_reload(str(artifact_file), source="t_ok") is True


def test_reload_blocks_blocked_and_revoked_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败/撤销工件不得因"任一 role 的 hash 命中"而放行（F23）。"""

    for state, model_id, blocked_reason in (
        (ModelLifecycleState.BLOCKED, "b2_blocked", "quality_failed:output_health"),
        (ModelLifecycleState.REVOKED, "b2_revoked", ""),
    ):
        service = _reload_service(tmp_path / state.value, monkeypatch)
        artifact_file = _saved_artifact(
            tmp_path / state.value, _healthy_metrics(), "bundle/model.json"
        )
        record = build_model_registry_record_from_artifact(
            artifact=ModelArtifact.load(artifact_file),
            artifact_uri=str(artifact_file),
            role=ModelRole.CHALLENGER,
            lifecycle_state=state,
            model_id=model_id,
        )
        if blocked_reason:
            # blocked 生命周期要求非空 blocked_reason（registry 状态机约束）。
            record = record.model_copy(update={"blocked_reason": blocked_reason})
        service._model_registry.register(record)

        assert (
            service._validated_predictor_reload(str(artifact_file), source=f"t_{state.value}")
            is False
        )
        assert service._audit_events[-1]["event_type"] == "predictor_reload_failed_state_blocked"


def test_reload_blocks_constant_output_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """训练入口 load_predictor 分支同样受输出健康门约束：退化输出不得被装载。"""

    service = _reload_service(tmp_path, monkeypatch)
    metrics = _healthy_metrics()
    metrics["unique_values_calibrated_blend"] = 1.0
    artifact_file = _saved_artifact(tmp_path, metrics, "bundle/degenerate.json")
    record = build_model_registry_record_from_artifact(
        artifact=ModelArtifact.load(artifact_file),
        artifact_uri=str(artifact_file),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
        model_id="b2_degenerate",
    )
    service._model_registry.register(record)

    assert service._validated_predictor_reload(str(artifact_file), source="t_deg") is False
    last_event = service._audit_events[-1]
    assert last_event["event_type"] == "predictor_reload_output_health_blocked"
    assert "output_health_constant_output:calibrated" in last_event["payload"]["blocking_reasons"]


def test_reload_allows_legacy_artifact_without_output_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """legacy 工件（无输出语义字段）不能判定 → 留审计放行，不被新门挡在加载之外。"""

    service = _reload_service(tmp_path, monkeypatch)
    artifact_file = _saved_artifact(tmp_path, {"auc": 0.33}, "bundle/legacy.json")
    record = build_model_registry_record_from_artifact(
        artifact=ModelArtifact.load(artifact_file),
        artifact_uri=str(artifact_file),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
        model_id="b2_legacy",
    )
    service._model_registry.register(record)

    assert service._validated_predictor_reload(str(artifact_file), source="t_legacy") is True
    assert service._audit_events[-1]["event_type"] == (
        "predictor_reload_output_health_legacy_skipped"
    )


class TestConstantOutputSampleSizeFloor:
    """常数输出判定的**样本量有效性下限**（B1×B2 交互修复）。

    背景：B1 让 trainer 开始产出 raw/calibrated 输出语义字段后，本门从「legacy
    无法判定→放行」变为激活，集成后立刻有 15 个走服务训练路径的既有测试被
    `output_health_constant_output:calibrated` 阻断。定位为**小样本伪影**：
    fixture 仅 6 个测试样本，calibrated unique=1 但 raw unique=6、auc_raw=0.333
    ——原始模型仍能排序，塌的只是小样本 isotonic 校准器。
    """

    @staticmethod
    def _small_sample_constant() -> dict[str, float]:
        metrics = _healthy_metrics()
        metrics["scored_samples_raw_blend"] = 6.0
        metrics["unique_values_raw_blend"] = 6.0
        metrics["auc_raw_blend"] = 0.333
        metrics["scored_samples_calibrated_blend"] = 6.0
        metrics["unique_values_calibrated_blend"] = 1.0
        metrics["mean_prob_spread_calibrated_blend"] = 0.0
        return metrics

    def test_small_sample_calibrated_constant_is_advisory_not_blocking(self) -> None:
        report = evaluate_output_health(self._small_sample_constant())

        assert report.blocking_reasons == []
        assert "output_health_constant_output_small_sample_advisory:calibrated" in report.warnings

    def test_floor_boundary_blocks_at_threshold(self) -> None:
        # 29 个样本 → 仍属小样本伪影；30 个样本 → 按确定性失败阻断。
        below = self._small_sample_constant()
        below["scored_samples_calibrated_blend"] = float(MIN_SCORED_FOR_CONSTANT_BLOCK - 1)
        assert evaluate_output_health(below).blocking_reasons == []

        at = self._small_sample_constant()
        at["scored_samples_calibrated_blend"] = float(MIN_SCORED_FOR_CONSTANT_BLOCK)
        assert (
            "output_health_constant_output:calibrated"
            in evaluate_output_health(at).blocking_reasons
        )

    def test_raw_constant_still_blocks_at_small_sample(self) -> None:
        # 下限只用于**校准器塌缩**这一伪影；原始输出常数是模型本身缺陷，小样本也阻断。
        metrics = self._small_sample_constant()
        metrics["unique_values_raw_blend"] = 1.0
        report = evaluate_output_health(metrics)
        assert "output_health_constant_output:raw" in report.blocking_reasons

    def test_large_sample_constant_still_blocks(self) -> None:
        # 真实运行量级（9/13 工件 2392 个测试样本）：门禁强度不变。
        metrics = _healthy_metrics()
        metrics["scored_samples_calibrated_blend"] = 2392.0
        metrics["unique_values_calibrated_blend"] = 1.0
        metrics["mean_prob_spread_calibrated_blend"] = 0.000272
        assert (
            "output_health_constant_output:calibrated"
            in evaluate_output_health(metrics).blocking_reasons
        )
