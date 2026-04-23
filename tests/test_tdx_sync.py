from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from struct import pack

from stock_analyzer.data.tdx_sync import inspect_tdx_source_freshness


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_inspect_tdx_source_freshness_reads_latest_daily_and_intraday(tmp_path: Path) -> None:
    vipdoc_root = tmp_path / "vipdoc"
    (vipdoc_root / "sh" / "lday").mkdir(parents=True, exist_ok=True)
    (vipdoc_root / "sh" / "fzline").mkdir(parents=True, exist_ok=True)

    day_path = vipdoc_root / "sh" / "lday" / "sh600000.day"
    day_path.write_bytes(pack("<I", 20260306) + b"\x00" * 28)

    lc5_date = (2026 - 2004) * 2048 + 2 * 100 + 6
    lc5_minutes = 15 * 60
    lc5_path = vipdoc_root / "sh" / "fzline" / "sh600000.lc5"
    lc5_path.write_bytes(pack("<HH", lc5_date, lc5_minutes) + b"\x00" * 28)

    lc1_date = (2026 - 2004) * 2048 + 2 * 100 + 6
    lc1_minutes = 14 * 60 + 59
    lc1_path = vipdoc_root / "sh" / "fzline" / "sh600000.lc1"
    lc1_path.write_bytes(pack("<HH", lc1_date, lc1_minutes) + b"\x00" * 28)

    freshness = inspect_tdx_source_freshness(vipdoc_root)
    daily = _as_mapping(freshness["daily"])
    minute_5 = _as_mapping(freshness["minute_5"])
    minute_1 = _as_mapping(freshness["minute_1"])

    assert daily["file_count"] == 1
    assert daily["latest_timestamp"] == "2026-03-06 00:00:00"
    assert minute_5["file_count"] == 1
    assert minute_5["latest_timestamp"] == "2026-02-06 15:00:00"
    assert minute_1["file_count"] == 1
    assert minute_1["latest_timestamp"] == "2026-02-06 14:59:00"
