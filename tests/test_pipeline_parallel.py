"""final pipeline 并行化（Phase 2 受控框架）测试。

覆盖范围（Week5 扫描漏斗整改计划 Phase 2）：
- Week5 配置 final_pipeline_transform_max_workers 默认 4；通用 pipeline 默认串行；
- 4 worker 与串行逐字段等价（score/grade/action/target_position/
  probabilities/reasons/decision_trace）；
- 单 worker transform 异常 fallback 为 feature_empty hold 信号，
  不影响其他股票；
- 顺序收敛：signals 顺序 == 原始 symbol 顺序；
- DuckDB/学习快照无并发写：worker 仅做 transform，写库全在主线程
  （架构保证，测试验证并行路径完整跑通且异常隔离）。
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, cast

import pandas as pd

import stock_analyzer.pipeline as pipeline_module
from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.pipeline import (
    AnalyzerPipeline,
    PipelineSignal,
    SymbolTransformInputs,
)
from tests.test_service_week5 import (
    _load_test_config,
    _new_service,
    _patch_attr,
)


def _assert_signal_equal(
    actual: PipelineSignal,
    expected: PipelineSignal,
    *,
    path: str = "",
) -> None:
    """逐字段断言（排除时间相关字段：learning_protocol.snapshot_id）。"""
    assert actual.symbol == expected.symbol, f"{path}.symbol"
    assert actual.strategy == expected.strategy, f"{path}.strategy"
    assert actual.score == expected.score, f"{path}.score"
    assert actual.grade == expected.grade, f"{path}.grade"
    assert actual.action == expected.action, f"{path}.action"
    assert actual.target_position == expected.target_position, f"{path}.target_position"
    assert actual.probabilities == expected.probabilities, f"{path}.probabilities"
    assert actual.reasons == expected.reasons, f"{path}.reasons"
    actual_trace = _without_learning_protocol(actual.decision_trace)
    expected_trace = _without_learning_protocol(expected.decision_trace)
    assert actual_trace == expected_trace, f"{path}.decision_trace"


def _without_learning_protocol(trace: Mapping[str, object]) -> dict[str, object]:
    clean = {key: value for key, value in trace.items() if key != "learning_protocol"}
    return cast(dict[str, object], clean)


def _build_service(config: StockAnalyzerConfig) -> tuple[AnalyzerPipeline, Any]:
    service = _new_service(config)
    return cast(AnalyzerPipeline, service._pipeline), service  # noqa: SLF001


def test_parallel_week5_default_workers_is_four(config_override: None = None) -> None:
    """Week5 默认 4 worker；通用 run_once 未显式传参时仍保持串行。"""
    config = _load_test_config()
    assert config.week5.final_pipeline_transform_max_workers == 4


def test_generic_pipeline_default_remains_serial() -> None:
    """通用 pipeline 未显式传 worker 时不启用 ProcessPool。"""
    config = _load_test_config()
    pipeline, _ = _build_service(config)

    pipeline.run_once(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
    )

    metrics = pipeline._last_parallel_transform  # noqa: SLF001
    assert metrics["enabled"] is False
    assert metrics["configured_workers"] == 1
    assert metrics["submitted_count"] == 0


def test_parallel_serial_vs_four_workers_field_equivalent() -> None:
    """串行（max_workers=1）与 4 worker 逐字段等价。"""
    # 禁用 dynamic cross-review：避免两个 service 实例共享同一 DuckDB 时
    # dynamic_history 计数随运行次序漂移（与 transform 并行等价无关）。
    config_serial = _load_test_config()
    config_serial.models.cross_review.dynamic_enabled = False
    pipeline_serial, _ = _build_service(config_serial)

    config_parallel = _load_test_config()
    config_parallel.models.cross_review.dynamic_enabled = False
    config_parallel.week5.final_pipeline_transform_max_workers = 4
    pipeline_parallel, _ = _build_service(config_parallel)

    symbols = ["600000", "000001", "600519", "000858"]
    serial_signals = pipeline_serial.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
    ).signals
    parallel_signals = pipeline_parallel.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
    ).signals

    assert len(serial_signals) == len(parallel_signals) == len(symbols)
    for index, (serial_signal, parallel_signal) in enumerate(
        zip(serial_signals, parallel_signals, strict=True)
    ):
        _assert_signal_equal(parallel_signal, serial_signal, path=f"symbol[{index}]")

    serial_metrics = pipeline_serial._last_parallel_transform  # noqa: SLF001
    parallel_metrics = pipeline_parallel._last_parallel_transform  # noqa: SLF001
    assert serial_metrics["enabled"] is False
    assert serial_metrics["configured_workers"] == 1
    assert parallel_metrics["enabled"] is True
    assert parallel_metrics["configured_workers"] == 4
    assert parallel_metrics["submitted_count"] == len(symbols)
    assert parallel_metrics["worker_count"] >= 2
    assert len(cast(list[int], parallel_metrics["worker_pids"])) >= 2
    assert parallel_metrics["max_concurrency"] >= 2
    assert parallel_metrics["wall_ms"] >= 0
    assert parallel_metrics["worker_transform_ms"] >= 0
    assert parallel_metrics["fallback_count"] == 0
    assert parallel_metrics["pool_fallback"] is False


def test_parallel_order_converges_to_input_order() -> None:
    """顺序收敛：signals 顺序 == 原始 symbol 顺序（主线程按序消费）。"""
    config = _load_test_config()
    config.week5.final_pipeline_transform_max_workers = 4
    pipeline, _ = _build_service(config)
    symbols = ["600519", "000001", "601318", "000858", "600000", "300750"]

    signals = pipeline.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
    ).signals

    assert [signal.symbol for signal in signals] == symbols


def test_parallel_single_worker_transform_failure_falls_back() -> None:
    """单 worker transform 异常：fallback 为 feature_empty hold 信号，
    其他股票正常产出（异常隔离）。"""
    config = _load_test_config()
    config.week5.final_pipeline_transform_max_workers = 4
    service = _new_service(config)
    pipeline = cast(AnalyzerPipeline, service._pipeline)
    original_prepare = pipeline._prepare_symbol_inputs  # noqa: SLF001

    def _broken_prepare(
        symbol: str, strategy: str, current_equity: float
    ) -> tuple[PipelineSignal | None, SymbolTransformInputs | None]:
        fail_signal, inputs = original_prepare(symbol, strategy, current_equity)
        if inputs is not None and symbol == "600519":
            # 删除必需列：worker 内 FeatureEngineer.transform 抛 ValueError。
            broken = dict(inputs)
            broken["analysis_bars"] = cast(
                pd.DataFrame, inputs["analysis_bars"]
            ).drop(columns=["close"])
            return fail_signal, broken
        return fail_signal, inputs

    _patch_attr(pipeline, "_prepare_symbol_inputs", _broken_prepare)

    symbols = ["600519", "000001", "601318"]
    signals = pipeline.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
    ).signals

    assert len(signals) == 3
    assert [signal.symbol for signal in signals] == symbols
    # 异常股票 fallback 为 feature_empty hold。
    failed = signals[0]
    assert failed.symbol == "600519"
    assert failed.action == "hold"
    assert failed.score == 0.0
    assert "feature_empty" in failed.reasons
    # 其他股票正常产出（非 hold 默认值、reasons 非 feature_empty）。
    for signal in signals[1:]:
        assert "feature_empty" not in signal.reasons


def test_parallel_progress_callback_order_and_pairing() -> None:
    """并行路径进度回调：每只股票 start/done 各一次，按 symbol 顺序。"""
    config = _load_test_config()
    config.week5.final_pipeline_transform_max_workers = 4
    pipeline, _ = _build_service(config)
    symbols = ["600519", "000001", "601318"]
    events: list[tuple[str, int, int, bool]] = []

    pipeline.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
        on_symbol_progress=lambda symbol, index, total, started: events.append(
            (symbol, index, total, started)
        ),
    )

    # 并行路径语义：prepare 阶段逐股发 start（全部先发），finalize 阶段
    # 按原始顺序发 done——每只股票开始/完成各更新一次（事件驱动心跳）。
    expected = [
        (symbols[0], 0, 3, True),
        (symbols[1], 1, 3, True),
        (symbols[2], 2, 3, True),
        (symbols[0], 0, 3, False),
        (symbols[1], 1, 3, False),
        (symbols[2], 2, 3, False),
    ]
    assert events == expected


def test_parallel_three_hundred_candidates_are_all_submitted(
    monkeypatch: Any,
) -> None:
    """300 候选在 worker>1 时全部进入并行任务队列。"""
    config = _load_test_config()
    pipeline, _ = _build_service(config)

    def _prepare(
        symbol: str, strategy: str, current_equity: float
    ) -> tuple[PipelineSignal | None, SymbolTransformInputs | None]:
        empty = pd.DataFrame()
        return None, {
            "symbol": symbol,
            "strategy": strategy,
            "current_equity": current_equity,
            "decision_time": datetime(2026, 8, 13),
            "bars": empty,
            "bars_time_gate": {},
            "analysis_bars": empty,
            "intraday_1m": empty,
            "intraday_5m": empty,
            "market_index": None,
            "feature_prepare_ms": 0.0,
            "provider_status": {},
        }

    def _worker(inputs: SymbolTransformInputs) -> dict[str, object]:
        _ = inputs
        return {
            "features": pd.DataFrame(),
            "transform_ms": 0.01,
            "worker_pid": 4242,
            "started_ns": 1,
            "finished_ns": 2,
        }

    def _finalize(
        symbol: str,
        strategy: str,
        current_equity: float,
        inputs: SymbolTransformInputs,
        features: pd.DataFrame,
    ) -> PipelineSignal:
        _ = current_equity, inputs, features
        return PipelineSignal(
            symbol=symbol,
            strategy=strategy,
            score=0.0,
            grade="C",
            action="hold",
            target_position=0.0,
            probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
            reasons=["test"],
        )

    monkeypatch.setattr(pipeline_module, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(pipeline_module, "_transform_symbol_features_worker", _worker)
    _patch_attr(pipeline, "_prepare_symbol_inputs", _prepare)
    _patch_attr(pipeline, "_finalize_symbol_signal", _finalize)
    symbols = [f"T{index:03d}" for index in range(300)]

    signals = pipeline.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
    ).signals

    metrics = pipeline._last_parallel_transform  # noqa: SLF001
    assert [signal.symbol for signal in signals] == symbols
    assert metrics["enabled"] is True
    assert metrics["configured_workers"] == 4
    assert metrics["submitted_count"] == 300
    assert metrics["fallback_count"] == 0


def test_parallel_pool_start_failure_falls_back_to_serial(
    monkeypatch: Any,
) -> None:
    """进程池整体不可用时，全量回退主线程 transform，结果仍完整有序。"""

    class _BrokenProcessPool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            raise OSError("process-pool-unavailable")

    config = _load_test_config()
    config.models.cross_review.dynamic_enabled = False
    pipeline, _ = _build_service(config)
    monkeypatch.setattr(pipeline_module, "ProcessPoolExecutor", _BrokenProcessPool)
    symbols = ["600519", "000001", "601318"]

    signals = pipeline.run_once(
        symbols=symbols,
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
    ).signals

    assert [signal.symbol for signal in signals] == symbols
    assert all("feature_empty" not in signal.reasons for signal in signals)
    metrics = pipeline._last_parallel_transform  # noqa: SLF001
    assert metrics["enabled"] is True
    assert metrics["configured_workers"] == 4
    assert metrics["submitted_count"] == 0
    assert metrics["worker_count"] == 0
    assert metrics["max_concurrency"] == 0
    assert metrics["fallback_count"] == len(symbols)
    assert metrics["pool_fallback"] is True
    assert "process-pool-unavailable" in str(metrics["pool_error"])


def test_parallel_provider_status_is_frozen_per_symbol() -> None:
    """后续 provider 失败不得污染较早股票的风险/决策状态。"""
    config = _load_test_config()
    config.models.cross_review.dynamic_enabled = False
    pipeline, _ = _build_service(config)
    original_prepare = pipeline._prepare_symbol_inputs  # noqa: SLF001

    healthy = {
        "degraded_mode": False,
        "hard_degraded_mode": False,
        "soft_degraded_mode": False,
        "degrade_reason": "",
        "hard_degraded_reason": "",
        "soft_degraded_reason": "",
    }
    degraded = {
        "degraded_mode": True,
        "hard_degraded_mode": True,
        "soft_degraded_mode": False,
        "degrade_reason": "later_fetch_failed",
        "hard_degraded_reason": "later_fetch_failed",
        "soft_degraded_reason": "",
    }

    def _prepare_with_status(
        symbol: str, strategy: str, current_equity: float
    ) -> tuple[PipelineSignal | None, SymbolTransformInputs | None]:
        fail_signal, inputs = original_prepare(symbol, strategy, current_equity)
        if inputs is not None:
            inputs["provider_status"] = dict(
                healthy if symbol == "600519" else degraded
            )
        return fail_signal, inputs

    _patch_attr(pipeline, "_prepare_symbol_inputs", _prepare_with_status)
    # 若 finalize 重新读取全局状态，两只都会被错误标记为 degraded。
    _patch_attr(pipeline, "provider_status", lambda: dict(degraded))

    signals = pipeline.run_once(
        symbols=["600519", "000001"],
        strategy="trend",
        current_equity=1.0,
        transform_max_workers=4,
    ).signals

    first_provider = cast(
        Mapping[str, object], signals[0].decision_trace["provider"]
    )
    second_provider = cast(
        Mapping[str, object], signals[1].decision_trace["provider"]
    )
    assert first_provider["hard_degraded_mode"] is False
    assert first_provider["hard_degraded_reason"] == ""
    assert second_provider["hard_degraded_mode"] is True
    assert second_provider["hard_degraded_reason"] == "later_fetch_failed"
