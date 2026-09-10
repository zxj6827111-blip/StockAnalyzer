"""主题事件规则抽取（一期纯关键词，LLM 抽取后置 Phase 4）。

从宏观快讯文本中匹配主题族关键词，产出
``{event_type, theme_id, direction, intensity, confidence}``。
纯函数实现（无 I/O），便于内联输入测试与后续 LLM 抽取的对照评估。

强度（intensity）按命中特征分层：
- 高（1.0）：标题命中关键词（标题权重高于正文）；
- 中（0.7）：正文命中 ≥2 个不同关键词（多关键词交叉佐证）；
- 低（0.4）：正文命中 1 个关键词。

置信度（confidence）= 强度 × 关键词覆盖度，夹在 [0, 1]。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from stock_analyzer.theme.taxonomy import ThemeTaxonomy

# 标题命中（最高强度）
_INTENSITY_TITLE = 1.0
# 正文多关键词命中
_INTENSITY_MULTI = 0.7
# 正文单关键词命中
_INTENSITY_SINGLE = 0.4


@dataclass(slots=True)
class ThemeEventExtraction:
    """一条快讯命中某主题族的抽取结果。"""

    theme_id: str
    event_type: str
    direction: int
    intensity: float
    confidence: float
    matched_keywords: list[str] = field(default_factory=list)
    title: str = ""
    published_at: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "theme_id": self.theme_id,
            "event_type": self.event_type,
            "direction": self.direction,
            "intensity": round(self.intensity, 4),
            "confidence": round(self.confidence, 4),
            "matched_keywords": list(self.matched_keywords),
            "title": self.title,
            "published_at": self.published_at,
            "source": self.source,
        }


def extract_theme_events(
    *,
    taxonomy: ThemeTaxonomy,
    title: str,
    content: str = "",
    published_at: str = "",
    source: str = "",
) -> list[ThemeEventExtraction]:
    """对单条快讯做关键词规则抽取（一条快讯可命中多个主题族）。"""
    normalized_title = str(title or "").strip()
    normalized_content = str(content or "").strip()
    if not normalized_title and not normalized_content:
        return []

    extractions: list[ThemeEventExtraction] = []
    for theme in taxonomy.themes:
        title_hits = [kw for kw in theme.event_keywords if kw and kw in normalized_title]
        content_hits = [kw for kw in theme.event_keywords if kw and kw in normalized_content]
        if not title_hits and not content_hits:
            continue
        if title_hits:
            intensity = _INTENSITY_TITLE
            matched = list(dict.fromkeys([*title_hits, *content_hits]))
        elif len(content_hits) >= 2:
            intensity = _INTENSITY_MULTI
            matched = list(content_hits)
        else:
            intensity = _INTENSITY_SINGLE
            matched = list(content_hits)
        coverage = min(1.0, len(matched) / max(1, len(theme.event_keywords)))
        confidence = min(1.0, intensity * (0.6 + 0.4 * coverage))
        extractions.append(
            ThemeEventExtraction(
                theme_id=theme.theme_id,
                event_type=theme.event_type,
                direction=theme.direction,
                intensity=intensity,
                confidence=confidence,
                matched_keywords=matched,
                title=normalized_title,
                published_at=published_at,
                source=source,
            )
        )
    return extractions


def extract_theme_events_from_records(
    *,
    taxonomy: ThemeTaxonomy,
    records: Sequence[Mapping[str, object]],
) -> list[ThemeEventExtraction]:
    """对一批归一化快讯记录（MacroNewsRecord.to_dict 形态）批量抽取。"""
    extractions: list[ThemeEventExtraction] = []
    for record in records:
        title = str(record.get("title", "") or "").strip()
        content = str(record.get("content", "") or "").strip()
        if not title and not content:
            continue
        extractions.extend(
            extract_theme_events(
                taxonomy=taxonomy,
                title=title,
                content=content,
                published_at=str(record.get("published_at", "") or ""),
                source=str(record.get("source", "") or ""),
            )
        )
    return extractions


def group_by_theme(
    extractions: Sequence[ThemeEventExtraction],
) -> dict[str, list[ThemeEventExtraction]]:
    """按 theme_id 聚合抽取结果（主题热度统计的输入）。"""
    grouped: dict[str, list[ThemeEventExtraction]] = {}
    for extraction in extractions:
        grouped.setdefault(extraction.theme_id, []).append(extraction)
    return grouped


def theme_heat(extractions: Sequence[ThemeEventExtraction]) -> float:
    """主题热度：去重快讯数驱动的 0-1 分（对数压缩 + 饱和）。

    1 条快讯 → 0.4，2 条 → 0.6，4 条 → 0.8，8 条及以上 → 1.0。
    去重按标题（同一事件多家转载只算一条热度）。
    """
    unique_titles = {extraction.title for extraction in extractions if extraction.title}
    count = len(unique_titles)
    if count <= 0:
        return 0.0
    if count == 1:
        return 0.4
    # log2 压缩：2→0.6, 4→0.8, 8→1.0（饱和封顶）
    return min(1.0, 0.6 + 0.2 * (count.bit_length() - 2))
