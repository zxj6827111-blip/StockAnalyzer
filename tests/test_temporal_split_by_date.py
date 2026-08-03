"""P1-2 regression tests: temporal split must group by trading dates.

The default temporal split is anchored on unique trading dates (never row
counts), every row of the same date lands in exactly one set, embargo_days
excludes whole date groups, the input does not need to be date-contiguous or
sorted, and unparseable trading dates fail loudly instead of falling back to
a row-count split.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.config import TrainingConfig
from stock_analyzer.models.trainer import ModelTrainer, _extract_trading_dates


def _panel_frame() -> pd.DataFrame:
    symbols = ["600000", "000001", "300750", "600519"]
    dates = pd.bdate_range(start="2024-01-02", periods=26)
    rows: list[tuple[str, pd.Timestamp, int]] = []
    for index, ts in enumerate(dates):
        for symbol in symbols:
            rows.append((symbol, ts, index))
    index = pd.MultiIndex.from_tuples(
        [(symbol, ts) for symbol, ts, _ in rows],
        names=["symbol", "date"],
    )
    frame = pd.DataFrame({"value": np.arange(len(rows), dtype=float)}, index=index)
    return frame


def _trainer() -> ModelTrainer:
    training = TrainingConfig(calibration_ratio=0.15, test_ratio=0.15)
    labels = _labels()
    return ModelTrainer(training=training, labels=labels)


def _labels() -> object:
    from stock_analyzer.config import LabelsConfig

    return LabelsConfig(horizon_days=3)


def _split_dates(split: object) -> tuple[list[str], list[str], list[str], list[str]]:
    return (
        list(split.train_dates),
        list(split.calibration_dates),
        list(split.test_dates),
        list(split.embargo_dates),
    )


def test_panel_split_groups_each_trading_date_into_exactly_one_set() -> None:
    frame = _panel_frame()
    split = _trainer()._build_temporal_split(aligned=frame)
    train_dates, calibration_dates, test_dates, _ = _split_dates(split)

    assert len(train_dates) >= 1 and len(calibration_dates) >= 1 and len(test_dates) >= 1
    train_set = set(train_dates)
    calibration_set = set(calibration_dates)
    test_set = set(test_dates)
    assert train_set.isdisjoint(calibration_set)
    assert train_set.isdisjoint(test_set)
    assert calibration_set.isdisjoint(test_set)

    unique_dates = sorted(set(_extract_trading_dates(frame)))
    embargo_set = {item.isoformat() for item in unique_dates} - set(split.embargo_dates)
    assigned = train_set | calibration_set | test_set
    assert assigned == embargo_set

    rows = frame.index.get_level_values("date").to_numpy()
    train_mask = split.train_mask
    calibration_mask = split.calibration_mask
    test_mask = split.test_mask
    assert len(train_mask) == len(rows) == len(calibration_mask) == len(test_mask)
    for date_value, is_train, is_calibration, is_test in zip(
        rows, train_mask, calibration_mask, test_mask, strict=True
    ):
        if pd.Timestamp(date_value).date().isoformat() in set(split.embargo_dates):
            assert not is_train and not is_calibration and not is_test
            continue
        total = int(is_train) + int(is_calibration) + int(is_test)
        assert total == 1, f"date {date_value} appears in {total} sets"
        expected = next(
            name
            for name, date_set in (
                ("train", train_set),
                ("calibration", calibration_set),
                ("test", test_set),
            )
            if pd.Timestamp(date_value).date().isoformat() in date_set
        )
        selected = {
            name: flag
            for name, flag in (
                ("train", is_train),
                ("calibration", is_calibration),
                ("test", is_test),
            )
        }
        assert bool(selected[expected]) is True


def test_embargo_dates_are_excluded_from_calibration_and_test() -> None:
    frame = _panel_frame()
    split = _trainer()._build_temporal_split(aligned=frame)
    train_dates, calibration_dates, test_dates, embargo_dates = _split_dates(split)

    embargo_set = set(embargo_dates)
    assert embargo_set.isdisjoint(set(calibration_dates))
    assert embargo_set.isdisjoint(set(test_dates))
    assert embargo_set.isdisjoint(set(train_dates))
    assert len(embargo_dates) == split.embargo_days


def test_double_sided_embargo_between_train_and_calibration() -> None:
    frame = _panel_frame()
    split = _trainer()._build_temporal_split(aligned=frame)
    train_dates, calibration_dates, _, embargo_dates = _split_dates(split)

    assert train_dates and calibration_dates and embargo_dates
    train_max = max(pd.Timestamp(item) for item in train_dates)
    calibration_min = min(pd.Timestamp(item) for item in calibration_dates)
    assert train_max < calibration_min
    # every trading date strictly between train and calibration is embargo
    all_dates = sorted(
        {
            pd.Timestamp(item)
            for item in split.train_dates + split.embargo_dates + split.calibration_dates
        }
    )
    gap_dates = [item for item in all_dates if train_max < item < calibration_min]
    assert gap_dates
    assert {item.date().isoformat() for item in gap_dates} <= set(embargo_dates)
    # the gap between train and calibration is as wide as the configured embargo
    assert len(gap_dates) == split.embargo_days // 2


def test_embargo_days_are_trading_days_not_rows() -> None:
    frame = _panel_frame()  # 4 symbols per trading date
    split = _trainer()._build_temporal_split(aligned=frame)
    embargo_rows = int(np.count_nonzero(split.embargo_mask))
    assert split.embargo_days == len(split.embargo_dates)
    assert embargo_rows == 4 * split.embargo_days
    assert embargo_rows != split.embargo_days


def test_shuffled_row_order_splits_identically_to_sorted() -> None:
    frame = _panel_frame()
    split_sorted = _trainer()._build_temporal_split(aligned=frame)

    shuffled = frame.sample(frac=1.0, random_state=5)
    split_shuffled = _trainer()._build_temporal_split(aligned=shuffled)

    assert sorted(split_sorted.train_dates) == sorted(split_shuffled.train_dates)
    assert sorted(split_sorted.calibration_dates) == sorted(split_shuffled.calibration_dates)
    assert sorted(split_sorted.test_dates) == sorted(split_shuffled.test_dates)
    assert sorted(split_sorted.embargo_dates) == sorted(split_shuffled.embargo_dates)


def test_sample_weight_is_aligned_with_split_masks() -> None:
    frame = _panel_frame()
    split = _trainer()._build_temporal_split(aligned=frame)
    weights = np.arange(len(frame), dtype=float)

    train_weight_rows = weights[split.train_mask]
    selected_rows = frame.index.get_level_values("date").to_numpy()[split.train_mask]
    assert len(train_weight_rows) == len(selected_rows)
    assert all(
        weight == index
        for weight, index in zip(
            train_weight_rows,
            np.arange(len(frame))[split.train_mask],
            strict=True,
        )
    )


def test_unparseable_dates_fail_loudly_without_row_count_fallback() -> None:
    frame = pd.DataFrame(
        {"value": [1.0, 2.0, 3.0]},
        index=["not-a-date", "also-not-a-date", "nope"],
    )
    with pytest.raises(ValueError, match="trading dates"):
        _trainer()._build_temporal_split(aligned=frame)


def test_integer_index_fails_loudly_instead_of_epoch_misparse() -> None:
    frame = pd.DataFrame({"value": [1.0, 2.0, 3.0]}, index=[10, 20, 30])
    with pytest.raises(ValueError, match="integer index"):
        _trainer()._build_temporal_split(aligned=frame)


def test_extract_trading_dates_multiindex_decision_time_level() -> None:
    times = pd.to_datetime(["2024-01-02 09:30:00", "2024-01-03 09:30:00"])
    frame = pd.DataFrame(
        {"value": [1.0, 2.0]},
        index=pd.MultiIndex.from_tuples(
            [("600000", ts) for ts in times],
            names=["symbol", "decision_time"],
        ),
    )
    dates = _extract_trading_dates(frame)
    assert dates == [date(2024, 1, 2), date(2024, 1, 3)]


def test_temporal_split_rejects_multiindex_without_date_level() -> None:
    frame = pd.DataFrame(
        {"value": [1.0, 2.0]},
        index=pd.MultiIndex.from_tuples([("a", "x"), ("b", "y")], names=["symbol", "snapshot_id"]),
    )
    with pytest.raises(ValueError, match="MultiIndex has no decision_time/date level"):
        _trainer()._build_temporal_split(aligned=frame)


def test_trainer_metrics_report_embargo_days_not_rows() -> None:
    from stock_analyzer.config import LabelsConfig, TrainingConfig

    dates = pd.bdate_range(start="2024-01-02", periods=22)
    symbols = ["600000", "000001"]
    rows = [(symbol, ts) for ts in dates for symbol in symbols]
    index = pd.MultiIndex.from_tuples(rows, names=["symbol", "date"])
    rng = np.random.default_rng(17)
    features = pd.DataFrame(
        {f"f{i}": rng.normal(size=len(rows)) for i in range(4)},
        index=index,
    )
    label = pd.Series(
        rng.integers(0, 2, size=len(rows)).astype(float),
        index=index,
        name="label_soup_tp_before_sl",
    )
    training = TrainingConfig(calibration_ratio=0.2, test_ratio=0.2, min_samples=8)
    labels = LabelsConfig(horizon_days=2)
    trainer = ModelTrainer(training=training, labels=labels)
    result = trainer.train_on_feature_label(features=features, labels=label)

    assert result.metrics["embargo_days"] >= 1
    # rows are strictly larger than trading days on a 2-symbol panel, so a
    # row-count-based embargo_days would violate the strict inequality.
    assert result.metrics["embargo_rows"] > result.metrics["embargo_days"]
    assert result.metrics["embargo_rows"] == 2 * result.metrics["embargo_days"]
    assert result.artifact.metadata["embargo_days"] == int(result.metrics["embargo_days"])
    assert result.artifact.metadata["embargo_rows"] == int(result.metrics["embargo_rows"])
