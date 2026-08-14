from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

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


def test_artifact_news_provider_does_not_use_market_average_when_symbol_missing(
    tmp_path: Path,
) -> None:
    """PLAN P2-2：无个股证据时不再回退全市场均值——返回中性 fallback。"""
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
    # 无 600000 个股证据 → 中性 fallback（0.50），而非全市场均值 0.6
    assert score == 0.5


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


# ---------------------------------------------------------------------------
# P2-2 结构化新闻风险接口：NewsRiskDecision + 无个股证据 → unavailable
# ---------------------------------------------------------------------------


def test_news_risk_unavailable_without_symbol_evidence(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"000001","sentiment":-0.5,"llm_confidence":0.9}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    decision = provider.news_risk(symbol="600000")
    assert decision.available_for_symbol is False
    assert decision.score is None
    assert decision.hard_veto is False
    assert "no_symbol_news_evidence" in decision.reasons
    assert decision.matched_event_ids == []
    assert "latest_event_time" in decision.freshness


def test_news_risk_available_with_symbol_evidence(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":1.0,"llm_confidence":0.9,"event_id":"evt-1"}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    decision = provider.news_risk(symbol="600000.SH")
    assert decision.available_for_symbol is True
    assert decision.score is not None
    assert decision.score > 0.9
    assert decision.hard_veto is False
    assert decision.matched_event_ids == ["evt-1"]


def test_news_risk_hard_veto_on_negative_high_confidence(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":-1.0,"llm_confidence":0.85,"event_id":"evt-neg"}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    decision = provider.news_risk(symbol="600000")
    assert decision.available_for_symbol is True
    assert decision.hard_veto is True
    assert decision.max_negative_confidence == pytest.approx(0.85)
    assert "negative_high_confidence" in decision.reasons
    assert decision.score < 0.2


def test_news_risk_low_confidence_negative_no_veto(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":-0.6,"llm_confidence":0.3}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    decision = provider.news_risk(symbol="600000")
    assert decision.available_for_symbol is True
    assert decision.hard_veto is False
    assert decision.max_negative_confidence == pytest.approx(0.3)
    assert "negative_news" in decision.reasons


def test_news_risk_available_returns_false_for_other_symbol(tmp_path: Path) -> None:
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":0.5}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    assert provider.available(symbol="600000") is True
    assert provider.available(symbol="300001") is False


def test_news_risk_missing_confidence_does_not_hard_veto(tmp_path: Path) -> None:
    """缺失 confidence 字段的负面新闻不得被当作满置信触发 hard_veto。"""
    records_path = tmp_path / "m7_news_latest.jsonl"
    _write_jsonl(
        records_path,
        [
            '{"symbol":"600000","sentiment":-1.0,"event_id":"evt-nc"}',
        ],
    )
    provider = ArtifactNewsSignalProvider(path=records_path)
    decision = provider.news_risk(symbol="600000")
    assert decision.available_for_symbol is True
    assert decision.hard_veto is False
    assert decision.max_negative_confidence == pytest.approx(0.5)
    assert "negative_news" in decision.reasons
