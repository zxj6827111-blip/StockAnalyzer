"""Alpha V2 统一评价指标（S15 起共用；S16/S19/S21 消费同一口径）。

**为什么单独一个模块**：一旦"命中率""IC""分位单调性"在不同阶段各写一遍，报告之间
就不可比，而且很容易出现"某个阶段偷偷换了分母/换了样本"。这里把指标**一次性**
定死为纯函数，输入是同一张 outcome 帧 + 一个分数列。

口径要点：

- **统计单位是 decision date**：日频 IC 先按日算，再做日级汇总与 date-block
  bootstrap（复用 ``learning.scoring_eval`` 的 moving-block 实现，块长预设）；
  绝不把"同一天 300 只股票"当 300 个独立样本；
- **命中率分母只算可成交且成熟**的样本：未成交的样本既不是赢也不是输，
  算进分母会把命中率稀释成假的；
- **同时给出收益幅度与尾部**：高命中率 + 极低收益（或极大尾部亏损）不是好系统
  （蓝图 §23），所以命中率、均值收益、超额、尾部损失必须同屏出现；
- **禁止自证循环**：分数列不得是 outcome 族列名。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.feature_audit import is_outcome_leak_column
from stock_analyzer.learning.scoring_eval import date_block_bootstrap_ci

NOT_AVAILABLE = "not_available"

PRIMARY_HORIZON = 5
DEFAULT_HORIZONS: tuple[int, ...] = (3, 5, 10, 15)
SHORT_HORIZONS: tuple[int, ...] = (3, 5)
DEFAULT_TOP_KS: tuple[int, ...] = (1, 3, 5)
DEFAULT_QUANTILES = 5
DEFAULT_MIN_CROSS_SECTION = 20
DEFAULT_BLOCK_TRADING_DAYS = 5

METRIC_TOTAL_RETURN = "net_return"
METRIC_EXCESS_RETURN = "excess_return"

# 尾部损失阈值（蓝图 §8.6："5% tail return"）
TAIL_LOSS_QUANTILE = 0.05
LARGE_LOSS_THRESHOLD = -0.05


def metric_column(kind: str, horizon: int) -> str:
    return f"{kind}_{int(horizon)}d"


def assert_score_column(column: str) -> str:
    """分数列不得是 outcome 族（否则指标自证循环）。"""
    text = str(column).strip()
    if not text:
        raise ValueError("score column must not be empty")
    if is_outcome_leak_column(text):
        raise ValueError(
            f"score column {text!r} 属于 outcome/label 族：用真实收益当分数会让所有指标"
            "自动变好（自证循环），拒绝计算"
        )
    return text


# ---------------------------------------------------------------------------
# 样本筛选
# ---------------------------------------------------------------------------


def usable_mask(
    frame: pd.DataFrame,
    *,
    metric: str,
    require_executable: bool = True,
    min_cross_section: int = 0,
) -> pd.Series:
    """可用样本掩码：可成交 + 该 horizon 已成熟 + 指标非空。"""
    if frame.empty:
        return pd.Series(dtype=bool)
    mask = pd.Series(True, index=frame.index)
    if require_executable and "executable" in frame.columns:
        mask &= frame["executable"].fillna(False).astype(bool)
    matured = metric.replace("net_return", "matured").replace("excess_return", "matured")
    if matured in frame.columns:
        mask &= frame[matured].fillna(False).astype(bool)
    if metric not in frame.columns:
        # 指标列整体缺失 → 没有可用样本（不是"全部可用"）
        return pd.Series(False, index=frame.index)
    values = pd.to_numeric(frame[metric], errors="coerce")
    mask &= values.notna()
    if min_cross_section > 0:
        day_counts = mask.groupby(frame["decision_date"]).transform("sum")
        mask &= day_counts >= int(min_cross_section)
    return mask.fillna(False)


# ---------------------------------------------------------------------------
# Rank IC
# ---------------------------------------------------------------------------


def daily_rank_ic(
    frame: pd.DataFrame,
    *,
    score_column: str,
    metric_column_: str,
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION,
) -> pd.DataFrame:
    """逐日 Spearman IC（同日横截面秩相关）。"""
    assert_score_column(score_column)
    if frame.empty or metric_column_ not in frame.columns:
        return pd.DataFrame(columns=["decision_date", "ic", "n"])
    mask = usable_mask(frame, metric=metric_column_, min_cross_section=min_cross_section)
    usable = frame[mask]
    rows: list[dict[str, object]] = []
    for decision_date, group in usable.groupby("decision_date", sort=True):
        scores = pd.to_numeric(group[score_column], errors="coerce").to_numpy(dtype=float)
        returns = pd.to_numeric(group[metric_column_], errors="coerce").to_numpy(dtype=float)
        valid = np.isfinite(scores) & np.isfinite(returns)
        scores, returns = scores[valid], returns[valid]
        if scores.size < 2:
            continue
        ic = _spearman(scores, returns)
        if not math.isfinite(ic):
            continue
        rows.append({"decision_date": str(decision_date), "ic": float(ic), "n": int(scores.size)})
    return pd.DataFrame(rows, columns=["decision_date", "ic", "n"])


def ic_summary(
    daily_ic: pd.DataFrame,
    *,
    rolling_windows: Sequence[int] = (20, 60),
    block_days: int = DEFAULT_BLOCK_TRADING_DAYS,
) -> dict[str, object]:
    """IC 汇总：均值/中位/ICIR/正比例/滚动/date-block CI。"""
    if daily_ic.empty:
        return {"status": "no_data", "mature_dates": 0}
    values = pd.to_numeric(daily_ic["ic"], errors="coerce").dropna()
    if values.empty:
        return {"status": "no_data", "mature_dates": 0}
    mean = float(values.mean())
    std = float(values.std(ddof=1)) if len(values) > 1 else float("nan")
    payload: dict[str, object] = {
        "status": "ok",
        "mature_dates": int(len(values)),
        "mean_ic": mean,
        "median_ic": float(values.median()),
        "icir": (mean / std) if std and math.isfinite(std) and std > 0 else NOT_AVAILABLE,
        "positive_ratio": float((values > 0).mean()),
        "std_ic": std if math.isfinite(std) else NOT_AVAILABLE,
    }
    for window in rolling_windows:
        rolled = values.rolling(int(window), min_periods=max(2, int(window) // 2)).mean()
        tail = rolled.dropna()
        payload[f"ic_{int(window)}d"] = float(tail.iloc[-1]) if not tail.empty else NOT_AVAILABLE
    ci = date_block_bootstrap_ci(
        list(zip(daily_ic["decision_date"], values, strict=False)),
        block_days=block_days,
    )
    payload["ci95"] = [ci["ci_low"], ci["ci_high"]]
    payload["ci_meta"] = {
        "method": ci["method"],
        "block_days": ci["block_days"],
        "valid_days": ci["valid_days"],
        "distinct_days": ci["distinct_days"],
    }
    payload["ci_crosses_zero"] = bool(
        not math.isfinite(ci["ci_low"])
        or not math.isfinite(ci["ci_high"])
        or ci["ci_low"] <= 0 <= ci["ci_high"]
    )
    return payload


# ---------------------------------------------------------------------------
# 分位单调性
# ---------------------------------------------------------------------------


def quantile_returns(
    frame: pd.DataFrame,
    *,
    score_column: str,
    metric_column_: str,
    quantiles: int = DEFAULT_QUANTILES,
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION,
) -> pd.DataFrame:
    """逐日分位收益（Q1=分数最低 … Qn=分数最高），再对分位聚合。"""
    assert_score_column(score_column)
    columns = ["quantile", "mean_return", "days", "positive_ratio"]
    if frame.empty or metric_column_ not in frame.columns:
        return pd.DataFrame(columns=columns)
    mask = usable_mask(frame, metric=metric_column_, min_cross_section=min_cross_section)
    usable = frame[mask]
    buckets: dict[int, list[float]] = {index + 1: [] for index in range(int(quantiles))}
    for _, group in usable.groupby("decision_date", sort=True):
        scores = pd.to_numeric(group[score_column], errors="coerce").to_numpy(dtype=float)
        returns = pd.to_numeric(group[metric_column_], errors="coerce").to_numpy(dtype=float)
        valid = np.isfinite(scores) & np.isfinite(returns)
        scores, returns = scores[valid], returns[valid]
        if scores.size < int(quantiles):
            continue
        order = np.argsort(scores, kind="mergesort")
        sorted_returns = returns[order]
        for index, bucket in enumerate(np.array_split(sorted_returns, int(quantiles)), start=1):
            if bucket.size:
                buckets[index].extend(float(value) for value in bucket)
    rows = [
        {
            "quantile": index,
            "mean_return": float(np.mean(values)) if values else float("nan"),
            "days": len(values),
            "positive_ratio": float(np.mean([value > 0 for value in values]))
            if values
            else float("nan"),
        }
        for index, values in sorted(buckets.items())
    ]
    return pd.DataFrame(rows, columns=columns)


def quantile_monotonicity(table: pd.DataFrame) -> dict[str, object]:
    """分位单调性：分位序号与平均收益的 Spearman，以及 top>bottom。"""
    if table.empty or len(table) < 2:
        return {"status": "no_data"}
    ranks = pd.to_numeric(table["quantile"], errors="coerce").to_numpy(dtype=float)
    means = pd.to_numeric(table["mean_return"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(ranks) & np.isfinite(means)
    if valid.sum() < 2:
        return {"status": "no_data"}
    return {
        "status": "ok",
        "monotonicity_rho": _spearman(ranks[valid], means[valid]),
        "top_minus_bottom": float(means[valid][-1] - means[valid][0]),
        "quantile_means": [float(value) for value in means[valid]],
    }


# ---------------------------------------------------------------------------
# TopK / 命中率
# ---------------------------------------------------------------------------


def select_top_k(frame: pd.DataFrame, *, score_column: str, k: int) -> pd.DataFrame:
    """逐日取分数最高的 k 只（缺失分数不参与排名）。"""
    assert_score_column(score_column)
    if frame.empty:
        return frame
    usable = frame.copy()
    usable["__score"] = pd.to_numeric(usable[score_column], errors="coerce")
    usable = usable[usable["__score"].notna()]
    usable["__rank"] = usable.groupby("decision_date")["__score"].rank(
        ascending=False, method="first"
    )
    selected = usable[usable["__rank"] <= max(1, int(k))].drop(columns=["__score", "__rank"])
    return selected


def topk_metrics(
    frame: pd.DataFrame,
    *,
    score_column: str,
    metric_columns: Sequence[str],
    ks: Sequence[int] = DEFAULT_TOP_KS,
) -> dict[str, dict[str, object]]:
    """TopK 的命中率与平均收益（含基准边际）。"""
    assert_score_column(score_column)
    output: dict[str, dict[str, object]] = {}
    for k in ks:
        selected = select_top_k(frame, score_column=score_column, k=int(k))
        entry: dict[str, object] = {
            "days": int(selected["decision_date"].nunique()) if not selected.empty else 0
        }
        for column in metric_columns:
            if column not in selected.columns or selected.empty:
                entry[column] = NOT_AVAILABLE
                entry[f"{column}_hit_rate"] = NOT_AVAILABLE
                continue
            values = pd.to_numeric(selected[column], errors="coerce").dropna()
            if values.empty:
                entry[column] = NOT_AVAILABLE
                entry[f"{column}_hit_rate"] = NOT_AVAILABLE
                continue
            entry[column] = float(values.mean())
            entry[f"{column}_hit_rate"] = float((values > 0).mean())
        output[f"top{int(k)}"] = entry
    return output


def hit_rate(
    frame: pd.DataFrame,
    *,
    metric_column_: str,
    ks: Sequence[int] = DEFAULT_TOP_KS,
    score_column: str = "score",
) -> dict[str, float]:
    metrics = topk_metrics(frame, score_column=score_column, metric_columns=[metric_column_], ks=ks)
    return {
        f"top{k}": float(metrics[f"top{k}"].get(f"{metric_column_}_hit_rate", float("nan")))
        for k in ks
    }


# ---------------------------------------------------------------------------
# 尾部 / 下行
# ---------------------------------------------------------------------------


def downside_metrics(
    frame: pd.DataFrame,
    *,
    horizon: int = PRIMARY_HORIZON,
    score_column: str | None = None,
) -> dict[str, object]:
    """MAE 均值、5% 尾部损失、大幅亏损频率（可选只在 TopK 上算）。"""
    mae_column = metric_column("mae", horizon)
    net_column = metric_column(METRIC_TOTAL_RETURN, horizon)
    scope = frame
    if score_column is not None and not frame.empty:
        scope = select_top_k(frame, score_column=score_column, k=5)
    payload: dict[str, object] = {"horizon": int(horizon), "rows": int(len(scope))}
    if scope.empty:
        return {**payload, "status": "no_data"}
    mask = usable_mask(scope, metric=net_column)
    usable = scope[mask]
    if usable.empty:
        return {**payload, "status": "no_data"}
    if mae_column in usable.columns:
        mae = pd.to_numeric(usable[mae_column], errors="coerce").dropna()
        payload["mean_mae"] = float(mae.mean()) if not mae.empty else NOT_AVAILABLE
    net = pd.to_numeric(usable[net_column], errors="coerce").dropna()
    if not net.empty:
        payload["mean_return"] = float(net.mean())
        payload["tail_loss_5pct"] = float(net.quantile(TAIL_LOSS_QUANTILE))
        payload["large_loss_frequency"] = float((net <= LARGE_LOSS_THRESHOLD).mean())
        payload["worst_return"] = float(net.min())
    payload["status"] = "ok"
    return payload


# ---------------------------------------------------------------------------
# 汇总块
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationSpec:
    """一次评价的口径（同一份口径贯穿 S15/S16/S19/S21）。"""

    score_column: str
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    primary_horizon: int = PRIMARY_HORIZON
    excess_kind: str = METRIC_EXCESS_RETURN
    total_kind: str = METRIC_TOTAL_RETURN
    top_ks: tuple[int, ...] = DEFAULT_TOP_KS
    quantiles: int = DEFAULT_QUANTILES
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION
    rolling_windows: tuple[int, ...] = (20, 60)

    def to_payload(self) -> dict[str, object]:
        return {
            "score_column": self.score_column,
            "horizons": [int(h) for h in self.horizons],
            "primary_horizon": int(self.primary_horizon),
            "top_ks": [int(k) for k in self.top_ks],
            "quantiles": int(self.quantiles),
            "min_cross_section": int(self.min_cross_section),
            "rolling_windows": [int(w) for w in self.rolling_windows],
            "statistics_unit": "decision_date",
        }


def evaluate_scores(frame: pd.DataFrame, spec: EvaluationSpec) -> dict[str, object]:
    """完整评价块：命中率 / 平均收益 / 平均超额 / 各 horizon IC / 分位 / 下行。"""
    assert_score_column(spec.score_column)
    payload: dict[str, object] = {"spec": spec.to_payload()}
    primary_excess = metric_column(spec.excess_kind, spec.primary_horizon)
    primary_total = metric_column(spec.total_kind, spec.primary_horizon)

    ic_by_horizon: dict[str, object] = {}
    for horizon in spec.horizons:
        key = int(horizon)
        excess = metric_column(spec.excess_kind, key)
        ic_by_horizon[f"{key}d"] = _ic_block(
            frame,
            score_column=spec.score_column,
            metric=excess,
            min_cross_section=spec.min_cross_section,
            rolling_windows=spec.rolling_windows,
        )
    payload["rank_ic"] = ic_by_horizon

    metric_columns = [
        column
        for column in (
            primary_excess,
            primary_total,
            metric_column(spec.excess_kind, 3),
            metric_column(spec.total_kind, 3),
        )
        if column in frame.columns
    ]
    payload["topk"] = topk_metrics(
        frame, score_column=spec.score_column, metric_columns=metric_columns, ks=spec.top_ks
    )
    table = quantile_returns(
        frame,
        score_column=spec.score_column,
        metric_column_=primary_excess,
        quantiles=spec.quantiles,
        min_cross_section=spec.min_cross_section,
    )
    payload["quantiles"] = {
        "table": table.to_dict(orient="records"),
        "monotonicity": quantile_monotonicity(table),
    }
    payload["downside_top5"] = downside_metrics(
        frame, horizon=spec.primary_horizon, score_column=spec.score_column
    )
    payload["mature_dates"] = _mature_dates(frame, metric=primary_excess)
    payload["research_gate"] = research_gate_status(int(payload["mature_dates"]))
    return payload


def _ic_block(
    frame: pd.DataFrame,
    *,
    score_column: str,
    metric: str,
    min_cross_section: int,
    rolling_windows: Sequence[int],
) -> dict[str, object]:
    daily = daily_rank_ic(
        frame,
        score_column=score_column,
        metric_column_=metric,
        min_cross_section=min_cross_section,
    )
    return ic_summary(daily, rolling_windows=rolling_windows)


def _mature_dates(frame: pd.DataFrame, *, metric: str) -> int:
    if frame.empty or metric not in frame.columns:
        return 0
    mask = usable_mask(frame, metric=metric)
    if not bool(mask.any()):
        return 0
    return int(frame.loc[mask, "decision_date"].nunique())


def research_gate_status(mature_dates: int) -> str:
    """研究样本门（蓝图 §7.4）——只描述样本阶段，不构成"模型有效"的结论。"""
    if mature_dates >= 250:
        return "governance_eligible"
    if mature_dates >= 120:
        return "advisory_eligible"
    if mature_dates >= 60:
        return "initial_direction_review"
    if mature_dates >= 20:
        return "failure_alert_only"
    return "insufficient"


def paired_delta(
    frame: pd.DataFrame,
    *,
    left_column: str,
    right_column: str,
    block_days: int = DEFAULT_BLOCK_TRADING_DAYS,
) -> dict[str, object]:
    """同日配对差值（左 − 右）的均值与 date-block CI。

    用于"V2 vs Simple Baseline""Top5 vs Quality300"这类**同日配对**比较：
    配对差里不含市场整体涨跌，只有相对增量。
    """
    if frame.empty or left_column not in frame.columns or right_column not in frame.columns:
        return {"status": "no_data"}
    left = pd.to_numeric(frame[left_column], errors="coerce")
    right = pd.to_numeric(frame[right_column], errors="coerce")
    valid = left.notna() & right.notna()
    if not bool(valid.any()):
        return {"status": "no_data"}
    delta = (left - right)[valid]
    days = frame.loc[valid, "decision_date"].astype(str)
    per_day = delta.groupby(days).mean()
    ci = date_block_bootstrap_ci(list(per_day.items()), block_days=block_days)
    return {
        "status": "ok",
        "rows": int(valid.sum()),
        "days": int(per_day.shape[0]),
        "mean_delta": float(delta.mean()),
        "mean_daily_delta": float(per_day.mean()),
        "ci95": [ci["ci_low"], ci["ci_high"]],
        "ci_crosses_zero": bool(ci["ci_low"] <= 0 <= ci["ci_high"]),
        "ci_meta": {"method": ci["method"], "block_days": ci["block_days"]},
    }


def _spearman(left: np.ndarray, right: np.ndarray) -> float:
    if left.size < 2 or right.size < 2:
        return float("nan")
    left_ranked = _rankdata(left)
    right_ranked = _rankdata(right)
    left_centered = left_ranked - left_ranked.mean()
    right_centered = right_ranked - right_ranked.mean()
    denominator = float(np.sqrt((left_centered**2).sum() * (right_centered**2).sum()))
    if denominator <= 0:
        return float("nan")
    return float((left_centered * right_centered).sum() / denominator)


def _rankdata(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    ranks[order] = np.arange(1, values.size + 1, dtype=float)
    # 并列取平均秩（与 Spearman 定义一致）
    sorted_values = values[order]
    index = 0
    while index < sorted_values.size:
        stop = index + 1
        while stop < sorted_values.size and sorted_values[stop] == sorted_values[index]:
            stop += 1
        if stop - index > 1:
            ranks[order[index:stop]] = (index + 1 + stop) / 2.0
        index = stop
    return ranks


__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_MIN_CROSS_SECTION",
    "DEFAULT_QUANTILES",
    "DEFAULT_TOP_KS",
    "EvaluationSpec",
    "LARGE_LOSS_THRESHOLD",
    "METRIC_EXCESS_RETURN",
    "METRIC_TOTAL_RETURN",
    "NOT_AVAILABLE",
    "PRIMARY_HORIZON",
    "SHORT_HORIZONS",
    "TAIL_LOSS_QUANTILE",
    "assert_score_column",
    "daily_rank_ic",
    "downside_metrics",
    "evaluate_scores",
    "hit_rate",
    "ic_summary",
    "metric_column",
    "paired_delta",
    "quantile_monotonicity",
    "quantile_returns",
    "research_gate_status",
    "select_top_k",
    "topk_metrics",
    "usable_mask",
]
