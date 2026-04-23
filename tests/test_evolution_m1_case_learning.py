from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from stock_analyzer.evolution.modules.m1_case_learning import run_m1_dual_learning


def test_m1_dual_learning_counts_buckets_and_poison(tmp_path: Path) -> None:
    result = run_m1_dual_learning(
        records=[
            {
                "symbol": "600000.SH",
                "realized_return": -0.03,
                "leak_flag": False,
                "available_at": "2026-03-01T10:00:00+00:00",
                "heat_ratio": 1.40,
                "ret_20d": 0.15,
                "recent_high_ratio": 0.98,
            },
            {
                "symbol": "000001.SZ",
                "realized_return": -0.12,
                "leak_flag": True,
                "available_at": "2026-03-01T11:00:00+00:00",
                "pressure_index": 0.66,
                "bearish_ratio": 0.61,
                "prediction_spread": 0.28,
                "financial_data_complete": False,
            },
        ],
        asof_date=date(2026, 3, 1),
        shared_dir=tmp_path / "shared",
        now=datetime(2026, 3, 1, tzinfo=UTC),
    )
    assert result.poison_hits == 1
    assert result.bucket_counts["mild"] == 1
    assert result.bucket_counts["severe"] == 1
    assert result.negative_case_count == 2
    assert result.reason_counts["chase_high"] >= 1
    assert result.reason_counts["high_sell_pressure"] >= 1
    assert result.reason_counts["model_divergence"] >= 1
    assert result.reason_counts["data_incomplete"] >= 1
    assert len(result.cases_preview) == 2
    assert "reason_codes" in result.cases_preview[0]
    assert result.shared_payload_uri is not None
    assert Path(result.shared_payload_uri).exists() is True


def test_m1_dual_learning_asof_violation_forces_low_score() -> None:
    result = run_m1_dual_learning(
        records=[
            {
                "symbol": "600000.SH",
                "realized_return": -0.02,
                "available_at": "2026-03-02T00:30:00+00:00",
            }
        ],
        asof_date=date(2026, 3, 1),
    )
    assert result.asof_pass is False
    assert result.score <= 40.0
