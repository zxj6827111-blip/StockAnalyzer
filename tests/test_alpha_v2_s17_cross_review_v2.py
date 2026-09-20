"""S17 Cross Review V2（分歧观测层）阶段验收测试。

核心守住：**没有证据就不得把分歧变成门**；Legacy 绝对概率门原样不动。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _alpha_v2_research_helpers import DAYS  # noqa: E402

from stock_analyzer.alpha_v2.research.cross_review_v2 import (
    LEGACY_CROSS_REVIEW_KEYS,
    POLICY_HARD_GATE_CANDIDATE,
    POLICY_OBSERVATION_ONLY,
    DisagreementSpec,
    assert_no_hard_gate,
    compute_disagreement,
    cross_review_observation_columns,
    disagreement_evidence,
    legacy_cross_review_policy,
)
from stock_analyzer.alpha_v2.research.multi_head import (
    ALPHA_TARGET_TEMPLATE,
    HEAD_NAMES,
    BuildStats,
    HeadFitSpec,
    SharedFeatureMatrix,
    build_head_targets,
    head_spec,
)
from stock_analyzer.config import AlphaV2Config, load_config

FEATURES = tuple(f"f{index:02d}" for index in range(10))
DAYS_TOTAL = 40
PER_DAY = 60


def _matrix_frame(*, seed: int = 4242, noise: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for day_index in range(DAYS_TOTAL):
        day = DAYS[day_index].isoformat()
        features = rng.normal(0.0, 1.0, size=(PER_DAY, len(FEATURES)))
        latent = features[:, 0] + rng.normal(0.0, noise, PER_DAY)
        excess = 0.02 * latent
        for index in range(PER_DAY):
            row: dict[str, object] = {
                "decision_date": day,
                "symbol": f"{600000 + index:06d}",
                "executable": True,
                "excess_return_5d": float(excess[index]),
                "net_return_5d": float(excess[index] + 0.001),
                "up_net_5d": bool(excess[index] > 0),
            }
            for column_index, column in enumerate(FEATURES):
                row[column] = float(features[index, column_index])
            rows.append(row)
    frame = pd.DataFrame(rows)
    frame["is_train"] = frame["decision_date"].isin(
        [DAYS[index].isoformat() for index in range(0, 28)]
    )
    frame["is_predict"] = frame["decision_date"].isin(
        [DAYS[index].isoformat() for index in range(28, 40)]
    )
    return build_head_targets(frame)


def _matrix(frame: pd.DataFrame) -> SharedFeatureMatrix:
    targets = tuple(
        column
        for spec in (head_spec(name) for name in HEAD_NAMES)
        for column in spec.targets
        if column in frame.columns
    )
    return SharedFeatureMatrix(
        frame=frame,
        feature_columns=FEATURES,
        target_columns=targets,
        stats=BuildStats(matrix_build_calls=1),
    )


# ---------------------------------------------------------------------------
# Legacy 不动
# ---------------------------------------------------------------------------


def test_legacy_cross_review_thresholds_are_reported_unchanged() -> None:
    config = load_config("config/default.yaml")
    payload = legacy_cross_review_policy(config)
    assert payload["modified_by_alpha_v2"] is False
    for key in LEGACY_CROSS_REVIEW_KEYS:
        assert key in payload
    # 与 S00 冻结的基线一致（改任何一项都会在 S00 golden 测试里失败）
    assert payload["p_lgbm_min"] == 0.60
    assert payload["p_xgb_min"] == 0.55


def test_alpha_v2_config_does_not_expose_cross_review_knobs() -> None:
    """结构守卫：V2 配置块里不允许出现任何 Legacy 阈值旋钮。"""
    fields = set(AlphaV2Config.model_fields)
    assert not {key for key in LEGACY_CROSS_REVIEW_KEYS if key in fields}


# ---------------------------------------------------------------------------
# 观测量
# ---------------------------------------------------------------------------


def test_disagreement_observation_columns_are_produced() -> None:
    frame = _matrix_frame()
    result = compute_disagreement(matrix=_matrix(frame), fit=HeadFitSpec(min_train_rows=200))
    for column in cross_review_observation_columns():
        assert column in result.frame.columns, column
    observed = result.frame["rank_disagreement"].dropna()
    assert not observed.empty
    assert (observed >= 0).all()
    assert (observed <= 1.0 + 1e-9).all()


def test_rank_pct_is_cross_sectional() -> None:
    frame = _matrix_frame()
    result = compute_disagreement(matrix=_matrix(frame))
    usable = result.frame.dropna(subset=["lgbm_rank_pct"])
    assert usable["lgbm_rank_pct"].between(0.0, 1.0, inclusive="both").all()
    assert usable["lgbm_rank_pct"].groupby(usable["decision_date"]).max().max() == pytest.approx(
        1.0
    )


def test_prob_disagreement_requires_default_probability_column() -> None:
    frame = _matrix_frame()
    result = compute_disagreement(matrix=_matrix(frame))
    assert "prob_disagreement" in result.frame.columns
    values = result.frame["prob_disagreement"].dropna()
    assert not values.empty
    assert (values >= 0).all() and (values <= 1.0 + 1e-9).all()


def test_disagreement_requires_training_rows() -> None:
    frame = _matrix_frame()
    frame["is_train"] = False
    with pytest.raises(ValueError, match="训练样本不足"):
        compute_disagreement(matrix=_matrix(frame))


def test_disagreement_rejects_missing_target_column() -> None:
    frame = _matrix_frame().drop(columns=[ALPHA_TARGET_TEMPLATE.format(h=5)])
    with pytest.raises(ValueError, match="没有可用的排序目标列"):
        compute_disagreement(matrix=_matrix(frame))


def test_disagreement_allows_explicit_target_override() -> None:
    """显式换目标列是允许的（留痕在 spec 里），但换了就得真的有那一列。"""
    frame = _matrix_frame()
    result = compute_disagreement(
        matrix=_matrix(frame),
        spec=DisagreementSpec(target_column="excess_return_5d"),
    )
    assert result.diagnostics["lgbm"]["train_rows"] > 0


def test_disagreement_output_is_deterministic() -> None:
    frame = _matrix_frame()
    first = compute_disagreement(matrix=_matrix(frame), fit=HeadFitSpec(seed=11))
    second = compute_disagreement(matrix=_matrix(frame), fit=HeadFitSpec(seed=11))
    pd.testing.assert_frame_equal(first.frame, second.frame)


# ---------------------------------------------------------------------------
# 证据与策略
# ---------------------------------------------------------------------------


def _evidence_fixture(*, disagree_means_worse: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = _matrix_frame(seed=99)
    disagreement = compute_disagreement(matrix=_matrix(frame)).frame
    outcomes = frame[["decision_date", "symbol", "excess_return_5d", "executable"]].copy()
    if disagree_means_worse:
        # 人为让高分歧样本的未来收益显著更差（构造"有证据"的场景）
        merged = disagreement.merge(outcomes, on=["decision_date", "symbol"], how="left")
        merged["__bucket"] = merged.groupby("decision_date")["rank_disagreement"].transform(
            lambda values: pd.qcut(values.rank(method="first"), 3, labels=False, duplicates="drop")
        )
        merged.loc[merged["__bucket"] == 2, "excess_return_5d"] = -0.05
        merged.loc[merged["__bucket"] == 0, "excess_return_5d"] = 0.02
        outcomes = merged[["decision_date", "symbol", "excess_return_5d", "executable"]]
    return disagreement, outcomes


def test_evidence_stays_observation_only_before_sample_gate() -> None:
    disagreement, outcomes = _evidence_fixture(disagree_means_worse=True)
    payload = disagreement_evidence(disagreement, outcomes)
    assert payload["status"] == "ok"
    assert payload["high_bucket_worse"] is True
    # 12 个成熟日期远不到 60 → 只能观测
    assert payload["evidence_sufficient"] is False
    assert payload["policy"] == POLICY_OBSERVATION_ONLY
    assert payload["research_gate"] == "insufficient"


def test_evidence_promotes_to_candidate_only_with_gate_and_negative_direction() -> None:
    disagreement, outcomes = _evidence_fixture(disagree_means_worse=True)
    payload = disagreement_evidence(
        disagreement, outcomes, spec=DisagreementSpec(evidence_min_dates=5)
    )
    assert payload["policy"] == POLICY_HARD_GATE_CANDIDATE
    assert payload["policy_note"].startswith("即使升级")
    assert payload["paired_extremes"]["status"] == "ok"
    assert payload["paired_extremes"]["mean_delta"] < 0


def test_evidence_not_promoted_when_high_bucket_is_not_worse() -> None:
    disagreement, outcomes = _evidence_fixture(disagree_means_worse=False)
    payload = disagreement_evidence(
        disagreement, outcomes, spec=DisagreementSpec(evidence_min_dates=5)
    )
    assert payload["policy"] == POLICY_OBSERVATION_ONLY


def test_evidence_handles_empty_overlap() -> None:
    disagreement, outcomes = _evidence_fixture(disagree_means_worse=True)
    payload = disagreement_evidence(disagreement.head(0), outcomes)
    assert payload["status"] == "no_data"
    assert payload["policy"] == POLICY_OBSERVATION_ONLY


def test_evidence_excludes_non_executable_rows() -> None:
    disagreement, outcomes = _evidence_fixture(disagree_means_worse=True)
    outcomes = outcomes.copy()
    outcomes["executable"] = False
    payload = disagreement_evidence(disagreement, outcomes)
    assert payload["status"] == "no_data"


def test_assert_no_hard_gate_rejects_unknown_policy() -> None:
    assert_no_hard_gate({"policy": POLICY_OBSERVATION_ONLY})
    assert_no_hard_gate({"policy": POLICY_HARD_GATE_CANDIDATE})
    with pytest.raises(ValueError, match="unsupported"):
        assert_no_hard_gate({"policy": "reject_all_disagreement"})


def test_spec_payload_documents_observational_policy() -> None:
    payload = DisagreementSpec().to_payload()
    assert payload["policy"].startswith("observation_only")
    assert payload["rank_disagreement_definition"] == "abs(lgbm_rank_pct - xgb_rank_pct)"
    assert "abs(lgbm_prob - xgb_prob)" == payload["prob_disagreement_definition"]


def test_bucket_helper_does_not_invent_buckets_on_thin_days() -> None:
    from stock_analyzer.alpha_v2.research.cross_review_v2 import _bucket

    values = pd.Series([0.1, 0.2])
    assert _bucket(values, 3).isna().all()


def test_disagreement_observations_do_not_touch_legacy_notification() -> None:
    """结构守卫：V2 观测层不 import 任何通知/正式结果模块。"""
    import pathlib

    source = pathlib.Path("src/stock_analyzer/alpha_v2/research/cross_review_v2.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("notification", "feishu", "final_signal", "order", "broker"):
        assert forbidden not in source.lower(), forbidden
    assert np is not None
