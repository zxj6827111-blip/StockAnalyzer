"""工件输出健康门（学习链整改 v2 批次 B2）。

定位：**补现有晋升门不覆盖的输出语义检查**，并把批准→发布→热载统一到同一判据。
不是"补一道不存在的门"（现有 `evaluate_promotion_validity` 已能拦下 9/13 工件，
靠的是 `nav_compounding_explosion`——而那是已知的口径产物，一旦被重新设计或
豁免，退化工件就没有任何检查能拦住）。

判据设计原则（v2 §B2）：

- **确定性失败立即 hard-block**：非有限值、常数输出、契约不匹配（**已声明输出
  语义但字段残缺**、无打分样本）；
- **无法评估只作 advisory**：工件完全没有 B1 输出语义字段（legacy / 其他产线
  的手写指标）→ 不能判定，记 warning + ``checks['evaluable']=False`` 放行——
  "契约不匹配"指声明与实际自相矛盾，不是"缺少新增字段"这种覆盖缺口，
  否则等于用新门把所有旧工件挡在门外；
- **经验阈值只作 advisory**：`unique < 20`、raw→calibrated 的 AUC 落差、跨尺度
  可比性（同尺度比值）——它们是待验证规则，不能当通用硬门；
- **不设 `positive_rate ∈ [0.30, 0.70]` 通用硬门**：审核反例成立——分数全在
  0.51~0.59、预测正率 100% 的输出可能 AUC=1、Precision@K=1，该门会误杀排序
  完全正确的模型；
- **不跨分数尺度复用 spread 绝对阈值**：`mean_prob_spread` 是正负真实标签组的
  预测均值差，依赖分数尺度，raw / rank / probability 之间不可共用同一阈值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# 主输出：raw 与 calibrated 的 blend（blend 是生产实际消费的那一路）。
RAW_BLEND = "raw_blend"
CALIBRATED_BLEND = "calibrated_blend"
# 「待验证规则」的经验阈值（advisory）：唯一取值数下限。
ADVISORY_MIN_UNIQUE_VALUES = 20
# raw→calibrated 的 AUC 落差怀疑阈值（advisory）。
ADVISORY_AUC_DROP = 0.30
# calibrated spread 相对 raw 塌缩比例（同尺度比值，advisory）。
ADVISORY_SPREAD_COLLAPSE_RATIO = 0.2


@dataclass(frozen=True, slots=True)
class OutputHealthReport:
    valid: bool
    blocking_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "blocking_reasons": list(self.blocking_reasons),
            "warnings": list(self.warnings),
            "checks": dict(self.checks),
        }


def evaluate_output_health(
    metrics_summary: Mapping[str, float],
    *,
    raw_output: str = RAW_BLEND,
    calibrated_output: str = CALIBRATED_BLEND,
) -> OutputHealthReport:
    """对工件落盘指标做输出语义检查（确定性失败 hard-block）。

    只读 ``metrics_summary``，不改任何状态——便于在批准、发布、热载三条路径
    复用同一个判据，理由可查。
    """

    blocking: list[str] = []
    warnings: list[str] = []
    checks: dict[str, object] = {}

    def _metric(name: str) -> float | None:
        value = metrics_summary.get(name)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    evaluable = any(
        str(key).startswith(("scored_samples_", "unique_values_")) for key in metrics_summary
    )
    checks["evaluable"] = evaluable
    if not evaluable:
        # 完全没有输出语义字段：无法判定（legacy / 其他产线工件）→ advisory。
        warnings.append("output_health_not_evaluable_missing_output_semantics")
        return OutputHealthReport(
            valid=True,
            blocking_reasons=[],
            warnings=sorted(set(warnings)),
            checks=checks,
        )

    for scale, output in (("raw", raw_output), ("calibrated", calibrated_output)):
        scored = _metric(f"scored_samples_{output}")
        unique = _metric(f"unique_values_{output}")
        non_finite = _metric(f"non_finite_count_{output}")
        checks[f"scored_samples_{scale}"] = scored
        checks[f"unique_values_{scale}"] = unique
        checks[f"non_finite_count_{scale}"] = non_finite
        checks[f"tie_fraction_{scale}"] = _metric(f"tie_fraction_{output}")
        checks[f"auc_{scale}"] = _metric(f"auc_{output}")

        if scored is None or unique is None:
            # 契约不匹配：工件**声明了**输出语义（有部分字段）却缺该尺度的关键项，
            # 自相矛盾 → fail-closed。完全没有输出语义字段的情形已在上面按
            # advisory 放行（legacy，不能判定）。
            blocking.append(f"output_health_metrics_missing:{scale}")
            continue
        if scored <= 0.0:
            blocking.append(f"output_health_no_scored_samples:{scale}")
            continue
        if non_finite is not None and non_finite > 0.0:
            blocking.append(f"output_health_non_finite_values:{scale}")
        if unique <= 1.0 and scored >= 2.0:
            # 常数输出无法排序；这是确定性失败，不是经验阈值。
            blocking.append(f"output_health_constant_output:{scale}")
        elif 0.0 < unique < float(ADVISORY_MIN_UNIQUE_VALUES) and scored >= float(
            ADVISORY_MIN_UNIQUE_VALUES
        ):
            warnings.append(f"output_health_low_unique_values_advisory:{scale}")

    raw_auc = _metric(f"auc_{raw_output}")
    calibrated_auc = _metric(f"auc_{calibrated_output}")
    raw_spread = _metric(f"mean_prob_spread_{raw_output}")
    calibrated_spread = _metric(f"mean_prob_spread_{calibrated_output}")
    if raw_auc is not None and calibrated_auc is not None:
        checks["auc_drop"] = round(raw_auc - calibrated_auc, 6)
        if raw_auc - calibrated_auc > ADVISORY_AUC_DROP:
            warnings.append("output_health_auc_drop_advisory")
    if raw_spread is not None and calibrated_spread is not None and abs(raw_spread) > 0.0:
        ratio = abs(calibrated_spread) / abs(raw_spread)
        checks["spread_retention_ratio"] = round(ratio, 6)
        # 只比同尺度比值，不引入跨 raw/rank/probability 的绝对阈值。
        if ratio < ADVISORY_SPREAD_COLLAPSE_RATIO:
            warnings.append("output_health_calibration_spread_collapse_advisory")

    return OutputHealthReport(
        valid=not blocking,
        blocking_reasons=sorted(set(blocking)),
        warnings=sorted(set(warnings)),
        checks=checks,
    )
