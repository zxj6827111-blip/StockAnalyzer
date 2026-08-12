"""Week5 扫描链路接入 feature snapshot 的集成测试。

覆盖范围（与 test_feature_snapshot.py 的 snapshot 层测试互补，这里是服务层）：
- 质量选择先于 snapshot 判断，快照只覆盖实际候选集（非全市场）；
- 快照 current 且候选集一致时跳过构建；
- 构建失败时 scheduler 路径 fail-closed，不进入重型 fallback；
- 最终 pipeline 输入不超过 deep target；
- 100/300 配置覆盖关系（默认 100、offhours 工作日档 300）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from stock_analyzer.feature.snapshot import build_feature_snapshot, load_feature_snapshot
from stock_analyzer.runtime.service import StockAnalyzerService
from tests.test_service_week5 import (
    _as_mapping,
    _as_text_list,
    _enable_universe_quality_selector,
    _load_test_config,
    _new_service,
    _patch_attr,
)


def _patch_scan_surroundings(service: StockAnalyzerService, captured: dict[str, object]) -> None:
    """Patch prefilter/pipeline/board/anomaly so the scan completes without a
    real model pipeline.  Unlike ``_patch_minimal_prefilter_and_pipeline`` this
    deliberately does NOT stub ensure_week5_feature_snapshot: the integration
    tests exercise the real snapshot build path."""
    _patch_attr(
        service,
        "_prefilter_week5_universe_symbols",
        lambda **kwargs: {
            "enabled": True,
            "applied": True,
            "lookback_days": 240,
            "top_k": 200,
            "universe_count": len(_as_text_list(kwargs.get("symbols", []))),
            "eligible_count": len(_as_text_list(kwargs.get("symbols", []))),
            "shortlisted_count": len(_as_text_list(kwargs.get("symbols", []))),
            "symbols": _as_text_list(kwargs.get("symbols", [])),
            "shortlisted": [
                {"symbol": symbol, "baseline_score": 70.0}
                for symbol in _as_text_list(kwargs.get("symbols", []))
            ],
            "preview": [],
            "stages": {
                "stage2": {
                    "applied": False,
                    "status": "pending_signal_scan",
                    "shortlist_top_n": 50,
                    "input_count": 0,
                    "advanced_count": 0,
                    "weights": {},
                    "preview": [],
                }
            },
        },
    )
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service,
        "_monster_isolation_gate",
        lambda **_: {
            "can_open_new_position": True,
            "reasons": [],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
    )


def _quality_selection_fake(selected_symbols: list[str], input_count: int) -> object:
    """Fake _select_universe_quality_candidates with an audit-shaped report."""

    def _fake_select(**kwargs: object) -> dict[str, object]:
        return {
            "selected": list(selected_symbols),
            "report": {
                "selector_mode": "quality",
                "selected_count": len(selected_symbols),
                "input_count": input_count,
                "selected": [
                    {"symbol": symbol, "score": 80.0 - i * 0.1}
                    for i, symbol in enumerate(selected_symbols)
                ],
                "board_quotas": {},
                "input_symbol_hash": "in-hash",
                "output_symbol_hash": "out-hash",
            },
        }

    return _fake_select


def _capture_pipeline(service: StockAnalyzerService, pipeline_symbols: list[list[str]]) -> None:
    def _capture(**kwargs: object) -> dict[str, object]:
        pipeline_symbols.append(_as_text_list(cast(list, kwargs.get("symbols", []))))
        return {
            "trace_id": "snapshot-integration-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        }

    _patch_attr(service, "run_pipeline", _capture)


def test_week5_scan_builds_snapshot_for_quality_candidates_only(
    tmp_path: Path,
) -> None:
    """质量选择得到实际候选集后，snapshot 只覆盖该候选集（不是全市场）；
    light/deep 阶段消费 snapshot，pipeline 只处理 deep 候选。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []

    universe_symbols = [f"600{i:03d}" for i in range(400)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    selected = ["600519", "000001", "601318", "000858", "600000", "300750"]
    select_calls: dict[str, object] = {}

    def _fake_select(**kwargs: object) -> dict[str, object]:
        # 延迟 stub：保证阶段耗时记录非零（P1 回归）。
        time.sleep(0.01)
        select_calls.update(kwargs)
        return cast(dict, _quality_selection_fake(selected, len(universe_symbols)))(**kwargs)

    _patch_attr(service, "_select_universe_quality_candidates", _fake_select)
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )

    # 质量选择先于 snapshot 构建：selector 拿到全市场，snapshot 只覆盖候选集。
    assert select_calls["target_size"] == 300
    symbols_passed = _as_text_list(cast(list, select_calls["symbols"]))
    assert len(symbols_passed) == 400
    manifest, frame = load_feature_snapshot(config)
    assert manifest is not None and frame is not None
    assert set(frame["symbol"]) == set(selected)
    assert manifest.scope == "universe_quality"
    assert manifest.requested_symbol_count == len(selected)
    assert manifest.published_symbol_count == len(selected)

    # ensure 报告挂在 scan report 上。
    fs = report.get("feature_snapshot")
    assert isinstance(fs, Mapping)
    assert fs["ok"] is True
    assert fs["skipped"] is False
    assert fs["requested_symbol_count"] == len(selected)
    # light/deep 均消费 snapshot。
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["mode"] == "snapshot_light"
    assert prefilter["universe_source"] == "test_universe:quality_selector"
    deep_stage = _as_mapping(prefilter["deep_stage"])
    assert deep_stage["mode"] == "snapshot_deep"
    assert _as_mapping(report["funnel"])["mode"] == "snapshot"
    # 阶段耗时结构化记录：修复前 quality/light 计时被后续初始化清零，
    # 这里断言全部阶段 duration_ms 非零（P1 回归）。
    stages = _as_mapping(report["scan_stages"])
    assert _as_mapping(stages["quality_selection"])["selected_count"] == 6
    assert _as_mapping(stages["quality_selection"])["duration_ms"] > 0
    assert _as_mapping(stages["snapshot_ensure"])["ok"] is True
    assert _as_mapping(stages["snapshot_ensure"])["requested_count"] == 6
    assert _as_mapping(stages["snapshot_ensure"])["duration_ms"] > 0
    assert _as_mapping(stages["light_stage"])["mode"] == "snapshot_light"
    assert _as_mapping(stages["light_stage"])["duration_ms"] > 0
    assert _as_mapping(stages["deep_stage"])["mode"] == "snapshot_deep"
    assert _as_mapping(stages["deep_stage"])["duration_ms"] > 0
    # 最终 pipeline 只处理 deep 候选。
    assert len(pipeline_symbols) == 1
    assert set(pipeline_symbols[0]) == set(selected)


def test_week5_scan_skips_snapshot_build_when_current_and_same_set(
    tmp_path: Path,
) -> None:
    """候选集与快照一致且快照 current 时，扫描不再重复构建。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []
    selected = ["600519", "000001", "601318", "000858", "600000", "300750"]

    build_report = build_feature_snapshot(
        config,
        service._provider,  # noqa: SLF001
        symbols=selected,
        lookback_days=250,
        force=True,
        scope="universe_quality",
    )
    assert build_report["ok"] is True

    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": selected, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(selected, len(selected)),
    )
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )
    fs = report.get("feature_snapshot")
    assert isinstance(fs, Mapping)
    assert fs["ok"] is True
    assert fs["skipped"] is True
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["mode"] == "snapshot_light"
    assert _as_mapping(report["funnel"])["mode"] == "snapshot"
    assert len(pipeline_symbols) == 1


def test_week5_scan_snapshot_build_failure_fail_closed_scheduler(
    tmp_path: Path,
) -> None:
    """scheduler 路径下 snapshot 构建失败必须 fail-closed，不进入重型 fallback。"""
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
            sync_reason="scheduler_week5_nightly",
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )
    assert str(report.get("status", "")) == "blocked_data_gate"
    gate = _as_mapping(report["data_gate"])
    assert gate["status"] == "blocked"
    assert any("feature_snapshot_stale" in str(reason) for reason in gate["reasons"])
    # 重型 fallback 从未被调用。
    assert heavy_prefilter_calls == []
    fs = report.get("feature_snapshot")
    assert isinstance(fs, Mapping)
    assert fs["ok"] is False


def test_week5_final_pipeline_input_within_deep_target(tmp_path: Path) -> None:
    """最终 pipeline 输入不超过 deep target（评分/风控/final signal 语义不变）。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    config.week5.deep_candidate_target = 3
    service = _new_service(config)
    service.state.watchlist = []
    candidates = ["600519", "000001", "601318", "000858", "600000", "300750"]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": candidates, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(candidates, len(candidates)),
    )
    _patch_scan_surroundings(service, {})
    pipeline_symbols: list[list[str]] = []
    _capture_pipeline(service, pipeline_symbols)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )
    assert len(pipeline_symbols) == 1
    assert len(pipeline_symbols[0]) == 3
    assert len(pipeline_symbols[0]) <= config.week5.deep_candidate_target
    funnel = _as_mapping(report["funnel"])
    assert funnel["deep_count"] == 3
    stages = _as_mapping(report["scan_stages"])
    assert _as_mapping(stages["deep_stage"])["selected_count"] == 3
    assert _as_mapping(stages["final_pipeline"])["symbols"] == 3


def test_week5_quality_target_default_100_offhours_overrides_300(
    tmp_path: Path,
) -> None:
    """配置覆盖关系：默认 quality target=100；offhours 工作日档以 300 覆盖。"""
    config = _load_test_config()
    config.week5.universe_quality_selector_enabled = True
    config.week5.universe_quality_target_size = 100  # 默认值
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    # 空 watchlist 会触发 forced_full_deep 档；本测试只验证 target 传递，
    # 关闭该触发条件让 weekday_light_topk_deep 档生效。
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 0
    service = _new_service(config)
    service.state.watchlist = []
    universe_symbols = [f"600{i:03d}" for i in range(160)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": universe_symbols, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    # 避免真实构建 100+ 只：ensure 打桩为快速成功（本测试只验证 target 传递）。
    _patch_attr(
        service,
        "ensure_week5_feature_snapshot",
        lambda **kwargs: {"ok": True, "skipped": True, "build": {"ok": True, "skipped": True}},
    )
    _patch_scan_surroundings(service, {})

    select_calls: dict[str, object] = {}

    def _fake_select(**kwargs: object) -> dict[str, object]:
        select_calls.update(kwargs)
        top = universe_symbols[:100]
        return cast(dict, _quality_selection_fake(top, len(universe_symbols)))(**kwargs)

    _patch_attr(service, "_select_universe_quality_candidates", _fake_select)

    service.run_week5_scan(
        timestamp=datetime(2026, 3, 16, 20, 30),
        notify_enabled=False,
        force_universe_scan=True,
        prefilter_enabled_override=True,
    )
    assert select_calls["target_size"] == 100  # 默认 100

    # offhours 工作日 profile 解析：universe_max_symbols=300。
    profile = _as_mapping(
        service._resolve_week5_offhours_scan_profile(now=datetime(2026, 3, 16, 20, 30))  # noqa: SLF001
    )
    assert profile["scan_profile"] == "offhours_weekday_light_topk_deep"
    assert profile["universe_max_symbols"] == 300

    # offhours 传递的 override 落到 quality target=300。
    select_calls.clear()
    service.run_week5_scan(
        timestamp=datetime(2026, 3, 16, 20, 30),
        notify_enabled=False,
        force_universe_scan=True,
        prefilter_enabled_override=True,
        universe_max_symbols_override=300,
    )
    assert select_calls["target_size"] == 300


def test_week5_scan_incremental_refresh_updates_candidate_set(tmp_path: Path) -> None:
    """跨扫描增量：候选集变化（移出 + 新增）后，快照跟随更新且不残留旧股票。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_root = str(tmp_path / "features_light")
    service = _new_service(config)
    service.state.watchlist = []
    first_set = ["600519", "000001", "601318", "000858", "600000", "300750"]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {"source": "test_universe", "symbols": first_set, "errors": []},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(first_set, len(first_set)),
    )
    _patch_scan_surroundings(service, {})
    _capture_pipeline(service, [])

    first_scan = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )
    fs = first_scan.get("feature_snapshot")
    assert isinstance(fs, Mapping)
    assert fs["skipped"] is False

    # 第二日候选集变化：移出 2 只，新增 1 只。
    second_set = ["601318", "000858", "600000", "300750", "002415"]
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        _quality_selection_fake(second_set, len(second_set)),
    )
    _capture_pipeline(service, [])
    second_scan = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 17, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )
    fs2 = second_scan.get("feature_snapshot")
    assert isinstance(fs2, Mapping)
    assert fs2["ok"] is True
    assert fs2["skipped"] is False
    manifest, frame = load_feature_snapshot(config)
    assert manifest is not None and frame is not None
    assert set(frame["symbol"]) == set(second_set)
    assert "600519" not in manifest.per_symbol
    assert "000001" not in manifest.per_symbol
    assert "002415" in manifest.per_symbol
    assert manifest.requested_symbol_count == len(second_set)
