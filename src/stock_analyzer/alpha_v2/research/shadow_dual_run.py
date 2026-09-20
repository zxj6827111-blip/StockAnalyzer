"""Alpha V2 Legacy vs V2 Shadow 双轨（S20 / 原 P2-01）。

```text
Legacy ──► 正式结果 / 正式通知（一字不改）
V2     ──► artifacts/alpha_v2 + shadow 报告（只读影子）
```

严格分离的三条保证：

1. **V2 不写正式结果**：本模块只产出 ``reports/dual_run_*.json`` 与
   ``decisions/`` / ``outcomes/`` 台账，不触碰通知链路与交易动作；
2. **回滚是一个开关**：``alpha_v2.enforce_final_selection``。本阶段它必须恒为
   ``false``（:func:`shadow_flags` 会读出来并在报告里写明），回滚不需要删任何
   Legacy 代码；
3. **可判定对照**：每天并排给出 ``legacy_final`` 与 ``v2_top1/3/5``，并把
   V2 侧的 ``alpha_rank / expected_return / direction / risk / data_health /
   market_regime`` 一并落盘——否则"双轨"只是一句口号。

同时闭合 M1 的两个非阻塞遗留项（DF-S10-001 / DF-S10-002）：

- :func:`build_shadow_decision_rows` 用 S16 的真实 Head 输出填 ``v2_*`` 字段
  （未实现时仍写 ``not_available``，不编造）；
- :func:`mature_shadow_outcomes` 把 M1 的 outcome 成熟函数接到本模块的台账上。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.decision_log import (
    DecisionRow,
    build_decision_rows,
    compute_outcomes,
    outcome_path,
    write_decision_rows,
    write_outcomes,
)
from stock_analyzer.alpha_v2.research.decision_policy import (
    NOT_AVAILABLE,
    ShadowPolicyResult,
)

DUAL_RUN_SCHEMA = "alpha_v2_shadow_dual_run.v1"

LEGACY_FIELDS_SELECTED = "final_signals"
LEGACY_FIELDS_REJECTED = "rejected"

# 报告里必须自述"没有碰 Legacy"的字段（结构守卫读它们）
UNTOUCHED_DECLARATIONS: dict[str, object] = {
    "legacy_modified": False,
    "legacy_notification_touched": False,
    "legacy_threshold_changed": False,
    "legacy_action_changed": False,
    "serving_model_changed": False,
}


@dataclass
class DualRunResult:
    payload: dict[str, object]
    legacy: dict[str, object]
    v2: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return dict(self.payload)


# ---------------------------------------------------------------------------
# Legacy 侧提取（只读）
# ---------------------------------------------------------------------------


def extract_legacy_final(report: Mapping[str, object]) -> dict[str, object]:
    """从夜扫报告里**只读**取出正式结果与拒因（不改任何内容）。"""
    selection = _find_final_selection(report)
    if selection is None:
        return {
            "status": "final_selection_not_found",
            "selected": [],
            "rejected": [],
            "min_threshold": NOT_AVAILABLE,
            "final_signal_cap": NOT_AVAILABLE,
            "allow_zero_signal": NOT_AVAILABLE,
            "selected_count": 0,
        }
    selected = _mapping_list(selection.get(LEGACY_FIELDS_SELECTED))
    rejected = _mapping_list(selection.get(LEGACY_FIELDS_REJECTED))
    return {
        "status": "ok",
        "selected": [
            {
                "symbol": str(row.get("symbol", "")),
                "score": row.get("score", NOT_AVAILABLE),
                "action": str(row.get("action", "") or NOT_AVAILABLE),
                "reject_reasons": list(_string_list(row.get("reject_reasons"))),
            }
            for row in selected
        ],
        "rejected": [
            {
                "symbol": str(row.get("symbol", "")),
                "score": row.get("score", NOT_AVAILABLE),
                "reject_reasons": list(_string_list(row.get("reject_reasons"))),
            }
            for row in rejected
        ],
        "min_threshold": selection.get("min_threshold", NOT_AVAILABLE),
        "final_signal_cap": selection.get("final_signal_cap", NOT_AVAILABLE),
        "allow_zero_signal": selection.get("allow_zero_signal", NOT_AVAILABLE),
        "selected_count": int(selection.get("selected_count", len(selected)) or 0),
        "rejected_count": int(selection.get("rejected_count", len(rejected)) or 0),
    }


def _find_final_selection(report: Mapping[str, object]) -> Mapping[str, object] | None:
    direct = report.get("final_selection")
    if isinstance(direct, Mapping):
        return direct
    funnel = report.get("funnel")
    if isinstance(funnel, Mapping):
        nested = funnel.get("final_selection")
        if isinstance(nested, Mapping):
            return nested
    return None


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value]


def shadow_flags(config: Any) -> dict[str, object]:
    """读出 V2 开关并断言"未接管"（回滚只需把这一个开关设回 false）。"""
    alpha_v2 = getattr(config, "alpha_v2", None)
    enforce = bool(getattr(alpha_v2, "enforce_final_selection", False))
    payload: dict[str, object] = {
        "enabled": bool(getattr(alpha_v2, "enabled", False)),
        "shadow_only": bool(getattr(alpha_v2, "shadow_only", True)),
        "enforce_final_selection": enforce,
        "rollback_switch": "alpha_v2.enforce_final_selection",
        "rollback_note": "设回 false 即回到纯 Shadow；不需要删除任何 Legacy 代码",
    }
    if enforce:
        payload["violation"] = (
            "Shadow 阶段 enforce_final_selection 必须为 false：本轮未获授权接管正式选股"
        )
    return payload


# ---------------------------------------------------------------------------
# V2 侧（只读影子）
# ---------------------------------------------------------------------------


def _v2_candidates(policy: ShadowPolicyResult) -> dict[str, list[dict[str, object]]]:
    return {
        name: list(rows)
        for name, rows in policy.to_payload().get("selections", {}).items()
        if isinstance(rows, list)
    }


def build_dual_run(
    *,
    legacy_report: Mapping[str, object],
    policy: ShadowPolicyResult,
    config: Any = None,
    decision_date: object | None = None,
    data_health: Mapping[str, object] | None = None,
    market_regime: Mapping[str, object] | None = None,
) -> DualRunResult:
    """把 Legacy 正式结果与 V2 影子候选并排落成一份可判定对照。"""
    legacy = extract_legacy_final(legacy_report)
    policy_payload = policy.to_payload()
    v2 = {
        "candidates": policy_payload.get("ranked", []),
        "selections": _v2_candidates(policy),
        "selection_sizes": policy_payload.get("selection_sizes", {}),
        "status": policy_payload.get("status", NOT_AVAILABLE),
        "unfillable_count": policy_payload.get("unfillable_count", NOT_AVAILABLE),
        "gates": policy_payload.get("gates", {}),
    }
    legacy_symbols = [str(row["symbol"]) for row in legacy.get("selected", [])]
    v2_top1 = [str(row.get("symbol")) for row in v2["selections"].get("v2_top1", [])]
    v2_top3 = [str(row.get("symbol")) for row in v2["selections"].get("v2_top3", [])]
    v2_top5 = [str(row.get("symbol")) for row in v2["selections"].get("v2_top5", [])]
    rejected = {str(row.get("symbol")) for row in legacy.get("rejected", [])}

    payload: dict[str, object] = {
        "schema": DUAL_RUN_SCHEMA,
        "decision_date": (
            str(decision_date) if decision_date is not None else policy_payload.get("decision_date")
        ),
        "legacy": legacy,
        "v2": v2,
        "comparison": {
            "legacy_final": legacy_symbols,
            "v2_top1": v2_top1,
            "v2_top3": v2_top3,
            "v2_top5": v2_top5,
            "overlap_with_legacy": sorted(set(v2_top5) & set(legacy_symbols)),
            "v2_top5_previously_rejected_by_legacy": sorted(set(v2_top5) & rejected),
            "legacy_reject_reasons_summary": _reason_summary(legacy.get("rejected", [])),
            "v2_only": sorted(set(v2_top5) - set(legacy_symbols)),
            "legacy_only": sorted(set(legacy_symbols) - set(v2_top5)),
        },
        "flags": shadow_flags(config) if config is not None else {"status": "config_not_provided"},
        "data_health": dict(data_health or {}) or {"status": NOT_AVAILABLE},
        "market_regime": dict(market_regime or {}) or {"status": NOT_AVAILABLE},
        "generated_at": datetime.now().astimezone().isoformat(),
        **UNTOUCHED_DECLARATIONS,
    }
    return DualRunResult(payload=payload, legacy=legacy, v2=v2)


def _reason_summary(rejected: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rejected:
        for reason in _string_list(row.get("reject_reasons")):
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def assert_legacy_untouched(payload: Mapping[str, object]) -> None:
    """结构守卫：双轨报告必须自述"没有改动 Legacy 的任何东西"。"""
    for key, expected in UNTOUCHED_DECLARATIONS.items():
        actual = payload.get(key)
        if actual is not expected:
            raise AssertionError(f"双轨报告缺少未触碰声明或声明不实: {key}={actual!r}")
    flags = payload.get("flags")
    if isinstance(flags, Mapping) and flags.get("enforce_final_selection"):
        raise AssertionError("Shadow 阶段 enforce_final_selection 不允许为 true（会接管正式选股）")


def write_dual_run_report(
    *, root: str | Path, payload: Mapping[str, object], decision_date: object | None = None
) -> Path:
    assert_legacy_untouched(payload)
    token = _date_token(
        decision_date if decision_date is not None else payload.get("decision_date")
    )
    return write_json_atomic(Path(root) / f"dual_run_{token}.json", dict(payload))


def _date_token(value: object) -> str:
    text = str(value or "").strip()[:10]
    if len(text) == 10 and text[4] == "-":
        return text.replace("-", "")
    return datetime.now().astimezone().strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 台账接线（DF-S10-001 / DF-S10-002）
# ---------------------------------------------------------------------------


def build_shadow_decision_rows(
    *,
    signal_date: date,
    candidates: Sequence[Mapping[str, object]],
    model_identity: Mapping[str, object] | None = None,
    feature_schema: Mapping[str, object] | None = None,
    label_policy: Mapping[str, object] | None = None,
    data_snapshot: Mapping[str, object] | None = None,
    selection_contract: Mapping[str, object] | None = None,
) -> list[DecisionRow]:
    """把 S16 的 Head 输出填进决策台账（字段缺失仍写 ``not_available``）。

    ``candidates`` 的键与 S16 输出对齐：``alpha_rank_score`` / ``expected_excess_return_5d``
    / ``p_up_net_5d`` / ``expected_mae_5d``。
    """
    enriched: list[dict[str, object]] = []
    for row in candidates:
        enriched.append(
            {
                "symbol": str(row.get("symbol", "")),
                "eligible": row.get("eligible", NOT_AVAILABLE),
                "quality_rank": row.get("quality_rank", NOT_AVAILABLE),
                "light_rank": row.get("light_rank", NOT_AVAILABLE),
                "deep_rank": row.get("rank", row.get("deep_rank", NOT_AVAILABLE)),
                "legacy_score": row.get("legacy_score", NOT_AVAILABLE),
                "legacy_reject_reasons": row.get("legacy_reject_reasons", NOT_AVAILABLE),
                "v2_rank_score": row.get("alpha_rank_score", NOT_AVAILABLE),
                "v2_expected_return": row.get(
                    "expected_excess_return_5d", row.get("expected_net_return_5d", NOT_AVAILABLE)
                ),
                "v2_direction_score": row.get(
                    "p_up_net_5d_calibrated", row.get("p_up_net_5d", NOT_AVAILABLE)
                ),
                "v2_risk_score": row.get(
                    "expected_mae_5d", row.get("p_mae_le_5pct_5d", NOT_AVAILABLE)
                ),
            }
        )
    return build_decision_rows(
        signal_date=signal_date,
        candidates=enriched,
        model_identity=model_identity,
        feature_schema=feature_schema,
        label_policy=label_policy,
        data_snapshot=data_snapshot,
        selection_contract=selection_contract,
    )


def persist_shadow_decision_log(
    *,
    root: str | Path,
    signal_date: date,
    rows: Sequence[DecisionRow],
    manifest: Mapping[str, object] | None = None,
) -> dict[str, str]:
    from stock_analyzer.alpha_v2.decision_log import write_manifest

    written: dict[str, str] = {
        "decisions": str(write_decision_rows(root=root, signal_date=signal_date, rows=rows))
    }
    if manifest is not None:
        written["manifest"] = str(
            write_manifest(root=root, signal_date=signal_date, payload=manifest)
        )
    return written


def mature_shadow_outcomes(
    *,
    root: str | Path,
    signal_date: date,
    evaluation_date: date,
    decision_rows: Sequence[Mapping[str, object]],
    bars_by_symbol: Mapping[str, Any],
    horizons: Sequence[int] = (3, 5, 10, 15),
    trading_days: Sequence[date] | None = None,
    config: Any = None,
) -> dict[str, object]:
    """把 M1 的 outcome 成熟函数接到影子台账（信号当天**不写**任何未来数据）。"""
    maturity = compute_outcomes(
        decision_rows=decision_rows,
        bars_by_symbol=bars_by_symbol,
        signal_date=signal_date,
        evaluation_date=evaluation_date,
        horizons=horizons,
        trading_days=trading_days,
        config=config,
    )
    payload: dict[str, object] = {
        "matured_horizons": list(maturity.matured_horizons),
        "pending_horizons": list(maturity.pending_horizons),
        "rows": len(maturity.rows),
    }
    if maturity.any_matured:
        payload["written"] = str(write_outcomes(root=root, maturity=maturity))
    else:
        payload["written"] = None
        payload["reason"] = "no_matured_horizons_on_evaluation_date"
    payload["path"] = str(outcome_path(root, signal_date=signal_date))
    return payload


def iter_dual_run_days(payload: Mapping[str, object]) -> Iterable[tuple[str, str]]:
    """(阶段, 符号) 迭代器，便于对照脚本消费。"""
    for symbol in payload.get("comparison", {}).get("legacy_final", []):  # type: ignore[union-attr]
        yield "legacy", str(symbol)
    for symbol in payload.get("comparison", {}).get("v2_top5", []):  # type: ignore[union-attr]
        yield "v2", str(symbol)


def dual_run_dataframe(payload: Mapping[str, object]) -> pd.DataFrame:
    comparison = payload.get("comparison")
    if not isinstance(comparison, Mapping):
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for stage in ("legacy_final", "v2_top1", "v2_top3", "v2_top5"):
        symbols = comparison.get(stage) or []
        for rank, symbol in enumerate(symbols, start=1):
            rows.append({"stage": stage, "rank": rank, "symbol": str(symbol)})
    return pd.DataFrame(rows)


__all__ = [
    "DUAL_RUN_SCHEMA",
    "UNTOUCHED_DECLARATIONS",
    "DualRunResult",
    "assert_legacy_untouched",
    "build_dual_run",
    "build_shadow_decision_rows",
    "dual_run_dataframe",
    "extract_legacy_final",
    "iter_dual_run_days",
    "mature_shadow_outcomes",
    "persist_shadow_decision_log",
    "shadow_flags",
    "write_dual_run_report",
]
