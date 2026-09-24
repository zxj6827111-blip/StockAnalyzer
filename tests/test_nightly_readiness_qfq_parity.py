"""P3.3.1b —— QFQ 对账接进真实 readiness 发布链（任务书 §9 Test 7..12）。

夹具是两份临时 duckdb delta 库 + 一个临时 marker 路径：**不碰生产库、不碰生产
marker 位置**。测的是发布链本身——对账在发布**之前**跑、不过就不写文件、
消费者拿不到旧 PASS、修好后能恢复且不产生重复逻辑键。
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

import stock_analyzer.ops.nightly_readiness as nr
from stock_analyzer.ops.nightly_readiness import (
    check_nightly_readiness,
    write_nightly_readiness,
)

TARGET = "2026-08-19"
#: symbol -> 有因子的日期。600000 在 08-18 **没有**因子时用于构造 Case B。
DAYS = ("2026-08-17", "2026-08-18", "2026-08-19")
FACTORS_FULL = {"000001": DAYS, "600000": DAYS}


def _db(path: Path, *, rows: dict[str, tuple[str, ...]], mode: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [(symbol, day, 10.0, mode) for symbol, days in rows.items() for day in days]
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE daily_bars ("
            "symbol VARCHAR, date DATE, close DOUBLE, price_series_mode VARCHAR)"
        )
        connection.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?)", payload)
    return path


def _index(path: Path, *, symbols: tuple[str, ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbols_total": len(symbols),
                "symbols": {symbol: {"latest_date": TARGET} for symbol in symbols},
            }
        ),
        encoding="utf-8",
    )
    return path


def _pair(
    tmp_path: Path,
    *,
    feature_rows: dict[str, tuple[str, ...]] | None = None,
    execution_rows: dict[str, tuple[str, ...]] | None = None,
    symbols: tuple[str, ...] = ("000001", "600000"),
) -> tuple[Path, Path, Path]:
    feature_rows = feature_rows or {symbol: DAYS for symbol in symbols}
    execution_rows = execution_rows or feature_rows
    return (
        _index(tmp_path / "index.json", symbols=symbols),
        _db(tmp_path / "feature.duckdb", rows=feature_rows, mode="qfq"),
        _db(tmp_path / "execution.duckdb", rows=execution_rows, mode="raw"),
    )


def _write(
    *,
    index_path: Path,
    feature_db: Path,
    execution_db: Path,
    out: Path,
    factors: dict[str, tuple[str, ...]] | None = FACTORS_FULL,
) -> Path:
    kwargs: dict[str, object] = {
        "target_trade_date": TARGET,
        "index_path": index_path,
        "db_path": feature_db,
        "execution_db_path": execution_db,
        "path": out,
        "verify_raw_baseline": False,
    }
    if factors is not None:
        kwargs["qfq_factor_days"] = factors
    write_nightly_readiness(**kwargs)  # type: ignore[arg-type]
    return out


def _publish(
    tmp_path: Path,
    *,
    out: Path | None = None,
    factors: dict[str, tuple[str, ...]] | None = FACTORS_FULL,
    feature_rows: dict[str, tuple[str, ...]] | None = None,
    execution_rows: dict[str, tuple[str, ...]] | None = None,
    symbols: tuple[str, ...] = ("000001", "600000"),
) -> Path:
    index_path, feature_db, execution_db = _pair(
        tmp_path, feature_rows=feature_rows, execution_rows=execution_rows, symbols=symbols
    )
    return _write(
        index_path=index_path,
        feature_db=feature_db,
        execution_db=execution_db,
        out=out or tmp_path / "ready.json",
        factors=factors,
    )


def test_qfq1_aligned_deltas_publish_evaluated_parity(tmp_path: Path) -> None:
    """Test 1 / §6：正常完整时对账**执行了**且 PASS，"缺多少/哪几天"可直接读出。"""
    out = _publish(tmp_path)
    parity = json.loads(out.read_text(encoding="utf-8"))["qfq_parity"]
    assert parity["evaluated"] is True and parity["ok"] is True
    assert parity["derivation_gap_count"] == 0
    assert parity["factor_missing_count"] == 0
    assert parity["qfq_present_raw_missing_count"] == 0
    assert parity["window"] == [DAYS[0], DAYS[-1]]
    assert parity["affected_dates"] == []


def test_qfq2_derivation_gap_blocks_publication(tmp_path: Path) -> None:
    """Test 7（§15）：raw 有 + 因子有 + qfq 没有 → **不发布**，原因码带计数。

    这就是 2026-07-17..07-30 那 295 个键的形状：两只票在目标日都齐，成员锁步
    完全正常，只有**逐键**对账看得见"某一天少了一行"。
    """
    out = tmp_path / "ready.json"
    with pytest.raises(ValueError, match="QFQ parity check failed") as excinfo:
        _publish(
            tmp_path,
            out=out,
            feature_rows={
                "000001": ("2026-08-18", "2026-08-19"),
                "600000": ("2026-08-19",),
            },
            execution_rows={
                "000001": ("2026-08-18", "2026-08-19"),
                "600000": ("2026-08-18", "2026-08-19"),
            },
        )
    assert "QFQ_DERIVATION_GAP:1" in str(excinfo.value)
    assert not out.exists(), "失败的发布不得留下任何可消费 marker"


def test_qfq3_missing_factor_is_recorded_but_not_blocked(tmp_path: Path) -> None:
    """Test 4 + §5：因子根本没有 → 只记账、**不拦**。

    拦它的是导入侧那一轮的 ``ok=false`` 与非零退出（见
    ``test_import_vendor_zip_to_delta.py::test_full_import_fails_closed_on_missing_qfq_factor``）。
    这条测试存在的意义是**防止反向误伤**：把 factor-missing 也判成故障会每晚误杀
    新股与因子尚未发布的票，而那正是 ``_lock_step_symbol_membership`` 包含链
    （``extra_execution_vs_expected`` 不阻塞）明确容忍的既有语义。
    """
    out = _publish(
        tmp_path,
        factors={"000001": DAYS},
        symbols=("000001",),  # 索引只声明 feature 侧真有的票，否则先撞成员锁步（另一维度）
        feature_rows={"000001": DAYS},
        execution_rows={"000001": DAYS, "600000": DAYS},
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["qfq_parity"]["ok"] is True
    assert payload["qfq_parity"]["factor_missing_count"] == len(DAYS)
    assert payload["qfq_parity"]["derivation_gap_count"] == 0
    for strict in (False, True):
        gate = check_nightly_readiness(
            expected_trade_date=TARGET, path=out, require_dual_delta=strict
        )
        assert gate.ready is True, gate.reason


def test_qfq4_qfq_row_without_raw_blocks(tmp_path: Path) -> None:
    """Test 5：qfq 有行而 raw 没有 → 派生物没有源，结构不一致，同样阻塞。"""
    with pytest.raises(ValueError, match="QFQ_ROW_WITHOUT_RAW:1"):
        _publish(
            tmp_path,
            feature_rows={
                "000001": ("2026-08-18", "2026-08-19"),
                "600000": ("2026-08-18", "2026-08-19"),
            },
            execution_rows={
                "000001": ("2026-08-18", "2026-08-19"),
                "600000": ("2026-08-19",),
            },
        )


def test_qfq5_unattributed_asymmetry_is_not_ready_for_the_strict_consumer(
    tmp_path: Path,
) -> None:
    """§12：有 raw-only 差异、却拿不到因子索引归因时，不得当成 PASS。

    "未证明一致"不等于"已证明一致"。但也不得反过来把 Week5 / Legacy 的既有契约
    在这里悄悄收紧——所以只有 active Alpha epoch（``require_dual_delta=True``）拒绝，
    且计数照样写进区块，缺陷不会因为没归因就隐身。

    注意与 qfq1 的区别：两份库**完全对齐**时根本不需要因子索引（没有差异要归因），
    那种情况下 ``evaluated=true / ok=true`` 是诚实的。
    """
    out = _publish(
        tmp_path,
        factors=None,
        symbols=("000001",),
        feature_rows={"000001": DAYS},
        execution_rows={"000001": DAYS, "600000": DAYS},
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    parity = payload["qfq_parity"]
    assert parity["evaluated"] is False
    assert parity["ok"] is False
    assert parity["raw_present_qfq_missing"] == len(DAYS), "未归因也必须看得见规模"
    assert parity["factor_index_consulted"] is False
    assert (
        check_nightly_readiness(
            expected_trade_date=TARGET, path=out, require_dual_delta=True
        ).ready
        is False
    )
    assert check_nightly_readiness(expected_trade_date=TARGET, path=out).ready is True


def test_qfq5b_aligned_deltas_need_no_factor_index(tmp_path: Path) -> None:
    """没有差异时不该去解析因子包，且这算**已证明**一致（不是"没查"）。"""
    out = _publish(tmp_path, factors=None)
    parity = json.loads(out.read_text(encoding="utf-8"))["qfq_parity"]
    assert parity["evaluated"] is True and parity["ok"] is True
    assert parity["factor_index_consulted"] is False
    assert (
        check_nightly_readiness(
            expected_trade_date=TARGET, path=out, require_dual_delta=True
        ).ready
        is True
    )


def test_qfq6_failed_run_leaves_nothing_consumable_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 16 + 17：PASS → 失败 → 旧 marker 不可消费 → 修好后重发布且不重复键。

    失败本身**不删**昨天的 marker；删它的是"每轮开头先 retire"
    （``invalidate_nightly_readiness``，updater 在动任何数据之前调用）。
    两件事合起来才成立：一次失败运行既不发布新 PASS，也不留下旧 PASS。
    """
    out = tmp_path / "runtime" / "nightly_data_ready.json"
    out.parent.mkdir(parents=True)
    monkeypatch.setattr(nr, "_candidate_readiness_paths", lambda: [out])
    index_path, feature_db, execution_db = _pair(tmp_path)

    def _write_run() -> Path:
        return _write(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=out,
        )

    def _ready() -> bool:
        return bool(
            check_nightly_readiness(
                expected_trade_date=TARGET, path=out, require_dual_delta=True
            ).ready
        )

    # run A：一切正常
    _write_run()
    assert out.exists() and _ready() is True

    # run B：先按生产契约 retire，再把 execution 补一行而 qfq 没跟上（=派生缺陷）
    nr.invalidate_nightly_readiness()
    assert not out.exists(), "retire 之后不得还有可消费的 PASS"
    with duckdb.connect(str(feature_db)) as connection:
        connection.execute("DELETE FROM daily_bars WHERE symbol='000001' AND date='2026-08-17'")
    with pytest.raises(ValueError, match="QFQ_DERIVATION_GAP:1"):
        _write_run()
    assert _ready() is False
    assert not out.exists(), "对账失败的一轮不得发布 PASS"

    # run C：补齐 qfq 后重发布；已写入的部分数据不重复、不回滚
    with duckdb.connect(str(feature_db)) as connection:
        connection.execute(
            "INSERT INTO daily_bars VALUES ('000001', DATE '2026-08-17', 10.0, 'qfq')"
        )
    _write_run()
    assert _ready() is True
    aligned = len(DAYS) * 2
    for db, mode, expected_rows in ((feature_db, "qfq", aligned), (execution_db, "raw", aligned)):
        with duckdb.connect(str(db), read_only=True) as connection:
            dupes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT symbol, date FROM daily_bars "
                    "GROUP BY symbol, date HAVING COUNT(*) > 1)"
                ).fetchone()[0]
            )
            rows = int(connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
            modes = {
                str(item[0])
                for item in connection.execute(
                    "SELECT DISTINCT price_series_mode FROM daily_bars"
                ).fetchall()
            }
        assert dupes == 0, f"{db} 出现重复逻辑键"
        assert rows == expected_rows, db
        assert modes == {mode}, db
