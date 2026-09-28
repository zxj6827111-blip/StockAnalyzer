"""夜扫数据新鲜度 fail-closed 门（Phase 1.1）测试。

覆盖两件事：
1. ``market_calendar.latest_expected_trading_day``：节假日正确回退到节前
   最后交易日（9/25-9/27 中秋 → 9/24），不会把正常休市误判为断供；
2. ``_build_data_gate(require_current_trade_date=True)``：行情数据未达最近
   已收盘交易日时 blocked（trade_date_not_current），且配置开关可作紧急
   回滚手段；不传该参数时保持旧行为（regression 保护）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from stock_analyzer.market_calendar import latest_expected_trading_day
from stock_analyzer.runtime.service import StockAnalyzerService
from tests.test_feature_snapshot import _make_service


def test_latest_expected_trading_day_falls_back_over_holiday() -> None:
    # 中秋休市 9/25-9/27：从休市中任何一天回看都应到节前 9/24（周四）。
    assert latest_expected_trading_day(date(2026, 9, 25)) == date(2026, 9, 24)
    assert latest_expected_trading_day(date(2026, 9, 26)) == date(2026, 9, 24)
    assert latest_expected_trading_day(date(2026, 9, 27)) == date(2026, 9, 24)
    # 节后首个交易日当天：期望就是当天。
    assert latest_expected_trading_day(date(2026, 9, 28)) == date(2026, 9, 28)
    # 普通周末回退到周五。
    assert latest_expected_trading_day(date(2026, 10, 10)) == date(2026, 10, 9)


def _gate(
    service: StockAnalyzerService,
    *,
    latest_trade_date: str,
    now: datetime,
    require_current_trade_date: bool,
):
    # 隔离 snapshot 门（feature_snapshot_stale 会先 blocked），聚焦交易日历门自身。
    # 快照存在（trade_date 与 latest_trade_date 一致）——与 engine 实际传参一致。
    service._config.week5.feature_snapshot_require_current = False  # noqa: SLF001
    manifest = SimpleNamespace(trade_date=latest_trade_date)
    return service._build_data_gate(  # noqa: SLF001
        snapshot_manifest=manifest,
        snapshot_current=False,
        latest_trade_date=latest_trade_date,
        now=now,
        require_current_trade_date=require_current_trade_date,
    )


def test_gate_blocks_when_data_misses_latest_trading_day(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    # 复刻 9/24 型场景：节后首日 9/28 晚间扫描，数据仍停在节前 9/24。
    # 旧行为只给 watch_only（自然日差 4 天未超阈值 3→不触发），新门必须 blocked。
    gate = _gate(
        service,
        latest_trade_date="2026-09-24",
        now=datetime(2026, 9, 28, 21, 45, tzinfo=UTC),
        require_current_trade_date=True,
    )
    assert gate["status"] == "blocked"
    assert any(
        reason.startswith("trade_date_not_current:") for reason in gate["reasons"]
    )


def test_gate_allows_holiday_with_prefoliday_data(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    # 中秋假期内（9/27 周日）扫描，数据停在节前 9/24 属于正常状态，不误伤。
    gate = _gate(
        service,
        latest_trade_date="2026-09-24",
        now=datetime(2026, 9, 27, 21, 45, tzinfo=UTC),
        require_current_trade_date=True,
    )
    assert gate["status"] == "ok"
    assert not any(
        reason.startswith("trade_date_not_current") for reason in gate["reasons"]
    )


def test_gate_blocks_missing_trade_date(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    # 快照存在但 trade_date 为空（异常 manifest）：同样按断供处理 blocked。
    # 快照整体缺失时检查跳过——那是 feature_snapshot_stale / recovery 直扫的
    # 管辖范围，不归本门。
    from types import SimpleNamespace

    manifest = SimpleNamespace(trade_date="")
    gate = service._build_data_gate(  # noqa: SLF001
        snapshot_manifest=manifest,
        snapshot_current=False,
        latest_trade_date="",
        now=datetime(2026, 9, 28, 21, 45, tzinfo=UTC),
        require_current_trade_date=True,
    )
    assert gate["status"] == "blocked"
    assert any("trade_date_not_current:missing" in reason for reason in gate["reasons"])


def test_gate_skips_check_when_snapshot_missing(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    # 快照缺失时本门不判 trade_date_not_current：保持 recovery 直扫
    # （snapshot_only_blocked）的既有触发语义。
    service._config.week5.feature_snapshot_require_current = False  # noqa: SLF001
    gate = service._build_data_gate(  # noqa: SLF001
        snapshot_manifest=None,
        snapshot_current=False,
        latest_trade_date="",
        now=datetime(2026, 9, 28, 21, 45, tzinfo=UTC),
        require_current_trade_date=True,
    )
    assert not any(
        reason.startswith("trade_date_not_current") for reason in gate["reasons"]
    )


def test_gate_config_switch_disables_check(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    # 紧急回滚手段：配置开关关闭后不启用交易日历门。
    service._config.week5.require_current_trade_date = False  # noqa: SLF001
    gate = _gate(
        service,
        latest_trade_date="2026-09-24",
        now=datetime(2026, 9, 28, 21, 45, tzinfo=UTC),
        require_current_trade_date=True,
    )
    assert gate["status"] != "blocked" or all(
        not reason.startswith("trade_date_not_current") for reason in gate["reasons"]
    )


def test_gate_default_keeps_legacy_behavior(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path)
    # regression 保护：不传 require_current_trade_date 的既有调用方
    # （历史回放等）行为完全不变——旧日期最多 watch_only，不会 blocked。
    gate = _gate(
        service,
        latest_trade_date="2026-09-24",
        now=datetime(2026, 9, 28, 21, 45, tzinfo=UTC),
        require_current_trade_date=False,
    )
    assert gate["status"] != "blocked"
