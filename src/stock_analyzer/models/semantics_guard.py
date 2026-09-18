"""模型输出语义守卫（S09 / 原 P0-09）。

**目标**（阶段施工提示词 S09）：每个模型输出必须显式声明

```text
label_policy_id / label_name / horizon / price_basis / output_kind / calibration
```

``output_kind`` 至少覆盖：``probability`` / ``rank_score`` / ``expected_return`` /
``risk_score``。

**关键规则**：``rank_quantile`` 分类器的输出只能解释为"该标签定义下的正类概率/分值"，
**不得**显示成"未来上涨概率"——除非模型确实训练的是
``P(net_return_h > 0)`` 且具备 OOS 校准。

**当前 legacy 的处理**：只标 ``legacy_model_health = degraded_unverified``，
**不自动反转、不自动替换**（阶段提示词明确要求；蓝图 §2.6 的 AUC 0.331 也不是
"取 1-p 就行"的理由）。

本模块只做"语义声明与显示口径守卫"，不改任何模型输出值，也不参与打分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stock_analyzer.models.output_semantics import (
    OUTPUT_SEMANTICS_EVENT_PROBABILITY,
    OUTPUT_SEMANTICS_RANK_QUANTILE,
    OUTPUT_SEMANTICS_RANKING_SCORE,
    output_semantics_for_basis,
    semantics_supports_event_label_metrics,
)

OUTPUT_KIND_PROBABILITY = "probability"
OUTPUT_KIND_RANK_SCORE = "rank_score"
OUTPUT_KIND_EXPECTED_RETURN = "expected_return"
OUTPUT_KIND_RISK_SCORE = "risk_score"
OUTPUT_KIND_UNKNOWN = "unknown"

OUTPUT_KINDS = (
    OUTPUT_KIND_PROBABILITY,
    OUTPUT_KIND_RANK_SCORE,
    OUTPUT_KIND_EXPECTED_RETURN,
    OUTPUT_KIND_RISK_SCORE,
    OUTPUT_KIND_UNKNOWN,
)

# 健康态（稳定契约）
MODEL_HEALTH_DEGRADED_UNVERIFIED = "degraded_unverified"
MODEL_HEALTH_OK = "ok"
MODEL_HEALTH_UNKNOWN = "unknown"

# 严禁的展示词（把非概率输出说成上涨概率）
FORBIDDEN_UP_PROBABILITY_TERMS = ("上涨概率", "上涨的可能性", "probability of rise")
# 只有真正的"上涨事件概率 + OOS 校准"才允许的展示词
UP_PROBABILITY_DISPLAY_TERM = "正收益概率"

_SEMANTICS_TO_KIND: dict[str, str] = {
    OUTPUT_SEMANTICS_EVENT_PROBABILITY: OUTPUT_KIND_PROBABILITY,
    OUTPUT_SEMANTICS_RANK_QUANTILE: OUTPUT_KIND_RANK_SCORE,
    OUTPUT_SEMANTICS_RANKING_SCORE: OUTPUT_KIND_RANK_SCORE,
}


@dataclass(frozen=True, slots=True)
class ModelSemantics:
    """一次模型输出的语义声明（报告/接口必须原样写出）。"""

    label_policy_id: str
    label_name: str
    horizon_days: int
    price_basis: str
    output_kind: str
    calibration: str
    output_semantics: str
    semantics_error: str = ""
    event_label_metrics_allowed: bool = False
    legacy_model_health: str = MODEL_HEALTH_UNKNOWN
    display_terms_allowed: tuple[str, ...] = ()
    display_terms_forbidden: tuple[str, ...] = ()
    display_note: str = ""
    label_basis: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "label_policy_id": self.label_policy_id,
            "label_name": self.label_name,
            "horizon_days": self.horizon_days,
            "price_basis": self.price_basis,
            "output_kind": self.output_kind,
            "calibration": self.calibration,
            "output_semantics": self.output_semantics,
            "semantics_error": self.semantics_error,
            "event_label_metrics_allowed": self.event_label_metrics_allowed,
            "legacy_model_health": self.legacy_model_health,
            "display_terms_allowed": list(self.display_terms_allowed),
            "display_terms_forbidden": list(self.display_terms_forbidden),
            "display_note": self.display_note,
            "label_basis": self.label_basis,
        }


def resolve_output_kind(*, label_basis: object = "", output_semantics: object = "") -> str:
    """把语义映射为 ``output_kind``：rank_quantile → rank_score（不是 probability）。"""
    semantics = str(output_semantics or "").strip()
    if not semantics and str(label_basis or "").strip():
        semantics, _ = _safe_semantics_for_basis(label_basis)
    if not semantics:
        return OUTPUT_KIND_UNKNOWN
    return _SEMANTICS_TO_KIND.get(semantics, OUTPUT_KIND_UNKNOWN)


def _safe_semantics_for_basis(basis: object) -> tuple[str, str]:
    """安全版 basis → 语义：未登记 basis 返回 ``("", 原因)`` 而不是抛异常。

    语义守卫活在报告/接口路径上，**绝不能因为遇到未登记 label 就把整条链路打崩**：
    未登记本身就是它要暴露的事实（fail-closed 的是"概率化文案"，不是报告）。
    """
    text = str(basis or "").strip()
    if not text:
        return "", ""
    try:
        resolved = output_semantics_for_basis(text)
    except Exception as exc:  # noqa: BLE001 - 未登记/解析失败都要如实记录
        return "", f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    return str(resolved or ""), ""


def describe_model_semantics(
    *,
    label_policy_id: object = "",
    label_basis: object = "",
    output_semantics: object = "",
    calibration: object = "",
    horizon_days: object = None,
    price_basis: object = "",
    label_name: object = "",
    has_oos_calibration: bool | None = None,
    artifact_metadata: dict[str, Any] | None = None,
) -> ModelSemantics:
    """汇总语义声明 + 显示口径守卫结论（不改任何输出值）。

    ``has_oos_calibration``：调用方若能证明该模型有 OOS 校准证据则传 True；
    为 None/False 时**不允许**把输出显示成"正收益概率"（fail-closed 的不是模型，
    而是文案）。

    ``label_basis``：语义注册表按 **label basis**（如 ``soup_10d_tp8_before_sl5`` /
    ``return_rank``）解析，而不是按 label_policy_id。未登记 basis 只记录原因，
    不抛异常。
    """
    label_id = str(label_policy_id or "").strip()
    metadata = artifact_metadata or {}
    basis = str(label_basis or "").strip() or str(
        metadata.get("label_basis", "") or metadata.get("basis", "") or ""
    )
    if not basis:
        # 兼容：部分调用方（或历史报告）把 basis 放在 label_policy_id 里
        basis = label_id
    semantics_from_basis, semantics_error = _safe_semantics_for_basis(basis)
    if not semantics_from_basis:
        # 形如 "<basis>_policy_<hash>" 的 id：退化到前缀再试一次
        prefix = basis.split("_policy_", 1)[0]
        if prefix and prefix != basis:
            semantics_from_basis, semantics_error = _safe_semantics_for_basis(prefix)
    resolved_semantics = str(output_semantics or semantics_from_basis or "").strip()
    if not resolved_semantics and not semantics_error:
        semantics_error = (
            f"semantics_unresolved_for_basis:{basis}" if basis else "semantics_undeclared"
        )
    kind = resolve_output_kind(
        label_basis=basis, output_semantics=resolved_semantics
    )
    event_metrics_allowed = semantics_supports_event_label_metrics(resolved_semantics or None)
    calibration_text = str(calibration or "").strip()
    resolved_label_name = str(label_name or "").strip() or str(
        metadata.get("label_name", "") or ""
    )
    resolved_horizon = _as_int(
        horizon_days if horizon_days is not None else metadata.get("horizon_days"), default=0
    )
    resolved_basis = str(price_basis or "").strip() or str(
        metadata.get("price_basis", "") or ""
    )

    allowed: list[str] = []
    note = ""
    if kind == OUTPUT_KIND_PROBABILITY and has_oos_calibration:
        allowed.append(UP_PROBABILITY_DISPLAY_TERM)
        note = "事件概率语义 + 有 OOS 校准证据：允许显示为正收益概率（仍需写明 hor/价格口径）"
    else:
        if kind == OUTPUT_KIND_RANK_SCORE:
            note = (
                "rank_quantile（横截面分位）只能解释为该标签定义下的分位归属："
                "0.5 是上尾 vs 下尾边界，不是涨跌边界；禁止显示为上涨概率。"
            )
        elif kind == OUTPUT_KIND_UNKNOWN:
            note = "语义未登记/无法解析：禁止任何概率化文案，先补 label 契约。"
        else:
            note = "无 OOS 校准证据：只允许作为排序分/分量使用，禁止概率化文案。"

    legacy_health = (
        MODEL_HEALTH_OK
        if (resolved_semantics and event_metrics_allowed and has_oos_calibration)
        else (
            MODEL_HEALTH_DEGRADED_UNVERIFIED
            if resolved_semantics
            else MODEL_HEALTH_UNKNOWN
        )
    )
    return ModelSemantics(
        label_policy_id=label_id,
        label_name=resolved_label_name,
        horizon_days=resolved_horizon,
        price_basis=resolved_basis,
        output_kind=kind,
        calibration=calibration_text,
        output_semantics=resolved_semantics,
        semantics_error=str(semantics_error or ""),
        event_label_metrics_allowed=bool(event_metrics_allowed),
        legacy_model_health=legacy_health,
        display_terms_allowed=tuple(allowed),
        display_terms_forbidden=FORBIDDEN_UP_PROBABILITY_TERMS,
        display_note=note,
        label_basis=basis,
    )


def guard_display_text(text: str, semantics: ModelSemantics) -> dict[str, object]:
    """检查一段展示文案是否违反语义守卫（只报结论，不改文案）。

    返回 ``{"allowed": bool, "violations": [...]}``：命中禁用词（把非概率输出说成
    上涨概率）即违规；即便语义是概率，只要没有 OOS 校准证据也算违规。
    """
    violations: list[str] = []
    lowered = str(text or "")
    for term in FORBIDDEN_UP_PROBABILITY_TERMS:
        if term in lowered:
            violations.append(f"forbidden_term:{term}")
    if UP_PROBABILITY_DISPLAY_TERM in lowered and UP_PROBABILITY_DISPLAY_TERM not in (
        semantics.display_terms_allowed
    ):
        violations.append(f"term_requires_oos_calibration:{UP_PROBABILITY_DISPLAY_TERM}")
    return {"allowed": not violations, "violations": violations}


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "FORBIDDEN_UP_PROBABILITY_TERMS",
    "MODEL_HEALTH_DEGRADED_UNVERIFIED",
    "MODEL_HEALTH_OK",
    "MODEL_HEALTH_UNKNOWN",
    "OUTPUT_KINDS",
    "OUTPUT_KIND_EXPECTED_RETURN",
    "OUTPUT_KIND_PROBABILITY",
    "OUTPUT_KIND_RANK_SCORE",
    "OUTPUT_KIND_RISK_SCORE",
    "OUTPUT_KIND_UNKNOWN",
    "UP_PROBABILITY_DISPLAY_TERM",
    "ModelSemantics",
    "describe_model_semantics",
    "guard_display_text",
    "resolve_output_kind",
]
