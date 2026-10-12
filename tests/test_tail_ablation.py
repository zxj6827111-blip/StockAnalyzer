from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.run_tail_model_ablation import _load_calendar
from stock_analyzer.models.tail_net_profit_trainer import KIND_LIGHTGBM, KIND_LOGISTIC
from stock_analyzer.research.tail_ablation import (
    holm_adjusted_p_values,
    select_stage2_candidate,
    stage2_arm_registry,
)
from stock_analyzer.research.tail_walk_forward import (
    select_training_features_by_correlation,
)


def test_stage2_calendar_separates_label_maturity_from_decision_coverage(tmp_path: Path) -> None:
    path = tmp_path / "calendar.csv"
    path.write_text(
        "trade_date,is_open,is_decision_session\n"
        "2026-05-28,1,1\n"
        "2026-05-29,1,0\n",
        encoding="utf-8",
    )
    assert _load_calendar(path) == (
        [date(2026, 5, 28), date(2026, 5, 29)],
        [date(2026, 5, 28)],
    )


def test_stage2_calendar_rejects_decision_coverage_on_closed_date(tmp_path: Path) -> None:
    path = tmp_path / "calendar.csv"
    path.write_text(
        "trade_date,is_open,is_decision_session\n2026-05-29,0,1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="closed date"):
        _load_calendar(path)


def test_stage2_matrix_registers_all_twelve_arms_and_blocks_unproven_event_inputs() -> None:
    arms = stage2_arm_registry()
    assert len(arms) == 12
    assert {arm["model_kind"] for arm in arms} == {KIND_LOGISTIC, KIND_LIGHTGBM}
    event_arms = [arm for arm in arms if arm["config_id"] == "all_price_plus_moneyflow_top_list"]
    assert len(event_arms) == 2
    assert all(arm["status"] == "blocked" for arm in event_arms)
    assert all(
        arm["blockers"] == ["event_feature_point_in_time_availability_not_proven"]
        for arm in event_arms
    )


def test_holm_adjustment_uses_fixed_family_size_and_is_monotone() -> None:
    adjusted = holm_adjusted_p_values(
        {"a": 0.01, "b": 0.04, "blocked": None},
        family_size=12,
    )
    assert adjusted["a"] == pytest.approx(0.12)
    assert adjusted["b"] == pytest.approx(0.44)
    assert adjusted["blocked"] == 1.0


def test_training_feature_dedup_uses_only_the_training_window() -> None:
    origin = date(2025, 1, 1)
    train_dates = [origin + timedelta(days=index) for index in range(40)]
    rows = [
        {
            "entry_date": day,
            "label": index % 2,
            "a": float(index),
            "b": float(index * 2),
        }
        for index, day in enumerate(train_dates)
    ]
    # In the held-out dates, b is deliberately unrelated. It cannot reverse the
    # training-only feature choice.
    for index in range(10):
        rows.append(
            {
                "entry_date": origin + timedelta(days=50 + index),
                "label": index % 2,
                "a": float(index),
                "b": float((-1) ** index * index),
            }
        )
    selected, report = select_training_features_by_correlation(
        rows,
        ("a", "b"),
        train_dates=train_dates,
        minimum_pair_rows=30,
    )
    assert selected == ("a",)
    assert report["scope"] == "current_fold_train_only"
    assert report["training_labeled_rows"] == 40
    assert report["dropped_correlated_pairs"][0]["correlation"] == pytest.approx(1.0)


def _candidate_arm(
    arm_id: str, model: str, *, oos_return: float, feature_count: int, adjusted_p: float = 0.01
) -> dict:
    return {
        "arm_id": arm_id,
        "model_kind": model,
        "status": "completed",
        "holm_adjusted_p": adjusted_p,
        "report": {
            "quality": {"passed": True},
            "fold_count": 4,
            "folds": [
                {
                    "artifact_roundtrip": {"status": "passed"},
                    "selection_metrics": {
                        "p_net_profit_5d_tail": {"mean_net_return": oos_return},
                    },
                    "feature_selection": {
                        "kept_features": [f"f{index}" for index in range(feature_count)],
                    },
                }
                for _ in range(4)
            ],
        },
    }


def test_candidate_selection_uses_oos_return_then_features_then_logistic() -> None:
    arms = [
        _candidate_arm("lr", KIND_LOGISTIC, oos_return=0.05, feature_count=2),
        _candidate_arm("lgbm", KIND_LIGHTGBM, oos_return=0.05, feature_count=2),
        _candidate_arm("higher_return", KIND_LIGHTGBM, oos_return=0.06, feature_count=8),
    ]
    report = select_stage2_candidate(arms)
    assert report["selected_arm_id"] == "higher_return"
    assert report["release_eligible"] is False

    tie_report = select_stage2_candidate(arms[:2])
    assert tie_report["selected_arm_id"] == "lr"


def test_candidate_selection_requires_all_development_and_holm_gates() -> None:
    arms = [_candidate_arm("lr", KIND_LOGISTIC, oos_return=0.05, feature_count=2, adjusted_p=0.05)]
    report = select_stage2_candidate(arms)
    assert report["status"] == "no_qualified_candidate"
    assert arms[0]["selection_eligible"] is False
    assert "development_holm_adjusted_p_not_below_0_05" in arms[0]["selection_blockers"]
