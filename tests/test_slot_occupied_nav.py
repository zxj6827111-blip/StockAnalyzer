"""Commit 4（P1-b）验收测试：slot 占用 NAV 模拟器 + 晋级有效性门。

规格重建说明：原 v3.1 计划文本在本节被会话截断，以下断言按 NAS 8/23
根因证据（e44 = 逐笔全仓复利 × 重复快照口径）与“结构性失效必须拦、
小样本不稳定只告警”的取向重建。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import pytest

from stock_analyzer.learning.slot_occupied_nav import (
    DEFAULT_EXPLOSION_THRESHOLD,
    EventSlotPositionInput,
    simulate_slot_occupied_realized_nav,
)
from stock_analyzer.learning.slot_occupied_nav import (
    evaluate_promotion_validity as gate_evaluate,
)

# ---------------------------------------------------------------------------
# 模拟器
# ---------------------------------------------------------------------------


def test_simulator_fixed_base_slot_allocation() -> None:
    report = simulate_slot_occupied_realized_nav(
        realized_returns=[0.08, -0.05],
        max_slots=4,
    )
    # 固定基数：NAV = 1 + Σ r / slots（不复利）。
    assert report.slot_occupied_realized_nav == pytest.approx(1 + (0.08 - 0.05) / 4)
    # 复利参照：Π(1+r)。
    assert report.naive_compounded_nav == pytest.approx(1.08 * 0.95)
    assert report.compounding_explosion is False
    assert report.trade_count == 2


def test_simulator_empty_and_single_slot() -> None:
    empty = simulate_slot_occupied_realized_nav(realized_returns=[], max_slots=4)
    assert empty.slot_occupied_realized_nav == 1.0
    assert empty.naive_compounded_nav == pytest.approx(1.0)
    assert empty.compounding_explosion is False

    single = simulate_slot_occupied_realized_nav(
        realized_returns=[0.1, 0.2], max_slots=1
    )
    # slots=1 时资金不分仓，但仍为固定基数（不复利）：1 + 0.1 + 0.2；
    # 复利参照才是 Π(1+r)=1.32。
    assert single.slot_occupied_realized_nav == pytest.approx(1.3)
    assert single.naive_compounded_nav == pytest.approx(1.32)


def test_simulator_detects_duplicate_inflated_compounding_explosion() -> None:
    # e44 场景重现：3936 笔 mean≈+2.9% 全仓复利上界 ~1e49；slot 口径保持有界。
    returns = [0.029] * 3936
    report = simulate_slot_occupied_realized_nav(
        realized_returns=returns,
        max_slots=4,
        explosion_threshold=DEFAULT_EXPLOSION_THRESHOLD,
    )
    assert report.compounding_explosion is True
    # 复利参照有限但已越过阈值（~7.4e48，与 NAS 实测 e44 同一量级形态）。
    assert report.naive_compounded_nav > DEFAULT_EXPLOSION_THRESHOLD
    expected_slot_nav = 1.0 + 3936 * 0.029 / 4
    assert report.slot_occupied_realized_nav == pytest.approx(expected_slot_nav)
    assert math.isfinite(report.slot_occupied_realized_nav)


def test_simulator_clamps_impossible_returns() -> None:
    report = simulate_slot_occupied_realized_nav(realized_returns=[-1.5], max_slots=4)
    assert report.naive_compounded_nav == pytest.approx(0.0)  # 破产
    assert report.slot_occupied_realized_nav == pytest.approx(1 - 0.25)
    assert report.compounding_explosion is False


def test_simulator_rejects_invalid_slots() -> None:
    with pytest.raises(ValueError, match="max_slots"):
        simulate_slot_occupied_realized_nav(realized_returns=[0.01], max_slots=0)


# ---------------------------------------------------------------------------
# 晋级有效性门
# ---------------------------------------------------------------------------


def _healthy_metrics() -> dict[str, float]:
    return {
        "auc": 0.62,
        "auc_valid": 1.0,
        "hard_label_count": 500,
        "soft_label_count": 12,
        "dataset_slot_occupied_realized_nav": 1.42,
        "dataset_naive_compounded_nav": 3.7,
        "dataset_nav_compounding_explosion": 0.0,
    }


def test_gate_passes_on_healthy_evaluation() -> None:
    report = gate_evaluate(metrics_summary=_healthy_metrics())
    assert report.valid is True
    assert report.blocking_reasons == []
    assert report.warnings == []


def test_gate_blocks_auc_single_class_only_with_enough_hard_labels() -> None:
    metrics = _healthy_metrics() | {"auc_valid": 0.0}
    blocked = gate_evaluate(metrics_summary=metrics)
    assert "auc_invalid_single_class" in blocked.blocking_reasons

    small = _healthy_metrics() | {"auc_valid": 0.0, "hard_label_count": 6}
    warned = gate_evaluate(metrics_summary=small)
    # 小样本下单类只告警不拦截（结构性失效才 blocking）。
    assert "auc_invalid_single_class" not in warned.blocking_reasons
    assert warned.valid is True


def test_gate_warns_on_insufficient_hard_labels() -> None:
    report = gate_evaluate(
        metrics_summary=_healthy_metrics() | {"hard_label_count": 10}
    )
    assert report.valid is True
    assert "insufficient_hard_labels" in report.warnings


def test_gate_blocks_duplicate_dominance_and_quality_flags() -> None:
    report = gate_evaluate(
        metrics_summary=_healthy_metrics(),
        dedup_quality={"rows_before_dedup": 100, "rows_dropped_by_dedup": 60},
    )
    assert "duplicate_dominance" in report.blocking_reasons

    flagged = gate_evaluate(
        metrics_summary=_healthy_metrics(),
        dedup_quality={
            "rows_before_dedup": 100,
            "rows_dropped_by_dedup": 10,
            "blocking_quality_flags": ["empty_after_dedup"],
        },
    )
    assert "blocking_quality_flags:empty_after_dedup" in flagged.blocking_reasons
    assert flagged.valid is False


def test_gate_blocks_nav_explosion_from_stored_metrics_or_returns() -> None:
    stored = gate_evaluate(
        metrics_summary=_healthy_metrics()
        | {
            "dataset_naive_compounded_nav": float(DEFAULT_EXPLOSION_THRESHOLD) * 10,
            "dataset_nav_compounding_explosion": 1.0,
        }
    )
    assert "nav_compounding_explosion" in stored.blocking_reasons

    recomputed = gate_evaluate(
        metrics_summary=_healthy_metrics(),
        realized_returns=[0.029] * 3936,
    )
    assert "nav_compounding_explosion" in recomputed.blocking_reasons


# ---------------------------------------------------------------------------
# 发布流程接线（故障注入）
# ---------------------------------------------------------------------------


def test_release_refuses_when_validity_gate_fails(tmp_path: Path) -> None:
    import stock_analyzer.runtime.services.learning_governance_service as gov_module
    from stock_analyzer.learning.slot_occupied_nav import PromotionValidityReport
    from tests.test_model_bundle_release import _prepare_release_ready_service

    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])
    alias_path = Path(str(service._config.training.artifact_path)).expanduser()
    alias_before = alias_path.read_bytes() if alias_path.exists() else None
    predictor_before = service._pipeline._predictor

    executor_cls = gov_module._TwoPhaseReleaseExecutor

    def _invalid_validity(self, **kwargs: object) -> PromotionValidityReport:
        return PromotionValidityReport(
            valid=False,
            blocking_reasons=["nav_compounding_explosion"],
        )

    # 在两阶段执行器类上注入无效判定（prepare 内部调用）。
    executor_cls._evaluate_candidate_validity = _invalid_validity
    try:
        execute = cast(
            dict[str, object],
            service.execute_learning_model_release_ticket(
                "release_manager",
                ticket_id=str(ctx["ticket"]["ticket_id"]),
            ),
        )
    finally:
        delattr(executor_cls, "_evaluate_candidate_validity")

    assert execute["accepted"] is False
    assert execute["code"] == "promotion_validity_failed"
    if alias_before is None:
        assert not alias_path.exists()
    else:
        assert alias_path.read_bytes() == alias_before
    assert service._pipeline._predictor is predictor_before
    roles = {
        record.model_id: record.role
        for record in service._model_registry.list_records(limit=50)
    }
    assert roles[ctx["champion_model_id"]] == __import__(
        "stock_analyzer.models.registry", fromlist=["ModelRole"]
    ).ModelRole.CHAMPION


# ---------------------------------------------------------------------------
# 补救验收后的边界补充：事件模拟器缺陷口径与重复注水门禁
# ---------------------------------------------------------------------------


def test_event_simulator_marks_same_day_and_post_horizon_as_defects() -> None:
    from stock_analyzer.learning.slot_occupied_nav import simulate_event_driven_slot_nav

    same_day = simulate_event_driven_slot_nav(
        [
            EventSlotPositionInput(
                symbol="600000.SH",
                entry_date="2026-06-01",
                exit_date="2026-06-01",
                realized_return=0.10,
                probability=0.9,
            )
        ]
    )
    assert same_day.insufficient_date_coverage is True
    assert any("same_day_entry_exit" in item for item in same_day.coverage_defects)
    assert same_day.settled_position_count == 0

    post_horizon = simulate_event_driven_slot_nav(
        [
            EventSlotPositionInput(
                symbol="600000.SH",
                entry_date="2026-06-10",
                exit_date="2026-06-15",
                realized_return=0.10,
                probability=0.9,
            )
        ],
        horizon_date="2026-06-05",
    )
    assert post_horizon.insufficient_date_coverage is True
    assert any("entry_after_horizon" in item for item in post_horizon.coverage_defects)
    # 观察期外入场不进入日级序列与模拟。
    assert post_horizon.event_days == 0
    assert post_horizon.final_nav == 1.0

    with pytest.raises(ValueError, match="horizon_date"):
        simulate_event_driven_slot_nav(
            [],
            horizon_date="not-a-date",
        )


def test_event_simulator_never_lets_nav_go_negative() -> None:
    from stock_analyzer.learning.slot_occupied_nav import simulate_event_driven_slot_nav

    report = simulate_event_driven_slot_nav(
        [
            EventSlotPositionInput(
                symbol="600000.SH",
                entry_date="2026-06-01",
                exit_date="2026-06-02",
                realized_return=-1.0,
                probability=0.9,
            ),
            EventSlotPositionInput(
                symbol="000001.SZ",
                entry_date="2026-06-03",
                exit_date="2026-06-04",
                realized_return=-1.0,
                probability=0.8,
            ),
        ],
        max_positions=1,
    )
    assert report.final_nav >= 0.0
    assert all(value >= 0.0 for value in report.daily_nav_series.values())


def test_gate_blocks_when_rows_are_duplicate_inflated() -> None:
    """重复快照注水：行数再多，逻辑样本不足仍必须拦截。"""
    inflated = gate_evaluate(
        metrics_summary=_healthy_metrics(),
        test_stats={
            "unique_trade_dates": 25,
            # NAS 事故形态：3936 行重复快照只有个位数独立逻辑样本。
            "unique_logical_samples": 5,
            "hard_positive_count": 40,
            "hard_negative_count": 40,
        },
    )
    assert "insufficient_unique_logical_samples" in inflated.blocking_reasons
    assert inflated.valid is False

    healthy = gate_evaluate(
        metrics_summary=_healthy_metrics(),
        test_stats={
            "unique_trade_dates": 25,
            "unique_logical_samples": 60,
            "hard_positive_count": 40,
            "hard_negative_count": 40,
        },
    )
    assert "insufficient_unique_logical_samples" not in healthy.blocking_reasons
