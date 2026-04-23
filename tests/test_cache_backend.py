from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.infra.cache import InMemoryCache
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def test_build_cache_uses_redis_backend_when_available(
    monkeypatch,
) -> None:
    config = _load_test_config()
    config.cache.backend = "redis"
    config.cache.redis_url = "redis://localhost:6379/0"

    sentinel = object()

    def _fake_redis_cache(redis_url: str) -> object:
        assert redis_url == "redis://localhost:6379/0"
        return sentinel

    monkeypatch.setattr(runtime_service_module, "RedisCache", _fake_redis_cache)

    cache = StockAnalyzerService._build_cache(config)

    assert cache is sentinel


def test_build_cache_falls_back_to_memory_when_redis_init_fails(
    monkeypatch,
) -> None:
    config = _load_test_config()
    config.cache.backend = "redis"
    config.cache.redis_url = "redis://localhost:6379/0"

    def _raise_redis_cache(redis_url: str) -> object:
        raise RuntimeError(f"boom:{redis_url}")

    monkeypatch.setattr(runtime_service_module, "RedisCache", _raise_redis_cache)

    cache = StockAnalyzerService._build_cache(config)

    assert isinstance(cache, InMemoryCache)
