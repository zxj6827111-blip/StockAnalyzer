"""Delisted A-share symbols support (P2-#27 phase 1).

幸存者偏差修复第一阶段：退市股名单（tushare ``stock_basic(list_status='D')``）
的获取与本地加载，供 universe 构建时排除已退市标的——当前 universe 完全基于
"当前上市"名单，历史回测会系统性遗漏退市股。

名单机制：
- 来源优先级：tushare ``stock_basic(list_status='D')``（token 可用时拉取并落盘）
  → 本地名单文件（预置/手工维护兜底）。
- 落盘结构（默认 ``artifacts/universe/delisted.json``）：:

    {
      "generated_at": "2026-08-10T12:00:00",
      "source": "tushare",
      "count": 2,
      "symbols": {
        "600002": {"name": "齐鲁石化", "delist_date": "2020-05-28"},
        "000003": {"name": "PT金田A", "delist_date": "2002-06-14"}
      }
    }

  兼容 ``symbols`` 为 ``[{symbol, name, delist_date}, ...]`` 列表形态。
- 加载失败（文件缺失 / JSON 损坏 / 结构异常）一律返回空集，不抛异常。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

DelistedSymbolInfo = dict[str, str]
DelistedSymbolMap = dict[str, DelistedSymbolInfo]

_SYMBOLS_KEY = "symbols"
_EMPTY_VALUES = frozenset({"", "nan", "none", "null", "nat", "na"})


def _normalize_symbol(value: object) -> str:
    digits = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    return digits if len(digits) == 6 else ""


def _normalize_delist_date(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return text


def _is_empty_value(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _EMPTY_VALUES


def _entry_from_parts(
    name: object,
    delist_date: object,
) -> DelistedSymbolInfo:
    info: DelistedSymbolInfo = {}
    if not _is_empty_value(name):
        info["name"] = str(name).strip()
    if not _is_empty_value(delist_date):
        info["delist_date"] = _normalize_delist_date(delist_date)
    return info


def _parse_delisted_payload(raw: dict[str, Any]) -> DelistedSymbolMap:
    """Parse either ``{symbol: {name, delist_date}}`` or list-of-dicts shapes."""
    parsed: DelistedSymbolMap = {}
    symbols = raw.get(_SYMBOLS_KEY)
    if isinstance(symbols, dict):
        for key, value in symbols.items():
            symbol = _normalize_symbol(key)
            if not symbol:
                continue
            if isinstance(value, dict):
                parsed[symbol] = _entry_from_parts(
                    value.get("name"),
                    value.get("delist_date"),
                )
            elif value is not None:
                parsed[symbol] = _entry_from_parts(value, None)
        return parsed
    if isinstance(symbols, list):
        for item in symbols:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_symbol(item.get("symbol"))
            if not symbol:
                continue
            parsed[symbol] = _entry_from_parts(
                item.get("name"),
                item.get("delist_date"),
            )
        return parsed
    return {}


def load_delisted_symbols(path: str | Path) -> DelistedSymbolMap:
    """Load the delisted-symbol map from ``path``.

    Missing file, invalid JSON, or an unexpected structure all yield an
    empty map and never raise.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return _parse_delisted_payload(raw)


def persist_delisted_symbols(
    path: str | Path,
    symbols: DelistedSymbolMap,
    *,
    source: str,
) -> None:
    """Persist the delisted-symbol map to ``path``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "source": str(source or "").strip() or "unknown",
        "count": len(symbols),
        _SYMBOLS_KEY: {symbol: info for symbol, info in sorted(symbols.items())},
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_delisted_frame(frame: pd.DataFrame) -> DelistedSymbolMap:
    ts_code_column = next(
        (column for column in ("ts_code", "symbol", "code") if column in frame.columns),
        "",
    )
    if not ts_code_column:
        return {}
    parsed: DelistedSymbolMap = {}
    for _, row in frame.iterrows():
        symbol = _normalize_symbol(row.get(ts_code_column))
        if not symbol:
            continue
        parsed[symbol] = _entry_from_parts(
            row.get("name"),
            row.get("delist_date"),
        )
    return parsed


def fetch_delisted_symbols_from_provider(
    provider: object,
    *,
    persist_path: str | Path | None = None,
) -> DelistedSymbolMap:
    """Fetch the delisted-symbol map from a tushare-capable provider.

    The provider must expose ``fetch_delisted_stock_basic`` returning a
    DataFrame with ``ts_code``/``name``/``delist_date`` columns. Any failure
    (capability missing, token missing, transport error, empty result)
    yields an empty map so callers can fall back to a local list file. When
    ``persist_path`` is given, a successful fetch is written there for
    offline reuse.
    """
    fetcher = getattr(provider, "fetch_delisted_stock_basic", None)
    if not callable(fetcher):
        return {}
    try:
        raw = fetcher()
    except Exception as exc:
        logger.warning("delisted_symbols fetch failed: %s: %s", type(exc).__name__, exc)
        return {}
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return {}
    parsed = _parse_delisted_frame(raw)
    if not parsed:
        return {}
    if persist_path is not None:
        try:
            persist_delisted_symbols(persist_path, parsed, source="tushare")
        except OSError as exc:
            logger.warning(
                "delisted_symbols persist failed: %s: %s",
                type(exc).__name__,
                exc,
            )
    return parsed
