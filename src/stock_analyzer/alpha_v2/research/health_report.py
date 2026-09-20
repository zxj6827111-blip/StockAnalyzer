"""Alpha V2 每日健康报告（S21 / 原 P2-02，蓝图 §11 的八块）。

固定八块（缺数据就写 ``not_available``，**不省略、不美化**）：

```text
1. Identity         代码/配置/模型/特征 schema/标签策略/selection contract
2. Data Health      数据新鲜度、覆盖率、board 覆盖、breadth artifact 状态
3. Funnel           Eligible → Quality → Light → Deep → V2 TopK → Legacy Final
4. Winner Recall    3/5/10D outcome 成熟后的滚动召回（未成熟如实标）
5. Score Distribution  rank / 预期收益 / 方向 / 风险 / legacy 分数的分布
6. Alpha Quality    20D/60D Rank IC、Top3/Top5 超额、分位单调性
7. Execution         可成交率、no-fill 原因分布、入场延迟
8. Drift / Governance 预测漂移、模型年龄、schema/标签一致性
```

两条纪律：

- **Review Trigger 只触发人工复核**：判据是
  ``20D IC < 0 AND 60D IC <= 0 AND 60D Top5 excess <= 0``；触发后**只**写
  ``action = human_review_only``，绝不自动降阈值/自动 retrain/自动上线；
- **文案语义检查（DF-S09-002）**：报告与展示文案里，rank score / 未校准方向分
  不得被写成"上涨概率"。本模块用 S09 的语义守卫做机器检查，把"说错话"
  也变成可测的缺陷。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_HORIZONS,
    DEFAULT_TOP_KS,
    NOT_AVAILABLE,
    PRIMARY_HORIZON,
    EvaluationSpec,
    daily_rank_ic,
    downside_metrics,
    evaluate_scores,
    metric_column,
    research_gate_status,
    usable_mask,
)
from stock_analyzer.alpha_v2.research.winner_recall import (
    RecallSpec,
    compute_winner_recall,
)

REPORT_SCHEMA = "alpha_v2_daily_health_report.v1"

BLOCK_NAMES: tuple[str, ...] = (
    "identity",
    "data_health",
    "funnel",
    "winner_recall",
    "score_distribution",
    "alpha_quality",
    "execution",
    "drift_governance",
)

REVIEW_ACTION = "human_review_only"
FORBIDDEN_AUTO_ACTIONS = (
    "auto_lower_threshold",
    "auto_retrain_and_promote",
    "auto_disable_gate",
    "auto_adjust_weights",
)

# DF-S09-002：禁止把非概率输出写成概率的展示词
FORBIDDEN_PROBABILITY_TERMS = (
    "上涨概率",
    "上涨的可能性",
    "probability of rise",
    "胜率",
    "命中概率",
)
RANK_LIKE_COLUMNS = ("alpha_rank_score", "baseline_score", "lgbm_score", "xgb_score")


@dataclass(frozen=True, slots=True)
class HealthReportSpec:
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    primary_horizon: int = PRIMARY_HORIZON
    top_ks: tuple[int, ...] = DEFAULT_TOP_KS
    rolling_windows: tuple[int, ...] = (20, 60)
    review_ic_20d_below: float = 0.0
    review_ic_60d_at_or_below: float = 0.0
    review_top5_excess_60d_at_or_below: float = 0.0

    def to_payload(self) -> dict[str, object]:
        return {
            "horizons": [int(h) for h in self.horizons],
            "primary_horizon": int(self.primary_horizon),
            "top_ks": [int(k) for k in self.top_ks],
            "rolling_windows": [int(w) for w in self.rolling_windows],
            "review_trigger_rule": (
                f"ic_20d < {self.review_ic_20d_below} AND ic_60d <= "
                f"{self.review_ic_60d_at_or_below} AND top5_excess_60d <= "
                f"{self.review_top5_excess_60d_at_or_below}"
            ),
            "review_action": REVIEW_ACTION,
            "blocks": list(BLOCK_NAMES),
        }


@dataclass
class HealthReport:
    payload: dict[str, object]
    spec: HealthReportSpec = field(default_factory=HealthReportSpec)

    def to_payload(self) -> dict[str, object]:
        return dict(self.payload)


def build_health_report(
    *,
    frame: pd.DataFrame | None = None,
    identity: Mapping[str, object] | None = None,
    data_health: Mapping[str, object] | None = None,
    funnel: Mapping[str, object] | None = None,
    execution: Mapping[str, object] | None = None,
    drift: Mapping[str, object] | None = None,
    spec: HealthReportSpec | None = None,
    report_date: object | None = None,
) -> HealthReport:
    """组装八块报告（每块独立计算，任一块缺输入即 ``not_available``）。"""
    resolved = spec or HealthReportSpec()
    payload: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "report_date": (
            str(report_date)
            if report_date is not None
            else datetime.now().astimezone().date().isoformat()
        ),
        "spec": resolved.to_payload(),
        "identity": dict(identity or {}) or {"status": NOT_AVAILABLE},
        "data_health": dict(data_health or {}) or {"status": NOT_AVAILABLE},
        "funnel": dict(funnel or {}) or {"status": NOT_AVAILABLE},
        "winner_recall": _winner_recall_block(frame, resolved),
        "score_distribution": _score_distribution_block(frame),
        "alpha_quality": _alpha_quality_block(frame, resolved),
        "execution": dict(execution or {}) or _execution_block(frame),
        "drift_governance": dict(drift or {}) or _drift_block(frame),
    }
    payload["review_trigger"] = review_trigger(payload, spec=resolved)
    payload["block_status"] = {name: _block_status(payload[name]) for name in BLOCK_NAMES}
    return HealthReport(payload=payload, spec=resolved)


def _block_status(block: object) -> str:
    if not isinstance(block, Mapping):
        return NOT_AVAILABLE
    status = block.get("status")
    if status is None:
        return "ok"
    return str(status)


# ---------------------------------------------------------------------------
# 4. Winner Recall
# ---------------------------------------------------------------------------


def _winner_recall_block(frame: pd.DataFrame | None, spec: HealthReportSpec) -> dict[str, object]:
    if frame is None or frame.empty:
        return {"status": NOT_AVAILABLE, "reason": "no_frame"}
    metric = metric_column("excess_return", spec.primary_horizon)
    if metric not in frame.columns:
        return {"status": NOT_AVAILABLE, "reason": f"missing {metric}"}
    report = compute_winner_recall(
        frame,
        spec=RecallSpec(
            metric=metric,
            rolling_windows=spec.rolling_windows,
            min_pool_size=10,
        ),
    )
    payload = dict(report.summary)
    payload["daily_rows"] = int(len(report.daily))
    return payload


# ---------------------------------------------------------------------------
# 5. Score Distribution
# ---------------------------------------------------------------------------


def _score_distribution_block(frame: pd.DataFrame | None) -> dict[str, object]:
    if frame is None or frame.empty:
        return {"status": NOT_AVAILABLE, "reason": "no_frame"}
    columns = [
        "alpha_rank_score",
        "expected_excess_return_5d",
        "expected_net_return_5d",
        "p_up_net_5d",
        "expected_mae_5d",
        "baseline_score",
        "legacy_score",
    ]
    payload: dict[str, object] = {}
    for column in columns:
        if column not in frame.columns:
            payload[column] = NOT_AVAILABLE
            continue
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if values.empty:
            payload[column] = NOT_AVAILABLE
            continue
        payload[column] = {
            "count": int(values.shape[0]),
            "mean": float(values.mean()),
            "p10": float(values.quantile(0.10)),
            "median": float(values.median()),
            "p90": float(values.quantile(0.90)),
        }
    return {"status": "ok", "columns": payload}


# ---------------------------------------------------------------------------
# 6. Alpha Quality
# ---------------------------------------------------------------------------


def _alpha_quality_block(frame: pd.DataFrame | None, spec: HealthReportSpec) -> dict[str, object]:
    if frame is None or frame.empty or "alpha_rank_score" not in frame.columns:
        return {"status": NOT_AVAILABLE, "reason": "missing alpha_rank_score"}
    evaluation = evaluate_scores(
        frame,
        EvaluationSpec(
            score_column="alpha_rank_score",
            horizons=spec.horizons,
            primary_horizon=spec.primary_horizon,
            top_ks=spec.top_ks,
            rolling_windows=spec.rolling_windows,
        ),
    )
    payload = dict(evaluation)
    payload["status"] = "ok"
    return payload


# ---------------------------------------------------------------------------
# 7. Execution
# ---------------------------------------------------------------------------


def _execution_block(frame: pd.DataFrame | None) -> dict[str, object]:
    if frame is None or frame.empty or "executable" not in frame.columns:
        return {"status": NOT_AVAILABLE, "reason": "missing executable column"}
    executable = frame["executable"].fillna(False).astype(bool)
    reasons: dict[str, int] = {}
    if "no_fill_reason" in frame.columns:
        counts = frame.loc[~executable, "no_fill_reason"].fillna("<unknown>").value_counts()
        reasons = {str(key): int(value) for key, value in counts.items()}
    delays: dict[str, object] = NOT_AVAILABLE
    if "entry_delay_sessions" in frame.columns:
        delay = pd.to_numeric(frame["entry_delay_sessions"], errors="coerce").dropna()
        if not delay.empty:
            delays = {"mean": float(delay.mean()), "max": float(delay.max())}
    return {
        "status": "ok",
        "rows": int(len(frame)),
        "fill_rate": float(executable.mean()),
        "no_fill_count": int((~executable).sum()),
        "no_fill_reasons": reasons,
        "entry_delay_sessions": delays,
    }


# ---------------------------------------------------------------------------
# 8. Drift / Governance
# ---------------------------------------------------------------------------


def _drift_block(frame: pd.DataFrame | None) -> dict[str, object]:
    if frame is None or frame.empty:
        return {"status": NOT_AVAILABLE, "reason": "no_frame"}
    payload: dict[str, object] = {"status": "ok"}
    if "alpha_rank_score" in frame.columns and "decision_date" in frame.columns:
        daily = (
            pd.to_numeric(frame["alpha_rank_score"], errors="coerce")
            .groupby(frame["decision_date"].astype(str))
            .mean()
        )
        if daily.shape[0] >= 2:
            payload["prediction_drift"] = {
                "first_day": float(daily.iloc[0]),
                "last_day": float(daily.iloc[-1]),
                "delta": float(daily.iloc[-1] - daily.iloc[0]),
                "days": int(daily.shape[0]),
            }
    # 特征漂移用"可用样本占比"的日间变化近似（无需额外口径）
    metric = metric_column("excess_return", PRIMARY_HORIZON)
    if metric in frame.columns:
        mask = usable_mask(frame, metric=metric)
        share = mask.groupby(frame["decision_date"].astype(str)).mean()
        if share.shape[0] >= 2:
            payload["usable_sample_share"] = {
                "first_day": float(share.iloc[0]),
                "last_day": float(share.iloc[-1]),
                "delta": float(share.iloc[-1] - share.iloc[0]),
            }
    return payload


# ---------------------------------------------------------------------------
# Review Trigger（只触发人工复核）
# ---------------------------------------------------------------------------


def review_trigger(
    payload: Mapping[str, object], *, spec: HealthReportSpec | None = None
) -> dict[str, object]:
    """按蓝图 §11.8 的规则判定是否触发人工复核（**不自动改任何参数**）。"""
    resolved = spec or HealthReportSpec()
    quality = payload.get("alpha_quality")
    trigger: dict[str, object] = {
        "rule": resolved.to_payload()["review_trigger_rule"],
        "action": REVIEW_ACTION,
        "triggered": False,
        "reasons": [],
        "forbidden_actions": list(FORBIDDEN_AUTO_ACTIONS),
    }
    if not isinstance(quality, Mapping) or quality.get("status") != "ok":
        trigger["status"] = NOT_AVAILABLE
        trigger["reasons"] = ["alpha_quality_not_available"]
        return trigger
    rank_ic = quality.get("rank_ic")
    if not isinstance(rank_ic, Mapping):
        trigger["status"] = NOT_AVAILABLE
        return trigger
    ic_20d = _rolling_ic(rank_ic, resolved.primary_horizon, 20)
    ic_60d = _rolling_ic(rank_ic, resolved.primary_horizon, 60)
    top5 = _top5_excess_60d(quality, resolved)
    mature = int(quality.get("mature_dates", 0) or 0)
    trigger["inputs"] = {"ic_20d": ic_20d, "ic_60d": ic_60d, "top5_excess_60d": top5}
    trigger["mature_dates"] = mature
    trigger["research_gate"] = research_gate_status(mature)
    if mature < 20:
        trigger["status"] = "insufficient_sample"
        trigger["reasons"] = ["mature_dates_below_20"]
        return trigger
    triggered = (
        ic_20d is not None
        and ic_60d is not None
        and top5 is not None
        and ic_20d < resolved.review_ic_20d_below
        and ic_60d <= resolved.review_ic_60d_at_or_below
        and top5 <= resolved.review_top5_excess_60d_at_or_below
    )
    trigger["status"] = "ok"
    trigger["triggered"] = bool(triggered)
    if triggered:
        trigger["reasons"] = [
            "ic_20d_below_zero",
            "ic_60d_at_or_below_zero",
            "top5_excess_60d_at_or_below_zero",
        ]
    return trigger


def _rolling_ic(rank_ic: Mapping[str, object], horizon: int, window: int) -> float | None:
    block = rank_ic.get(f"{int(horizon)}d")
    if not isinstance(block, Mapping):
        return None
    value = block.get(f"ic_{int(window)}d")
    if isinstance(value, (int, float)) and pd.notna(value):
        return float(value)
    return None


def _top5_excess_60d(quality: Mapping[str, object], spec: HealthReportSpec) -> float | None:
    rolling = quality.get("topk_rolling")
    if isinstance(rolling, Mapping):
        block = rolling.get("top5")
        if isinstance(block, Mapping):
            value = block.get("excess_return_60d")
            if isinstance(value, (int, float)) and pd.notna(value):
                return float(value)
    topk = quality.get("topk")
    if isinstance(topk, Mapping):
        block = topk.get("top5")
        if isinstance(block, Mapping):
            value = block.get(metric_column("excess_return", spec.primary_horizon))
            if isinstance(value, (int, float)) and pd.notna(value):
                return float(value)
    return None


def assert_no_auto_action(payload: Mapping[str, object]) -> None:
    """结构守卫：报告里不得出现任何"自动改参数"的动作。"""
    trigger = payload.get("review_trigger")
    if not isinstance(trigger, Mapping):
        return
    action = str(trigger.get("action", ""))
    if action != REVIEW_ACTION:
        raise AssertionError(f"Review Trigger 的动作必须是 {REVIEW_ACTION}，收到 {action!r}")
    for forbidden in FORBIDDEN_AUTO_ACTIONS:
        if forbidden in action:
            raise AssertionError(f"不允许的自动动作: {forbidden}")


# ---------------------------------------------------------------------------
# 文案语义检查（DF-S09-002）
# ---------------------------------------------------------------------------


def audit_display_text(
    text: str,
    *,
    column: str = "",
    calibrated: bool = False,
    semantics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """检查一段展示文案是否把非概率输出写成了概率。

    ``calibrated=False`` 时，文案里出现"上涨概率/胜率"等词即判违规——
    与 S09 的语义守卫同一条底线，只是作用在报告/UI 文案层。
    """
    resolved = dict(semantics or {})
    may_call_probability = bool(resolved.get("may_call_probability", False)) or bool(calibrated)
    hits = [term for term in FORBIDDEN_PROBABILITY_TERMS if term in str(text)]
    rank_like = str(column) in RANK_LIKE_COLUMNS
    violations = [] if may_call_probability else hits
    return {
        "text": str(text),
        "column": str(column),
        "may_call_probability": may_call_probability,
        "probability_terms_found": hits,
        "violations": violations,
        "verdict": "ok" if not violations else "mislabeled_as_probability",
        "rank_like_column": rank_like,
    }


def audit_report_text_blocks(payload: Mapping[str, object]) -> dict[str, object]:
    """对报告里所有面向人的文案块跑一遍语义检查。"""
    checks: list[dict[str, object]] = []
    for key, value in payload.items():
        if isinstance(value, str) and value:
            checks.append(audit_display_text(value, column=str(key)))
    violations = [item for item in checks if item["verdict"] != "ok"]
    return {
        "checked": len(checks),
        "violations": violations,
        "verdict": "ok" if not violations else "mislabeled_as_probability",
    }


# ---------------------------------------------------------------------------
# 渲染与落盘
# ---------------------------------------------------------------------------


def render_markdown(payload: Mapping[str, object]) -> str:
    """把八块渲染成可读 Markdown（供日报/飞书草稿复用同一份数据）。"""
    lines: list[str] = []
    lines.append(f"# Alpha V2 健康报告 {payload.get('report_date', '')}")
    lines.append("")
    lines.append("## 1. Identity")
    lines.append(_render_identity(payload.get("identity")))
    lines.append("")
    lines.append("## 2. Data Health")
    lines.append(_render_key_values(payload.get("data_health")))
    lines.append("")
    lines.append("## 3. Funnel")
    lines.append(_render_key_values(payload.get("funnel")))
    lines.append("")
    lines.append("## 4. Winner Recall")
    lines.append(_render_key_values(payload.get("winner_recall")))
    lines.append("")
    lines.append("## 5. Score Distribution")
    lines.append(_render_key_values(payload.get("score_distribution")))
    lines.append("")
    lines.append("## 6. Alpha Quality")
    lines.append(_render_alpha_quality(payload.get("alpha_quality")))
    lines.append("")
    lines.append("## 7. Execution")
    lines.append(_render_key_values(payload.get("execution")))
    lines.append("")
    lines.append("## 8. Drift / Governance")
    lines.append(_render_key_values(payload.get("drift_governance")))
    lines.append("")
    trigger = payload.get("review_trigger")
    if isinstance(trigger, Mapping):
        lines.append("## Review Trigger")
        lines.append(
            f"- triggered: {trigger.get('triggered')} "
            f"(action: {trigger.get('action')}, status: {trigger.get('status')})"
        )
    lines.append("")
    return "\n".join(lines)


def _render_identity(block: object) -> str:
    if not isinstance(block, Mapping) or not block:
        return "- not_available"
    fields = (
        "code_commit",
        "config_hash",
        "model_id",
        "artifact_content_hash",
        "feature_schema_id",
        "label_policy_id",
        "selection_contract_id",
    )
    lines = [f"- {field}: {block.get(field, NOT_AVAILABLE)}" for field in fields]
    return "\n".join(lines)


def _render_key_values(block: object) -> str:
    if not isinstance(block, Mapping) or not block:
        return "- not_available"
    lines: list[str] = []
    for key, value in block.items():
        if isinstance(value, Mapping):
            lines.append(f"- {key}:")
            for inner_key, inner_value in value.items():
                lines.append(f"  - {inner_key}: {inner_value}")
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _render_alpha_quality(block: object) -> str:
    if not isinstance(block, Mapping) or block.get("status") != "ok":
        return "- not_available"
    rank_ic = block.get("rank_ic")
    lines: list[str] = []
    if isinstance(rank_ic, Mapping):
        for horizon, value in rank_ic.items():
            if isinstance(value, Mapping):
                lines.append(
                    f"- Rank IC {horizon}: mean={value.get('mean_ic')} "
                    f"20D={value.get('ic_20d')} 60D={value.get('ic_60d')} "
                    f"CI={value.get('ci95')}"
                )
    topk = block.get("topk")
    if isinstance(topk, Mapping):
        for name, value in topk.items():
            lines.append(f"- {name}: {value}")
    return "\n".join(lines) if lines else "- not_available"


def write_health_report(
    *, root: str | Path, payload: Mapping[str, object], report_date: object | None = None
) -> Path:
    assert_no_auto_action(payload)
    token = _date_token(report_date if report_date is not None else payload.get("report_date"))
    return write_json_atomic(Path(root) / f"health_report_{token}.json", dict(payload))


def write_health_report_markdown(
    *, root: str | Path, payload: Mapping[str, object], report_date: object | None = None
) -> Path:
    token = _date_token(report_date if report_date is not None else payload.get("report_date"))
    path = Path(root) / f"health_report_{token}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(payload), encoding="utf-8")
    return path


def _date_token(value: object) -> str:
    text = str(value or "").strip()[:10]
    if len(text) == 10 and text[4] == "-":
        return text.replace("-", "")
    return datetime.now().astimezone().strftime("%Y%m%d")


def health_report_markdown_blocks(payload: Mapping[str, object]) -> Sequence[str]:
    return tuple(str(payload.get(name, NOT_AVAILABLE)) for name in BLOCK_NAMES)


def alpha_quality_daily_ic(frame: pd.DataFrame, *, horizon: int = PRIMARY_HORIZON) -> pd.DataFrame:
    """供报告层单独取日频 IC（与 Alpha Quality 块同口径）。"""
    return daily_rank_ic(
        frame,
        score_column="alpha_rank_score",
        metric_column_=metric_column("excess_return", horizon),
    )


def alpha_quality_downside(
    frame: pd.DataFrame, *, horizon: int = PRIMARY_HORIZON
) -> dict[str, object]:
    return downside_metrics(frame, horizon=horizon, score_column="alpha_rank_score")


__all__ = [
    "BLOCK_NAMES",
    "FORBIDDEN_AUTO_ACTIONS",
    "FORBIDDEN_PROBABILITY_TERMS",
    "HealthReport",
    "HealthReportSpec",
    "REPORT_SCHEMA",
    "REVIEW_ACTION",
    "alpha_quality_daily_ic",
    "alpha_quality_downside",
    "assert_no_auto_action",
    "audit_display_text",
    "audit_report_text_blocks",
    "build_health_report",
    "health_report_markdown_blocks",
    "render_markdown",
    "review_trigger",
    "write_health_report",
    "write_health_report_markdown",
]
