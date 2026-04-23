from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from stock_analyzer.news.provider import ArtifactNewsSignalProvider


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def test_artifact_news_provider_returns_fallback_when_missing() -> None:
    provider = ArtifactNewsSignalProvider(path="artifacts/evolution/inputs/not-found.jsonl")
    score = provider.score(
        symbol="600000",
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        strategy="trend",
    )
    assert score == 0.5


def test_artifact_news_provider_prefers_symbol_specific_sentiment(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":1.0}',
            '{"symbol":"000001","sentiment":-1.0}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    score_sh = provider.score(
        symbol="600000.SH",
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        strategy="trend",
    )
    score_sz = provider.score(
        symbol="000001.SZ",
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        strategy="trend",
    )
    assert score_sh > 0.9
    assert score_sz < 0.1


def test_artifact_news_provider_uses_market_average_when_symbol_missing(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"000001","sentiment":0.2}',
            '{"symbol":"000002","sentiment":0.2}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    score = provider.score(
        symbol="600000",
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        strategy="trend",
    )
    assert 0.59 <= score <= 0.61


def test_artifact_news_provider_confidence_weighting_biases_result(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":1.0,"llm_confidence":1.0}',
            '{"symbol":"600000","sentiment":-1.0,"llm_confidence":0.1}',
        ],
    )
    provider = ArtifactNewsSignalProvider(
        path=records_path,
        confidence_floor=0.0,
    )
    score = provider.score(
        symbol="600000",
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        strategy="trend",
    )
    assert score > 0.8


def test_artifact_news_provider_filters_stale_news(tmp_path: Path) -> None:
    now = datetime(2026, 3, 5, 12, 0, 0, tzinfo=UTC)
    recent = (now - timedelta(days=1)).isoformat()
    stale = (now - timedelta(days=10)).isoformat()
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            f'{{"symbol":"600000","sentiment":1.0,"event_time":"{stale}"}}',
            f'{{"symbol":"600000","sentiment":-1.0,"event_time":"{recent}"}}',
        ],
    )
    provider = ArtifactNewsSignalProvider(
        path=records_path,
        max_age_days=3,
        now_func=lambda: now,
    )
    score = provider.score(
        symbol="600000",
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        strategy="trend",
    )
    assert score < 0.1
