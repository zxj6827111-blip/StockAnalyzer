"""#15 auction report (09:26 竞价速报) acceptance tests.

Covers the missing direct assertions for the auction report output:
- ``_build_auction_report_content`` content shape (prefix trigger line, bidding
  data fields, action distribution, empty-data behavior).
- ``_job_auction_report`` push chain: main report notification plus the
  actionable-signal notification carrying the "09:26竞价" phase prefix.

NOTE on PRD fields: PRD mentions 标的/价格/成交量 fields; the current
implementation renders 标的(symbol)/结论(action)/评分(score)/目标仓位(target_position)
only - there is no price/volume field in the auction report content. Assertions
below follow the implementation.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _make_test_temp_root() -> Path:
    try:
        candidate = Path(tempfile.mkdtemp(prefix="stock_analyzer_tests_"))
        probe = candidate / ".write_probe"
        probe.mkdir(parents=True, exist_ok=True)
        return candidate
    except PermissionError:
        root = Path(__file__).resolve().parents[1]
        candidate = root / "manual_test_tmp" / f"stock_analyzer_tests_{uuid.uuid4().hex}"
        probe = candidate / ".write_probe"
        probe.mkdir(parents=True, exist_ok=True)
        return candidate


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week5.auto_notify = False
    config.week5.first_board_windows = ["23:58-23:59"]
    config.week6.auto_run = False
    config.week6.auto_notify = False
    config.market_warehouse.enabled = False
    config.market_warehouse.auto_run = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False
    temp_root = _make_test_temp_root()
    offline_root = temp_root / "missing_offline_package"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    config.tdx_sync.vipdoc_root = str(offline_root)
    config.command_channel.state_persist_path = str(temp_root / "runtime_state.json")
    config.command_channel.history_archive_dir = str(temp_root / "runtime_history")
    config.market_warehouse.db_path = str(temp_root / "market_warehouse.duckdb")
    config.market_warehouse.package_root = str(temp_root / "market_warehouse_package")
    config.market_warehouse.bootstrap_source_root = str(offline_root)
    config.training.artifact_path = str(temp_root / "test_model_auction.json")
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_auction.json")
    config.acceptance.export_enabled = False
    config.acceptance.auto_notify = False
    config.acceptance.notify_on_pass = False
    config.evolution.auto_run = False
    config.evolution.dry_run = True
    return config


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    return StockAnalyzerService(config=config)


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    raise AssertionError(f"Expected dict, got {type(value).__name__}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _sample_report(*, with_actionable: bool = True) -> dict[str, object]:
    report: dict[str, object] = {
        "trace_id": "auction-trace-1",
        "signals": [
            {
                "symbol": "600000",
                "action": "buy",
                "score": 82.5,
                "target_position": 0.1,
                "grade": "A",
            },
            {
                "symbol": "000001",
                "action": "watch",
                "score": 70.0,
                "target_position": 0.0,
                "grade": "B",
            },
            {
                "symbol": "300750",
                "action": "hold",
                "score": 45.0,
                "target_position": 0.0,
                "grade": "C",
            },
        ],
        "week6_execution": {"regime": "trend", "global_risk_score": 62.3},
        "execution_mode": "live",
        "portfolio_update": {"executions": []},
    }
    if with_actionable:
        report["actionable_signals"] = [
            {
                "symbol": "600000",
                "action": "buy",
                "score": 82.5,
                "target_position": 0.1,
                "grade": "A",
                "strategy": "trend",
                "reasons": ["auction_test_reason"],
            }
        ]
    return report


def test_auction_report_content_includes_prefix_bidding_data_and_distribution() -> None:
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    content = service._build_auction_report_content(  # noqa: SLF001
        report=_sample_report(),
        actionable_count=1,
    )

    # Trigger line carries the 09:26 auction report identity ("09:26竞价" prefix
    # appears as the phase prefix in the actionable push, asserted in the job test).
    assert "09:25 集合竞价数据已完成分析，系统在 09:26 输出开盘前最后一轮竞价速报。" in content
    # Bidding data summary: watchlist / signal counts / regime / risk / execution mode.
    assert "观察池=2；候选信号=3；可执行信号=1；市场状态=趋势；全局风险分=62.30；执行模式=live" in content
    # Action distribution line.
    assert "动作分布：买入 1 / 观察 1 / 持有或忽略 1" in content
    # Focus lines: buy first, then watch - symbol + action label + score + target position.
    assert "600000｜结论=买入｜评分=82.50｜目标仓位=10%" in content
    assert "000001｜结论=观察｜评分=70.00｜目标仓位=0%" in content
    # Action guidance when actionable_count > 0.
    assert "优先复核可执行买卖指令，再结合开盘后前 5 分钟量价变化决定是否处理。" in content
    # Detail section title.
    assert "竞价摘要：" in content


@pytest.mark.parametrize("raw_signals", [None, "garbage", {}, []])
def test_auction_report_content_handles_empty_or_malformed_signals(
    raw_signals: object,
) -> None:
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = []

    report: dict[str, object] = {
        "trace_id": "auction-trace-empty",
        "signals": raw_signals,
        "actionable_signals": [],
        "week6_execution": None,
        "execution_mode": "",
    }
    content = service._build_auction_report_content(  # noqa: SLF001
        report=report,
        actionable_count=0,
    )

    # Non-list signals must be treated as empty without raising.
    assert "候选信号=0；可执行信号=0" in content
    assert "观察池=0" in content
    # Missing regime / execution mode fall back to "-" / "unknown".
    assert "市场状态=-" in content
    assert "执行模式=unknown" in content
    assert "动作分布：买入 0 / 观察 0 / 持有或忽略 0" in content
    # No-signal fallback guidance.
    assert "当前没有直接买卖指令；开盘后请优先观察观察池内是否出现超预期异动。" in content


def test_auction_report_content_focus_branch_when_signals_but_no_actionable() -> None:
    """Focus symbols present but actionable_count == 0: name the top symbols for
    the open instead of the plain watchlist fallback."""
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    report: dict[str, object] = {
        "trace_id": "auction-trace-focus",
        "signals": [{"symbol": "600000", "action": "buy", "score": 80.0, "target_position": 0.1}],
        "actionable_signals": [],
        "week6_execution": {"regime": "trend", "global_risk_score": 55.0},
        "execution_mode": "live",
    }
    content = service._build_auction_report_content(  # noqa: SLF001
        report=report,
        actionable_count=0,
    )

    assert "候选信号=1；可执行信号=0" in content
    assert "600000｜结论=买入｜评分=80.00｜目标仓位=10%" in content
    assert "09:30 开盘后优先盯 600000 的开盘强弱、量能和承接变化。" in content


def test_auction_report_job_pushes_report_and_actionable_notifications_with_0926_prefix() -> None:
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    notifications: list[dict[str, object]] = []
    service.notify = lambda **kwargs: notifications.append(dict(kwargs))  # type: ignore[method-assign]
    _patch_attr(service, "run_pipeline", lambda **kwargs: _sample_report())

    result = _as_dict(service._job_auction_report())

    assert result["trace_id"] == "auction-trace-1"
    assert _as_int(result["signals"]) == 3
    assert _as_int(result["actionable"]) == 1

    # Exactly two notifications: main report digest + actionable buy signal.
    assert len(notifications) == 2

    report_notifications = [
        item
        for item in notifications
        if "09:25 集合竞价数据已完成分析，系统在 09:26 输出开盘前最后一轮竞价速报。" in str(
            item.get("content", "")
        )
    ]
    assert len(report_notifications) == 1
    main = report_notifications[0]
    assert main["title"] == runtime_service_module._push_title(  # noqa: SLF001
        priority="P2",
        category="morning",
        summary="auction report",
    )
    assert "600000｜结论=买入｜评分=82.50｜目标仓位=10%" in str(main["content"])

    # The actionable buy push must carry the "09:26竞价" phase prefix.
    actionable_notifications = [
        item
        for item in notifications
        if "扫描阶段：09:26竞价" in str(item.get("content", ""))
    ]
    assert len(actionable_notifications) == 1
    actionable = actionable_notifications[0]
    assert "600000" in str(actionable["content"])
    assert "目标仓位：10.00%" in str(actionable["content"])
    assert "评分等级：82.50 分 / A" in str(actionable["content"])
    assert actionable["level"] == "info"
