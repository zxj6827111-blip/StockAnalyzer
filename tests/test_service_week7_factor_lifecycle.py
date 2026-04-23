from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.factor_lifecycle.enabled = True
    config.factor_lifecycle.psr_min = 0.60
    config.factor_lifecycle.shap_drift_threshold = 0.25
    config.factor_lifecycle.graveyard_enabled = True
    config.factor_lifecycle.graveyard_observation_months = 2
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    return config


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise AssertionError(f"Expected bool value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def test_factor_lifecycle_observation_triggers_by_psr_and_drift() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    first = _as_mapping(
        service.record_factor_lifecycle(
            month="2026-01",
            strategy="trend",
            psr=0.72,
            ic_mean=0.03,
            top_features=[
                {"name": "volume_ratio", "importance": 0.40},
                {"name": "atr14", "importance": 0.30},
                {"name": "turnover_rate", "importance": 0.20},
            ],
        )
    )
    assert _as_bool(first["accepted"]) is True
    assert _as_bool(_as_mapping(first["record"])["observation_mode"]) is False

    second = _as_mapping(
        service.record_factor_lifecycle(
            month="2026-02",
            strategy="trend",
            psr=0.52,
            ic_mean=0.01,
            top_features=[
                {"name": "news_alpha", "importance": 0.50},
                {"name": "gap_pct", "importance": 0.30},
                {"name": "limit_up_streak", "importance": 0.15},
            ],
        )
    )
    second_record = _as_mapping(second["record"])
    assert _as_bool(second["accepted"]) is True
    assert _as_bool(second_record["observation_mode"]) is True
    assert _as_bool(second_record["psr_breach"]) is True
    assert _as_bool(second_record["drift_breach"]) is True

    status = _as_mapping(service.factor_lifecycle_status(strategy="trend"))
    state = status["state"]
    assert state is not None
    state_view = _as_mapping(state)
    assert _as_bool(state_view["observation_mode"]) is True
    assert _as_int(state_view["consecutive_observation"]) >= 1

    history = _as_mapping(service.factor_lifecycle_history(strategy="trend", limit=10))
    assert _as_int(history["records"]) >= 2


def test_factor_lifecycle_reset_clears_records() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    _ = service.record_factor_lifecycle(
        month="2026-01",
        strategy="trend",
        psr=0.50,
        ic_mean=0.01,
        top_features=[{"name": "volume_ratio", "importance": 0.5}],
    )
    status_before = _as_mapping(service.factor_lifecycle_status(strategy="trend"))
    status_before_state = _as_mapping(status_before["state"])
    assert _as_int(status_before_state["records"]) >= 1

    reset = _as_mapping(service.reset_factor_lifecycle(strategy="trend"))
    assert _as_bool(reset["accepted"]) is True
    assert _as_int(reset["removed_records"]) >= 1

    status_after = _as_mapping(service.factor_lifecycle_status(strategy="trend"))
    status_after_state = _as_mapping(status_after["state"])
    assert _as_int(status_after_state["records"]) == 0
    assert _as_bool(status_after_state["observation_mode"]) is False


def test_factor_lifecycle_graveyard_records_drifted_factors() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    _ = service.record_factor_lifecycle(
        month="2026-01",
        strategy="trend",
        psr=0.72,
        ic_mean=0.03,
        top_features=[
            {"name": "volume_ratio", "importance": 0.40},
            {"name": "atr14", "importance": 0.30},
            {"name": "turnover_rate", "importance": 0.20},
        ],
    )
    _ = service.record_factor_lifecycle(
        month="2026-02",
        strategy="trend",
        psr=0.50,
        ic_mean=0.01,
        top_features=[
            {"name": "news_alpha", "importance": 0.50},
            {"name": "gap_pct", "importance": 0.30},
            {"name": "limit_up_streak", "importance": 0.15},
        ],
    )
    third = _as_mapping(
        service.record_factor_lifecycle(
            month="2026-03",
            strategy="trend",
            psr=0.49,
            ic_mean=0.01,
            top_features=[
                {"name": "flow_burst", "importance": 0.45},
                {"name": "macro_beta", "importance": 0.28},
                {"name": "chip_spread", "importance": 0.20},
            ],
        )
    )
    assert _as_bool(third["accepted"]) is True
    assert _as_int(third["graveyard_updates"]) >= 1

    graveyard = _as_mapping(service.factor_graveyard(strategy="trend", limit=10))
    assert _as_int(graveyard["records"]) >= 1
    factors = {str(row["factor"]) for row in _as_mapping_list(graveyard["graveyard"])}
    assert "news_alpha" in factors or "gap_pct" in factors or "limit_up_streak" in factors
