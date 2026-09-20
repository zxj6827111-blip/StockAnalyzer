"""Alpha V2 Theme / News / Intraday 增量实验框架（S23 / 原 P3）。

**这不是"再加几个信号"的阶段**，而是"如何公平地判断某个信号有没有增量"的阶段。
蓝图 §13 的判据只有一条：

```text
same date + same pool + same base alpha
control    = base
experiment = base + 信号
看 paired excess（同日配对超额），不看"出票变多"
```

因此本模块的设计要点：

1. **配对**：两臂在同一天、同一候选池、同一 K 上比较，逐日取差再平均
   （date-block CI）；出票数量完全一致，**"票多了"在结构上不可能成为成功证据**；
2. **受影响子集**：只有"信号真的改变了排序"的日子/标的才算有效影响，
   样本门按蓝图要求：``>=60`` 交易日、``>=30`` 独立受影响日、``>=200`` 受影响 symbol-day；
3. **前置条件不满足就阻断**：News 需要 PIT 路径完整；Intraday 需要
   coverage/freshness/PIT 三项证明，且**禁止把不完整分钟数据填 0 后混训**
   ——这些在代码里是 ``status=blocked`` 的显式返回，不是注释里的提醒。

Theme 在本阶段保持 shadow：本模块只给"能不能做实验"的门与实验本身，不启用任何
信号（``enabled=False`` 恒成立）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pandas as pd

from stock_analyzer.alpha_v2.research.metrics import (
    NOT_AVAILABLE,
    PRIMARY_HORIZON,
    metric_column,
    paired_delta,
    research_gate_status,
)

EXPERIMENT_THEME = "theme"
EXPERIMENT_NEWS = "news"
EXPERIMENT_INTRAADAY = "intraday"

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_INSUFFICIENT_SAMPLE = "insufficient_sample"

MODE_SHADOW = "shadow"

DEFAULT_MIN_TRADING_DATES = 60
DEFAULT_MIN_AFFECTED_DATES = 30
DEFAULT_MIN_AFFECTED_SYMBOL_DATES = 200
DEFAULT_MIN_SCORE_DELTA = 1e-9


@dataclass(frozen=True, slots=True)
class ExperimentGate:
    """样本门（蓝图 §13.1 的数值要求，全部可配置但默认不放宽）。"""

    min_trading_dates: int = DEFAULT_MIN_TRADING_DATES
    min_affected_dates: int = DEFAULT_MIN_AFFECTED_DATES
    min_affected_symbol_dates: int = DEFAULT_MIN_AFFECTED_SYMBOL_DATES
    min_score_delta: float = DEFAULT_MIN_SCORE_DELTA

    def to_payload(self) -> dict[str, object]:
        return {
            "min_trading_dates": int(self.min_trading_dates),
            "min_affected_dates": int(self.min_affected_dates),
            "min_affected_symbol_dates": int(self.min_affected_symbol_dates),
            "min_score_delta": float(self.min_score_delta),
        }


@dataclass(frozen=True, slots=True)
class IncrementalExperimentSpec:
    """一次增量实验的口径。"""

    kind: str = EXPERIMENT_THEME
    base_score_column: str = "alpha_rank_score"
    experiment_score_column: str = "alpha_rank_score_with_signal"
    metric_column: str = ""
    top_k: int = 5
    horizon: int = PRIMARY_HORIZON
    gate: ExperimentGate = field(default_factory=ExperimentGate)
    mode: str = MODE_SHADOW
    enabled: bool = False
    lookback_windows: tuple[str, ...] = ("20d", "60d")

    def resolved_metric(self) -> str:
        return self.metric_column or metric_column("excess_return", self.horizon)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "base_score_column": self.base_score_column,
            "experiment_score_column": self.experiment_score_column,
            "metric_column": self.resolved_metric(),
            "top_k": int(self.top_k),
            "horizon": int(self.horizon),
            "mode": self.mode,
            "enabled": bool(self.enabled),
            "gate": self.gate.to_payload(),
            "lookback_windows": list(self.lookback_windows),
            "success_criterion": "same_day_paired_excess",
            "forbidden_success_evidence": (
                "出票数量增加 / 命中率上升但收益与超额未改善 / 只在少数日子上有差异"
            ),
        }


# ---------------------------------------------------------------------------
# 前置条件
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    """一个信号进入实验前必须给出的证明。"""

    kind: str
    pit_path_verified: bool = False
    coverage_verified: bool = False
    freshness_verified: bool = False
    zero_fill_mixing: bool = False
    notes: str = ""

    def blocking_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.kind == EXPERIMENT_NEWS and not self.pit_path_verified:
            reasons.append("news_pit_path_incomplete")
        if self.kind == EXPERIMENT_INTRAADAY:
            if not self.coverage_verified:
                reasons.append("intraday_coverage_unverified")
            if not self.freshness_verified:
                reasons.append("intraday_freshness_unverified")
            if not self.pit_path_verified:
                reasons.append("intraday_pit_unverified")
            if self.zero_fill_mixing:
                reasons.append("intraday_zero_fill_mixing_forbidden")
        return reasons

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "pit_path_verified": bool(self.pit_path_verified),
            "coverage_verified": bool(self.coverage_verified),
            "freshness_verified": bool(self.freshness_verified),
            "zero_fill_mixing": bool(self.zero_fill_mixing),
            "notes": self.notes,
            "blocking_reasons": self.blocking_reasons(),
        }


def readiness_block(
    *, kind: str, evidence: ReadinessEvidence | Mapping[str, object] | None = None
) -> dict[str, object]:
    """判定某类增量实验是否具备开跑条件（不具备就 ``blocked``）。"""
    resolved = (
        evidence
        if isinstance(evidence, ReadinessEvidence)
        else ReadinessEvidence(kind=kind, **(dict(evidence) if evidence else {}))
    )
    reasons = resolved.blocking_reasons()
    return {
        "kind": kind,
        "status": STATUS_BLOCKED if reasons else STATUS_OK,
        "blocked_reasons": reasons,
        "evidence": resolved.to_payload(),
    }


# ---------------------------------------------------------------------------
# 同日配对实验
# ---------------------------------------------------------------------------


def _select_top_k(frame: pd.DataFrame, *, score_column: str, k: int) -> pd.DataFrame:
    values = pd.to_numeric(frame[score_column], errors="coerce")
    ranked = frame.assign(__score=values).dropna(subset=["__score"])
    ranked = ranked.sort_values(
        ["decision_date", "__score", "symbol"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    ranked["__rank"] = ranked.groupby("decision_date").cumcount() + 1
    return ranked[ranked["__rank"] <= max(1, int(k))]


def run_incremental_experiment(
    frame: pd.DataFrame,
    *,
    spec: IncrementalExperimentSpec,
    evidence: ReadinessEvidence | Mapping[str, object] | None = None,
) -> dict[str, object]:
    """执行（或阻断）一次同日配对增量实验。

    两臂选择规模**完全相同**（同池同 K），差异只来自排序——因此"出票变多"
    在结构上不可能被当成证据。
    """
    readiness = readiness_block(kind=spec.kind, evidence=evidence)
    payload: dict[str, object] = {
        "spec": spec.to_payload(),
        "readiness": readiness,
        "status": STATUS_BLOCKED if readiness["status"] == STATUS_BLOCKED else STATUS_OK,
    }
    if readiness["status"] == STATUS_BLOCKED:
        payload["reason"] = "readiness_evidence_incomplete"
        return payload
    if frame.empty:
        payload["status"] = "no_data"
        return payload
    metric = spec.resolved_metric()
    required = {spec.base_score_column, spec.experiment_score_column, metric}
    missing = sorted(column for column in required if column not in frame.columns)
    if missing:
        payload["status"] = "no_data"
        payload["missing_columns"] = missing
        return payload

    scoped = frame[pd.to_numeric(frame[metric], errors="coerce").notna()]
    if "executable" in scoped.columns:
        scoped = scoped[scoped["executable"].fillna(False).astype(bool)]
    if scoped.empty:
        payload["status"] = "no_data"
        payload["reason"] = "no_usable_rows"
        return payload

    base = _select_top_k(scoped, score_column=spec.base_score_column, k=spec.top_k)
    experiment = _select_top_k(scoped, score_column=spec.experiment_score_column, k=spec.top_k)
    base_daily = _daily_mean(base, metric=metric)
    experiment_daily = _daily_mean(experiment, metric=metric)
    paired = pd.DataFrame(
        {
            "decision_date": base_daily.index,
            "base": base_daily.to_numpy(),
            "experiment": experiment_daily.to_numpy(),
        }
    ).dropna()
    delta = paired_delta(paired, left_column="experiment", right_column="base")

    affected = _affected_rows(
        scoped,
        base_score=spec.base_score_column,
        experiment_score=spec.experiment_score_column,
        min_delta=spec.gate.min_score_delta,
    )
    affected_dates = int(affected["decision_date"].nunique()) if not affected.empty else 0
    trading_dates = int(scoped["decision_date"].nunique())
    gate_pass = (
        trading_dates >= spec.gate.min_trading_dates
        and affected_dates >= spec.gate.min_affected_dates
        and int(len(affected)) >= spec.gate.min_affected_symbol_dates
    )
    payload.update(
        {
            "status": STATUS_OK if gate_pass else STATUS_INSUFFICIENT_SAMPLE,
            "trading_dates": trading_dates,
            "affected_dates": affected_dates,
            "affected_symbol_dates": int(len(affected)),
            "selection_size_per_arm": int(spec.top_k),
            "arms_size_equal": int(len(base)) == int(len(experiment)),
            "turnover_between_arms": int(
                len(set(base["symbol"].astype(str)) ^ set(experiment["symbol"].astype(str)))
            ),
            "paired_excess": delta,
            "research_gate": research_gate_status(trading_dates),
            "gate_pass": bool(gate_pass),
            "verdict": _verdict(delta, gate_pass=gate_pass),
            "note": (
                "成功判据只有同日配对超额；两臂选择规模完全相同，因此「票变多」在结构上不构成证据"
            ),
        }
    )
    return payload


def _daily_mean(frame: pd.DataFrame, *, metric: str) -> pd.Series:
    values = pd.to_numeric(frame[metric], errors="coerce")
    return values.groupby(frame["decision_date"].astype(str)).mean()


def _affected_rows(
    frame: pd.DataFrame, *, base_score: str, experiment_score: str, min_delta: float
) -> pd.DataFrame:
    base = pd.to_numeric(frame[base_score], errors="coerce")
    experiment = pd.to_numeric(frame[experiment_score], errors="coerce")
    delta = (experiment - base).abs()
    return frame[delta.notna() & (delta > float(min_delta))]


def _verdict(delta: Mapping[str, object], *, gate_pass: bool) -> str:
    if delta.get("status") != "ok":
        return "inconclusive"
    if not gate_pass:
        return "awaiting_sample"
    mean_delta = delta.get("mean_delta")
    crosses = bool(delta.get("ci_crosses_zero", True))
    if not isinstance(mean_delta, (int, float)):
        return "inconclusive"
    if crosses:
        return "inconclusive"
    return "incremental_positive" if float(mean_delta) > 0 else "incremental_negative"


def theme_experiment_spec(**overrides: object) -> IncrementalExperimentSpec:
    """Theme 实验的默认口径（保持 shadow，不启用）。"""
    payload: dict[str, object] = {
        "kind": EXPERIMENT_THEME,
        "base_score_column": "alpha_rank_score",
        "experiment_score_column": "alpha_rank_score_with_theme",
        "mode": MODE_SHADOW,
        "enabled": False,
    }
    payload.update(overrides)
    return IncrementalExperimentSpec(**payload)  # type: ignore[arg-type]


def news_experiment_spec(**overrides: object) -> IncrementalExperimentSpec:
    payload: dict[str, object] = {
        "kind": EXPERIMENT_NEWS,
        "base_score_column": "alpha_rank_score",
        "experiment_score_column": "alpha_rank_score_with_news",
        "mode": MODE_SHADOW,
        "enabled": False,
    }
    payload.update(overrides)
    return IncrementalExperimentSpec(**payload)  # type: ignore[arg-type]


def intraday_experiment_spec(**overrides: object) -> IncrementalExperimentSpec:
    payload: dict[str, object] = {
        "kind": EXPERIMENT_INTRAADAY,
        "base_score_column": "alpha_rank_score",
        "experiment_score_column": "alpha_rank_score_with_intraday",
        "mode": MODE_SHADOW,
        "enabled": False,
    }
    payload.update(overrides)
    return IncrementalExperimentSpec(**payload)  # type: ignore[arg-type]


def default_readiness() -> dict[str, dict[str, object]]:
    """当前仓库实测状态下的就绪度（Theme 可做实验；News/Intraday 被阻断）。"""
    return {
        EXPERIMENT_THEME: readiness_block(
            kind=EXPERIMENT_THEME,
            evidence=ReadinessEvidence(
                kind=EXPERIMENT_THEME,
                pit_path_verified=True,
                notes="theme 已在 shadow；板块/概念映射走 tushare 板块（9/11 生产验证通过）",
            ),
        ),
        EXPERIMENT_NEWS: readiness_block(
            kind=EXPERIMENT_NEWS,
            evidence=ReadinessEvidence(
                kind=EXPERIMENT_NEWS,
                pit_path_verified=False,
                notes="新闻发布时间尚未证明 <= 决策时点（蓝图 §3.4 保持 shadow）",
            ),
        ),
        EXPERIMENT_INTRAADAY: readiness_block(
            kind=EXPERIMENT_INTRAADAY,
            evidence=ReadinessEvidence(
                kind=EXPERIMENT_INTRAADAY,
                coverage_verified=False,
                freshness_verified=False,
                pit_path_verified=False,
                zero_fill_mixing=False,
                notes="分钟链 2026-04 起断供、7/30 后无写入；coverage/freshness 均未通过",
            ),
        ),
    }


def assert_never_uses_ticket_count(payload: Mapping[str, object]) -> None:
    """结构守卫：增量结论不得建立在"出票数量"上。"""
    note = str(payload.get("note", ""))
    if "same_day_paired" not in str(payload.get("spec", {}).get("success_criterion", "")):
        raise AssertionError("增量实验的成功判据必须是 same_day_paired_excess")
    if payload.get("status") == STATUS_OK and payload.get("gate_pass") is not True:
        raise AssertionError("样本门未过时不得标 ok")
    del note


__all__ = [
    "DEFAULT_MIN_AFFECTED_DATES",
    "DEFAULT_MIN_AFFECTED_SYMBOL_DATES",
    "DEFAULT_MIN_TRADING_DATES",
    "EXPERIMENT_INTRAADAY",
    "EXPERIMENT_NEWS",
    "EXPERIMENT_THEME",
    "ExperimentGate",
    "IncrementalExperimentSpec",
    "MODE_SHADOW",
    "NOT_AVAILABLE",
    "ReadinessEvidence",
    "STATUS_BLOCKED",
    "STATUS_INSUFFICIENT_SAMPLE",
    "STATUS_OK",
    "assert_never_uses_ticket_count",
    "default_readiness",
    "intraday_experiment_spec",
    "news_experiment_spec",
    "readiness_block",
    "run_incremental_experiment",
    "theme_experiment_spec",
]
