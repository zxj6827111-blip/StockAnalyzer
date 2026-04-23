from __future__ import annotations

import json

from pytest import MonkeyPatch
from typer.testing import CliRunner

import stock_analyzer.cli as cli_module


class _FakeService:
    def __init__(self, config: object) -> None:
        self._config = config

    def preview_news_component(self, symbol: str, strategy: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "strategy": strategy,
            "news_component": 0.61,
            "status": "ok",
        }

    def preview_news_components(self, symbols: list[str], strategy: str) -> dict[str, object]:
        return {
            "strategy": strategy,
            "records": len(symbols),
            "items": [{"symbol": item, "news_component": 0.5, "status": "ok"} for item in symbols],
            "status": "ok",
        }

    def preview_news_watchlist(self, strategy: str, limit: int) -> dict[str, object]:
        return {
            "strategy": strategy,
            "limit": limit,
            "source": "watchlist",
            "items": [{"symbol": "600000", "news_component": 0.5, "status": "ok"}],
            "status": "ok",
        }

    def news_score_history(self, limit: int, symbol: str, strategy: str) -> dict[str, object]:
        return {
            "records": 1,
            "filters": {"limit": limit, "symbol": symbol, "strategy": strategy},
            "summary": {"average_news_component": 0.5},
            "items": [
                {
                    "event_id": "AUD-00000001",
                    "symbol": symbol or "600000",
                    "strategy": strategy or "trend",
                    "news_component": 0.5,
                }
            ],
        }

    def news_score_cache_state(self) -> dict[str, object]:
        return {"entries": 2, "ttl_sec": 60.0}

    def clear_news_score_cache(self, symbol: str, strategy: str) -> dict[str, object]:
        return {"cleared": 1, "remaining": 1, "symbol": symbol, "strategy": strategy}

    def run_m7_live_news_sync(
        self,
        *,
        symbols: list[str] | None,
        timestamp: object,
        force_refresh: bool,
        enable_ai_review: bool,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "selected_symbols": symbols or [],
            "force_refresh": force_refresh,
            "ai_review_enabled": enable_ai_review,
            "records": len(symbols or []),
        }


def test_cli_news_score_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["news-score", "--symbol", "600000", "--strategy", "trend"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["symbol"] == "600000"
    assert payload["strategy"] == "trend"


def test_cli_news_score_batch_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["news-score-batch", "--symbols", "600000,000001", "--strategy", "trend"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["records"] == 2
    assert payload["status"] == "ok"


def test_cli_news_score_watchlist_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["news-score-watchlist", "--strategy", "trend", "--limit", "5"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "watchlist"
    assert payload["limit"] == 5


def test_cli_news_score_history_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "news-score-history",
            "--limit",
            "20",
            "--symbol",
            "600000",
            "--strategy",
            "trend",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["records"] == 1
    assert payload["filters"]["symbol"] == "600000"


def test_cli_news_score_cache_state_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["news-score-cache-state"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["entries"] == 2


def test_cli_news_score_cache_clear_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["news-score-cache-clear", "--symbol", "600000", "--strategy", "trend"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cleared"] == 1
    assert payload["symbol"] == "600000"


def test_cli_m7_live_news_sync_command_outputs_json(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeService)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "m7-live-news-sync",
            "--symbols",
            "600000,000001",
            "--force-refresh",
            "--enable-ai-review",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["records"] == 2
    assert payload["ai_review_enabled"] is True
