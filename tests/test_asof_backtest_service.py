"""AsofBacktestService 落盘路径回归测试。

背景：``__init__`` 的 project_root 曾错解析为 ``parents[3]``（``src/``），
容器内落盘进了 ``/app/src/artifacts`` 容器层而非 ``/app/artifacts`` 数据卷，
容器重建即丢 latest.json/history.jsonl 与 week5_task_* 目录——与
``acceptance_service``/``week7_sim_broker_service`` 的 ``parents[4]`` 取法
不一致，这里固定住正确层级。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from stock_analyzer.runtime.services.asof_backtest_service import AsofBacktestService


def test_output_dir_resolves_to_repo_root_not_src() -> None:
    # __init__ 只读 config.asof_backtest.output_dir，鸭子类型 stub 即可。
    fake_config = SimpleNamespace(
        asof_backtest=SimpleNamespace(output_dir="artifacts/backtest/asof_scan")
    )
    service = AsofBacktestService(SimpleNamespace(_config=fake_config))
    repo_root = Path(__file__).resolve().parents[1]
    assert service._output_dir == (repo_root / "artifacts/backtest/asof_scan").resolve()
    # 防再次落到 src/ 下（parents[3] 的旧错误行为）。
    assert "src/artifacts" not in str(service._output_dir)
