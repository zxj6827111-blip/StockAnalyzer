from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analyzer.config import Week6MainForceConfig
from stock_analyzer.week6.engines import MainForceTracker


def test_main_force_tracker_outputs_completion_score_and_factors() -> None:
    dates = pd.bdate_range("2026-01-02", periods=40)
    close = np.linspace(10.0, 12.0, num=40)
    volume = np.linspace(2_000_000, 1_200_000, num=40)
    turnover = close * volume

    bars = pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "turnover": turnover,
            "volume": volume,
            "holder_count": np.linspace(100_000, 80_000, num=40),
            "block_trade_net": np.linspace(5_000_000, 8_000_000, num=40),
            "financing_balance": np.linspace(3_000_000_000, 2_700_000_000, num=40),
        },
        index=dates,
    )

    tracker = MainForceTracker(config=Week6MainForceConfig(lookback_days=40, strong_score=50))
    report = tracker.analyze_symbol(symbol="600000", bars=bars)
    assert "completion_score" in report
    assert "completion_factors" in report
    completion_factors = report["completion_factors"]
    completion_score = report["completion_score"]
    assert isinstance(completion_factors, dict)
    assert isinstance(completion_score, (int, float))
    assert float(completion_score) >= 0.0
