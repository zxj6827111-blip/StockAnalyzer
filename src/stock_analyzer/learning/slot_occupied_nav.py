"""Slot-occupied realized NAV simulator and promotion validity gate (P1-b).

（置于 learning 包以避免 evolution 包 __init__ 的重导入链造成循环依赖。）

背景（NAS 8/23 根因重定性）：e44 的 5.58e44 “收益”来自
**逐笔全仓复利 × 重复快照** 口径——3936 笔 mean_ret≈+2.9% 全仓复利的
理论上界达 1.5e46，数字本身无意义。整改两件事：

1. :func:`simulate_slot_occupied_realized_nav`——按 ``max_slots`` 等分资金、
   单笔只占用一个槽位、收益不滚动进后续仓位规模（固定基数）的保守 NAV
   模拟；同时给出旧复利口径参照值并标记爆炸（compounding_explosion）。
2. :func:`evaluate_promotion_validity`——晋级有效性门：发布/晋级前对评估
   产物做口径与有限性检查，任一 blocking 命中即拒绝晋级。

注：门的具体阈值与旗标清单为按诊断证据重建的实现细节（原规格文本在会话
截断中丢失），设计取向是“结构性失效必须拦、小样本不稳定只告警”。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

# 复利参照超过该倍数视为口径爆炸（e44 案例 >1e44；正常策略难以越过 1e6）。
DEFAULT_EXPLOSION_THRESHOLD = 1_000_000.0
DEFAULT_SLOTS = 4
# 硬标签达到该数量后，AUC 单类（auc_valid=0）才视为 blocking；小样本下
# 单类测试集常见，只作警告。
_AUC_BLOCK_MIN_HARD_LABELS = 20


@dataclass(frozen=True, slots=True)
class SlotOccupiedNavReport:
    """一次 slot 占用 NAV 模拟的结果。"""

    trade_count: int
    slots: int
    slot_capital_fraction: float
    slot_occupied_realized_nav: float
    naive_compounded_nav: float
    compounding_explosion: bool


def simulate_slot_occupied_realized_nav(
    *,
    realized_returns: Sequence[float],
    max_slots: int = DEFAULT_SLOTS,
    explosion_threshold: float = DEFAULT_EXPLOSION_THRESHOLD,
) -> SlotOccupiedNavReport:
    """固定基数槽位占用 NAV 模拟。

    - 每个 slot 初始资金 = 总资金的 ``1/max_slots``；
    - 每笔交易占用一个 slot，盈亏记在该 slot 的**初始**份额上，
      不复利滚入后续交易（与逐笔全仓复利相对）；
    - ``naive_compounded_nav`` 为旧口径参照：Π(1+r)，用于识别口径爆炸。
    """

    normalized_slots = int(max_slots)
    if normalized_slots < 1:
        raise ValueError("max_slots must be >= 1")
    fraction = 1.0 / normalized_slots

    slot_pnl_sum = 0.0
    compounded_log_sum = 0.0
    compounded_overflowed = False
    ruined = False
    for raw_return in realized_returns:
        trade_return = float(raw_return)
        # 已实现收益不可能低于 -100%；钳制保证有限性。
        clamped = max(-1.0, trade_return)
        slot_pnl_sum += clamped * fraction
        if ruined:
            continue
        if clamped <= -1.0:
            # 单笔 -100% 即复利口径下破产（NAV=0），后续无需累加。
            ruined = True
            continue
        compounded_log_sum += math.log1p(clamped)
        if not math.isfinite(compounded_log_sum) or compounded_log_sum > 700:
            # exp(700) ≈ 1e304，超出即认定爆炸，停止累加。
            compounded_overflowed = True

    slot_nav = 1.0 + slot_pnl_sum
    if ruined:
        naive_nav = 0.0
    elif compounded_overflowed:
        naive_nav = math.inf
    else:
        naive_nav = math.exp(min(compounded_log_sum, 700))
    explosion = (
        math.isinf(naive_nav)
        or naive_nav > explosion_threshold
        or not math.isfinite(slot_nav)
    )
    return SlotOccupiedNavReport(
        trade_count=len(realized_returns),
        slots=normalized_slots,
        slot_capital_fraction=fraction,
        slot_occupied_realized_nav=float(max(0.0, slot_nav)),
        naive_compounded_nav=float(naive_nav),
        compounding_explosion=bool(explosion),
    )


@dataclass(frozen=True, slots=True)
class PromotionValidityReport:
    """晋级有效性门的判定结果。"""

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


def _dedup_count(dedup_quality: Mapping[str, object] | None, key: str) -> float:
    if dedup_quality is None:
        return 0.0
    try:
        return float(str(dedup_quality.get(key, 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def evaluate_promotion_validity(
    *,
    metrics_summary: Mapping[str, float],
    dedup_quality: Mapping[str, object] | None = None,
    realized_returns: Sequence[float] | None = None,
    slots: int = DEFAULT_SLOTS,
    explosion_threshold: float = DEFAULT_EXPLOSION_THRESHOLD,
    min_hard_labels_warn: int = 30,
) -> PromotionValidityReport:
    """晋级有效性门（fail-closed）。

    blocking：
    - ``auc_invalid_single_class``：硬标签充足（≥20）却无两类样本；
    - ``duplicate_dominance``：去重丢弃占比 > 50%（重复主导的数据集）；
    - ``blocking_quality_flags``：manifest 层任何 blocking 旗标；
    - ``nav_compounding_explosion``：复利参照 NAV 超阈值（含显式传入的
      realized_returns 重算结果或训练时落盘的 dataset 级指标）。

    warnings：
    - ``insufficient_hard_labels``：硬标签少于 ``min_hard_labels_warn``。
    """

    blocking: list[str] = []
    warnings: list[str] = []

    def _metric(name: str) -> float | None:
        value = metrics_summary.get(name)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    hard_label_count = _metric("hard_label_count") or 0.0
    auc_valid = _metric("auc_valid")

    checks: dict[str, object] = {
        "hard_label_count": hard_label_count,
        "auc_valid": auc_valid,
    }

    if hard_label_count < min_hard_labels_warn:
        warnings.append("insufficient_hard_labels")
    if (
        auc_valid is not None
        and auc_valid < 1.0
        and hard_label_count >= _AUC_BLOCK_MIN_HARD_LABELS
    ):
        blocking.append("auc_invalid_single_class")

    rows_before = _dedup_count(dedup_quality, "rows_before_dedup")
    rows_dropped = _dedup_count(dedup_quality, "rows_dropped_by_dedup")
    quality_flags: list[str] = []
    if dedup_quality is not None:
        raw_flags = cast(
            Sequence[object],
            dedup_quality.get("blocking_quality_flags", []) or [],
        )
        quality_flags = [str(item) for item in raw_flags]
    checks["dedup_rows_before"] = rows_before
    checks["dedup_rows_dropped"] = rows_dropped
    if rows_before > 0 and rows_dropped / rows_before > 0.5:
        blocking.append("duplicate_dominance")
    for flag in quality_flags:
        blocking.append(f"blocking_quality_flags:{flag}")

    nav_metrics_naive = _metric("dataset_naive_compounded_nav")
    nav_slot = _metric("dataset_slot_occupied_realized_nav")
    simulated: SlotOccupiedNavReport | None = None
    if realized_returns is not None:
        simulated = simulate_slot_occupied_realized_nav(
            realized_returns=realized_returns,
            max_slots=slots,
            explosion_threshold=explosion_threshold,
        )
        checks["slot_occupied_realized_nav"] = simulated.slot_occupied_realized_nav
        checks["naive_compounded_nav"] = simulated.naive_compounded_nav
        checks["trade_count"] = simulated.trade_count
        if simulated.compounding_explosion:
            blocking.append("nav_compounding_explosion")
    elif nav_metrics_naive is not None:
        checks["slot_occupied_realized_nav"] = nav_slot
        checks["naive_compounded_nav"] = nav_metrics_naive
        # JSON 指标里 inf 不可安全序列化：trainer 落盘的是封顶值 + 爆炸旗标。
        explosion_flag = _metric("dataset_nav_compounding_explosion")
        if (
            not math.isfinite(nav_metrics_naive)
            or nav_metrics_naive > explosion_threshold
            or (explosion_flag is not None and explosion_flag >= 1.0)
        ):
            blocking.append("nav_compounding_explosion")

    return PromotionValidityReport(
        valid=not blocking,
        blocking_reasons=sorted(set(blocking)),
        warnings=warnings,
        checks=checks,
    )
