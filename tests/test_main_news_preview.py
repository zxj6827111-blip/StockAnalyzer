from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from stock_analyzer import main as main_module


class FakeNewsPreviewService:
    def __init__(self) -> None:
        self._history: list[dict[str, object]] = []
        self._cache: dict[tuple[str, str], dict[str, object]] = {}
        self._watchlist = ["600000", "000001", "300001"]

    def _news_component(self, symbol: str, strategy: str) -> float:
        seed = sum(ord(char) for char in f"{symbol}:{strategy}")
        return min(0.95, 0.30 + (seed % 40) / 100.0)

    def preview_news_component(self, symbol: str, strategy: str) -> dict[str, object]:
        normalized_symbol = str(symbol).strip()
        normalized_strategy = str(strategy).strip() or "trend"
        payload = {
            "symbol": normalized_symbol,
            "strategy": normalized_strategy,
            "news_component": self._news_component(normalized_symbol, normalized_strategy),
            "status": "ok",
            "reasons": [],
        }
        self._cache[(normalized_symbol, normalized_strategy)] = dict(payload)
        self._history.append(
            {
                "timestamp": "2026-03-15T10:18:00",
                **payload,
            }
        )
        return payload

    def preview_news_components(self, symbols: list[str], strategy: str) -> dict[str, object]:
        items = [
            self.preview_news_component(symbol=symbol, strategy=strategy)
            for symbol in symbols
        ]
        return {
            "status": "ok",
            "strategy": strategy,
            "records": len(items),
            "items": items,
        }

    def preview_news_watchlist(self, strategy: str, limit: int) -> dict[str, object]:
        items = [
            self.preview_news_component(symbol=symbol, strategy=strategy)
            for symbol in self._watchlist[:limit]
        ]
        average = (
            sum(float(item["news_component"]) for item in items) / len(items)
            if items
            else 0.0
        )
        return {
            "status": "ok",
            "source": "watchlist",
            "limit": limit,
            "records": len(items),
            "items": items,
            "summary": {"average_news_component": average},
        }

    def news_score_history(
        self,
        *,
        limit: int,
        symbol: str,
        strategy: str,
    ) -> dict[str, object]:
        filtered = [
            item
            for item in self._history
            if (not symbol or str(item["symbol"]) == symbol)
            and (not strategy or str(item["strategy"]) == strategy)
        ]
        items = filtered[-limit:]
        average = (
            sum(float(item["news_component"]) for item in items) / len(items)
            if items
            else 0.0
        )
        return {
            "status": "ok",
            "records": len(items),
            "filters": {"symbol": symbol, "strategy": strategy},
            "summary": {"average_news_component": average},
            "items": items,
        }

    def news_score_cache_state(self) -> dict[str, object]:
        return {"status": "ok", "entries": len(self._cache), "ttl_sec": 300}

    def clear_news_score_cache(self, *, symbol: str, strategy: str) -> dict[str, object]:
        keys_to_delete = [
            key
            for key in self._cache
            if (not symbol or key[0] == symbol) and (not strategy or key[1] == strategy)
        ]
        for key in keys_to_delete:
            del self._cache[key]
        return {
            "status": "ok",
            "cleared": len(keys_to_delete),
            "remaining": len(self._cache),
        }


@contextmanager
def _patched_client() -> Iterator[TestClient]:
    original_service = main_module._service
    original_feishu_enabled = main_module._config.feishu_interaction.enabled
    main_module._service = FakeNewsPreviewService()
    main_module._config.feishu_interaction.enabled = False
    try:
        with TestClient(main_module.app) as client:
            yield client
    finally:
        main_module._config.feishu_interaction.enabled = original_feishu_enabled
        main_module._service = original_service


def test_news_score_preview_endpoint_returns_payload() -> None:
    with _patched_client() as client:
        response = client.get(
            "/news/score",
            params={"symbol": "600000", "strategy": "trend"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["symbol"] == "600000"
        assert payload["strategy"] == "trend"
        assert 0.0 <= float(payload["news_component"]) <= 1.0
        assert payload["status"] in {
            "ok",
            "data_source_error",
            "feature_empty",
            "time_invariant_violation",
        }


def test_news_score_preview_endpoint_requires_symbol() -> None:
    with _patched_client() as client:
        response = client.get("/news/score")
        assert response.status_code == 422


def test_news_score_preview_batch_endpoint_returns_payload() -> None:
    with _patched_client() as client:
        response = client.get(
            "/news/score/batch",
            params=[("symbols", "600000"), ("symbols", "000001"), ("strategy", "trend")],
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["records"] == 2
        assert len(payload["items"]) == 2


def test_news_score_preview_batch_endpoint_requires_symbols() -> None:
    with _patched_client() as client:
        response = client.get("/news/score/batch")
        assert response.status_code == 422


def test_news_score_preview_watchlist_endpoint_returns_payload() -> None:
    with _patched_client() as client:
        response = client.get(
            "/news/score/watchlist",
            params={"strategy": "trend", "limit": 5},
        )
        assert response.status_code == 200
        payload = response.json()
        assert "source" in payload
        assert payload["limit"] == 5
        assert "items" in payload


def test_news_score_history_endpoint_returns_payload() -> None:
    with _patched_client() as client:
        _ = client.get("/news/score", params={"symbol": "600000", "strategy": "trend"})
        response = client.get(
            "/news/score/history",
            params={"limit": 20, "symbol": "600000", "strategy": "trend"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert "records" in payload
        assert "summary" in payload
        assert payload["filters"]["symbol"] == "600000"


def test_news_score_cache_state_and_clear_endpoints() -> None:
    with _patched_client() as client:
        _ = client.get("/news/score", params={"symbol": "600000", "strategy": "trend"})
        state_resp = client.get("/news/score/cache/state")
        assert state_resp.status_code == 200
        state_payload = state_resp.json()
        assert state_payload["entries"] >= 1

        clear_resp = client.post(
            "/news/score/cache/clear",
            json={"symbol": "600000", "strategy": "trend"},
        )
        assert clear_resp.status_code == 200
        clear_payload = clear_resp.json()
        assert clear_payload["cleared"] >= 1
