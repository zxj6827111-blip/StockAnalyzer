from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import pandas as pd
from pytest import MonkeyPatch, fixture

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records
from stock_analyzer.infra.cache import InMemoryCache
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services import news_service as news_service_module


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise AssertionError(f"Expected path-like value, got {value!r}")


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.notification_filter.enabled = False
    return config


def _new_service(config: StockAnalyzerConfig | None = None) -> StockAnalyzerService:
    provider = SyntheticProvider(seed_offset=2031)
    effective_config = config or _load_test_config()
    original_build_runtime_provider = runtime_service_module.build_runtime_provider
    original_build_realtime_runtime_provider = (
        runtime_service_module.build_realtime_runtime_provider
    )
    original_build_market_depth_provider = runtime_service_module.build_market_depth_provider
    try:
        runtime_service_module.build_runtime_provider = (
            lambda config, synthetic_seed=2026: provider
        )
        runtime_service_module.build_realtime_runtime_provider = (
            lambda config, synthetic_seed=2026, timezone="Asia/Shanghai": provider
        )
        runtime_service_module.build_market_depth_provider = lambda config: None
        service = StockAnalyzerService(config=effective_config)
    finally:
        runtime_service_module.build_runtime_provider = original_build_runtime_provider
        runtime_service_module.build_realtime_runtime_provider = (
            original_build_realtime_runtime_provider
        )
        runtime_service_module.build_market_depth_provider = original_build_market_depth_provider
    service._provider = provider  # noqa: SLF001
    service._pipeline._provider = provider  # noqa: SLF001
    service._realtime_provider = provider  # noqa: SLF001
    if service._realtime_pipeline is not None:
        service._realtime_pipeline._provider = provider  # noqa: SLF001
    service._refresh_runtime_state_from_disk_if_changed = lambda: None  # noqa: SLF001
    return service


def _seed_lightweight_news_preview(service: StockAnalyzerService) -> None:
    cache_entries: dict[tuple[str, str], dict[str, object]] = {}
    service._test_news_preview_cache_entries = cache_entries  # noqa: SLF001

    def _preview_payload(symbol: str, strategy: str) -> dict[str, object]:
        normalized_symbol = str(symbol).strip()
        normalized_strategy = str(strategy).strip().lower() or "trend"
        return {
            "symbol": normalized_symbol,
            "strategy": normalized_strategy,
            "news_component": 0.61 if normalized_symbol == "600000" else 0.47,
            "status": "ok",
        }

    def preview_news_component(symbol: str, strategy: str = "trend") -> dict[str, object]:
        payload = _preview_payload(symbol=symbol, strategy=strategy)
        cache_entries[(str(payload["symbol"]), str(payload["strategy"]))] = dict(payload)
        return payload

    def preview_news_components(
        symbols: list[str],
        strategy: str = "trend",
    ) -> dict[str, object]:
        items = [preview_news_component(symbol=symbol, strategy=strategy) for symbol in symbols]
        average_news_component = (
            sum(_as_float(item["news_component"]) for item in items) / len(items)
            if items
            else 0.0
        )
        return {
            "status": "ok",
            "strategy": str(strategy).strip().lower() or "trend",
            "records": len(items),
            "ok_records": len(items),
            "average_news_component": average_news_component,
            "items": items,
        }

    def news_preview_cache_state() -> dict[str, object]:
        return {"entries": len(cache_entries), "ttl_sec": 300}

    def clear_news_preview_cache(symbol: str = "", strategy: str = "") -> dict[str, object]:
        normalized_symbol = str(symbol).strip()
        normalized_strategy = str(strategy).strip().lower()
        keys_to_delete = [
            key
            for key in cache_entries
            if (not normalized_symbol or key[0] == normalized_symbol)
            and (not normalized_strategy or key[1] == normalized_strategy)
        ]
        for key in keys_to_delete:
            del cache_entries[key]
        return {
            "symbol": normalized_symbol,
            "strategy": normalized_strategy,
            "cleared": len(keys_to_delete),
            "remaining": len(cache_entries),
        }

    service._pipeline.preview_news_component = preview_news_component  # noqa: SLF001
    service._pipeline.preview_news_components = preview_news_components  # noqa: SLF001
    service._pipeline.news_preview_cache_state = news_preview_cache_state  # noqa: SLF001
    service._pipeline.clear_news_preview_cache = clear_news_preview_cache  # noqa: SLF001


def _reset_preview_test_state(service: StockAnalyzerService) -> None:
    service._audit_events.clear()  # noqa: SLF001
    service._audit_seq = 0  # noqa: SLF001
    service.state.watchlist = []
    cache_entries = getattr(service, "_test_news_preview_cache_entries", None)
    if isinstance(cache_entries, dict):
        cache_entries.clear()
    try:
        service._pipeline.clear_news_preview_cache(symbol="", strategy="")  # noqa: SLF001
    except Exception:
        pass


_SHARED_NEWS_PREVIEW_SERVICE = _new_service()
_SHARED_LIGHTWEIGHT_NEWS_PREVIEW_SERVICE = _new_service()
_seed_lightweight_news_preview(_SHARED_LIGHTWEIGHT_NEWS_PREVIEW_SERVICE)
_SHARED_LIVE_NEWS_BRIEFING_SERVICE = _new_service()
_FIRST_CROSS_INSTANCE_BRIEFING_SERVICE = _new_service()
_SECOND_CROSS_INSTANCE_BRIEFING_SERVICE = _new_service()
_SHARED_M7_NEWS_SERVICE = _new_service()


def _reset_live_news_briefing_service(
    service: StockAnalyzerService,
    *,
    cache: InMemoryCache | None = None,
) -> None:
    service.state.watchlist = []
    service._cache = cache or InMemoryCache()  # noqa: SLF001


def _reset_m7_live_news_service(
    service: StockAnalyzerService,
    *,
    artifact_path: Path,
) -> None:
    service._cache = InMemoryCache()  # noqa: SLF001
    service._config.evolution.m7_news_records_path = str(artifact_path)  # noqa: SLF001




@fixture(scope="module")
def shared_news_preview_service() -> StockAnalyzerService:
    return _SHARED_NEWS_PREVIEW_SERVICE


@fixture(scope="module")
def shared_lightweight_news_preview_service() -> StockAnalyzerService:
    return _SHARED_LIGHTWEIGHT_NEWS_PREVIEW_SERVICE


def test_service_news_preview_records_audit_event(
    shared_news_preview_service: StockAnalyzerService,
) -> None:
    service = shared_news_preview_service
    _reset_preview_test_state(service)
    payload = _as_mapping(service.preview_news_component(symbol="600000", strategy="trend"))
    assert payload["symbol"] == "600000"
    assert payload["strategy"] == "trend"
    assert 0.0 <= _as_float(payload["news_component"]) <= 1.0
    assert payload["status"] in {
        "ok",
        "data_source_error",
        "feature_empty",
        "time_invariant_violation",
    }

    events = _as_mapping(service.audit_events(limit=20, event_type="news_component_preview"))
    assert _as_int(events["records"]) >= 1
    latest = _as_mapping(_as_mapping_list(events["events"])[-1])
    assert _as_mapping(latest["payload"])["symbol"] == "600000"


def test_service_news_preview_batch_records_audit_event(
    shared_lightweight_news_preview_service: StockAnalyzerService,
) -> None:
    service = shared_lightweight_news_preview_service
    _reset_preview_test_state(service)
    payload = _as_mapping(
        service.preview_news_components(symbols=["600000", "000001"], strategy="trend")
    )
    assert payload["status"] == "ok"
    assert payload["records"] == 2
    assert _as_int(payload["ok_records"]) <= 2
    assert len(_as_mapping_list(payload["items"])) == 2

    events = _as_mapping(service.audit_events(limit=20, event_type="news_component_preview_batch"))
    assert _as_int(events["records"]) >= 1
    latest = _as_mapping(_as_mapping_list(events["events"])[-1])
    assert _as_mapping(latest["payload"])["records"] == 2


def test_service_news_preview_watchlist_uses_runtime_watchlist(
    shared_lightweight_news_preview_service: StockAnalyzerService,
) -> None:
    service = shared_lightweight_news_preview_service
    _reset_preview_test_state(service)
    service.state.watchlist = ["600000", "000001", "600000"]
    payload = _as_mapping(service.preview_news_watchlist(strategy="trend", limit=2))
    assert payload["source"] == "watchlist"
    assert payload["limit"] == 2
    assert payload["selected_symbols"] == ["600000", "000001"]
    assert payload["records"] == 2

    events = _as_mapping(
        service.audit_events(limit=20, event_type="news_component_preview_watchlist")
    )
    assert _as_int(events["records"]) >= 1


def test_service_news_score_history_supports_filters_and_summary(
    shared_lightweight_news_preview_service: StockAnalyzerService,
) -> None:
    service = shared_lightweight_news_preview_service
    _reset_preview_test_state(service)
    _ = service.preview_news_component(symbol="600000", strategy="trend")
    _ = service.preview_news_component(symbol="000001", strategy="trend")
    payload = _as_mapping(service.news_score_history(limit=20, symbol="600000", strategy="trend"))
    assert _as_int(payload["records"]) >= 1
    assert _as_int(payload["total_matched"]) >= _as_int(payload["records"])
    filters = _as_mapping(payload["filters"])
    assert filters["symbol"] == "600000"
    assert filters["strategy"] == "trend"
    assert "summary" in payload
    items = _as_mapping_list(payload["items"])
    assert all(str(item.get("symbol", "")) == "600000" for item in items)


def test_service_news_score_cache_clear_records_audit_event(
    shared_lightweight_news_preview_service: StockAnalyzerService,
) -> None:
    service = shared_lightweight_news_preview_service
    _reset_preview_test_state(service)
    _ = service.preview_news_component(symbol="600000", strategy="trend")
    before = _as_mapping(service.news_score_cache_state())
    assert _as_int(before["entries"]) >= 1
    cleared = _as_mapping(service.clear_news_score_cache(symbol="600000", strategy="trend"))
    assert _as_int(cleared["cleared"]) >= 1
    after = _as_mapping(service.news_score_cache_state())
    assert _as_int(after["entries"]) == 0
    events = _as_mapping(service.audit_events(limit=20, event_type="news_component_cache_clear"))
    assert _as_int(events["records"]) >= 1


def test_live_news_briefing_service_cache_hits_after_first_build(
    monkeypatch: MonkeyPatch,
) -> None:
    service = _SHARED_LIVE_NEWS_BRIEFING_SERVICE
    _reset_live_news_briefing_service(service)
    service.state.watchlist = ["600000", "000001"]

    def fake_preview_news_watchlist(
        strategy: str = "trend",
        limit: int = 20,
        record_audit: bool = True,
    ) -> dict[str, object]:
        assert strategy == "trend"
        return {
            "strategy": strategy,
            "items": [
                {"symbol": "600000", "news_component": 0.81},
                {"symbol": "000001", "news_component": 0.72},
            ],
            "records": 2,
            "selected_symbols": ["600000", "000001"][:limit],
        }

    call_counter = {"count": 0}

    def fake_fetch_symbol_live_news(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        call_counter["count"] += 1
        return [
            {
                "symbol": symbol,
                "title": f"{symbol} 新闻标题",
                "content": "",
                "published_at": now.isoformat(),
                "source": "mock",
                "url": "",
            }
        ]

    monkeypatch.setattr(service, "preview_news_watchlist", fake_preview_news_watchlist)
    monkeypatch.setattr(service, "_fetch_symbol_live_news", fake_fetch_symbol_live_news)

    first = _as_mapping(
        service.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=2,
            max_items=2,
            record_audit=False,
        )
    )
    second = _as_mapping(
        service.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=2,
            max_items=2,
            record_audit=False,
        )
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["records"] == 2
    assert second["records"] == 2
    assert call_counter["count"] == 2


def test_live_news_briefing_cache_can_be_reused_across_service_instances(
    monkeypatch: MonkeyPatch,
) -> None:
    shared_cache = InMemoryCache()
    first_service = _FIRST_CROSS_INSTANCE_BRIEFING_SERVICE
    second_service = _SECOND_CROSS_INSTANCE_BRIEFING_SERVICE
    _reset_live_news_briefing_service(first_service, cache=shared_cache)
    _reset_live_news_briefing_service(second_service, cache=shared_cache)
    first_service.state.watchlist = ["600000", "000001"]
    second_service.state.watchlist = ["600000", "000001"]

    def fake_preview_news_watchlist(
        strategy: str = "trend",
        limit: int = 20,
        record_audit: bool = True,
    ) -> dict[str, object]:
        return {
            "strategy": strategy,
            "items": [
                {"symbol": "600000", "news_component": 0.81},
                {"symbol": "000001", "news_component": 0.72},
            ],
            "records": 2,
            "selected_symbols": ["600000", "000001"][:limit],
        }

    first_counter = {"count": 0}
    second_counter = {"count": 0}

    def first_fetch(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        first_counter["count"] += 1
        return [
            {
                "symbol": symbol,
                "title": f"{symbol} 新闻标题",
                "content": "",
                "published_at": now.isoformat(),
                "source": "mock",
                "url": "",
            }
        ]

    def second_fetch(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        second_counter["count"] += 1
        return []

    monkeypatch.setattr(first_service, "preview_news_watchlist", fake_preview_news_watchlist)
    monkeypatch.setattr(second_service, "preview_news_watchlist", fake_preview_news_watchlist)
    monkeypatch.setattr(first_service, "_fetch_symbol_live_news", first_fetch)
    monkeypatch.setattr(second_service, "_fetch_symbol_live_news", second_fetch)

    warmed = _as_mapping(
        first_service.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=2,
            max_items=2,
            record_audit=False,
        )
    )
    reused = _as_mapping(
        second_service.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=2,
            max_items=2,
            record_audit=False,
        )
    )

    assert warmed["cache_hit"] is False
    assert reused["cache_hit"] is True
    assert first_counter["count"] == 2
    assert second_counter["count"] == 0


def test_fetch_symbol_live_news_reads_real_akshare_columns(monkeypatch: MonkeyPatch) -> None:
    service = _SHARED_LIVE_NEWS_BRIEFING_SERVICE
    _reset_live_news_briefing_service(service)
    now = datetime(2026, 3, 16, 8, 30)

    class _FakeAkshare:
        @staticmethod
        def stock_news_em(symbol: str) -> pd.DataFrame:
            assert symbol == "600438"
            return pd.DataFrame(
                [
                    {
                        "关键词": "600438",
                        "新闻标题": "600438 重大资产重组",
                        "新闻内容": "公司披露重大资产重组事项。",
                        "发布时间": "2026-03-16 07:45:00",
                        "文章来源": "测试来源",
                        "新闻链接": "https://example.com/news/600438",
                    }
                ]
            )

    monkeypatch.setattr(service, "_import_akshare", lambda: _FakeAkshare())

    payload = service._fetch_symbol_live_news(
        symbol="600438",
        now=now,
        max_age_hours=18.0,
        per_symbol_limit=2,
        force_refresh=True,
    )

    assert len(payload) == 1
    assert payload[0]["title"] == "600438 重大资产重组"
    assert payload[0]["source"] == "测试来源"
    assert payload[0]["url"] == "https://example.com/news/600438"


def test_live_news_briefing_premarket_retries_with_relaxed_lookback(
    monkeypatch: MonkeyPatch,
) -> None:
    service = _SHARED_LIVE_NEWS_BRIEFING_SERVICE
    _reset_live_news_briefing_service(service)
    service.state.watchlist = ["600438"]
    now = datetime(2026, 3, 16, 8, 30)

    def fake_preview_news_watchlist(
        strategy: str = "trend",
        limit: int = 20,
        record_audit: bool = True,
    ) -> dict[str, object]:
        _ = record_audit
        return {
            "strategy": strategy,
            "items": [{"symbol": "600438", "news_component": 0.81}],
            "records": 1,
            "selected_symbols": ["600438"][:limit],
        }

    calls: list[tuple[float, bool]] = []

    def fake_fetch_symbol_live_news(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        _ = per_symbol_limit
        calls.append((max_age_hours, force_refresh))
        if max_age_hours < 120.0:
            return []
        return [
            {
                "symbol": symbol,
                "title": "600438 周末重组进展",
                "content": "公司周末披露新的重组进展。",
                "published_at": "2026-03-14T20:15:00",
                "source": "mock",
                "url": "https://example.com/weekend-news",
            }
        ]

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            _ = tz
            return now

    monkeypatch.setattr(service, "preview_news_watchlist", fake_preview_news_watchlist)
    monkeypatch.setattr(service, "_fetch_symbol_live_news", fake_fetch_symbol_live_news)
    monkeypatch.setattr(news_service_module, "datetime", _FixedDateTime)

    payload = _as_mapping(
        service.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=1,
            max_items=1,
            max_age_hours=18.0,
            force_refresh=False,
            record_audit=False,
        )
    )

    assert payload["real_news_available"] is True
    assert payload["records"] == 1
    assert payload["lookback_hours"] == 120.0
    assert calls == [(18.0, False), (120.0, True)]


def test_live_news_briefing_deduplicates_same_symbol_and_attaches_names(
    monkeypatch: MonkeyPatch,
) -> None:
    service = _SHARED_LIVE_NEWS_BRIEFING_SERVICE
    _reset_live_news_briefing_service(service)
    service.state.watchlist = ["600000", "000001"]
    now = datetime(2026, 3, 16, 10, 30)

    def fake_preview_news_watchlist(
        strategy: str = "trend",
        limit: int = 20,
        record_audit: bool = True,
    ) -> dict[str, object]:
        _ = record_audit
        return {
            "strategy": strategy,
            "items": [
                {"symbol": "600000", "news_component": 0.81},
                {"symbol": "000001", "news_component": 0.72},
            ],
            "records": 2,
            "selected_symbols": ["600000", "000001"][:limit],
        }

    def fake_fetch_symbol_live_news(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        _ = now, max_age_hours, per_symbol_limit, force_refresh
        if symbol == "600000":
            return [
                {
                    "symbol": symbol,
                    "title": "600000 首条新闻",
                    "content": "",
                    "published_at": "2026-03-16T10:10:00",
                    "source": "测试来源",
                    "url": "",
                },
                {
                    "symbol": symbol,
                    "title": "600000 次条新闻",
                    "content": "",
                    "published_at": "2026-03-16T10:18:00",
                    "source": "测试来源",
                    "url": "",
                },
            ]
        return [
            {
                "symbol": symbol,
                "title": "000001 独立新闻",
                "content": "",
                "published_at": "2026-03-16T10:16:00",
                "source": "测试来源",
                "url": "",
            }
        ]

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            _ = tz
            return now

    monkeypatch.setattr(service, "preview_news_watchlist", fake_preview_news_watchlist)
    monkeypatch.setattr(service, "_fetch_symbol_live_news", fake_fetch_symbol_live_news)
    monkeypatch.setattr(
        service,
        "_resolve_symbol_display_name",
        lambda symbol: {"600000": "浦发银行", "000001": "平安银行"}.get(symbol, ""),
    )
    monkeypatch.setattr(news_service_module, "datetime", _FixedDateTime)

    payload = _as_mapping(
        service.build_live_news_briefing(
            phase="premarket",
            strategy="trend",
            max_symbols=2,
            max_items=5,
            record_audit=False,
        )
    )
    items = _as_mapping_list(payload["items"])

    assert payload["raw_records"] == 3
    assert payload["records"] == 2
    assert len(items) == 2
    assert len({str(item.get("symbol", "")) for item in items}) == 2
    assert {str(item.get("name", "")) for item in items} == {"浦发银行", "平安银行"}


def test_news_notification_content_uses_deduped_counts_and_symbol_names(
    monkeypatch: MonkeyPatch,
) -> None:
    service = _new_service()
    monkeypatch.setattr(
        service,
        "_resolve_symbol_display_name",
        lambda symbol: {"600000": "浦发银行", "000001": "平安银行"}.get(symbol, ""),
    )
    news_watchlist = {
        "records": 5,
        "source": "watchlist",
        "selected_symbols": ["600000", "000001", "300001", "600010", "002594"],
        "items": [
            {"symbol": "600000", "news_component": 0.81},
            {"symbol": "000001", "news_component": 0.72},
        ],
    }
    news_briefing = {
        "focus_count": 5,
        "raw_records": 5,
        "records": 2,
        "items": [
            {
                "symbol": "600000",
                "name": "浦发银行",
                "title": "浦发银行披露新进展",
                "content": "浦发银行披露一季度经营改善，零售贷款不良率继续回落。",
                "published_at": "2026-03-16T08:10:00",
                "source": "证券时报",
            },
            {
                "symbol": "000001",
                "name": "平安银行",
                "title": "平安银行午间公告",
                "content": "平安银行公告拟优化零售业务结构，并同步调整部分资产投放节奏。",
                "published_at": "2026-03-16T11:20:00",
                "source": "上海证券报",
            },
        ],
    }

    premarket_content = service._build_premarket_strategy_brief_content(  # noqa: SLF001
        regime_display="偏强",
        weights={"trend": 0.6, "monster": 0.4},
        global_risk_score=62.5,
        news_avg=0.73,
        news_records=5,
        news_source="watchlist",
        news_watchlist=news_watchlist,
        news_briefing=news_briefing,
        actionable_count=1,
    )
    midday_content = service._build_midday_news_brief_content(  # noqa: SLF001
        news_briefing=news_briefing,
        week5_report={"summary": {"leaders": 2, "anomalies": 1}},
    )

    assert "情绪覆盖标的=5；新闻摘要=2（原始新闻5条，已按股票去重）" in premarket_content
    assert "重点新闻=\n1. 600000 浦发银行｜03-16 08:10｜证券时报" in premarket_content
    assert "标题：披露新进展" in premarket_content
    assert "摘要：披露一季度经营改善，零售贷款不良率继续回落。" in premarket_content
    assert "有效摘要=2（原始新闻5条，已按股票去重）" in midday_content
    assert "2. 000001 平安银行｜03-16 11:20｜上海证券报" in midday_content
    assert "标题：午间公告" in midday_content
    assert "摘要：公告拟优化零售业务结构，并同步调整部分资产投放节奏。" in midday_content


def test_news_brief_push_summary_promotes_first_headline(monkeypatch: MonkeyPatch) -> None:
    service = _new_service()
    monkeypatch.setattr(
        service,
        "_resolve_symbol_display_name",
        lambda symbol: {"600000": "浦发银行", "000001": "平安银行"}.get(symbol, ""),
    )
    news_briefing = {
        "focus_count": 5,
        "records": 2,
        "items": [
            {
                "symbol": "600000",
                "name": "浦发银行",
                "title": "浦发银行披露新进展",
                "published_at": "2026-03-16T08:10:00",
                "source": "证券时报",
            },
            {
                "symbol": "000001",
                "name": "平安银行",
                "title": "平安银行午间公告",
                "published_at": "2026-03-16T11:20:00",
                "source": "上海证券报",
            },
        ],
    }

    summary = service._build_news_brief_push_summary(  # noqa: SLF001
        news_briefing=news_briefing,
        empty_summary="暂无标题",
    )
    fallback_summary = service._build_news_brief_push_summary(  # noqa: SLF001
        news_briefing={"focus_count": 3, "records": 0, "items": []},
        empty_summary="暂无新的个股新闻标题",
    )

    assert summary == "600000 浦发银行：披露新进展 等2条"
    assert fallback_summary == "覆盖3只，暂无新的个股新闻标题"


def test_news_brief_detail_block_skips_duplicate_content(monkeypatch: MonkeyPatch) -> None:
    service = _new_service()
    monkeypatch.setattr(
        service,
        "_resolve_symbol_display_name",
        lambda symbol: {"600000": "浦发银行"}.get(symbol, ""),
    )

    rendered = service._render_news_briefing_detail_block(  # noqa: SLF001
        item={
            "symbol": "600000",
            "name": "浦发银行",
            "title": "浦发银行披露新进展",
            "content": "浦发银行披露新进展",
            "published_at": "2026-03-16T08:10:00",
            "source": "证券时报",
        },
        index=1,
    )

    assert "标题：披露新进展" in rendered
    assert "摘要：" not in rendered


def _legacy_test_run_m7_live_news_sync_writes_artifact(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.evolution.m7_news_records_path = str(tmp_path / "m7_news_latest.jsonl")
    service = _new_service(config)

    def fake_fetch_symbol_live_news(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        _ = max_age_hours, per_symbol_limit, force_refresh
        return [
            {
                "symbol": symbol,
                "title": f"{symbol} 获得大单",
                "content": "公司公告称中标重大项目",
                "published_at": now.isoformat(),
                "source": "mock",
                "url": "",
            }
        ]

    monkeypatch.setattr(service, "_fetch_symbol_live_news", fake_fetch_symbol_live_news)

    payload = _as_mapping(
        service.run_m7_live_news_sync(
            symbols=["600000", "000001"],
            force_refresh=True,
        )
    )

    assert payload["status"] == "ok"
    assert payload["records"] == 2
    assert _as_int(payload["persisted_records"]) >= 2
    artifact_path = _as_path(payload["artifact_path"])
    assert artifact_path.exists() is True
    records = load_m7_news_records(artifact_path)
    assert len(records) >= 2
    assert all(str(item.get("headline", "")).strip() for item in records[:2])


def _legacy_test_run_m7_live_news_sync_honors_ai_review_override(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.evolution.m7_news_records_path = str(tmp_path / "m7_news_ai.jsonl")
    service = _new_service(config)

    def fake_fetch_symbol_live_news(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        _ = max_age_hours, per_symbol_limit, force_refresh
        return [
            {
                "symbol": symbol,
                "title": f"{symbol} 利好公告",
                "content": "订单增长明显",
                "published_at": now.isoformat(),
                "source": "mock",
                "url": "",
            }
        ]

    def fake_enrich_m7_news_records_with_ai_review(
        *,
        records: list[dict[str, object]],
        enabled_override: bool | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        assert enabled_override is True
        enriched = []
        for item in records:
            enriched.append(
                {
                    **item,
                    "sentiment": 0.82,
                    "llm_sentiment": 0.82,
                    "llm_verdict": "approve",
                    "llm_confidence": 0.93,
                    "llm_news_verdict": "positive",
                }
            )
        return enriched, {
            "enabled": True,
            "attempted": len(records),
            "succeeded": len(records),
            "failed": 0,
        }

    monkeypatch.setattr(service, "_fetch_symbol_live_news", fake_fetch_symbol_live_news)
    monkeypatch.setattr(
        service,
        "_enrich_m7_news_records_with_ai_review",
        fake_enrich_m7_news_records_with_ai_review,
    )

    payload = _as_mapping(
        service.run_m7_live_news_sync(
            symbols=["600000"],
            force_refresh=True,
            enable_ai_review=True,
        )
    )

    assert payload["ai_review_enabled"] is True
    assert _as_mapping(payload["ai_review"])["succeeded"] == 1
    records = load_m7_news_records(_as_path(payload["artifact_path"]))
    assert records[0]["llm_confidence"] == 0.93
    assert records[0]["llm_news_verdict"] == "positive"


def test_run_m7_live_news_sync_writes_artifact(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _SHARED_M7_NEWS_SERVICE
    _reset_m7_live_news_service(service, artifact_path=tmp_path / "m7_news_latest.jsonl")

    def fake_collect_live_m7_news_records(
        *,
        symbols: list[str],
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
        enable_ai_review: bool,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        _ = max_age_hours, per_symbol_limit, force_refresh, enable_ai_review
        records = [
            {
                "event_id": f"live-{symbol}",
                "symbol": symbol,
                "headline": f"{symbol} 鑾峰緱澶у崟",
                "content": "鍏徃鍏憡绉颁腑鏍囬噸澶ч」鐩?",
                "published_at": now.isoformat(),
                "source": "mock",
                "url": "",
                "sentiment": 0.62,
                "llm_sentiment": 0.62,
                "cost": 0.01,
                "llm_verdict": "approve",
                "llm_confidence": 0.88,
                "source_file": "__live_akshare_em__",
                "provider": "akshare_em",
                "proxy_generated": False,
            }
            for symbol in symbols
        ]
        return records, {
            "provider": "akshare_em",
            "symbol_count": len(symbols),
            "fetched_symbols": len(symbols),
            "raw_items": len(symbols),
            "records": len(records),
            "ai_review": {"enabled": False, "attempted": 0, "succeeded": 0, "failed": 0},
            "errors": [],
        }

    monkeypatch.setattr(service, "_collect_live_m7_news_records", fake_collect_live_m7_news_records)

    payload = _as_mapping(
        service.run_m7_live_news_sync(
            symbols=["600000", "000001"],
            force_refresh=True,
        )
    )

    assert payload["status"] == "ok"
    assert payload["records"] == 2
    assert _as_int(payload["persisted_records"]) >= 2
    artifact_path = _as_path(payload["artifact_path"])
    assert artifact_path.exists() is True
    records = load_m7_news_records(artifact_path)
    assert len(records) >= 2
    assert all(str(item.get("headline", "")).strip() for item in records[:2])


def test_run_m7_live_news_sync_honors_ai_review_override(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _SHARED_M7_NEWS_SERVICE
    _reset_m7_live_news_service(service, artifact_path=tmp_path / "m7_news_ai.jsonl")

    def fake_fetch_symbol_live_news(
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        _ = max_age_hours, per_symbol_limit, force_refresh
        return [
            {
                "symbol": symbol,
                "title": f"{symbol} 鍒╁ソ鍏憡",
                "content": "璁㈠崟澧為暱鏄庢樉",
                "published_at": now.isoformat(),
                "source": "mock",
                "url": "",
            }
        ]

    def fake_enrich_m7_news_records_with_ai_review(
        *,
        records: list[dict[str, object]],
        enabled_override: bool | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        assert enabled_override is True
        enriched = []
        for item in records:
            enriched.append(
                {
                    **item,
                    "sentiment": 0.82,
                    "llm_sentiment": 0.82,
                    "llm_verdict": "approve",
                    "llm_confidence": 0.93,
                    "llm_news_verdict": "positive",
                }
            )
        return enriched, {
            "enabled": True,
            "attempted": len(records),
            "succeeded": len(records),
            "failed": 0,
        }

    monkeypatch.setattr(service, "_fetch_symbol_live_news", fake_fetch_symbol_live_news)
    monkeypatch.setattr(
        service,
        "_enrich_m7_news_records_with_ai_review",
        fake_enrich_m7_news_records_with_ai_review,
    )

    payload = _as_mapping(
        service.run_m7_live_news_sync(
            symbols=["600000"],
            force_refresh=True,
            enable_ai_review=True,
        )
    )

    assert payload["ai_review_enabled"] is True
    assert _as_mapping(payload["ai_review"])["succeeded"] == 1
    records = load_m7_news_records(_as_path(payload["artifact_path"]))
    assert records[0]["llm_confidence"] == 0.93
    assert records[0]["llm_news_verdict"] == "positive"
