"""M7 news-sentiment clustering, deduplication, and budget guard."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from pydantic import ValidationError

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field


class M7EmbeddingProvider(Protocol):
    """Embedding provider contract for M7 dedup clustering."""

    @property
    def backend(self) -> str:
        """Return backend name."""

    def embed(self, text: str) -> list[float] | None:
        """Build embedding for one text input."""


@dataclass(frozen=True, slots=True)
class BgeM3HashEmbeddingProvider:
    """Deterministic BGE-m3 compatible fallback embedding provider.

    This provider does not require external model dependencies. It provides
    stable hash-based vectors that preserve token overlap patterns and enables
    embedding-first dedup behavior in default deployments.
    """

    dim: int = 24
    _backend: str = "bge_m3_hash"

    @property
    def backend(self) -> str:
        return self._backend

    def embed(self, text: str) -> list[float] | None:
        tokens = _tokenize(text)
        if not tokens:
            return None
        dim = max(8, self.dim)
        vector: NDArray[np.float64] = np.zeros(dim, dtype=float)
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            primary = int.from_bytes(digest[:4], "big") % dim
            secondary = int.from_bytes(digest[4:8], "big") % dim
            sign = 1.0 if (digest[8] % 2 == 0) else -1.0
            scale = 1.0 + (digest[9] / 255.0) * 0.2
            vector[primary] += sign * scale
            vector[secondary] += sign * (0.5 * scale)
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            return None
        return [float(item) for item in (vector / norm).tolist()]


@dataclass(frozen=True, slots=True)
class M7NewsMetrics:
    """M7 summary metrics."""

    total_events: int
    valid_events: int
    unique_events: int
    deduplicated_events: int
    dropped_by_budget: int
    budget_spend: float
    budget_utilization: float
    mean_sentiment: float
    mean_abs_sentiment: float
    positive_ratio: float
    negative_ratio: float
    symbol_coverage: int
    embedding_events: int
    provided_embeddings: int
    generated_embeddings: int
    embedding_coverage_ratio: float
    embedding_backend: str


@dataclass(frozen=True, slots=True)
class M7ClusterSummary:
    """One deduplicated sentiment cluster summary."""

    cluster_id: int
    representative_headline: str
    symbols: list[str]
    members: int
    mean_sentiment: float
    cost: float


@dataclass(frozen=True, slots=True)
class M7NewsSentimentResult:
    """M7 output payload."""

    score: float
    status: str
    metrics: M7NewsMetrics
    clusters: list[M7ClusterSummary]


class _M7NewsEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str
    symbol: str
    headline: str = Field(min_length=1)
    sentiment: float = Field(ge=-1.0, le=1.0)
    cost: float = Field(gt=0.0)
    embedding: list[float] | None = None
    embedding_source: str = "none"


def evaluate_m7_news_sentiment(
    records: Sequence[Mapping[str, object]],
    dedup_similarity_threshold: float = 0.85,
    daily_budget: float = 15.0,
    default_event_cost: float = 0.20,
    sentiment_floor: float = 0.05,
    budget_warn_utilization: float = 0.80,
    max_clusters_in_report: int = 5,
    embedding_backend: str = "bge_m3_hash",
    embedding_dim: int = 24,
    embedding_required: bool = False,
) -> M7NewsSentimentResult:
    """Evaluate M7 news sentiment with deduplication and budget circuit breaker.

    Args:
        records: Raw candidate records.
        dedup_similarity_threshold: Similarity threshold for deduplicating events.
        daily_budget: Maximum daily cost budget.
        default_event_cost: Default per-event cost when cost field is missing.
        sentiment_floor: Minimum absolute sentiment used for quality checks.
        budget_warn_utilization: Utilization level after which status degrades to watch.
        max_clusters_in_report: Number of kept cluster summaries in payload.
        embedding_backend: Embedding backend selector. ``bge_m3_hash`` enables
            deterministic BGE-m3 compatible vectors.
        embedding_dim: Generated embedding dimension for hash backend.
        embedding_required: If ``True``, rows without embeddings are dropped.

    Returns:
        M7 result payload with score, status, metrics, and representative clusters.
    """
    if not 0.0 < dedup_similarity_threshold <= 1.0:
        raise ValueError("dedup_similarity_threshold must be in (0, 1]")
    if daily_budget <= 0.0:
        raise ValueError("daily_budget must be > 0")
    if default_event_cost <= 0.0:
        raise ValueError("default_event_cost must be > 0")
    if sentiment_floor < 0.0:
        raise ValueError("sentiment_floor must be >= 0")
    if not 0.0 <= budget_warn_utilization <= 1.0:
        raise ValueError("budget_warn_utilization must be in [0, 1]")
    if embedding_dim <= 0:
        raise ValueError("embedding_dim must be > 0")

    provider = _build_embedding_provider(backend=embedding_backend, dim=embedding_dim)

    total_events = len(records)
    events: list[_M7NewsEvent] = []
    for idx, record in enumerate(records):
        event = _parse_event(
            record=record,
            idx=idx,
            default_event_cost=default_event_cost,
            embedding_provider=provider,
            embedding_required=embedding_required,
        )
        if event is not None:
            events.append(event)

    valid_events = len(events)
    provided_embeddings = sum(1 for item in events if item.embedding_source == "record")
    generated_embeddings = sum(1 for item in events if item.embedding_source == "bge_m3_hash")
    embedding_events = provided_embeddings + generated_embeddings
    embedding_coverage_ratio = embedding_events / max(valid_events, 1)
    active_embedding_backend = provider.backend if provider is not None else "record_only"
    if valid_events == 0:
        metrics = M7NewsMetrics(
            total_events=total_events,
            valid_events=0,
            unique_events=0,
            deduplicated_events=0,
            dropped_by_budget=0,
            budget_spend=0.0,
            budget_utilization=0.0,
            mean_sentiment=0.0,
            mean_abs_sentiment=0.0,
            positive_ratio=0.0,
            negative_ratio=0.0,
            symbol_coverage=0,
            embedding_events=0,
            provided_embeddings=0,
            generated_embeddings=0,
            embedding_coverage_ratio=0.0,
            embedding_backend=active_embedding_backend,
        )
        return M7NewsSentimentResult(score=50.0, status="no_data", metrics=metrics, clusters=[])

    raw_clusters = _cluster_events(
        events=events,
        threshold=dedup_similarity_threshold,
    )
    deduplicated_events = valid_events - len(raw_clusters)
    kept_clusters, dropped_by_budget, budget_spend = _apply_budget(
        clusters=raw_clusters,
        daily_budget=daily_budget,
    )

    unique_events = len(kept_clusters)
    budget_utilization = budget_spend / max(daily_budget, 1e-9)
    mean_sentiment = _cluster_mean(kept_clusters, mode="mean")
    mean_abs_sentiment = _cluster_mean(kept_clusters, mode="abs_mean")
    positive_ratio = _cluster_ratio(kept_clusters, kind="positive")
    negative_ratio = _cluster_ratio(kept_clusters, kind="negative")
    symbol_coverage = len({event.symbol for cluster in kept_clusters for event in cluster})

    coverage = unique_events / max(len(raw_clusters), 1)
    sentiment_strength = min(1.0, mean_abs_sentiment / max(sentiment_floor, 1e-6))
    dedup_noise_ratio = deduplicated_events / max(valid_events, 1)
    dedup_quality = 1.0 - min(1.0, dedup_noise_ratio)
    budget_health = 1.0 - max(0.0, budget_utilization - 1.0)
    symbol_diversity = symbol_coverage / max(unique_events, 1)
    score = _clamp100(
        100.0
        * (
            0.28 * coverage
            + 0.22 * sentiment_strength
            + 0.20 * dedup_quality
            + 0.20 * budget_health
            + 0.10 * symbol_diversity
        )
    )

    if dropped_by_budget > 0:
        status = "budget_capped"
    elif unique_events == 0:
        status = "watch"
    elif mean_abs_sentiment < sentiment_floor * 0.5 and unique_events >= 3:
        status = "degraded"
    elif score >= 75.0 and budget_utilization <= budget_warn_utilization:
        status = "optimized"
    elif score < 60.0:
        status = "degraded"
    else:
        status = "watch"

    metrics = M7NewsMetrics(
        total_events=total_events,
        valid_events=valid_events,
        unique_events=unique_events,
        deduplicated_events=deduplicated_events,
        dropped_by_budget=dropped_by_budget,
        budget_spend=budget_spend,
        budget_utilization=budget_utilization,
        mean_sentiment=mean_sentiment,
        mean_abs_sentiment=mean_abs_sentiment,
        positive_ratio=positive_ratio,
        negative_ratio=negative_ratio,
        symbol_coverage=symbol_coverage,
        embedding_events=embedding_events,
        provided_embeddings=provided_embeddings,
        generated_embeddings=generated_embeddings,
        embedding_coverage_ratio=embedding_coverage_ratio,
        embedding_backend=active_embedding_backend,
    )
    clusters = _build_cluster_summaries(
        clusters=kept_clusters,
        max_clusters=max_clusters_in_report,
    )
    return M7NewsSentimentResult(score=score, status=status, metrics=metrics, clusters=clusters)


def _parse_event(
    record: Mapping[str, object],
    idx: int,
    default_event_cost: float,
    embedding_provider: M7EmbeddingProvider | None,
    embedding_required: bool,
) -> _M7NewsEvent | None:
    headline = _first_non_empty_str(
        record,
        keys=("headline", "title", "news_headline", "news_title", "news", "text"),
    )
    if headline is None:
        return None

    symbol = _first_non_empty_str(record, keys=("symbol", "code", "ticker")) or "UNKNOWN"
    event_id = _first_non_empty_str(record, keys=("event_id", "news_id", "id")) or f"evt_{idx}"
    sentiment = _first_float(
        record,
        keys=("sentiment", "news_sentiment", "llm_sentiment"),
        default=0.0,
    )
    cost = _first_float(
        record,
        keys=("cost", "event_cost", "token_cost"),
        default=default_event_cost,
    )
    embedding = _first_embedding(record, keys=("embedding", "vector"))
    embedding_source = "record" if embedding is not None else "none"
    if embedding is None and embedding_provider is not None:
        embedding = embedding_provider.embed(headline)
        if embedding is not None:
            embedding_source = embedding_provider.backend
    if embedding_required and embedding is None:
        return None

    payload = {
        "event_id": event_id,
        "symbol": symbol,
        "headline": headline,
        "sentiment": _clamp(sentiment, -1.0, 1.0),
        "cost": max(float(cost), 1e-6),
        "embedding": embedding,
        "embedding_source": embedding_source,
    }
    try:
        return _M7NewsEvent.model_validate(payload)
    except ValidationError:
        return None


def _cluster_events(events: Sequence[_M7NewsEvent], threshold: float) -> list[list[_M7NewsEvent]]:
    clusters: list[list[_M7NewsEvent]] = []
    representatives: list[_M7NewsEvent] = []
    for event in events:
        best_idx = -1
        best_similarity = -1.0
        for idx, representative in enumerate(representatives):
            similarity = _event_similarity(event, representative)
            if similarity > best_similarity:
                best_similarity = similarity
                best_idx = idx
        if best_idx >= 0 and best_similarity >= threshold:
            clusters[best_idx].append(event)
            continue
        representatives.append(event)
        clusters.append([event])
    return clusters


def _apply_budget(
    clusters: Sequence[Sequence[_M7NewsEvent]],
    daily_budget: float,
) -> tuple[list[list[_M7NewsEvent]], int, float]:
    ranked = sorted(
        clusters,
        key=lambda cluster: abs(
            float(np.mean(np.asarray([item.sentiment for item in cluster], dtype=float)))
        ),
        reverse=True,
    )
    kept: list[list[_M7NewsEvent]] = []
    spend = 0.0
    dropped = 0
    for cluster in ranked:
        cluster_cost = float(cluster[0].cost)
        if spend + cluster_cost > daily_budget + 1e-9:
            dropped += 1
            continue
        kept.append(list(cluster))
        spend += cluster_cost
    return kept, dropped, spend


def _build_cluster_summaries(
    clusters: Sequence[Sequence[_M7NewsEvent]],
    max_clusters: int,
) -> list[M7ClusterSummary]:
    capped = max(1, max_clusters)
    summaries: list[M7ClusterSummary] = []
    for idx, cluster in enumerate(clusters[:capped]):
        sentiments = np.asarray([item.sentiment for item in cluster], dtype=float)
        summaries.append(
            M7ClusterSummary(
                cluster_id=idx,
                representative_headline=cluster[0].headline,
                symbols=sorted({item.symbol for item in cluster}),
                members=len(cluster),
                mean_sentiment=float(np.mean(sentiments)),
                cost=float(cluster[0].cost),
            )
        )
    return summaries


def _cluster_mean(clusters: Sequence[Sequence[_M7NewsEvent]], mode: str) -> float:
    if not clusters:
        return 0.0
    values: list[float] = []
    for cluster in clusters:
        mean = float(np.mean(np.asarray([item.sentiment for item in cluster], dtype=float)))
        values.append(abs(mean) if mode == "abs_mean" else mean)
    return float(np.mean(np.asarray(values, dtype=float)))


def _cluster_ratio(clusters: Sequence[Sequence[_M7NewsEvent]], kind: str) -> float:
    if not clusters:
        return 0.0
    sentiments = [
        float(np.mean(np.asarray([item.sentiment for item in cluster], dtype=float)))
        for cluster in clusters
    ]
    if kind == "positive":
        return float(np.mean((np.asarray(sentiments, dtype=float) > 0).astype(float)))
    return float(np.mean((np.asarray(sentiments, dtype=float) < 0).astype(float)))


def _event_similarity(a: _M7NewsEvent, b: _M7NewsEvent) -> float:
    if a.event_id == b.event_id:
        return 1.0
    embedding_similarity = _embedding_similarity(a.embedding, b.embedding)
    if embedding_similarity is not None:
        return embedding_similarity
    return _headline_similarity(a.headline, b.headline)


def _embedding_similarity(a: list[float] | None, b: list[float] | None) -> float | None:
    if a is None or b is None or len(a) == 0 or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sqrt(sum(x * x for x in a))
    norm_b = sqrt(sum(y * y for y in b))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return None
    return dot / (norm_a * norm_b)


def _headline_similarity(a: str, b: str) -> float:
    tokens_a = set(_tokenize(a))
    tokens_b = set(_tokenize(b))
    if not tokens_a or not tokens_b:
        return 0.0
    intersect = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersect / max(union, 1)


def _tokenize(value: str) -> list[str]:
    normalized = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", value.lower()).strip()
    if not normalized:
        return []
    return [token for token in normalized.split() if token]


def _first_non_empty_str(record: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _first_float(record: Mapping[str, object], keys: Sequence[str], default: float) -> float:
    for key in keys:
        value = record.get(key)
        parsed = _as_float_or_none(value)
        if parsed is not None:
            return parsed
    return default


def _first_embedding(record: Mapping[str, object], keys: Sequence[str]) -> list[float] | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, list):
            embedding: list[float] = []
            valid = True
            for item in value:
                parsed = _as_float_or_none(item)
                if parsed is None:
                    valid = False
                    break
                embedding.append(parsed)
            if valid and embedding:
                return embedding
    return None


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _clamp100(value: float) -> float:
    return max(0.0, min(100.0, value))


def _build_embedding_provider(backend: str, dim: int) -> M7EmbeddingProvider | None:
    normalized = backend.strip().lower()
    if normalized in {"", "record_only", "none", "disabled"}:
        return None
    if normalized in {"bge_m3_hash", "bge-m3", "bge_m3"}:
        return BgeM3HashEmbeddingProvider(dim=dim)
    return BgeM3HashEmbeddingProvider(dim=dim)
