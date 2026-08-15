"""P1 Warehouse 更新闭环：warehouse_freshness.json + 陈旧数据禁止开仓。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from stock_analyzer.ops.warehouse_freshness import (
    DEFAULT_STALE_TRADE_DAYS,
    freshness_status,
    open_position_blocked_by_stale_data,
    read_warehouse_freshness,
    write_warehouse_freshness,
)


def _freshness(
    *,
    source: str,
    date_max: date,
    updated_at: str = "2026-08-14T21:45:00",
    verification: str = "ok",
    row_count: int = 5000,
) -> dict[str, object]:
    return {
        "source": source,
        "date_max": date_max.isoformat(),
        "updated_at": updated_at,
        "verification_status": verification,
        "row_count": row_count,
        "data_source": "tushare",
    }


def test_write_and_read_freshness_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "runtime" / "warehouse_freshness.json"
    written = write_warehouse_freshness(
        path=path,
        source="delta",
        date_max=date(2026, 8, 13),
        updated_at=datetime(2026, 8, 13, 21, 45, 0),
        verification_status="ok",
        row_count=5400,
        data_source="tushare",
    )
    assert written.exists()
    payload = read_warehouse_freshness(path)
    assert payload is not None
    assert payload["source"] == "delta"
    assert payload["date_max"] == "2026-08-13"
    assert payload["row_count"] == 5400


def test_freshness_status_fresh_delta() -> None:
    freshness = _freshness(source="delta", date_max=date(2026, 8, 13))
    status = freshness_status(freshness, now=date(2026, 8, 14))
    assert status.ok is True
    assert status.source == "delta"
    assert status.stale_trade_days == 1


def test_freshness_status_package_fallback_stale_blocks() -> None:
    # fallback（package）落后 3 个交易日 → 超过 2 → 禁止开仓
    freshness = _freshness(source="package", date_max=date(2026, 8, 11))
    status = freshness_status(freshness, now=date(2026, 8, 14))
    assert status.ok is False
    assert status.stale_trade_days == 3
    assert status.reason.startswith("data_stale_")


def test_freshness_missing_blocks_open() -> None:
    gate = open_position_blocked_by_stale_data(None, now=date(2026, 8, 14))
    assert gate["blocked"] is True
    assert gate["reason"] == "freshness_artifact_missing"


def test_fresh_date_max_missing_blocks_open() -> None:
    gate = open_position_blocked_by_stale_data(
        {"source": "delta", "date_max": ""},
        now=date(2026, 8, 14),
    )
    assert gate["blocked"] is True
    assert gate["reason"] == "date_max_missing"


def test_fresh_delta_allows_open() -> None:
    gate = open_position_blocked_by_stale_data(
        _freshness(source="delta", date_max=date(2026, 8, 13)),
        now=date(2026, 8, 14),
    )
    assert gate["blocked"] is False
    assert gate["source"] == "delta"
    assert gate["stale_trade_days"] == 1


def test_stale_within_limit_allows_open() -> None:
    # 落后 2 个交易日恰好等于阈值 → 不阻断
    freshness = _freshness(source="package", date_max=date(2026, 8, 12))
    gate = open_position_blocked_by_stale_data(freshness, now=date(2026, 8, 14))
    assert gate["blocked"] is False
    assert gate["stale_trade_days"] == 2


def test_default_stale_trade_days_is_two() -> None:
    assert DEFAULT_STALE_TRADE_DAYS == 2


def test_weekend_is_not_a_trade_day() -> None:
    # 周五数据，周一判定：只隔一个交易日
    freshness = _freshness(source="delta", date_max=date(2026, 8, 14))
    status = freshness_status(freshness, now=date(2026, 8, 17))
    assert status.stale_trade_days == 1
    assert status.ok is True
