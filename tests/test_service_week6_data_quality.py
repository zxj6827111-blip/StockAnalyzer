from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_mapping(item) for item in value]
    assert len(items) == len(value)
    return items


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_text(item) for item in value]
    assert len(items) == len(value)
    return items


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.week6.auto_notify = False
    config.week6.data_quality_notify = False
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    return config


class _MissingFieldsProvider:
    def __init__(self) -> None:
        self._cache: dict[int, pd.DataFrame] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        _ = symbol
        normalized_lookback = max(20, lookback_days)
        frame = self._cache.get(normalized_lookback)
        if frame is None:
            dates = pd.bdate_range("2026-01-02", periods=normalized_lookback)
            close = pd.Series(range(len(dates)), index=dates, dtype=float) * 0.03 + 10.0
            frame = pd.DataFrame(
                {
                    "open": close * 0.99,
                    "high": close * 1.01,
                    "low": close * 0.98,
                    "close": close,
                    "volume": 2_000_000.0,
                    "turnover": close * 2_000_000.0,
                    "float_market_cap": 10_000_000_000.0,
                },
                index=dates,
            )
            frame.index.name = "date"
            self._cache[normalized_lookback] = frame
        return frame.copy()


class _HealthyFieldsProvider:
    def __init__(self) -> None:
        self._cache: dict[int, pd.DataFrame] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        _ = symbol
        normalized_lookback = max(20, lookback_days)
        frame = self._cache.get(normalized_lookback)
        if frame is None:
            dates = pd.bdate_range("2026-01-02", periods=normalized_lookback)
            close = pd.Series(range(len(dates)), index=dates, dtype=float) * 0.03 + 10.0
            frame = pd.DataFrame(
                {
                    "open": close * 0.99,
                    "high": close * 1.01,
                    "low": close * 0.98,
                    "close": close,
                    "volume": 2_000_000.0,
                    "turnover": close * 2_000_000.0,
                    "float_market_cap": 10_000_000_000.0,
                    "financial_data_complete": True,
                    "roe": 0.12,
                    "debt_ratio": 0.35,
                    "holder_count": 25_000.0,
                    "block_trade_net": 0.0,
                    "financing_balance": 120_000_000.0,
                    "northbound_net": 8_000_000.0,
                    "dragon_tiger_flag": False,
                    "background_data_complete": True,
                    "financial_source": "stub",
                    "background_data_source": "stub",
                    "financial_missing_fields": "",
                },
                index=dates,
            )
            frame.index.name = "date"
            self._cache[normalized_lookback] = frame
        return frame.copy()


def _reset_shared_week6_quality_service(service: StockAnalyzerService) -> None:
    _patch_attr(service, "_last_week6_data_quality_report", None)
    service._week6_data_quality_history.clear()
    service._audit_events.clear()
    _patch_attr(service, "_audit_seq", 0)


_SHARED_HEALTHY_WEEK6_QUALITY_SERVICE = StockAnalyzerService(config=_load_test_config())
_patch_attr(_SHARED_HEALTHY_WEEK6_QUALITY_SERVICE, "_provider", _HealthyFieldsProvider())
_SHARED_MISSING_WEEK6_QUALITY_SERVICE = StockAnalyzerService(config=_load_test_config())
_patch_attr(_SHARED_MISSING_WEEK6_QUALITY_SERVICE, "_provider", _MissingFieldsProvider())


def test_week6_data_quality_scan_healthy_with_complete_provider() -> None:
    service = _SHARED_HEALTHY_WEEK6_QUALITY_SERVICE
    _reset_shared_week6_quality_service(service)

    report = service.run_week6_data_prewarm(
        symbols=["600000", "000001"],
        lookback_days=60,
        notify_enabled=False,
        source_trace_id="test-week6-quality-healthy",
    )
    assert _as_text(report["status"]) == "healthy"
    assert _as_int(report["success_symbols"]) >= 2
    assert _as_float(report["overall_coverage_ratio"]) > 0.8

    latest = service.latest_week6_data_quality_report()
    assert latest is not None
    history = service.week6_data_quality_history(limit=10)
    assert _as_int(history["records"]) >= 1


def test_week6_data_quality_scan_flags_missing_fields() -> None:
    service = _SHARED_MISSING_WEEK6_QUALITY_SERVICE
    _reset_shared_week6_quality_service(service)

    report = service.run_week6_data_prewarm(
        symbols=["600000"],
        lookback_days=40,
        notify_enabled=False,
        source_trace_id="test-week6-quality-missing",
    )
    assert _as_text(report["status"]) == "critical"
    assert _as_int(report["success_symbols"]) == 1
    assert _as_float(report["overall_coverage_ratio"]) == 0.0
    critical_fields = _as_text_list(report["critical_fields"])
    assert "roe" in critical_fields
    assert "financial_data_complete" in critical_fields

    events = service.audit_events(limit=20, event_type="week6_data_quality_scan")
    assert _as_int(events["records"]) >= 1
    latest = _as_mapping_list(events["events"])[-1]
    assert _as_text(latest["level"]) == "warn"
