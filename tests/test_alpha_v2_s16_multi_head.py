"""S16 共享 Feature Matrix + 多 Head 阶段验收测试。

重点守四件事：

1. **"只跑一遍"是可验证属性**：矩阵构建 1 次、推理 1 次，多 Head 指纹一致；
2. **特征矩阵只能是 PIT 已证明的列**（S14 准入断言在矩阵构建处生效）；
3. **Head 语义不能串**：rank_score / expected_return / probability / risk_score 各归其位，
   未校准 Direction 不得被称"上涨概率"；
4. **Risk 不得回流成 Alpha**：收益/风险列是标签，任何一条都进不了特征集合。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _alpha_v2_research_helpers import DAYS, matcher, panel, walk  # noqa: E402

from stock_analyzer.alpha_v2.research.multi_head import (
    ALPHA_TARGET_TEMPLATE,
    CALIBRATION_ISOTONIC_OOS,
    CALIBRATION_NONE,
    HEAD_ALPHA_RANK,
    HEAD_DIRECTION,
    HEAD_EXPECTED_RETURN,
    HEAD_NAMES,
    HEAD_RISK,
    MAE_BREACH_COLUMN,
    OUTPUT_KIND_EXPECTED_RETURN,
    OUTPUT_KIND_PROBABILITY,
    OUTPUT_KIND_RANK_SCORE,
    OUTPUT_KIND_RISK_SCORE,
    BuildStats,
    HeadFitSpec,
    SharedFeatureMatrix,
    build_head_targets,
    build_shared_feature_matrix,
    calibrate_direction,
    fit_and_predict_heads,
    head_display_semantics,
    head_spec,
    matrix_fingerprint,
    multi_head_payload,
)
from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint, build_label_v2

FEATURES = tuple(f"f{index:02d}" for index in range(12))
DAYS_TOTAL = 40
PER_DAY = 60


def _synthetic_matrix_frame(*, seed: int = 20260918, signal: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for day_index in range(DAYS_TOTAL):
        day = DAYS[day_index].isoformat()
        features = rng.normal(0.0, 1.0, size=(PER_DAY, len(FEATURES)))
        # 未来超额收益由 f00 + f01 决定（含噪声），其余特征是纯噪声
        latent = signal * (0.6 * features[:, 0] + 0.4 * features[:, 1]) + rng.normal(
            0.0, 1.0, PER_DAY
        )
        excess_5d = 0.02 * latent
        for index in range(PER_DAY):
            row: dict[str, object] = {
                "decision_date": day,
                "symbol": f"{600000 + index:06d}",
                "executable": True,
                "excess_return_5d": float(excess_5d[index]),
                "net_return_5d": float(excess_5d[index] + 0.001),
                "excess_return_3d": float(excess_5d[index] * 0.6),
                "net_return_3d": float(excess_5d[index] * 0.6 + 0.001),
                "excess_return_10d": float(excess_5d[index] * 1.2),
                "net_return_10d": float(excess_5d[index] * 1.2 + 0.001),
                "excess_return_15d": float(excess_5d[index] * 1.4),
                "net_return_15d": float(excess_5d[index] * 1.4 + 0.001),
                "up_net_3d": bool(excess_5d[index] > -0.001),
                "up_net_5d": bool(excess_5d[index] > 0.0),
                "up_excess_3d": bool(excess_5d[index] > 0.001),
                "up_excess_5d": bool(excess_5d[index] > 0.001),
                "mae_3d": float(-abs(excess_5d[index]) - 0.01),
                "mae_5d": float(-abs(excess_5d[index]) - 0.015),
            }
            for column_index, column in enumerate(FEATURES):
                row[column] = float(features[index, column_index])
            rows.append(row)
    frame = pd.DataFrame(rows)
    frame["is_train"] = frame["decision_date"].isin(
        [DAYS[index].isoformat() for index in range(0, 28)]
    )
    frame["is_calibration"] = frame["decision_date"].isin(
        [DAYS[index].isoformat() for index in range(28, 34)]
    )
    frame["is_predict"] = frame["decision_date"].isin(
        [DAYS[index].isoformat() for index in range(34, 40)]
    )
    return build_head_targets(frame)


def _matrix(
    frame: pd.DataFrame | None = None, *, stats: BuildStats | None = None
) -> SharedFeatureMatrix:
    resolved = _synthetic_matrix_frame() if frame is None else frame
    targets = tuple(
        column
        for spec in (head_spec(name) for name in HEAD_NAMES)
        for column in spec.targets
        if column in resolved.columns
    )
    counters = stats if stats is not None else BuildStats(matrix_build_calls=1)
    return SharedFeatureMatrix(
        frame=resolved,
        feature_columns=FEATURES,
        target_columns=targets,
        stats=counters,
    )


# ---------------------------------------------------------------------------
# Head 契约
# ---------------------------------------------------------------------------


def test_head_specs_declare_expected_semantics() -> None:
    assert head_spec(HEAD_ALPHA_RANK).output_kind == OUTPUT_KIND_RANK_SCORE
    assert head_spec(HEAD_EXPECTED_RETURN).output_kind == OUTPUT_KIND_EXPECTED_RETURN
    assert head_spec(HEAD_DIRECTION).output_kind == OUTPUT_KIND_PROBABILITY
    assert head_spec(HEAD_DIRECTION).requires_calibration is True
    assert head_spec(HEAD_RISK).output_kind == OUTPUT_KIND_RISK_SCORE
    assert head_spec(HEAD_RISK).requires_calibration is False
    with pytest.raises(KeyError):
        head_spec("mystery_head")


def test_alpha_target_is_cross_sectional_rank_not_raw_return() -> None:
    frame = _synthetic_matrix_frame()
    target = ALPHA_TARGET_TEMPLATE.format(h=5)
    assert target in frame.columns
    for _, group in frame.groupby("decision_date"):
        values = group[target].dropna()
        assert values.min() == pytest.approx(1.0 / len(group), abs=1e-9)
        assert values.max() == pytest.approx(1.0, abs=1e-9)
    # 与原始超额收益完全不是一回事（rank 化）
    assert frame[target].corr(frame["excess_return_5d"]) > 0.9
    assert not np.allclose(frame[target], frame["excess_return_5d"])


def test_head_targets_include_mae_breach_flag() -> None:
    frame = _synthetic_matrix_frame()
    column = MAE_BREACH_COLUMN.format(h=5)
    assert column in frame.columns
    usable = frame[frame["mae_5d"].notna()]
    expected = (usable["mae_5d"] <= -0.05).astype(bool)
    assert usable[column].astype(bool).equals(expected)


# ---------------------------------------------------------------------------
# 共享矩阵
# ---------------------------------------------------------------------------


def test_matrix_fingerprint_changes_with_columns_and_rows() -> None:
    frame = _synthetic_matrix_frame()
    first = matrix_fingerprint(FEATURES, frame)
    assert first == matrix_fingerprint(FEATURES, frame)
    assert first != matrix_fingerprint(FEATURES[:-1], frame)
    assert first != matrix_fingerprint(FEATURES, frame.iloc[:-10])


def test_all_heads_report_same_matrix_fingerprint() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix)
    fingerprints = {output.matrix_fingerprint for output in result.heads.values()}
    assert fingerprints == {matrix.fingerprint}
    payload = multi_head_payload(result)
    assert payload["matrix"]["matrix_fingerprint"] == matrix.fingerprint


def test_build_stats_asserts_single_pass() -> None:
    stats = BuildStats(matrix_build_calls=1, prediction_calls=1)
    stats.assert_single_pass()
    with pytest.raises(AssertionError, match="只构建一次"):
        BuildStats(matrix_build_calls=4).assert_single_pass()
    with pytest.raises(AssertionError, match="一次批量推理"):
        BuildStats(matrix_build_calls=1, prediction_calls=4).assert_single_pass()


def test_fit_and_predict_heads_rejects_mismatched_matrix_fingerprint() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix)
    # 人为篡改一个 Head 的指纹 → 必须被共享矩阵守卫拦下
    result.heads[HEAD_RISK] = type(result.heads[HEAD_RISK])(
        name=HEAD_RISK,
        output_kind=OUTPUT_KIND_RISK_SCORE,
        columns=result.heads[HEAD_RISK].columns,
        calibration=CALIBRATION_NONE,
        matrix_fingerprint="deadbeefdeadbeef",
    )
    from stock_analyzer.alpha_v2.research.multi_head import _assert_shared_matrix

    with pytest.raises(AssertionError, match="共享矩阵"):
        _assert_shared_matrix(result.heads)


# ---------------------------------------------------------------------------
# 多 Head 推理
# ---------------------------------------------------------------------------


def test_all_heads_predict_in_one_pass() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix, spec=HeadFitSpec(min_train_rows=200))
    assert matrix.stats.matrix_build_calls == 1
    assert matrix.stats.prediction_calls == 1
    assert set(result.heads) == set(HEAD_NAMES)
    columns = set(result.predictions.columns)
    assert "alpha_rank_score" in columns
    assert "expected_excess_return_5d" in columns
    assert "p_up_net_5d" in columns
    assert "p_up_excess_3d" in columns
    assert "expected_mae_5d" in columns
    assert f"p_{MAE_BREACH_COLUMN.format(h=5)}" in columns


def test_alpha_rank_score_is_cross_sectional_percentile() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix)
    usable = result.predictions[result.predictions["alpha_rank_score"].notna()]
    assert not usable.empty
    counts = usable.groupby("decision_date")["alpha_rank_score"].count()
    assert counts.max() <= PER_DAY
    assert usable["alpha_rank_score"].between(0.0, 1.0, inclusive="both").all()


def test_direction_probabilities_are_uncalibrated_by_default() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix)
    output = result.heads[HEAD_DIRECTION]
    assert output.calibration == CALIBRATION_NONE
    assert output.calibrated_columns == ()


def test_alpha_head_learns_signal_better_than_noise() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix)
    predict = result.predictions.merge(
        matrix.frame[["decision_date", "symbol", "excess_return_5d", "is_predict"]],
        on=["decision_date", "symbol"],
    )
    usable = predict[predict["is_predict"]].dropna(subset=["alpha_rank_score", "excess_return_5d"])
    ic = usable["alpha_rank_score"].corr(usable["excess_return_5d"], method="spearman")
    assert ic > 0  # 合成数据里 f00/f01 有效 → Head A 必须学到正向排序


def test_head_fit_is_deterministic_for_same_seed() -> None:
    frame = _synthetic_matrix_frame()
    first = fit_and_predict_heads(
        matrix=_matrix(frame), spec=HeadFitSpec(seed=7, min_train_rows=200)
    )
    second = fit_and_predict_heads(
        matrix=_matrix(frame), spec=HeadFitSpec(seed=7, min_train_rows=200)
    )
    pd.testing.assert_frame_equal(first.predictions, second.predictions)


def test_insufficient_train_rows_leaves_head_unavailable() -> None:
    frame = _synthetic_matrix_frame()
    frame["is_train"] = False  # 完全没有训练样本
    matrix = _matrix(frame)
    result = fit_and_predict_heads(matrix=matrix, spec=HeadFitSpec(min_train_rows=200))
    assert result.predictions["alpha_rank_score"].isna().all()
    assert result.heads[HEAD_ALPHA_RANK].diagnostics["status"] == "insufficient_train_rows"


# ---------------------------------------------------------------------------
# 校准
# ---------------------------------------------------------------------------


def test_calibration_requires_disjoint_window() -> None:
    frame = _synthetic_matrix_frame()
    matrix = _matrix(frame)
    result = fit_and_predict_heads(matrix=matrix)
    with pytest.raises(ValueError, match="重叠"):
        calibrate_direction(
            result.predictions,
            probability_columns=["p_up_net_5d"],
            calibration_mask=frame["is_train"],
            labels=frame,
            train_mask=frame["is_train"],
        )


def test_calibration_produces_calibrated_column_and_diagnostics() -> None:
    frame = _synthetic_matrix_frame()
    matrix = _matrix(frame)
    result = fit_and_predict_heads(matrix=matrix)
    calibrated, diagnostics = calibrate_direction(
        result.predictions,
        probability_columns=["p_up_net_5d", "p_up_excess_5d"],
        calibration_mask=frame["is_calibration"] & result.predictions["p_up_net_5d"].notna(),
        labels=frame,
        train_mask=frame["is_train"],
    )
    assert "p_up_net_5d_calibrated" in calibrated.columns
    assert diagnostics["calibration_rows"] > 0
    assert diagnostics["calibrated"], diagnostics
    values = calibrated["p_up_net_5d_calibrated"].dropna()
    assert not values.empty
    assert values.between(0.0, 1.0, inclusive="both").all()


def test_calibration_skips_when_target_missing() -> None:
    frame = _synthetic_matrix_frame()
    matrix = _matrix(frame)
    result = fit_and_predict_heads(matrix=matrix)
    # 1) 列本身不存在 → column_not_present
    _, missing_column = calibrate_direction(
        result.predictions,
        probability_columns=["p_unknown_target_5d"],
        calibration_mask=frame["is_calibration"],
        labels=frame,
        train_mask=frame["is_train"],
    )
    assert missing_column["skipped"][0]["reason"] == "column_not_present"
    # 2) 列存在但标签列缺失 → target_unavailable（不静默用别的列校准）
    predictions = result.predictions.copy()
    predictions["p_orphan_5d"] = 0.5
    _, missing_target = calibrate_direction(
        predictions,
        probability_columns=["p_orphan_5d"],
        calibration_mask=frame["is_calibration"],
        labels=frame.drop(columns=["up_net_5d"]),
        train_mask=frame["is_train"],
    )
    assert missing_target["skipped"][0]["reason"] == "target_unavailable"


# ---------------------------------------------------------------------------
# 展示语义
# ---------------------------------------------------------------------------


def test_uncalibrated_direction_must_not_be_called_probability() -> None:
    semantics = head_display_semantics(head_spec(HEAD_DIRECTION), calibrated=False)
    assert semantics["may_call_probability"] is False
    assert semantics["calibration"] == CALIBRATION_NONE
    assert all("概率" not in term or "未校准" in term for term in semantics["display_terms"])


def test_calibrated_direction_may_be_called_probability() -> None:
    semantics = head_display_semantics(head_spec(HEAD_DIRECTION), calibrated=True)
    assert semantics["may_call_probability"] is True
    assert semantics["calibration"] == CALIBRATION_ISOTONIC_OOS
    assert "正收益概率" in semantics["display_terms"]


def test_non_direction_heads_never_claim_probability() -> None:
    for name in (HEAD_ALPHA_RANK, HEAD_EXPECTED_RETURN, HEAD_RISK):
        semantics = head_display_semantics(head_spec(name), calibrated=False)
        assert semantics["may_call_probability"] is False


def test_multi_head_payload_carries_semantics_for_every_head() -> None:
    matrix = _matrix()
    result = fit_and_predict_heads(matrix=matrix)
    payload = multi_head_payload(result)
    assert set(payload["head_semantics"]) == set(HEAD_NAMES)
    assert payload["head_semantics"][HEAD_ALPHA_RANK]["output_kind"] == OUTPUT_KIND_RANK_SCORE


# ---------------------------------------------------------------------------
# 与 S14 准入闸门联动
# ---------------------------------------------------------------------------


def test_matrix_build_rejects_unproven_feature_columns() -> None:
    built = panel(walk("600000", [10.0 + index * 0.05 for index in range(40)]))
    decisions = [DecisionPoint("600000", DAYS[30])]
    features = pd.DataFrame(
        {
            "decision_date": [DAYS[30].isoformat()],
            "symbol": ["600000"],
            "ma20": [10.5],
            "bg_roe": [0.15],  # financial_pit 组：未证明 → 必须被拒
        }
    )
    with pytest.raises(ValueError, match="fail-closed"):
        build_shared_feature_matrix(panel=built, decisions=decisions, feature_frame=features)


def test_matrix_build_rejects_outcome_columns_as_features() -> None:
    built = panel(walk("600000", [10.0 + index * 0.05 for index in range(40)]))
    decisions = [DecisionPoint("600000", DAYS[30])]
    features = pd.DataFrame(
        {
            "decision_date": [DAYS[30].isoformat()],
            "symbol": ["600000"],
            "ma20": [10.5],
            "excess_return_5d": [0.05],  # 收益列当特征 = 直接把答案喂进去
        }
    )
    with pytest.raises(ValueError, match="fail-closed"):
        build_shared_feature_matrix(panel=built, decisions=decisions, feature_frame=features)


def test_matrix_build_keeps_only_safe_columns_and_counts_once() -> None:
    built = panel(
        [
            bar
            for symbol in ("600001", "600002")
            for bar in walk(symbol, [10.0 + i * 0.05 for i in range(40)])
        ]
    )
    decisions = [DecisionPoint(symbol, DAYS[30]) for symbol in ("600001", "600002")]
    features = pd.DataFrame(
        {
            "decision_date": [DAYS[30].isoformat()] * 2,
            "symbol": ["600001", "600002"],
            "ma20": [10.5, 10.4],
            "rsi14": [55.0, 45.0],
        }
    )
    stats = BuildStats()
    matrix = build_shared_feature_matrix(
        panel=built, decisions=decisions, feature_frame=features, stats=stats
    )
    assert stats.matrix_build_calls == 1
    assert stats.feature_build_calls == 0  # 传入了已算好的特征
    assert set(matrix.feature_columns) == {"ma20", "rsi14"}
    assert matrix.diagnostics["rejected_feature_columns"] == 0
    stats.assert_single_pass()


def test_risk_and_return_columns_never_enter_feature_set() -> None:
    """结构守卫：任何收益/风险/标签列都不可能成为特征（S14 黑名单 + 准入断言）。"""
    matrix = _matrix()
    forbidden = {
        "excess_return_5d",
        "net_return_3d",
        "mae_5d",
        "up_net_5d",
        ALPHA_TARGET_TEMPLATE.format(h=5),
        MAE_BREACH_COLUMN.format(h=5),
    }
    assert forbidden.isdisjoint(set(matrix.feature_columns))


def test_end_to_end_matrix_with_real_outcomes() -> None:
    symbols = [f"60001{index}" for index in range(4)]
    bars = []
    for index, symbol in enumerate(symbols):
        bars.extend(walk(symbol, [10.0 * (1.0 + 0.004 * index * step) for step in range(40)]))
    built = panel(bars)
    decisions = [DecisionPoint(symbol, DAYS[30]) for symbol in symbols]
    outcomes = build_label_v2(
        panel=built,
        decisions=decisions,
        matcher=matcher(),
        price_mode="raw",
        price_mode_certified=True,
    ).frame
    features = pd.DataFrame(
        {
            "decision_date": [DAYS[30].isoformat()] * len(symbols),
            "symbol": symbols,
            "ma20": [10.0 + 0.1 * index for index in range(len(symbols))],
            "rsi14": [50.0 + index for index in range(len(symbols))],
        }
    )
    matrix = build_shared_feature_matrix(
        panel=built, decisions=decisions, outcomes=outcomes, feature_frame=features
    )
    assert len(matrix.frame) == len(symbols)
    assert ALPHA_TARGET_TEMPLATE.format(h=5) in matrix.frame.columns
    assert MAE_BREACH_COLUMN.format(h=5) in matrix.frame.columns
