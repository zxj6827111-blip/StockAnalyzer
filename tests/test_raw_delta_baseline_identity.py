"""RAW execution delta 的基线身份与覆盖校验（P1 §6–§8）。

这些测试盯的是一件事：**"raw 库存在"不等于"raw 基线成立"**。一个用
``--incremental`` 在空路径上跑出来的浅深度库看起来完全正常，却支撑不了候选模型的
source window；而一旦它被当成基线，执行侧的价格口径就再没人拦得住了。

所以这里覆盖两条互补的判据：

- :func:`evaluate_coverage` 的实测覆盖（窗口 / 符号 / 行 / 口径 / 重复行）；
- :func:`verify_bootstrap_marker` 的身份门（marker 声明 + 库的单调不变量）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import duckdb
import pytest

from stock_analyzer.ops.raw_delta_baseline import (
    RAW_BOOTSTRAP_MARKER_SCHEMA,
    RAW_DELTA_PRICE_MODE,
    REASON_BASELINE_MISSING,
    REASON_COVERAGE,
    REASON_DB_IDENTITY,
    REASON_MARKER_UNREADABLE,
    REASON_PRICE_MODE,
    RawDeltaBaselineError,
    bootstrap_marker_path,
    build_bootstrap_marker,
    symbol_set_hash,
    verify_bootstrap_marker,
    write_bootstrap_marker,
)

ROOT = Path(__file__).resolve().parents[1]
_COVERAGE_SCRIPT = ROOT / "scripts" / "alpha_v2_raw_delta_coverage.py"


def _load_coverage_module() -> object:
    spec = importlib.util.spec_from_file_location("alpha_v2_raw_delta_coverage", _COVERAGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def coverage_module() -> object:
    return _load_coverage_module()


WINDOW_START = "2025-01-06"
WINDOW_END = "2025-01-10"
DATES = ("2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10")


def _make_delta_db(
    path: Path,
    *,
    rows: dict[str, tuple[str, ...]],
    mode: str = "raw",
    extra_rows: dict[str, tuple[str, ...]] | None = None,
) -> Path:
    """构造一份 delta 库：``rows[symbol]`` 是目标窗口内的日期，``extra_rows`` 在窗口外。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: list[tuple[object, ...]] = []
    for symbol, dates in rows.items():
        for item in dates:
            payload.append((symbol, item, 10.0, mode))
    for symbol, dates in (extra_rows or {}).items():
        for item in dates:
            payload.append((symbol, item, 10.0, mode))
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                symbol VARCHAR, date DATE, close DOUBLE, price_series_mode VARCHAR
            )
            """
        )
        if payload:
            connection.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?)", payload)
    return path


def _make_index(path: Path, *, latest_date: str, symbols: tuple[str, ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "symbols_total": len(symbols),
                "symbols": {
                    symbol: {
                        "latest_date": latest_date,
                        "entries": [
                            {"year": 2025, "zip": "全A日K/2025.zip", "entry": f"2025/{symbol}.csv"}
                        ],
                    }
                    for symbol in symbols
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _evaluate(
    coverage_module: object,
    *,
    raw_db: Path,
    feature_db: Path,
    index_path: Path,
    **kwargs: object,
) -> dict[str, object]:
    return coverage_module.evaluate_coverage(
        raw_db=raw_db,
        feature_db=feature_db,
        index_path=index_path,
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
        skip_price_mode_certification=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 8.1–8.5 覆盖判定
# ---------------------------------------------------------------------------


def test_coverage_pass_with_identical_rows(coverage_module: object, tmp_path: Path) -> None:
    rows = {"600000": DATES, "000001": DATES}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=rows, mode="qfq")
    index_path = _make_index(
        tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000", "000001")
    )

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "PASS"
    assert report["blockers"] == []
    assert report["price_mode_check"]["observed"] == "raw"
    assert report["symbol_coverage"]["symbols_expected"] == 2
    assert report["symbol_coverage"]["missing_symbols"] == 0
    assert (
        report["symbol_coverage"]["symbol_set_hash_expected"]
        == report["symbol_coverage"]["symbol_set_hash_raw"]
    )
    assert report["row_coverage"]["missing_in_raw"] == 0


def test_coverage_blocked_when_raw_is_too_shallow(coverage_module: object, tmp_path: Path) -> None:
    """--limit-days 造出的浅深度基线：窗口起点没被覆盖。"""
    feature_rows = {"600000": DATES}
    # raw 只从窗口中间开始——正是"用默认 400 行在空路径上补导"的典型产物。
    raw_rows = {"600000": ("2025-01-09", "2025-01-10")}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=raw_rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=feature_rows, mode="qfq")
    index_path = _make_index(tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000",))

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert any(item.startswith("raw_window_start_not_covered") for item in report["blockers"])
    assert report["row_coverage"]["missing_in_raw"] == 3


def test_coverage_blocked_when_raw_window_end_not_covered(
    coverage_module: object, tmp_path: Path
) -> None:
    rows = {"600000": DATES[:-1]}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=rows, mode="qfq")
    index_path = _make_index(tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000",))

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert any(item.startswith("raw_window_end_not_covered") for item in report["blockers"])


def test_coverage_blocked_on_missing_symbol(coverage_module: object, tmp_path: Path) -> None:
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows={"600000": DATES}, mode="raw")
    feature_db = _make_delta_db(
        tmp_path / "feature.duckdb", rows={"600000": DATES, "000001": DATES}, mode="qfq"
    )
    index_path = _make_index(
        tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000", "000001")
    )

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert any(item.startswith("raw_missing_symbols:1") for item in report["blockers"])
    assert report["symbol_coverage"]["missing_examples"] == ["000001"]


def test_coverage_blocked_on_duplicate_symbol_date(coverage_module: object, tmp_path: Path) -> None:
    raw_db = _make_delta_db(
        tmp_path / "raw.duckdb",
        rows={"600000": DATES},
        # 同一 (symbol,date) 再来一行：增量重试写成重复行是真实发生过的事故类型。
        extra_rows={},
        mode="raw",
    )
    with duckdb.connect(str(raw_db)) as connection:
        connection.execute("INSERT INTO daily_bars VALUES ('600000', '2025-01-06', 11.0, 'raw')")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows={"600000": DATES}, mode="qfq")
    index_path = _make_index(tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000",))

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert any(item.startswith("raw_duplicate_symbol_date") for item in report["blockers"])


def test_raw_extra_symbols_and_rows_are_not_gaps(coverage_module: object, tmp_path: Path) -> None:
    """raw 多出来的符号/行不算缺口：qfq 侧因子缺失会被跳过，raw 侧不依赖因子。"""
    feature_rows = {"600000": DATES}
    raw_rows = {"600000": DATES, "000001": DATES}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=raw_rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=feature_rows, mode="qfq")
    index_path = _make_index(
        tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000", "000001")
    )

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "PASS"
    assert report["symbol_coverage"]["extra_symbols"] == 1
    # 000001 在 feature 侧没有（qfq 因子缺失会被跳过），raw 侧那 5 行是 extra 而非缺口。
    assert report["row_coverage"]["extra_in_raw"] == 5
    assert report["row_coverage"]["missing_in_raw"] == 0


@pytest.mark.parametrize(
    ("mode", "blocker_prefix"),
    [("qfq", "raw_price_mode_not_raw:qfq"), ("", "raw_price_mode_not_raw:unknown")],
)
def test_coverage_blocked_on_non_raw_mode(
    coverage_module: object, tmp_path: Path, mode: str, blocker_prefix: str
) -> None:
    rows = {"600000": DATES}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=rows, mode=mode)
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=rows, mode="qfq")
    index_path = _make_index(tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000",))

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert blocker_prefix in report["blockers"]


def test_coverage_blocked_on_mixed_mode(coverage_module: object, tmp_path: Path) -> None:
    """一个 symbol 声明 raw、另一个声明 qfq —— 混口径必须拦下。"""
    raw_db = tmp_path / "raw.duckdb"
    _make_delta_db(raw_db, rows={"600000": DATES}, mode="raw")
    with duckdb.connect(str(raw_db)) as connection:
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?, ?, ?)",
            [("000001", item, 10.0, "qfq") for item in DATES],
        )
    feature_db = _make_delta_db(
        tmp_path / "feature.duckdb", rows={"600000": DATES, "000001": DATES}, mode="qfq"
    )
    index_path = _make_index(
        tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000", "000001")
    )

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert "raw_price_mode_not_raw:mixed" in report["blockers"]


def test_coverage_blocked_when_index_cannot_reach_window_end(
    coverage_module: object, tmp_path: Path
) -> None:
    rows = {"600000": DATES}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=rows, mode="qfq")
    index_path = _make_index(tmp_path / "index.json", latest_date="2025-01-08", symbols=("600000",))

    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)

    assert report["coverage_status"] == "BLOCKED"
    assert any(
        item.startswith("index_latest_before_source_window_end") for item in report["blockers"]
    )


def test_coverage_reuses_alpha_v2_price_mode_certification(
    coverage_module: object, tmp_path: Path
) -> None:
    """不另写判据：认证走 Alpha V2 的 certify_price_mode，证据原样进报告。"""
    rows = {"600000": DATES, "000001": DATES}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=rows, mode="qfq")
    index_path = _make_index(
        tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000", "000001")
    )

    report = coverage_module.evaluate_coverage(
        raw_db=raw_db,
        feature_db=feature_db,
        index_path=index_path,
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
        skip_price_mode_certification=False,
    )

    assert report["coverage_status"] == "PASS"
    certification = report["price_mode_check"]["certification"]
    assert certification["mode"] == RAW_DELTA_PRICE_MODE
    assert certification["certified"] is True
    assert certification["evidence"]["decision_rule"] == "panel_rows_declare_raw"


# ---------------------------------------------------------------------------
# §6 Bootstrap marker：身份、单调不变量、写入前置
# ---------------------------------------------------------------------------


def _pass_report(coverage_module: object, tmp_path: Path) -> dict[str, object]:
    rows = {"600000": DATES}
    raw_db = _make_delta_db(tmp_path / "raw.duckdb", rows=rows, mode="raw")
    feature_db = _make_delta_db(tmp_path / "feature.duckdb", rows=rows, mode="qfq")
    index_path = _make_index(tmp_path / "index.json", latest_date=WINDOW_END, symbols=("600000",))
    report = _evaluate(coverage_module, raw_db=raw_db, feature_db=feature_db, index_path=index_path)
    assert report["coverage_status"] == "PASS"
    report["_paths"] = {"raw": raw_db, "feature": feature_db, "index": index_path}
    return report


def test_bootstrap_marker_written_only_from_pass(coverage_module: object, tmp_path: Path) -> None:
    report = _pass_report(coverage_module, tmp_path)
    paths = report["_paths"]
    payload = build_bootstrap_marker(
        db_path=paths["raw"],
        coverage_report=report,
        source_index_path=paths["index"],
        source_index_hash="deadbeef",
        source_index_latest_date=WINDOW_END,
        build_commit="abc1234",
    )
    marker_path = write_bootstrap_marker(payload, raw_db_path=paths["raw"])

    assert marker_path == bootstrap_marker_path(paths["raw"])
    on_disk = json.loads(marker_path.read_text(encoding="utf-8"))
    assert on_disk["schema"] == RAW_BOOTSTRAP_MARKER_SCHEMA
    assert on_disk["price_series_mode"] == "raw"
    assert on_disk["coverage_status"] == "PASS"
    assert on_disk["required_source_window"] == {"start": WINDOW_START, "end": WINDOW_END}
    assert on_disk["build_commit"] == "abc1234"
    # 身份用内容事实而不是整库 SHA256：随每日增量变化，只作取证快照。
    assert on_disk["db_content_identity"]["rows"] == 5
    assert "db_sha256" not in on_disk

    verified = verify_bootstrap_marker(raw_db_path=paths["raw"])
    assert verified["schema"] == RAW_BOOTSTRAP_MARKER_SCHEMA


def test_bootstrap_marker_refuses_blocked_coverage(coverage_module: object, tmp_path: Path) -> None:
    report = _pass_report(coverage_module, tmp_path)
    report["coverage_status"] = "BLOCKED"
    with pytest.raises(RawDeltaBaselineError) as excinfo:
        build_bootstrap_marker(
            db_path=report["_paths"]["raw"],
            coverage_report=report,
            source_index_path=report["_paths"]["index"],
            source_index_hash="x",
            source_index_latest_date=WINDOW_END,
        )
    assert excinfo.value.reason == REASON_COVERAGE


def test_verify_marker_requires_the_file(coverage_module: object, tmp_path: Path) -> None:
    report = _pass_report(coverage_module, tmp_path)
    raw_db = report["_paths"]["raw"]
    with pytest.raises(RawDeltaBaselineError) as excinfo:
        verify_bootstrap_marker(raw_db_path=raw_db)
    assert excinfo.value.reason == REASON_MARKER_UNREADABLE


def test_verify_marker_rejects_missing_db(tmp_path: Path) -> None:
    with pytest.raises(RawDeltaBaselineError) as excinfo:
        verify_bootstrap_marker(raw_db_path=tmp_path / "not-there.duckdb")
    assert excinfo.value.reason == REASON_BASELINE_MISSING


def test_verify_marker_rejects_qfq_db(coverage_module: object, tmp_path: Path) -> None:
    """marker 说 raw、库实际 qfq：库被换成另一份，必须拦下。"""
    report = _pass_report(coverage_module, tmp_path)
    raw_db = report["_paths"]["raw"]
    payload = build_bootstrap_marker(
        db_path=raw_db,
        coverage_report=report,
        source_index_path=report["_paths"]["index"],
        source_index_hash="x",
        source_index_latest_date=WINDOW_END,
    )
    write_bootstrap_marker(payload, raw_db_path=raw_db)
    with duckdb.connect(str(raw_db)) as connection:
        connection.execute("UPDATE daily_bars SET price_series_mode = 'qfq'")

    with pytest.raises(RawDeltaBaselineError) as excinfo:
        verify_bootstrap_marker(raw_db_path=raw_db)
    assert excinfo.value.reason == REASON_PRICE_MODE


def test_verify_marker_rejects_wiped_db(coverage_module: object, tmp_path: Path) -> None:
    """单调不变量：行数/符号数不得少于建基线时记录值——库被清空即身份不成立。"""
    report = _pass_report(coverage_module, tmp_path)
    raw_db = report["_paths"]["raw"]
    payload = build_bootstrap_marker(
        db_path=raw_db,
        coverage_report=report,
        source_index_path=report["_paths"]["index"],
        source_index_hash="x",
        source_index_latest_date=WINDOW_END,
    )
    write_bootstrap_marker(payload, raw_db_path=raw_db)
    with duckdb.connect(str(raw_db)) as connection:
        connection.execute("DELETE FROM daily_bars")

    with pytest.raises(RawDeltaBaselineError) as excinfo:
        verify_bootstrap_marker(raw_db_path=raw_db)
    assert excinfo.value.reason == REASON_DB_IDENTITY


def test_verify_marker_allows_growth(coverage_module: object, tmp_path: Path) -> None:
    """逐日增量只会让计数增长，正常推进不能被身份门挡住。"""
    report = _pass_report(coverage_module, tmp_path)
    raw_db = report["_paths"]["raw"]
    payload = build_bootstrap_marker(
        db_path=raw_db,
        coverage_report=report,
        source_index_path=report["_paths"]["index"],
        source_index_hash="x",
        source_index_latest_date=WINDOW_END,
    )
    write_bootstrap_marker(payload, raw_db_path=raw_db)
    with duckdb.connect(str(raw_db)) as connection:
        connection.execute("INSERT INTO daily_bars VALUES ('600000', '2025-01-13', 12.0, 'raw')")

    verified = verify_bootstrap_marker(raw_db_path=raw_db)
    assert verified["coverage_status"] == "PASS"


def test_verify_marker_rejects_unknown_schema(coverage_module: object, tmp_path: Path) -> None:
    report = _pass_report(coverage_module, tmp_path)
    raw_db = report["_paths"]["raw"]
    payload = build_bootstrap_marker(
        db_path=raw_db,
        coverage_report=report,
        source_index_path=report["_paths"]["index"],
        source_index_hash="x",
        source_index_latest_date=WINDOW_END,
    )
    payload["schema"] = "alpha_v2_raw_delta_bootstrap.v0"
    write_bootstrap_marker(payload, raw_db_path=raw_db)
    with pytest.raises(RawDeltaBaselineError):
        verify_bootstrap_marker(raw_db_path=raw_db)


def test_symbol_set_hash_distinguishes_membership() -> None:
    """同数量不同成员必须给出不同摘要——这是"计数一样"能漏掉的唯一信号。"""
    assert symbol_set_hash(["600000", "000001"]) == symbol_set_hash(["000001", "600000"])
    assert symbol_set_hash(["600000", "000001"]) != symbol_set_hash(["600000", "600001"])
