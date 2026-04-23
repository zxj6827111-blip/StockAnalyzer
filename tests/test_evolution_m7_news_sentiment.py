from __future__ import annotations

from stock_analyzer.evolution.modules.m7_news_sentiment import evaluate_m7_news_sentiment


def test_m7_news_sentiment_budget_capped_with_limited_daily_budget() -> None:
    result = evaluate_m7_news_sentiment(
        records=[
            {
                "event_id": "n1",
                "symbol": "600000.SH",
                "headline": "券商板块走强，成交放量",
                "sentiment": 0.90,
                "cost": 0.20,
            },
            {
                "event_id": "n2",
                "symbol": "600000.SH",
                "headline": "券商板块走强，成交放量",
                "sentiment": 0.88,
                "cost": 0.20,
            },
            {
                "event_id": "n3",
                "symbol": "000001.SZ",
                "headline": "地产政策预期升温",
                "sentiment": 0.75,
                "cost": 0.20,
            },
            {
                "event_id": "n4",
                "symbol": "300001.SZ",
                "headline": "海外风险上升压制偏好",
                "sentiment": -0.70,
                "cost": 0.20,
            },
        ],
        daily_budget=0.40,
        default_event_cost=0.20,
    )
    assert result.status == "budget_capped"
    assert result.metrics.valid_events >= 3
    assert result.metrics.dropped_by_budget >= 1
    assert result.metrics.budget_utilization <= 1.0 + 1e-9


def test_m7_news_sentiment_returns_no_data_without_news_fields() -> None:
    result = evaluate_m7_news_sentiment(
        records=[
            {"symbol": "600000.SH", "open": 10.0, "close": 10.2},
            {"symbol": "000001.SZ", "open": 8.0, "close": 7.9},
        ]
    )
    assert result.status == "no_data"
    assert result.score == 50.0
    assert result.metrics.valid_events == 0


def test_m7_news_sentiment_generates_bge_m3_hash_embeddings_by_default() -> None:
    result = evaluate_m7_news_sentiment(
        records=[
            {"event_id": "n1", "symbol": "600000.SH", "headline": "券商板块走强", "sentiment": 0.8},
            {
                "event_id": "n2",
                "symbol": "600000.SH",
                "headline": "券商板块继续走强",
                "sentiment": 0.7,
            },
            {
                "event_id": "n3",
                "symbol": "000001.SZ",
                "headline": "地产政策边际改善",
                "sentiment": 0.5,
            },
        ],
        daily_budget=10.0,
        default_event_cost=0.2,
    )
    assert result.metrics.embedding_backend == "bge_m3_hash"
    assert result.metrics.embedding_events >= 1
    assert result.metrics.embedding_coverage_ratio > 0.0


def test_m7_news_sentiment_can_require_embeddings() -> None:
    result = evaluate_m7_news_sentiment(
        records=[
            {
                "event_id": "n1",
                "symbol": "600000.SH",
                "headline": "only record embedding",
                "sentiment": 0.8,
                "embedding": [1.0, 0.0, 0.0],
            },
            {"event_id": "n2", "symbol": "600000.SH", "headline": "", "sentiment": 0.6},
        ],
        embedding_backend="record_only",
        embedding_required=True,
    )
    assert result.metrics.valid_events == 1
    assert result.metrics.provided_embeddings == 1
