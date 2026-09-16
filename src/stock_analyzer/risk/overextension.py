"""Unified overextension (过热) risk model shared by bars and snapshot paths.

PLAN P1: bars 与 snapshot 两条 baseline 路径必须共用同一 evaluator，消除
公式漂移。bias_ma5 乖离分层扣罚：

- 10%~15% 或距 MA5 超过 2 ATR：扣 0.3（warn）；
- 超过 15% 或超过 3 ATR：trend 轨拒绝新买入（reject_new_buy=True）；
- 5 日涨幅、跳空、量价背离作为附加风险项，阈值来自 ``overextension`` 配置，
  判定结果写入扫描审计结果。

输入偏好 snapshot 新特征列（ma5/ma10/atr14/bias_ma5/ret5/gap_pct/volume_ratio_5d），
缺失时回退到 bars 路径提供的近似字段（ma5/atr_20d/…），从而与直接 bars
评分路径共用同一份决策逻辑。

必要指标（close/ma5/atr14）任一不可用时**不做判定**，返回
``evaluation_status="insufficient_input"``：``level`` 仍是 ``none`` 以保持旧字段
兼容，但消费方不得据此认定"没有风险"——最终买入准入按输入不足显式拦截。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stock_analyzer.config import OverextensionConfig

# 评估状态：缺输入必须与外层"评估完成但结论是安全"区分开。只依赖
# ``level == "none"`` 判断安全的调用方会把"没算出来"读成"没有风险"
# （2026-09-16 的 P0 就是这么发生的：指标缺失 → 占位常量 → 无条件 reject；
# 修掉占位值后又变成缺输入静默 fail-open）。
EVALUATION_EVALUATED = "evaluated"
EVALUATION_INSUFFICIENT_INPUT = "insufficient_input"

# 算 ma5/atr14 的口径参数（与 learning/gate_metrics 的 harness 口径一致）
MA5_WINDOW = 5
ATR14_WINDOW = 14
LOOKBACK_BARS = 20
MIN_BARS_FOR_METRICS = 6


@dataclass(slots=True)
class OverextensionInputs:
    """从 OHLC 序列算出的过热闸输入（可直接并入 evaluator 的 row）。"""

    ma5: float
    atr14: float
    close: float
    ret5: float
    gap_pct: float
    bias_ma5: float
    atr_distance: float


def overextension_inputs_from_ohlc(
    ohlc: Sequence[Sequence[float]],
    *,
    lookback_bars: int = LOOKBACK_BARS,
    ma_window: int = MA5_WINDOW,
    atr_window: int = ATR14_WINDOW,
    min_bars: int = MIN_BARS_FOR_METRICS,
) -> OverextensionInputs | None:
    """由 ``(open, high, low, close)`` 序列（时间正序、末根为最新）算过热闸输入。

    公式**必须只有这一份定义**：`learning/gate_metrics.py`（harness 口径）与生产快照
    路径都调它。2026-09-16 的事故正是"同一个 evaluator、两套输入"——生产路径喂的 bar
    没有 ma5/atr14，于是 evaluator 取占位常量算出 ``bias = close - 1``，把任何股价
    > 1.15 元的票都判成"过热"、无条件否决买入。两处各算一套公式就会再犯一次。

    ``bias_ma5`` 与 ``atr_distance`` 对价格的**均匀缩放不变**（qfq/raw 只差一个常因子，
    分子分母同时缩放），故不要求与其它调用方共用复权口径。

    历史不足（< ``min_bars``）或数值不可用时返回 ``None``：调用方据此走"无法评估"
    分支（生产侧维持既有的 ``level: none`` 默认，即 fail-open），**绝不返回占位值**
    ——占位值一旦被当真实值参与阈值比较，就会算出荒谬结论。
    """
    points: list[tuple[float, float, float, float]] = []
    for item in ohlc:
        values = list(item)
        if len(values) < 4:
            return None
        o, h, low, c = (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
        if not all(math.isfinite(v) for v in (o, h, low, c)):
            continue
        if c <= 0 or h <= 0 or low <= 0:
            continue
        points.append((o, h, low, c))
    if len(points) < min_bars:
        return None
    window = points[-lookback_bars:] if lookback_bars > 0 else points
    closes = [p[3] for p in window]
    ma5 = sum(closes[-ma_window:]) / float(ma_window)
    if ma5 <= 0:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(window)):
        prev_close = window[index - 1][3]
        high_i, low_i = window[index][1], window[index][2]
        true_ranges.append(max(high_i - low_i, abs(high_i - prev_close), abs(low_i - prev_close)))
    recent = true_ranges[-atr_window:]
    if not recent:
        return None
    atr14 = sum(recent) / float(len(recent))
    if atr14 <= 0:
        return None
    close = closes[-1]
    prev_close = closes[-2]
    open_last = window[-1][0]
    ret5 = (close / closes[-ma_window] - 1.0) if len(closes) >= ma_window else 0.0
    gap_pct = ((open_last - prev_close) / prev_close) if prev_close > 0 else 0.0
    return OverextensionInputs(
        ma5=ma5,
        atr14=atr14,
        close=close,
        ret5=ret5,
        gap_pct=gap_pct,
        bias_ma5=abs(close / ma5 - 1.0),
        atr_distance=abs(close - ma5) / atr14,
    )


def overextension_row_from_bars(
    bars: object,
    *,
    base_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """由 bars（DataFrame 或 ``(open, high, low, close)`` 行序列）产出喂 evaluator 的 row。

    算不出输入时**原样返回 row**（不加任何占位键），让 evaluator 走"输入缺失"路径，
    而不是拿假值去比阈值。
    """
    row: dict[str, Any] = dict(base_row or {})
    if bars is None:
        return row
    records: object = bars
    to_dict = getattr(bars, "to_dict", None)
    if callable(to_dict):
        try:
            records = bars.to_dict(orient="records")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 结构不符就不加列
            return row
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        return row
    ohlc: list[list[float]] = []
    for item in records:
        if not isinstance(item, Mapping):
            return row
        if not all(key in item for key in ("open", "high", "low", "close")):
            return row
        ohlc.append([_numeric(item[key], 0.0) for key in ("open", "high", "low", "close")])
    inputs = overextension_inputs_from_ohlc(ohlc)
    if inputs is None:
        return row
    row.update(
        {
            "close": inputs.close,
            "ma5": inputs.ma5,
            "atr14": inputs.atr14,
            "ret5": inputs.ret5,
            "gap_pct": inputs.gap_pct,
        }
    )
    return row


@dataclass(slots=True)
class OverextensionRiskDecision:
    level: str  # none | warn | reject
    penalty: float
    reject_new_buy: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    # evaluated | insufficient_input。``level`` 继续保持旧语义（缺输入时仍是
    # "none"），因此**只判 level 的调用方必须同时看 evaluation_status**。
    evaluation_status: str = EVALUATION_EVALUATED
    missing_inputs: list[str] = field(default_factory=list)


def _numeric(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _positive_metric(row: Mapping[str, Any], names: Sequence[str]) -> tuple[float, bool]:
    """按名字顺序取第一个**可用**的正数指标，返回 (值, 是否可用)。

    可用 = 键存在、不是布尔、可转 float、有限且 > 0。缺键、None、NaN、inf、
    0、负数一律视为不可用——它们都算不出有意义的乖离，不能拿默认值顶上。
    """
    for name in names:
        if name not in row:
            continue
        raw = row.get(name)
        if raw is None or isinstance(raw, bool):
            continue
        value = _numeric(raw, float("nan"))
        if not math.isfinite(value) or value <= 0:
            continue
        return value, True
    return 0.0, False


@dataclass(slots=True)
class _ResolvedInputs:
    close: float
    ma5: float
    atr14: float
    bias: float
    atr_distance: float


# 过热判定的必要指标：三者的真值都必须存在，缺任何一个都无法给出可信结论。
_REQUIRED_METRIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "close": ("close",),
    "ma5": ("ma5", "ma5_from_ma20"),
    "atr14": ("atr14", "atr_20d"),
}


def _resolve_inputs(row: Mapping[str, Any]) -> tuple[_ResolvedInputs | None, list[str]]:
    """解析必要指标；任一不可用时返回 (None, 缺失清单)。"""
    resolved: dict[str, float] = {}
    missing: list[str] = []
    for label, names in _REQUIRED_METRIC_COLUMNS.items():
        value, ok = _positive_metric(row, names)
        if not ok:
            missing.append(label)
            continue
        resolved[label] = value
    if missing:
        return None, missing
    close = resolved["close"]
    ma5 = resolved["ma5"]
    atr14 = resolved["atr14"]
    return (
        _ResolvedInputs(
            close=close,
            ma5=ma5,
            atr14=atr14,
            bias=abs(close / ma5 - 1.0),
            atr_distance=abs(close - ma5) / atr14,
        ),
        [],
    )


def evaluate_overextension(
    row: Mapping[str, Any],
    config: OverextensionConfig,
) -> OverextensionRiskDecision:
    """单行（symbol/trade_date 对齐的 bar 或 snapshot 行）过热风险判定。

    输入不足（历史太短、缺列、非有限值、无有效 ATR）时**不做判定**：返回
    ``evaluation_status=insufficient_input``、``level="none"``、
    ``reject_new_buy=False``，且 ``metrics`` 里**不出现** bias/atr 键（缺键，
    不用 null 占位，也不用 1.0/0.03 之类的假默认值）。

    为什么在这里 fail-open 而不直接 reject：把"没算出来"当成"过热"就是
    2026-09-16 的 P0（600/600 候选被无条件否决）；但把它当成"安全"同样是错的。
    真正的拦截放在最终买入准入（`_final_signal_selector`）——那里按"输入不足"
    拒绝，对外原因是输入不足，而不是谎称过热。
    """
    resolved, missing = _resolve_inputs(row)
    if resolved is None:
        ret5 = _numeric(row.get("ret5"), 0.0)
        gap_pct = _numeric(row.get("gap_pct"), 0.0)
        volume_ratio_5d = _numeric(row.get("volume_ratio_5d"), 1.0)
        return OverextensionRiskDecision(
            level="none",
            penalty=0.0,
            reject_new_buy=False,
            reasons=["insufficient_input"],
            metrics={
                "ret5": round(ret5, 6),
                "gap_pct": round(gap_pct, 6),
                "volume_ratio_5d": round(volume_ratio_5d, 6),
            },
            evaluation_status=EVALUATION_INSUFFICIENT_INPUT,
            missing_inputs=missing,
        )

    bias = resolved.bias
    atr_distance = resolved.atr_distance
    level = "none"
    penalty = 0.0
    reasons: list[str] = []
    metrics: dict[str, float] = {
        "bias_ma5": round(bias, 6),
        "atr_distance": round(atr_distance, 6),
    }

    warn = bias >= config.bias_warn_min or atr_distance >= config.atr_distance_warn
    reject = bias >= config.bias_reject_min or atr_distance >= config.atr_distance_reject
    if warn:
        level = "warn"
        penalty = float(config.bias_penalty)
        reasons.append("bias_or_atr_distance_warn")
    if reject:
        level = "reject"
        reasons.append("bias_or_atr_distance_reject")

    # 附加风险项：5 日涨幅 / 跳空 / 量价背离。单独命中时至少升 warn 档
    # （避免 5 日大涨等过热信号在 low-bias 情况下完全无罚分）。
    ret5 = _numeric(row.get("ret5"), 0.0)
    gap_pct = _numeric(row.get("gap_pct"), 0.0)
    volume_ratio_5d = _numeric(row.get("volume_ratio_5d"), 1.0)
    if ret5 >= config.ret5_warn_threshold:
        if level == "none":
            level = "warn"
        reasons.append("ret5_high")
        penalty = max(penalty, float(config.extra_penalty))
    if gap_pct >= config.gap_warn_threshold:
        if level == "none":
            level = "warn"
        reasons.append("large_gap")
        penalty = max(penalty, float(config.extra_penalty))
    if volume_ratio_5d >= config.volume_divergence_ratio and bias > config.bias_warn_min:
        if level == "none":
            level = "warn"
        reasons.append("volume_divergence")
        penalty = max(penalty, float(config.extra_penalty))
    metrics.update(
        {
            "ret5": round(ret5, 6),
            "gap_pct": round(gap_pct, 6),
            "volume_ratio_5d": round(volume_ratio_5d, 6),
        }
    )

    return OverextensionRiskDecision(
        level=level,
        penalty=penalty,
        reject_new_buy=level == "reject",
        reasons=reasons,
        metrics=metrics,
        evaluation_status=EVALUATION_EVALUATED,
    )
