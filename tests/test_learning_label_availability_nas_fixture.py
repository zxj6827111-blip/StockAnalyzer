"""A1 NAS 回归夹具：真实 manifest 的决策日截面画像 → 隔离 purge 判据。

夹具来源（只读探测，未 docker cp、未改 NAS 任何状态）：

    docker exec -i stock-analyzer-api python -
    duckdb.connect('/app/artifacts/training/learning_protocol.duckdb', read_only=True)

对 ``dataset_manifest_v2_8b250aa25009`` 取
``dataset_manifest_items × signal_snapshots × outcome_records``，按**上海决策日**
聚合出每日行数、最早决策时间、**截面最晚标签可用时间**（= 该日所有行
``label_mature_time`` 的最大值）以及该日在旧切分里的归属，落成
``tests/fixtures/manifest_label_availability/..._days.json``。

夹具口径限制：每行的可用时间用该日截面最晚值代替，因此只复现**日级** purge 决策；
``late_maturing_row_count`` 之类的行级统计不以此夹具为断言对象。

本测试钉住的两件事：
1. 旧（成熟日分组）切分确实存在标签可用性泄漏——需要显式量出来，不能静默通过；
2. 新（决策日 purge）切分在**同一份数据**上逐边界满足
   ``max(前段截面标签可用时间) < min(后段决策时间)``，并报出剔除规模。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.learning.dataset_manifest import _build_manifest_items_and_split_plan
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "manifest_label_availability"
    / "dataset_manifest_v2_8b250aa25009_days.json"
)
_MANIFEST_ID = "dataset_manifest_v2_8b250aa25009"
# NAS .env 的生产契约：horizon_days=10 + settlement_lag=1。
_EMBARGO_DAYS = 11


def _load_days() -> list[dict[str, object]]:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert payload["manifest"] == _MANIFEST_ID
    return list(payload["days"])


def _pairs_from_day_profile(
    days: list[dict[str, object]],
) -> list[tuple[SignalSnapshot, OutcomeRecord]]:
    """按日画像合成 (snapshot, outcome) 行；日级聚合与夹具逐字一致。"""

    pairs: list[tuple[SignalSnapshot, OutcomeRecord]] = []
    for entry in days:
        day = str(entry["day"])
        min_decision = datetime.fromisoformat(str(entry["min_decision"]))
        max_available = datetime.fromisoformat(str(entry["max_available"]))
        for index in range(int(entry["rows"])):
            snapshot_id = f"{day}-{index:04d}"
            symbol = f"{600000 + index:06d}.SH"
            pairs.append(
                (
                    SignalSnapshot(
                        snapshot_id=snapshot_id,
                        code_version="git:nas-fixture",
                        symbol=symbol,
                        strategy="trend",
                        decision_time=min_decision,
                        feature_vector={"ret_1d": 0.0},
                        feature_schema_id="fs-nas",
                        feature_schema_hash="fsh-nas",
                        runtime_config_hash="runtime_hash_nas",
                        label_policy_id="label_policy_v3_b0b3724553b5",
                        label_policy_hash="nas-fixture",
                    ),
                    OutcomeRecord(
                        snapshot_id=snapshot_id,
                        maturity_status=MaturityStatus.RECONCILED,
                        label_anchor_time=min_decision,
                        label_mature_time=max_available,
                        realized_return=0.0,
                        backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                        backfill_source="nas_fixture_probe",
                    ),
                )
            )
    return pairs


def _old_split_leak(days: list[dict[str, object]], previous: str, following: str) -> dict[str, object]:
    """旧切分下该边界的泄漏规模（决策日粒度）。

    口径：后段某决策日的**最早决策时间**早于前段截面标签可用时间（前段各决策日
    ``max_available`` 的最大值）即为泄漏。行数按该日在**后段**的分段行数计，
    避免旧切分里同日跨段（校准/测试重叠日）被重复计数。
    """

    previous_days = [day for day in days if previous in dict(day["splits"])]  # type: ignore[arg-type]
    following_days = [day for day in days if following in dict(day["splits"])]  # type: ignore[arg-type]
    prev_max_available = max(
        datetime.fromisoformat(str(day["max_available"])) for day in previous_days
    )
    following_min_decision = min(
        datetime.fromisoformat(str(day["min_decision"])) for day in following_days
    )
    leaked_days = [
        day
        for day in following_days
        if datetime.fromisoformat(str(day["min_decision"])) < prev_max_available
    ]
    return {
        "prev_max_label_available": prev_max_available.isoformat(),
        "next_min_decision": following_min_decision.isoformat(),
        "prev_rows": sum(int(dict(day["splits"])[previous]) for day in previous_days),  # type: ignore[arg-type]
        "next_rows": sum(int(dict(day["splits"])[following]) for day in following_days),  # type: ignore[arg-type]
        "leaked_decision_days": len(leaked_days),
        "leaked_rows": sum(int(dict(day["splits"])[following]) for day in leaked_days),  # type: ignore[arg-type]
        "satisfied": bool(prev_max_available < following_min_decision),
    }


def test_nas_fixture_old_maturity_split_leaks_label_availability() -> None:
    """旧切分的泄漏必须可复现地量出来（否则下面的修复无从对照）。"""

    days = _load_days()
    assert len(days) == 120
    assert sum(int(day["rows"]) for day in days) == 40000
    assert {name for day in days for name in dict(day["splits"])} == {  # type: ignore[arg-type]
        "train",
        "calibration",
        "test",
    }

    train_to_calibration = _old_split_leak(days, "train", "calibration")
    calibration_to_test = _old_split_leak(days, "calibration", "test")

    for boundary in (train_to_calibration, calibration_to_test):
        assert boundary["satisfied"] is False
        assert boundary["leaked_rows"] > 0
        assert boundary["leaked_decision_days"] > 0


def test_nas_fixture_new_split_satisfies_isolation_and_reports_purge() -> None:
    """修复后：逐边界满足判据，并显式报出剔除规模与额外交易日间隔。"""

    days = _load_days()
    pairs = _pairs_from_day_profile(days)

    items, split_plan, report, blocking_flags = _build_manifest_items_and_split_plan(
        included_pairs=pairs,
        calibration_ratio=0.1,
        test_ratio=0.1,
        embargo_days=_EMBARGO_DAYS,
    )

    assert blocking_flags == []
    assert report["policy"] == "decision_day_label_availability_purge_v1"
    assert report["status"] == "isolated"
    assert report["rows_before_purge"] == 40000
    assert report["violations"] == 0

    # 逐边界断言（两侧均取决策日粒度）。
    boundaries = {item["name"]: item for item in report["boundaries"]}  # type: ignore[union-attr]
    assert set(boundaries) == {"train->calibration", "calibration->test"}
    for name in ("train->calibration", "calibration->test"):
        boundary = boundaries[name]
        assert boundary["satisfied"] is True
        assert datetime.fromisoformat(
            boundary["prev_max_label_available"]
        ) < datetime.fromisoformat(boundary["next_min_decision"])
        assert boundary["gap_trading_days"] > 0

    # 显式报出剔除规模（不是静默通过），且账本闭合。
    assert int(report["purged_decision_days"]) > 0
    assert int(report["purged_rows"]) > 0
    assert int(report["purged_rows"]) == 40000 - len(items)
    assert report["purged_rows"] == int(report["purged_rows"])
    assert (
        sum(int(day["rows"]) for day in days)
        == len(items) + int(report["purged_rows"])
    )

    # 三段非空，且 test 取最后 10% 决策日（口径不变）。
    counts = {entry.split_name: entry.row_count for entry in split_plan}
    assert set(counts) == {"train", "calibration", "test"}
    assert all(count > 0 for count in counts.values())
    assert counts["test"] == sum(int(day["rows"]) for day in days[-12:])

    # 账本的另一半：段内有效交易日 + 剔除决策日 = 全部决策日，且各段行数与
    # 成员总数一致（剔除的是整日截面，不产生残缺日）。
    splits = report["splits"]
    assert isinstance(splits, dict)
    assigned_days = sum(
        int(splits[name]["effective_trading_days"])  # type: ignore[index]
        for name in ("train", "calibration", "test")
    )
    assert assigned_days + int(report["purged_decision_days"]) == int(
        report["decision_days_total"]
    )
    assert sum(counts.values()) == len(items)


def test_nas_fixture_new_split_is_deterministic() -> None:
    """同一画像重跑两次，剔除决策日集合与报告必须一致。"""

    pairs = _pairs_from_day_profile(_load_days())
    first_items, _, first_report, _ = _build_manifest_items_and_split_plan(
        included_pairs=pairs,
        calibration_ratio=0.1,
        test_ratio=0.1,
        embargo_days=_EMBARGO_DAYS,
    )
    second_items, _, second_report, _ = _build_manifest_items_and_split_plan(
        included_pairs=list(reversed(pairs)),
        calibration_ratio=0.1,
        test_ratio=0.1,
        embargo_days=_EMBARGO_DAYS,
    )

    assert first_report == second_report
    assert [(item["snapshot_id"], item["split_name"]) for item in first_items] == [
        (item["snapshot_id"], item["split_name"]) for item in second_items
    ]
