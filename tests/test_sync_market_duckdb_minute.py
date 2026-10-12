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


def _set_target_dbs(
    module, monkeypatch, market_db: Path, delta_db: Path | None = None
) -> None:
    monkeypatch.setattr(module, "MARKET_DB", str(market_db))
    monkeypatch.setattr(module, "DELTA_DB", str(delta_db or market_db))


def test_half_written_date_is_still_a_gap(tmp_path: Path, monkeypatch) -> None:
    """1m 有、5m 缺的日期必须仍算缺口。

    原实现只拿 `intraday_summary_1m` 判缺口，写入端却逐日写 1m+5m——"写 2 张、只查 1 张"
    会让部分缺失**静默留存**（与 8/28 那次静默空转同一类）。
    """
    module = _load_module()
    db = tmp_path / "market.duckdb"
    _make_db(db, ["2026-09-15"], table="intraday_summary_1m")  # 只有 1m
    _set_target_dbs(module, monkeypatch, db)
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")

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
    _set_target_dbs(module, monkeypatch, db)
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")

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
    _set_target_dbs(module, monkeypatch, db)
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    for name in ("minute_1m_20260827.zip", "minute_1m_20260828.zip", "minute_1m_20260915.zip"):
        ymd = name.removeprefix("minute_1m_").removesuffix(".zip")
        _write_minute_source(zip_root, "1m", ymd)
        _write_minute_source(zip_root, "5m", ymd)
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
    _set_target_dbs(module, monkeypatch, db)
    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")
    assert module._sync_minute(zip_root=zip_root, since=date(2026, 9, 1)) == 0


@pytest.mark.parametrize("missing_target", ["market", "delta"])
def test_missing_minute_dates_checks_both_databases(
    tmp_path: Path, monkeypatch, missing_target: str
) -> None:
    module = _load_module()
    market_db = tmp_path / "market.duckdb"
    delta_db = tmp_path / "delta.duckdb"
    complete_dates = ["2026-09-15"]
    _make_db(market_db, [] if missing_target == "market" else complete_dates)
    _make_db(delta_db, [] if missing_target == "delta" else complete_dates)
    _set_target_dbs(module, monkeypatch, market_db, delta_db)

    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")

    assert module._missing_minute_dates(zip_root, date(2026, 9, 1)) == [
        date(2026, 9, 15)
    ]


def _write_minute_source(zip_root: Path, interval: str, ymd: str = "20260915") -> None:
    import zipfile

    trade_date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    payload = (
        "date,time,open,high,low,close,volume\n"
        f"{trade_date},09:30:00,10,10.2,9.8,10.1,100\n"
        f"{trade_date},09:31:00,10.1,10.3,10,10.2,120\n"
    )
    with zipfile.ZipFile(zip_root / f"minute_{interval}_{ymd}.zip", "w") as archive:
        archive.writestr("SH#600000.csv", payload)


def _make_warehouse_db(module, path: Path, package_root: Path) -> None:
    module.MarketWarehouse(
        db_path=path,
        package_root=package_root,
        package_writes_enabled=False,
    ).ensure_schema()


def test_sync_minute_writes_same_summaries_to_market_and_delta(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_module()
    market_db = tmp_path / "market.duckdb"
    delta_db = tmp_path / "delta.duckdb"
    for path in (market_db, delta_db):
        _make_warehouse_db(module, path, tmp_path / "package")
    _set_target_dbs(module, monkeypatch, market_db, delta_db)

    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")

    assert module._sync_minute(zip_root=zip_root, since=date(2026, 9, 1)) == 4
    for path in (market_db, delta_db):
        con = duckdb.connect(str(path), read_only=True)
        try:
            for table in ("intraday_summary_1m", "intraday_summary_5m"):
                count = con.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE symbol='600000' AND date='2026-09-15'"
                ).fetchone()[0]
                assert count == 1
        finally:
            con.close()


def test_partial_target_failure_is_nonzero_and_remains_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_module()
    market_db = tmp_path / "market.duckdb"
    delta_db = tmp_path / "delta.duckdb"
    for path in (market_db, delta_db):
        _make_warehouse_db(module, path, tmp_path / "package")
    _set_target_dbs(module, monkeypatch, market_db, delta_db)

    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")

    real_warehouse = module.MarketWarehouse

    class FailingDeltaWarehouse:
        def __init__(self, *, db_path, package_root, package_writes_enabled):
            self.db_path = str(db_path)
            self.inner = real_warehouse(
                db_path=db_path,
                package_root=package_root,
                package_writes_enabled=package_writes_enabled,
            )

        def upsert_intraday_summaries(self, *, interval, frame):
            if self.db_path == str(delta_db):
                raise RuntimeError("simulated delta write failure")
            return self.inner.upsert_intraday_summaries(interval=interval, frame=frame)

    monkeypatch.setattr(module, "MarketWarehouse", FailingDeltaWarehouse)
    with pytest.raises(SystemExit, match="incomplete synchronization"):
        module._sync_minute(zip_root=zip_root, since=date(2026, 9, 1))

    assert module._missing_minute_dates(zip_root, date(2026, 9, 1)) == [
        date(2026, 9, 15)
    ]


def test_invalid_source_symbol_is_nonzero_and_remains_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    import zipfile

    module = _load_module()
    market_db = tmp_path / "market.duckdb"
    delta_db = tmp_path / "delta.duckdb"
    for path in (market_db, delta_db):
        _make_warehouse_db(module, path, tmp_path / "package")
    _set_target_dbs(module, monkeypatch, market_db, delta_db)

    zip_root = tmp_path / "minute_raw"
    zip_root.mkdir()
    _write_minute_source(zip_root, "1m")
    _write_minute_source(zip_root, "5m")
    with zipfile.ZipFile(zip_root / "minute_1m_20260915.zip", "a") as archive:
        archive.writestr("SH#600001.csv", "date,time,open,high,low,close,volume\n")

    with pytest.raises(SystemExit, match="incomplete synchronization"):
        module._sync_minute(zip_root=zip_root, since=date(2026, 9, 1))

    # 600000 同步成功，但 600001 缺少有效分钟行，因此日期仍会重试。
    assert module._missing_minute_dates(zip_root, date(2026, 9, 1)) == [
        date(2026, 9, 15)
    ]
