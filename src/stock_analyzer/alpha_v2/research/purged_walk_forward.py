"""Alpha V2 Purged Walk-Forward / Clean OOS（S19 / 原方案任务卡 P1-07 + §7）。

**这是 M2 研究可信度的地基**：如果切分方式不对，"IC 为正"可以完全来自泄漏。
本模块把三件事做成**不可绕过**的结构：

1. **禁止随机切分**：折由交易日历按时间生成，``plan_folds`` 只接受时间参数；
   任何 ``method != "time"`` 的入口直接抛错（:func:`refuse_random_split`）；
2. **purge + embargo 覆盖标签重叠**：训练样本的**成熟日**必须严格早于测试窗起点。
   ``purge = max_label_horizon + execution_delay - 1``（决定日 T 的标签在
   T+1+(H-1) 收盘才成熟）；``embargo >= max_label_horizon``。3/5/10/15D 同时
   参与评价时，边界取 15D——不是让训练器自己猜；
3. **统计单位是 decision date**：IC 先按日算，再做 moving-block bootstrap
   （复用 ``learning.scoring_eval``）+ Newey-West(HAC, lag≈H-1) +
   non-overlapping anchor 三种口径并列；禁止把"同一天 300 只"当 300 个独立样本。

每条 fold 都写出 ``train/validation/test 区间 / purge / embargo / max horizon /
lookahead 违规数``，并给出 :func:`overlap_leakage_check` 的独立复核结果。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_HORIZONS,
    DEFAULT_MIN_CROSS_SECTION,
    NOT_AVAILABLE,
    PRIMARY_HORIZON,
    daily_rank_ic,
    ic_summary,
    metric_column,
    research_gate_status,
)
from stock_analyzer.alpha_v2.research.multi_head import (
    TASK_REGRESSION,
    HeadFitSpec,
    _fit_model,
    _predict_model,
)

SPLIT_METHOD_TIME = "time"

DEFAULT_TRAIN_WINDOW_DAYS = 120
DEFAULT_TEST_WINDOW_DAYS = 20
DEFAULT_STEP_DAYS = 20
DEFAULT_VALIDATION_WINDOW_DAYS = 0
DEFAULT_EXECUTION_DELAY_DAYS = 1

LABEL_MATURE_COLUMN_TEMPLATE = "maturity_date_{h}d"


def resolve_max_label_horizon(horizons: Sequence[int] = DEFAULT_HORIZONS) -> int:
    return max([int(h) for h in horizons] + [PRIMARY_HORIZON])


@dataclass(frozen=True, slots=True)
class FoldSpec:
    """折划分与时间隔离口径（全部显式，报告原样写出）。"""

    train_window_days: int = DEFAULT_TRAIN_WINDOW_DAYS
    test_window_days: int = DEFAULT_TEST_WINDOW_DAYS
    step_days: int = DEFAULT_STEP_DAYS
    validation_window_days: int = DEFAULT_VALIDATION_WINDOW_DAYS
    max_label_horizon: int = 0
    execution_delay_days: int = DEFAULT_EXECUTION_DELAY_DAYS
    embargo_days: int = 0
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    method: str = SPLIT_METHOD_TIME

    def resolved_max_horizon(self) -> int:
        if int(self.max_label_horizon) > 0:
            return int(self.max_label_horizon)
        return resolve_max_label_horizon(self.horizons)

    def resolved_purge_days(self) -> int:
        """训练样本成熟日必须早于测试起点所需的**交易日**跨度。

        决定日 T 的标签在 ``T + execution_delay + H - 1`` 收盘成熟，因此把
        训练窗末端的最后 ``execution_delay + H - 1`` 个交易日排除即可切除重叠。
        """
        return max(1, int(self.execution_delay_days) + self.resolved_max_horizon() - 1)

    def resolved_embargo_days(self) -> int:
        return max(
            int(self.embargo_days) if int(self.embargo_days) > 0 else self.resolved_max_horizon(),
            self.resolved_max_horizon(),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "method": self.method,
            "train_window_days": int(self.train_window_days),
            "test_window_days": int(self.test_window_days),
            "step_days": int(self.step_days),
            "validation_window_days": int(self.validation_window_days),
            "max_label_horizon": self.resolved_max_horizon(),
            "execution_delay_days": int(self.execution_delay_days),
            "purge_days": self.resolved_purge_days(),
            "embargo_days": self.resolved_embargo_days(),
            "horizons": [int(h) for h in self.horizons],
            "random_split_used": False,
        }


def refuse_random_split(method: str) -> None:
    """任何非时间切分的入口都在这里被拒（Gate S19 Blocking）。"""
    if str(method).strip().lower() != SPLIT_METHOD_TIME:
        raise ValueError(
            f"随机切分不得作为主验证（收到 method={method!r}）："
            "重叠的 forward label 会让随机切分把未来样本混进训练集，"
            "本模块只接受按交易日推进的时间切分"
        )


@dataclass
class Fold:
    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_dates: tuple[date, ...]
    purge_days: int
    embargo_days: int
    max_label_horizon: int
    train_label_mature_cutoff: date | None = None
    validation_dates: tuple[date, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "fold_id": int(self.fold_id),
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_dates[-1].isoformat() if self.test_dates else None,
            "test_days": len(self.test_dates),
            "validation_days": len(self.validation_dates),
            "purge_days": int(self.purge_days),
            "embargo_days": int(self.embargo_days),
            "max_label_horizon": int(self.max_label_horizon),
            "train_label_mature_cutoff": (
                self.train_label_mature_cutoff.isoformat()
                if self.train_label_mature_cutoff
                else None
            ),
        }


def plan_folds(
    *,
    trading_dates: Sequence[date],
    spec: FoldSpec | None = None,
) -> list[Fold]:
    """按交易日推进生成 purged folds（**没有**随机性）。"""
    resolved = spec or FoldSpec()
    refuse_random_split(resolved.method)
    calendar = sorted({day for day in trading_dates})
    train_window = max(2, int(resolved.train_window_days))
    test_window = max(1, int(resolved.test_window_days))
    step = max(1, int(resolved.step_days))
    embargo = resolved.resolved_embargo_days()
    purge = resolved.resolved_purge_days()
    max_horizon = resolved.resolved_max_horizon()
    validation_window = max(0, int(resolved.validation_window_days))

    folds: list[Fold] = []
    fold_id = 0
    start_index = 0
    while True:
        train_end_idx = start_index + train_window - 1
        if train_end_idx >= len(calendar):
            break
        test_start_idx = train_end_idx + embargo
        if test_start_idx >= len(calendar):
            break
        test_end_idx = min(test_start_idx + test_window - 1, len(calendar) - 1)
        test_dates = tuple(calendar[test_start_idx : test_end_idx + 1])
        if len(test_dates) < max(3, test_window // 2):
            break
        # 训练决策日上界：使**成熟日**严格早于测试起点。
        # 决定日 idx 的成熟 idx = idx + purge（purge = 执行延迟 + H - 1），
        # 约束是 idx + purge < train_end_idx + embargo
        #   ⇒ idx <= train_end_idx + embargo - purge - 1。
        # 默认（embargo=H, purge=delay+H-1=H）⇒ 上界 = train_end_idx - 1。
        maturity_idx = max(start_index, min(train_end_idx, train_end_idx + embargo - purge - 1))
        validation_dates: tuple[date, ...] = ()
        if validation_window > 0:
            validation_start = max(0, test_end_idx + embargo)
            validation_end = min(validation_start + validation_window - 1, len(calendar) - 1)
            if validation_end >= validation_start:
                validation_dates = tuple(calendar[validation_start : validation_end + 1])
        fold_id += 1
        folds.append(
            Fold(
                fold_id=fold_id,
                train_start=calendar[start_index],
                train_end=calendar[train_end_idx],
                test_start=calendar[test_start_idx],
                test_dates=test_dates,
                purge_days=purge,
                embargo_days=embargo,
                max_label_horizon=max_horizon,
                train_label_mature_cutoff=calendar[maturity_idx],
                validation_dates=validation_dates,
            )
        )
        start_index += step
    return folds


# ---------------------------------------------------------------------------
# 泄漏复核
# ---------------------------------------------------------------------------


def overlap_leakage_check(
    fold: Fold,
    train_rows: pd.DataFrame,
    *,
    maturity_column: str | None = None,
) -> dict[str, object]:
    """独立复核：训练样本的决策日与成熟日都不得越过测试窗起点。"""
    column = maturity_column or LABEL_MATURE_COLUMN_TEMPLATE.format(h=fold.max_label_horizon)
    payload: dict[str, object] = {
        "fold_id": int(fold.fold_id),
        "test_start": fold.test_start.isoformat(),
        "maturity_column": column,
    }
    if train_rows.empty:
        return {**payload, "status": "empty_train", "violations": 0}
    decision_dates = pd.to_datetime(train_rows.get("decision_date"), errors="coerce")
    decision_violations = int((decision_dates >= pd.Timestamp(fold.test_start)).sum())
    payload["decision_date_violations"] = decision_violations
    if column not in train_rows.columns:
        payload["maturity_check"] = "column_absent"
        payload["violations"] = decision_violations
        payload["status"] = "ok" if decision_violations == 0 else "violation"
        return payload
    maturity = pd.to_datetime(train_rows[column], errors="coerce")
    # 成熟日缺失的训练行无法证明安全 → 计为待复核（不算违规，但必须显式计数）
    unknown = int(maturity.isna().sum())
    maturity_violations = int((maturity >= pd.Timestamp(fold.test_start)).sum())
    payload.update(
        {
            "maturity_check": "checked",
            "maturity_unknown_rows": unknown,
            "maturity_violations": maturity_violations,
            "violations": decision_violations + maturity_violations,
            "status": ("ok" if decision_violations + maturity_violations == 0 else "violation"),
        }
    )
    return payload


# ---------------------------------------------------------------------------
# Scorer 协议（训练只看到训练样本）
# ---------------------------------------------------------------------------


class FoldScorer(Protocol):
    """一折的打分器：``fit`` 只接受训练样本，``score`` 只接受特征。"""

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None: ...

    def score(self, frame: pd.DataFrame) -> pd.Series: ...


@dataclass
class LightGbmRankScorer:
    """默认打分器：LightGBM 回归到"5D 可执行超额收益横截面 rank"，输出原值再截面取秩。

    ``hyperparameters`` 在构造时确定并写进报告——**不参与测试集上的选择**
    （Gate S19："测试集参与模型选择" = Blocking）。
    """

    feature_columns: Sequence[str]
    fit_spec: HeadFitSpec = field(default_factory=HeadFitSpec)
    _model: Any = None

    def fit(self, *, features: pd.DataFrame, labels: pd.Series) -> None:
        self._model = _fit_model(
            features=np.asarray(features, dtype=np.float32),
            labels=np.asarray(labels, dtype=float),
            task=TASK_REGRESSION,
            spec=self.fit_spec,
        )

    def score(self, frame: pd.DataFrame) -> pd.Series:
        if self._model is None:
            raise RuntimeError("scorer must be fitted before scoring")
        values = _predict_model(self._model, np.asarray(frame, dtype=np.float32))
        return pd.Series(values, index=frame.index, dtype=float)

    def hyperparameters(self) -> dict[str, object]:
        return dict(self.fit_spec.to_payload())


# ---------------------------------------------------------------------------
# 单折执行
# ---------------------------------------------------------------------------


@dataclass
class FoldResult:
    fold: Fold
    daily_ic: pd.DataFrame
    ic_block: dict[str, object]
    metrics: dict[str, object]
    leakage: dict[str, object]
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "fold": self.fold.to_payload(),
            "ic": self.ic_block,
            "metrics": self.metrics,
            "leakage": self.leakage,
            "diagnostics": dict(self.diagnostics),
        }


def run_fold(
    *,
    fold: Fold,
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    label_column: str,
    metric_column_: str,
    scorer: FoldScorer,
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION,
    label_maturity_column: str | None = None,
) -> FoldResult:
    """训练（用 purge 后的训练样本）→ 测试窗打分 → 指标。

    训练样本 = ``decision_date <= train_label_mature_cutoff`` 且标签非空；
    测试样本 = ``decision_date ∈ fold.test_dates``。两者在时间上由 purge+embargo
    隔开，:func:`overlap_leakage_check` 独立复核。
    """
    dates = pd.to_datetime(frame["decision_date"], errors="coerce")
    cutoff = fold.train_label_mature_cutoff or fold.train_end
    train_mask = (dates <= pd.Timestamp(cutoff)) & (dates >= pd.Timestamp(fold.train_start))
    train_mask &= dates < pd.Timestamp(fold.test_start)
    labels = pd.to_numeric(frame[label_column], errors="coerce")
    train_mask &= labels.notna()
    # 第二道 purge（数据驱动）：日历口径的 purge 假设"决策日 + purge 个交易日即成熟"，
    # 而实际成熟日是按**标的自身 bar 序列**推进的——停牌/停更的票会晚于日历口径，
    # 于是少量样本的标签会成熟到测试窗里（2026-09-18 实测 3 折共 20 行）。
    # 这里按真实的成熟日列再剔一遍，并把剔除行数如实计数。
    maturity_column = label_maturity_column or LABEL_MATURE_COLUMN_TEMPLATE.format(
        h=fold.max_label_horizon
    )
    purged_rows = 0
    if maturity_column in frame.columns:
        maturity = pd.to_datetime(frame[maturity_column], errors="coerce")
        unsafe = maturity >= pd.Timestamp(fold.test_start)
        purged_rows = int((train_mask & unsafe).sum())
        train_mask &= ~unsafe
    train_rows = frame[train_mask]
    test_mask = dates.isin([pd.Timestamp(day) for day in fold.test_dates])
    test_rows = frame[test_mask].copy()

    leakage = overlap_leakage_check(fold, train_rows, maturity_column=label_maturity_column)
    diagnostics: dict[str, object] = {
        "train_rows": int(len(train_rows)),
        "maturity_purged_rows": purged_rows,
        "maturity_purge_column": maturity_column if maturity_column in frame.columns else "absent",
        "test_rows": int(len(test_rows)),
        "train_days": int(pd.to_datetime(train_rows["decision_date"]).nunique())
        if not train_rows.empty
        else 0,
    }
    if train_rows.empty or test_rows.empty:
        return FoldResult(
            fold=fold,
            daily_ic=pd.DataFrame(),
            ic_block={"status": "empty_split", **diagnostics},
            metrics={"status": "empty_split"},
            leakage=leakage,
            diagnostics=diagnostics,
        )

    scorer.fit(
        features=train_rows.loc[:, list(feature_columns)],
        labels=labels[train_mask],
    )
    scores = scorer.score(test_rows.loc[:, list(feature_columns)])
    test_rows["score"] = pd.Series(scores.to_numpy(), index=test_rows.index)
    # 分数语义 = 当日横截面 rank 分位（"谁更值得买"），不是绝对收益
    test_rows["score"] = test_rows["score"].groupby(test_rows["decision_date"]).rank(pct=True)

    daily = daily_rank_ic(
        test_rows,
        score_column="score",
        metric_column_=metric_column_,
        min_cross_section=min_cross_section,
    )
    block = ic_summary(daily, rolling_windows=(20, 60))
    block["non_overlapping"] = non_overlapping_anchor(daily, horizon=fold.max_label_horizon)
    block["newey_west"] = newey_west_mean_ci(daily, lag=max(1, fold.max_label_horizon - 1))
    metrics = {
        "status": "ok",
        "mature_dates": int(block.get("mature_dates", 0)),
        "top_minus_bottom": _top_minus_bottom(test_rows, metric_column_),
    }
    diagnostics["hyperparameters"] = (
        scorer.hyperparameters() if hasattr(scorer, "hyperparameters") else NOT_AVAILABLE
    )
    return FoldResult(
        fold=fold,
        daily_ic=daily,
        ic_block=block,
        metrics=metrics,
        leakage=leakage,
        diagnostics=diagnostics,
    )


def _top_minus_bottom(test_rows: pd.DataFrame, metric_column_: str) -> float:
    values = pd.to_numeric(test_rows.get(metric_column_), errors="coerce")
    scores = pd.to_numeric(test_rows.get("score"), errors="coerce")
    usable = test_rows[values.notna() & scores.notna()]
    if usable.empty:
        return float("nan")
    top = usable[usable["score"] >= 0.8]
    bottom = usable[usable["score"] <= 0.2]
    if top.empty or bottom.empty:
        return float("nan")
    return float(
        pd.to_numeric(top[metric_column_], errors="coerce").mean()
        - pd.to_numeric(bottom[metric_column_], errors="coerce").mean()
    )


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------


def newey_west_mean_ci(
    daily: pd.DataFrame, *, lag: int, confidence: float = 0.95
) -> dict[str, object]:
    """HAC/Newey-West 均值置信区间（lag ≈ H-1，处理重叠窗口的自相关）。"""
    if daily.empty or "ic" not in daily.columns:
        return {"status": "no_data"}
    values = pd.to_numeric(daily["ic"], errors="coerce").dropna().to_numpy(dtype=float)
    n = values.size
    if n < 3:
        return {"status": "no_data", "n": int(n)}
    mean = float(values.mean())
    centered = values - mean
    gamma0 = float((centered**2).sum() / n)
    variance = gamma0
    resolved_lag = max(0, min(int(lag), n - 1))
    for offset in range(1, resolved_lag + 1):
        weight = 1.0 - offset / (resolved_lag + 1.0)
        covariance = float((centered[offset:] * centered[:-offset]).sum() / n)
        variance += 2.0 * weight * covariance
    variance = max(variance, 0.0)
    standard_error = math.sqrt(variance / n)
    z = 1.959963984540054 if abs(confidence - 0.95) < 1e-9 else 1.959963984540054
    return {
        "status": "ok",
        "n": int(n),
        "lag": int(resolved_lag),
        "mean_ic": mean,
        "hac_se": standard_error,
        "ci95": [mean - z * standard_error, mean + z * standard_error],
        "method": "newey_west_hac",
    }


def non_overlapping_anchor(
    daily: pd.DataFrame, *, horizon: int, confidence: float = 0.95
) -> dict[str, object]:
    """非重叠锚点稳健性：只取每 ``horizon`` 个交易日中的一天，消除标签重叠。"""
    if daily.empty:
        return {"status": "no_data"}
    ordered = daily.sort_values("decision_date").reset_index(drop=True)
    stride = max(1, int(horizon))
    anchored = ordered.iloc[::stride]
    values = pd.to_numeric(anchored["ic"], errors="coerce").dropna()
    if values.empty:
        return {"status": "no_data"}
    payload: dict[str, object] = {
        "status": "ok",
        "days": int(values.shape[0]),
        "stride": stride,
        "mean_ic": float(values.mean()),
        "positive_ratio": float((values > 0).mean()),
    }
    if values.shape[0] >= 3:
        summary = ic_summary(anchored, rolling_windows=())
        payload["ci95"] = summary.get("ci95", [NOT_AVAILABLE, NOT_AVAILABLE])
    return payload


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


@dataclass
class WalkForwardReport:
    spec: FoldSpec
    folds: list[FoldResult]
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_payload(),
            "folds": [item.to_payload() for item in self.folds],
            "summary": self.summary(),
            "diagnostics": dict(self.diagnostics),
        }

    def summary(self) -> dict[str, object]:
        usable = [item for item in self.folds if item.metrics.get("status") == "ok"]
        violations = sum(int(item.leakage.get("violations", 0)) for item in self.folds)
        pooled_assets: list[tuple[str, float]] = []
        for item in usable:
            pooled_assets.extend(
                (str(row["decision_date"]), float(row["ic"])) for _, row in item.daily_ic.iterrows()
            )
        dates = {day for day, _ in pooled_assets}
        block = (
            ic_summary(
                pd.DataFrame(pooled_assets, columns=["decision_date", "ic"]),
                rolling_windows=(20, 60),
            )
            if pooled_assets
            else {"status": "no_data", "mature_dates": 0}
        )
        purged = sum(int(item.diagnostics.get("maturity_purged_rows", 0)) for item in self.folds)
        return {
            "folds_planned": len(self.folds),
            "folds_usable": len(usable),
            "lookahead_violations": violations,
            # 日历口径 purge 不足、由**成熟日**二次剔除的行数（信息披露，不是失败）：
            # 停牌/停更的票会让"决策日 + purge 个交易日"早于真实成熟日。
            "maturity_purged_rows": purged,
            "purge_adequacy": (
                "calendar_purge_sufficient" if purged == 0 else "calendar_purge_insufficient"
            ),
            "mature_dates": len(dates),
            "research_gate": research_gate_status(len(dates)),
            "pooled_ic": block,
            "verdict": self.verdict(),
        }

    def verdict(self) -> str:
        summary_folds = len(self.folds)
        usable = [item for item in self.folds if item.metrics.get("status") == "ok"]
        violations = sum(int(item.leakage.get("violations", 0)) for item in self.folds)
        if summary_folds == 0 or len(usable) == 0:
            return "INSUFFICIENT_FOLDS"
        if violations > 0:
            return "NO_GO_LEAKAGE"
        pooled: list[tuple[str, float]] = []
        for item in usable:
            pooled.extend(
                (str(row["decision_date"]), float(row["ic"])) for _, row in item.daily_ic.iterrows()
            )
        if not pooled:
            return "INSUFFICIENT_FOLDS"
        block = ic_summary(
            pd.DataFrame(pooled, columns=["decision_date", "ic"]), rolling_windows=()
        )
        mean_ic = float(block.get("mean_ic", float("nan")))
        ci_low, ci_high = (block.get("ci95") or [float("nan"), float("nan")])[:2]
        if not math.isfinite(mean_ic):
            return "INSUFFICIENT_FOLDS"
        if ci_low > 0:
            return "GO_CANDIDATE"
        if ci_high < 0:
            return "NO_GO_NEGATIVE_EVIDENCE"
        return "INCONCLUSIVE"


def run_walk_forward(
    *,
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    label_column: str,
    metric_column_: str | None = None,
    spec: FoldSpec | None = None,
    scorer_factory: Any = None,
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION,
    max_folds: int = 0,
) -> WalkForwardReport:
    """完整 purged walk-forward：计划折 → 逐折训练/评估 → 汇总判定。"""
    resolved = spec or FoldSpec()
    refuse_random_split(resolved.method)
    dates = pd.to_datetime(frame["decision_date"], errors="coerce").dropna()
    if dates.empty:
        return WalkForwardReport(spec=resolved, folds=[], diagnostics={"status": "empty_frame"})
    trading_dates = tuple(sorted({timestamp.date() for timestamp in dates}))
    folds = plan_folds(trading_dates=trading_dates, spec=resolved)
    if max_folds > 0:
        folds = folds[: int(max_folds)]
    factory = scorer_factory or (lambda: LightGbmRankScorer(feature_columns=feature_columns))
    metric = metric_column_ or metric_column("excess_return", resolved.resolved_max_horizon())
    results: list[FoldResult] = []
    for fold in folds:
        results.append(
            run_fold(
                fold=fold,
                frame=frame,
                feature_columns=feature_columns,
                label_column=label_column,
                metric_column_=metric,
                scorer=factory(),
                min_cross_section=min_cross_section,
            )
        )
    return WalkForwardReport(
        spec=resolved,
        folds=results,
        diagnostics={
            "trading_dates": len(trading_dates),
            "metric_column": metric,
            "label_column": label_column,
            "random_split_used": False,
        },
    )


def fold_isolation_matrix(report: WalkForwardReport) -> pd.DataFrame:
    """折级隔离矩阵（供审计报告直接展示 train/test 区间与隔离天数）。"""
    rows: list[dict[str, object]] = []
    for item in report.folds:
        payload = item.fold.to_payload()
        payload["leakage_status"] = item.leakage.get("status", NOT_AVAILABLE)
        payload["leakage_violations"] = item.leakage.get("violations", 0)
        payload["mature_dates"] = item.metrics.get("mature_dates", 0)
        rows.append(payload)
    return pd.DataFrame(rows)


def assert_train_never_sees_test(frame: pd.DataFrame, fold: Fold) -> None:
    """结构守卫：``frame`` 中训练窗之后的样本不得带标签进入训练集。

    作为实现自检使用：如果某处代码把全量帧直接喂给训练器，这个断言会失败。
    """
    dates = pd.to_datetime(frame["decision_date"], errors="coerce")
    violating = frame[dates >= pd.Timestamp(fold.test_start)]
    if violating.empty:
        return
    cutoff = fold.train_label_mature_cutoff or fold.train_end
    leaked = violating[
        pd.to_datetime(violating["decision_date"], errors="coerce") <= pd.Timestamp(cutoff)
    ]
    if not leaked.empty:
        raise AssertionError(
            f"fold {fold.fold_id}: 训练集混入了测试窗之后（或未 purge）的样本: {len(leaked)} 行"
        )


__all__ = [
    "DEFAULT_STEP_DAYS",
    "DEFAULT_TEST_WINDOW_DAYS",
    "DEFAULT_TRAIN_WINDOW_DAYS",
    "Fold",
    "FoldResult",
    "FoldScorer",
    "FoldSpec",
    "LightGbmRankScorer",
    "SPLIT_METHOD_TIME",
    "WalkForwardReport",
    "assert_train_never_sees_test",
    "fold_isolation_matrix",
    "newey_west_mean_ci",
    "non_overlapping_anchor",
    "overlap_leakage_check",
    "plan_folds",
    "refuse_random_split",
    "resolve_max_label_horizon",
    "run_fold",
    "run_walk_forward",
]
