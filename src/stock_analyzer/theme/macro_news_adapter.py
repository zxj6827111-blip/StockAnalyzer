"""akshare 宏观快讯抓取 adapter（照 ``AkshareFinancialAdapter`` 模板）。

akshare 宏观新闻接口只有实时、无历史（财联社电报/东财全球快讯等），因此
主题线只能前瞻验证，shadow 期（4-8 周）是必要的硬成本。adapter 支持注入
``ak_module`` 便于测试（``sys.modules["akshare"]`` fake），TTL 缓存避免
调度重复触发时重复打网络，失败抛 :class:`DataSourceError`。

provider 选型：默认 ``cls``（财联社电报，逐条结构最干净），降级 ``em``
（东财全球财经快讯）。两者都返回 DataFrame，列名以中文为主（同时兼容
英文列名），适配器统一归一化为 ``{id, title, content, published_at, source, url}``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import time
from typing import cast

import pandas as pd

from stock_analyzer.data.provider import DataSourceError

_VALID_PROVIDERS = frozenset({"cls", "em"})

# 主源失败时的备选源映射（方案风险项 1：不可用时降级东财备选接口）
_FALLBACK_PROVIDER = {"cls": "em", "em": "cls"}

# 财联社电报 stock_info_global_cls 列名（新老版本兼容）
_CLS_TITLE_KEYS = ("标题", "title", "新闻标题")
_CLS_CONTENT_KEYS = ("内容", "content", "新闻内容")
_CLS_TIME_KEYS = ("发布日期", "发布时间", "时间", "datetime", "date", "publish_time")
_CLS_SOURCE_KEYS = ("来源", "source", "媒体", "media")
_CLS_URL_KEYS = ("链接", "url", "新闻链接")
_CLS_ID_KEYS = ("id", "编号", "news_id")

# 东财全球快讯 stock_info_global_em 列名
_EM_TITLE_KEYS = ("标题", "title", "新闻标题")
_EM_TIME_KEYS = ("发布时间", "时间", "datetime", "date", "publish_time")
_EM_URL_KEYS = ("链接", "url", "新闻链接")
_EM_SOURCE_KEYS = ("来源", "source")


@dataclass(slots=True)
class MacroNewsRecord:
    """归一化后的宏观快讯记录（theme 线的最小单位）。"""

    id: str = ""
    title: str = ""
    content: str = ""
    published_at: str = ""
    source: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "published_at": self.published_at,
            "source": self.source,
            "url": self.url,
        }


class MacroNewsAdapter:
    """抓取 akshare 宏观实时快讯，带 TTL 缓存与 provider 降级。"""

    def __init__(
        self,
        provider: str = "cls",
        cache_ttl_sec: int = 15 * 60,
        max_items: int = 200,
        ak_module: object | None = None,
    ) -> None:
        normalized_provider = str(provider).strip().lower() or "cls"
        self._provider = normalized_provider if normalized_provider in _VALID_PROVIDERS else "cls"
        self._cache_ttl_sec = max(30, int(cache_ttl_sec))
        self._max_items = max(1, int(max_items))
        self._ak_module = ak_module
        self._cache: tuple[float, list[MacroNewsRecord]] = (0.0, [])
        # 实际服务本次抓取的 provider（主源失败自动降级后与 _provider 不同）
        self.last_provider: str = self._provider

    @property
    def provider(self) -> str:
        return self._provider

    def fetch_latest(self, force_refresh: bool = False) -> list[MacroNewsRecord]:
        """返回最新的宏观快讯（TTL 内命中缓存）。

        接口只有实时数据（无历史），故本方法语义为"此刻可见的快讯"。
        主源（默认财联社 cls）抛 :class:`DataSourceError` 时自动降级备选源
        （东财 em），两个源都失败才抛 DataSourceError（方案风险项 1 缓解）。
        """
        now = time()
        if not force_refresh:
            cached_at, cached = self._cache
            if cached and now - cached_at <= self._cache_ttl_sec:
                return list(cached)

        ak = self._resolve_ak_module()
        if ak is None:
            raise DataSourceError("akshare module unavailable for macro news")
        errors: list[str] = []
        for provider in (self._provider, _FALLBACK_PROVIDER.get(self._provider, "em")):
            try:
                records = self._fetch_from_ak(ak=ak, provider=provider)
            except DataSourceError as exc:
                errors.append(f"{provider}: {exc}")
                continue
            self._cache = (now, records)
            self.last_provider = provider
            return list(records)
        raise DataSourceError(
            "macro news all providers failed: " + " | ".join(errors)[:400]
        )

    def _resolve_ak_module(self) -> object | None:
        if self._ak_module is not None:
            return self._ak_module
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except Exception:
            return None
        return cast(object, ak)

    def _fetch_from_ak(self, *, ak: object, provider: str) -> list[MacroNewsRecord]:
        func_name = "stock_info_global_cls" if provider == "cls" else "stock_info_global_em"
        func = getattr(ak, func_name, None)
        if not callable(func):
            raise DataSourceError(f"akshare.{func_name} not callable")
        try:
            frame = func()
        except Exception as exc:
            raise DataSourceError(
                f"akshare.{func_name} failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []

        records: list[MacroNewsRecord] = []
        for _, row in frame.head(self._max_items).iterrows():
            record = self._row_to_record(row=row, provider=provider)
            if record is None:
                continue
            records.append(record)
        return records

    def _row_to_record(self, *, row: pd.Series, provider: str) -> MacroNewsRecord | None:
        if provider == "cls":
            title = _row_first_text(row, _CLS_TITLE_KEYS)
            if not title:
                return None
            return MacroNewsRecord(
                id=_row_first_text(row, _CLS_ID_KEYS),
                title=title,
                content=_row_first_text(row, _CLS_CONTENT_KEYS),
                published_at=_row_first_text(row, _CLS_TIME_KEYS),
                source=_row_first_text(row, _CLS_SOURCE_KEYS),
                url=_row_first_text(row, _CLS_URL_KEYS),
            )
        title = _row_first_text(row, _EM_TITLE_KEYS)
        if not title:
            return None
        return MacroNewsRecord(
            id=_row_first_text(row, ("id", "编号")),
            title=title,
            content="",
            published_at=_row_first_text(row, _EM_TIME_KEYS),
            source=_row_first_text(row, _EM_SOURCE_KEYS),
            url=_row_first_text(row, _EM_URL_KEYS),
        )


def _row_first_text(row: pd.Series, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if pd.isna(value):
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


def _normalize_published_at(value: str) -> str:
    """把快讯时间统一成 ISO 字符串（解析失败原样返回）。"""
    text = str(value).strip()
    if not text:
        return ""
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.isoformat()
        except ValueError:
            continue
    return text
