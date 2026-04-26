"""Date-versioned A-share limit and stamp-tax rule helpers."""

from __future__ import annotations

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
    if not config.cost_schedule_by_date:
        return default_rate
    day = _to_date(trade_date)
    selected_rate = default_rate
    selected_from = date.min
    for row in config.cost_schedule_by_date:
        from_day = _parse_iso_date(row.from_date)
        if from_day is None:
            continue
        if day is not None and from_day > day:
            continue
        if from_day >= selected_from:
            selected_from = from_day
            selected_rate = float(row.stamp_tax_rate)
    return selected_rate


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
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
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
