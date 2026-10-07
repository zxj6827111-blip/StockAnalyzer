"""带时刻分钟行情研究库的验收（改进计划 §3.1 数据补齐 / §5 继续采集）。

钉住的是"时刻信息在不在、口径说不说得清、契约吃不吃得下"，
不是"源 ZIP 里到底有几天数据"。
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    evaluate_tail_entry,
    simulate_tail_exit,
)
from stock_analyzer.data.intraday_summary_builder import entry_symbol
from stock_analyzer.research.minute_bar_store import (
    BAR_TIME_BAR_END,
    BAR_TIME_BAR_START,
    PRICE_BASIS_QFQ,
    PRICE_BASIS_RAW,
    SOURCE_VENDOR_ZIP,
    MinuteBarStore,
    MinuteStoreError,
    coverage_report,
    read_vendor_zip_minutes,
)

DAY = date(2026, 10, 9)
ENTRY_NAME = "sh600000_20261009.csv"
SYMBOL = entry_symbol(ENTRY_NAME)
CONTRACT = DEFAULT_TREND_CONTRACT


def _csv(*, price: float = 10.0, first: int = 20, last: int = 49) -> str:
    """14:{first}-14:{last} 的分钟 bar，列名与 vendor 源一致。"""
    lines = ["datetime,open,high,low,close,volume,amount"]
    for minute in range(first, last + 1):
        stamp = datetime(2026, 10, 9, 14, minute).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{stamp},{price},{price},{price},{price},1000,10000")
    return "\n".join(lines) + "\n"


def _zip(tmp_path: Path, *, csv_text: str | None = None, name: str = ENTRY_NAME) -> Path:
    path = tmp_path / "part.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, csv_text if csv_text is not None else _csv())
    return path


def _store(tmp_path: Path) -> MinuteBarStore:
    return MinuteBarStore(tmp_path / "research" / "tail_minute_bars.duckdb")


def _ingest(store: MinuteBarStore, archive: Path, **kwargs) -> int:
    frame = read_vendor_zip_minutes(archive)
    return store.upsert_frame(
        frame,
        interval=kwargs.pop("interval", "1m"),
        price_basis=kwargs.pop("price_basis", PRICE_BASIS_RAW),
        bar_time_semantics=kwargs.pop("bar_time_semantics", BAR_TIME_BAR_END),
        source=kwargs.pop("source", SOURCE_VENDOR_ZIP),
    )


def test_vendor_symbol_is_normalized_the_same_way_as_the_summary_builder() -> None:
    assert SYMBOL.startswith("600000")


def test_round_trip_keeps_every_bar_time(tmp_path) -> None:
    archive = _zip(tmp_path)
    store = _store(tmp_path)
    try:
        assert _ingest(store, archive) == 30
        bars = store.bars_for(SYMBOL, DAY)
        assert len(bars) == 30
        assert bars[0][0] == datetime(2026, 10, 9, 14, 20)
        assert bars[-1][0] == datetime(2026, 10, 9, 14, 49)
        assert [ts for ts, _ in bars] == sorted(ts for ts, _ in bars)
        assert bars[0][1]["close"] == pytest.approx(10.0)
    finally:
        store.close()


def test_bar_start_semantics_is_shifted_into_completion_time(tmp_path) -> None:
    archive = _zip(tmp_path)
    store = _store(tmp_path)
    try:
        _ingest(store, archive, bar_time_semantics=BAR_TIME_BAR_START)
        assert store.bars_for(SYMBOL, DAY)[0][0] == datetime(2026, 10, 9, 14, 21)
    finally:
        store.close()


def test_upsert_refuses_undeclared_basis_semantics_and_source(tmp_path) -> None:
    frame = read_vendor_zip_minutes(_zip(tmp_path))
    store = _store(tmp_path)
    try:
        with pytest.raises(MinuteStoreError, match="price_basis"):
            store.upsert_frame(frame, interval="1m", price_basis="",
                               bar_time_semantics=BAR_TIME_BAR_END,
                               source=SOURCE_VENDOR_ZIP)
        with pytest.raises(MinuteStoreError, match="bar_time_semantics"):
            store.upsert_frame(frame, interval="1m", price_basis=PRICE_BASIS_RAW,
                               bar_time_semantics="guess", source=SOURCE_VENDOR_ZIP)
        with pytest.raises(MinuteStoreError, match="source"):
            store.upsert_frame(frame, interval="1m", price_basis=PRICE_BASIS_RAW,
                               bar_time_semantics=BAR_TIME_BAR_END, source="usb_stick")
        with pytest.raises(MinuteStoreError, match="interval"):
            store.upsert_frame(frame, interval="15m", price_basis=PRICE_BASIS_RAW,
                               bar_time_semantics=BAR_TIME_BAR_END,
                               source=SOURCE_VENDOR_ZIP)
    finally:
        store.close()


def test_qfq_minute_prices_are_refused_for_fill_simulation(tmp_path) -> None:
    archive = _zip(tmp_path)
    store = _store(tmp_path)
    try:
        _ingest(store, archive, price_basis=PRICE_BASIS_QFQ)
        with pytest.raises(MinuteStoreError, match="raw"):
            store.bars_for(SYMBOL, DAY)
        # 只有明确不用于成交时才允许读复权价（例如画特征）。
        assert len(store.bars_for(SYMBOL, DAY, require_raw=False)) == 30
    finally:
        store.close()


def test_tail_window_coverage_requires_a_bar_after_the_last_slot(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        assert store.tail_window_coverage().status == "blocked"
        # 14:45 是最后一个确认点，14:45 之后就断掉 → 没有可成交的下一根。
        _ingest(store, _zip(tmp_path, csv_text=_csv(last=45)))
        coverage = store.tail_window_coverage()
        assert coverage.status == "insufficient"
        assert coverage.detail["missing_next_bar_symbol_days"] == 1
        assert coverage.symbol_days == 1
        assert coverage.slots_per_day == len(CONTRACT.confirmation_slots)
    finally:
        store.close()


def test_tail_window_coverage_counts_complete_symbol_days(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _ingest(store, _zip(tmp_path))
        coverage = store.tail_window_coverage()
        assert coverage.status == "ok"
        assert coverage.complete_symbol_days == 1
        assert coverage.days == 1
        report = coverage_report(store)
        assert report["table"] == "minute_bars_1min"
        assert report["first_day"] == "2026-10-09"
        assert report["tail_window"]["complete_symbol_days"] == 1
    finally:
        store.close()


def test_stored_bars_drive_the_tail_entry_and_exit_rules(tmp_path) -> None:
    """研究库读出来的形状必须能直接喂契约，不额外转换。"""
    archive = _zip(tmp_path)
    store = _store(tmp_path)
    try:
        _ingest(store, archive)
        bars = store.bars_for(
            SYMBOL, DAY, day_limits={"up_limit": 11.0, "trade_status": "normal"}
        )
        decision = evaluate_tail_entry(
            symbol=SYMBOL, trading_day=DAY, minute_bars=bars,
            confirmation=lambda context: (True, ""),
        )
        assert decision.filled, decision.no_fill_reason
        # 14:30 确认后按下一根（14:31）成交，正是契约写死的口径。
        assert decision.confirmation_slot == datetime(2026, 10, 9, 14, 30)
        assert decision.fill_time == datetime(2026, 10, 9, 14, 31)

        exit_result = simulate_tail_exit(
            symbol=SYMBOL, entry_date=DAY,
            entry_price=float(decision.net_fill_price or 0.0),
            quantity=int(decision.quantity), buy_cost=float(decision.buy_cost),
            daily_bars=[
                (DAY, {"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0,
                       "trade_status": "normal"}),
                *[(day, {"open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2,
                         "trade_status": "normal"})
                  for day in (date(2026, 10, 12), date(2026, 10, 13),
                              date(2026, 10, 14))],
                (date(2026, 10, 15), {"open": 10.2, "high": 10.3, "low": 10.1,
                                      "close": 10.25, "trade_status": "normal"}),
            ],
            contract=CONTRACT,
        )
        assert exit_result.status == "filled_and_exited"
        assert exit_result.take_profit_hit is False
        assert exit_result.stop_loss_hit is False
        # 入场日算第 1 日 → 第 5 个交易日（10-15）收盘退出。
        assert exit_result.exit_date == datetime(2026, 10, 15)
        assert exit_result.net_return is not None
    finally:
        store.close()


def test_missing_limit_prices_stay_visible_instead_of_being_filled(
    tmp_path,
) -> None:
    """分钟源没有 up_limit：不替它编一个，让契约自己判无有效价格。"""
    store = _store(tmp_path)
    try:
        _ingest(store, _zip(tmp_path))
        bars = store.bars_for(SYMBOL, DAY)
        assert all("up_limit" not in bar for _, bar in bars)
        decision = evaluate_tail_entry(
            symbol=SYMBOL, trading_day=DAY, minute_bars=bars,
            confirmation=lambda context: (True, ""),
        )
        assert decision.filled is False
        assert decision.no_fill_reason == "no_valid_price_data"
    finally:
        store.close()


def test_upsert_is_idempotent_on_duplicate_bar_times(tmp_path) -> None:
    archive = _zip(tmp_path)
    store = _store(tmp_path)
    try:
        _ingest(store, archive)
        _ingest(store, archive)  # 重跑同一批源不该翻倍
        assert len(store.bars_for(SYMBOL, DAY)) == 30
    finally:
        store.close()


def test_read_vendor_zip_minutes_filters_by_symbol_and_date(tmp_path) -> None:
    archive = _zip(tmp_path)
    frame = read_vendor_zip_minutes(archive, symbols=["不存在的代码"])
    assert frame.empty
    frame = read_vendor_zip_minutes(archive, start=date(2026, 10, 10))
    assert frame.empty
    with pytest.raises(MinuteStoreError, match="does not exist"):
        read_vendor_zip_minutes(tmp_path / "nope.zip")


def test_corrupt_entries_are_skipped_not_fatal(tmp_path) -> None:
    path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(ENTRY_NAME, _csv())
        archive.writestr("sz000001_20261009.csv", "not,a,minute,header\nbroken\n")
    frame = read_vendor_zip_minutes(path)
    assert sorted(frame["symbol"].unique().tolist()) == [SYMBOL]


def test_sync_cli_fails_visibly_when_the_source_root_is_missing(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "sync_tail_minute_bars.py"),
         "--root", str(tmp_path / "nowhere"), "--out", str(tmp_path / "x.duckdb"),
         "--price-basis", "raw", "--bar-time-semantics", "bar_end",
         "--report", ""],
        capture_output=True, text=True, cwd=str(root), check=False,
    )
    assert result.returncode == 5
    assert "vendor root does not exist" in result.stderr


def test_normalize_helper_rejects_ambiguous_clock() -> None:
    from stock_analyzer.research.minute_bar_store import _minute_of

    assert _minute_of("14:30") == 14 * 60 + 30
    with pytest.raises(MinuteStoreError):
        _minute_of("1430")


def test_research_store_flips_the_tail_window_readiness_check(tmp_path) -> None:
    """就绪门只认"真的有带时刻的 bar"，不看有没有人声称采到了。"""
    import duckdb

    from stock_analyzer.research.trend_data_readiness import (
        audit_trend_data_readiness,
        readiness_exit_code,
    )

    warehouse = duckdb.connect(":memory:")
    store = MinuteBarStore(tmp_path / "research" / "m.duckdb")
    try:
        _ingest(store, _zip(tmp_path))
    finally:
        store.close()
    try:
        empty_report = audit_trend_data_readiness(connection=warehouse)
        tail = [item for item in empty_report["checks"]
                if item["name"] == "tail_window_minute_bars"][0]
        assert tail["status"] == "blocked"

        minute_conn = duckdb.connect(str(store.path), read_only=True)
        try:
            report = audit_trend_data_readiness(
                connection=warehouse, minute_connection=minute_conn
            )
        finally:
            minute_conn.close()
        tail = [item for item in report["checks"]
                if item["name"] == "tail_window_minute_bars"][0]
        assert tail["status"] == "ok"
        assert tail["detail"]["research_minute:1min"]["rows"] == 30
        # 分钟补齐了也不等于整体 ready：其它硬门仍然压着退出码。
        assert report["readiness"] != "ready"
        assert readiness_exit_code(report) == 5
    finally:
        warehouse.close()
