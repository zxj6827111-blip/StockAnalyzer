"""Tests for scripts/update_vendor_daily_from_tushare.py.

The factor segment (qfq/hfq re-anchoring, per-year factor ZIP entries) and
the ZIP rebuild segment (atomic replace, old-row merge, resume-by-ZIP-date)
are exercised with fake tushare API objects; no network or real token needed.
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
_UPDATE_PATH = ROOT / "scripts" / "update_vendor_daily_from_tushare.py"


def _load_updater() -> object:
    module_name = "update_vendor_daily_from_tushare"
    spec = importlib.util.spec_from_file_location(module_name, _UPDATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def updater() -> object:
    return _load_updater()


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode("utf-8"))


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20250717", "20250720"],
            "open": [10.0, 9.5],
            "high": [11.0, 10.0],
            "low": [9.0, 9.0],
            "close": [10.5, 9.5],
            "pre_close": [10.2, 10.5],
            "change": [0.3, -1.0],
            "pct_chg": [2.9, -9.5],
            "vol": [100, 300],
            "amount": [123.4, 345.6],
        }
    )


def _basic_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20250717", "20250720"],
            "turnover_rate": [0.5, 0.7],
            "turnover_rate_f": [0.4, 0.6],
            "volume_ratio": [1.1, 1.3],
            "pe": [10.1, 10.3],
            "pe_ttm": [9.1, 9.3],
            "pb": [1.1, 1.3],
            "ps": [2.1, 2.3],
            "ps_ttm": [2.0, 2.2],
            "dv_ratio": [3.1, 3.3],
            "dv_ttm": [3.0, 3.2],
            "total_share": [100.0, 100.0],
            "float_share": [80.0, 80.0],
            "free_share": [70.0, 70.0],
            "total_mv": [1000.0, 1000.0],
            "circ_mv": [800.0, 800.0],
        }
    )


def _adj_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH"] * 3,
            "trade_date": ["20250701", "20250717", "20250720"],
            "adj_factor": [1.1, 1.1, 2.2],
        }
    )


# ---------------------------------------------------------------------------
# 25-column daily mapping
# ---------------------------------------------------------------------------
def test_daily_to_25_columns_maps_tushare_fields(updater: object) -> None:
    frame = updater._daily_to_25_columns(
        ts_code="600000.SH",
        daily=_daily_frame(),
        basic=_basic_frame(),
    )

    assert list(frame.columns) == updater.DAILY_COLUMNS
    assert frame["code"].tolist() == ["600000.SH", "600000.SH"]
    # tushare datetime YYYYMMDD kept for internal merging.
    assert frame["datetime"].tolist() == ["20250717", "20250720"]
    assert frame["volume"].tolist() == [100, 300]
    assert frame["amount"].tolist() == [123.4, 345.6]
    assert frame["turnover"].tolist() == [0.5, 0.7]
    assert frame["turnover_free"].tolist() == [0.4, 0.6]
    assert frame["dv_yield"].tolist() == [3.1, 3.3]
    assert frame["pe_ttm"].tolist() == [9.1, 9.3]
    assert frame["circ_mv"].tolist() == [800.0, 800.0]


def test_daily_to_25_columns_tolerates_missing_basic_columns(updater: object) -> None:
    basic = _basic_frame().drop(columns=["ps_ttm", "dv_ttm"])
    frame = updater._daily_to_25_columns(
        ts_code="600000.SH",
        daily=_daily_frame(),
        basic=basic,
    )

    assert list(frame.columns) == updater.DAILY_COLUMNS
    assert frame["ps_ttm"].isna().all()
    assert frame["dv_ttm"].isna().all()


# ---------------------------------------------------------------------------
# factor re-anchoring (qfq latest = 1.0, hfq earliest = 1.0)
# ---------------------------------------------------------------------------
def test_factor_rows_qfq_anchors_latest_to_one(updater: object) -> None:
    rows = updater._factor_rows(ts_code="600000.SH", adj=_adj_frame(), anchor="latest")

    assert list(rows.columns) == ["股票代码", "交易日期", "复权因子"]
    assert rows["交易日期"].tolist() == ["20250701", "20250717", "20250720"]
    assert rows["复权因子"].tolist() == pytest.approx([0.5, 0.5, 1.0])


def test_factor_rows_hfq_anchors_earliest_to_one(updater: object) -> None:
    rows = updater._factor_rows(ts_code="600000.SH", adj=_adj_frame(), anchor="earliest")

    assert rows["交易日期"].tolist() == ["20250701", "20250717", "20250720"]
    assert rows["复权因子"].tolist() == pytest.approx([1.0, 1.0, 2.0])


def test_factor_rows_rejects_bad_adj(updater: object) -> None:
    from stock_analyzer.data.provider import DataSourceError

    with pytest.raises(DataSourceError, match="adj_factor empty"):
        updater._factor_rows(ts_code="600000.SH", adj=pd.DataFrame(), anchor="latest")
    with pytest.raises(DataSourceError, match="600000.SH"):
        updater._factor_rows(
            ts_code="600000.SH",
            adj=pd.DataFrame({"trade_date": ["20250720"], "adj_factor": [0.0]}),
            anchor="latest",
        )


def test_factor_rows_to_year_entries(updater: object) -> None:
    rows = updater._factor_rows(ts_code="600000.SH", adj=_adj_frame(), anchor="latest")
    entries = updater._frame_to_year_entries(rows)

    assert list(entries) == ["2025/600000.SH.csv"]
    lines = entries["2025/600000.SH.csv"].splitlines()
    assert lines[0] == "股票代码,交易日期,复权因子"
    assert lines[1].startswith("600000.SH,20250701,0.5")
    assert lines[-1] == "600000.SH,20250720,1.0"


# ---------------------------------------------------------------------------
# ZIP rebuild: atomic replace + merge + verification
# ---------------------------------------------------------------------------
def test_rebuild_zip_replaces_entries_copies_others_and_verifies(
    updater: object, tmp_path: Path
) -> None:
    _write_zip(
        tmp_path / "2025.zip",
        {
            "2025/600000.SH.csv": "code,datetime,close\n600000.SH,2025-07-17,10.5\n",
            "2025/000001.SZ.csv": "code,datetime,close\n000001.SZ,2025-07-17,12.0\n",
            "2025/noise.txt": "keep me",
        },
    )
    report = updater._rebuild_zip(
        tmp_path / "2025.zip",
        {"2025/600000.SH.csv": "code,datetime,close\n600000.SH,2025-07-20,9.5\n"},
    )

    assert report["entries_total"] == 3
    with zipfile.ZipFile(tmp_path / "2025.zip") as archive:
        names = sorted(archive.namelist())
        assert names == ["2025/000001.SZ.csv", "2025/600000.SH.csv", "2025/noise.txt"]
        # replaced entry has the new content; others copied byte-for-byte.
        assert archive.read("2025/600000.SH.csv").decode("utf-8") == (
            "code,datetime,close\n600000.SH,2025-07-20,9.5\n"
        )
        assert archive.read("2025/000001.SZ.csv").decode("utf-8") == (
            "code,datetime,close\n000001.SZ,2025-07-17,12.0\n"
        )
        assert archive.read("2025/noise.txt").decode("utf-8") == "keep me"
    assert not list(tmp_path.glob("*.tmp"))


def test_zip_rebuild_validation_failure_preserves_old_archive(
    updater: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild whose post-write validation fails must keep the old ZIP."""
    target = tmp_path / "2025.zip"
    _write_zip(target, {"2025/600000.SH.csv": "old content\n"})
    original_bytes = target.read_bytes()
    real_zipfile = updater.zipfile.ZipFile
    open_count = {"n": 0}

    class _FlakyZipFile:
        def __init__(self, file, mode="r", *args, **kwargs):
            self._zf = real_zipfile(file, mode, *args, **kwargs)
            open_count["n"] += 1
            self._flaky = open_count["n"] >= 2  # validation open is the 2nd

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._zf.__exit__(*args)

        def infolist(self):
            return self._zf.infolist()

        def read(self, name):
            if self._flaky:
                raise updater.zipfile.BadZipFile("corrupt probe")
            return self._zf.read(name)

        def writestr(self, *args, **kwargs):
            return self._zf.writestr(*args, **kwargs)

        def close(self):
            return self._zf.close()

    monkeypatch.setattr(updater.zipfile, "ZipFile", _FlakyZipFile)
    with pytest.raises(updater.zipfile.BadZipFile):
        updater._rebuild_zip(
            target, {"2025/600000.SH.csv": "new content\n"}
        )
    # The official archive is byte-identical: validation failed before replace.
    assert target.read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.tmp"))  # temp cleaned up on failure


def test_rebuild_daily_year_zip_merges_old_and_new_rows(updater: object, tmp_path: Path) -> None:
    _write_zip(
        tmp_path / "全A日K" / "2025.zip",
        {
            "2025/600000.SH.csv": (
                "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount\n"
                "600000.SH,2025-07-17,10,11,9,10.5,10.5,0,0,100,123.4\n"
            )
        },
    )
    fresh = updater._daily_to_25_columns(
        ts_code="600000.SH",
        daily=_daily_frame(),
        basic=_basic_frame(),
    )
    updater._rebuild_daily_year_zip(tmp_path / "全A日K", 2025, {"600000.SH": fresh})

    with zipfile.ZipFile(tmp_path / "全A日K" / "2025.zip") as archive:
        content = archive.read("2025/600000.SH.csv").decode("utf-8")
    rows = [line for line in content.splitlines() if line.startswith("600000")]
    # old row kept and normalized to YYYY-MM-DD; new rows appended.
    assert [row.split(",")[1] for row in rows] == ["2025-07-17", "2025-07-20"]
    assert rows[0].split(",")[5] == "10.5"
    assert rows[1].split(",")[9] == "300"


def test_rebuild_daily_year_zip_deduplicates_by_date(updater: object, tmp_path: Path) -> None:
    _write_zip(
        tmp_path / "全A日K" / "2025.zip",
        {
            "2025/600000.SH.csv": (
                "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount\n"
                "600000.SH,2025-07-20,99,99,99,99,99,0,0,1,1\n"
            )
        },
    )
    fresh = updater._daily_to_25_columns(
        ts_code="600000.SH",
        daily=_daily_frame(),
        basic=_basic_frame(),
    )
    updater._rebuild_daily_year_zip(tmp_path / "全A日K", 2025, {"600000.SH": fresh})

    with zipfile.ZipFile(tmp_path / "全A日K" / "2025.zip") as archive:
        content = archive.read("2025/600000.SH.csv").decode("utf-8")
    rows = [line for line in content.splitlines() if line.startswith("600000")]
    # Newest row wins on the shared date: 2025-07-20 volume 300 replaces 1.
    assert [row.split(",")[1] for row in rows] == ["2025-07-17", "2025-07-20"]
    assert rows[1].split(",")[9] == "300"


def test_rebuild_factor_zip_replaces_all_years_of_symbol(
    updater: object, tmp_path: Path
) -> None:
    _write_zip(
        tmp_path / "复权因子" / "复权因子_前复权.zip",
        {
            "2024/600000.SH.csv": "股票代码,交易日期,复权因子\n600000.SH,20241231,0.4\n",
            "2025/600000.SH.csv": "股票代码,交易日期,复权因子\n600000.SH,20250717,0.8\n",
            "2025/000001.SZ.csv": "股票代码,交易日期,复权因子\n000001.SZ,20250717,1.0\n",
        },
    )
    qfq = updater._factor_rows(ts_code="600000.SH", adj=_adj_frame(), anchor="latest")
    updater._rebuild_factor_zip(
        tmp_path / "复权因子",
        "复权因子_前复权.zip",
        {"600000.SH": qfq},
    )

    with zipfile.ZipFile(tmp_path / "复权因子" / "复权因子_前复权.zip") as archive:
        names = sorted(archive.namelist())
        assert names == ["2024/600000.SH.csv", "2025/000001.SZ.csv", "2025/600000.SH.csv"]
        # Old legacy anchor 0.4/0.8 fully replaced by the re-anchored series.
        content = archive.read("2025/600000.SH.csv").decode("utf-8")
        assert "0.5" in content and "1.0" in content and "0.8" not in content
        assert archive.read("2025/000001.SZ.csv").decode("utf-8") == (
            "股票代码,交易日期,复权因子\n000001.SZ,20250717,1.0\n"
        )


# ---------------------------------------------------------------------------
# resume contract: skip decisions read actual ZIP last dates
# ---------------------------------------------------------------------------
def _fake_pro() -> object:
    class _FakePro:
        def daily(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _daily_frame()

        def daily_basic(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _basic_frame()

        def adj_factor(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _adj_frame()

    return _FakePro()


def _fake_api(updater: object, calls: list[str]) -> object:
    from stock_analyzer.data.tushare_provider import TushareProvider

    class _FakeApi(TushareProvider):
        def _resolve_pro_api(self) -> object:
            if not hasattr(self, "_fake"):
                self._fake = _fake_pro()
            return self._fake

        def _call_with_retry(self, fn: object) -> object:
            calls.append(type(fn).__name__)
            return fn()

    return _FakeApi(token="x", retry_delay_sec=0.0, min_request_interval_sec=0.0, max_attempts=1)


def _daily_fixture(tmp_path: Path) -> Path:
    _write_zip(
        tmp_path / "全A日K" / "2025.zip",
        {
            "2025/600000.SH.csv": (
                "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount\n"
                "600000.SH,2025-07-17,10,11,9,10.5,10.5,0,0,100,123.4\n"
            )
        },
    )
    return tmp_path / "全A日K"


def _factor_fixture(tmp_path: Path) -> Path:
    _write_zip(
        tmp_path / "复权因子" / "复权因子_前复权.zip",
        {
            "2025/600000.SH.csv": (
                "股票代码,交易日期,复权因子\n"
                "600000.SH,20250701,0.9\n"
                "600000.SH,20250717,1.0\n"
            )
        },
    )
    _write_zip(
        tmp_path / "复权因子" / "复权因子_后复权.zip",
        {
            "2025/600000.SH.csv": (
                "股票代码,交易日期,复权因子\n"
                "600000.SH,20250701,1.0\n"
                "600000.SH,20250717,1.1111111111111112\n"
            )
        },
    )
    return tmp_path / "复权因子"


def test_fetch_symbol_skips_when_zip_dates_are_current(
    updater: object, tmp_path: Path
) -> None:
    daily_root = _daily_fixture(tmp_path)
    factors_root = _factor_fixture(tmp_path)
    calls: list[str] = []
    api = _fake_api(updater, calls)

    result = updater._fetch_symbol(
        api=api,
        symbol="600000",
        end_date=date(2025, 7, 17),
        daily_root=daily_root,
        factors_root=factors_root,
        skip_factors=False,
        dry_run=False,
    )

    assert result["status"] == "skipped"
    assert calls == []


def test_fetch_symbol_force_factors_refetches_current_factors(
    updater: object, tmp_path: Path
) -> None:
    """force_factors 使因子 ZIP 已覆盖 end_date 时仍重拉 adj_factor（修复截断历史）。"""
    daily_root = _daily_fixture(tmp_path)
    factors_root = _factor_fixture(tmp_path)
    calls: list[str] = []
    api = _fake_api(updater, calls)

    result = updater._fetch_symbol(
        api=api,
        symbol="600000",
        end_date=date(2025, 7, 17),
        daily_root=daily_root,
        factors_root=factors_root,
        skip_factors=False,
        force_factors=True,
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert result["daily_fetch"] is False
    assert result["factor_fetch"] is True
    # adj_factor only (paged); no daily/daily_basic calls.
    assert len(calls) == 1
    assert not result.get("adj", pd.DataFrame()).empty


def test_fetch_symbol_fetches_only_missing_factors(updater: object, tmp_path: Path) -> None:
    daily_root = _daily_fixture(tmp_path)
    factors_root = tmp_path / "复权因子_missing"
    factors_root.mkdir(parents=True)
    calls: list[str] = []
    api = _fake_api(updater, calls)

    result = updater._fetch_symbol(
        api=api,
        symbol="600000",
        end_date=date(2025, 7, 17),
        daily_root=daily_root,
        factors_root=factors_root,
        skip_factors=False,
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert result["daily_fetch"] is False
    assert result["factor_fetch"] is True
    # adj_factor only: no daily/daily_basic calls.
    assert len(calls) == 1


def test_fetch_symbol_fetches_missing_daily_and_factors(
    updater: object, tmp_path: Path
) -> None:
    daily_root = _daily_fixture(tmp_path)
    factors_root = _factor_fixture(tmp_path)
    calls: list[str] = []
    api = _fake_api(updater, calls)

    result = updater._fetch_symbol(
        api=api,
        symbol="600000",
        end_date=date(2025, 7, 20),
        daily_root=daily_root,
        factors_root=factors_root,
        skip_factors=False,
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert result["daily_fetch"] is True
    assert result["factor_fetch"] is True
    assert len(calls) == 3


def test_fetch_symbol_skip_factors_ignores_factor_zip(updater: object, tmp_path: Path) -> None:
    daily_root = _daily_fixture(tmp_path)
    factors_root = _factor_fixture(tmp_path)
    calls: list[str] = []
    api = _fake_api(updater, calls)

    result = updater._fetch_symbol(
        api=api,
        symbol="600000",
        end_date=date(2025, 7, 17),
        daily_root=daily_root,
        factors_root=factors_root,
        skip_factors=True,
        dry_run=False,
    )

    assert result["status"] == "skipped"
    assert calls == []


def test_symbol_last_dates_read_real_zip_content(updater: object, tmp_path: Path) -> None:
    daily_root = _daily_fixture(tmp_path)
    factors_root = _factor_fixture(tmp_path)

    assert updater._symbol_daily_last_date(daily_root, "600000.SH") == date(2025, 7, 17)
    assert updater._symbol_factor_last_date(factors_root, "600000.SH") == date(2025, 7, 17)


def test_dry_run_reports_without_api_calls(updater: object, tmp_path: Path) -> None:
    daily_root = _daily_fixture(tmp_path)
    factors_root = _factor_fixture(tmp_path)
    calls: list[str] = []
    api = _fake_api(updater, calls)

    result = updater._fetch_symbol(
        api=api,
        symbol="600000",
        end_date=date(2025, 7, 20),
        daily_root=daily_root,
        factors_root=factors_root,
        skip_factors=False,
        dry_run=True,
    )

    assert result["status"] == "dry-run"
    assert result["daily_fetch"] is True
    assert result["factor_fetch"] is True
    assert calls == []


def test_main_dry_run_summary_json(
    updater: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daily_fixture(tmp_path)
    _factor_fixture(tmp_path)
    output: list[str] = []

    class _Stdout:
        def write(self, text: str) -> int:
            output.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(updater.sys, "stdout", _Stdout())
    exit_code = updater._main(
        [
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            "2025-07-20",
            "--dry-run",
            "--interval-sec",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads("".join(output))
    assert summary["dry_run"] is True
    assert summary["symbols_total"] == 1
    assert summary["fetched"] == 1
    assert summary["zip_rebuilds"] == []


# ---------------------------------------------------------------------------
# --sync-vendor-delta 钩子（审查修复：显式路径加载 + 集成覆盖）
# ---------------------------------------------------------------------------


def _main_with_fake_api(
    updater: object,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> tuple[int, dict[str, object]]:
    """跑 updater._main，tushare API 用 fake（无网络），捕获 stdout JSON。"""
    class _FakeTushareProvider:
        def __init__(self, **kwargs: object) -> None:
            pass

        def _resolve_pro_api(self) -> object:
            return _fake_pro()

        def _call_with_retry(self, fn: object) -> object:
            return fn()

    monkeypatch.setattr(updater, "TushareProvider", _FakeTushareProvider)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    output: list[str] = []

    class _Stdout:
        def write(self, text: str) -> int:
            output.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(updater.sys, "stdout", _Stdout())
    exit_code = updater._main(argv)
    summary = json.loads("".join(output))
    return exit_code, summary


def _full_daily_index(root: Path) -> Path:
    """用 build_vendor_zip_daily_index 生成完整 index（updater 增量更新的对象）。"""
    from stock_analyzer.data.vendor_zip_overlay import write_vendor_zip_daily_index

    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def test_main_sync_vendor_delta_runs_incremental_import(
    updater: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """钩子成功路径：ZIP+索引更新后自动增量同步 delta 库。"""
    _daily_fixture(tmp_path)
    _factor_fixture(tmp_path)
    index_path = _full_daily_index(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    readiness_path = tmp_path / "runtime" / "nightly_data_ready.json"
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(readiness_path))

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        [
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            "2025-07-20",
            "--interval-sec",
            "0",
            "--index-path",
            str(index_path),
            "--sync-vendor-delta",
            str(delta_db),
        ],
    )

    assert exit_code == 0
    assert summary["delta_sync"]["updated"] is True
    assert summary["delta_sync"]["exit_code"] == 0
    assert summary["readiness"]["written"] is True
    assert readiness_path.exists()
    # delta 库已含 600000 的两行（07-17 旧行 + 07-20 新行）。
    from stock_analyzer.data.market_warehouse import MarketWarehouse

    warehouse = MarketWarehouse(
        db_path=delta_db, package_root=tmp_path / "delta" / "package"
    )
    frame = warehouse.fetch_all_daily_bars(symbol="600000")
    assert len(frame) == 2
    # qfq 最新因子锚定 07-20（=1.0）：新行 close 保持 raw 值 9.5。
    assert float(frame["close"].iloc[-1]) == pytest.approx(9.5)


def test_main_sync_vendor_delta_failure_blocks_readiness(
    updater: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delta 导入失败必须返回非零且不得发布 readiness。"""
    _daily_fixture(tmp_path)
    _factor_fixture(tmp_path)
    # Index 成功后，把 delta DB 路径预先建成目录，强制 importer 非零退出。
    index_path = _full_daily_index(tmp_path)
    delta_db = tmp_path / "delta-target-is-directory"
    delta_db.mkdir()
    readiness_path = tmp_path / "runtime" / "nightly_data_ready.json"
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(readiness_path))

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        [
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            "2025-07-20",
            "--interval-sec",
            "0",
            "--index-path",
            str(index_path),
            "--sync-vendor-delta",
            str(delta_db),
        ],
    )

    assert exit_code == 1
    assert summary["delta_sync"]["updated"] is False
    assert summary["delta_sync"]["exit_code"] != 0
    assert summary["readiness"]["written"] is False
    assert not readiness_path.exists()
    assert delta_db.is_dir()  # 非法目标未被替换成 DuckDB 文件


def test_main_batch_skips_per_symbol_and_writes_readiness_after_delta(
    updater: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _daily_fixture(tmp_path)
    _factor_fixture(tmp_path)
    index_path = _full_daily_index(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    readiness_path = tmp_path / "runtime" / "nightly_data_ready.json"
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(readiness_path))
    per_symbol_calls: list[str] = []

    def _fake_batch(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "attempted": True,
            "ok": True,
            "latest_daily_date": "2025-07-20",
            "symbols_updated": 1,
            "zip_rebuilds": [],
            "index": {"updated": True},
        }

    def _unexpected_fetch(**kwargs: object) -> dict[str, object]:
        per_symbol_calls.append(str(kwargs.get("symbol", "")))
        raise AssertionError("batch mode must not dispatch per-symbol fetches")

    monkeypatch.setattr(updater, "_run_batch", _fake_batch)
    monkeypatch.setattr(updater, "_fetch_symbol", _unexpected_fetch)

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        [
            "--batch",
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            "2025-07-20",
            "--interval-sec",
            "0",
            "--index-path",
            str(index_path),
            "--sync-vendor-delta",
            str(delta_db),
        ],
    )

    assert exit_code == 0
    assert per_symbol_calls == []
    assert summary["mode"] == "batch"
    assert summary["delta_sync"]["updated"] is True
    assert summary["readiness"]["written"] is True
    assert readiness_path.exists()


def test_main_batch_index_failure_blocks_delta_and_readiness(
    updater: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _daily_fixture(tmp_path)
    index_path = _full_daily_index(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    readiness_path = tmp_path / "runtime" / "nightly_data_ready.json"
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(readiness_path))

    def _fake_batch(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "attempted": True,
            "ok": True,
            "latest_daily_date": "2025-07-20",
            "symbols_updated": 1,
            "zip_rebuilds": [],
            "index": {"updated": False, "reason": "write_failed"},
        }

    monkeypatch.setattr(updater, "_run_batch", _fake_batch)
    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        [
            "--batch",
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            "2025-07-20",
            "--interval-sec",
            "0",
            "--index-path",
            str(index_path),
            "--sync-vendor-delta",
            str(delta_db),
        ],
    )

    assert exit_code == 1
    assert summary["delta_sync"] == {
        "updated": False,
        "reason": "index_update_failed",
    }
    assert summary["readiness"]["written"] is False
    assert not readiness_path.exists()
    assert not delta_db.exists()


def test_main_readiness_write_failure_is_fatal(
    updater: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _daily_fixture(tmp_path)
    _factor_fixture(tmp_path)

    def _fail_readiness(**kwargs: object) -> None:
        _ = kwargs
        raise OSError("readiness volume is read-only")

    monkeypatch.setattr(updater, "write_nightly_readiness", _fail_readiness)
    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        [
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            "2025-07-20",
            "--interval-sec",
            "0",
        ],
    )

    assert exit_code == 1
    assert summary["readiness"]["written"] is False
    assert "readiness volume is read-only" in summary["readiness"]["error"]


# ---------------------------------------------------------------------------
# adj_factor 分页：全历史查询（1996→今 ≈7400 行）超过单次上限时不再截断
# ---------------------------------------------------------------------------
def test_fetch_adj_factor_paged_concats_pages(updater: object) -> None:
    calls: list[dict[str, object]] = []

    class _PaginatedPro:
        def adj_factor(self, **kwargs: object) -> pd.DataFrame:
            calls.append(dict(kwargs))
            offset = int(kwargs.get("offset", 0))
            total = 5
            if offset >= total:
                return pd.DataFrame()
            rows = [
                {
                    "ts_code": "600000.SH",
                    "trade_date": f"{20000101 + i}",
                    "adj_factor": 1.0 + i * 0.1,
                }
                for i in range(offset, min(offset + 2, total))
            ]
            return pd.DataFrame(rows)

    class _PaginatedApi:
        def _resolve_pro_api(self) -> _PaginatedPro:
            return _PaginatedPro()

        def _call_with_retry(self, fn: object) -> object:
            return fn() if callable(fn) else fn

    result = updater._fetch_adj_factor_paged(
        _PaginatedApi(),
        ts_code="600000.SH",
        start_date="19900101",
        end_date="20260813",
        page_size=2,
    )

    assert len(result) == 5
    assert [int(call.get("offset", 0)) for call in calls] == [0, 2, 4]
    assert all("limit" in call for call in calls)


def test_fetch_adj_factor_paged_trade_date_single_call(updater: object) -> None:
    """trade_date 全市场单日调用不携带 offset/limit（接口不支持分页）。"""
    class _Pro:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def adj_factor(self, **kwargs: object) -> pd.DataFrame:
            self.calls.append(dict(kwargs))
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "trade_date": ["20260813"],
                    "adj_factor": [2.0],
                }
            )

    class _Api:
        def __init__(self) -> None:
            self.pro = _Pro()

        def _resolve_pro_api(self) -> _Pro:
            return self.pro

        def _call_with_retry(self, fn: object) -> object:
            return fn() if callable(fn) else fn

    api = _Api()
    result = updater._fetch_adj_factor_paged(api, trade_date="20260813")

    assert len(result) == 1
    assert len(api.pro.calls) == 1
    assert "offset" not in api.pro.calls[0]
    assert "limit" not in api.pro.calls[0]
    assert api.pro.calls[0]["trade_date"] == "20260813"


def test_fetch_symbol_adj_factor_empty_raises(
    updater: object, tmp_path: Path
) -> None:
    """adj_factor 空响应视为失败而非无数据（8-13 现场 0 成功 0 失败被当 empty）。"""
    daily_root = _daily_fixture(tmp_path)
    factors_root = tmp_path / "复权因子_missing"
    factors_root.mkdir(parents=True)

    class _EmptyAdjPro:
        def daily(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _daily_frame()

        def daily_basic(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _basic_frame()

        def adj_factor(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return pd.DataFrame()

    class _Api(updater.TushareProvider):
        def _resolve_pro_api(self) -> _EmptyAdjPro:
            return _EmptyAdjPro()

        def _call_with_retry(self, fn: object) -> object:
            return fn() if callable(fn) else fn

    api = _Api(
        token="x", retry_delay_sec=0.0, min_request_interval_sec=0.0, max_attempts=1
    )
    with pytest.raises(updater.DataSourceError, match="adj_factor empty"):
        updater._fetch_symbol(
            api=api,
            symbol="600000",
            end_date=date(2025, 7, 20),
            daily_root=daily_root,
            factors_root=factors_root,
            skip_factors=False,
            dry_run=False,
        )
