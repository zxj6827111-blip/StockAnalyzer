"""板块成分股多源解析（按日缓存，全部源失败时降级沿用上日成分）。

背景（2026-09-10 NAS 实测）：东财 ``push2*.eastmoney.com`` 行情主机族被网络
策略切断，akshare 的 ``stock_board_concept_cons_em`` / ``stock_board_industry_cons_em``
全部抛 ConnectionError，主题→个股映射恒为 unresolved。tushare Pro 的同花顺板块
接口不受该策略影响，且是唯一同时覆盖"概念 + 行业"两类的免费源：

- 名录：``ths_index``（type=N 概念 / type=I 行业）→ 名称→``ts_code``；
- 成分：``ths_member``（N/I 代码均可用；实测 页岩气 51 只、火电 31 只）；
- 申万备源：``index_classify``（SW2021）→ ``index_member``。

板块名以 taxonomy 的 ``boards`` 为规范名（种子期按东财命名），跨源命名差异由
``ThemeDefinition.board_aliases`` 显式声明别名后逐名精确匹配——**不做模糊匹配**，
否则"国产芯片"会被猜成 900 只规模的泛概念板块，静默把主题稀释成噪声。

解析顺序：tushare 同花顺 → tushare 申万 → akshare 东财概念/行业 → 上日缓存
（stale 标记）→ 空成分（unresolved）。结果按日落盘（JSON），当日重复调用零网络。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

_CONCEPT_FUNC = "stock_board_concept_cons_em"
_INDUSTRY_FUNC = "stock_board_industry_cons_em"

_CODE_KEYS = ("代码", "code", "symbol", "股票代码")
_NAME_KEYS = ("名称", "name", "股票简称")
# tushare 成分帧必须显式指定成分代码列：ths_member 有 ts_code/con_code 两列、
# index_member 有 index_code/con_code 两列，按 _CODE_KEYS 的 "code" 泛匹配会
# 先命中指数自身的代码列，把"指数代码"当成成分股返回。
_MEMBER_CODE_KEYS = ("con_code",)

_CATALOGUE_DIR_NAME = "_catalogues"


@dataclass(slots=True)
class BoardConstituents:
    """单个板块的成分股解析结果。"""

    board: str
    symbols: list[str] = field(default_factory=list)
    source: str = ""  # ths_concept | ths_industry | sw_industry | concept | industry
    #                    | stale_cache | resolved_empty | unresolved
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
    """把主题知识库的板块名解析为成分股列表（多源链 + 按日文件缓存 + 降级）。"""

    def __init__(
        self,
        cache_dir: str | Path = "artifacts/evolution/theme_board_cache",
        ak_module: object | None = None,
        tushare_client: object | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._ak_module = ak_module
        # duck-typed：需提供 _call(api_name, **params) -> DataFrame（见
        # data.tushare_provider._HttpTushareProApi）。None 时跳过 tushare 源。
        self._tushare_client = tushare_client
        self._memory_cache: dict[str, BoardConstituents] = {}
        self._catalogue_cache: tuple[dict[str, tuple[str, str]], dict[str, str]] | None = None

    def resolve(
        self,
        board: str,
        *,
        today: date | None = None,
        aliases: Sequence[str] = (),
    ) -> BoardConstituents:
        """解析单个板块成分；当日缓存命中直接返回，失败降级上日缓存。

        ``aliases`` 为该板块名的跨源别名（同花顺/申万名录里的等价名），按
        [规范名, *别名] 顺序逐个精确匹配。
        """
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

        candidates = _candidate_names(normalized, aliases)
        fetched = self._fetch_from_tushare(board=normalized, candidates=candidates)
        if fetched is None:
            fetched = self._fetch_from_ak(board=normalized)
        if fetched is None:
            # 全部源失败：降级沿用最近一个缓存日的同板块成分（stale 标记）。
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
                error="all_sources_failed_no_cache",
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
        aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, BoardConstituents]:
        """批量解析板块成分（顺序调用——板块接口有限频，勿并行）。"""
        alias_map = aliases or {}
        return {
            board: self.resolve(board, today=today, aliases=alias_map.get(board, ()))
            for board in boards
        }

    # ------------------------------------------------------------------
    # tushare（同花顺名录/成分 + 申万名录/成分）
    # ------------------------------------------------------------------

    def _fetch_from_tushare(
        self,
        *,
        board: str,
        candidates: list[str],
    ) -> BoardConstituents | None:
        """按名录精确匹配解析成分。

        返回 None 表示"该板块在 tushare 名录里查无此名"（交由 akshare 源继续
        尝试）；返回空成分表示"名录命中但成分查询为空"（resolved_empty，不再
        降级 stale——板块确实存在，只是当前无成分或本次查询失败）。
        """
        if self._tushare_client is None:
            return None
        ths_map, sw_map = self._catalogues()
        if not ths_map and not sw_map:
            return None
        matched = False
        for candidate in candidates:
            entry = ths_map.get(candidate)
            if entry is not None:
                matched = True
                symbols = self._ths_member_symbols(entry[0])
                if symbols:
                    source = "ths_concept" if entry[1] == "N" else "ths_industry"
                    return BoardConstituents(board=board, symbols=symbols, source=source)
            index_code = sw_map.get(candidate)
            if index_code is not None:
                matched = True
                symbols = self._index_member_symbols(index_code)
                if symbols:
                    return BoardConstituents(board=board, symbols=symbols, source="sw_industry")
        if matched:
            return BoardConstituents(board=board, symbols=[], source="resolved_empty")
        return None

    def _ths_member_symbols(self, ts_code: str) -> list[str]:
        frame = _safe_tushare_call(
            self._tushare_client, "ths_member", ts_code=ts_code, fields="ts_code,con_code"
        )
        return _extract_symbols(frame, code_keys=_MEMBER_CODE_KEYS)

    def _index_member_symbols(self, index_code: str) -> list[str]:
        frame = _safe_tushare_call(self._tushare_client, "index_member", index_code=index_code)
        records = _frame_records(frame)
        if records and "is_new" in records[0]:
            # 历史成分表含已调出记录，只保留当前成分（is_new="Y"）。
            current = [
                row for row in records if str(row.get("is_new", "")).strip().upper() == "Y"
            ]
            return _symbols_from_records(current, code_keys=_MEMBER_CODE_KEYS)
        return _extract_symbols(frame, code_keys=_MEMBER_CODE_KEYS)

    def _catalogues(self) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        """(同花顺 名称→(ts_code, 类型), 申万 名称→index_code)。

        进程内缓存 + 按日落盘：名录一天内不变，避免每次解析都打三次 tushare。
        全空（token 缺失/无权限/网络失败）时不写盘，防止把空名录固化一天。
        """
        if self._catalogue_cache is not None:
            return self._catalogue_cache
        day_key = datetime.now().date().isoformat()
        path = self._cache_dir / _CATALOGUE_DIR_NAME / f"{day_key}.json"
        payload = _read_json(path)
        if payload is None:
            fetched = self._fetch_catalogues()
            if fetched["ths"] or fetched["sw"]:
                _write_json(path, fetched)
            payload = fetched
        ths = {
            str(name): (str(code), str(kind))
            for name, (code, kind) in (payload.get("ths") or {}).items()
        }
        sw = {str(name): str(code) for name, code in (payload.get("sw") or {}).items()}
        self._catalogue_cache = (ths, sw)
        return self._catalogue_cache

    def _fetch_catalogues(self) -> dict[str, Any]:
        ths: dict[str, list[str]] = {}
        for kind in ("N", "I"):
            for row in _frame_records(
                _safe_tushare_call(
                    self._tushare_client,
                    "ths_index",
                    exchange="A",
                    type=kind,
                    fields="ts_code,name",
                )
            ):
                code = str(row.get("ts_code", "")).strip()
                name = str(row.get("name", "")).strip()
                if code and name:
                    ths.setdefault(name, [code, kind])
        sw: dict[str, str] = {}
        for row in _frame_records(
            _safe_tushare_call(
                self._tushare_client,
                "index_classify",
                src="SW2021",
                fields="index_code,industry_name",
            )
        ):
            code = str(row.get("index_code", "")).strip()
            name = str(row.get("industry_name", "")).strip()
            if code and name:
                sw.setdefault(name, code)
        return {"ths": ths, "sw": sw}

    # ------------------------------------------------------------------
    # akshare（东财概念/行业）
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def _load_cache_file(self, path: Path, *, board: str) -> BoardConstituents:
        payload = _read_json(path)
        if payload is None:
            return BoardConstituents(board=board, source="unresolved", error="cache_read_failed")
        symbols = [str(item) for item in (payload.get("symbols") or []) if str(item).strip()]
        return BoardConstituents(
            board=board,
            symbols=symbols,
            source=str(payload.get("source", "")),
            stale=False,
        )

    def _write_cache_file(self, path: Path, *, result: BoardConstituents, day_key: str) -> None:
        _write_json(
            path,
            {
                "board": result.board,
                "symbols": result.symbols,
                "source": result.source,
                "date": day_key,
            },
        )

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


def _safe_tushare_call(client: object | None, api_name: str, **params: object) -> object | None:
    """调用 tushare 接口；任何异常（无权限/限频/网络）返回 None，由调用方退避。

    ``_call`` 是 ``_HttpTushareProApi`` 的既有私有入口，本模块与
    ``data.warehouse_enrichment`` 一致地复用它，避免为只读查询再造一层包装。
    """
    if client is None:
        return None
    call = getattr(client, "_call", None)
    if not callable(call):
        return None
    result: object | None
    try:
        result = call(api_name, **params)  # noqa: SLF001
    except Exception:
        return None
    return result


def _frame_records(frame: object) -> list[dict[str, object]]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return [{str(key): value for key, value in row.items()} for row in frame.to_dict("records")]


def _candidate_names(board: str, aliases: Sequence[str]) -> list[str]:
    """tushare 侧的候选名顺序：声明的别名在前，规范名兜底。

    规范名是东财命名，tushare 名录里本就不该有它；反过来，同花顺存在
    "同名不同粒度"的实体——``煤化工`` 既是行业（884281.TI，8 只）也是概念
    （885300 系，112 只），若按 [规范名, *别名] 顺序匹配，行业名胜出会把要
    表达的**概念**板块缩成 8 只（实测 2026-09-10）。因此把策展人显式声明的
    别名当作目标名（别名空则只剩规范名，行为不变），规范名仅在别名全部落空
    时兜底——它同时也是 akshare 东财侧的唯一候选名。
    去重保序，同名不重复请求。
    """
    ordered = [*[str(alias).strip() for alias in aliases], board]
    return list(dict.fromkeys(name for name in ordered if name))


def _extract_symbols(frame: object, code_keys: tuple[str, ...] = _CODE_KEYS) -> list[str]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    code_col = _pick_column(frame.columns, code_keys)
    if code_col is None:
        return []
    return _symbols_from_values(frame[code_col].tolist())


def _symbols_from_records(
    records: Sequence[Mapping[str, object]],
    *,
    code_keys: tuple[str, ...],
) -> list[str]:
    for key in code_keys:
        if records and key in records[0]:
            return _symbols_from_values([record.get(key) for record in records])
    return []


def _symbols_from_values(values: Sequence[object]) -> list[str]:
    symbols: list[str] = []
    for raw in values:
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


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: object) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError:
        # 缓存写失败不阻断解析结果（内存缓存本进程内仍然有效）。
        pass


def _safe_name(board: str) -> str:
    """板块名转文件名（剔除路径不安全字符）。"""
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in board.strip())
