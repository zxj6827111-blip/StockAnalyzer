"""Week5 扫描漏斗整改（Phase 1）测试。

覆盖范围（Week5 扫描漏斗整改计划-修订版）：
- 三类选择策略：snapshot_funnel / intentional_full_deep / direct_non_universe；
- forced profile 改为执行 snapshot light/deep 漏斗，不再隐式全量重型扫描；
- snapshot_funnel deep 空结果 fail-closed：绝不回退至 raw 候选，
  仅允许 pinned 增量（light 为空 / 快照无匹配行 / deep 筛选为空 三态归因）；
- Friday/weekend intentional_full_deep 保留 raw -> cap 语义，
  不因"deep 未运行"被误判为 fail-closed；
- pinned 超出 deep target 时保留优先级，仍受 monster_scan_max_symbols 约束；
- funnel 结构化计数 + monster_scan_controls.selection_source；
- week5.scan_progress_path 原子进度文件：阶段推进、单股心跳、完成/失败终态；
- final pipeline 聚合计时（fetch/feature/inference）+ 最慢 5 只股票。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.feature.snapshot import build_feature_snapshot
from stock_analyzer.pipeline import AnalyzerPipeline
from stock_analyzer.runtime.service import StockAnalyzerService
from tests.test_service_week5 import (
    _as_mapping,
    _as_text_list,
    _enable_universe_quality_selector,
    _load_test_config,
    _new_service,
    _patch_attr,
)
from tests.test_week5_snapshot_integration import (
    _capture_pipeline,
    _patch_scan_surroundings,
    _quality_selection_fake,
)


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _prepare_snapshot_service(
    tmp_path: Path,
    *,
    candidates: list[str],
    deep_target: int | None = None,
) -> StockAnalyzerService:
    """构建带真实 current 快照的 service（候选集=快照覆盖集）。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    if deep_target is not None:
        config.week5.deep_candidate_target = deep_target
    service = _new_service(config)
    service.state.watchlist = []
    universe_symbols = [f"600{i:03d}" for i in range(400)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(candidates, len(universe_symbols)),
    )
    _patch_scan_surroundings(service, {})
    build_report = build_feature_snapshot(
        config,
        service._provider,  # noqa: SLF001
        symbols=candidates,
        lookback_days=250,
        force=True,
        scope="universe_quality",
    )
    assert build_report["ok"] is True
    return service


def _run_scan(
    service: StockAnalyzerService,
    *,
    timestamp: datetime | None = None,
    pinned_symbols: list[str] | None = None,
    prefilter_enabled_override: bool | None = None,
    scan_profile: str = "",
) -> Mapping[str, object]:
    return _as_mapping(
        service.run_week5_scan(
            timestamp=timestamp or datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=prefilter_enabled_override,
            pinned_symbols=pinned_symbols,
            scan_profile=scan_profile,
        )
    )


def test_week5_scan_normal_snapshot_funnel_input_within_deep_target(
    tmp_path: Path,
) -> None:
    """普通 snapshot 路径：300 候选经 light/deep，pipeline 输入=deep 入选项
    （不超过 deep target），排序/字段与串行基线一致，不回退 raw。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=3)
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _run_scan(service)

    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "snapshot_funnel"
    assert funnel["deep_stage_ran"] is True
    assert funnel["deep_symbols_empty"] is False
    assert funnel["deep_empty_reason"] == ""
    assert funnel["deep_selected_count"] == 3
    assert funnel["pinned_added_count"] == 0
    assert funnel["pipeline_input_count"] == 3
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "snapshot_deep"
    assert controls["input_count"] == 3
    assert controls["effective_input_cap"] == 3

    # pipeline 输入 = deep 入选项（≤ deep target），顺序与 deep selected 完全一致。
    assert len(pipeline_symbols) == 1
    deep_selected = _as_mapping_list(_as_mapping(report["prefilter"])["deep_stage"]["selected"])
    assert [item["symbol"] for item in deep_selected] == pipeline_symbols[0]
    assert len(pipeline_symbols[0]) == 3
    assert set(pipeline_symbols[0]) <= set(candidates)

    # deep 报告诊断字段：light shortlist 数 / 快照匹配行数 / 模型预测降级状态。
    deep_stage = _as_mapping(_as_mapping(report["prefilter"])["deep_stage"])
    assert deep_stage["mode"] == "snapshot_deep"
    assert deep_stage["light_shortlist_count"] > 0
    assert deep_stage["snapshot_match_rows"] > 0
    assert isinstance(deep_stage["model_prediction_degraded"], bool)

    # first-board/anomaly 阶段计时（P2 修复）。
    stages = _as_mapping(report["scan_stages"])
    assert _as_mapping(stages["first_board_anomaly"])["duration_ms"] > 0


def test_week5_offhours_forced_profile_runs_snapshot_funnel(tmp_path: Path) -> None:
    """forced profile：保留名称与触发原因，但实际执行 snapshot light/deep；
    pipeline 输入为 deep selected + pinned，不再出现 raw 300/120 fallback。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 5
    service = _new_service(config)
    service.state.watchlist = []
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": [f"600{i:03d}" for i in range(10)],
            "errors": [],
        },
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(candidates, 10),
    )
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 11, 20, 30),
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    # forced profile 保留名称与触发原因。
    assert report["scan_profile"] == "offhours_forced_full_deep"
    profile = _as_mapping(report["offhours_refresh_profile"])
    assert "watchlist_below_5" in [str(item) for item in cast(list[object], profile["reasons"])]
    # 但执行 snapshot light/deep 漏斗（prefilter 打开），而非隐式全量重型扫描。
    assert profile["prefilter_enabled"] is True
    assert profile["funnel_policy"] == "snapshot_funnel"
    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "snapshot_funnel"
    assert funnel["deep_stage_ran"] is True
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "snapshot_deep"
    # pipeline 输入 = deep 入选项（≤ deep target），raw 300/120 未进入。
    assert len(pipeline_symbols) == 1
    assert set(pipeline_symbols[0]) <= set(candidates)
    assert len(pipeline_symbols[0]) == min(
        len(candidates), config.week5.deep_candidate_target
    )


@pytest.mark.parametrize(
    ("deep_override", "expected_reason"),
    [
        # light 为空：light shortlist 为空，deep 无入选项。
        ("light_empty", "light_shortlist_empty"),
        # 快照无匹配行：light 有 shortlist，但快照帧无对应行。
        ("no_rows", "no_snapshot_matching_rows"),
        # deep 筛选为空：light/快照行均正常，cross-review/评分后无入选项。
        ("deep_empty", "deep_selected_empty"),
    ],
)
def test_week5_scan_deep_empty_fail_closed_pinned_only(
    tmp_path: Path,
    deep_override: str,
    expected_reason: str,
) -> None:
    """deep 空回退三态：均只运行 pinned，未传入任何 raw 候选，
    并记录准确的 deep_empty_reason。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates)

    if deep_override == "light_empty":
        # light 为空：打桩 light 返回空 shortlist，deep 走真实 early-return。
        _patch_attr(
            service,
            "_light_stage_from_snapshot",
            lambda **kwargs: {
                "enabled": True,
                "applied": True,
                "mode": "snapshot_light",
                "universe_count": 6,
                "eligible_count": 0,
                "shortlisted_count": 0,
                "shortlisted": [],
            },
        )
    elif deep_override == "no_rows":
        _patch_attr(
            service,
            "_deep_stage_from_snapshot",
            lambda **kwargs: {
                "applied": True,
                "mode": "snapshot_deep",
                "input_count": 0,
                "selected_count": 0,
                "selected": [],
                "cross_review_passed": 0,
                "light_shortlist_count": 5,
                "snapshot_match_rows": 0,
                "model_prediction_degraded": False,
            },
        )
    else:
        _patch_attr(
            service,
            "_deep_stage_from_snapshot",
            lambda **kwargs: {
                "applied": True,
                "mode": "snapshot_deep",
                "input_count": 5,
                "selected_count": 0,
                "selected": [],
                "cross_review_passed": 0,
                "light_shortlist_count": 5,
                "snapshot_match_rows": 5,
                "model_prediction_degraded": False,
            },
        )

    pinned = ["002415", "601998"]
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _run_scan(service, pinned_symbols=pinned)

    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "snapshot_funnel"
    assert funnel["deep_stage_ran"] is True
    assert funnel["deep_symbols_empty"] is True
    assert funnel["deep_empty_reason"] == expected_reason
    assert funnel["deep_selected_count"] == 0
    assert funnel["pinned_added_count"] == 2
    assert funnel["pipeline_input_count"] == 2
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "deep_empty_pinned_only"
    # 只运行 pinned：raw 候选（质量选择 300 只）一个都未进入 pipeline。
    assert len(pipeline_symbols) == 1
    assert pipeline_symbols[0] == pinned
    assert not set(pipeline_symbols[0]) & set(candidates)


def test_week5_scan_deep_empty_fail_closed_no_pinned_empty_report(
    tmp_path: Path,
) -> None:
    """deep 空回退且无 pinned：fail-closed 空报告，明确记录归因，
    不返回 raw 候选。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates)
    _patch_attr(
        service,
        "_deep_stage_from_snapshot",
        lambda **kwargs: {
            "applied": True,
            "mode": "snapshot_deep",
            "input_count": 5,
            "selected_count": 0,
            "selected": [],
            "cross_review_passed": 0,
            "light_shortlist_count": 5,
            "snapshot_match_rows": 5,
            "model_prediction_degraded": False,
        },
    )
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _run_scan(service)

    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "snapshot_funnel"
    assert funnel["deep_symbols_empty"] is True
    assert funnel["deep_empty_reason"] == "deep_selected_empty"
    assert funnel["pipeline_input_count"] == 0
    empty_signal = _as_mapping(report["empty_signal"])
    assert "snapshot_deep_empty_fail_closed" in [
        str(item) for item in cast(list[object], empty_signal["reasons"])
    ]
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "deep_empty_pinned_only"
    assert controls["input_count"] == 0
    # 未传入任何 raw 候选。
    assert pipeline_symbols == []


def test_week5_scan_friday_intentional_full_deep_keeps_raw_candidates(
    tmp_path: Path,
) -> None:
    """Friday intentional_full_deep：标记明确，绕过漏斗，raw 候选 -> cap；
    deep 未运行，不因空结果被误判为 fail-closed。"""
    candidates = [f"600{i:03d}" for i in range(6)]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=3)
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _run_scan(
        service,
        timestamp=datetime(2026, 3, 13, 20, 30),
        prefilter_enabled_override=False,
        scan_profile="offhours_friday_full_deep",
    )

    assert report["scan_profile"] == "offhours_friday_full_deep"
    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "intentional_full_deep"
    # 有意绕过漏斗：deep 未运行，deep_symbols_empty 不得为 True（区别于 fail-closed）。
    assert funnel["deep_stage_ran"] is False
    assert funnel["deep_symbols_empty"] is False
    assert funnel["deep_empty_reason"] == ""
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "intentional_full_deep"
    # 维持 full-deep 输入：raw 6 只全部进入 pipeline（deep target=3 不生效）。
    assert len(pipeline_symbols) == 1
    assert pipeline_symbols[0] == candidates
    assert funnel["pipeline_input_count"] == 6


def test_week5_scan_pinned_priority_beyond_deep_target(tmp_path: Path) -> None:
    """pinned 超出 deep target：pinned 保留优先级；monster_scan_max_symbols
    仍是硬上限，effective_input_cap = min(monster cap, deep+pinned)。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=3)
    pinned = ["002415", "601998", "000725", "600036", "002594"]
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _run_scan(service, pinned_symbols=pinned)

    funnel = _as_mapping(report["funnel"])
    assert funnel["deep_selected_count"] == 3
    assert funnel["pinned_added_count"] == 5
    assert funnel["pipeline_input_count"] == 8
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["effective_input_cap"] == 8
    assert len(pipeline_symbols) == 1
    assert len(pipeline_symbols[0]) == 8
    # pinned 全部保留；顺序 = deep 在前，pinned 追加在后。
    assert set(pinned) <= set(pipeline_symbols[0])

    # 硬上限：monster_scan_max_symbols=5 时 pinned 优先保留。
    service._config.week5.monster_scan_max_symbols = 5  # noqa: SLF001
    pipeline_symbols.clear()
    report = _run_scan(service, pinned_symbols=pinned)
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["cap_applied"] is True
    assert controls["effective_input_cap"] == 5
    assert controls["input_count"] == 8
    capped_input = pipeline_symbols[0]
    assert len(capped_input) == 5
    assert set(capped_input) <= set(pinned)


def test_week5_scan_progress_file_phases_heartbeat_and_completion(
    tmp_path: Path,
) -> None:
    """进度文件：阶段推进、单股心跳、completed 终态，原子 JSON 可解析。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    progress_path = tmp_path / "week5_scan_progress.json"
    config.week5.scan_progress_path = str(progress_path)
    service = _new_service(config)
    service.state.watchlist = []
    universe_symbols = [f"600{i:03d}" for i in range(400)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(candidates, len(universe_symbols)),
    )
    _patch_scan_surroundings(service, {})
    heartbeat_snapshots: list[dict[str, object]] = []
    pipeline_input_order: list[str] = []
    pipeline_worker_counts: list[int] = []
    phase_observations: list[tuple[str, str]] = []

    def _observe_phase(hook: str, wrapped: object, *args: object, **kwargs: object) -> object:
        snapshot = cast(
            dict[str, object],
            json.loads(progress_path.read_text(encoding="utf-8")),
        )
        phase_observations.append((hook, snapshot["phase"]))
        return cast(object, wrapped(*args, **kwargs))

    original_select = service._select_universe_quality_candidates  # noqa: SLF001
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        lambda **kwargs: _observe_phase("quality", original_select, **kwargs),
    )
    original_ensure = service.ensure_week5_feature_snapshot
    _patch_attr(
        service,
        "ensure_week5_feature_snapshot",
        lambda **kwargs: _observe_phase("snapshot", original_ensure, **kwargs),
    )
    original_light = service._light_stage_from_snapshot  # noqa: SLF001
    _patch_attr(
        service,
        "_light_stage_from_snapshot",
        lambda **kwargs: _observe_phase("light", original_light, **kwargs),
    )
    original_deep = service._deep_stage_from_snapshot  # noqa: SLF001
    _patch_attr(
        service,
        "_deep_stage_from_snapshot",
        lambda **kwargs: _observe_phase("deep", original_deep, **kwargs),
    )

    def _fake_pipeline(**kwargs: object) -> dict[str, object]:
        symbols = _as_text_list(cast(list, kwargs.get("symbols", [])))
        pipeline_input_order.extend(symbols)
        pipeline_worker_counts.append(int(kwargs.get("transform_max_workers", 0)))
        callback = kwargs.get("on_symbol_progress")
        for index, symbol in enumerate(symbols):
            if callable(callback):
                callback(symbol, index, len(symbols), True)
                heartbeat_snapshots.append(
                    cast(dict[str, object], json.loads(progress_path.read_text(encoding="utf-8")))
                )
                callback(symbol, index, len(symbols), False)
        return {
            "trace_id": "progress-test-trace",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        }

    _patch_attr(service, "run_pipeline", _fake_pipeline)

    service.run_week5_scan(
        timestamp=datetime(2026, 3, 16, 20, 30),
        notify_enabled=False,
        force_universe_scan=True,
        prefilter_enabled_override=True,
    )

    # completed 终态：字段齐全、原子 JSON 可解析。
    final = cast(dict[str, object], json.loads(progress_path.read_text(encoding="utf-8")))
    assert final["status"] == "completed"
    assert final["scan_profile"] == "default"
    assert final["funnel_policy"] == "snapshot_funnel"
    assert final["trace_id"] == "progress-test-trace"
    for key in (
        "trace_id",
        "status",
        "phase",
        "started_at",
        "updated_at",
        "elapsed_ms",
        "completed",
        "total",
        "current_symbol",
        "scan_profile",
        "funnel_policy",
    ):
        assert key in final

    # 单股心跳：final pipeline 阶段逐股记录当前符号与进度（按 pipeline 输入顺序）。
    assert len(heartbeat_snapshots) == len(pipeline_input_order)
    assert len(pipeline_input_order) == len(candidates)
    assert pipeline_worker_counts == [4]
    for index, snapshot in enumerate(heartbeat_snapshots):
        assert snapshot["status"] == "running"
        assert snapshot["phase"] == "final_pipeline"
        assert snapshot["current_symbol"] == pipeline_input_order[index]
        assert snapshot["completed"] == index
        assert snapshot["total"] == len(candidates)
        assert snapshot["elapsed_ms"] >= 0

    # 阶段推进：quality -> snapshot -> light -> deep -> (final_pipeline 心跳)。
    assert phase_observations == [
        ("quality", "quality"),
        ("snapshot", "snapshot"),
        ("light", "light"),
        ("deep", "deep"),
    ]


def test_week5_scan_progress_file_failed_terminal_state(tmp_path: Path) -> None:
    """异常路径：进度文件写 failed + 受控错误摘要，异常照常传播。"""
    candidates = ["600519", "000001"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates)
    progress_path = tmp_path / "week5_scan_progress_fail.json"
    service._config.week5.scan_progress_path = str(progress_path)  # noqa: SLF001

    def _boom(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("quality-selector-boom")

    _patch_attr(service, "_select_universe_quality_candidates", _boom)

    with pytest.raises(RuntimeError, match="quality-selector-boom"):
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )

    payload = cast(dict[str, object], json.loads(progress_path.read_text(encoding="utf-8")))
    assert payload["status"] == "failed"
    assert "RuntimeError" in str(payload.get("error_summary", ""))
    assert "quality-selector-boom" in str(payload.get("error_summary", ""))


def test_week5_scan_final_pipeline_timing_report(tmp_path: Path) -> None:
    """final pipeline 报告：fetch/feature/inference 聚合计时 + 最慢 5 只。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=5)
    pipeline_symbols: list[list[str]] = []
    symbols_under_test: list[str] = []

    def _fake_pipeline(**kwargs: object) -> dict[str, object]:
        symbols = _as_text_list(cast(list, kwargs.get("symbols", [])))
        pipeline_symbols.append(symbols)
        symbols_under_test.extend(symbols)
        return {
            "trace_id": "timing-test-trace",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
            "runtime": {
                "duration_ms": 3000,
                "pipeline_stage_ms": {
                    "fetch_bars_ms": 120,
                    "feature_engine_ms": 2500,
                    "inference_ms": 180,
                },
                "pipeline_symbol_ms": [
                    {"symbol": symbol, "duration_ms": 1000 - index * 150}
                    for index, symbol in enumerate(symbols)
                ],
                "pipeline_parallel_transform": {
                    "enabled": True,
                    "configured_workers": 4,
                    "submitted_count": len(symbols),
                    "worker_count": 4,
                    "worker_pids": [101, 102, 103, 104],
                    "max_concurrency": 4,
                    "wall_ms": 2000,
                    "worker_transform_ms": 7000,
                    "fallback_count": 0,
                    "pool_fallback": False,
                    "pool_error": "",
                },
            },
        }

    _patch_attr(service, "run_pipeline", _fake_pipeline)
    report = _run_scan(service)

    final_pipeline = _as_mapping(_as_mapping(report["scan_stages"])["final_pipeline"])
    assert final_pipeline["duration_ms"] == 3000
    assert final_pipeline["symbols"] == len(symbols_under_test)
    assert final_pipeline["fetch_bars_ms"] == 120
    assert final_pipeline["feature_engine_ms"] == 2500
    assert final_pipeline["inference_ms"] == 180
    parallel_transform = _as_mapping(final_pipeline["parallel_transform"])
    assert parallel_transform["configured_workers"] == 4
    assert parallel_transform["worker_count"] == 4
    assert parallel_transform["max_concurrency"] == 4
    slowest = _as_mapping_list(final_pipeline["slowest_symbols"])
    assert len(slowest) == 5
    durations = [int(item["duration_ms"]) for item in slowest]
    assert durations == sorted(durations, reverse=True)
    # 最慢 5 只来自实际 pipeline 输入。
    assert {str(item["symbol"]) for item in slowest} <= set(symbols_under_test)


def test_week5_scan_manual_symbols_direct_non_universe() -> None:
    """非全市场直接扫描（manual symbols）：policy=direct_non_universe，
    selection_source=direct_scan，不执行 light/deep，输入=手动列表。"""
    config = _load_test_config()
    config.week5.feature_snapshot_root = str(
        Path(config.week5.feature_snapshot_root).parent / "direct_scan_never_built"
    )
    service = _new_service(config)
    service.state.watchlist = []
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    manual = ["600000", "000001"]
    report = _as_mapping(
        service.run_week5_scan(
            symbols=manual,
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
        )
    )

    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "direct_non_universe"
    assert funnel["mode"] == "direct"
    assert funnel["deep_stage_ran"] is False
    assert funnel["deep_symbols_empty"] is False
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "direct_scan"
    assert len(pipeline_symbols) == 1
    assert pipeline_symbols[0] == manual
    assert funnel["pipeline_input_count"] == 2


def test_week5_scan_snapshot_unavailable_not_marked_deep_empty(
    tmp_path: Path,
) -> None:
    """snapshot_funnel 意图下快照不可用（ensure 失败、非 scheduler 走直接
    prefilter）：deep 未执行，selection_source 必须为 direct_scan，
    不得误标为 deep_empty_pinned_only（避免与 deep 空回退混淆）。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = False
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    universe_symbols = [f"600{i:03d}" for i in range(40)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(candidates, len(universe_symbols)),
    )
    _patch_attr(
        service,
        "ensure_week5_feature_snapshot",
        lambda **kwargs: {"ok": False, "skipped": False, "build": {"ok": False}},
    )
    _patch_attr(
        service,
        "_prefilter_week5_universe_symbols",
        lambda **kwargs: {
            "enabled": True,
            "applied": True,
            "shortlisted_count": len(candidates),
            "symbols": list(candidates),
            "shortlisted": [{"symbol": symbol, "baseline_score": 70.0} for symbol in candidates],
        },
    )
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _run_scan(service)

    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "snapshot_funnel"
    assert funnel["deep_stage_ran"] is False
    assert funnel["deep_symbols_empty"] is False
    assert funnel["deep_empty_reason"] == ""
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["selection_source"] == "direct_scan"
    assert controls["effective_input_cap"] == config.week5.monster_scan_max_symbols
    # 既有直接路径语义：raw 候选（非 deep）进入 pipeline。
    assert len(pipeline_symbols) == 1
    assert pipeline_symbols[0] == candidates
    assert funnel["pipeline_input_count"] == len(candidates)


def test_week5_scan_offhours_refresh_snapshot_failure_fail_closed(
    tmp_path: Path,
) -> None:
    """自动 offhours 链路（sync_reason=offhours_refresh）快照失败必须
    fail-closed：不得回退重型直接 prefilter（P1 修复）。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []
    universe_symbols = ["600519", "000001", "601318"]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(universe_symbols, len(universe_symbols)),
    )
    _patch_attr(
        service,
        "ensure_week5_feature_snapshot",
        lambda **kwargs: {"ok": False, "skipped": False, "build": {"ok": False}},
    )
    heavy_prefilter_calls: list[bool] = []

    def _heavy_prefilter(**kwargs: object) -> dict[str, object]:
        heavy_prefilter_calls.append(True)
        return {"applied": True, "shortlisted_count": 3, "symbols": universe_symbols}

    _patch_attr(service, "_prefilter_week5_universe_symbols", _heavy_prefilter)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            sync_reason="offhours_refresh",
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )

    assert str(report.get("status", "")) == "blocked_data_gate"
    assert heavy_prefilter_calls == []
    assert report.get("funnel_policy") == "snapshot_funnel"


def test_week5_scan_offhours_refresh_watchlist_scan_not_blocked(
    tmp_path: Path,
) -> None:
    """offhours_refresh + 非全市场（watchlist 直接扫描）不被误判 fail-closed：
    direct_non_universe 路径在快照失败时保持既有直接扫描行为。"""
    config = _load_test_config()
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _as_mapping(
        service.run_week5_scan(
            symbols=["600000", "000001"],
            timestamp=datetime(2026, 3, 16, 20, 30),
            sync_reason="offhours_refresh",
            notify_enabled=False,
        )
    )

    assert str(report.get("status", "")) != "blocked_data_gate"
    assert _as_mapping(report["funnel"])["policy"] == "direct_non_universe"
    assert len(pipeline_symbols) == 1
    assert pipeline_symbols[0] == ["600000", "000001"]


def test_week5_scan_manual_recovery_bypasses_fail_closed(tmp_path: Path) -> None:
    """手动恢复（自定义 sync_reason）在快照失败时仍可绕过 fail-closed，
    走既有直接 prefilter 路径（恢复通道保留）。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []
    universe_symbols = ["600519", "000001", "601318"]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(universe_symbols, len(universe_symbols)),
    )
    _patch_attr(
        service,
        "ensure_week5_feature_snapshot",
        lambda **kwargs: {"ok": False, "skipped": False, "build": {"ok": False}},
    )
    _patch_scan_surroundings(service, {})
    heavy_prefilter_calls: list[bool] = []

    def _heavy_prefilter(**kwargs: object) -> dict[str, object]:
        heavy_prefilter_calls.append(True)
        return {
            "applied": True,
            "shortlisted_count": 3,
            "symbols": universe_symbols,
            "shortlisted": [],
        }

    # 必须在 _patch_scan_surroundings 之后覆盖：后者也会打桩该 prefilter。
    _patch_attr(service, "_prefilter_week5_universe_symbols", _heavy_prefilter)
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            sync_reason="manual_recovery",
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )

    assert str(report.get("status", "")) != "blocked_data_gate"
    assert heavy_prefilter_calls == [True]
    assert len(pipeline_symbols) == 1


def test_pipeline_run_once_symbol_timing_and_heartbeat_callback() -> None:
    """pipeline.run_once：逐股计时（输入顺序）+ 进度回调开始/完成各一次。"""
    config = _load_test_config()
    service = _new_service(config)
    pipeline = cast(AnalyzerPipeline, service._pipeline)
    events: list[tuple[str, int, int, bool]] = []

    report = pipeline.run_once(
        symbols=["600000", "000001"],
        strategy="trend",
        current_equity=1.0,
        on_symbol_progress=lambda symbol, index, total, started: events.append(
            (symbol, index, total, started)
        ),
    )

    assert len(report.signals) == 2
    assert events == [
        ("600000", 0, 2, True),
        ("600000", 0, 2, False),
        ("000001", 1, 2, True),
        ("000001", 1, 2, False),
    ]
    symbol_ms = pipeline._last_symbol_stage_ms  # noqa: SLF001
    assert [item["symbol"] for item in symbol_ms] == ["600000", "000001"]
    assert all(int(item["duration_ms"]) >= 0 for item in symbol_ms)


def test_week5_scan_blocked_report_carries_real_scan_profile_and_progress(
    tmp_path: Path,
) -> None:
    """blocked 报告保留真实 scan_profile，进度文件终态为 blocked 而非
    completed（复核 P2 修复）。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    progress_path = tmp_path / "week5_scan_progress_blocked.json"
    config.week5.scan_progress_path = str(progress_path)
    service = _new_service(config)
    service.state.watchlist = []
    universe_symbols = ["600519", "000001", "601318"]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(universe_symbols, len(universe_symbols)),
    )
    _patch_attr(
        service,
        "ensure_week5_feature_snapshot",
        lambda **kwargs: {"ok": False, "skipped": False, "build": {"ok": False}},
    )

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            sync_reason="offhours_refresh",
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
            scan_profile="offhours_forced_full_deep",
        )
    )

    assert str(report.get("status", "")) == "blocked_data_gate"
    # blocked 报告保留真实 scan_profile（不再是固定 default）。
    assert report.get("scan_profile") == "offhours_forced_full_deep"
    # 进度文件终态为 blocked，不误报 completed。
    payload = cast(
        dict[str, object],
        json.loads(progress_path.read_text(encoding="utf-8")),
    )
    assert payload["status"] == "blocked"
    assert payload["scan_profile"] == "offhours_forced_full_deep"


def test_week5_scan_final_pipeline_substage_timing_and_completed_count(
    tmp_path: Path,
) -> None:
    """final pipeline 报告：pipeline_stage_ms 新增 5 子阶段 + completed_count
    （Phase 1 收尾：细分计时下钻）。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=5)
    symbols_under_test: list[str] = []

    def _fake_pipeline(**kwargs: object) -> dict[str, object]:
        symbols = _as_text_list(cast(list, kwargs.get("symbols", [])))
        symbols_under_test.extend(symbols)
        return {
            "trace_id": "substage-timing-trace",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
            "runtime": {
                "duration_ms": 3000,
                "pipeline_stage_ms": {
                    "fetch_bars_ms": 120,
                    "feature_engine_ms": 2500,
                    "inference_ms": 180,
                    "intraday_ms": 400,
                    "market_context_ms": 300,
                    "cross_review_ms": 50,
                    "score_risk_ms": 90,
                    "learning_persist_ms": 60,
                    "completed_count": len(symbols),
                },
                "pipeline_symbol_ms": [],
                "pipeline_parallel_transform": {
                    "enabled": False,
                    "configured_workers": 1,
                    "submitted_count": 0,
                    "worker_count": 0,
                    "worker_pids": [],
                    "max_concurrency": 0,
                    "wall_ms": 0,
                    "worker_transform_ms": 0,
                    "fallback_count": 0,
                    "pool_fallback": False,
                    "pool_error": "",
                },
            },
        }

    _patch_attr(service, "run_pipeline", _fake_pipeline)
    report = _run_scan(service)

    final_pipeline = _as_mapping(_as_mapping(report["scan_stages"])["final_pipeline"])
    assert final_pipeline["intraday_ms"] == 400
    assert final_pipeline["market_context_ms"] == 300
    assert final_pipeline["cross_review_ms"] == 50
    assert final_pipeline["score_risk_ms"] == 90
    assert final_pipeline["learning_persist_ms"] == 60
    assert final_pipeline["completed_count"] == len(symbols_under_test)


def test_week5_scan_light_records_iteration_equivalent(tmp_path: Path) -> None:
    """light 从 iterrows 改为 records 迭代后：deep 入选项/顺序与既有行为一致
    （评分公式/排序键不变，逐字段等价）。"""
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=3)
    report = _run_scan(service)

    funnel = _as_mapping(report["funnel"])
    assert funnel["policy"] == "snapshot_funnel"
    assert funnel["deep_stage_ran"] is True
    deep_stage = _as_mapping(_as_mapping(report["prefilter"])["deep_stage"])
    # light 迭代方式不影响 deep 入选项数量与快照匹配行数。
    assert _as_mapping(deep_stage)["light_shortlist_count"] > 0
    assert _as_mapping(deep_stage)["snapshot_match_rows"] > 0
    assert _as_mapping(deep_stage)["selected_count"] == 3


def test_week5_scan_post_scan_enrichment_reuses_bars(tmp_path: Path) -> None:
    """final pipeline 开启 post_scan_enrichment 后，first-board/anomaly 复用
    bars 尾部快照，不再对每只 symbol 二次 fetch_daily_bars。"""
    candidates = ["600519", "000001", "601318"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=3)
    fetched_extra: list[str] = []

    _patch_attr(
        service,
        "_select_provider",
        lambda **_: _FetchStub(service, fetched_extra),
    )
    _patch_attr(
        service,
        "run_pipeline",
        _post_scan_pipeline_fake(candidates),
    )

    report = _run_scan(service)
    assert str(report.get("status", "")) != "blocked_data_gate"
    # first-board/anomaly 复用 enrich 数据：无额外 provider 拉取记录。
    assert fetched_extra == []


def test_week5_scan_post_scan_enrichment_fallback_fetches(tmp_path: Path) -> None:
    """post_scan_enrichment 缺失（字段为空）时，first-board/anomaly 回退
    live_provider 拉取（向后兼容）。"""
    candidates = ["600519", "000001", "601318"]
    service = _prepare_snapshot_service(tmp_path, candidates=candidates, deep_target=3)
    fetched_extra: list[str] = []

    _patch_attr(
        service,
        "_select_provider",
        lambda **_: _FetchStub(service, fetched_extra),
    )
    _patch_attr(
        service,
        "run_pipeline",
        _post_scan_pipeline_fake(candidates, include_enrichment=False),
    )

    report = _run_scan(service)
    assert str(report.get("status", "")) != "blocked_data_gate"
    # enrich 为空 => 回退拉取，每只 pipeline 输入 symbol 都被 fetch。
    assert set(fetched_extra) == set(candidates)


class _FetchStub:
    def __init__(self, service: StockAnalyzerService, fetched: list[str]) -> None:
        self._service = service
        self._fetched = fetched

    def fetch_daily_bars(self, symbol: str, lookback_days: int) -> object:
        self._fetched.append(symbol)
        return self._service._provider.fetch_daily_bars(  # noqa: SLF001
            symbol=symbol,
            lookback_days=lookback_days,
        )


def _post_scan_pipeline_fake(
    candidates: list[str],
    include_enrichment: bool = True,
):
    """构造带（或不带）post_scan_enrichment 的 signals，覆盖 first-board 的
    复用与回退两条消费路径。"""
    import json as _json

    def _fake(**kwargs: object) -> dict[str, object]:
        symbols = _as_text_list(cast(list, kwargs.get("symbols", [])))
        rows = [
            {
                "date": f"2026-03-1{day}",
                "open": 10.0 + day,
                "high": 11.0 + day,
                "low": 9.0 + day,
                "close": 10.5 + day,
                "turnover": 20_000_000.0 + day,
            }
            for day in range(1, 6)
        ]
        return {
            "trace_id": "post-scan-trace",
            "signals": [
                {
                    "symbol": symbol,
                    "strategy": "monster",
                    "score": 80.0,
                    "grade": "A",
                    "action": "buy",
                    "target_position": 0.1,
                    "probabilities": {"lgbm": 0.8, "xgb": 0.7, "meta": 0.75},
                    "reasons": ["test"],
                    "decision_trace": {},
                    "post_scan_enrichment": (
                        _json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
                        if include_enrichment
                        else ""
                    ),
                }
                for symbol in symbols
                if symbol in candidates
            ],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
            "runtime": {"duration_ms": 1000},
        }

    return _fake


class _GateDroppingProvider(SyntheticProvider):
    """包装 SyntheticProvider：最新一根 daily bar 的 available_time 设为未来，
    使 time-gate 丢弃最新行（模拟 realtime 决策时最新 bar 尚未可用）。
    记录 gate 前的最新收盘，供 post_scan_enrichment 断言对比。"""

    def __init__(self) -> None:
        super().__init__(seed_offset=2027)
        self.original_latest_close: dict[str, float] = {}

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: object | None = None,
    ) -> pd.DataFrame:
        frame = super().fetch_daily_bars(
            symbol=symbol,
            lookback_days=lookback_days,
            end_date=end_date,  # type: ignore[arg-type]
        )
        self.original_latest_close[symbol] = float(frame.iloc[-1]["close"])
        times = pd.to_datetime(frame.index)
        future = pd.Timestamp(datetime.now() + timedelta(days=1))
        available = pd.Series(times, index=frame.index)
        available.iloc[-1] = future
        frame = frame.copy()
        frame["available_time"] = available
        return frame


def test_post_scan_enrichment_uses_gate_pre_raw_bars() -> None:
    """🟡 回归：time-gate 丢弃最新 bar 时，post_scan_enrichment 仍应反映
    gate 前的原始最新收盘，而非 gate 后的倒数第二根（first-board/anomaly
    依赖最新收盘判断涨停/跳空，不能用被丢行的 bars）。"""
    config = _load_test_config()
    service = _new_service(config)
    pipeline = cast(AnalyzerPipeline, service._pipeline)
    provider = _GateDroppingProvider()
    pipeline._provider = provider  # noqa: SLF001

    report = pipeline.run_once(
        symbols=["600000"],
        strategy="monster",
        current_equity=1.0,
        capture_post_scan_enrichment=True,
    )

    assert len(report.signals) == 1
    enrich = report.signals[0].post_scan_enrichment
    assert enrich
    rows = json.loads(enrich)
    assert isinstance(rows, list) and rows
    # 反序列化后的最新收盘 == gate 前原始最新收盘（含被 time-gate 丢弃的行）。
    assert float(rows[-1]["close"]) == pytest.approx(provider.original_latest_close["600000"])
