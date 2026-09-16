"""`scripts/sync_market_duckdb.py --minute` 的缺口检测与**拒绝空转**契约。

背景（2026-09-15 深夜定位）：容器里没有 `/data/qq_minute_raw` 挂载，于是
`_missing_minute_dates` 的 `zip_dates` 恒为空 → 缺口恒为空 → 每天打印
`no missing dates`，而 `intraday_summary_1m/5m` 停在 8/28，静默 18 天。
本文件钉住两件事：

1. 源目录不存在 → **非零退出**（"没有缺口"与"看不到源"必须可分）；
2. 缺口 = zip 日期 − 库里已有日期，且 `--since` 之前的不参与。
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sync_market_duckdb", REPO_ROOT / "scripts" / "sync_market_duckdb.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path, dates: list[str], *, table: str | None = None) -> None:
    """建两张汇总表；``table=None`` 时把日期写进**两张**表（= 该日期已完成）。

    2026-09-16 起缺口检测是**交集**语义（所有目标表都有才算完成），所以"已完成"的夹具
    必须两张表都写——只写 1m 现在表示"半写、仍缺 5m"。
    """
    targets = [table] if table else ["intraday_summary_1m", "intraday_summary_5m"]
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE intraday_summary_1m (symbol VARCHAR, date DATE)")
        con.execute("CREATE TABLE intraday_summary_5m (symbol VARCHAR, date DATE)")
        for value in dates:
            for target in targets:
                con.execute(f"INSERT INTO {target} VALUES ('600000', ?)", [value])
    finally:
        con.close()


def test_half_written_date_is_still_a_gap(tmp_path: Path, monkeypatch) -> None:
    """1m 有、5m 缺的日期必须仍算缺口。

    原实现只拿 `intraday_summary_1m` 判缺口，写入端却逐日写 1m+5m——"写 2 张、只查 1 张"
    会让部分缺失**静默留存**（与 8/28 那次静默空转同一类）。
    """
    module = _load_module()
    db = tmp_path / "market.duckdb"
    _make_db(db, ["2026-09-15"], table="intraday_summary_1m")  # 只有 1m
    monkeypatch.setattr(module, "MARKET_DB", str(db))
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    (zip_root / "minute_1m_20260915.zip").write_bytes(b"")

    assert module._missing_minute_dates(zip_root, date(2026, 9, 1)) == [date(2026, 9, 15)]


def test_fully_written_date_is_not_a_gap(tmp_path: Path, monkeypatch) -> None:
    """1m 与 5m 都有的日期不算缺口（别把完整日期反复重写）。"""
    module = _load_module()
    db = tmp_path / "market.duckdb"
    _make_db(db, ["2026-09-15"], table="intraday_summary_1m")
    con = duckdb.connect(str(db))
    try:
        con.execute("INSERT INTO intraday_summary_5m VALUES ('600000', '2026-09-15')")
    finally:
        con.close()
    monkeypatch.setattr(module, "MARKET_DB", str(db))
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    (zip_root / "minute_1m_20260915.zip").write_bytes(b"")

    assert module._missing_minute_dates(zip_root, date(2026, 9, 1)) == []


def test_sync_minute_refuses_to_noop_when_source_missing(tmp_path: Path) -> None:
    """源目录不存在必须非零退出——这正是本次静默故障的形态。"""
    module = _load_module()
    with pytest.raises(SystemExit) as excinfo:
        module._sync_minute(zip_root=tmp_path / "does-not-exist", since=date(2026, 8, 1))
    message = str(excinfo.value)
    assert "源目录不存在" in message
    assert "qq_minute_raw" in message


def test_missing_minute_dates_is_zip_dates_minus_db(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    db = tmp_path / "market.duckdb"
    # 8/27、8/28 库里已有（zip 里也有）→ 不应出现在缺口里；只有 9/15 是缺口
    _make_db(db, ["2026-08-27", "2026-08-28"])
    monkeypatch.setattr(module, "MARKET_DB", str(db))
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    for name in ("minute_1m_20260827.zip", "minute_1m_20260828.zip", "minute_1m_20260915.zip"):
        (zip_root / name).write_bytes(b"")
    (zip_root / "minute_1m_20260916").mkdir()  # 目录不算 zip 日期
    (zip_root / "unrelated.txt").write_text("x", encoding="utf-8")

    missing = module._missing_minute_dates(zip_root, date(2026, 8, 1))
    assert missing == [date(2026, 9, 15)]

    # --since 之前的不参与扫描
    assert module._missing_minute_dates(zip_root, date(2026, 9, 1)) == [date(2026, 9, 15)]


def test_sync_minute_reports_source_when_no_gap(tmp_path: Path, monkeypatch) -> None:
    """无缺口时的日志要带上源路径，便于一眼看出读的是哪个目录。"""
    module = _load_module()
    db = tmp_path / "market.duckdb"
    _make_db(db, ["2026-09-15"])
    monkeypatch.setattr(module, "MARKET_DB", str(db))
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    (zip_root / "minute_1m_20260915.zip").write_bytes(b"")
    assert module._sync_minute(zip_root=zip_root, since=date(2026, 9, 1)) == 0
