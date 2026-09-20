"""S09 模型/标签语义守卫：输出必须声明语义，禁止把排序分说成上涨概率。

对应蓝图 §5 P0-09 / 阶段施工提示词 S09：

```text
每个模型输出必须声明：label_policy_id / label_name / horizon / price_basis /
output_kind / calibration
rank_quantile 的概率只能解释为该标签定义下的正类概率/分值，
禁止显示成「未来上涨概率」（除非确实训练 P(net_return_h > 0) 且有 OOS 校准）
legacy 只标 degraded_unverified，不自动反转、不自动替换
```
"""

from __future__ import annotations

import pytest

from stock_analyzer.models.semantics_guard import (
    FORBIDDEN_UP_PROBABILITY_TERMS,
    MODEL_HEALTH_DEGRADED_UNVERIFIED,
    MODEL_HEALTH_OK,
    OUTPUT_KIND_PROBABILITY,
    OUTPUT_KIND_RANK_SCORE,
    OUTPUT_KIND_UNKNOWN,
    UP_PROBABILITY_DISPLAY_TERM,
    describe_model_semantics,
    guard_display_text,
    resolve_output_kind,
)

# ---------------------------------------------------------------------------
# output_kind 映射
# ---------------------------------------------------------------------------


def test_rank_quantile_maps_to_rank_score_not_probability() -> None:
    assert resolve_output_kind(label_basis="return_rank") == OUTPUT_KIND_RANK_SCORE
    assert resolve_output_kind(output_semantics="rank_quantile") == OUTPUT_KIND_RANK_SCORE
    assert resolve_output_kind(output_semantics="ranking_score") == OUTPUT_KIND_RANK_SCORE


def test_event_probability_maps_to_probability() -> None:
    assert resolve_output_kind(label_basis="soup") == OUTPUT_KIND_PROBABILITY
    assert resolve_output_kind(output_semantics="event_probability") == OUTPUT_KIND_PROBABILITY


def test_unknown_semantics_stays_unknown() -> None:
    assert resolve_output_kind(output_semantics="") == OUTPUT_KIND_UNKNOWN
    assert resolve_output_kind(output_semantics="something_new") == OUTPUT_KIND_UNKNOWN


# ---------------------------------------------------------------------------
# 语义声明完整性 + 未登记 fail-closed（不抛异常）
# ---------------------------------------------------------------------------


def test_declaration_payload_has_all_required_fields() -> None:
    payload = describe_model_semantics(
        label_policy_id="label_policy_v3_b0b3724553b5",
        label_name="label_return_rank",
        horizon_days=5,
        price_basis="next_tradable_open",
        calibration="isotonic",
    ).to_payload()
    for key in (
        "label_policy_id",
        "label_name",
        "horizon_days",
        "price_basis",
        "output_kind",
        "calibration",
    ):
        assert key in payload
    assert payload["output_kind"] == OUTPUT_KIND_RANK_SCORE
    assert payload["horizon_days"] == 5
    assert payload["price_basis"] == "next_tradable_open"


def test_unregistered_basis_is_recorded_not_raised() -> None:
    """守卫在报告路径上：未登记 label 只记录原因，不得把链路打崩。"""
    semantics = describe_model_semantics(
        label_policy_id="label_policy_v1_e2afc1135a3f", label_basis="soup_10d_tp8_before_sl5"
    )
    assert semantics.output_kind == OUTPUT_KIND_UNKNOWN
    assert semantics.semantics_error  # 未登记 → 有原因
    # 未登记时不得授予任何概率化展示词
    assert semantics.display_terms_allowed == ()


def test_legacy_model_health_is_degraded_unverified() -> None:
    """legacy 只标记、不反转：可解析语义但无 OOS 校准证据 → degraded_unverified。"""
    semantics = describe_model_semantics(
        label_policy_id="label_policy_v3_b0b3724553b5", has_oos_calibration=False
    )
    assert semantics.legacy_model_health == MODEL_HEALTH_DEGRADED_UNVERIFIED


def test_health_ok_requires_event_semantics_and_oos_calibration() -> None:
    ok = describe_model_semantics(label_basis="soup", has_oos_calibration=True)
    assert ok.output_kind == OUTPUT_KIND_PROBABILITY
    assert ok.legacy_model_health == MODEL_HEALTH_OK
    assert UP_PROBABILITY_DISPLAY_TERM in ok.display_terms_allowed


# ---------------------------------------------------------------------------
# 展示口径守卫
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("term", FORBIDDEN_UP_PROBABILITY_TERMS)
def test_forbidden_terms_are_rejected_for_rank_score(term: str) -> None:
    semantics = describe_model_semantics(
        label_policy_id="label_policy_v3_b0b3724553b5", has_oos_calibration=True
    )
    assert semantics.output_kind == OUTPUT_KIND_RANK_SCORE
    result = guard_display_text(f"该股未来{term}为 62%", semantics)
    assert result["allowed"] is False
    assert any(str(item).startswith("forbidden_term:") for item in result["violations"])


def test_up_probability_wording_requires_oos_calibration_even_for_event_label() -> None:
    without = describe_model_semantics(label_basis="soup", has_oos_calibration=False)
    blocked = guard_display_text(f"{UP_PROBABILITY_DISPLAY_TERM} 55%", without)
    assert blocked["allowed"] is False

    with_cal = describe_model_semantics(label_basis="soup", has_oos_calibration=True)
    allowed = guard_display_text(f"{UP_PROBABILITY_DISPLAY_TERM} 55%", with_cal)
    assert allowed["allowed"] is True


def test_rank_score_output_note_explains_quantile_semantics() -> None:
    semantics = describe_model_semantics(label_policy_id="label_policy_v3_b0b3724553b5")
    assert "上尾" in semantics.display_note or "分位" in semantics.display_note
    assert semantics.display_terms_forbidden == FORBIDDEN_UP_PROBABILITY_TERMS


def test_unknown_semantics_forbids_any_probability_wording() -> None:
    semantics = describe_model_semantics(label_basis="not_registered")
    assert semantics.output_kind == OUTPUT_KIND_UNKNOWN
    assert "禁止" in semantics.display_note
    assert guard_display_text("上涨概率 60%", semantics)["allowed"] is False
