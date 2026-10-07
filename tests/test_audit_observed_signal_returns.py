"""Research audit contracts: provenance, entry timing, and no future-label weights."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PATH = Path(__file__).parents[1] / "scripts/audit_observed_signal_returns.py"
_SPEC = importlib.util.spec_from_file_location("audit_observed_signal_returns", _PATH)
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["000001"] * 6,
            "date": pd.date_range("2026-09-01", periods=6),
            "open": [1.0, 100.0, 110.0, 120.0, 130.0, 140.0],
            "close": [1000.0, 101.0, 112.0, 123.0, 135.0, 150.0],
            "low": [0.5, 90.0, 100.0, 110.0, 120.0, 130.0],
        }
    )


def test_next_open_uses_no_signal_day_price_and_horizon_includes_entry_day() -> None:
    signals = pd.DataFrame({"symbol": ["000001"], "decision_day": [pd.Timestamp("2026-09-01")]})
    result = audit.attach_returns(signals, _bars()).iloc[0]
    assert result.entry_day == pd.Timestamp("2026-09-02")
    assert result.return_1 == pytest.approx(0.01)
    assert result.return_5 == pytest.approx(0.50)
    assert result.mae_5 == pytest.approx(-0.10)
    assert pd.isna(result.return_10)


def test_unmatured_and_missing_symbol_are_counted_not_treated_as_losses() -> None:
    signals = pd.DataFrame(
        {
            "symbol": ["000001", "missing"],
            "decision_day": pd.to_datetime(["2026-09-05", "2026-09-01"]),
        }
    )
    result = audit.attach_returns(signals, _bars())
    summary = audit.summarize(result, 5)
    assert summary["matured"] == 0
    assert summary["pending_or_missing_outcome"] == 2
    assert summary["missing_entry"] == 1
    assert summary["win_rate_gross"] is None


@pytest.mark.parametrize("defect", ["duplicate", "zero", "missing"])
def test_ambiguous_or_invalid_prices_fail_closed(defect: str) -> None:
    bars = _bars()
    if defect == "duplicate":
        bars = pd.concat([bars, bars.iloc[:1]])
    else:
        bars.loc[1, "open"] = 0 if defect == "zero" else np.nan
    signals = pd.DataFrame({"symbol": ["000001"], "decision_day": [pd.Timestamp("2026-09-01")]})
    with pytest.raises(ValueError):
        audit.attach_returns(signals, bars)


def test_replay_cannot_displace_observed_snapshot_in_dedup() -> None:
    snapshots = pd.DataFrame(
        {
            "snapshot_id": ["observed", "replayed"],
            "symbol": ["000001"] * 2,
            "strategy": ["trend"] * 2,
            "feature_capture_mode": ["observed_snapshot", "replayed_recompute"],
            "decision_day": pd.to_datetime(["2026-09-01"] * 2),
            "decision_time": ["2026-09-01T10:00:00Z", "2026-09-01T15:00:00Z"],
            "created_at": ["2026-09-01T10:00:00Z", "2026-10-01T10:00:00Z"],
        }
    )
    assert set(audit.deduplicate(snapshots).snapshot_id) == {"observed", "replayed"}


def test_exchange_timezone_and_forensic_clock_are_explicitly_different() -> None:
    values = pd.Series(["2026-09-01T22:00:00+00:00"])
    assert audit.decision_days(values, "exchange-tz").iloc[0] == pd.Timestamp("2026-09-02")
    assert audit.decision_days(values, "stored-wall-clock").iloc[0] == pd.Timestamp("2026-09-01")
    with pytest.raises(ValueError):
        audit.decision_days(values, "unknown")


def test_known_mislabelled_utc_clock_cannot_silently_use_timezone_conversion() -> None:
    snapshots = pd.DataFrame(
        {
            "decision_time": ["2026-09-01T22:00:00+00:00"],
            "created_at": ["2026-09-01T14:00:00+00:00"],
            "feature_capture_mode": ["observed_snapshot"],
        }
    )
    with pytest.raises(ValueError, match="audit timezone provenance"):
        audit.build_report(
            snapshots, _bars(), start="2026-09-01", end="2026-09-06", policy="exchange-tz"
        )


def test_topk_selects_before_outcome_and_only_once_per_stock() -> None:
    frame = pd.DataFrame(
        {
            "decision_day": pd.to_datetime(["2026-09-01"] * 3),
            "symbol": ["000001", "000001", "000002"],
            "strategy": ["trend", "monster", "trend"],
            "snapshot_id": ["a", "b", "c"],
            "meta": [1.0, 0.9, 0.8],
            "return_5": [np.nan, -0.9, 0.9],
        }
    )
    assert audit.top_k(frame, "meta", 2).snapshot_id.tolist() == ["a", "c"]


def test_walk_forward_excludes_entire_incompletely_matured_day() -> None:
    records = []
    for day in pd.date_range("2026-09-01", periods=5):
        for i in range(5):
            row = {
                "symbol": str(i),
                "decision_day": day,
                "return_5": i / 100,
                "exit_day_5": day + pd.Timedelta(days=1),
            }
            row.update({score: float(i) for score in audit.SCORES})
            records.append(row)
    frame = pd.DataFrame(records)
    frame.loc[
        frame.decision_day.eq(pd.Timestamp("2026-09-01")) & frame.symbol.eq("0"), "return_5"
    ] = np.nan
    result = audit.walk_forward(frame, min_history_days=1)
    assert result.decision_day.min() == pd.Timestamp("2026-09-04")
    assert (result.weight_training_last_exit < result.decision_day).all()
    # Changing future returns cannot change weights already used for a decision.
    frame.loc[frame.decision_day >= pd.Timestamp("2026-09-04"), "return_5"] *= -100
    changed = audit.walk_forward(frame, min_history_days=1)
    np.testing.assert_allclose(
        result.loc[result.decision_day.eq(pd.Timestamp("2026-09-04")), "wf_ic_mix"],
        changed.loc[changed.decision_day.eq(pd.Timestamp("2026-09-04")), "wf_ic_mix"],
    )
