"""特征价格序列 vs 成交价格序列（S07 / 原 P0-06）。

原则（蓝图 §2.13）：

```text
Feature Series != Tradable Execution Series
```

- **特征**可以用稳定定义的复权序列（qfq），但必须固定规则并证明 as-of 安全；
- **成交**必须用 raw：open/high/low/close、涨跌停价、金额、费用、滑点、可成交性；
- **禁止**拿 qfq 的 open/close 当真实成交价（复权价是研究口径，不是当天挂单能成交的价）。

本模块把这两个口径**显式化并落报告**：每个回测/研究报告必须同时写出
``feature_price_mode`` 与 ``execution_price_mode``；执行侧不是 raw 时标
``execution_uncertain``，该样本应从主评价样本剔除（保留数量统计），而不是继续当
"可成交结果"用。

第一版不处理完整 corporate action（阶段施工提示词 S07：若无法完整处理，标
``execution_uncertain`` 并保留数量统计）。
"""

from __future__ import annotations

from dataclasses import dataclass

from stock_analyzer.config import StockAnalyzerConfig

FEATURE_PRICE_MODE_QFQ = "qfq"
FEATURE_PRICE_MODE_RAW = "raw"
EXECUTION_PRICE_MODE_RAW = "raw"


@dataclass(frozen=True, slots=True)
class PriceContract:
    """一次研究/回测的价格口径契约。"""

    feature_price_mode: str
    execution_price_mode: str
    dividend_treatment: str
    execution_uncertain: bool
    execution_uncertain_reason: str = ""

    @property
    def feature_equals_execution(self) -> bool:
        return self.feature_price_mode == self.execution_price_mode

    def to_payload(self) -> dict[str, object]:
        return {
            "feature_price_mode": self.feature_price_mode,
            "execution_price_mode": self.execution_price_mode,
            "feature_equals_execution": self.feature_equals_execution,
            "dividend_treatment": self.dividend_treatment,
            "execution_uncertain": self.execution_uncertain,
            "execution_uncertain_reason": self.execution_uncertain_reason,
            "policy": "feature_may_be_adjusted_execution_must_be_raw",
        }


def resolve_price_contract(config: StockAnalyzerConfig) -> PriceContract:
    """从配置解析价格口径（缺省：特征 qfq / 成交 raw）。"""
    feature_mode = _normalize_mode(
        getattr(config.data_source, "vendor_zip_price_series_mode", ""),
        default=FEATURE_PRICE_MODE_QFQ,
    )
    execution_mode = _normalize_mode(
        getattr(config.evolution.execution_spec, "price_series_mode", ""),
        default=EXECUTION_PRICE_MODE_RAW,
    )
    dividend_treatment = str(
        getattr(config.evolution.execution_spec, "dividend_treatment", "") or ""
    ).strip()
    uncertain = execution_mode != EXECUTION_PRICE_MODE_RAW
    return PriceContract(
        feature_price_mode=feature_mode,
        execution_price_mode=execution_mode,
        dividend_treatment=dividend_treatment,
        execution_uncertain=uncertain,
        execution_uncertain_reason=(
            f"execution_price_mode={execution_mode} 不是 raw：复权价不得当作当天可成交价"
            if uncertain
            else ""
        ),
    )


def _normalize_mode(value: object, *, default: str) -> str:
    text = str(value or "").strip().lower()
    if text in {FEATURE_PRICE_MODE_QFQ, FEATURE_PRICE_MODE_RAW}:
        return text
    # 未配置/无法识别时按默认值，并由报告如实写出——不静默当作 raw。
    return default


__all__ = [
    "EXECUTION_PRICE_MODE_RAW",
    "FEATURE_PRICE_MODE_QFQ",
    "FEATURE_PRICE_MODE_RAW",
    "PriceContract",
    "resolve_price_contract",
]
