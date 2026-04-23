"""Factory for runtime pipeline news providers."""

from __future__ import annotations

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.news.provider import ArtifactNewsSignalProvider
from stock_analyzer.pipeline import NewsSignalProvider


def build_news_provider(config: StockAnalyzerConfig) -> NewsSignalProvider:
    """Build default runtime news provider based on evolution inputs."""
    return ArtifactNewsSignalProvider(
        path=config.evolution.m7_news_records_path,
        max_age_days=config.evolution.m7_pipeline_max_age_days,
        half_life_hours=config.evolution.m7_pipeline_half_life_hours,
        confidence_floor=config.evolution.m7_pipeline_confidence_floor,
    )
