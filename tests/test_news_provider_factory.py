from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.news.provider import ArtifactNewsSignalProvider
from stock_analyzer.runtime.news_provider_factory import build_news_provider


def test_news_provider_factory_wires_m7_pipeline_config() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.evolution.m7_news_records_path = "artifacts/custom/news.jsonl"
    config.evolution.m7_pipeline_max_age_days = 5
    config.evolution.m7_pipeline_half_life_hours = 12.0
    config.evolution.m7_pipeline_confidence_floor = 0.4

    provider = build_news_provider(config)
    assert isinstance(provider, ArtifactNewsSignalProvider)
    assert provider._max_age_days == 5
    assert provider._half_life_hours == 12.0
    assert provider._confidence_floor == 0.4

