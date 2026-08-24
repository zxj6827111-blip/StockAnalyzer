"""Slot-occupied realized NAV simulator and promotion validity gate (P1-b).

（置于 learning 包以避免 evolution 包 __init__ 的重导入链造成循环依赖。）

背景（NAS 8/23 根因重定性）：e44 的 5.58e44 "收益"来自
**逐笔全仓复利 × 重复快照** 口径——3936 笔 mean_ret≈+2.9% 全仓复利的
理论上界达 1.5e46，数字本身无意义。本模块提供三件事：

1. :func:`simulate_event_driven_slot_nav`——**事件驱动** slot NAV 模拟
   （补救版主口径）：仓位金额=入场时 current_nav/max_positions（NAV 滚动）、
   同侧同 symbol 最多一个未退出仓位、日内先结算退出再开新仓、期末未退出
   不结算计 open_position_count、max_drawdown 为已实现 NAV 回撤（不含浮动）、
   确定性排序（概率 desc, symbol）、日级序列基于 entry/exit 事件日并集。
2. :func:`simulate_slot_occupied_realized_nav`——旧"固定基数"版，保留为
   naive 参照指标（naive_compounded_nav/compounding_explosion 语义不变）。
3. :func:`evaluate_promotion_validity`——晋级有效性门：发布/晋级前对评估
   产物做口径、日期覆盖与类别充足性检查，任一 blocking 命中即拒绝晋级。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import cast

# 复利参照超过该倍数视为口径爆炸（e44 案例 >1e44；正常策略难以越过 1e6）。
DEFAULT_EXPLOSION_THRESHOLD = 1_000_000.0
DEFAULT_SLOTS = 4
# 事件驱动模拟的默认仓位上限（与 EvolutionConfig.m11_max_positions 默认一致）。
DEFAULT_MAX_POSITIONS = 10
# 硬标签达到该数量后，AUC 单类（auc_valid=0）才视为 blocking；小样本下
# 单类测试集常见，只作警告。
_AUC_BLOCK_MIN_HARD_LABELS = 20
# 晋级硬门默认阈值（补救计划 A7：unique 日期 ≥20、硬类别 ≥30/类）。
DEFAULT_MIN_TEST_TRADE_DATES = 20
DEFAULT_MIN_HARD_CLASS_SAMPLES = 30


@dataclass(frozen=True, slots=True)
class SlotOccupiedNavReport:
    """一次 slot 占用 NAV 模拟的结果。"""

    trade_count: int
    slots: int
    slot_capital_fraction: float
    slot_occupied_realized_nav: float
    naive_compounded_nav: float
    compounding_explosion: bool


@dataclass(frozen=True, slots=True)
class EventSlotPositionInput:
    """一笔候选仓位的事件输入（单侧：同一 side 的事件列表单独模拟）。

    缺陷输入（缺日期/exit<entry/非法收益/不可归一日期）不参与模拟，由
    :attr:`EventSlotNavReport.coverage_defects` 以 ``insufficient_date_coverage``
    redline 显式暴露——绝不静默回退。
    """

    symbol: str
    entry_date: str
    exit_date: str
    realized_return: float | None
    probability: float = 0.5


@dataclass(frozen=True, slots=True)
class EventSlotNavReport:
    """事件驱动 slot NAV 模拟结果。"""

    final_nav: float
    total_return: float
    settled_position_count: int
    open_position_count: int
    event_days: int
    daily_nav_series: dict[str, float]
    max_drawdown: float
    skipped_capacity_count: int
    skipped_symbol_conflict_count: int
    insufficient_date_coverage: bool
    coverage_defects: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "final_nav": round(self.final_nav, 6),
            "total_return": round(self.total_return, 6),
            "settled_position_count": self.settled_position_count,
            "open_position_count": self.open_position_count,
            "event_days": self.event_days,
            "daily_nav_series": {
                key: round(value, 6) for key, value in self.daily_nav_series.items()
            },
            "max_drawdown": round(self.max_drawdown, 6),
            "skipped_capacity_count": self.skipped_capacity_count,
            "skipped_symbol_conflict_count": self.skipped_symbol_conflict_count,
            "insufficient_date_coverage": self.insufficient_date_coverage,
            "coverage_defects": list(self.coverage_defects),
        }


def _parse_event_date(text: str) -> date | None:
    """按 Asia/Shanghai 日历日归一事件日期文本；无法解析返回 None。

    输入约定为本地（上海）日历日的 ISO 文本；带时区的 ISO 时间戳先落到
    上海墙钟（+8 固定偏移，无夏令时）再取日期。
    """

    normalized = (text or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # UTC 时间戳 +8h 归一为上海墙钟日期。
        parsed = parsed + timedelta(hours=8)
    return parsed.date()


def simulate_event_driven_slot_nav(
    events: Sequence[EventSlotPositionInput],
    *,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    initial_nav: float = 1.0,
    horizon_date: str | None = None,
) -> EventSlotNavReport:
    """事件驱动 slot NAV 模拟（P1-b 主口径）。

    规则：
    - 仓位金额 = **入场时** ``current_nav / max_positions``（NAV 随已实现盈亏滚动）；
    - 同侧同 symbol 最多一个未退出仓位（冲突开仓跳过并计数）；
    - 同一事件日**先结算退出、再开新仓**；
    - 期末未退出的仓位不结算，计入 ``open_position_count``；
    - ``max_drawdown`` 只基于已实现 NAV 日级序列（不含浮动盈亏）；
    - 同日内开仓顺序确定性：概率降序、symbol 升序；
    - ``horizon_date``（ISO）为观察期截止：退出晚于该日的仓位不结算，
      计入 ``open_position_count``（“期末未退出不结算”）；不传则全部结算。
    """

    normalized_positions = int(max_positions)
    if normalized_positions < 1:
        raise ValueError("max_positions must be >= 1")
    defects: list[str] = []
    valid_entries: list[tuple[date, date, float, str, float]] = []
    all_dates: set[date] = set()
    horizon: date | None = None
    if horizon_date:
        horizon = _parse_event_date(horizon_date)
        if horizon is None:
            # horizon 是调用参数而非样本字段：非法值必须显式失败，
            # 不允许静默降级为“无观察期”。
            raise ValueError(f"horizon_date is not parseable: {horizon_date!r}")

    for index, event in enumerate(events):
        label = f"#{index}:{event.symbol}"
        entry_day = _parse_event_date(event.entry_date)
        exit_day = _parse_event_date(event.exit_date)
        realized = event.realized_return
        if entry_day is None:
            defects.append(f"{label}:missing_or_unparseable_entry_date")
            continue
        if exit_day is None:
            defects.append(f"{label}:missing_or_unparseable_exit_date")
            continue
        if exit_day < entry_day:
            defects.append(f"{label}:exit_before_entry")
            continue
        if exit_day == entry_day:
            # 同日进出在 horizon>0 的标签语义下不可能合法出现，
            # 视为数据缺陷 fail-closed（不参与模拟、不静默漏算）。
            defects.append(f"{label}:same_day_entry_exit")
            continue
        if horizon is not None and entry_day > horizon:
            # 观察期截止之后才入场：整条事件不参与模拟与日级序列。
            defects.append(f"{label}:entry_after_horizon")
            continue
        if realized is None or not math.isfinite(float(realized)):
            defects.append(f"{label}:missing_or_invalid_realized_return")
            continue
        clamped_return = max(-1.0, float(realized))
        valid_entries.append(
            (
                entry_day,
                exit_day,
                clamped_return,
                event.symbol.strip(),
                float(event.probability),
            )
        )
        all_dates.add(entry_day)
        all_dates.add(min(exit_day, horizon) if horizon is not None else exit_day)

    entries_by_day: dict[date, list[tuple[float, str, date, float, int]]] = {}
    exits_by_day: dict[date, list[int]] = {}
    for order_index, (entry_day, exit_day, trade_return, symbol, probability) in enumerate(
        valid_entries
    ):
        entries_by_day.setdefault(entry_day, []).append(
            (probability, symbol, exit_day, trade_return, order_index)
        )
        exits_by_day.setdefault(exit_day, []).append(order_index)

    current_nav = float(initial_nav)
    open_positions: dict[str, tuple[int, float]] = {}  # symbol -> (order_index, size)
    settled = 0
    skipped_capacity = 0
    skipped_conflict = 0
    daily_series: dict[str, float] = {}

    for day in sorted(all_dates):
        # ① 先结算当日到期仓位（先退后进，测试锁定）。
        for order_index in exits_by_day.get(day, []):
            holder = next(
                (
                    (sym, slot)
                    for sym, slot in open_positions.items()
                    if slot[0] == order_index
                ),
                None,
            )
            if holder is None:
                continue
            symbol, (_idx, size) = holder
            _entry_day, _exit_day, trade_return, _sym, _prob = valid_entries[order_index]
            # NAV 钳到 0：旧仓位以更高 NAV 定价、结算时 NAV 已回落的情况下，
            # 极端亏损叠加可能把 NAV 压成负数；破产即停，保持口径可解释。
            current_nav = max(0.0, current_nav + size * trade_return)
            del open_positions[symbol]
            settled += 1
        # ② 再开新仓：概率 desc、symbol asc；同 symbol 已有未平仓 → 冲突跳过。
        for _probability, symbol, _exit_day, _trade_return, order_index in sorted(
            entries_by_day.get(day, []), key=lambda item: (-item[0], item[1])
        ):
            if symbol in open_positions:
                skipped_conflict += 1
                continue
            if len(open_positions) >= normalized_positions:
                skipped_capacity += 1
                continue
            position_size = current_nav / normalized_positions
            open_positions[symbol] = (order_index, position_size)

        daily_series[day.isoformat()] = current_nav

    peak = initial_nav
    max_drawdown = 0.0
    for value in daily_series.values():
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, 1.0 - value / peak)

    return EventSlotNavReport(
        final_nav=current_nav,
        total_return=current_nav - initial_nav,
        settled_position_count=settled,
        open_position_count=len(open_positions),
        event_days=len(all_dates),
        daily_nav_series=daily_series,
        max_drawdown=max_drawdown,
        skipped_capacity_count=skipped_capacity,
        skipped_symbol_conflict_count=skipped_conflict,
        insufficient_date_coverage=bool(defects),
        coverage_defects=sorted(defects),
    )


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
    test_stats: Mapping[str, float] | None = None,
    manifest_schema_version: str = "",
    require_full_gates: bool = False,
    min_test_trade_dates: int = DEFAULT_MIN_TEST_TRADE_DATES,
    min_hard_class_samples: int = DEFAULT_MIN_HARD_CLASS_SAMPLES,
    min_logical_samples: int | None = None,
) -> PromotionValidityReport:
    """晋级有效性门（fail-closed）。

    blocking（基础四项）：
    - ``auc_invalid_single_class``：硬标签充足（≥20）却无两类样本；
    - ``duplicate_dominance``：去重丢弃占比 > 50%（重复主导的数据集）；
    - ``blocking_quality_flags``：manifest 层任何 blocking 旗标；
    - ``nav_compounding_explosion``：复利参照 NAV 超阈值（含显式传入的
      realized_returns 重算结果或训练时落盘的 dataset 级指标）。

    blocking（测试段硬门，提供 ``test_stats`` 或 ``require_full_gates`` 时）：
    - ``insufficient_test_trade_dates``：unique test 交易日 < ``min_test_trade_dates``；
    - ``insufficient_unique_logical_samples``：unique 逻辑样本
      (symbol,strategy,trade_date) 不足；
    - ``insufficient_hard_class_samples``：hard 正/负类各自
      < ``min_hard_class_samples``；
    - ``auc_invalid``：``require_full_gates`` 时 auc_valid 必须 =1；
    - ``manifest_not_v2``：manifest schema 非 v2（v1 无法自证无重复，
      fail-closed 一律拦截）。

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

    # —— 测试段硬门（补救 A7）——
    effective_logical_min = (
        min_logical_samples
        if min_logical_samples is not None
        else max(min_hard_class_samples, 1)
    )
    if manifest_schema_version:
        checks["manifest_schema_version"] = manifest_schema_version
        if manifest_schema_version.strip() != "2":
            # v1 manifest 无法自证无重复快照：一律拦截（fail-closed）。
            blocking.append("manifest_not_v2")
    if test_stats is not None or require_full_gates:
        stats = dict(test_stats or {})

        def _stat(name: str) -> float:
            try:
                return float(stats.get(name, 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        unique_dates = _stat("unique_trade_dates")
        unique_logical = _stat("unique_logical_samples")
        hard_positive = _stat("hard_positive_count")
        hard_negative = _stat("hard_negative_count")
        checks["test_unique_trade_dates"] = unique_dates
        checks["test_unique_logical_samples"] = unique_logical
        checks["test_hard_positive_count"] = hard_positive
        checks["test_hard_negative_count"] = hard_negative
        if unique_dates < max(1, int(min_test_trade_dates)):
            blocking.append("insufficient_test_trade_dates")
        if unique_logical < effective_logical_min:
            blocking.append("insufficient_unique_logical_samples")
        if (
            hard_positive < max(1, int(min_hard_class_samples))
            or hard_negative < max(1, int(min_hard_class_samples))
        ):
            blocking.append("insufficient_hard_class_samples")
        if require_full_gates and (auc_valid is None or auc_valid < 1.0):
            blocking.append("auc_invalid")

    return PromotionValidityReport(
        valid=not blocking,
        blocking_reasons=sorted(set(blocking)),
        warnings=warnings,
        checks=checks,
    )
