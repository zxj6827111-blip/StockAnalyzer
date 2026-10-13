from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.tail_observed_inputs import (
    RUNTIME_CONTEXT_SCHEMA,
    SCHEMA,
    write_tail_observed_input_snapshot,
    write_tail_observed_runtime_context_snapshot,
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


def test_runtime_context_is_saved_but_keeps_candidate_scope_limitation(tmp_path: Path) -> None:
    timestamp = datetime(2026, 10, 9, 14, 45, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    context = {
        "schema": "tail_live_runtime_context.v1",
        "symbols": ["600000.SH"],
        "pipeline_report": {"signals": [{"symbol": "600000.SH"}]},
    }

    result = write_tail_observed_input_snapshot(
        tmp_path,
        timestamp=timestamp,
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        watch_pool=[{"symbol": "600000.SH"}],
        minute_bars={},
        runtime_context=context,
    )

    assert result is not None and result["status"] == "captured"
    assert result["capture_scope"] == "tail_shadow_watch_pool_minute_and_runtime_context"
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["runtime_context"] == context
    assert "runtime_context_is_not_per_field_pit_evidence" in payload["limitations"]
    assert payload["full_chain_pit_evidence"] is False


def test_runtime_context_sidecar_is_linked_and_only_written_inside_decision_slot(
    tmp_path: Path,
) -> None:
    timestamp = datetime(2026, 10, 9, 14, 45, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    input_snapshot = write_tail_observed_input_snapshot(
        tmp_path,
        timestamp=timestamp,
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        watch_pool=[{"symbol": "600000.SH"}],
        minute_bars={},
    )
    assert input_snapshot is not None
    context = {"schema": "tail_live_runtime_context.v1", "symbols": ["600000.SH"]}

    result = write_tail_observed_runtime_context_snapshot(
        tmp_path,
        timestamp=timestamp,
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        input_snapshot=input_snapshot,
        runtime_context=context,
    )

    assert result["status"] == "captured"
    path = Path(result["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == RUNTIME_CONTEXT_SCHEMA
    assert payload["capture_scope"] == "candidate_pipeline_runtime_context_only"
    assert payload["input_snapshot"]["sha256"] == input_snapshot["sha256"]
    assert payload["runtime_context"] == context
    assert payload["full_chain_pit_evidence"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["sha256"]

    duplicate = write_tail_observed_runtime_context_snapshot(
        tmp_path,
        timestamp=timestamp,
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        input_snapshot=input_snapshot,
        runtime_context=context,
    )
    assert duplicate == {
        "status": "blocked",
        "reason": "runtime_context_snapshot_already_exists",
    }
    late = write_tail_observed_runtime_context_snapshot(
        tmp_path,
        timestamp=datetime(2026, 10, 9, 14, 46, tzinfo=ZoneInfo("Asia/Shanghai")),
        trade_date=date(2026, 10, 9),
        contract=DEFAULT_TREND_CONTRACT,
        input_snapshot=input_snapshot,
        runtime_context=context,
    )
    assert late == {
        "status": "not_persisted",
        "reason": "pipeline_completed_outside_decision_minute",
    }
