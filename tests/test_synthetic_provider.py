from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from stock_analyzer.data import provider as provider_module
from stock_analyzer.data.provider import SyntheticProvider


def test_synthetic_provider_handles_weekend_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WeekendDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> _WeekendDateTime:
            _ = tz
            return cls(2026, 3, 7, 10, 0, 0)

    monkeypatch.setattr(provider_module, "datetime", _WeekendDateTime)

    provider = SyntheticProvider(seed_offset=123)
    frame = provider.fetch_daily_bars(symbol="600000", lookback_days=120)
    expected_index = pd.bdate_range(end=_WeekendDateTime.now().date(), periods=120)

    assert len(frame) == len(expected_index)
    assert frame.index.name == "date"
    assert len(frame.columns) >= 10
