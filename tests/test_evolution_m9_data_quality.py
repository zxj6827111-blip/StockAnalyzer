from __future__ import annotations

from stock_analyzer.evolution.modules.m9_data_quality import inspect_data_quality


def test_missing_key_field_freezes_symbol() -> None:
    result = inspect_data_quality(
        [{"symbol": "600000.SH", "open": 1.0, "high": 1.1, "low": 0.9, "close": None, "volume": 10}]
    )
    assert result.frozen_symbols == ("600000.SH",)
    assert result.degraded is True


def test_volume_zero_freezes_symbol() -> None:
    result = inspect_data_quality(
        [{"symbol": "600001.SH", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 0}]
    )
    assert result.frozen_symbols == ("600001.SH",)
    assert result.freeze_reasons["600001.SH"] == "volume_non_positive"


def test_blackout_day_when_all_symbols_frozen() -> None:
    result = inspect_data_quality(
        [
            {"symbol": "A", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 0},
            {"symbol": "B", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 0},
        ]
    )
    assert result.blackout_day is True


def test_explicit_blackout_day_flag_is_respected() -> None:
    result = inspect_data_quality(
        [
            {
                "symbol": "600002.SH",
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 100,
                "blackout_day": True,
            }
        ]
    )
    assert result.blackout_day is True


def test_healthy_records_not_degraded() -> None:
    result = inspect_data_quality(
        [{"symbol": "600003.SH", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100}]
    )
    assert result.frozen_symbols == ()
    assert result.degraded is False
    assert result.blackout_day is False
