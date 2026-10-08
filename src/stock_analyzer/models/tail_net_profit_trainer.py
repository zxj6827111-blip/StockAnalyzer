"""``p_net_profit_5d_tail`` 的候选模型训练与"选股质量是否真变好"的验收口径。

改进计划 §3.3 与 §4 的两条硬要求在这里落地：

1. **模型只许两种**：逻辑回归基线，或用现有 LightGBM 参数（不搜网格、不扩模型）。
   原生 booster 不可用或训练失败时**直接停止**，不静默换成别的模型。
2. **分段必须让标签先成熟**：训练 / 校准 / 测试按交易日滚动切分，段间留 embargo，
   长度至少是契约持有期，否则后一段的样本标签还没走完持仓期就被前一段"偷看"了。

验收部分实现计划 §4 的判定：净盈利率较**匹配基线**提高 ≥ 5 个百分点，且
**按交易日分块 bootstrap** 的差值置信区间下界 > 0；平均净收益为正；尾部亏损
不得明显恶化。分块而不是逐笔：同一天推荐的股票高度相关，逐笔重抽会低估方差。

真实观察（``observed_snapshot``）与事后重建（``replayed_recompute``）永远分开报告——
本项目已实测两者前向结果差一个量级，混算会失真。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    TrendStrategyContract,
)
from stock_analyzer.labels.tail_net_profit import CAPTURE_OBSERVED, CAPTURE_REPLAYED
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.fallback import LogisticProbModel

KIND_LOGISTIC = "logistic_baseline"
KIND_LIGHTGBM = "lightgbm_existing_params"
_KINDS = (KIND_LOGISTIC, KIND_LIGHTGBM)

#: 与 ``models/adapters.py:76-83`` 一致的生产 LightGBM 参数。写在这里是为了让
#: "第一轮不搜模型"这句话可以被测试钉住，而不是靠口头承诺。
DEFAULT_LIGHTGBM_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "verbose": -1,
    "num_boost_round": 80,
}

MIN_IMPROVEMENT_PP = 0.05
MIN_SHADOW_TRADE_DAYS = 60
MIN_SHADOW_MATURED_FILLS = 100
MIN_TEST_FOLDS = 4


class TailTrainingError(RuntimeError):
    """训练前置条件不成立。停止，不降级、不换模型、不放宽阈值。"""


@dataclass(frozen=True)
class TailModelSpec:
    kind: str = KIND_LOGISTIC
    learning_rate: float = 0.05
    epochs: int = 200
    l2: float = 1e-3
    seed: int = 42
    min_train_samples: int = 200
    min_calibration_samples: int = 50
    min_test_samples: int = 50
    lightgbm_params: Mapping[str, Any] = field(
        default_factory=lambda: dict(DEFAULT_LIGHTGBM_PARAMS)
    )

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise TailTrainingError(
                f"unknown spec.kind {self.kind!r}; first round allows only {list(_KINDS)}"
            )
        if self.kind == KIND_LIGHTGBM and dict(self.lightgbm_params) != DEFAULT_LIGHTGBM_PARAMS:
            raise TailTrainingError(
                "first round must use the existing production LightGBM parameters; "
                "hyper-parameter search is out of scope"
            )


@dataclass(frozen=True)
class DateSplit:
    train_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    test_dates: tuple[date, ...]
    embargo_sessions: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "train_dates": len(self.train_dates),
            "calibration_dates": len(self.calibration_dates),
            "test_dates": len(self.test_dates),
            "embargo_sessions": self.embargo_sessions,
            "train_last": str(self.train_dates[-1]) if self.train_dates else None,
            "calibration_first": str(self.calibration_dates[0]) if self.calibration_dates else None,
            "test_first": str(self.test_dates[0]) if self.test_dates else None,
        }


def build_date_split(
    trade_dates: Sequence[date],
    *,
    train_ratio: float = 0.6,
    calibration_ratio: float = 0.2,
    embargo_sessions: int = DEFAULT_TREND_CONTRACT.holding_days,
) -> DateSplit:
    """按**交易日**切分并在段间插入 embargo，保证标签在后段开始前已成熟。

    段之间跳过 ``embargo_sessions`` 个交易日：否则前段最后一天入选的样本，其持仓期
    会跨进后段（契约持有 5 日），后段就等于偷看了未成熟标签（计划 §4"标签必须在
    后续阶段开始前真实成熟"）。
    """
    ordered = sorted({day for day in trade_dates})
    if embargo_sessions < 1:
        raise TailTrainingError(
            "embargo_sessions must be >= 1: labels must mature before the next segment"
        )
    total = len(ordered)
    train_end = int(math.floor(total * train_ratio))
    calibration_start = train_end + int(embargo_sessions)
    calibration_end = calibration_start + int(math.floor(total * calibration_ratio))
    test_start = calibration_end + int(embargo_sessions)
    if train_end < 1 or calibration_start >= calibration_end or calibration_end >= total:
        raise TailTrainingError(
            f"{total} trade dates cannot fill train/calibration segments with "
            f"embargo={embargo_sessions} (train_end={train_end}, "
            f"calibration={calibration_start}..{calibration_end})"
        )
    if test_start >= total:
        raise TailTrainingError(
            f"embargo={embargo_sessions} leaves no test segment out of {total} dates"
        )
    return DateSplit(
        train_dates=tuple(ordered[:train_end]),
        calibration_dates=tuple(ordered[calibration_start:calibration_end]),
        test_dates=tuple(ordered[test_start:]),
        embargo_sessions=int(embargo_sessions),
    )


def _observed_session_gap(dates: Sequence[Any], left: Any, right: Any) -> int:
    """样本里真实存在的、落在两个段之间的交易日数（段相邻时为 0）。"""
    if right <= left:
        return 0
    return len({day for day in dates if day is not None and left < day < right})


def _require_injected_split(
    split: DateSplit, dates: Sequence[Any], *, contract: TrendStrategyContract
) -> None:
    """注入的折边界必须自带 embargo。

    训练/校准段最后一天与后段首日之间至少要隔着 ``holding_days`` 个**观察到的**交易日，
    否则后段就是在偷看还没成熟的标签（计划 §4"标签必须在后续阶段开始前真实成熟"）。
    数不满就失败，不做"大概是够的"这种推断。
    """
    required = max(1, int(contract.holding_days))
    if int(split.embargo_sessions) < required:
        raise TailTrainingError(
            f"injected split embargo={split.embargo_sessions} < {required} holding sessions"
        )
    for name, prior, following in (
        ("train→calibration", split.train_dates, split.calibration_dates),
        ("calibration→test", split.calibration_dates, split.test_dates),
    ):
        if not prior or not following:
            raise TailTrainingError(f"injected split has an empty segment at {name}")
        gap = _observed_session_gap(dates, prior[-1], following[0])
        if gap < required:
            raise TailTrainingError(
                f"injected split gap {name}={gap} observed trade days < {required}"
            )


def _auc(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    pairs = [(float(value), int(label)) for value, label in zip(scores, labels, strict=True)]
    positives = [value for value, label in pairs if label == 1]
    negatives = [value for value, label in pairs if label == 0]
    if not positives or not negatives:
        return None
    wins = sum(
        1.0 if pos > neg else 0.5 if pos == neg else 0.0
        for pos in positives
        for neg in negatives
    )
    return wins / (len(positives) * len(negatives))


def train_tail_net_profit_model(
    *,
    rows: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    spec: TailModelSpec | None = None,
    label_field: str = "label",
    date_field: str = "entry_date",
    probability_field: str = "p_net_profit_5d_tail",
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    model_id: str,
    training_commit: str,
    feature_compute_version: int,
    label_policy_id: str,
    booster_trainer: Any | None = None,
    split: DateSplit | None = None,
) -> dict[str, Any]:
    """训练一个与选股目标一致的候选模型；返回可注册的工件 dict（不落盘）。

    ``booster_trainer`` 是 LightGBM 路径注入点：必须是能产出 ``predict`` 对象的
    可调用体。为 None 且 spec 要求 LightGBM 时**直接失败**——本模块不 import
    lightgbm，也不许悄悄改用逻辑回归顶替。

    ``split`` 由滚动前推验证注入：折边界由调用方决定时，标签成熟/embargo 的核对
    也只能在那一组日期上做，不接受"训练时再自己切一刀"。
    """
    resolved_spec = spec or TailModelSpec()
    if resolved_spec.kind == KIND_LOGISTIC and booster_trainer is not None:
        raise TailTrainingError("booster_trainer only applies to the lightgbm spec")
    if resolved_spec.kind == KIND_LIGHTGBM and booster_trainer is None:
        raise TailTrainingError(
            "native booster trainer is unavailable; first round must not fall back "
            "to another model"
        )
    if not str(model_id).strip():
        raise TailTrainingError("model_id must not be empty")
    if not str(training_commit).strip():
        raise TailTrainingError("training_commit must not be empty (identity cannot be unknown)")
    if int(feature_compute_version) <= 0:
        raise TailTrainingError("feature_compute_version must be positive")
    if not str(label_policy_id).strip():
        raise TailTrainingError("label_policy_id must not be empty")

    usable = [
        row for row in rows
        if row.get(label_field) in (0, 1, 0.0, 1.0)
        and row.get(date_field) is not None
        and all(name in row for name in feature_names)
    ]
    if not usable:
        raise TailTrainingError("no labelled rows: every candidate sample is untrained")
    dates = [row[date_field] for row in usable]
    if split is None:
        split = build_date_split(
            dates, embargo_sessions=max(1, int(contract.holding_days))
        )
    else:
        _require_injected_split(split, dates, contract=contract)
    train_rows = [row for row in usable if row[date_field] in set(split.train_dates)]
    calib_rows = [row for row in usable if row[date_field] in set(split.calibration_dates)]
    test_rows = [row for row in usable if row[date_field] in set(split.test_dates)]
    for name, bucket, minimum in (
        ("train", train_rows, resolved_spec.min_train_samples),
        ("calibration", calib_rows, resolved_spec.min_calibration_samples),
        ("test", test_rows, resolved_spec.min_test_samples),
    ):
        if len(bucket) < minimum:
            raise TailTrainingError(
                f"{name} split has {len(bucket)} rows < required {minimum}"
            )
    for name, bucket in (("train", train_rows), ("calibration", calib_rows)):
        if len({int(row[label_field]) for row in bucket}) < 2:
            raise TailTrainingError(f"{name} split is single-class; direction cannot be assessed")

    matrix_train = np.asarray([[float(row[f]) for f in feature_names] for row in train_rows])
    vector_train = np.asarray([float(row[label_field]) for row in train_rows])
    matrix_calib = np.asarray([[float(row[f]) for f in feature_names] for row in calib_rows])
    vector_calib = np.asarray([float(row[label_field]) for row in calib_rows])
    matrix_test = np.asarray([[float(row[f]) for f in feature_names] for row in test_rows])

    if resolved_spec.kind == KIND_LOGISTIC:
        model = LogisticProbModel(
            learning_rate=resolved_spec.learning_rate,
            epochs=resolved_spec.epochs,
            l2=resolved_spec.l2,
            seed=resolved_spec.seed,
        )
        model.fit(matrix_train, vector_train)
        predict = model.predict_proba
    else:
        model = booster_trainer(
            matrix_train, vector_train, dict(resolved_spec.lightgbm_params)
        )
        predict = lambda features: _booster_predict(model, features)  # noqa: E731

    raw_calib = [float(value) for value in predict(matrix_calib)]
    calibration_auc = _auc(raw_calib, [int(value) for value in vector_calib])
    if calibration_auc is not None and calibration_auc <= 0.5:
        # isotonic 保序在方向反了的窗上唯一诚实的解是常数：这是方向事实，不是校准器缺陷。
        raise TailTrainingError(
            f"calibration window direction is not positive (raw AUC={calibration_auc:.4f}); "
            "training must stop instead of emitting a collapsed probability model"
        )
    calibrator = IsotonicCalibrator()
    calibrator.fit(np.asarray(raw_calib), vector_calib)

    raw_test = [float(value) for value in predict(matrix_test)]
    calibrated_test = [float(value) for value in calibrator.predict(np.asarray(raw_test))]
    metrics = _test_metrics(
        test_rows=test_rows,
        label_field=label_field,
        raw=raw_test,
        calibrated=calibrated_test,
        probability_field=probability_field,
    )
    payload = {
        "model_id": model_id,
        "kind": resolved_spec.kind,
        "feature_names": list(feature_names),
        "training_commit": training_commit,
        "feature_compute_version": int(feature_compute_version),
        "label_policy_id": label_policy_id,
        "contract_version": contract.contract_version,
        "contract_digest": contract.digest(),
        "probability_field": probability_field,
        "split": split.as_dict(),
        "calibration_auc": calibration_auc,
        "metrics": metrics,
        "model": model,
        "calibrator": calibrator,
    }
    payload["artifact_digest"] = _digest(payload)
    return payload


def _booster_predict(model: Any, features: np.ndarray) -> Any:
    predict = getattr(model, "predict", None)
    if not callable(predict):
        raise TailTrainingError("booster trainer must return an object with .predict()")
    return predict(features)


def _test_metrics(
    *,
    test_rows: Sequence[Mapping[str, Any]],
    label_field: str,
    raw: Sequence[float],
    calibrated: Sequence[float],
    probability_field: str,
) -> dict[str, Any]:
    """测试段指标。observed 与 replayed **分开**给，绝不合并成一个总数。"""
    labels = [int(row[label_field]) for row in test_rows]
    overall = {
        "n": len(test_rows),
        "test_auc": _auc(calibrated, labels),
        "test_auc_raw": _auc(raw, labels),
        "base_rate": float(np.mean(labels)) if labels else None,
        "calibration_error": _calibration_error(calibrated, labels),
        "mean_score": float(np.mean(calibrated)) if calibrated else None,
    }
    per_mode = {}
    for mode in (CAPTURE_OBSERVED, CAPTURE_REPLAYED):
        picked = [(score, label) for row, score, label in zip(
            test_rows, calibrated, labels, strict=True
        ) if str(row.get("capture_mode")) == mode]
        if picked:
            scores = [value for value, _ in picked]
            tag_labels = [value for _, value in picked]
            per_mode[mode] = {
                "n": len(picked),
                "test_auc": _auc(scores, tag_labels),
                "base_rate": float(np.mean(tag_labels)),
                "mean_score": float(np.mean(scores)),
            }
        else:
            per_mode[mode] = {"n": 0}
    return {
        "overall": overall,
        "reported_separately": per_mode,
        "capture_modes_in_test": sorted(
            {str(row.get("capture_mode")) for row in test_rows} - {"None"}
        ),
        "probability_field": probability_field,
    }


def _calibration_error(
    scores: Sequence[float], labels: Sequence[int], bins: int = 10
) -> float | None:
    if not scores or sum(labels) == 0 or sum(labels) == len(labels):
        return None
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    weight = 0.0
    for index in range(bins):
        mask = (np.asarray(scores) >= edges[index]) & (np.asarray(scores) < edges[index + 1])
        count = int(mask.sum())
        if count == 0:
            continue
        total += count * abs(float(np.asarray(scores)[mask].mean())
                             - float(np.asarray(labels)[mask].mean()))
        weight += count
    return total / weight if weight else None


# ---------------------------------------------------------------------------
# 选股质量验收（计划 §4）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayOutcome:
    """一个交易日上、某一方案（新模型 / 匹配基线）的最终推荐结果。"""

    trade_date: date
    arm: str
    recommendations: int
    fills: int
    matured_fills: int
    net_profits: int
    net_returns: tuple[float, ...]

    @property
    def net_profit_rate(self) -> float | None:
        return (self.net_profits / self.matured_fills) if self.matured_fills else None

    @property
    def mean_net_return(self) -> float | None:
        return float(np.mean(self.net_returns)) if self.net_returns else None


def evaluate_selection_quality(
    *,
    outcomes: Sequence[DayOutcome],
    candidate_days: int,
    baseline_arm: str,
    treatment_arm: str,
    min_improvement_pp: float = MIN_IMPROVEMENT_PP,
    bootstrap_draws: int = 2000,
    seed: int = 42,
    tail_loss_tolerance_pp: float = 0.02,
) -> dict[str, Any]:
    """净盈利率 / 净收益 / 成交率 / 尾部亏损 / 覆盖率 / 资金占用的联合判定。

    判定要求（全部满足才算 pass）：
    净盈利率提升 ≥ 5pp、**按交易日分块 bootstrap** 的差值 95% CI 下界 > 0、
    平均净收益 > 0、尾部亏损（p05）不明显恶化、测试折 ≥ 4。
    """
    days = sorted({outcome.trade_date for outcome in outcomes})
    grid = {
        (outcome.trade_date, outcome.arm): outcome
        for outcome in outcomes
    }
    base = [grid.get((day, baseline_arm)) for day in days]
    treat = [grid.get((day, treatment_arm)) for day in days]
    paired = [
        (b, t) for b, t in zip(base, treat, strict=True) if b is not None and t is not None
    ]
    if not paired:
        raise ValueError("no paired day outcomes between baseline and treatment arms")

    base_matured = sum(item.matured_fills for item, _ in paired)
    treat_matured = sum(item.matured_fills for _, item in paired)
    base_profit = sum(item.net_profits for item, _ in paired)
    treat_profit = sum(item.net_profits for _, item in paired)
    base_rate = (base_profit / base_matured) if base_matured else None
    treat_rate = (treat_profit / treat_matured) if treat_matured else None
    improvement = None
    if base_rate is not None and treat_rate is not None:
        improvement = treat_rate - base_rate

    # 分块 bootstrap：重抽的是"交易日"，不是笔。同日推荐彼此相关，逐笔重抽会低估方差。
    rng = np.random.default_rng(seed)
    deltas: list[float] = []
    per_day_delta = [
        (item.net_profit_rate - first.net_profit_rate)
        for first, item in paired
        if first.matured_fills and item.matured_fills and first.net_profit_rate is not None
        and item.net_profit_rate is not None
    ]
    if per_day_delta:
        array = np.asarray(per_day_delta)
        for _ in range(int(bootstrap_draws)):
            sample = rng.choice(array, size=array.size, replace=True)
            deltas.append(float(sample.mean()))
    deltas.sort()
    ci_low = deltas[int(0.025 * len(deltas))] if deltas else None
    ci_high = deltas[int(0.975 * len(deltas))] if deltas else None

    treat_returns = [value for _, item in paired for value in item.net_returns]
    base_returns = [value for item, _ in paired for value in item.net_returns]
    mean_net_return = float(np.mean(treat_returns)) if treat_returns else None
    tail_loss = float(np.percentile(treat_returns, 5)) if treat_returns else None
    base_tail_loss = float(np.percentile(base_returns, 5)) if base_returns else None
    fills = sum(item.fills for _, item in paired)
    recommendations = sum(item.recommendations for _, item in paired)
    days_with_recommendation = sum(1 for _, item in paired if item.recommendations > 0)
    folds = _fold_count(days)

    reasons: list[str] = []
    if improvement is None:
        reasons.append("no_matured_fills_in_either_arm")
    else:
        if improvement < min_improvement_pp:
            reasons.append("improvement_below_5pp")
        if ci_low is None or ci_low <= 0.0:
            reasons.append("block_bootstrap_ci_lower_bound_not_positive")
    if mean_net_return is None or mean_net_return <= 0:
        reasons.append("mean_net_return_not_positive")
    if tail_loss is None or (base_tail_loss is not None
                             and tail_loss < base_tail_loss - tail_loss_tolerance_pp):
        reasons.append("tail_loss_materially_worse")
    if folds < MIN_TEST_FOLDS:
        reasons.append(f"fewer_than_{MIN_TEST_FOLDS}_test_folds")
    if base_matured == 0:
        reasons.append("baseline_has_no_fill_samples")

    return {
        "passed": not reasons,
        "failed_gates": reasons,
        "trade_days": len(days),
        "paired_days": len(paired),
        "test_folds": folds,
        "candidate_days": int(candidate_days),
        "recommendation_coverage": (days_with_recommendation / len(days)) if days else None,
        "baseline": {
            "matured_fills": base_matured,
            "net_profit_rate": base_rate,
            "mean_net_return": float(np.mean(base_returns)) if base_returns else None,
            "tail_loss_p05": base_tail_loss,
        },
        "treatment": {
            "matured_fills": treat_matured,
            "net_profit_rate": treat_rate,
            "mean_net_return": mean_net_return,
            "tail_loss_p05": tail_loss,
            "fill_rate": (fills / recommendations) if recommendations else None,
            "capital_employed_cny": recommendations * float(
                DEFAULT_TREND_CONTRACT.reference_notional
            ),
        },
        "improvement_pp": improvement,
        "block_bootstrap": {
            "draws": int(bootstrap_draws),
            "seed": int(seed),
            "ci_low": ci_low,
            "ci_high": ci_high,
            "unit": "trade_day",
        },
        "note": "旧链路没有成交样本时如实报 baseline_has_no_fill_samples，不得伪造命中率",
    }


def _fold_count(days: Sequence[date]) -> int:
    """把测试期按连续 20 个交易日算折数（不足 20 天的一截也计入但会缩短）。"""
    if not days:
        return 0
    return max(1, int(math.ceil(len(days) / 20.0)))


def shadow_readiness(
    *,
    observed_trade_days: int,
    matured_simulated_fills: int,
    min_days: int = MIN_SHADOW_TRADE_DAYS,
    min_fills: int = MIN_SHADOW_MATURED_FILLS,
) -> dict[str, Any]:
    """未来影子验证门槛：≥60 个完整交易日 **且** ≥100 笔成熟模拟成交。

    证据不足就保持影子状态 —— 历史数字漂亮不构成上线理由（计划 §4）。
    """
    blockers = []
    if observed_trade_days < min_days:
        blockers.append(f"observed_trade_days={observed_trade_days} < {min_days}")
    if matured_simulated_fills < min_fills:
        blockers.append(f"matured_simulated_fills={matured_simulated_fills} < {min_fills}")
    return {"ready_for_release_review": not blockers, "blockers": blockers,
            "state": "shadow" if blockers else "review"}


def _digest(payload: Mapping[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"model", "calibrator"}
    }
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "DEFAULT_LIGHTGBM_PARAMS",
    "KIND_LIGHTGBM",
    "KIND_LOGISTIC",
    "MIN_IMPROVEMENT_PP",
    "MIN_SHADOW_MATURED_FILLS",
    "MIN_SHADOW_TRADE_DAYS",
    "MIN_TEST_FOLDS",
    "DateSplit",
    "DayOutcome",
    "TailModelSpec",
    "TailTrainingError",
    "build_date_split",
    "evaluate_selection_quality",
    "shadow_readiness",
    "train_tail_net_profit_model",
]
