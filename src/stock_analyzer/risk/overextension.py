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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from stock_analyzer.config import OverextensionConfig

DEFAULT_ATR14_FALLBACK = 0.03
DEFAULT_MA5_FALLBACK = 1.0


@dataclass(slots=True)
class OverextensionRiskDecision:
    level: str  # none | warn | reject
    penalty: float
    reject_new_buy: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


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


def _normalized_bias(row: Mapping[str, Any]) -> float:
    """bias_ma5 = (close - ma5) / ma5，取绝对值上限封顶 1.0。"""
    close = _numeric(row.get("close"), 0.0)
    ma5 = _numeric(row.get("ma5"), _numeric(row.get("ma5_from_ma20"), DEFAULT_MA5_FALLBACK))
    if close <= 0 or ma5 <= 0:
        return 0.0
    return abs(close / ma5 - 1.0)


def _atr_distance(row: Mapping[str, Any]) -> float:
    """(close - ma5) 以 ATR 衡量的距离；无 ATR 时用保守默认。"""
    close = _numeric(row.get("close"), 0.0)
    ma5 = _numeric(row.get("ma5"), _numeric(row.get("ma5_from_ma20"), DEFAULT_MA5_FALLBACK))
    atr14 = _numeric(row.get("atr14"), _numeric(row.get("atr_20d"), DEFAULT_ATR14_FALLBACK))
    if close <= 0 or ma5 <= 0 or atr14 <= 0:
        return 0.0
    return abs(close - ma5) / atr14


def evaluate_overextension(
    row: Mapping[str, Any],
    config: OverextensionConfig,
) -> OverextensionRiskDecision:
    """单行（symbol/trade_date 对齐的 bar 或 snapshot 行）过热风险判定。"""
    bias = _normalized_bias(row)
    atr_distance = _atr_distance(row)
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

    # 附加风险项：5 日涨幅 / 跳空 / 量价背离
    ret5 = _numeric(row.get("ret5"), 0.0)
    gap_pct = _numeric(row.get("gap_pct"), 0.0)
    volume_ratio_5d = _numeric(row.get("volume_ratio_5d"), 1.0)
    if ret5 >= config.ret5_warn_threshold:
        level = max(level, "warn") if level == "warn" else level
        reasons.append("ret5_high")
        penalty = max(penalty, float(config.extra_penalty))
    if gap_pct >= config.gap_warn_threshold:
        reasons.append("large_gap")
        penalty = max(penalty, float(config.extra_penalty))
    if volume_ratio_5d >= config.volume_divergence_ratio and bias > config.bias_warn_min:
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
    )
