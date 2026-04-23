from __future__ import annotations

import pandas as pd

from stock_analyzer.labels.soup import (
    build_soup_labels,
    detect_soup_label_same_bar_conflicts,
)


def test_soup_labels_mark_tp_before_sl() -> None:
    dates = pd.bdate_range("2025-01-01", periods=8)
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.2, 10.1, 10.0, 10.1, 10.2, 10.3, 10.4],
            "high": [10.0, 10.6, 10.2, 10.1, 10.2, 10.3, 10.4, 10.5],
            "low": [10.0, 10.1, 9.8, 9.9, 10.0, 10.1, 10.2, 10.3],
        },
        index=dates,
    )

    labels = build_soup_labels(bars, take_profit_pct=0.05, stop_loss_pct=0.05, horizon_days=3)
    assert float(labels.iloc[0]) == 1.0


def test_soup_labels_mark_sl_before_tp() -> None:
    dates = pd.bdate_range("2025-01-01", periods=8)
    bars = pd.DataFrame(
        {
            "close": [10.0, 9.9, 10.1, 10.2, 10.1, 10.0, 9.9, 9.8],
            "high": [10.0, 10.1, 10.2, 10.3, 10.2, 10.1, 10.0, 9.9],
            "low": [10.0, 9.4, 9.8, 10.0, 9.9, 9.8, 9.7, 9.6],
        },
        index=dates,
    )

    labels = build_soup_labels(bars, take_profit_pct=0.05, stop_loss_pct=0.05, horizon_days=3)
    assert float(labels.iloc[0]) == 0.0


def test_soup_labels_support_next_tradable_vwap_basis() -> None:
    dates = pd.bdate_range("2025-01-01", periods=8)
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.1, 10.2, 10.4, 10.6, 10.7, 10.8, 10.9],
            "high": [10.1, 10.2, 10.5, 10.6, 10.8, 10.9, 11.0, 11.0],
            "low": [9.9, 10.0, 10.0, 10.2, 10.4, 10.5, 10.6, 10.7],
            "suspended": [False, True, False, False, False, False, False, False],
            "vwap": [10.0, 10.1, 10.25, 10.35, 10.55, 10.65, 10.75, 10.85],
        },
        index=dates,
    )
    labels = build_soup_labels(
        bars,
        take_profit_pct=0.03,
        stop_loss_pct=0.03,
        horizon_days=3,
        price_basis="next_tradable_vwap",
        exclude_untradable=True,
    )
    assert labels.notna().sum() >= 1


def test_soup_labels_support_bar_shape_heuristic_conflict_policy() -> None:
    dates = pd.bdate_range("2025-01-01", periods=6)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.3, 10.2, 10.1, 10.0],
            "close": [10.0, 10.4, 10.2, 10.1, 10.0, 9.9],
            "high": [10.0, 10.6, 10.5, 10.3, 10.2, 10.1],
            "low": [10.0, 9.4, 10.0, 10.0, 9.9, 9.8],
        },
        index=dates,
    )

    labels = build_soup_labels(
        bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
        conflict_policy="bar_shape_heuristic",
    )
    assert float(labels.iloc[0]) == 1.0


def test_soup_labels_support_soft_label_conflict_policy() -> None:
    dates = pd.bdate_range("2025-01-01", periods=6)
    bars = pd.DataFrame(
        {
            "close": [10.0, 10.1, 10.0, 9.9, 9.8, 9.7],
            "high": [10.0, 10.6, 10.1, 10.0, 9.9, 9.8],
            "low": [10.0, 9.4, 9.9, 9.8, 9.7, 9.6],
        },
        index=dates,
    )

    labels = build_soup_labels(
        bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
        conflict_policy="soft_label",
        conflict_soft_label_value=0.5,
    )
    assert float(labels.iloc[0]) == 0.5


def test_detect_soup_label_same_bar_conflicts_marks_conflict_rows() -> None:
    dates = pd.bdate_range("2025-01-01", periods=6)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.3, 10.2, 10.1, 10.0],
            "close": [10.0, 10.4, 10.2, 10.1, 10.0, 9.9],
            "high": [10.0, 10.6, 10.5, 10.3, 10.2, 10.1],
            "low": [10.0, 9.4, 10.0, 10.0, 9.9, 9.8],
        },
        index=dates,
    )

    conflicts = detect_soup_label_same_bar_conflicts(
        bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=2,
    )

    assert bool(conflicts.iloc[0]) is True
    assert conflicts.sum() >= 1
