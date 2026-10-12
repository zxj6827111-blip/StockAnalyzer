from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.tail_observed_inputs import (
    SCHEMA,
    write_tail_observed_input_snapshot,
)


def test_snapshot_is_written_only_at_latest_confirmation_slot(tmp_path: Path) -> None:
    timestamp = datetime(2026, 10, 9, 14, 45, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [{"symbol": "600000", "features": {"atr14_pct": 0.02}}]
    bars = {"600000": [(timestamp, {"close": 10.2, "volume": 1000})]}

    result = write_tail_observed_input_snapshot(
        tmp_path,
        timestamp=timestamp,
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        watch_pool=rows,
        minute_bars=bars,
    )

    assert result is not None
    assert result["status"] == "captured"
    assert result["full_chain_pit_evidence"] is False
    path = Path(result["path"])
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == result["sha256"]
    payload = json.loads(content)
    assert payload["schema"] == SCHEMA
    assert payload["capture_mode"] == "observed_snapshot"
    assert payload["capture_scope"] == "tail_shadow_watch_pool_and_minute_inputs_only"
    assert payload["watch_pool"] == rows
    assert payload["minute_bars"]["600000"][0]["bar"]["close"] == 10.2

    second = write_tail_observed_input_snapshot(
        tmp_path,
        timestamp=timestamp,
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        watch_pool=rows,
        minute_bars=bars,
    )
    assert second == {"status": "blocked", "reason": "snapshot_already_exists"}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["sha256"]


def test_snapshot_skips_other_slots_and_post_cutoff_runs(tmp_path: Path) -> None:
    for timestamp in (
        datetime(2026, 10, 9, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 10, 9, 14, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 10, 9, 14, 45),
    ):
        assert write_tail_observed_input_snapshot(
            tmp_path,
            timestamp=timestamp,
            trade_date=date(2026, 10, 9),
            contract=DEFAULT_TREND_CONTRACT,
            watch_pool=[],
            minute_bars={},
        ) is None
    assert not (tmp_path / "observed_inputs").exists()


def test_snapshot_rejects_trade_date_mismatch(tmp_path: Path) -> None:
    result = write_tail_observed_input_snapshot(
        tmp_path,
        timestamp=datetime(2026, 10, 9, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
        trade_date=date(2026, 10, 8),
        contract=DEFAULT_TREND_CONTRACT,
        watch_pool=[],
        minute_bars={},
    )
    assert result is None
    assert not (tmp_path / "observed_inputs").exists()
