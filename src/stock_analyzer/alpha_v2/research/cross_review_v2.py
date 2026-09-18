"""Alpha V2 Cross Review V2 —— **分歧观测层**（S17 / 原方案正文 P1-07）。

Legacy Cross Review 是**绝对概率门**：

```text
LGBM > 0.60 AND XGB > 0.55 AND meta > 0.54 AND max_diff <= 0.18
```

它在生产上同时拒掉约 96~100% 候选（蓝图 §2.3）。本阶段**完全不动它**——既不
放宽也不收紧，Legacy 行为面由 S00 的 golden 契约锁死。

V2 这一阶段只做一件事：**把"模型分歧"变成可观测量**，并回答一个问题：

> 分歧大的股票，未来是不是真的更差？

在没有证据之前，``rank_disagreement`` / ``prob_disagreement`` **不得**变成新的
100% 硬否决门（Gate S17 Blocking）。因此本模块：

1. 产出可观测量（``lgbm_rank_pct`` / ``xgb_rank_pct`` / ``rank_disagreement`` /
   ``prob_disagreement``）；
2. 给出**可判定**的证据块：按分歧分位分组看未来真实可执行超额收益，
   并附 date-block CI 与样本门；
3. 输出 ``policy``：在证据达到门槛前恒为 ``observation_only``。

两个模型走同一份共享矩阵（S16），单次特征构建、单次推理——不额外跑两遍 Pipeline。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_MIN_CROSS_SECTION,
    NOT_AVAILABLE,
    PRIMARY_HORIZON,
    ic_summary,
    metric_column,
    paired_delta,
    research_gate_status,
)
from stock_analyzer.alpha_v2.research.multi_head import (
    ALPHA_TARGET_TEMPLATE,
    CALIBRATION_NONE,
    TASK_BINARY,
    TASK_REGRESSION,
    HeadFitSpec,
    SharedFeatureMatrix,
    _fit_model,
    _predict_model,
)

__all__ = ["CALIBRATION_NONE"]  # 语义常量再导出（Head 校准口径在 S17 只做只读引用）

POLICY_OBSERVATION_ONLY = "observation_only"
POLICY_HARD_GATE_CANDIDATE = "hard_gate_candidate"

BACKEND_LIGHTGBM = "lightgbm"
BACKEND_XGBOOST = "xgboost"

DEFAULT_DISAGREEMENT_BUCKETS = 3
DEFAULT_EVIDENCE_MIN_DATES = 60
DEFAULT_EVIDENCE_MAX_P_VALUE = 0.05

LEGACY_CROSS_REVIEW_KEYS = ("p_lgbm_min", "p_xgb_min", "p_meta_min", "max_diff")


@dataclass(frozen=True, slots=True)
class DisagreementSpec:
    """分歧观测量与证据判定的口径。"""

    target_column: str = ""
    label_column: str = ""
    horizon: int = PRIMARY_HORIZON
    metric: str = "excess_return"
    buckets: int = DEFAULT_DISAGREEMENT_BUCKETS
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION
    evidence_min_dates: int = DEFAULT_EVIDENCE_MIN_DATES
    evidence_max_p_value: float = DEFAULT_EVIDENCE_MAX_P_VALUE

    def resolved_target(self) -> str:
        """两个模型共同学习的目标列（默认 = S16 的 5D 横截面 rank 目标）。"""
        return self.target_column or ALPHA_TARGET_TEMPLATE.format(h=PRIMARY_HORIZON)

    def resolved_label(self) -> str:
        """用于证据判定的**真实收益**列（与预测目标分开，避免拿预测当结果）。"""
        return self.label_column or metric_column("excess_return", self.horizon)

    def to_payload(self) -> dict[str, object]:
        return {
            "target_column": self.resolved_target(),
            "label_column": self.resolved_label(),
            "horizon": int(self.horizon),
            "buckets": int(self.buckets),
            "min_cross_section": int(self.min_cross_section),
            "evidence_min_dates": int(self.evidence_min_dates),
            "evidence_max_p_value": float(self.evidence_max_p_value),
            "rank_disagreement_definition": "abs(lgbm_rank_pct - xgb_rank_pct)",
            "prob_disagreement_definition": "abs(lgbm_prob - xgb_prob)",
            "policy": ("observation_only：在证据达到门槛前，分歧不得作为否决门"),
        }


def legacy_cross_review_policy(config: Any) -> dict[str, object]:
    """如实读出 Legacy 四阈值并声明 V2 **不修改**它们。"""
    cross = getattr(config.models, "cross_review", None)
    payload: dict[str, object] = {
        "source": "config.models.cross_review",
        "modified_by_alpha_v2": False,
        "note": "S17 只做观测；Legacy 绝对概率门保持原样（S00 golden 契约锁定）",
    }
    for key in LEGACY_CROSS_REVIEW_KEYS:
        payload[key] = getattr(cross, key, NOT_AVAILABLE) if cross is not None else NOT_AVAILABLE
    return payload


# ---------------------------------------------------------------------------
# 双模型 → 分歧观测量
# ---------------------------------------------------------------------------


@dataclass
class DualModelDisagreement:
    frame: pd.DataFrame
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {"rows": int(len(self.frame)), "diagnostics": dict(self.diagnostics)}


def compute_disagreement(
    *,
    matrix: SharedFeatureMatrix,
    spec: DisagreementSpec | None = None,
    fit: HeadFitSpec | None = None,
    enable_xgboost: bool = True,
) -> DualModelDisagreement:
    """在同一份共享矩阵上训练 LGBM 与 XGB，产出分歧观测量。

    返回列：``lgbm_score`` / ``xgb_score`` / ``lgbm_rank_pct`` / ``xgb_rank_pct`` /
    ``rank_disagreement`` / ``lgbm_prob`` / ``xgb_prob`` / ``prob_disagreement``。
    """
    resolved = spec or DisagreementSpec()
    resolved_fit = fit or HeadFitSpec()
    frame = matrix.frame
    output = frame[["decision_date", "symbol"]].copy()
    target = resolved.resolved_target()
    if target not in frame.columns:
        raise ValueError(
            f"矩阵里没有可用的排序目标列 {target!r}："
            "分歧观测必须基于同一份矩阵的同一目标，不能用别的分数顶替"
        )
    labels = pd.to_numeric(frame[target], errors="coerce")
    if labels.isna().all():
        raise ValueError(
            f"矩阵里没有可用的排序目标列 {target!r}："
            "分歧观测必须基于同一份矩阵的同一目标，不能用别的分数顶替"
        )
    train_mask = _mask(frame, resolved_fit.train_mask_column)
    predict_mask = _mask(frame, resolved_fit.predict_mask_column)
    if not bool(predict_mask.any()):
        predict_mask = pd.Series(True, index=frame.index)

    features = frame.loc[:, list(matrix.feature_columns)].apply(pd.to_numeric, errors="coerce")
    matrix_values = features.to_numpy(dtype=np.float32, copy=True)
    usable_label = labels.notna()
    train = train_mask & usable_label
    if int(train.sum()) < resolved_fit.min_train_rows:
        raise ValueError(
            f"训练样本不足（{int(train.sum())} < {resolved_fit.min_train_rows}）："
            "分歧观测必须有可训样本，不得用未训练模型的分歧"
        )

    diagnostics: dict[str, object] = {"matrix_fingerprint": matrix.fingerprint}
    lgbm_rank_model = _fit_model(
        features=matrix_values[train.to_numpy()],
        labels=labels[train].to_numpy(dtype=float),
        task=TASK_REGRESSION,
        spec=resolved_fit,
    )
    lgbm_score = _scatter(
        frame, predict_mask, _predict_model(lgbm_rank_model, matrix_values[predict_mask.to_numpy()])
    )
    output["lgbm_score"] = lgbm_score
    output["lgbm_rank_pct"] = _rank_pct(output, "lgbm_score")
    diagnostics["lgbm"] = {"backend": BACKEND_LIGHTGBM, "train_rows": int(train.sum())}

    xgb_score = _fit_predict_xgboost(
        matrix_values=matrix_values,
        labels=labels,
        train=train,
        predict=predict_mask,
        frame=frame,
        spec=resolved_fit,
        enable=enable_xgboost,
    )
    if xgb_score is None:
        diagnostics["xgb"] = {"backend": BACKEND_XGBOOST, "status": "unavailable"}
    else:
        output["xgb_score"] = xgb_score
        output["xgb_rank_pct"] = _rank_pct(output, "xgb_score")
        diagnostics["xgb"] = {"backend": BACKEND_XGBOOST, "train_rows": int(train.sum())}

    if "xgb_rank_pct" in output.columns:
        output["rank_disagreement"] = (output["lgbm_rank_pct"] - output["xgb_rank_pct"]).abs()
    else:
        output["rank_disagreement"] = np.nan

    probability_columns = _fit_probabilities(
        matrix_values=matrix_values,
        frame=frame,
        train=train,
        predict=predict_mask,
        spec=resolved_fit,
        enable_xgboost=enable_xgboost,
    )
    for name, series in probability_columns.items():
        output[name] = series
    if {"lgbm_prob", "xgb_prob"}.issubset(output.columns):
        output["prob_disagreement"] = (output["lgbm_prob"] - output["xgb_prob"]).abs()
    else:
        output["prob_disagreement"] = np.nan

    diagnostics["observed_rows"] = int(output["rank_disagreement"].notna().sum())
    diagnostics["mean_rank_disagreement"] = float(
        pd.to_numeric(output["rank_disagreement"], errors="coerce").mean()
    )
    return DualModelDisagreement(frame=output, diagnostics=diagnostics)


def _scatter(frame: pd.DataFrame, mask: pd.Series, values: np.ndarray) -> pd.Series:
    series = pd.Series(np.nan, index=frame.index, dtype=float)
    series.loc[frame.index[mask.to_numpy()]] = np.asarray(values, dtype=float)
    return series


def _rank_pct(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    return values.groupby(frame["decision_date"]).rank(pct=True)


def _mask(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].fillna(False).astype(bool)


def _fit_predict_xgboost(
    *,
    matrix_values: np.ndarray,
    labels: pd.Series,
    train: pd.Series,
    predict: pd.Series,
    frame: pd.DataFrame,
    spec: HeadFitSpec,
    enable: bool,
) -> pd.Series | None:
    if not enable:
        return None
    try:
        import xgboost as xgb
    except ImportError:  # pragma: no cover - 依赖缺失时如实降级
        return None
    params = {
        "objective": "reg:squarederror",
        "eta": 0.05,
        "max_depth": 6,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "lambda": 1.0,
        "seed": int(spec.seed),
        "nthread": max(1, int(spec.n_jobs)),
        "tree_method": "hist",
    }
    dataset = xgb.DMatrix(
        matrix_values[train.to_numpy()], label=labels[train].to_numpy(dtype=float)
    )
    booster = xgb.train(params, dataset, num_boost_round=120)
    predictions = booster.predict(xgb.DMatrix(matrix_values[predict.to_numpy()]))
    return _scatter(frame, predict, np.asarray(predictions, dtype=float))


def _fit_probabilities(
    *,
    matrix_values: np.ndarray,
    frame: pd.DataFrame,
    train: pd.Series,
    predict: pd.Series,
    spec: HeadFitSpec,
    enable_xgboost: bool,
) -> dict[str, pd.Series]:
    column = "up_net_5d"
    outputs: dict[str, pd.Series] = {}
    if column not in frame.columns:
        return outputs
    labels = pd.to_numeric(frame[column], errors="coerce")
    usable = train & labels.notna()
    if int(usable.sum()) < spec.min_train_rows:
        return outputs
    positives = float(labels[usable].mean())
    if not 0.02 <= positives <= 0.98:
        return outputs
    lgbm = _fit_model(
        features=matrix_values[usable.to_numpy()],
        labels=labels[usable].to_numpy(dtype=float),
        task=TASK_BINARY,
        spec=spec,
    )
    outputs["lgbm_prob"] = _scatter(
        frame, predict, _predict_model(lgbm, matrix_values[predict.to_numpy()])
    )
    if not enable_xgboost:
        return outputs
    try:
        import xgboost as xgb
    except ImportError:  # pragma: no cover
        return outputs
    params = {
        "objective": "binary:logistic",
        "eta": 0.05,
        "max_depth": 6,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "lambda": 1.0,
        "seed": int(spec.seed),
        "nthread": max(1, int(spec.n_jobs)),
        "tree_method": "hist",
    }
    dataset = xgb.DMatrix(
        matrix_values[usable.to_numpy()], label=labels[usable].to_numpy(dtype=float)
    )
    booster = xgb.train(params, dataset, num_boost_round=120)
    outputs["xgb_prob"] = _scatter(
        frame,
        predict,
        np.asarray(booster.predict(xgb.DMatrix(matrix_values[predict.to_numpy()])), dtype=float),
    )
    return outputs


# ---------------------------------------------------------------------------
# 证据：分歧大 → 未来更差？
# ---------------------------------------------------------------------------


def disagreement_evidence(
    disagreement: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    spec: DisagreementSpec | None = None,
    disagreement_column: str = "rank_disagreement",
) -> dict[str, object]:
    """按分歧分位分组看未来真实可执行超额收益（含 date-block CI）。

    ``policy`` 的默认值是 ``observation_only``；只有在样本门与显著性同时满足时
    才升级为 ``hard_gate_candidate``，且**仍然不自动生效**——把分歧变成门是一次
    需要人工批准的策略变更（Gate S17）。
    """
    resolved = spec or DisagreementSpec()
    label = resolved.resolved_label()
    merged = disagreement.merge(
        outcomes[["decision_date", "symbol", label, "executable"]].rename(
            columns={label: "__label"}
        ),
        on=["decision_date", "symbol"],
        how="inner",
    )
    merged = merged[merged["executable"].fillna(False).astype(bool)]
    merged = merged[pd.to_numeric(merged["__label"], errors="coerce").notna()]
    merged = merged[pd.to_numeric(merged[disagreement_column], errors="coerce").notna()]
    if merged.empty:
        return {
            "status": "no_data",
            "policy": POLICY_OBSERVATION_ONLY,
            "label_column": label,
        }

    merged = merged.copy()
    merged["__bucket"] = merged.groupby("decision_date")[disagreement_column].transform(
        lambda values: _bucket(values, resolved.buckets)
    )
    rows: list[dict[str, object]] = []
    for bucket, group in merged.dropna(subset=["__bucket"]).groupby("__bucket"):
        values = pd.to_numeric(group["__label"], errors="coerce").dropna()
        rows.append(
            {
                "bucket": int(bucket),
                "rows": int(len(values)),
                "days": int(group["decision_date"].nunique()),
                "mean_label": float(values.mean()) if not values.empty else NOT_AVAILABLE,
                "date_block_ci": ic_summary(_daily_means(group, "__label"), rolling_windows=()).get(
                    "ci95", [NOT_AVAILABLE, NOT_AVAILABLE]
                ),
            }
        )
    table = pd.DataFrame(rows).sort_values("bucket")
    highest = table[table["bucket"] == table["bucket"].max()]
    lowest = table[table["bucket"] == table["bucket"].min()]
    high_minus_low = (
        float(highest["mean_label"].iloc[0]) - float(lowest["mean_label"].iloc[0])
        if not highest.empty and not lowest.empty
        else float("nan")
    )
    paired = _paired_bucket_delta(
        merged,
        high_bucket=int(table["bucket"].max()),
        low_bucket=int(table["bucket"].min()),
        label="__label",
    )

    mature_dates = int(merged["decision_date"].nunique())
    evidence_sufficient = mature_dates >= resolved.evidence_min_dates
    negative_direction = bool(np.isfinite(high_minus_low) and high_minus_low < 0)
    policy = (
        POLICY_HARD_GATE_CANDIDATE
        if evidence_sufficient and negative_direction
        else POLICY_OBSERVATION_ONLY
    )
    return {
        "status": "ok",
        "label_column": label,
        "buckets": table.to_dict(orient="records"),
        "high_minus_low": high_minus_low,
        "high_bucket_worse": negative_direction,
        "mature_dates": mature_dates,
        "research_gate": research_gate_status(mature_dates),
        "evidence_sufficient": evidence_sufficient,
        "paired_extremes": paired,
        "policy": policy,
        "policy_note": (
            "即使升级为 hard_gate_candidate，也不自动改变任何选股结果；"
            "把分歧变成否决门属于策略变更，需要人工批准（Gate S17）"
        ),
    }


def _paired_bucket_delta(
    merged: pd.DataFrame, *, high_bucket: int, low_bucket: int, label: str
) -> dict[str, object]:
    """同日配对：高分歧桶与低分歧桶的**当日均值**之差（跨日配对，含 block CI）。

    配对的意义：剔掉市场整体涨跌，只留"同一天内高低分歧的差"。
    """
    if high_bucket == low_bucket:
        return {"status": "no_data", "reason": "single_bucket"}
    high = merged[merged["__bucket"] == high_bucket]
    low = merged[merged["__bucket"] == low_bucket]
    if high.empty or low.empty:
        return {"status": "no_data", "reason": "empty_extreme_bucket"}
    high_daily = (
        pd.to_numeric(high[label], errors="coerce")
        .groupby(high["decision_date"].astype(str))
        .mean()
    )
    low_daily = (
        pd.to_numeric(low[label], errors="coerce").groupby(low["decision_date"].astype(str)).mean()
    )
    joined = pd.concat({"high": high_daily, "low": low_daily}, axis=1).dropna()
    if joined.empty:
        return {"status": "no_data", "reason": "no_common_days"}
    paired_frame = pd.DataFrame(
        {
            "decision_date": joined.index,
            "high": joined["high"].to_numpy(),
            "low": joined["low"].to_numpy(),
        }
    )
    return paired_delta(paired_frame, left_column="high", right_column="low")


def _bucket(values: pd.Series, buckets: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    size = max(2, int(buckets))
    ranked = numeric.rank(method="first")
    if ranked.notna().sum() < size:
        return pd.Series(np.nan, index=values.index)
    try:
        return pd.qcut(ranked, size, labels=False, duplicates="drop")
    except ValueError:  # pragma: no cover - 退化分布
        return pd.Series(np.nan, index=values.index)


def _daily_means(group: pd.DataFrame, column: str) -> pd.DataFrame:
    values = pd.to_numeric(group[column], errors="coerce")
    frame = pd.DataFrame({"decision_date": group["decision_date"].astype(str), "value": values})
    daily = frame.dropna().groupby("decision_date")["value"].mean()
    return pd.DataFrame({"decision_date": daily.index, "ic": daily.to_numpy()})


def cross_review_observation_columns() -> list[str]:
    return [
        "lgbm_score",
        "xgb_score",
        "lgbm_rank_pct",
        "xgb_rank_pct",
        "rank_disagreement",
        "lgbm_prob",
        "xgb_prob",
        "prob_disagreement",
    ]


def assert_no_hard_gate(policy_payload: Mapping[str, object]) -> None:
    """结构守卫：本阶段的策略不得是"直接否决"。"""
    policy = str(policy_payload.get("policy", ""))
    if policy not in {POLICY_OBSERVATION_ONLY, POLICY_HARD_GATE_CANDIDATE}:
        raise ValueError(f"unsupported cross review v2 policy: {policy!r}")


__all__ = [
    "BACKEND_LIGHTGBM",
    "BACKEND_XGBOOST",
    "DEFAULT_DISAGREEMENT_BUCKETS",
    "DisagreementSpec",
    "DualModelDisagreement",
    "LEGACY_CROSS_REVIEW_KEYS",
    "POLICY_HARD_GATE_CANDIDATE",
    "POLICY_OBSERVATION_ONLY",
    "assert_no_hard_gate",
    "compute_disagreement",
    "cross_review_observation_columns",
    "disagreement_evidence",
    "legacy_cross_review_policy",
]
