from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


class _StubProvider:
    def __init__(
        self,
        frame_by_symbol: dict[str, pd.DataFrame],
        intraday_by_symbol: dict[tuple[str, str], pd.DataFrame] | None = None,
    ) -> None:
        self._frame_by_symbol = frame_by_symbol
        self._intraday_by_symbol = intraday_by_symbol or {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 0) -> pd.DataFrame:
        frame = self._frame_by_symbol.get(symbol)
        if frame is None:
            return pd.DataFrame()
        return frame.tail(lookback_days if lookback_days > 0 else len(frame)).copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        frame = self._intraday_by_symbol.get((symbol, interval))
        if frame is None:
            return pd.DataFrame()
        return frame.tail(lookback_days if lookback_days > 0 else len(frame)).copy()


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.evolution.strict_dependency_check = False
    config.evolution.code_commit_id = "git:test"
    return config


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    raise AssertionError(f"Expected dict, got {type(value).__name__}")


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [str(item) for item in value]


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _build_bars(*, days: int, missing_turnover: bool) -> pd.DataFrame:
    idx = pd.bdate_range("2025-11-03", periods=days)
    close = pd.Series([10.0 + i * 0.02 for i in range(days)], index=idx)
    volume = pd.Series([2_000_000 + i * 2000 for i in range(days)], index=idx)
    data: dict[str, object] = {
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": volume,
    }
    if missing_turnover:
        data["turnover"] = [None] * days
        data["volume"] = [None] * days
    else:
        data["turnover"] = (close * volume).to_list()
    return pd.DataFrame(data, index=idx)


def _build_intraday_summary() -> pd.DataFrame:
    idx = pd.bdate_range("2026-02-02", periods=5)
    return pd.DataFrame(
        {
            "session_return": [0.01, 0.02, 0.03, 0.04, 0.05],
            "realized_vol": [0.02, 0.03, 0.04, 0.05, 0.06],
            "vwap_gap": [0.001, 0.002, 0.003, 0.004, 0.005],
            "last30_return": [0.006, 0.007, 0.008, 0.009, 0.010],
            "close_position": [0.4, 0.5, 0.6, 0.7, 0.8],
            "am_pm_diff": [0.01, 0.00, -0.01, 0.02, 0.03],
        },
        index=idx,
    )


def test_build_evolution_m9_records_marks_sparse_history_fallback_when_adv60_missing() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    provider = _StubProvider({"600000.SH": _build_bars(days=80, missing_turnover=True)})
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)

    records = service._build_evolution_m9_records(["600000.SH"])
    first = _as_mapping(records[0])
    assert first["symbol"] == "600000.SH"
    assert first["liquidity_tier"] == "small"
    assert first["liquidity_tier_fallback"] is True
    assert first["sparse_history_flag"] is True
    assert first["mapping_level_used"] == "regime_x_liquidity"
    assert "liquidity_tier_fallback_small" in _as_text_list(first["mapping_fallback_steps"])


def test_build_evolution_m9_records_uses_non_sparse_mapping_when_adv60_available() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    provider = _StubProvider({"000001.SZ": _build_bars(days=90, missing_turnover=False)})
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)

    records = service._build_evolution_m9_records(["000001.SZ"])
    first = _as_mapping(records[0])
    assert first["symbol"] == "000001.SZ"
    assert first["sparse_history_flag"] is False
    assert first["liquidity_tier_fallback"] is False
    assert first["mapping_level_used"] == "regime_x_liquidity_x_volatility"
    assert first["adv60"] is not None


def test_build_evolution_m9_records_includes_intraday_summary_fields() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    provider = _StubProvider(
        {"000001.SZ": _build_bars(days=90, missing_turnover=False)},
        intraday_by_symbol={
            ("000001.SZ", "1m"): _build_intraday_summary(),
            ("000001.SZ", "5m"): _build_intraday_summary(),
        },
    )
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)

    records = service._build_evolution_m9_records(["000001.SZ"])
    first = _as_mapping(records[0])
    assert first["intraday_1m_latest_date"] == "2026-02-06"
    assert first["intraday_1m_session_return"] == 0.05
    assert first["intraday_1m_realized_vol"] == 0.06
    assert first["intraday_5m_latest_date"] == "2026-02-06"
    assert first["intraday_5m_am_pm_diff"] == 0.03
    assert first["intraday_5m_close_position"] == 0.8
