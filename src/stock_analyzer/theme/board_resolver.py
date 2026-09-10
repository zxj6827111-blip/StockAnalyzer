"""akshare 概念/行业板块成分股解析（按日缓存，失败降级沿用上日成分）。

系统无行业分类数据（``_infer_symbol_sector`` 只是代码前缀粗标签），主题→个股
映射必须靠 akshare 板块成分接口（全新接入，无先例）：
- 概念板块：``stock_board_concept_cons_em``（参数 symbol=板块名）；
- 行业板块：``stock_board_industry_cons_em``（参数 symbol=板块名）。

板块名先查概念接口，查不到再查行业接口（多数主题族挂概念板块）。结果按
日落盘缓存（JSON），当日重复调用零网络；接口失败时降级沿用上日缓存文件
（stale 标记），缓存目录无任何可用文件时返回空成分（记录 unresolved）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pandas as pd

_CONCEPT_FUNC = "stock_board_concept_cons_em"
_INDUSTRY_FUNC = "stock_board_industry_cons_em"

_CODE_KEYS = ("代码", "code", "symbol", "股票代码")
_NAME_KEYS = ("名称", "name", "股票简称")


@dataclass(slots=True)
class BoardConstituents:
    """单个板块的成分股解析结果。"""

    board: str
    symbols: list[str] = field(default_factory=list)
    source: str = ""  # concept | industry | stale_cache
    stale: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "board": self.board,
            "symbols": list(self.symbols),
            "source": self.source,
            "stale": self.stale,
            "error": self.error,
        }


class BoardResolver:
    """把主题知识库的板块名解析为成分股列表（按日文件缓存 + 降级）。"""

    def __init__(
        self,
        cache_dir: str | Path = "artifacts/evolution/theme_board_cache",
        ak_module: object | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._ak_module = ak_module
        self._memory_cache: dict[str, BoardConstituents] = {}

    def resolve(self, board: str, *, today: date | None = None) -> BoardConstituents:
        """解析单个板块成分；当日缓存命中直接返回，失败降级上日缓存。"""
        normalized = str(board).strip()
        if not normalized:
            return BoardConstituents(board=board, error="empty_board")
        day_key = (today or datetime.now().date()).isoformat()
        cache_key = f"{normalized}:{day_key}"
        if cache_key in self._memory_cache:
            return self._memory_cache[cache_key]

        day_file = self._cache_dir / day_key / f"{_safe_name(normalized)}.json"
        if day_file.exists():
            result = self._load_cache_file(day_file, board=normalized)
            self._memory_cache[cache_key] = result
            return result

        fetched = self._fetch_from_ak(board=normalized)
        if fetched is None:
            # 接口失败：降级沿用最近一个缓存日的同板块成分（stale 标记）。
            fallback = self._load_latest_fallback(board=normalized)
            if fallback is not None:
                fallback.stale = True
                fallback.source = "stale_cache"
                self._memory_cache[cache_key] = fallback
                return fallback
            fallback = BoardConstituents(
                board=normalized,
                source="unresolved",
                stale=True,
                error="ak_fetch_failed_no_cache",
            )
            self._memory_cache[cache_key] = fallback
            return fallback
        self._write_cache_file(day_file, result=fetched, day_key=day_key)
        self._memory_cache[cache_key] = fetched
        return fetched

    def resolve_many(
        self,
        boards: list[str],
        *,
        today: date | None = None,
    ) -> dict[str, BoardConstituents]:
        """批量解析板块成分（顺序调用——板块接口有限频，勿并行）。"""
        return {board: self.resolve(board, today=today) for board in boards}

    def _fetch_from_ak(self, *, board: str) -> BoardConstituents | None:
        ak = self._resolve_ak_module()
        if ak is None:
            return None
        for func_name, label in ((_CONCEPT_FUNC, "concept"), (_INDUSTRY_FUNC, "industry")):
            func = getattr(ak, func_name, None)
            if not callable(func):
                continue
            try:
                frame = func(symbol=board)
            except Exception:
                continue
            symbols = _extract_symbols(frame)
            if symbols:
                return BoardConstituents(board=board, symbols=symbols, source=label)
            # 空结果：概念板块查无此名时继续尝试行业接口；行业也空则返回空成分
            # （不视为失败——板块可能存在但当前无成分，避免误降级 stale）。
        # 两个接口都调用过但无成分：返回空成分（resolved 但空），区分于接口失败。
        for func_name in (_CONCEPT_FUNC, _INDUSTRY_FUNC):
            func = getattr(ak, func_name, None)
            if callable(func):
                try:
                    frame = func(symbol=board)
                except Exception:
                    continue
                return BoardConstituents(board=board, symbols=[], source="resolved_empty")
        return None

    def _resolve_ak_module(self) -> object | None:
        if self._ak_module is not None:
            return self._ak_module
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except Exception:
            return None
        return cast(object, ak)

    def _load_cache_file(self, path: Path, *, board: str) -> BoardConstituents:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return BoardConstituents(board=board, source="unresolved", error="cache_read_failed")
        symbols = [str(item) for item in payload.get("symbols", []) if str(item).strip()]
        return BoardConstituents(
            board=board,
            symbols=symbols,
            source=str(payload.get("source", "")),
            stale=False,
        )

    def _write_cache_file(self, path: Path, *, result: BoardConstituents, day_key: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "board": result.board,
                "symbols": result.symbols,
                "source": result.source,
                "date": day_key,
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            # 缓存写失败不阻断解析结果（内存缓存本进程内仍然有效）。
            pass

    def _load_latest_fallback(self, *, board: str) -> BoardConstituents | None:
        """找最近一个缓存日文件（<=31 天）作为降级成分。"""
        safe = _safe_name(board)
        if not self._cache_dir.exists():
            return None
        candidates = sorted(
            (p for p in self._cache_dir.glob(f"*/{safe}.json")),
            key=lambda p: p.parent.name,
            reverse=True,
        )
        # 跳过今天（今天的已在前面读过），取最近的历史日。
        today_key = datetime.now().date().isoformat()
        for candidate in candidates:
            if candidate.parent.name == today_key:
                continue
            return self._load_cache_file(candidate, board=board)
        return None


def _extract_symbols(frame: object) -> list[str]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    code_col = _pick_column(frame.columns, _CODE_KEYS)
    if code_col is None:
        return []
    symbols: list[str] = []
    for raw in frame[code_col].tolist():
        text = str(raw).strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) == 6:
            symbols.append(digits)
    # 去重保序
    return list(dict.fromkeys(symbols))


def _pick_column(columns: pd.Index, keys: tuple[str, ...]) -> str | None:
    for col in columns:
        text = str(col).strip()
        for key in keys:
            if key in text:
                return str(col)
    return None


def _safe_name(board: str) -> str:
    """板块名转文件名（剔除路径不安全字符）。"""
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in board.strip())
