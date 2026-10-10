"""留档时间的带版本解释规则验收（改进计划 §3.1"旧记录保留原始值，通过带版本的解释规则兼容"）。

钉住的是读侧最容易糊过去的一步：同一目录里同时躺着修复前后的留档，裸时间戳没有偏移，
把它当成 Asia/Shanghai 是**猜测**。规则必须把猜测变成可见的版本与 caveat，
而不是悄悄替旧记录补一个时区。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    ModelIdentity,
    build_funnel_trace,
    read_trace,
    record_stage,
    write_trace,
)
from stock_analyzer.research.record_time_semantics import (
    LEGACY_TIME_CAVEAT,
    TIME_INTERPRETATION_V1,
    TIME_INTERPRETATION_V2,
    annotate_time_semantics,
    interpret_record_time,
)


def _payload(**overrides) -> dict:
    payload = {
        "trade_date": "2026-10-09",
        "written_at": "2026-10-09T14:50:03+08:00",
        "written_at_timezone": "Asia/Shanghai",
    }
    payload.update(overrides)
    return payload


def test_aware_record_with_matching_zone_is_v2_and_usable() -> None:
    read = interpret_record_time(_payload(), expected_timezone="Asia/Shanghai")
    assert read.interpretation_version == TIME_INTERPRETATION_V2
    assert read.evidence_eligible is True
    assert read.caveats == ()
    assert read.instant_iso == "2026-10-09T14:50:03+08:00"


def test_naive_record_keeps_its_value_but_gains_no_time_zone() -> None:
    """旧记录：原值一字不改，但不给它发明时区，也不当可用证据。"""
    record = _payload(written_at="2026-10-09T14:50:03")
    read = interpret_record_time(record)
    assert read.interpretation_version == TIME_INTERPRETATION_V1
    assert read.evidence_eligible is False
    assert read.instant_iso == ""
    assert read.caveats == (LEGACY_TIME_CAVEAT,)
    assert annotate_time_semantics(record)["written_at"] == "2026-10-09T14:50:03"


def test_absent_or_unparseable_written_at_is_named_not_silently_eligible() -> None:
    assert interpret_record_time({"written_at": ""}).caveats == ("written_at_absent",)
    broken = interpret_record_time({"written_at": "not-a-time"})
    assert broken.interpretation_version == TIME_INTERPRETATION_V1
    assert "written_at_unparseable" in broken.caveats
    assert broken.evidence_eligible is False


def test_declaration_that_contradicts_the_offset_loses_evidence() -> None:
    """声明与时刻的真实偏移矛盾：矛盾比缺失危险，必须撤销证据资格。"""
    read = interpret_record_time(_payload(written_at_timezone="America/New_York"))
    assert read.evidence_eligible is False
    assert "timezone_declaration_mismatch" in read.caveats

    unknown = interpret_record_time(_payload(written_at_timezone="Mars/Olympus"))
    assert unknown.evidence_eligible is False
    assert any(item.startswith("timezone_name_unknown") for item in unknown.caveats)


def test_undeclared_zone_is_still_an_absolute_instant_but_flagged() -> None:
    read = interpret_record_time({"written_at": "2026-10-09T06:50:03+00:00"})
    assert read.interpretation_version == TIME_INTERPRETATION_V2
    assert read.evidence_eligible is True
    assert "timezone_name_undeclared" in read.caveats

    other = interpret_record_time(
        _payload(written_at="2026-10-09T06:50:03+00:00", written_at_timezone="UTC"),
        expected_timezone="Asia/Shanghai",
    )
    assert any(item.startswith("timezone_not_expected") for item in other.caveats)
    # "不是本项目期望的时区" 不等于 "这条记录说谎"：资格保留，只留 caveat。
    assert other.evidence_eligible is True


def test_read_trace_annotates_both_generations_without_rewriting(tmp_path: Path) -> None:
    identity = ModelIdentity(
        model_id="trend-tail-lgbm-1", artifact_content_hash="sha256:deadbeef",
        training_commit="eb691ed", runtime_commit="eb691ed", feature_compute_version=5,
        label_policy_id="label_policy_v4_abc",
        contract_digest=DEFAULT_TREND_CONTRACT.digest(),
    )
    stage = record_stage(
        stage="hard_eligibility", kind=KIND_HARD_GATE,
        input_symbols=["600000.SH", "600001.SH", "600002.SH"],
        advanced_symbols=["600000.SH", "600001.SH"],
        rejected={"low_liquidity": ["600002.SH"]},
        data_as_of="2026-10-09T14:50:00", contract=DEFAULT_TREND_CONTRACT,
        model_identity=identity, feature_compute_version=5,
        label_policy_id="label_policy_v4_abc", features_used=["avg_turnover_20"],
    )
    fresh = write_trace(build_funnel_trace(
        trade_date=datetime(2026, 10, 9).date(), stages=[stage],
        final_recommendations=[], rejected_final=[], contract=DEFAULT_TREND_CONTRACT,
    ), tmp_path)
    payload = json.loads(fresh.read_text(encoding="utf-8"))
    # 修复前的留档长这样：裸时间戳、没有时区声明。
    payload.update(written_at="2026-10-09T14:50:03", written_at_timezone="")
    legacy = tmp_path / "funnel_trace_legacy.json"
    legacy.write_text(json.dumps(payload), encoding="utf-8")

    assert read_trace(fresh)["time_interpretation"]["interpretation_version"] \
        == TIME_INTERPRETATION_V2
    assert read_trace(fresh)["time_interpretation"]["evidence_eligible"] is True
    old_read = read_trace(legacy)
    assert old_read["written_at"] == "2026-10-09T14:50:03"
    assert old_read["time_interpretation"]["interpretation_version"] == TIME_INTERPRETATION_V1
    assert old_read["time_interpretation"]["caveats"] == [LEGACY_TIME_CAVEAT]

