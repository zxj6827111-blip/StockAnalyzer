from datetime import datetime

import pandas as pd

from stock_analyzer.runtime.market_outcomes import summarize_market_observation


def test_market_observation_computes_path_without_creating_execution_outcome() -> None:
    bars = pd.DataFrame(
        {
            "high": [10.2, 10.8, 11.0, 10.9],
            "low": [9.8, 9.7, 10.1, 10.0],
            "close": [10.0, 10.5, 10.7, 10.6],
        },
        index=pd.date_range("2026-03-02", periods=4, freq="B"),
    )
    outcome = summarize_market_observation(
        bars=bars,
        recommended_at=datetime.fromisoformat("2026-03-02T09:30:00"),
        observed_at=datetime.fromisoformat("2026-03-06T15:00:00"),
        reference_price=10.0,
        horizon_days=3,
    )

    assert outcome["status"] == "observed"
    assert outcome["expiry_return_pct"] == 0.06
    assert outcome["maximum_favorable_excursion_pct"] == 0.1
    assert "trade_id" not in outcome


def test_market_observation_explains_missing_mature_path() -> None:
    outcome = summarize_market_observation(
        bars=pd.DataFrame(),
        recommended_at=datetime.fromisoformat("2026-03-02T09:30:00"),
        observed_at=datetime.fromisoformat("2026-03-30T15:00:00"),
        reference_price=10.0,
        horizon_days=7,
    )

    assert outcome == {"status": "pending", "pending_reason": "market_path_missing"}
