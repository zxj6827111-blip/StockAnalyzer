"""News score providers for runtime pipeline usage."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records


@dataclass(slots=True)
class NewsRiskDecision:
    """结构化新闻风险判定（PLAN P2-2）。

    单一 float 新闻分数升级为结构化决策：无个股证据时明确返回
    ``available_for_symbol=False``（score=None），不再回退全市场均值。
    ``hard_veto`` 仅在负面且置信度达标时为 True（LLM 不得单独执行硬否决，
    本字段只作证据输出，决策由 news_risk_mode 门消费）。
    """

    score: float | None
    available_for_symbol: bool
    hard_veto: bool
    max_negative_confidence: float
    matched_event_ids: list[str] = field(default_factory=list)
    freshness: dict[str, object] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


class ArtifactNewsSignalProvider:
    """Read latest M7 news artifacts and produce normalized component score."""

    def __init__(
        self,
        path: str | Path = "",
        fallback_score: float = 0.50,
        max_age_days: int = 3,
        half_life_hours: float = 24.0,
        confidence_floor: float = 0.25,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = _resolve_news_path(path)
        self._fallback_score = _clamp_01(fallback_score)
        self._max_age_days = max(0, int(max_age_days))
        self._half_life_hours = max(0.1, float(half_life_hours))
        self._confidence_floor = _clamp_01(confidence_floor)
        self._now_func = now_func if now_func is not None else _utc_now
        self._cached_mtime: float | None = None
        self._cached_records: list[dict[str, object]] = []

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = bars, features, strategy
        records = self._load_records()
        if not records:
            return self._fallback_score

        now = _ensure_utc(self._now_func())
        normalized_symbol = _normalize_symbol(symbol)
        symbol_scores: list[tuple[float, float]] = []
        for record in records:
            sentiment = _extract_sentiment(record)
            if sentiment is None:
                continue
            event_time = _extract_event_time(record)
            if _is_stale(
                event_time=event_time,
                now=now,
                max_age_days=self._max_age_days,
            ):
                continue
            score = _map_sentiment_to_score(sentiment)
            confidence = max(self._confidence_floor, _extract_confidence(record))
            recency_weight = _recency_weight(
                event_time=event_time,
                now=now,
                half_life_hours=self._half_life_hours,
            )
            weight = confidence * recency_weight
            if weight <= 0:
                continue
            symbols = _extract_symbols(record)
            if normalized_symbol and normalized_symbol in symbols:
                symbol_scores.append((score, weight))

        # PLAN P2-2：无个股证据时不再回退全市场均值——返回中性 fallback，
        # 结构化决策（news_risk）中明确标记 unavailable_for_symbol。
        if not symbol_scores:
            return self._fallback_score
        weighted_score = _weighted_mean(symbol_scores)
        if weighted_score is None:
            return self._fallback_score
        return _clamp_01(weighted_score)

    def news_risk(self, *, symbol: str) -> NewsRiskDecision:
        """结构化新闻风险判定：个股证据为准，无证据 → unavailable。"""
        records = self._load_records()
        now = _ensure_utc(self._now_func())
        normalized_symbol = _normalize_symbol(symbol)
        symbol_events: list[dict[str, object]] = []
        all_events: list[dict[str, object]] = []
        for record in records:
            event_time = _extract_event_time(record)
            if _is_stale(
                event_time=event_time,
                now=now,
                max_age_days=self._max_age_days,
            ):
                continue
            all_events.append(record)
            symbols = _extract_symbols(record)
            if normalized_symbol and normalized_symbol in symbols:
                symbol_events.append(record)

        if not symbol_events:
            return NewsRiskDecision(
                score=None,
                available_for_symbol=False,
                hard_veto=False,
                max_negative_confidence=0.0,
                matched_event_ids=[],
                freshness=_freshness_fields(
                    events=all_events,
                    now=now,
                    max_age_days=self._max_age_days,
                ),
                reasons=["no_symbol_news_evidence"],
            )

        scores: list[float] = []
        negative_confidences: list[float] = []
        event_ids: list[str] = []
        for record in symbol_events:
            sentiment = _extract_sentiment(record)
            if sentiment is None:
                continue
            event_id = str(
                record.get("event_id") or record.get("id") or ""
            ).strip()
            if event_id:
                event_ids.append(event_id)
            scores.append(_map_sentiment_to_score(sentiment))
            confidence = max(self._confidence_floor, _risk_confidence(record))
            if sentiment < 0:
                negative_confidences.append(confidence)
        if not scores:
            return NewsRiskDecision(
                score=None,
                available_for_symbol=False,
                hard_veto=False,
                max_negative_confidence=0.0,
                matched_event_ids=[],
                freshness=_freshness_fields(
                    events=all_events,
                    now=now,
                    max_age_days=self._max_age_days,
                ),
                reasons=["symbol_news_without_sentiment"],
            )
        weighted_pairs: list[tuple[float, float]] = []
        for record in symbol_events:
            sentiment_value = _extract_sentiment(record)
            if sentiment_value is None:
                continue
            event_time = _extract_event_time(record)
            recency = _recency_weight(
                event_time=event_time,
                now=now,
                half_life_hours=self._half_life_hours,
            )
            weighted_pairs.append(
                (
                    _map_sentiment_to_score(sentiment_value),
                    max(self._confidence_floor, _risk_confidence(record)) * recency,
                )
            )
        weighted = _weighted_mean(weighted_pairs)
        max_negative = max(negative_confidences) if negative_confidences else 0.0
        hard_veto = bool(negative_confidences) and max_negative >= 0.6
        reasons: list[str] = []
        if hard_veto:
            reasons.append("negative_high_confidence")
        elif negative_confidences:
            reasons.append("negative_news")
        return NewsRiskDecision(
            score=_clamp_01(weighted) if weighted is not None else None,
            available_for_symbol=True,
            hard_veto=hard_veto,
            max_negative_confidence=round(max_negative, 4),
            matched_event_ids=event_ids[:20],
            freshness=_freshness_fields(
                events=all_events,
                now=now,
                max_age_days=self._max_age_days,
            ),
            reasons=reasons,
        )

    def available(self, symbol: str = "") -> bool:
        """返回新闻数据是否可用（有非空记录且不是纯 fallback）。"""
        if symbol:
            decision = self.news_risk(symbol=symbol)
            return decision.available_for_symbol
        return bool(self._load_records())

    def _load_records(self) -> list[dict[str, object]]:
        try:
            stat = self._path.stat()
        except OSError:
            self._cached_mtime = None
            self._cached_records = []
            return []
        mtime = float(stat.st_mtime)
        if self._cached_mtime is not None and mtime == self._cached_mtime:
            return self._cached_records
        loaded = load_m7_news_records(path=self._path)
        self._cached_mtime = mtime
        self._cached_records = loaded
        return loaded


def _resolve_news_path(path: str | Path) -> Path:
    raw_path = str(path).strip()
    if not raw_path:
        return _project_root() / "artifacts" / "evolution" / "inputs" / "m7_news_latest.jsonl"
    target = Path(raw_path)
    if target.is_absolute():
        return target
    return _project_root() / target


def _freshness_fields(
    *,
    events: list[dict[str, object]],
    now: datetime,
    max_age_days: int,
) -> dict[str, object]:
    """新闻数据新鲜度：最新事件时间、事件数、数据窗口天数。"""
    event_times = [
        parsed
        for record in events
        if (parsed := _extract_event_time(record)) is not None
    ]
    if not event_times:
        return {"event_count": len(events), "latest_event_time": "", "window_days": 0}
    latest = max(event_times)
    window_days = max(0, (now - min(event_times)).days) if len(event_times) > 1 else 0
    return {
        "event_count": len(events),
        "latest_event_time": latest.isoformat(),
        "window_days": window_days,
        "max_age_days": max_age_days,
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _extract_sentiment(record: Mapping[str, Any]) -> float | None:
    for key in ("sentiment", "news_sentiment", "llm_sentiment"):
        value = _as_float(record.get(key))
        if value is not None:
            return max(-1.0, min(1.0, value))
    return None


def _extract_symbols(record: Mapping[str, Any]) -> set[str]:
    keys = ("symbol", "code", "ticker", "stock_code", "ts_code")
    symbols: set[str] = set()
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            normalized = _normalize_symbol(value)
            if normalized:
                symbols.add(normalized)
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    normalized = _normalize_symbol(item)
                    if normalized:
                        symbols.add(normalized)
    return symbols


def _extract_confidence(record: Mapping[str, Any]) -> float:
    for key in ("llm_confidence", "confidence", "probability", "weight"):
        value = _as_float(record.get(key))
        if value is not None:
            return _clamp_01(value)
    return 1.0


def _risk_confidence(record: Mapping[str, Any]) -> float:
    """结构化风险判定用的置信度：缺失 confidence 字段时取中性 0.5。

    ``_extract_confidence`` 的 1.0 默认是为旧权重路径设计（无标注按满权重
    参与加权）；但硬否决/强扣分门槛不能把"未标注"当成"满置信"——缺失
    置信度的负面新闻按 0.5 处理，避免误触发 hard_veto。
    """
    for key in ("llm_confidence", "confidence", "probability", "weight"):
        value = _as_float(record.get(key))
        if value is not None:
            return _clamp_01(value)
    return 0.5


def _extract_event_time(record: Mapping[str, Any]) -> datetime | None:
    for key in (
        "event_time",
        "published_at",
        "publish_time",
        "timestamp",
        "time",
        "created_at",
        "datetime",
        "date",
    ):
        parsed = _to_datetime(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _normalize_symbol(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    primary = text.split(".", maxsplit=1)[0]
    digits = "".join(ch for ch in primary if ch.isdigit())
    if len(digits) == 6:
        return digits
    return primary.upper()


def _map_sentiment_to_score(sentiment: float) -> float:
    return _clamp_01((sentiment + 1.0) / 2.0)


def _weighted_mean(items: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for _score, weight in items)
    if total_weight <= 0:
        return None
    weighted_sum = sum(score * weight for score, weight in items)
    return weighted_sum / total_weight


def _is_stale(event_time: datetime | None, now: datetime, max_age_days: int) -> bool:
    if event_time is None or max_age_days <= 0:
        return False
    return now - event_time > timedelta(days=max_age_days)


def _recency_weight(event_time: datetime | None, now: datetime, half_life_hours: float) -> float:
    if event_time is None:
        return 1.0
    age_seconds = max(0.0, (now - event_time).total_seconds())
    half_life_seconds = max(1.0, half_life_hours * 3600.0)
    return float(0.5 ** (age_seconds / half_life_seconds))


def _to_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, (int, float)):
        if not isfinite(float(value)):
            return None
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp = timestamp / 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            return _ensure_utc(datetime.fromisoformat(normalized))
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
            return parsed if isfinite(parsed) else None
        except ValueError:
            return None
    return None


def _clamp_01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
