"""Alpha V2 Winner Recall（S13 / 原 P1-03）。

**它回答什么**：当天真正会涨的那批股票，在漏斗的哪一级被筛掉了？

```text
Eligible ─► Quality300 ─► Light100 ─► Deep50 ─► Final
   │            │            │           │         │
   └────────────┴────────────┴───────────┴─────────┘
        Recall@each  = 赢家在这级还剩多少
```

三条硬纪律（对应 Gate S13 的 Blocking 项）：

1. **赢家由未来真实可执行超额收益定义**，不是由任何模型分数定义。本模块的
   排名列只接受 outcome 族列名（``excess_return_*d`` / ``net_return_*d``），
   传预测分数进来会**直接报错**——自证循环（用分数定义赢家、再夸分数召回率高）
   是漏斗指标最容易犯的错；
2. **按 decision_date 分组**：每天的赢家集合独立计算，跨日不混；
3. **样本不足就说不足**：某天池子太小（< ``min_pool_size``）不计入统计，
   成熟日期数一并报出，用于对照研究样本门（20/60/120/250）。

除每日值外还给出 20D / 60D 滚动，以及"漏斗各级赢家留存曲线"。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

NOT_AVAILABLE = "not_available"

# 只有 outcome 族列名可以定义赢家（正则守卫，见 :func:`assert_outcome_metric`）。
_OUTCOME_METRIC_PATTERN = re.compile(r"^(excess_return|net_return|mae|mfe)_\d+d$")

STAGE_QUALITY = "quality"
STAGE_LIGHT = "light"
STAGE_DEEP = "deep"
STAGE_FINAL = "final"

DEFAULT_STAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    (STAGE_QUALITY, "quality_pool"),
    (STAGE_LIGHT, "light_pool"),
    (STAGE_DEEP, "deep_pool"),
    (STAGE_FINAL, "final_pool"),
)

DEFAULT_ROLLING_WINDOWS: tuple[int, ...] = (20, 60)
DEFAULT_WINNER_QUANTILE = 0.10
DEFAULT_MIN_POOL_SIZE = 20


def assert_outcome_metric(column: str) -> str:
    """赢家排名列必须是 outcome 族；否则立即报错（禁止自证循环）。"""
    text = str(column).strip()
    if not _OUTCOME_METRIC_PATTERN.match(text):
        raise ValueError(
            f"winner 排序必须使用真实 outcome 列（excess_return_*/net_return_*/mae_*/mfe_*），"
            f"收到 {column!r}：用预测分数定义赢家会形成自证循环（Gate S13 Blocking）"
        )
    return text


@dataclass(frozen=True, slots=True)
class RecallSpec:
    """一次 winner recall 计算的口径。"""

    metric: str = "excess_return_5d"
    winner_quantile: float = DEFAULT_WINNER_QUANTILE
    winner_top_n: int = 0
    stage_columns: tuple[tuple[str, str], ...] = DEFAULT_STAGE_COLUMNS
    rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS
    min_pool_size: int = DEFAULT_MIN_POOL_SIZE
    scope_column: str = "quality_pool"

    def to_payload(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "winner_quantile": float(self.winner_quantile),
            "winner_top_n": int(self.winner_top_n),
            "winner_definition": "top_by_future_real_executable_outcome",
            "scope_column": self.scope_column,
            "stage_columns": {name: column for name, column in self.stage_columns},
            "rolling_windows": [int(window) for window in self.rolling_windows],
            "min_pool_size": int(self.min_pool_size),
            "grouped_by": "decision_date",
        }


@dataclass
class WinnerRecallReport:
    """每日召回 + 滚动 + 留存曲线。"""

    spec: RecallSpec
    daily: pd.DataFrame
    summary: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_payload(),
            "summary": dict(self.summary),
            "daily_rows": int(len(self.daily)),
        }


def winner_mask(
    frame: pd.DataFrame,
    *,
    sort_column: str,
    quantile: float = DEFAULT_WINNER_QUANTILE,
    top_n: int = 0,
) -> pd.Series:
    """按真实 outcome 逐日取"赢家"（Top quantile 或 Top N）。"""
    metric = assert_outcome_metric(sort_column)
    if frame.empty:
        return pd.Series(dtype=bool)
    values = pd.to_numeric(frame[metric], errors="coerce")
    mask = pd.Series(False, index=frame.index)
    for _, group in frame.groupby("decision_date", sort=False):
        usable = pd.to_numeric(group[metric], errors="coerce")
        usable = usable[np.isfinite(usable.to_numpy(dtype=float))]
        if usable.empty:
            continue
        if int(top_n) > 0:
            count = min(int(top_n), len(usable))
        else:
            count = max(1, int(round(len(usable) * max(0.0, min(1.0, float(quantile))))))
        selected = usable.sort_values(ascending=False).index[:count]
        mask.loc[selected] = True
    del values
    return mask


def compute_winner_recall(
    frame: pd.DataFrame,
    *,
    spec: RecallSpec | None = None,
) -> WinnerRecallReport:
    """逐日计算赢家在漏斗各级的召回率。

    ``frame`` 需要：``decision_date`` / ``symbol`` / outcome 列 / 各级成员布尔列。
    缺失的级列按 ``not_available`` 处理，**不猜**成"全都留下来了"。
    """
    resolved = spec or RecallSpec()
    assert_outcome_metric(resolved.metric)
    stage_columns = dict(resolved.stage_columns)
    if frame.empty or resolved.metric not in frame.columns:
        return WinnerRecallReport(
            spec=resolved,
            daily=pd.DataFrame(columns=["decision_date", "pool_size", "winner_count"]),
            summary={
                "status": "no_data",
                "mature_dates": 0,
                "reason": f"missing metric column {resolved.metric}",
            },
        )

    scope_column = resolved.scope_column
    if scope_column in frame.columns:
        scope = frame[frame[scope_column].fillna(False).astype(bool)]
    else:
        scope = frame
    # 用 scope 自己的布尔列过滤（用全帧的布尔列去切子集会被 pandas 重索引成空）
    if "executable" in scope.columns:
        scope = scope[scope["executable"].fillna(False).astype(bool)]

    rows: list[dict[str, object]] = []
    for decision_date, group in scope.groupby("decision_date", sort=True):
        values = pd.to_numeric(group[resolved.metric], errors="coerce")
        usable = group[np.isfinite(values.to_numpy(dtype=float))]
        if len(usable) < max(1, int(resolved.min_pool_size)):
            continue
        mask = winner_mask(
            usable,
            sort_column=resolved.metric,
            quantile=resolved.winner_quantile,
            top_n=resolved.winner_top_n,
        )
        winners = usable[mask]
        row: dict[str, object] = {
            "decision_date": str(decision_date),
            "pool_size": int(len(usable)),
            "winner_count": int(len(winners)),
            "pool_mean_return": float(
                pd.to_numeric(usable[resolved.metric], errors="coerce").mean()
            ),
            "winner_mean_return": float(
                pd.to_numeric(winners[resolved.metric], errors="coerce").mean()
            ),
        }
        for stage, column in stage_columns.items():
            if column not in usable.columns:
                row[f"recall_{stage}"] = NOT_AVAILABLE
                row[f"stage_size_{stage}"] = NOT_AVAILABLE
                continue
            stage_mask = usable[column].fillna(False).astype(bool)
            row[f"recall_{stage}"] = (
                float(stage_mask[mask].sum()) / float(len(winners)) if len(winners) else 0.0
            )
            row[f"stage_size_{stage}"] = int(stage_mask.sum())
        rows.append(row)

    daily = pd.DataFrame(rows)
    summary = _summarize(daily, spec=resolved)
    return WinnerRecallReport(spec=resolved, daily=daily, summary=summary)


def _summarize(daily: pd.DataFrame, *, spec: RecallSpec) -> dict[str, object]:
    if daily.empty:
        return {
            "status": "no_mature_dates",
            "mature_dates": 0,
            "research_gate": _research_gate(0),
        }
    summary: dict[str, object] = {
        "status": "ok",
        "mature_dates": int(len(daily)),
        "research_gate": _research_gate(len(daily)),
        "median_pool_size": float(daily["pool_size"].median()),
        "median_winner_count": float(daily["winner_count"].median()),
        "winner_minus_pool_mean": float(
            (daily["winner_mean_return"] - daily["pool_mean_return"]).mean()
        ),
    }
    for stage, _ in spec.stage_columns:
        column = f"recall_{stage}"
        if column not in daily.columns:
            continue
        values = pd.to_numeric(daily[column], errors="coerce").dropna()
        if values.empty:
            summary[f"recall_{stage}_mean"] = NOT_AVAILABLE
            continue
        summary[f"recall_{stage}_mean"] = float(values.mean())
        for window in spec.rolling_windows:
            rolled = values.rolling(int(window), min_periods=max(2, int(window) // 2)).mean()
            tail = rolled.dropna()
            summary[f"recall_{stage}_{int(window)}d"] = (
                float(tail.iloc[-1]) if not tail.empty else NOT_AVAILABLE
            )
    return summary


def _research_gate(mature_dates: int) -> str:
    """研究样本门（蓝图 §7.4）：只描述样本阶段，绝不据此宣称模型有效。"""
    if mature_dates >= 250:
        return "governance_eligible"
    if mature_dates >= 120:
        return "advisory_eligible"
    if mature_dates >= 60:
        return "initial_direction_review"
    if mature_dates >= 20:
        return "failure_alert_only"
    return "insufficient"


def recall_curve(
    frame: pd.DataFrame,
    *,
    spec: RecallSpec | None = None,
) -> dict[str, float]:
    """漏斗各级对"赢家"的留存率（pool → 各级），用于定位赢家被哪级杀掉。"""
    resolved = spec or RecallSpec()
    report = compute_winner_recall(frame, spec=resolved)
    if report.daily.empty:
        return {}
    curve: dict[str, float] = {}
    for stage, _ in resolved.stage_columns:
        column = f"recall_{stage}"
        if column in report.daily.columns:
            values = pd.to_numeric(report.daily[column], errors="coerce").dropna()
            if not values.empty:
                curve[stage] = float(values.mean())
    return curve


def stage_survival_table(
    frame: pd.DataFrame,
    *,
    spec: RecallSpec | None = None,
) -> pd.DataFrame:
    """各级池子的规模与"赢家占比"，用来判断是不是某一级把池子砍得太狠。"""
    resolved = spec or RecallSpec()
    columns = [
        "decision_date",
        "pool_size",
        "winner_count",
        "winner_mean_return",
        "pool_mean_return",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    report = compute_winner_recall(frame, spec=resolved)
    return report.daily[columns] if not report.daily.empty else pd.DataFrame(columns=columns)


def build_stage_membership(
    frame: pd.DataFrame,
    *,
    stage_sizes: Mapping[str, int],
    rank_column: str,
) -> pd.DataFrame:
    """按名次列构造各级成员布尔列（漏斗名次 → 是否进入该级）。

    ``rank_column`` 是**漏斗名次**（越大越靠后），不是预测分数；用名次而不是
    分数是为了让"第几名被砍掉"这件事可复现，也不引入新的排序口径。
    """
    result = frame.copy()
    rank = pd.to_numeric(result[rank_column], errors="coerce")
    for stage, size in stage_sizes.items():
        # 名次 1..size 为"进入该级"；缺失名次（NaN 比较恒 False）自然落选。
        result[f"{stage}_pool"] = rank.le(int(size)) & rank.ge(1)
    return result


__all__ = [
    "DEFAULT_ROLLING_WINDOWS",
    "DEFAULT_STAGE_COLUMNS",
    "DEFAULT_WINNER_QUANTILE",
    "NOT_AVAILABLE",
    "RecallSpec",
    "STAGE_DEEP",
    "STAGE_FINAL",
    "STAGE_LIGHT",
    "STAGE_QUALITY",
    "WinnerRecallReport",
    "assert_outcome_metric",
    "build_stage_membership",
    "compute_winner_recall",
    "recall_curve",
    "stage_survival_table",
    "winner_mask",
]
