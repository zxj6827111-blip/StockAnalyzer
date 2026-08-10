from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.vendor_zip_overlay import (
    VendorZipOverlayProvider,
    write_vendor_zip_daily_index,
)

_IMPORT_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "import_vendor_zip_to_delta.py"
)
_spec = importlib.util.spec_from_file_location("import_vendor_zip_to_delta", _IMPORT_SCRIPT)
assert _spec is not None and _spec.loader is not None
delta_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delta_import)


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode("utf-8"))


def _daily_csv(*, symbol: str, close: float = 11.0) -> str:
    rows = [
        "code,datetime,open,high,low,close,volume,amount,circ_mv",
        f"{symbol},2024-12-30,10,11,9.5,{close - 0.5},100,123.4,200000",
        f"{symbol},2024-12-31,10.5,11.5,10.5,{close},200,234.5,210000",
        f"{symbol},2025-12-30,10,11,9.5,{close + 0.5},100,123.4,200000",
        f"{symbol},2025-12-31,10.5,11.5,10.5,{close + 1.0},200,234.5,210000",
    ]
    return "\n".join(rows)


def _build_daily_fixture(root: Path) -> Path:
    """Two symbols, two annual archives (2024/2025), four trading days each."""
    _write_zip(
        root / "全A日K" / "2024.zip",
        {
            "2024/600000.SH.csv": _daily_csv(symbol="600000.SH"),
            "2024/000001.SZ.csv": _daily_csv(symbol="000001.SZ"),
        },
    )
    _write_zip(
        root / "全A日K" / "2025.zip",
        {
            "2025/600000.SH.csv": _daily_csv(symbol="600000.SH"),
            "2025/000001.SZ.csv": _daily_csv(symbol="000001.SZ"),
        },
    )
    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def _write_factors_zip(root: Path, entries: dict[str, str]) -> None:
    _write_zip(root / "复权因子" / "复权因子_前复权.zip", entries)


def _factor_csv(*, ts_code: str, year: str = "2024", anchor_factor: float = 1.0) -> str:
    return "\n".join(
        [
            "股票代码,交易日期,复权因子",
            f"{ts_code},{year}1230,{anchor_factor}",
            f"{ts_code},{year}1231,{anchor_factor}",
        ]
    )


def _qfq_fixture(root: Path) -> Path:
    index_path = _build_daily_fixture(root)
    _write_factors_zip(
        root,
        {
            "2024/600000.SH.csv": _factor_csv(ts_code="600000.SH", year="2024"),
            "2024/000001.SZ.csv": _factor_csv(ts_code="000001.SZ", year="2024"),
            "2025/600000.SH.csv": _factor_csv(ts_code="600000.SH", year="2025"),
            "2025/000001.SZ.csv": _factor_csv(ts_code="000001.SZ", year="2025"),
        },
    )
    return index_path


def _run(args: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    rc = delta_import._main(args)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    return payload


def _provider(root: Path, index_path: Path, *, qfq: bool = False) -> VendorZipOverlayProvider:
    return VendorZipOverlayProvider(
        data_root=str(root),
        index_path=str(index_path),
        delta_db_path=str(root / "delta" / "market_delta.duckdb"),
        delta_package_root=str(root / "delta" / "package"),
        price_series_mode="qfq" if qfq else "raw",
    )


def _tushare_style_frame(*, close: float = 13.0, roe: float | None = None) -> pd.DataFrame:
    """最小合规 tushare 风格行（含 fetch 读回所需的必需列）。"""
    data: dict[str, object] = {
        "open": [close - 0.5],
        "high": [close + 0.5],
        "low": [close - 1.0],
        "close": [close],
        "volume": [9999.0],
        "turnover": [999_900.0],
        "float_market_cap": [2_200_000_000.0],
        "price_series_mode": ["raw"],
        "adjustment_source": ["tushare_raw"],
    }
    if roe is not None:
        data["roe"] = [roe]
        data["debt_ratio"] = [0.30]
        data["financial_data_complete"] = [True]
    return pd.DataFrame(data, index=pd.to_datetime(["2025-12-31"]))


def _base_args(root: Path, index_path: Path) -> list[str]:
    return [
        "--data-root",
        str(root),
        "--index-path",
        str(index_path),
        "--delta-db-path",
        str(root / "delta" / "market_delta.duckdb"),
        "--price-series-mode",
        "raw",
    ]


# ---------------------------------------------------------------------------
# MarketWarehouse.upsert_daily_bars
# ---------------------------------------------------------------------------


def test_upsert_daily_bars_refuses_read_only(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
        read_only=True,
    )
    with pytest.raises(DataSourceError):
        warehouse.upsert_daily_bars(
            frame=pd.DataFrame({"symbol": ["600000"], "date": ["2025-12-31"]})
        )


def test_upsert_daily_bars_fills_missing_columns_with_null(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
    )
    stored = warehouse.upsert_daily_bars(
        frame=pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "date": ["2025-12-30", "2025-12-31"],
                "open": [9.5, 10.0],
                "high": [11.0, 11.5],
                "low": [9.0, 9.5],
                "close": [10.0, 11.0],
                "volume": [1000.0, 2000.0],
                "turnover": [12_340.0, 23_450.0],
            }
        )
    )
    assert stored == 2
    frame = warehouse.fetch_all_daily_bars(symbol="600000")
    assert len(frame) == 2
    # 未提供的列保持 NULL，不伪造。
    assert pd.isna(frame["roe"].iloc[-1])
    assert pd.isna(frame["holder_count"].iloc[-1])
    assert float(frame["close"].iloc[-1]) == 11.0


def test_upsert_daily_bars_filters_invalid_symbols(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
    )
    stored = warehouse.upsert_daily_bars(
        frame=pd.DataFrame(
            {
                "symbol": ["600000", "not-a-symbol"],
                "date": ["2025-12-31", "2025-12-31"],
                "open": [10.0, 10.0],
                "high": [11.0, 11.0],
                "low": [9.0, 9.0],
                "close": [10.5, 10.5],
                "volume": [100.0, 100.0],
                "turnover": [1234.0, 1234.0],
            }
        )
    )
    assert stored == 1
    assert warehouse.list_symbols() == ["600000"]


# ---------------------------------------------------------------------------
# 全量导入
# ---------------------------------------------------------------------------


def test_full_import_writes_delta_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)

    first = _run(args + ["--limit-days", "400"], capsys)
    assert first["mode"] == "full"
    assert first["loaded_symbols"] == 2
    assert first["rows_stored"] == 8

    provider = _provider(tmp_path, index_path)
    rows = provider.fetch_daily_bars("600000", lookback_days=400)
    assert len(rows) == 4
    assert float(rows.loc["2025-12-31", "close"]) == 12.0

    second = _run(args + ["--limit-days", "400"], capsys)
    assert second["rows_stored"] == 0  # 幂等：已存在日期不重复写入
    provider.clear_cache()
    assert len(provider.fetch_daily_bars("600000", lookback_days=400)) == 4


def test_full_import_keeps_existing_delta_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """已存在的 delta 行（如 tushare 增量）不被 ZIP 基线覆盖——delta 赢语义。"""
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    warehouse = MarketWarehouse(
        db_path=delta_db, package_root=tmp_path / "delta" / "package"
    )
    warehouse.replace_daily_bars(symbol="600000", frame=_tushare_style_frame(roe=0.18))

    _run(_base_args(tmp_path, index_path) + ["--limit-days", "400"], capsys)

    frame = warehouse.fetch_all_daily_bars(symbol="600000")
    # 2025-12-31 是 tushare 行：close/roe 保留；其余日期为 ZIP 行。
    assert len(frame) == 4
    assert float(frame.loc["2025-12-31", "close"]) == 13.0
    assert float(frame.loc["2025-12-31", "roe"]) == 0.18
    assert float(frame.loc["2025-12-30", "close"]) == 11.5
    assert pd.isna(frame.loc["2025-12-30", "roe"])


def test_full_import_overwrite_existing_replaces_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    warehouse = MarketWarehouse(
        db_path=delta_db, package_root=tmp_path / "delta" / "package"
    )
    warehouse.replace_daily_bars(symbol="600000", frame=_tushare_style_frame(close=99.0))

    report = _run(
        _base_args(tmp_path, index_path)
        + ["--limit-days", "400", "--overwrite-existing"],
        capsys,
    )
    assert report["rows_stored"] == 8

    frame = warehouse.fetch_all_daily_bars(symbol="600000")
    assert float(frame.loc["2025-12-31", "close"]) == 12.0  # 已被 ZIP 值覆盖


def test_full_import_dry_run_never_creates_delta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)

    report = _run(args + ["--limit-days", "400", "--dry-run"], capsys)

    assert report["dry_run"] is True
    assert report["rows_read"] == 8
    assert not (tmp_path / "delta" / "market_delta.duckdb").exists()


def test_full_import_skips_missing_qfq_factor_symbols(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """qfq 模式下缺因子符号跳过并统计，不失败（与 batch 路径一致）。"""
    index_path = _build_daily_fixture(tmp_path)
    # 只有 000001.SZ 有因子；600000.SH 无因子，必须被跳过。
    _write_factors_zip(
        tmp_path,
        {
            "2024/000001.SZ.csv": _factor_csv(ts_code="000001.SZ"),
            "2025/000001.SZ.csv": _factor_csv(ts_code="000001.SZ"),
        },
    )

    report = _run(
        [
            "--data-root",
            str(tmp_path),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(tmp_path / "delta" / "market_delta.duckdb"),
            "--price-series-mode",
            "qfq",
            "--limit-days",
            "400",
        ],
        capsys,
    )

    assert report["loaded_symbols"] == 1
    assert report["skipped_symbols"] == ["600000"]
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
    )
    assert warehouse.list_symbols() == ["000001"]


# ---------------------------------------------------------------------------
# 增量导入
# ---------------------------------------------------------------------------


def test_incremental_import_writes_only_new_dates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400"], capsys)

    # ZIP 追加 2026-01-02（仅 600000），并重建索引。
    new_row = _daily_csv(symbol="600000.SH").splitlines()[0] + (
        "\n600000.SH,2026-01-02,11,12,10.5,12.5,300,345.6,220000"
    )
    _write_zip(
        tmp_path / "全A日K" / "2026.zip",
        {"2026/600000.SH.csv": new_row},
    )
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)

    report = _run(args + ["--incremental"], capsys)

    assert report["mode"] == "incremental"
    assert report["incremental_new_rows"] == 1
    assert report["full_import_symbol_count"] == 0
    assert report["drift_refreshed_symbol_count"] == 0

    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
    )
    frame = warehouse.fetch_all_daily_bars(symbol="600000")
    assert len(frame) == 5
    assert frame.index.max().strftime("%Y-%m-%d") == "2026-01-02"
    frame_000001 = warehouse.fetch_all_daily_bars(symbol="000001")
    assert len(frame_000001) == 4  # 无新日期，未写入


def test_incremental_import_fills_symbols_without_delta_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """增量时 delta 缺失的符号按全量导入补齐（自愈）。"""
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400"], capsys)

    # 直接删掉 000001 的 delta 行，模拟基线不完整。
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    warehouse = MarketWarehouse(
        db_path=delta_db, package_root=tmp_path / "delta" / "package"
    )
    from stock_analyzer.data.market_warehouse import _DAILY_TABLE

    with warehouse._connect_write() as connection:
        connection.execute(f"DELETE FROM {_DAILY_TABLE} WHERE symbol = '000001'")

    report = _run(args + ["--incremental"], capsys)

    assert report["full_import_symbol_count"] == 1
    frame = warehouse.fetch_all_daily_bars(symbol="000001")
    assert len(frame) == 4  # 全量补齐


def test_incremental_refreshes_qfq_factor_drift_symbols(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """因子重标定（除权）后，增量把漂移符号整段重算并覆盖。"""
    index_path = _qfq_fixture(tmp_path)
    args = [
        "--data-root",
        str(tmp_path),
        "--index-path",
        str(index_path),
        "--delta-db-path",
        str(tmp_path / "delta" / "market_delta.duckdb"),
        "--price-series-mode",
        "qfq",
    ]
    _run(args + ["--limit-days", "400"], capsys)

    # 模拟除权：600000 的 2025 年度因子从 1.0 重标定为 2.0（历史全部重算）。
    _write_factors_zip(
        tmp_path,
        {
            "2024/600000.SH.csv": _factor_csv(ts_code="600000.SH", year="2024"),
            "2024/000001.SZ.csv": _factor_csv(ts_code="000001.SZ", year="2024"),
            "2025/600000.SH.csv": _factor_csv(
                ts_code="600000.SH", year="2025", anchor_factor=2.0
            ),
            "2025/000001.SZ.csv": _factor_csv(ts_code="000001.SZ", year="2025"),
        },
    )

    report = _run(args + ["--incremental"], capsys)

    assert report["drift_refreshed_symbols"] == ["600000"]
    assert report["drift_refreshed_symbol_count"] == 1
    assert report["incremental_new_rows"] == 0

    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta" / "market_delta.duckdb",
        package_root=tmp_path / "delta" / "package",
    )
    frame = warehouse.fetch_all_daily_bars(symbol="600000")
    # 2025 年度因子全部重标定为 2.0：raw close × 2.0（旧值已被覆盖）。
    # raw close: 2025-12-30 = 11.5, 2025-12-31 = 12.0（_daily_csv 默认 close=11.0）。
    assert float(frame.loc["2025-12-31", "close"]) == pytest.approx(24.0)
    assert float(frame.loc["2025-12-30", "close"]) == pytest.approx(23.0)
    # 2024 年度因子仍为 1.0：close 保持 raw 值。
    assert float(frame.loc["2024-12-31", "close"]) == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# batch 读取：delta-first
# ---------------------------------------------------------------------------


def test_batch_after_full_import_matches_pure_zip_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """delta 全量导入后，batch 结果与纯 ZIP 路径一致（同一符号集逐值对比）。"""
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400"], capsys)

    zip_provider = _provider(tmp_path, index_path)
    delta_provider = _provider(tmp_path, index_path)
    zip_provider._warehouse = None  # 模拟无 delta（纯 ZIP 基线）
    delta_provider.delta_access_mode = "read_only"

    zip_frame = zip_provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"], lookback_days=3
    )
    delta_frame = delta_provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"], lookback_days=3
    )

    assert not delta_frame.empty
    assert len(delta_frame) == len(zip_frame) == 6
    compare_columns = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "float_market_cap",
        "suspended",
        "price_series_mode",
        "adjustment_source",
    ]
    assert delta_frame[compare_columns].equals(zip_frame[compare_columns])


def test_batch_partial_delta_gets_zip_history_for_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """delta 只覆盖部分符号时，其余符号从 ZIP 补齐，结果保持完整。"""
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400", "--symbols", "600000"], capsys)

    provider = _provider(tmp_path, index_path)
    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"], lookback_days=10
    )

    assert sorted(frame["symbol"].unique()) == ["000001", "600000"]
    for symbol in ("600000", "000001"):
        assert len(frame[frame["symbol"] == symbol]) == 4


def test_batch_shallow_delta_symbol_gets_zip_history_filled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """delta 中行数不足 lookback 的符号：ZIP 补历史，delta 行仍赢。"""
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    warehouse = MarketWarehouse(
        db_path=delta_db, package_root=tmp_path / "delta" / "package"
    )
    warehouse.replace_daily_bars(symbol="600000", frame=_tushare_style_frame())
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000"], lookback_days=4
    )

    rows = frame[frame["symbol"] == "600000"]
    assert len(rows) == 4  # ZIP 补齐到 4 天
    duplicated = rows[rows["date"].dt.strftime("%Y-%m-%d") == "2025-12-31"]
    assert float(duplicated.iloc[0]["close"]) == 13.0  # delta 行赢


def test_batch_falls_back_to_zip_when_delta_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """delta 整体滞后超过阈值：整批回退 ZIP 全量（保底正确性）。"""
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400"], capsys)

    # ZIP 推进到 2026-01-06（两符号），delta 停在 2025-12-31：滞后 6 天 > 3。
    _header = "code,datetime,open,high,low,close,volume,amount,circ_mv"
    _row = "600000.SH,2026-01-06,12,13,11.5,13.5,300,345.6,220000"
    _row_000001 = _row.replace("600000.SH", "000001.SZ")
    _write_zip(
        tmp_path / "全A日K" / "2026.zip",
        {
            "2026/600000.SH.csv": f"{_header}\n{_row}",
            "2026/000001.SZ.csv": f"{_header}\n{_row_000001}",
        },
    )
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    provider = _provider(tmp_path, index_path)

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"], lookback_days=2
    )

    assert len(frame) == 4  # 每符号最新 2 行（来自 ZIP）
    latest = frame[frame["symbol"] == "600000"]["date"].max()
    assert latest.strftime("%Y-%m-%d") == "2026-01-06"
    # 回退合并仍保留 delta 赢：2025-12-31 行来自 delta（导入值 close=12.0 同 ZIP）。
    rows = frame[frame["symbol"] == "600000"]
    assert float(rows.iloc[-1]["close"]) == 13.5


def test_batch_uses_delta_without_zip_when_fully_imported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """delta 全量覆盖后，batch 不再打开任何年度 ZIP。"""
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400"], capsys)
    provider = _provider(tmp_path, index_path)
    provider.delta_access_mode = "read_only"

    opened: list[str] = []

    class _CountingZipFile(zipfile.ZipFile):
        def __init__(self, file, *args: object, **kwargs: object) -> None:
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", _CountingZipFile)
    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"], lookback_days=3
    )
    assert len(frame) == 6
    assert opened == []  # delta 全覆盖：ZIP 一次都没打开


def test_batch_delta_lagging_without_shallow_still_returns_latest_zip_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """回归：非滞后且非浅 delta 时直接走 delta，不做 ZIP 读取。"""
    index_path = _build_daily_fixture(tmp_path)
    args = _base_args(tmp_path, index_path)
    _run(args + ["--limit-days", "400"], capsys)
    provider = _provider(tmp_path, index_path)
    provider.delta_access_mode = "read_only"

    frame = provider.fetch_universe_quality_metrics(
        symbols=["600000", "000001"], lookback_days=3
    )
    assert len(frame) == 6
    assert frame[frame["symbol"] == "600000"]["date"].max().strftime(
        "%Y-%m-%d"
    ) == "2025-12-31"
