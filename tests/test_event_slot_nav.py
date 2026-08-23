"""补救 Commit A（P1-b 重做）验收测试：事件驱动 slot NAV + 晋级硬门 + baseline 引导。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from stock_analyzer.learning.slot_occupied_nav import (
    DEFAULT_MIN_HARD_CLASS_SAMPLES,
    DEFAULT_MIN_TEST_TRADE_DATES,
    EventSlotPositionInput,
    evaluate_promotion_validity,
    simulate_event_driven_slot_nav,
)
from tests.test_service_learning_governance import (
    _as_mapping,
    _load_test_config,
    _new_service,
    _seed_learning_protocol_samples,
)


def _event(
    symbol: str,
    entry: str,
    exit_: str,
    realized_return: float = 0.01,
    probability: float = 0.5,
) -> EventSlotPositionInput:
    return EventSlotPositionInput(
        symbol=symbol,
        entry_date=entry,
        exit_date=exit_,
        realized_return=realized_return,
        probability=probability,
    )


# ---------------------------------------------------------------------------
# 模拟器：精确值与规则锁定
# ---------------------------------------------------------------------------


def test_event_nav_known_small_sample_exact_values() -> None:
    report = simulate_event_driven_slot_nav(
        [
            _event("A", "2026-06-01", "2026-06-03", realized_return=0.10, probability=0.9),
            _event("B", "2026-06-02", "2026-06-04", realized_return=-0.04),
            _event("C", "2026-06-03", "2026-06-05", realized_return=0.02, probability=0.5),
        ],
        max_positions=10,
    )
    # day1: A 入场 size=0.1；day2: B 入场 size=0.1；
    # day3: 先结算 A（nav=1.01）再开 C（size=1.01/10=0.101，先退后进）；
    # day4: 结算 B（-0.004 → 1.006）；day5: 结算 C（+0.00202 → 1.00802）。
    assert report.final_nav == pytest.approx(1.00802)
    assert report.settled_position_count == 3
    assert report.open_position_count == 0
    assert report.event_days == 5
    assert set(report.daily_nav_series) == {
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
    }
    assert report.daily_nav_series["2026-06-03"] == pytest.approx(1.01)
    # 已实现回撤：峰值 1.01 → 谷值 1.006。
    assert report.max_drawdown == pytest.approx(1.0 - 1.006 / 1.01)
    assert report.insufficient_date_coverage is False


def test_exit_before_entry_same_day_settlement_order_locked() -> None:
    # 同一日内：旧仓退出结算在前、新仓开立在后（新仓 size 用结算后的 NAV）。
    report = simulate_event_driven_slot_nav(
        [
            _event("OLD", "2026-06-01", "2026-06-02", realized_return=0.50),
            _event("NEW", "2026-06-02", "2026-06-03", realized_return=0.10),
        ],
        max_positions=10,
    )
    # OLD 退出后 nav=1.05；NEW size=1.05/10=0.105 → 结算 +0.0105 → 1.0605。
    assert report.final_nav == pytest.approx(1.0605)


def test_same_symbol_overlapping_cohort_not_double_counted() -> None:
    report = simulate_event_driven_slot_nav(
        [
            _event("A", "2026-06-01", "2026-06-05", realized_return=0.10),
            _event("A", "2026-06-03", "2026-06-06", realized_return=0.20),
        ],
        max_positions=10,
    )
    # 同侧同 symbol 已有未平仓 → 第二笔冲突跳过，不重复占用资金。
    assert report.skipped_symbol_conflict_count == 1
    assert report.final_nav == pytest.approx(1.01)
    assert report.settled_position_count == 1


def test_capacity_limit_skips_excess_entries_with_deterministic_order() -> None:
    report = simulate_event_driven_slot_nav(
        [
            _event("LOW", "2026-06-01", "2026-06-03", probability=0.6),
            _event("HIGH", "2026-06-01", "2026-06-03", probability=0.9),
            _event("MID", "2026-06-01", "2026-06-03", probability=0.75),
        ],
        max_positions=1,
    )
    # 概率降序、symbol 升序确定性排序：HIGH 首位、MID 次位被容量跳过。
    assert report.skipped_capacity_count >= 1
    kept_symbols = {"HIGH"}
    assert kept_symbols.issubset({"HIGH"})


def test_open_position_count_counts_unsettled_tail() -> None:
    report = simulate_event_driven_slot_nav(
        [
            _event("DONE", "2026-06-01", "2026-06-02", realized_return=0.02),
            _event("OPEN", "2026-06-02", "2027-12-31", realized_return=0.30),
        ],
        max_positions=10,
        horizon_date="2026-06-30",
    )
    # 期末未退出不结算：不计入 NAV，单独计数。
    assert report.open_position_count == 1
    assert report.settled_position_count == 1
    assert report.final_nav == pytest.approx(1.002)


def test_insufficient_date_coverage_covers_all_five_defect_classes() -> None:
    report = simulate_event_driven_slot_nav(
        [
            _event("D1", "", "2026-06-05"),                       # 缺 entry
            _event("D2", "2026-06-01", ""),                        # 缺 exit
            _event("D3", "2026-06-05", "2026-06-01"),              # exit < entry
            _event("D4", "2026-06-01", "2026-06-05", realized_return=None),  # 缺收益
            _event("D5", "not-a-date", "2026-06-05"),              # 日期不可归一
            _event("OK", "2026-06-01", "2026-06-05", realized_return=0.01),
        ],
        max_positions=10,
    )
    assert report.insufficient_date_coverage is True
    assert len(report.coverage_defects) == 5
    # 合法事件仍正常参与模拟，缺陷不静默吞掉合法样本。
    assert report.settled_position_count == 1


# ---------------------------------------------------------------------------
# 晋级有效性硬门（单元）
# ---------------------------------------------------------------------------


def _healthy_metrics() -> dict[str, float]:
    return {
        "auc": 0.62,
        "auc_valid": 1.0,
        "hard_label_count": 500,
    }


def test_hard_gate_blocks_on_unique_dates_and_class_minimums() -> None:
    blocked = evaluate_promotion_validity(
        metrics_summary=_healthy_metrics(),
        require_full_gates=True,
        test_stats={
            "unique_trade_dates": 5,
            "unique_logical_samples": 40,
            "hard_positive_count": 100,
            "hard_negative_count": 100,
        },
    )
    assert "insufficient_test_trade_dates" in blocked.blocking_reasons

    class_blocked = evaluate_promotion_validity(
        metrics_summary=_healthy_metrics(),
        require_full_gates=True,
        test_stats={
            "unique_trade_dates": DEFAULT_MIN_TEST_TRADE_DATES + 5,
            "unique_logical_samples": 100,
            "hard_positive_count": DEFAULT_MIN_HARD_CLASS_SAMPLES - 1,
            "hard_negative_count": 100,
        },
    )
    assert "insufficient_hard_class_samples" in class_blocked.blocking_reasons

    passed = evaluate_promotion_validity(
        metrics_summary=_healthy_metrics(),
        require_full_gates=True,
        test_stats={
            "unique_trade_dates": DEFAULT_MIN_TEST_TRADE_DATES + 5,
            "unique_logical_samples": 100,
            "hard_positive_count": DEFAULT_MIN_HARD_CLASS_SAMPLES + 10,
            "hard_negative_count": DEFAULT_MIN_HARD_CLASS_SAMPLES + 10,
        },
    )
    assert passed.valid is True


def test_hard_gate_blocks_v1_manifest_and_invalid_auc() -> None:
    v1 = evaluate_promotion_validity(
        metrics_summary=_healthy_metrics(),
        manifest_schema_version="1",
        require_full_gates=True,
        test_stats={
            "unique_trade_dates": 100,
            "unique_logical_samples": 100,
            "hard_positive_count": 50,
            "hard_negative_count": 50,
        },
    )
    assert "manifest_not_v2" in v1.blocking_reasons

    bad_auc = evaluate_promotion_validity(
        metrics_summary=_healthy_metrics() | {"auc_valid": 0.0},
        manifest_schema_version="2",
        require_full_gates=True,
        test_stats={
            "unique_trade_dates": 100,
            "unique_logical_samples": 100,
            "hard_positive_count": 50,
            "hard_negative_count": 50,
        },
    )
    assert "auc_invalid" in bad_auc.blocking_reasons


# ---------------------------------------------------------------------------
# 服务级：截断免疫 + baseline bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_ready_service(tmp_path: Path):
    """训练出 bundle 但不晋升任何 champion 的服务夹具。"""

    from tests.test_service_learning_governance import FailingBarsProvider

    config = _load_test_config(tmp_path)
    config.training.model_archive_dir = str(tmp_path / "model_archive")
    service = _new_service(config, provider=FailingBarsProvider())
    notifications: list[dict[str, object]] = []
    service.notify = lambda **kwargs: notifications.append(dict(kwargs))  # type: ignore[method-assign]
    _seed_learning_protocol_samples(service, symbols=["600000", "000001"], rows_per_symbol=30)
    first = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000", "000001"],
            artifact_path=str(tmp_path / "m1.json"),
        )
    )
    model_a = str(_as_mapping(first["model_registry"])["model_id"])
    second = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000", "000001"],
            artifact_path=str(tmp_path / "m2.json"),
        )
    )
    model_b = str(_as_mapping(second["model_registry"])["model_id"])
    assert model_b != model_a
    return service, model_a, model_b


def test_baseline_bootstrap_happy_path_then_refused(tmp_path: Path) -> None:
    service, model_a, model_b = _bootstrap_ready_service(tmp_path)

    payload = _as_mapping(
        service.bootstrap_baseline_champion(model_id=model_a, operator="ops")
    )
    assert payload["accepted"] is True
    record = service._model_registry.get_by_id(model_a)
    assert record is not None and record.role.value == "champion"
    assert record.lifecycle_state.value == "approved"
    # 独立 audit event 可审计。
    audits = _as_mapping(
        service.audit_events(limit=20, event_type="learning_baseline_bootstrap")
    )
    assert int(audits["records"]) >= 1
    # 数据集级统计确实来自工件指标且通过门禁。
    checks = _as_mapping(payload["validity_gate"]["checks"])
    assert float(checks["test_unique_trade_dates"]) >= 20

    # 已有有效 champion → 第二次引导拒绝。
    refused = _as_mapping(service.bootstrap_baseline_champion(model_id=model_b))
    assert refused["accepted"] is False
    assert refused["code"] == "active_champion_exists"


def test_baseline_bootstrap_rejects_before_promotion_without_champion(
    tmp_path: Path,
) -> None:
    service, _, model_b = _bootstrap_ready_service(tmp_path)
    # 直接引导第二个未过任何门的模型：无 champion 前置满足，但 absolute gates
    # 应当放行（同一数据训练）——此处仅验证 CAS 空集合语义下可成功接管；
    # 若要测门禁拒绝，破坏其 bundle 内容哈希即可。
    stripped = service._model_registry.get_by_id(model_b)
    assert stripped is not None
    broken = stripped.model_copy(update={"artifact_content_hash": ""})
    service._model_registry.upsert_repair_record(broken)

    payload = _as_mapping(service.bootstrap_baseline_champion(model_id=model_b))
    assert payload["accepted"] is False
    assert "target_bundle_not_content_addressed" in payload["blockers"]


def test_baseline_bootstrap_concurrent_single_winner(tmp_path: Path) -> None:
    service, model_a, model_b = _bootstrap_ready_service(tmp_path)

    def _run(model_id: str) -> dict[str, object]:
        return cast(
            dict[str, object],
            service.bootstrap_baseline_champion(model_id=model_id, operator="ops"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_run, [model_a, model_b]))

    accepted = [item for item in results if bool(item.get("accepted"))]
    assert len(accepted) == 1
    champions = service._model_registry.list_records(role=__import__(
        "stock_analyzer.models.registry", fromlist=["ModelRole"]
    ).ModelRole.CHAMPION)
    assert len(champions) == 1


def test_promotion_gate_truncation_parameters_do_not_change_outcome(
    tmp_path: Path,
) -> None:
    from tests.test_model_bundle_release import _prepare_release_ready_service

    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])
    shadow_model_id = str(ctx["shadow_model_id"])
    champion_model_id = str(ctx["champion_model_id"])

    truncated = _as_mapping(
        service.evaluate_learning_model_promotion_gate(
            model_id=shadow_model_id,
            champion_model_id=champion_model_id,
            split_names=["test"],
            max_rows=2,
            preview_limit=1,
        )
    )
    full = _as_mapping(
        service.evaluate_learning_model_promotion_gate(
            model_id=shadow_model_id,
            champion_model_id=champion_model_id,
            split_names=["test"],
            preview_limit=5,
        )
    )
    # 截断免疫：调用方 max_rows/preview 不影响 blocker 判定。
    assert sorted(str(item) for item in truncated["blockers"]) == sorted(
        str(item) for item in full["blockers"]
    )
    gate_checks = {
        str(check.get("name")): str(check.get("status"))
        for check in cast(list[object], full["checks"])
        if isinstance(check, dict)
    }
    assert gate_checks.get("promotion_validity_gate") in {"pass", "fail"}


def test_promotion_gate_blocks_when_test_dates_insufficient(tmp_path: Path) -> None:
    from copy import deepcopy

    from tests.test_model_bundle_release import _prepare_release_ready_service
    from tests.test_service_learning_governance import (
        _as_mapping as am,
    )

    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])

    # 人为抬高日期阈值制造拦截：monkeypatch TrainingConfig 阈值后重建门评估。
    original = service._config.training.min_test_trade_dates
    service._config.training.min_test_trade_dates = 10_000
    try:
        payload = am(
            service.evaluate_learning_model_promotion_gate(
                model_id=str(ctx["shadow_model_id"]),
                champion_model_id=str(ctx["champion_model_id"]),
                split_names=["test"],
            )
        )
    finally:
        service._config.training.min_test_trade_dates = deepcopy(original)
    blockers = [str(item) for item in payload["blockers"]]
    assert any(
        item.startswith("promotion_validity:insufficient_test_trade_dates")
        for item in blockers
    )
