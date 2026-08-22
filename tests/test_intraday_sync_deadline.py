"""Deadline is a hard wall: slow probes/fetches must not block beyond it."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stock_analyzer.data.intraday_sync import sync_intraday_symbols


def _slow_fetch_provider(probe_sleep: float = 0.05, fetch_sleep: float = 2.0) -> object:
    """First 2 calls (probes) are fast, subsequent fetches are slow."""

    class _Slow:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_minute_bars(
            self,
            symbol: str,
            start_date: object,
            end_date: object,
            freq: str = "1min",
        ) -> pd.DataFrame:
            self.calls += 1
            if self.calls <= 2:
                time.sleep(probe_sleep)
            else:
                time.sleep(fetch_sleep)
            return pd.DataFrame()

    return _Slow()


def test_deadline_does_not_wait_for_slow_concurrent_fetches(tmp_path: Path) -> None:
    """Concurrent fetches that overrun deadline must not block caller.

    Probes are fast; the 4 concurrent fetches each sleep 2s but deadline
    is 1s — with hard deadline the sync should return well before 4s
    (old bug: shutdown(wait=True) blocked until all 4 workers finished).
    """
    with patch("stock_analyzer.data.intraday_sync._fetch_with_sina", return_value=pd.DataFrame()):
        provider = _slow_fetch_provider(probe_sleep=0.05, fetch_sleep=2.0)
        start = time.monotonic()
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000", "600001", "600002", "600003"],
            required_trade_date="2026-08-18",
            primary="tushare",
            fallback="sina",
            deadline_sec=1,
            concurrency=4,
            timeout_sec=1,
            tushare_provider=provider,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 2.5, f"elapsed {elapsed:.3f}s exceeds hard deadline"
        assert report.elapsed_ms < 2500


def test_deadline_includes_slow_capability_probes() -> None:
    """Two slow probes must share the same wall-clock budget as fetches."""

    class _SlowProbe:
        def fetch_minute_bars(
            self,
            symbol: str,
            start_date: object,
            end_date: object,
            freq: str = "1min",
        ) -> pd.DataFrame:
            time.sleep(2.0)
            return pd.DataFrame()

    with patch(
        "stock_analyzer.data.intraday_sync._fetch_with_sina",
        return_value=pd.DataFrame(),
    ):
        start = time.monotonic()
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000", "600001"],
            required_trade_date="2026-08-18",
            primary="tushare",
            fallback="sina",
            deadline_sec=1,
            concurrency=2,
            timeout_sec=1,
            tushare_provider=_SlowProbe(),
        )
        elapsed = time.monotonic() - start

    assert elapsed < 1.8, f"slow probes escaped hard deadline: {elapsed:.3f}s"
    assert report.capability_probe["tushare_ok"] is False
    assert report.capability_probe["error"] == "deadline_exceeded"


class _BusyLock:
    """模拟跨进程锁被占用：acquire 返回 False。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        pass


class _ExplodingLock:
    """模拟锁文件系统故障：构造即抛错。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("lock path unwritable")


def _assert_sina_primary_probe(probe: dict[str, object]) -> None:
    assert probe == {
        "probed": 0,
        "tushare_ok": None,
        "error": "",
        "note": "primary_not_tushare",
    }


def test_capability_probe_shape_for_sina_primary_bj_only_symbols() -> None:
    """BJ-only 候选走 unsupported_market 早退，probe 不得出现误导性 tushare_ok=False。"""
    report = sync_intraday_symbols(
        warehouse=None,
        symbols=["830001"],
        required_trade_date="2026-08-18",
        primary="sina",
        fallback="sina",
    )
    assert report.unsupported_market == ["830001"]
    assert report.ok == 0
    _assert_sina_primary_probe(report.capability_probe)


def test_capability_probe_shape_for_sina_primary_empty_symbols() -> None:
    report = sync_intraday_symbols(
        warehouse=None,
        symbols=[],
        required_trade_date="2026-08-18",
        primary="sina",
        fallback="sina",
    )
    assert report.symbols_total == 0
    _assert_sina_primary_probe(report.capability_probe)


def test_capability_probe_shape_for_sina_primary_when_lock_busy() -> None:
    with patch("stock_analyzer.ops.file_lock.DistributedFileLock", _BusyLock):
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000"],
            required_trade_date="2026-08-18",
            primary="sina",
            fallback="sina",
        )
    assert report.detail.get("lock_busy") is True
    assert report.skipped == 1
    _assert_sina_primary_probe(report.capability_probe)


def test_capability_probe_shape_for_sina_primary_when_lock_errors() -> None:
    with patch("stock_analyzer.ops.file_lock.DistributedFileLock", _ExplodingLock):
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000"],
            required_trade_date="2026-08-18",
            primary="sina",
            fallback="sina",
        )
    assert "lock_error" in report.detail
    assert report.failed == 1
    _assert_sina_primary_probe(report.capability_probe)


def test_capability_probe_tushare_primary_keeps_false_semantics() -> None:
    """tushare 主源下早退路径维持既有形态（False 表示未探测成功），不回归。"""
    report = sync_intraday_symbols(
        warehouse=None,
        symbols=["830001"],
        required_trade_date="2026-08-18",
        primary="tushare",
        fallback="sina",
    )
    assert report.unsupported_market == ["830001"]
    assert report.capability_probe == {"probed": 0, "tushare_ok": False, "error": ""}


def test_lock_error_report_feeds_all_failed_audit_integration() -> None:
    """NO-GO 复核修复验证：lock 构造异常的真实报告必须能触发 all_failed 审计。

    构造链路与生产一致（真实 sync_intraday_symbols + 打补丁的锁），而不是
    直接给 helper 喂合成 dict——此前该路径因 detail 无 primary 被静默跳过。
    """
    from stock_analyzer.runtime.services.week5_service import (
        _record_intraday_sync_health_audits,
    )

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def _record_audit_event(self, **kwargs: object) -> None:
            self.events.append(dict(kwargs))

    with patch("stock_analyzer.ops.file_lock.DistributedFileLock", _ExplodingLock):
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000"],
            required_trade_date="2026-08-18",
            primary="tushare",
            fallback="sina",
        )

    sync_payload = report.to_dict()
    detail = sync_payload["detail"]
    assert isinstance(detail, dict)
    # lock_error 通过 update 写入，primary/fallback 必须保留。
    assert detail["primary"] == "tushare"
    assert detail["fallback"] == "sina"
    assert "lock_error" in detail
    assert sync_payload["ok"] == 0
    assert sync_payload["failed"] == 1

    recorder = _Recorder()
    _record_intraday_sync_health_audits(service=recorder, sync_report=sync_payload)

    assert len(recorder.events) == 1
    assert recorder.events[0]["event_type"] == "intraday_sync_all_failed"
    assert recorder.events[0]["level"] == "warn"


def test_lock_busy_report_keeps_primary_in_detail() -> None:
    with patch("stock_analyzer.ops.file_lock.DistributedFileLock", _BusyLock):
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000"],
            required_trade_date="2026-08-18",
            primary="sina",
            fallback="sina",
        )

    assert report.detail.get("lock_busy") is True
    assert report.detail.get("primary") == "sina"
    assert report.detail.get("fallback") == "sina"
