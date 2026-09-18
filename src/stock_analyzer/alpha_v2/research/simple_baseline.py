"""Alpha V2 Simple Factor Baseline（S15 / 原 P1-05）。

**公平性是本阶段的全部意义**。蓝图要求 ML 与简单基线在**完全相同**的条件下比较：

```text
相同 universe（同一 PIT 合格池与决策集合）
相同 entry（S02 的 T+1 可开盘成交，不可成交即 no_fill）
相同 outcome（S11 的多 horizon 可执行净/超额收益）
相同 benchmark（S12 的同一层基准）
相同 OOS window（S19 的同一 fold 划分）
相同评价指标（S21 的同一评价块）
```

任何一条不同，比较就不成立。因此本模块**不自己算 outcome、不自己选 OOS 窗口**，
只产出"分数列 + 成员掩码"，其余全部交给 S11/S12/S19 的同一套机制。

另外两个必备能力：

- :func:`compare_with_ml` 与 :func:`require_baseline_companion`：把"ML 必须与
  baseline 同屏"从口头约定变成可执行检查（蓝图："每次 ML 评估必须同时显示
  simple baseline"）；
- :func:`baseline_pool_mask`：给 S12 的 ``simple_baseline`` 基准层提供成员。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.factors import (
    BASELINE_FACTORS,
    blocked_groups_payload,
    composite_score,
    compute_factor_frame,
    factor_definitions,
    factor_diagnostics,
)
from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_HORIZONS,
    DEFAULT_MIN_CROSS_SECTION,
    NOT_AVAILABLE,
    PRIMARY_HORIZON,
    EvaluationSpec,
    daily_rank_ic,
    evaluate_scores,
    ic_summary,
    metric_column,
)

BASELINE_SCORE_COLUMN = "baseline_score"
BASELINE_SOURCE = "simple_factor_baseline_v1"

DEFAULT_TOP_FRACTION = 0.20
DEFAULT_WEIGHTING = "equal"


@dataclass(frozen=True, slots=True)
class SimpleBaselineSpec:
    """简单因子基线的口径（因子集合、权重方式、截面下限）。"""

    factors: tuple[str, ...] = BASELINE_FACTORS
    weighting: str = DEFAULT_WEIGHTING
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION
    top_fraction: float = DEFAULT_TOP_FRACTION

    def to_payload(self) -> dict[str, object]:
        return {
            "source": BASELINE_SOURCE,
            "factors": list(self.factors),
            "weighting": self.weighting,
            "min_cross_section": int(self.min_cross_section),
            "top_fraction": float(self.top_fraction),
            "no_fitting": True,
            "note": (
                "无训练、无超参、无标签消费：分数 = 方向统一后的截面 rank 等权平均；"
                "评测时必须与 ML 使用同一 universe/entry/outcome/benchmark/OOS window"
            ),
        }


@dataclass
class SimpleBaselineResult:
    """baseline 分数帧 + 定义/覆盖率的自述报告。"""

    frame: pd.DataFrame
    spec: SimpleBaselineSpec
    report: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_payload(),
            "rows": int(len(self.frame)),
            "report": dict(self.report),
        }


def compute_simple_baseline(
    *,
    panel: object,
    decisions: Sequence[object],
    spec: SimpleBaselineSpec | None = None,
) -> SimpleBaselineResult:
    """计算 baseline 分数帧（``decision_date`` / ``symbol`` / 各因子 / ``baseline_score``）。"""
    resolved = spec or SimpleBaselineSpec()
    raw = compute_factor_frame(panel=panel, decisions=decisions, factors=resolved.factors)
    frame = raw
    if not frame.empty:
        frame = frame.copy()
        frame[BASELINE_SCORE_COLUMN] = composite_score(
            frame, factors=resolved.factors, min_cross_section=resolved.min_cross_section
        )
        for name in resolved.factors:
            frame[f"rank_{name}"] = (
                pd.to_numeric(frame[name], errors="coerce")
                .groupby(frame["decision_date"])
                .rank(pct=True)
            )
    else:
        frame = pd.DataFrame(
            columns=["decision_date", "symbol", *resolved.factors, BASELINE_SCORE_COLUMN]
        )
    report = {
        "factor_definitions": factor_definitions(factors=resolved.factors),
        "factor_coverage": factor_diagnostics(frame, factors=resolved.factors).to_dict(
            orient="records"
        ),
        "blocked_groups": blocked_groups_payload(),
        "scored_rows": int(frame[BASELINE_SCORE_COLUMN].notna().sum()) if not frame.empty else 0,
        "unscored_rows": int(frame[BASELINE_SCORE_COLUMN].isna().sum()) if not frame.empty else 0,
        "min_cross_section": int(resolved.min_cross_section),
    }
    return SimpleBaselineResult(frame=frame, spec=resolved, report=report)


def baseline_pool_mask(
    baseline: pd.DataFrame,
    *,
    top_fraction: float = DEFAULT_TOP_FRACTION,
    score_column: str = BASELINE_SCORE_COLUMN,
) -> pd.Series:
    """baseline 层基准的成员：每日分数前 ``top_fraction``。

    用于 S12 的 ``simple_baseline`` 基准层——"ML 是否真的赢过简单因子组合"，
    既要看分数相关性，也要看**同池对照**：ML 的 TopK 是不是只是 baseline 榜上
    的那批票。
    """
    if baseline.empty or score_column not in baseline.columns:
        return pd.Series(dtype=bool)
    values = pd.to_numeric(baseline[score_column], errors="coerce")
    fraction = max(0.0, min(1.0, float(top_fraction)))
    ranks = values.groupby(baseline["decision_date"]).rank(ascending=False, method="first")
    counts = values.groupby(baseline["decision_date"]).transform("count")
    target = (counts * fraction).apply(lambda value: max(1, int(round(float(value)))))
    return (ranks <= target) & values.notna()


def evaluate_baseline(
    frame: pd.DataFrame,
    *,
    spec: SimpleBaselineSpec | None = None,
    score_column: str = BASELINE_SCORE_COLUMN,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    primary_horizon: int = PRIMARY_HORIZON,
    top_ks: Sequence[int] = (1, 3, 5),
) -> dict[str, object]:
    """用**与 ML 完全相同**的评价块评估 baseline。"""
    resolved = spec or SimpleBaselineSpec()
    eval_spec = EvaluationSpec(
        score_column=score_column,
        horizons=tuple(int(h) for h in horizons),
        primary_horizon=int(primary_horizon),
        top_ks=tuple(int(k) for k in top_ks),
        min_cross_section=resolved.min_cross_section,
    )
    payload = evaluate_scores(frame, eval_spec)
    payload["baseline"] = {"spec": resolved.to_payload(), "source": BASELINE_SOURCE}
    return payload


def factor_ic_declared_vs_realized(
    frame: pd.DataFrame,
    *,
    factors: Sequence[str] = BASELINE_FACTORS,
    horizon: int = PRIMARY_HORIZON,
    metric: str = "excess_return",
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION,
) -> pd.DataFrame:
    """每个因子的**登记方向**与**实测 IC** 并列——事后翻方向必须留下痕迹。"""
    from stock_analyzer.alpha_v2.research.factors import factor_definitions

    definitions = {item["name"]: item for item in factor_definitions(factors=factors)}
    column = metric_column(metric, horizon)
    rows: list[dict[str, object]] = []
    for name in factors:
        if name not in frame.columns or column not in frame.columns:
            continue
        daily = daily_rank_ic(
            frame,
            score_column=name,
            metric_column_=column,
            min_cross_section=min_cross_section,
        )
        summary = ic_summary(daily, rolling_windows=())
        definition = definitions.get(name, {})
        mean_ic = summary.get("mean_ic", float("nan"))
        direction = int(definition.get("direction", 1))
        rows.append(
            {
                "factor": name,
                "group": definition.get("group", ""),
                "declared_direction": direction,
                "mean_ic": mean_ic,
                "direction_consistent": (
                    NOT_AVAILABLE
                    if not isinstance(mean_ic, (int, float)) or np.isnan(mean_ic)
                    else bool(np.sign(float(mean_ic)) == np.sign(direction))
                ),
                "mature_dates": summary.get("mature_dates", 0),
            }
        )
    return pd.DataFrame(rows)


def compare_with_ml(
    ml_payload: Mapping[str, object],
    baseline_payload: Mapping[str, object],
    *,
    horizon: int = PRIMARY_HORIZON,
    top_k: int = 5,
    ic_tolerance: float = 0.0,
) -> dict[str, object]:
    """ML vs baseline 的可判定比较（**不是**"看一眼"）。

    判据（与蓝图 §20 的停止条件一致）：

    - ``rank_ic``：ML 的 primary horizon 平均 IC 是否高于 baseline（需超过容差）；
    - ``topk_excess``：ML 的 TopK 平均超额是否高于 baseline；
    - ``sample_gate``：两者各自成熟日期数 —— 样本门不到 60 时结论只能是"方向待验证"。
    """
    ml_ic = _extract_ic(ml_payload, horizon)
    baseline_ic = _extract_ic(baseline_payload, horizon)
    ml_top = _extract_topk(ml_payload, top_k)
    baseline_top = _extract_topk(baseline_payload, top_k)
    gate_dates = min(
        int(ml_payload.get("mature_dates", 0) or 0),
        int(baseline_payload.get("mature_dates", 0) or 0),
    )
    ic_delta = _delta(ml_ic, baseline_ic)
    top_delta = _delta(ml_top, baseline_top)
    beats = bool(
        ic_delta is not None
        and top_delta is not None
        and ic_delta > float(ic_tolerance)
        and top_delta > 0.0
    )
    return {
        "ml_mean_ic": ml_ic,
        "baseline_mean_ic": baseline_ic,
        "ic_delta": ic_delta,
        "ml_topk_excess": ml_top,
        "baseline_topk_excess": baseline_top,
        "topk_excess_delta": top_delta,
        "ml_beats_baseline": beats,
        "mature_dates_min": gate_dates,
        "verdict": _compare_verdict(beats, ic_delta, top_delta, gate_dates),
    }


def _compare_verdict(
    beats: bool, ic_delta: float | None, top_delta: float | None, gate_dates: int
) -> str:
    if ic_delta is None or top_delta is None:
        return "insufficient_evidence"
    if gate_dates < 60:
        return "awaiting_sample" if beats else "baseline_not_beaten_awaiting_sample"
    return "ml_beats_baseline" if beats else "baseline_not_beaten_stop_adding_complexity"


def require_baseline_companion(payload: Mapping[str, object], *, label: str = "ml") -> None:
    """ML 评测块必须携带 baseline 同屏结果（蓝图 §P1-05 的 Done When）。"""
    if "baseline_companion" not in payload:
        raise ValueError(
            f"{label} 评测缺少 simple baseline 同屏结果（baseline_companion）："
            "ML 与简单基线必须使用同一 universe/entry/outcome/benchmark/OOS 窗口同时呈现"
        )


def _extract_ic(payload: Mapping[str, object], horizon: int) -> float | None:
    rank_ic = payload.get("rank_ic")
    if not isinstance(rank_ic, Mapping):
        return None
    block = rank_ic.get(f"{int(horizon)}d")
    if not isinstance(block, Mapping):
        return None
    value = block.get("mean_ic")
    if isinstance(value, (int, float)) and not np.isnan(float(value)):
        return float(value)
    return None


def _extract_topk(payload: Mapping[str, object], top_k: int) -> float | None:
    topk = payload.get("topk")
    if not isinstance(topk, Mapping):
        return None
    block = topk.get(f"top{int(top_k)}")
    if not isinstance(block, Mapping):
        return None
    value = block.get(metric_column("excess_return", PRIMARY_HORIZON))
    if isinstance(value, (int, float)) and not np.isnan(float(value)):
        return float(value)
    return None


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return float(left - right)


__all__ = [
    "BASELINE_SCORE_COLUMN",
    "BASELINE_SOURCE",
    "DEFAULT_TOP_FRACTION",
    "SimpleBaselineResult",
    "SimpleBaselineSpec",
    "baseline_pool_mask",
    "compare_with_ml",
    "compute_simple_baseline",
    "evaluate_baseline",
    "factor_ic_declared_vs_realized",
    "require_baseline_companion",
]
