"""候选模型训练与"选股质量验收"口径的测试（改进计划 §3.3 / §4）。

重点验收的是那些**不许商量**的行为：模型只有两种、原生 booster 缺失就停、
段间必须留 embargo 让标签先成熟、校准窗方向为反就 raise、验收必须按交易日
分块做 bootstrap 而不是逐笔。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.labels.tail_net_profit import CAPTURE_OBSERVED, CAPTURE_REPLAYED
from stock_analyzer.models.tail_net_profit_trainer import (
    DEFAULT_LIGHTGBM_PARAMS,
    KIND_LIGHTGBM,
    KIND_LOGISTIC,
    MIN_SHADOW_MATURED_FILLS,
    MIN_SHADOW_TRADE_DAYS,
    DayOutcome,
    TailModelSpec,
    TailTrainingError,
    build_date_split,
    evaluate_selection_quality,
    shadow_readiness,
    train_tail_net_profit_model,
)

CONTRACT = DEFAULT_TREND_CONTRACT
FEATURES = ["excess_ret_20", "close_position", "atr14_pct"]
START = date(2026, 1, 5)


def _weekdays(count: int) -> list[date]:
    days: list[date] = []
    cursor = START
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor = cursor + timedelta(days=1)
    return days


def _rows(days: list[date], *, per_day: int = 12, flip_from: int | None = None,
          capture: str = CAPTURE_REPLAYED) -> list[dict]:
    """f1 高 → 更可能盈利；``flip_from`` 之后的日子反过来（模拟制度翻转）。"""
    rng = np.random.default_rng(7)
    rows: list[dict] = []
    for index, day in enumerate(days):
        flipped = flip_from is not None and index >= flip_from
        for slot in range(per_day):
            f1 = float(rng.uniform())
            positive = (f1 > 0.5) if not flipped else (f1 <= 0.5)
            rows.append({
                "entry_date": day,
                "symbol": f"{slot:06d}.SH",
                "label": 1 if positive else 0,
                "capture_mode": capture if index % 2 == 0 else CAPTURE_OBSERVED,
                FEATURES[0]: f1,
                FEATURES[1]: float(rng.uniform()),
                FEATURES[2]: float(rng.uniform(0.01, 0.05)),
            })
    return rows


def _identity_kwargs() -> dict:
    return {
        "model_id": "trend-tail-lgbm-2026q4",
        "training_commit": "1f970ac",
        "feature_compute_version": 5,
        "label_policy_id": "label_policy_v4_abc",
    }


# ---------------------------------------------------------------------------
# 切分：标签必须先成熟
# ---------------------------------------------------------------------------


def test_split_inserts_embargo_between_segments() -> None:
    days = _weekdays(60)
    split = build_date_split(days, embargo_sessions=CONTRACT.holding_days)
    positions = {day: index for index, day in enumerate(days)}
    assert positions[split.calibration_dates[0]] - positions[split.train_dates[-1]] >= 6
    assert positions[split.test_dates[0]] - positions[split.calibration_dates[-1]] >= 6
    assert not set(split.train_dates) & set(split.calibration_dates) & set(split.test_dates)


def test_split_rejects_too_short_history_and_missing_embargo() -> None:
    with pytest.raises(TailTrainingError):
        build_date_split(_weekdays(8), embargo_sessions=5)
    with pytest.raises(TailTrainingError, match="mature"):
        build_date_split(_weekdays(60), embargo_sessions=0)


# ---------------------------------------------------------------------------
# 只许两种模型，不许静默降级
# ---------------------------------------------------------------------------


def test_lightgbm_without_native_trainer_stops_instead_of_falling_back() -> None:
    with pytest.raises(TailTrainingError, match="must not fall back"):
        train_tail_net_profit_model(
            rows=_rows(_weekdays(90)), feature_names=FEATURES,
            spec=TailModelSpec(kind=KIND_LIGHTGBM), **_identity_kwargs(),
        )


def test_hyper_parameter_search_is_out_of_first_round_scope() -> None:
    with pytest.raises(TailTrainingError, match="existing production LightGBM"):
        TailModelSpec(kind=KIND_LIGHTGBM,
                      lightgbm_params={**DEFAULT_LIGHTGBM_PARAMS, "num_leaves": 128})


def test_unknown_model_kind_is_rejected() -> None:
    with pytest.raises(TailTrainingError, match="first round allows only"):
        TailModelSpec(kind="mlp")


def test_logistic_spec_rejects_a_booster_injection() -> None:
    with pytest.raises(TailTrainingError, match="only applies to the lightgbm"):
        train_tail_net_profit_model(
            rows=_rows(_weekdays(90)), feature_names=FEATURES,
            spec=TailModelSpec(kind=KIND_LOGISTIC), booster_trainer=lambda *a: None,
            **_identity_kwargs(),
        )


def test_non_reproducible_features_are_refused_before_any_sample_work() -> None:
    """§3.2"历史不可复现的信息不得混入训练"：门排在查样本之前。

    ``rows=[]`` 本会先撞上"no labelled rows"，这里仍报特征问题，证明训练根本没开始。
    """
    with pytest.raises(TailTrainingError, match="不可复现的信息源"):
        train_tail_net_profit_model(
            rows=[], feature_names=[*FEATURES, "news_sentiment"], **_identity_kwargs(),
        )
    with pytest.raises(TailTrainingError, match="未登记在特征契约里"):
        train_tail_net_profit_model(
            rows=[], feature_names=["composite_score"], **_identity_kwargs(),
        )


# ---------------------------------------------------------------------------
# 端到端：逻辑回归基线
# ---------------------------------------------------------------------------


def test_logistic_baseline_produces_auditable_artifact() -> None:
    artifact = train_tail_net_profit_model(
        rows=_rows(_weekdays(90)), feature_names=FEATURES, **_identity_kwargs(),
    )
    assert artifact["kind"] == KIND_LOGISTIC
    assert artifact["contract_digest"] == CONTRACT.digest()
    assert artifact["probability_field"] == "p_net_profit_5d_tail"
    assert artifact["label_policy_id"] == "label_policy_v4_abc"
    assert artifact["feature_compute_version"] == 5
    assert artifact["calibration_auc"] is not None and artifact["calibration_auc"] > 0.5
    assert artifact["artifact_digest"]
    assert artifact["metrics"]["reported_separately"][CAPTURE_REPLAYED]["n"] > 0
    assert artifact["metrics"]["reported_separately"][CAPTURE_OBSERVED]["n"] > 0
    assert set(artifact["metrics"]["capture_modes_in_test"]) == {
        CAPTURE_OBSERVED, CAPTURE_REPLAYED
    }


def test_artifact_digest_changes_with_runtime_identity() -> None:
    rows = _rows(_weekdays(90))
    base = train_tail_net_profit_model(rows=rows, feature_names=FEATURES,
                                       **_identity_kwargs())
    variant = train_tail_net_profit_model(
        rows=rows, feature_names=FEATURES, contract=CONTRACT,
        **{**_identity_kwargs(), "feature_compute_version": 6},
    )
    assert base["artifact_digest"] != variant["artifact_digest"]


def test_identity_fields_cannot_be_empty_or_unknown() -> None:
    rows = _rows(_weekdays(90))
    for key, bad in (
        ("model_id", ""),
        ("training_commit", ""),
        ("feature_compute_version", 0),
        ("label_policy_id", ""),
    ):
        kwargs = {**_identity_kwargs(), key: bad}
        with pytest.raises(TailTrainingError):
            train_tail_net_profit_model(rows=rows, feature_names=FEATURES, **kwargs)


def test_calibration_direction_reversal_stops_training() -> None:
    """校准窗方向为反时 raise —— isotonic 唯一诚实的解是常数，那是方向事实。"""
    days = _weekdays(90)
    flip_at = int(len(days) * 0.6) + CONTRACT.holding_days + 8
    with pytest.raises(TailTrainingError, match="direction is not positive"):
        train_tail_net_profit_model(
            rows=_rows(days, flip_from=flip_at), feature_names=FEATURES,
            **_identity_kwargs(),
        )


def test_unlabelled_or_single_class_input_stops() -> None:
    days = _weekdays(90)
    with pytest.raises(TailTrainingError, match="no labelled rows"):
        train_tail_net_profit_model(rows=[], feature_names=FEATURES, **_identity_kwargs())
    with pytest.raises(TailTrainingError, match="single-class|split has"):
        train_tail_net_profit_model(
            rows=[{**row, "label": 1} for row in _rows(days)],
            feature_names=FEATURES, **_identity_kwargs(),
        )
    with pytest.raises(TailTrainingError, match="train split has"):
        train_tail_net_profit_model(
            rows=_rows(_weekdays(90), per_day=2), feature_names=FEATURES,
            **_identity_kwargs(),
        )


def test_feature_incomplete_rows_are_excluded_and_reported_never_zero_filled() -> None:
    """特征缺失（计划 §4 工程验收项）：不进训练、不填 0，但每个缺失都要留痕。

    修之前它崩在 ``float(None)`` 上抛裸 ``TypeError``，整段训练没有结果也没有归因；
    填 0 更糟——0 会被模型当成真实观测值（§3.1「缺失不能被填零后当成有效信息」）。
    """
    days = _weekdays(90)
    rows = _rows(days)
    null_slots = set(range(0, len(rows), 37))
    nan_slots = set(range(5, len(rows), 41))
    for index in null_slots:
        rows[index][FEATURES[1]] = None
    for index in nan_slots:
        rows[index][FEATURES[2]] = float("nan")

    artifact = train_tail_net_profit_model(
        rows=rows, feature_names=FEATURES, **_identity_kwargs(),
    )

    completeness = artifact["feature_completeness"]
    assert completeness["zero_filled"] is False
    assert completeness["rows_excluded"] == len(null_slots | nan_slots)
    assert completeness["per_column"] == {FEATURES[1]: len(null_slots), FEATURES[2]: len(nan_slots)}
    assert completeness["rows_trainable"] == len(rows) - len(null_slots | nan_slots)
    # 排除不能只写在 completeness 里：测试段样本数必须真的少掉，否则等于没排除
    assert artifact["metrics"]["overall"]["n"] <= completeness["rows_trainable"]


def test_feature_source_totally_down_stops_with_a_column_breakdown() -> None:
    """整列不可用是数据源故障，不是零星滞后——必须停下来并说清是谁缺。"""
    rows = _rows(_weekdays(90))
    for row in rows:
        row[FEATURES[2]] = None

    with pytest.raises(TailTrainingError, match="feature source is down") as caught:
        train_tail_net_profit_model(rows=rows, feature_names=FEATURES, **_identity_kwargs())
    assert str(caught.value).count(FEATURES[2]) >= 1


def test_missing_value_outside_the_feature_contract_does_not_block() -> None:
    """未进契约的列不参与训练，它的缺失不该把整次训练挡下来。"""
    days = _weekdays(90)
    rows = _rows(days)
    for row in rows[::37]:
        row["sector_momentum"] = None

    artifact = train_tail_net_profit_model(
        rows=rows, feature_names=FEATURES, **_identity_kwargs(),
    )
    assert artifact["kind"] == KIND_LOGISTIC
    assert artifact["artifact_digest"]


def test_booster_path_trains_with_the_frozen_production_params() -> None:
    seen: dict = {}

    class Stub:
        def predict(self, features):
            return (features[:, 0] > 0.5).astype(float)

    def trainer(matrix, vector, params):
        seen["params"] = dict(params)
        seen["rows"] = int(matrix.shape[0])
        return Stub()

    artifact = train_tail_net_profit_model(
        rows=_rows(_weekdays(90)), feature_names=FEATURES,
        spec=TailModelSpec(kind=KIND_LIGHTGBM), booster_trainer=trainer,
        **_identity_kwargs(),
    )
    assert seen["params"] == DEFAULT_LIGHTGBM_PARAMS
    assert artifact["kind"] == KIND_LIGHTGBM
    assert artifact["metrics"]["overall"]["test_auc"] > 0.5


# ---------------------------------------------------------------------------
# 选股质量验收
# ---------------------------------------------------------------------------


def _outcomes(days: list[date], *, treatment_rate: float, baseline_rate: float,
              recommendations: int = 20, fills: int = 12,
              matured: int = 10) -> list[DayOutcome]:
    out: list[DayOutcome] = []
    for day in days:
        for arm, rate in (("baseline", baseline_rate), ("tail", treatment_rate)):
            wins = int(round(matured * rate))
            returns = tuple([0.05 if i < wins else -0.02 for i in range(matured)])
            out.append(DayOutcome(
                trade_date=day, arm=arm, recommendations=recommendations,
                fills=fills, matured_fills=matured, net_profits=wins,
                net_returns=returns,
            ))
    return out


def _verdict(days: list[date], **kwargs) -> dict:
    return evaluate_selection_quality(
        outcomes=_outcomes(days, **kwargs["rates"]),
        candidate_days=kwargs.get("candidate_days", len(days)),
        baseline_arm="baseline",
        treatment_arm="tail",
        bootstrap_draws=200,
    )


def test_acceptance_passes_only_with_5pp_and_positive_ci_and_folds() -> None:
    days = _weekdays(90)
    result = _verdict(days, rates={"treatment_rate": 0.58, "baseline_rate": 0.50})
    assert result["passed"] is True, result["failed_gates"]
    # round(10*0.58)=6 → 0.6 vs round(10*0.50)=5 → 0.5
    assert result["improvement_pp"] == pytest.approx(0.10)
    assert result["block_bootstrap"]["ci_low"] > 0.0
    assert result["block_bootstrap"]["unit"] == "trade_day"
    assert result["test_folds"] >= 4
    assert result["treatment"]["fill_rate"] == pytest.approx(0.6)
    assert result["treatment"]["capital_employed_cny"] == pytest.approx(
        20 * 90 * CONTRACT.reference_notional
    )


def test_acceptance_fails_when_improvement_is_below_five_points() -> None:
    result = _verdict(_weekdays(90), rates={"treatment_rate": 0.52, "baseline_rate": 0.50})
    assert result["passed"] is False
    assert "improvement_below_5pp" in result["failed_gates"]


def test_acceptance_reports_missing_baseline_fills_instead_of_faking_a_hit_rate() -> None:
    days = _weekdays(90)
    outcomes = _outcomes(days, treatment_rate=0.7, baseline_rate=0.5)
    outcomes = [
        DayOutcome(item.trade_date, item.arm, item.recommendations, item.fills, 0, 0, ())
        if item.arm == "baseline" else item for item in outcomes
    ]
    result = evaluate_selection_quality(
        outcomes=outcomes, candidate_days=len(days), baseline_arm="baseline",
        treatment_arm="tail", bootstrap_draws=100,
    )
    assert "baseline_has_no_fill_samples" in result["failed_gates"]
    assert result["baseline"]["net_profit_rate"] is None


def test_acceptance_blocks_when_there_are_too_few_test_folds() -> None:
    result = _verdict(_weekdays(30), rates={"treatment_rate": 0.7, "baseline_rate": 0.5})
    assert result["test_folds"] < 4
    assert any("test_folds" in gate for gate in result["failed_gates"])


def test_acceptance_rejects_worse_tail_loss() -> None:
    days = _weekdays(90)
    outcomes = []
    for day in days:
        outcomes.append(DayOutcome(day, "baseline", 20, 12, 10, 5,
                                   (0.05,) * 5 + (-0.03,) * 5))
        # 净盈利率提高了，但最差一笔从 -3% 恶化到 -25%
        outcomes.append(DayOutcome(day, "tail", 20, 12, 10, 8,
                                   (0.05,) * 8 + (-0.25,) * 2))
    result = evaluate_selection_quality(
        outcomes=outcomes, candidate_days=len(days), baseline_arm="baseline",
        treatment_arm="tail", bootstrap_draws=100,
    )
    assert "tail_loss_materially_worse" in result["failed_gates"]


def test_unpaired_days_are_excluded_rather_than_counted_as_zero() -> None:
    days = _weekdays(90)
    outcomes = _outcomes(days, treatment_rate=0.7, baseline_rate=0.5)
    outcomes = [item for item in outcomes if item.trade_date not in days[:10]]
    result = evaluate_selection_quality(
        outcomes=outcomes, candidate_days=len(days), baseline_arm="baseline",
        treatment_arm="tail", bootstrap_draws=100,
    )
    assert result["paired_days"] == 80
    assert result["trade_days"] == 80
    assert result["candidate_days"] == 90


# ---------------------------------------------------------------------------
# 影子验证门槛
# ---------------------------------------------------------------------------


def test_shadow_gate_requires_both_days_and_matured_fills() -> None:
    blocked_days = shadow_readiness(observed_trade_days=MIN_SHADOW_TRADE_DAYS - 1,
                                   matured_simulated_fills=500)
    assert blocked_days["ready_for_release_review"] is False
    assert blocked_days["state"] == "shadow"

    blocked_fills = shadow_readiness(observed_trade_days=MIN_SHADOW_TRADE_DAYS,
                                     matured_simulated_fills=MIN_SHADOW_MATURED_FILLS - 1)
    assert any("matured" in item for item in blocked_fills["blockers"])

    ready = shadow_readiness(observed_trade_days=MIN_SHADOW_TRADE_DAYS,
                             matured_simulated_fills=MIN_SHADOW_MATURED_FILLS)
    assert ready == {"ready_for_release_review": True, "blockers": [], "state": "review"}
