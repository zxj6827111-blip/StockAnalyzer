"""C2 输出语义契约测试。

覆盖：
1. label basis → 输出语义的映射（已知/未知/空值三态，未知 fail-closed）；
2. return_rank（rank_quantile）不得被当作全市场上涨概率：中间段剔除说明必须
   随语义输出，且事件标签类指标（Brier/logloss/accuracy）明确标记为不可用；
3. 生产端（SignalPredictor）如实声明工件语义，未登记契约在**推理路径**上
   fail-soft（返回 None + 错误串留痕）而不是抛异常；
4. 消费端：cross review 的阈值语义随调用留痕；shadow 的 Brier/logloss 在
   非事件语义下必须拒绝价格代理标签并计数留痕。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stock_analyzer.evolution.modules.shadow_online_model_v2 import (
    _extract_label,
    _label_semantics_mismatch,
    run_shadow_online_model_v2,
)
from stock_analyzer.models.output_semantics import (
    MIDDLE_DROPPED_NOTE,
    OUTPUT_SEMANTICS_EVENT_PROBABILITY,
    OUTPUT_SEMANTICS_RANK_QUANTILE,
    describe_output_semantics,
    output_semantics_for_basis,
    semantics_supports_event_label_metrics,
)
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.signal.cross_review import evaluate_cross_review


class _CrossReviewConfig:
    p_lgbm_min = 0.4
    p_xgb_min = 0.4
    p_meta_min = 0.4
    max_diff = 0.5
    dynamic_enabled = False
    relax_threshold_on_low_auc = False
    tighten_threshold_on_high_auc = False
    relax_threshold_delta = 0.05
    relax_max_diff_delta = 0.1
    tighten_threshold_delta = 0.05
    tighten_max_diff_delta = 0.1
    degraded_consensus_enabled = False


class TestBasisToSemanticsMapping:
    def test_known_bases(self) -> None:
        assert (
            output_semantics_for_basis("soup_5d_tp5_before_sl5")
            == OUTPUT_SEMANTICS_EVENT_PROBABILITY
        )
        assert output_semantics_for_basis("return_rank") == OUTPUT_SEMANTICS_RANK_QUANTILE
        assert output_semantics_for_basis("label_policy_v3_b0b3724553b5") == (
            OUTPUT_SEMANTICS_RANK_QUANTILE
        )
        assert output_semantics_for_basis("label_return_rank") == OUTPUT_SEMANTICS_RANK_QUANTILE

    def test_empty_basis_is_unknown_not_guessed(self) -> None:
        assert output_semantics_for_basis(None) is None
        assert output_semantics_for_basis("") is None
        assert output_semantics_for_basis("   ") is None

    def test_unregistered_basis_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="unregistered label basis"):
            output_semantics_for_basis("brand_new_exotic_basis")

    def test_event_metric_allowance(self) -> None:
        assert semantics_supports_event_label_metrics(None) is True  # 历史兼容
        assert semantics_supports_event_label_metrics(OUTPUT_SEMANTICS_EVENT_PROBABILITY) is True
        assert semantics_supports_event_label_metrics(OUTPUT_SEMANTICS_RANK_QUANTILE) is False

    def test_middle_dropped_note_only_for_rank_quantile(self) -> None:
        rank = describe_output_semantics("return_rank")
        assert rank["output_semantics"] == OUTPUT_SEMANTICS_RANK_QUANTILE
        assert rank["event_label_metrics_allowed"] is False
        assert rank["middle_dropped_note"] == MIDDLE_DROPPED_NOTE
        assert "中间 40%" in MIDDLE_DROPPED_NOTE
        assert "不自动等于全市场上涨概率" in MIDDLE_DROPPED_NOTE

        soup = describe_output_semantics("soup_5d_tp5_before_sl5")
        assert soup["event_label_metrics_allowed"] is True
        assert soup["middle_dropped_note"] == ""


class TestProducerDeclaresSemantics:
    @staticmethod
    def _predictor(label_policy_id: str) -> SignalPredictor:
        return SignalPredictor(
            feature_columns=[],
            lgbm=None,  # type: ignore[arg-type]
            xgb=None,  # type: ignore[arg-type]
            lgbm_calibrator=None,  # type: ignore[arg-type]
            xgb_calibrator=None,  # type: ignore[arg-type]
            label_policy_id=label_policy_id,
        )

    def test_return_rank_artifact_declares_rank_quantile(self) -> None:
        report = self._predictor("return_rank").output_semantics_report()
        assert report["output_semantics"] == OUTPUT_SEMANTICS_RANK_QUANTILE
        assert report["event_label_metrics_allowed"] is False
        assert report["output_semantics_error"] == ""

    def test_soup_artifact_declares_event_probability(self) -> None:
        report = self._predictor("soup_5d_tp5_before_sl5").output_semantics_report()
        assert report["output_semantics"] == OUTPUT_SEMANTICS_EVENT_PROBABILITY
        assert report["event_label_metrics_allowed"] is True

    def test_unregistered_contract_is_fail_soft_on_inference_path(self) -> None:
        # 推理路径不得因新契约整体失败，但必须留痕（审计路径仍 fail-closed）。
        predictor = self._predictor("some_unregistered_contract")
        assert predictor.output_semantics is None
        report = predictor.output_semantics_report()
        assert report["output_semantics"] is None
        assert "unregistered label basis" in str(report["output_semantics_error"])
        with pytest.raises(ValueError):
            describe_output_semantics("some_unregistered_contract")


class TestConsumerContracts:
    def test_cross_review_echoes_semantics(self) -> None:
        kwargs = {"lgbm_prob": 0.6, "xgb_prob": 0.6, "meta_prob": 0.6}
        plain = evaluate_cross_review(config=_CrossReviewConfig(), **kwargs)
        assert not any(str(r).startswith("output_semantics:") for r in plain.reasons)

        tagged = evaluate_cross_review(
            config=_CrossReviewConfig(), output_semantics=OUTPUT_SEMANTICS_RANK_QUANTILE, **kwargs
        )
        assert "output_semantics:rank_quantile" in tagged.reasons

    def test_shadow_rejects_price_proxy_label_under_rank_semantics(self) -> None:
        record = {
            "symbol": "600000.SH",
            "label_basis": "return_rank",
            "open": 10.0,
            "close": 10.4,
        }
        assert _label_semantics_mismatch(record) is True
        assert _extract_label(record) is None  # 不得用"收盘>=开盘"代理

    def test_shadow_keeps_price_proxy_for_event_semantics(self) -> None:
        record = {
            "symbol": "600000.SH",
            "label_basis": "soup_5d_tp5_before_sl5",
            "open": 10.0,
            "close": 10.4,
        }
        assert _label_semantics_mismatch(record) is False
        assert _extract_label(record) == 1

    def test_shadow_explicit_label_wins_regardless_of_semantics(self) -> None:
        record = {"label_basis": "return_rank", "label": 0, "open": 10.0, "close": 10.4}
        assert _extract_label(record) == 0

    def test_shadow_counts_excluded_rows_in_reasons(self) -> None:
        # 缺显式 label 的 rank_quantile 记录必须被排除并留痕，不得默默产出损失值。
        records = []
        for i in range(3):
            records.append(
                {
                    "symbol": f"60000{i}.SH",
                    "trade_date": "2026-03-01",
                    "label_mature_time": "2026-03-02T15:00:00",
                    "label_basis": "return_rank",
                    "open": 10.0,
                    "close": 10.4,
                    "champion_scores": {"p_meta": 0.5},
                    "shadow_scores": {"p_meta": 0.5},
                }
            )
        result = run_shadow_online_model_v2(
            records=records,
            previous_state=None,
            now=datetime(2026, 3, 10, tzinfo=UTC),
            min_samples=1,
            max_samples=10,
            learning_rate=0.1,
        )
        assert any(
            str(reason).startswith("excluded_label_semantics_mismatch:") for reason in result.reasons
        ), result.reasons
