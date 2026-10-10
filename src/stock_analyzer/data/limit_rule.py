"""Date-versioned A-share limit-price and trading-cost rule helpers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from stock_analyzer.config import LimitRuleConfig


@dataclass(slots=True)
class PriceLimits:
    up_limit: float | None
    down_limit: float | None
    limit_pct: float | None
    source: str


def build_price_limits(
    bar: dict[str, Any],
    config: LimitRuleConfig,
) -> PriceLimits:
    source_up = _optional_float(bar.get("up_limit"))
    source_down = _optional_float(bar.get("down_limit"))
    if config.use_source_first and source_up is not None and source_down is not None:
        return PriceLimits(
            up_limit=source_up,
            down_limit=source_down,
            limit_pct=None,
            source="source",
        )

    if not config.fallback_by_board:
        return PriceLimits(
            up_limit=source_up,
            down_limit=source_down,
            limit_pct=None,
            source="none",
        )

    close = _optional_float(bar.get("close")) or 0.0
    pre_close = _optional_float(bar.get("pre_close"))
    if pre_close is None or pre_close <= 0:
        pct_change = _optional_float(bar.get("pct_change"))
        if pct_change is not None and abs(pct_change) < 0.95:
            base = 1.0 + pct_change
            if abs(base) > 1e-9:
                pre_close = close / base

    if pre_close is None or pre_close <= 0:
        return PriceLimits(
            up_limit=source_up,
            down_limit=source_down,
            limit_pct=None,
            source="none",
        )

    trade_date = _parse_trade_date(bar.get("trade_date") or bar.get("date"))
    board = _normalize_board(value=bar.get("board"), symbol=bar.get("symbol"))
    is_st = bool(bar.get("is_st", False)) or _contains_st(bar.get("name"))
    listing_days = _optional_int(bar.get("listing_days"))
    limit_pct = resolve_limit_pct(
        config=config,
        trade_date=trade_date,
        board=board,
        is_st=is_st,
        listing_days=listing_days,
    )
    if limit_pct is None:
        return PriceLimits(up_limit=None, down_limit=None, limit_pct=None, source="no_limit")
    return PriceLimits(
        up_limit=pre_close * (1.0 + limit_pct),
        down_limit=pre_close * (1.0 - limit_pct),
        limit_pct=limit_pct,
        source="fallback",
    )


def resolve_limit_pct(
    config: LimitRuleConfig,
    trade_date: date | None,
    board: str,
    is_st: bool,
    listing_days: int | None,
) -> float | None:
    if is_st:
        st_pct = _schedule_pct(config=config, board="ST", trade_date=trade_date)
        return st_pct if st_pct is not None else 0.05

    board_name = board.strip() or "主板"
    pct = _schedule_pct(config=config, board=board_name, trade_date=trade_date)
    if pct is None:
        pct = _fallback_pct(board_name)

    no_limit_days = _schedule_ipo_days(config=config, board=board_name, trade_date=trade_date)
    if no_limit_days > 0 and listing_days is not None and listing_days <= no_limit_days:
        return None
    return pct


def resolve_stamp_tax_rate(
    config: LimitRuleConfig,
    trade_date: date | datetime | None,
    default_rate: float,
) -> float:
    return resolve_cost_profile(
        limit_rule=config,
        matcher=None,
        trade_date=trade_date,
        static_defaults={"stamp_tax_rate": float(default_rate)},
    ).stamp_tax_rate


@dataclass(frozen=True)
class FrozenCostProfile:
    """某一交易日生效的冻结成本口径。

    ``overridden`` 记录哪些字段真的来自 ``limit_rule.cost_schedule_by_date``，
    其余沿用 ``backtest_matcher`` 的静态值。留档必须带上这个集合，否则事后无法
    判断某笔收益是按冻结历史成本算的、还是按当前静态成本补的。
    """

    commission_rate: float
    min_commission_per_order: float
    transfer_fee_rate: float
    stamp_tax_rate: float
    slippage_ratio: float | None
    effective_from: date | None
    overridden: frozenset[str] = frozenset()

    @property
    def source(self) -> str:
        return "cost_schedule" if self.overridden else "static_matcher"


_COST_FIELDS = (
    "commission_rate",
    "min_commission_per_order",
    "transfer_fee_rate",
    "stamp_tax_rate",
    "slippage_ratio",
)


def resolve_cost_profile(
    *,
    limit_rule: LimitRuleConfig | None,
    matcher: Any | None,
    trade_date: date | datetime | None,
    static_defaults: Mapping[str, Any] | None = None,
) -> FrozenCostProfile:
    """按日期把冻结成本表与静态值合并；表里没覆盖的字段取静态值。

    选取规则与涨跌停规则表一致：取 ``from <= trade_date`` 中 ``from`` 最大的那一档。
    无法解析的 ``from`` 直接跳过（不把坏数据当成 0 费率）。
    """
    statics = dict(static_defaults or {})
    if matcher is not None:
        for field_name in _COST_FIELDS:
            if field_name in statics:
                continue
            value = getattr(matcher, field_name, None)
            if value is not None:
                statics[field_name] = value

    resolved = {
        "commission_rate": float(statics.get("commission_rate", 0.0003)),
        "min_commission_per_order": float(statics.get("min_commission_per_order", 5.0)),
        "transfer_fee_rate": float(statics.get("transfer_fee_rate", 0.00001)),
        "stamp_tax_rate": float(statics.get("stamp_tax_rate", 0.0005)),
        "slippage_ratio": (
            float(statics["slippage_ratio"])
            if statics.get("slippage_ratio") is not None
            else None
        ),
    }
    day = _to_date(trade_date)
    schedule = list(getattr(limit_rule, "cost_schedule_by_date", []) or [])
    selected_row: Any | None = None
    selected_from: date | None = None
    for row in schedule:
        from_day = _parse_iso_date(row.from_date)
        if from_day is None:
            continue
        if day is not None and from_day > day:
            continue
        if selected_from is None or from_day >= selected_from:
            selected_from = from_day
            selected_row = row

    overridden: set[str] = set()
    if selected_row is not None:
        for field_name in _COST_FIELDS:
            value = getattr(selected_row, field_name, None)
            if value is None:
                continue
            resolved[field_name] = float(value)
            overridden.add(field_name)

    return FrozenCostProfile(
        commission_rate=resolved["commission_rate"],
        min_commission_per_order=resolved["min_commission_per_order"],
        transfer_fee_rate=resolved["transfer_fee_rate"],
        stamp_tax_rate=resolved["stamp_tax_rate"],
        slippage_ratio=resolved["slippage_ratio"],
        effective_from=selected_from,
        overridden=frozenset(overridden),
    )


def _schedule_pct(config: LimitRuleConfig, board: str, trade_date: date | None) -> float | None:
    selected: tuple[date, float | None] | None = None
    for row in config.rule_version_by_date:
        if _normalize_board(value=row.board) != _normalize_board(value=board):
            continue
        from_day = _parse_iso_date(row.from_date)
        if from_day is None:
            continue
        if trade_date is not None and from_day > trade_date:
            continue
        if selected is None or from_day >= selected[0]:
            selected = (from_day, row.limit_pct)
    if selected is None:
        return None
    return selected[1]


def _schedule_ipo_days(config: LimitRuleConfig, board: str, trade_date: date | None) -> int:
    selected: tuple[date, int] | None = None
    for row in config.rule_version_by_date:
        if _normalize_board(value=row.board) != _normalize_board(value=board):
            continue
        from_day = _parse_iso_date(row.from_date)
        if from_day is None:
            continue
        if trade_date is not None and from_day > trade_date:
            continue
        if selected is None or from_day >= selected[0]:
            selected = (from_day, max(0, int(row.ipo_no_limit_days)))
    return selected[1] if selected is not None else 0


def _fallback_pct(board: str) -> float:
    normalized = _normalize_board(value=board)
    if normalized == "北交所":
        return 0.30
    if normalized in {"科创板", "创业板"}:
        return 0.20
    return 0.10


def _normalize_board(value: object, symbol: object = "") -> str:
    raw = str(value or "").strip()
    if not raw:
        symbol_text = str(symbol or "").strip()
        if symbol_text.startswith("688"):
            return "科创板"
        if symbol_text.startswith("300") or symbol_text.startswith("301"):
            return "创业板"
        if symbol_text.startswith("8") or symbol_text.startswith("4"):
            return "北交所"
        return "主板"
    if raw in {"st", "ST"}:
        return "ST"
    if "科创" in raw:
        return "科创板"
    if "创业" in raw:
        return "创业板"
    if "北交" in raw:
        return "北交所"
    if raw.upper() == "ST":
        return "ST"
    return raw


def _parse_trade_date(value: object) -> date | None:
    if isinstance(value, date):
        return value if not isinstance(value, datetime) else value.date()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None
    return None


def _parse_iso_date(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _contains_st(value: object) -> bool:
    text = str(value or "").strip().upper()
    return text.startswith("ST") or "*ST" in text


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        # NaN/Inf 不是"有效涨跌停价"：返回 None 让调用方走 fail-closed 分支。
        # 此前 NaN 被当作"有 source 值"透传，而 NaN 比较恒 False → 涨停门静默放行
        # （DF-S02-002，2026-09-18 实测复现）。
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _to_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value
