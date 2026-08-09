from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from stock_analyzer.config import FactorLifecycleConfig, load_config
from stock_analyzer.research.ic_decay_report import (
    bucket_ic_series_by_month,
    compute_factor_ic_series,
    compute_ic_decay_from_monthly_ics,
    compute_ic_decay_report,
    persist_ic_decay_report,
)
from stock_analyzer.runtime.service import (
    StockAnalyzerService,
    _last_trading_day_of_month,
)


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"Expected mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [_as_mapping(item) for item in value if isinstance(item, Mapping)]


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def _monthly_ic_series() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "decaying_factor": [0.10, 0.09, 0.08, 0.07, 0.06, 0.05],
            "healthy_factor": [0.08, 0.09, 0.08, 0.09, 0.08, 0.09],
            "low_mean_factor": [0.02] * 6,
            "short_factor": [
                0.04,
                float("nan"),
                float("nan"),
                float("nan"),
                float("nan"),
                float("nan"),
            ],
        },
        index=pd.to_datetime(
            [
                "2025-01-31",
                "2025-02-28",
                "2025-03-31",
                "2025-04-30",
                "2025-05-31",
                "2025-06-30",
            ]
        ),
    )


def test_bucket_ic_series_by_month_groups_into_monthly_means() -> None:
    index = pd.to_datetime(
        [
            "2025-01-02",
            "2025-01-03",
            "2025-01-06",
            "2025-02-03",
            "2025-02-04",
            "2025-03-03",
        ]
    )
    series = pd.DataFrame({"f1": [0.10, 0.20, 0.30, 0.05, 0.15, 0.40]}, index=index)
    monthly = bucket_ic_series_by_month(series)

    assert list(monthly.index) == ["2025-01", "2025-02", "2025-03"]
    assert _as_float(monthly.at["2025-01", "f1"]) == pytest.approx(0.20)
    assert _as_float(monthly.at["2025-02", "f1"]) == pytest.approx(0.10)
    assert _as_float(monthly.at["2025-03", "f1"]) == pytest.approx(0.40)


def test_bucket_ic_series_by_month_trims_to_lookback_months() -> None:
    index = pd.to_datetime([f"2025-{month:02d}-28" for month in range(1, 13)])
    series = pd.DataFrame({"f1": [0.01 * month for month in range(1, 13)]}, index=index)
    monthly = bucket_ic_series_by_month(series, lookback_months=3)

    assert list(monthly.index) == ["2025-10", "2025-11", "2025-12"]
    assert _as_float(monthly.at["2025-12", "f1"]) == pytest.approx(0.12)


def test_ic_decay_report_marks_decaying_factor_by_slope() -> None:
    report = compute_ic_decay_report(ic_series=_monthly_ic_series())
    factors = {
        str(item["factor"]): item
        for item in _as_mapping_list(report["factors"])
    }
    decaying = factors["decaying_factor"]

    assert _as_float(decaying["recent_ic_mean"]) == pytest.approx(0.06)
    assert _as_float(decaying["ic_slope"]) == pytest.approx(-0.01)
    assert _as_float(decaying["ic_time_corr"]) == pytest.approx(-1.0)
    assert _as_float(decaying["decay_rate"]) == pytest.approx(-1.0 / 3.0)
    assert decaying["health"] == "decaying"
    assert decaying["reason"] == "slope_below_threshold"


def test_ic_decay_report_marks_healthy_and_low_mean_factors() -> None:
    report = compute_ic_decay_report(ic_series=_monthly_ic_series())
    factors = {
        str(item["factor"]): item
        for item in _as_mapping_list(report["factors"])
    }
    healthy = factors["healthy_factor"]
    assert healthy["health"] == "healthy"
    assert _as_float(healthy["recent_ic_mean"]) == pytest.approx(0.086667, abs=1e-5)
    assert _as_float(healthy["ic_mean_breach"]) == 0.0 or healthy["ic_mean_breach"] is False

    low_mean = factors["low_mean_factor"]
    assert low_mean["health"] == "decaying"
    assert low_mean["reason"] == "recent_ic_mean_below_threshold"
    assert _as_float(low_mean["ic_mean_breach"]) == 1.0 or low_mean["ic_mean_breach"] is True


def test_ic_decay_report_marks_insufficient_data_and_summarizes() -> None:
    report = compute_ic_decay_report(ic_series=_monthly_ic_series(), min_months=3)
    factors = {
        str(item["factor"]): item
        for item in _as_mapping_list(report["factors"])
    }

    assert factors["short_factor"]["health"] == "insufficient_data"
    assert report["status"] == "ok"
    assert report["factor_count"] == 4
    summary = _as_mapping(report["summary"])
    assert summary["factors_total"] == 4
    assert summary["factors_healthy"] == 1
    assert summary["factors_decaying"] == 2
    assert summary["factors_insufficient"] == 1
    assert sorted(str(item) for item in summary["decaying_factors"]) == [
        "decaying_factor",
        "low_mean_factor",
    ]
    assert list(report["months"]) == [
        "2025-01",
        "2025-02",
        "2025-03",
        "2025-04",
        "2025-05",
        "2025-06",
    ]


def test_ic_decay_report_all_insufficient_status() -> None:
    series = pd.DataFrame({"f1": [0.04]}, index=pd.to_datetime(["2025-01-31"]))
    report = compute_ic_decay_report(ic_series=series, min_months=3)
    assert report["status"] == "insufficient_data"
    assert report["summary"]["factors_total"] == 1


def test_ic_decay_report_empty_and_invalid_inputs() -> None:
    empty = compute_ic_decay_report(ic_series=pd.DataFrame())
    assert empty["status"] == "empty"
    assert empty["factor_count"] == 0

    invalid = compute_ic_decay_report(records=[])
    assert invalid["status"] == "invalid_input"


def test_ic_decay_report_from_monthly_ics_and_lookback_trim() -> None:
    monthly_ics = {
        "trend_factor": [
            {"month": f"2024-{month:02d}", "ic": 0.02 + 0.01 * month}
            for month in range(1, 13)
        ]
    }
    report = compute_ic_decay_from_monthly_ics(monthly_ics=monthly_ics, lookback_months=6)
    factors = _as_mapping_list(report["factors"])
    assert len(factors) == 1
    factor = factors[0]
    assert factor["factor"] == "trend_factor"
    assert factor["months_used"] == 6
    points = [str(item.get("month")) for item in _as_mapping_list(factor["monthly_ics"])]
    assert points == ["2024-07", "2024-08", "2024-09", "2024-10", "2024-11", "2024-12"]
    assert factor["health"] == "healthy"


def test_ic_decay_report_normalizes_month_formats() -> None:
    monthly_ics = {
        "factor_a": [
            {"month": "2025-01-15", "ic": 0.05},
            {"month": "202502", "ic": 0.05},
            {"month": "2025-03", "ic": 0.05},
        ]
    }
    report = compute_ic_decay_from_monthly_ics(monthly_ics=monthly_ics, min_months=3)
    factor = _as_mapping_list(report["factors"])[0]
    points = [str(item.get("month")) for item in _as_mapping_list(factor["monthly_ics"])]
    assert points == ["2025-01", "2025-02", "2025-03"]
    assert factor["health"] == "healthy"


def test_ic_decay_report_custom_thresholds_override_defaults() -> None:
    monthly_ics = {
        "factor_a": [
            {"month": f"2025-0{index}", "ic": 0.04}
            for index in range(1, 5)
        ]
    }
    report = compute_ic_decay_from_monthly_ics(
        monthly_ics=monthly_ics,
        min_months=3,
        healthy_threshold=0.05,
        slope_threshold=-0.02,
    )
    factor = _as_mapping_list(report["factors"])[0]
    assert factor["health"] == "decaying"
    assert factor["reason"] == "recent_ic_mean_below_threshold"
    assert report["healthy_threshold"] == 0.05
    assert report["slope_threshold"] == -0.02


def test_ic_decay_report_from_records_builds_monthly_report() -> None:
    records = []
    closes = {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}
    offsets = 80
    for day in range(offsets):
        trade_date = f"2025-{1 + day // 28:02d}-{1 + day % 28:02d}"
        for symbol in ("AAA", "BBB", "CCC"):
            if symbol == "AAA":
                closes[symbol] *= 1.01
            elif symbol == "BBB":
                closes[symbol] *= 1.002
            else:
                closes[symbol] *= 0.998
            records.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "close": round(closes[symbol], 6),
                    "momentum_factor": (
                        0.5 if symbol == "AAA" else (0.3 if symbol == "BBB" else 0.1)
                    ),
                }
            )

    report = compute_ic_decay_report(records=records, horizon=1, lookback_months=12)
    assert report["status"] == "ok"
    assert report["horizon"] == 1
    assert report["lookback_months"] == 12
    factors = _as_mapping_list(report["factors"])
    assert any(str(item["factor"]) == "momentum_factor" for item in factors)
    for item in factors:
        assert "recent_ic_mean" in item
        assert "ic_slope" in item
        assert "ic_time_corr" in item
        assert "decay_rate" in item
        assert "health" in item
        assert item["health"] in {"healthy", "decaying", "insufficient_data"}


def test_compute_factor_ic_series_returns_daily_series_per_factor() -> None:
    records = []
    for day in range(6):
        trade_date = f"2026-03-0{day + 1}"
        records.extend(
            [
                {
                    "symbol": "AAA",
                    "trade_date": trade_date,
                    "close": 10.0 * (1.02**day),
                    "quality_factor": 0.9,
                },
                {
                    "symbol": "BBB",
                    "trade_date": trade_date,
                    "close": 10.0 * (1.01**day),
                    "quality_factor": 0.5,
                },
                {
                    "symbol": "CCC",
                    "trade_date": trade_date,
                    "close": 10.0 * (1.005**day),
                    "quality_factor": 0.2,
                },
            ]
        )
    series = compute_factor_ic_series(records=records, horizon=1)
    assert not series.empty
    assert "quality_factor" in series.columns
    assert series.index.name in {"trade_date", None} or series.index.name is None
    assert len(series.index) == len(series.index.unique())


def test_persist_ic_decay_report_writes_json(tmp_path: Path) -> None:
    report = {
        "status": "ok",
        "engine": "ic_decay_report",
        "factor_count": 1,
        "factors": [{"factor": "demo_factor", "health": "healthy"}],
        "summary": {"factors_total": 1, "factors_healthy": 1},
    }
    path = tmp_path / "research" / "ic_decay_report.json"
    written = persist_ic_decay_report(report=report, output_path=path)

    assert Path(written).exists() is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["factor_count"] == 1


def test_factor_lifecycle_config_ic_decay_defaults_match_prd() -> None:
    config = FactorLifecycleConfig()
    assert config.ic_decay_report == "monthly"
    assert config.ic_decay_lookback_months == 12
    assert config.ic_decay_min_months == 3
    assert config.ic_decay_healthy_threshold == 0.03
    assert config.ic_decay_slope_threshold == -0.005
    assert config.ic_decay_report_time == "21:00"

    loaded = load_config(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
    assert loaded.factor_lifecycle.ic_decay_report == "monthly"


def test_factor_lifecycle_config_ic_decay_mode_validation() -> None:
    assert FactorLifecycleConfig(ic_decay_report="off").ic_decay_report == ""
    assert FactorLifecycleConfig(ic_decay_report="").ic_decay_report == ""
    with pytest.raises(ValidationError):
        FactorLifecycleConfig(ic_decay_report="weekly")


def test_last_trading_day_of_month_predicate() -> None:
    assert _last_trading_day_of_month(date(2026, 1, 30)) is True
    assert _last_trading_day_of_month(date(2026, 1, 29)) is False
    assert _last_trading_day_of_month(date(2026, 3, 31)) is True
    assert _last_trading_day_of_month(date(2026, 3, 30)) is False
    assert _last_trading_day_of_month(date(2026, 5, 29)) is True
    assert _last_trading_day_of_month(date(2026, 5, 28)) is False
    assert _last_trading_day_of_month(date(2026, 6, 19)) is False
    assert _last_trading_day_of_month(date(2026, 10, 1)) is False
    assert _last_trading_day_of_month(date(2026, 12, 31)) is True


def test_factor_ic_decay_report_job_registered_with_monthly_predicate() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    service = StockAnalyzerService(config=config)

    job = service._scheduler._jobs.get("factor_ic_decay_report")
    assert job is not None
    assert job.trigger_time.isoformat() == "21:00:00"
    assert job.date_predicate is not None
    assert job.date_predicate(date(2026, 3, 31)) is True
    assert job.date_predicate(date(2026, 3, 30)) is False


def test_factor_ic_decay_report_job_skipped_when_disabled() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.factor_lifecycle.ic_decay_report = ""
    service = StockAnalyzerService(config=config)

    assert "factor_ic_decay_report" not in service._scheduler._jobs
