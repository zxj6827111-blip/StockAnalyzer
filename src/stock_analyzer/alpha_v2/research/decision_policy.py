"""Alpha V2 Final Decision Policy V2 —— **Shadow 研究候选**（S18 / 原方案正文 P1-08）。

**它不是"新的 70 分"。** Shadow 阶段每天把研究候选**全部存下来**，用于将来
统计 Alpha；而不是先定一个阈值再决定谁入选。具体规则：

```text
Candidate = Deep50（当日深池）
排序      = alpha_rank（Head A，5D 可执行超额收益横截面 rank）
同时记录  = 预期收益 / 预期超额 / 方向 P(up) / 风险 / 可成交性 / 数据健康 / 市场状态
输出      = v2_top1 / v2_top3 / v2_top5（**允许为空**，绝不强制凑满）
```

四条禁止（Gate S18 Blocking）：

1. 不造"V2 70 分"：本模块没有任何分数阈值参数，也不读 Legacy 阈值；
2. 不强制每天选满：候选不足时 top3/top5 少于 K 是合法结果；
3. 不接管 Legacy：输出只落 ``artifacts/alpha_v2``，不接触正式通知/下单；
4. 不编造值：某个 Head 没有合法模型时，对应字段写 ``not_available``。

额外记录两层"如果启用会怎样"：可成交性（T+1 能否真实买入）与风险/数据健康/市场
状态。它们在本阶段只做**标注**（``enforced=False``），不改变候选列表——这样既保留
"未来正式门"的研究输入，又不会在证据不足时提前启用硬门。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.research.metrics import (
    NOT_AVAILABLE,
    PRIMARY_HORIZON,
    metric_column,
)

POLICY_SCHEMA = "alpha_v2_final_policy_shadow.v1"
POLICY_ID = "night_alpha_v2_shadow_v1"

CANDIDATE_STAGE_DEEP = "deep_pool"
DEFAULT_TOP_KS: tuple[int, ...] = (1, 3, 5)

RANK_COLUMN = "alpha_rank_score"

# 候选行上要原样带出的决策信息（缺列 → not_available，不猜）
DECISION_FIELDS: tuple[str, ...] = (
    "alpha_rank_score",
    "expected_excess_return_5d",
    "expected_net_return_5d",
    "p_up_net_5d",
    "p_up_excess_5d",
    "expected_mae_5d",
    "p_mae_le_5pct_5d",
    "executable",
    "no_fill_reason",
    "entry_delay_sessions",
    "risk_level",
    "data_health",
    "market_regime",
)


@dataclass(frozen=True, slots=True)
class FinalPolicySpec:
    """Shadow 决策策略的口径（**没有**分数阈值参数）。"""

    policy_id: str = POLICY_ID
    candidate_stage: str = CANDIDATE_STAGE_DEEP
    rank_column: str = RANK_COLUMN
    top_ks: tuple[int, ...] = DEFAULT_TOP_KS
    horizon: int = PRIMARY_HORIZON
    require_fillable: bool = True
    require_stage_column: bool = True
    allow_zero_signal: bool = True
    enforced: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "schema": POLICY_SCHEMA,
            "candidate_stage": self.candidate_stage,
            "rank_column": self.rank_column,
            "top_ks": [int(k) for k in self.top_ks],
            "horizon": int(self.horizon),
            "require_fillable": bool(self.require_fillable),
            "require_stage_column": bool(self.require_stage_column),
            "allow_zero_signal": True,
            "enforced": False,
            "score_thresholds": [],
            "note": (
                "Shadow 策略：按 alpha_rank 排序取前 K（可成交优先），"
                "不设分数阈值、不强制填满、不接管 Legacy 正式结果"
            ),
        }


@dataclass
class ShadowPolicyResult:
    ranked: pd.DataFrame
    selections: dict[str, pd.DataFrame]
    payload: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return dict(self.payload)


def build_shadow_selection(
    frame: pd.DataFrame,
    *,
    spec: FinalPolicySpec | None = None,
    decision_date: object | None = None,
) -> ShadowPolicyResult:
    """在候选帧上做一次 Shadow 决策（按 alpha_rank 排序 + 可成交优先取前 K）。"""
    resolved = spec or FinalPolicySpec()
    if frame.empty:
        return _empty_result(resolved, decision_date)
    day = str(decision_date) if decision_date is not None else None
    scoped = frame
    if day is not None and "decision_date" in frame.columns:
        scoped = frame[frame["decision_date"].astype(str) == day]
    stage_column = f"{resolved.candidate_stage}"
    if stage_column in scoped.columns:
        scoped = scoped[scoped[stage_column].fillna(False).astype(bool)]
    elif resolved.require_stage_column:
        # 没有候选阶段标记时**不得**把所有行当 Deep50 候选：那是"扩大候选池"
        # 这类最典型的无声越界。宁可报 zero signal，也不猜。
        payload = _empty_result(resolved, day).to_payload()
        payload["status"] = "candidate_stage_column_missing"
        payload["missing_stage_column"] = stage_column
        return ShadowPolicyResult(ranked=pd.DataFrame(), selections={}, payload=payload)
    if scoped.empty:
        return _empty_result(resolved, day)

    ranked = scoped.copy()
    rank_values = pd.to_numeric(ranked.get(resolved.rank_column), errors="coerce")
    ranked["__rank"] = rank_values.rank(ascending=False, method="first")
    # 缺 rank 的候选排在最后但仍留在表里（不静默丢弃，便于审计覆盖率）
    ranked = ranked.sort_values(["__rank"], na_position="last", kind="mergesort").reset_index(
        drop=True
    )

    if resolved.require_fillable and "executable" in ranked.columns:
        ranked["__fillable"] = ranked["executable"].fillna(False).astype(bool)
    else:
        ranked["__fillable"] = True

    selections: dict[str, pd.DataFrame] = {}
    for k in resolved.top_ks:
        fillable = ranked[ranked["__fillable"]]
        selected = fillable.head(int(k)).copy()
        selections[f"v2_top{int(k)}"] = selected
    payload = _policy_payload(ranked, selections, resolved, decision_date=day)
    return ShadowPolicyResult(ranked=ranked, selections=selections, payload=payload)


def _empty_result(spec: FinalPolicySpec, decision_date: object | None) -> ShadowPolicyResult:
    empty = pd.DataFrame()
    payload = {
        "schema": POLICY_SCHEMA,
        "spec": spec.to_payload(),
        "decision_date": None if decision_date is None else str(decision_date),
        "candidate_count": 0,
        "fillable_count": 0,
        "status": "no_candidates",
        "selections": {f"v2_top{int(k)}": [] for k in spec.top_ks},
        "allow_zero_signal": True,
        "zero_signal_is_valid": True,
    }
    return ShadowPolicyResult(ranked=empty, selections={}, payload=payload)


def _policy_payload(
    ranked: pd.DataFrame,
    selections: Mapping[str, pd.DataFrame],
    spec: FinalPolicySpec,
    *,
    decision_date: str | None,
) -> dict[str, object]:
    fillable_count = int(ranked["__fillable"].sum()) if "__fillable" in ranked.columns else 0
    payload: dict[str, object] = {
        "schema": POLICY_SCHEMA,
        "spec": spec.to_payload(),
        "decision_date": decision_date,
        "candidate_count": int(len(ranked)),
        "fillable_count": fillable_count,
        "unfillable_count": int(len(ranked)) - fillable_count,
        "status": "ok" if len(ranked) else "no_candidates",
        "ranked": [_candidate_payload(row) for _, row in ranked.iterrows()],
        "selections": {
            name: [_candidate_payload(row) for _, row in selected.iterrows()]
            for name, selected in selections.items()
        },
        "selection_sizes": {name: int(len(selected)) for name, selected in selections.items()},
        "allow_zero_signal": True,
        "zero_signal_is_valid": True,
        "gates": _gate_annotations(ranked, selections),
    }
    return payload


def _candidate_payload(row: pd.Series) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": str(row.get("symbol", "")),
        "decision_date": (
            str(row.get("decision_date")) if row.get("decision_date") is not None else None
        ),
        "rank": (
            int(row["__rank"])
            if "__rank" in row.index and pd.notna(row.get("__rank"))
            else NOT_AVAILABLE
        ),
        "fillable": bool(row.get("__fillable", True)),
    }
    for field_name in DECISION_FIELDS:
        if field_name not in row.index:
            payload[field_name] = NOT_AVAILABLE
            continue
        value = row.get(field_name)
        payload[field_name] = _json_safe(value)
    return payload


def _json_safe(value: object) -> object:
    if value is None:
        return NOT_AVAILABLE
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        parsed = float(value)
        return parsed if np.isfinite(parsed) else NOT_AVAILABLE
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float):
        return value if np.isfinite(value) else NOT_AVAILABLE
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (int, bool, str)):
        return value
    return str(value)


def _gate_annotations(
    ranked: pd.DataFrame, selections: Mapping[str, pd.DataFrame]
) -> dict[str, object]:
    """把"未来正式门"的研究输入记录下来，但在 Shadow 阶段**不启用**。"""
    annotations: dict[str, object] = {"enforced": False}
    for name, column in (
        ("data_health", "data_health"),
        ("market_regime", "market_regime"),
        ("risk", "risk_level"),
    ):
        if column not in ranked.columns:
            annotations[name] = {"status": NOT_AVAILABLE, "would_block": []}
            continue
        values = ranked[column].fillna(NOT_AVAILABLE).astype(str)
        blocked = ranked[values.isin({"broken", "weak", "reject", "high"})]
        annotations[name] = {
            "status": NOT_AVAILABLE,
            "values": dict(values.value_counts()),
            "would_block": [str(symbol) for symbol in blocked.get("symbol", [])],
        }
    top1 = selections.get("v2_top1")
    annotations["would_be_empty"] = bool(top1 is None or top1.empty)
    return annotations


def write_shadow_policy_report(
    *,
    root: str | Path,
    payload: Mapping[str, object],
    decision_date: object | None = None,
) -> Path:
    """落盘 ``reports/shadow_policy_YYYYMMDD.json``（也可写 ``latest.json``）。"""
    base = Path(root)
    resolved_date = _date_token(
        payload.get("decision_date") if decision_date is None else decision_date
    )
    target = base / f"shadow_policy_{resolved_date}.json"
    return write_json_atomic(target, dict(payload))


def _date_token(value: object) -> str:
    text = str(value or "").strip()[:10]
    if len(text) == 10 and text[4] == "-":
        return text.replace("-", "")
    return datetime.now().astimezone().strftime("%Y%m%d")


def shadow_policy_json(result: ShadowPolicyResult) -> str:
    return json.dumps(result.to_payload(), ensure_ascii=False, sort_keys=True, default=str)


# 分数阈值参数的**语义片段**（不列具体业务字段名，避免守卫自身成为字符串来源）。
THRESHOLD_FIELD_FRAGMENTS: tuple[str, ...] = (
    "threshold",
    "min_score",
    "score_floor",
    "floor_score",
    "min_probability",
    "p_up_min",
)


def assert_no_threshold_knobs() -> None:
    """结构守卫：Shadow 策略不得含任何分数阈值参数。"""
    names = set(FinalPolicySpec.__dataclass_fields__)
    for name in names:
        if any(fragment in name for fragment in THRESHOLD_FIELD_FRAGMENTS):
            raise AssertionError(f"Shadow 策略不允许出现阈值参数: {name}")


def policy_horizon_columns(horizon: int, *, short_horizons: Sequence[int] = (3, 5)) -> list[str]:
    """对外输出字段清单（含 3D 确认位；未实现的 Horizon 由调用方标 not_available）。"""
    columns = [
        f"expected_excess_return_{int(horizon)}d",
        f"expected_net_return_{int(horizon)}d",
        f"p_up_net_{int(horizon)}d",
        f"p_up_excess_{int(horizon)}d",
        metric_column("mae", horizon),
    ]
    for short in short_horizons:
        columns.append(f"p_up_net_{int(short)}d")
    return columns


__all__ = [
    "CANDIDATE_STAGE_DEEP",
    "DECISION_FIELDS",
    "DEFAULT_TOP_KS",
    "FinalPolicySpec",
    "POLICY_ID",
    "POLICY_SCHEMA",
    "RANK_COLUMN",
    "THRESHOLD_FIELD_FRAGMENTS",
    "ShadowPolicyResult",
    "assert_no_threshold_knobs",
    "build_shadow_selection",
    "policy_horizon_columns",
    "shadow_policy_json",
    "write_shadow_policy_report",
]
