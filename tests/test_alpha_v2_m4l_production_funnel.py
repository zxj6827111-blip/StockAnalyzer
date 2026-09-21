"""M4-L / R1：Production Funnel Snapshot 契约测试（§27 + §8 + FUNNEL-1/2）。

覆盖：成员/名次提取、pinned 分离、source evidence（成员原文）与正式报告两条证据、
写入纪律（幂等 / 冲突拒绝 / linked 不可变）、捕获侧硬门（日期 / 契约 / 来源 /
selector_mode / 嵌套 / 计数 / 唯一性 / rank 自洽 / 源证据复算 / 报告 sha256 / 篡改）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from stock_analyzer.alpha_v2.validation.production_funnel import (
    FUNNEL_SCHEMA,
    FUNNEL_SOURCE,
    SOURCE_EVIDENCE_SCHEMA,
    FunnelNotFoundError,
    FunnelTamperError,
    FunnelVerificationError,
    build_source_evidence,
    emit_funnel_snapshot,
    extract_funnel_from_source_evidence,
    file_sha256,
    funnel_snapshot_hash,
    funnel_snapshot_path,
    link_funnel_to_report,
    load_funnel_snapshot,
    source_evidence_hash,
    source_evidence_path,
    verify_funnel_for_capture,
    write_source_evidence,
)

DAY = date(2026, 9, 21)
CONTRACT_ID = "night_alpha_v2_v1"


def _scan_report(
    *,
    quality: int = 5,
    light: int = 4,
    deep: int = 3,
    selector_mode: str = "quality",
    pinned: list[str] | None = None,
    deep_symbols: list[str] | None = None,
) -> dict[str, object]:
    """按 Week5SelectionEngine 报告形状造一份"生产夜扫结果"。"""
    quality_members = [{"symbol": f"q{i:03d}", "score": 100 - i} for i in range(quality)]
    light_members = [{"symbol": f"q{i:03d}", "baseline_score": 80 - i} for i in range(light)]
    if deep_symbols is None:
        deep_members = [
            {"symbol": f"q{i:03d}", "funnel_score": 70 - i} for i in range(deep)
        ]
    else:
        deep_members = [
            {"symbol": symbol, "funnel_score": 70 - i}
            for i, symbol in enumerate(deep_symbols)
        ]
    return {
        "funnel": {
            "policy": "snapshot_funnel",
            "deep_stage_ran": True,
            "deep_empty_reason": "",
            "selection_contract": {
                "selection_contract_id": CONTRACT_ID,
                "quality_target": 300,
                "light_target": 100,
                "deep_target": 50,
            },
        },
        "prefilter": {
            "universe_quality_selection": {
                "selector_mode": selector_mode,
                "selected": quality_members,
            },
            "shortlisted": light_members,
            "deep_stage": {"selected": deep_members},
            "pinned_symbols": list(pinned or []),
            "intraday_degraded": False,
        },
    }


def _evidence(tmp_path: Path, **kwargs: object) -> tuple[Path, dict[str, object]]:
    evidence = build_source_evidence(
        source_report=_scan_report(**kwargs),  # type: ignore[arg-type]
        trade_date=DAY.isoformat(),
        trace_id="week5-night-scan-20260921220000",
        created_at=f"{DAY.isoformat()}T22:05:00+08:00",
    )
    path = write_source_evidence(funnel_root=tmp_path, payload=evidence)
    return path, evidence


def _payload_from_evidence(evidence_path: Path, evidence: dict[str, object]) -> dict[str, object]:
    payload = extract_funnel_from_source_evidence(
        evidence,
        source_artifact_path=str(evidence_path),
        source_artifact_sha256=file_sha256(evidence_path),
    )
    payload["signal_date"] = DAY.isoformat()
    payload["trade_date"] = DAY.isoformat()
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    return payload


def _payload(tmp_path: Path, **kwargs: object) -> dict[str, object]:
    evidence_path, evidence = _evidence(tmp_path, **kwargs)
    return _payload_from_evidence(evidence_path, evidence)


def _emitted(tmp_path: Path, **kwargs: object) -> tuple[Path, dict[str, object]]:
    payload = _payload(tmp_path, **kwargs)
    path = emit_funnel_snapshot(funnel_root=tmp_path, payload=payload)
    return path, payload


def _formal_report(path: Path, *, report_id: str, trade_date: str = DAY.isoformat()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "report_id": report_id,
                "report_kind": "formal",
                "trade_date": trade_date,
                "scan_status": "completed",
            }
        ),
        encoding="utf-8",
    )
    return path


def _verified(payload: dict[str, object], tmp_path: Path, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "signal_date": DAY,
        "selection_contract_id": CONTRACT_ID,
        "require_linked_report": False,
        "report_root": None,
        "funnel_root": tmp_path,
    }
    kwargs.update(overrides)
    return verify_funnel_for_capture(payload, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 提取与写入
# ---------------------------------------------------------------------------


def test_source_evidence_captures_member_origin(tmp_path: Path):
    path, evidence = _evidence(tmp_path, quality=5, light=4, deep=3, pinned=["999999"])
    assert evidence["schema"] == SOURCE_EVIDENCE_SCHEMA
    assert len(evidence["quality_selected"]) == 5
    assert len(evidence["light_shortlisted"]) == 4
    assert len(evidence["deep_selected"]) == 3
    assert evidence["pinned_symbols"] == ["999999"]
    assert evidence["source_evidence_hash"] == source_evidence_hash(evidence)
    assert path == source_evidence_path(tmp_path, DAY)


def test_source_evidence_rejects_conflicting_same_day_content(tmp_path: Path):
    _evidence(tmp_path, deep=3)
    conflicting = build_source_evidence(
        source_report=_scan_report(deep=2),
        trade_date=DAY.isoformat(),
        trace_id="t2",
        created_at="t2",
    )
    with pytest.raises(FunnelTamperError, match="内容不同"):
        write_source_evidence(funnel_root=tmp_path, payload=conflicting)


def test_extract_members_ranks_and_pinned_separation(tmp_path: Path):
    payload = _payload(tmp_path, quality=5, light=4, deep=3, pinned=["999999", "600000"])
    assert payload["schema"] == FUNNEL_SCHEMA
    assert payload["source"] == FUNNEL_SOURCE
    assert payload["selection_contract_id"] == CONTRACT_ID
    assert [m["symbol"] for m in payload["deep_members"]] == ["q000", "q001", "q002"]
    assert [m["rank"] for m in payload["deep_members"]] == [1, 2, 3]
    assert (payload["quality_count"], payload["light_count"], payload["deep_count"]) == (5, 4, 3)
    assert payload["pinned_override_members"] == [{"symbol": "999999"}, {"symbol": "600000"}]
    assert all(
        m["symbol"] not in {"999999", "600000"}
        for m in payload["deep_members"] + payload["light_members"] + payload["quality_members"]
    )
    # 两条证据指针：源证据在 emit 时就有；报告身份要等 link
    assert payload["source_night_scan_artifact_sha256"]
    assert payload["published_report_id"] == ""


def test_emit_is_idempotent_for_same_content(tmp_path: Path):
    path, payload = _emitted(tmp_path)
    again = emit_funnel_snapshot(funnel_root=tmp_path, payload=dict(payload))
    assert again == path
    assert load_funnel_snapshot(path)["deep_count"] == 3


def test_emit_rejects_conflicting_same_day_content(tmp_path: Path):
    """同日两份成员不同的 funnel 必须拒绝（源证据层与漏斗层各有一次拦截）。"""
    _, payload = _emitted(tmp_path)
    conflicting = dict(payload)
    conflicting["deep_members"] = payload["deep_members"][:1]
    conflicting["deep_count"] = 1
    conflicting["funnel_snapshot_hash"] = funnel_snapshot_hash(conflicting)
    with pytest.raises(FunnelTamperError):
        emit_funnel_snapshot(funnel_root=tmp_path, payload=conflicting)
    # 换一天重新造证据（内容不同）同样被源证据层拒绝
    other_day = build_source_evidence(
        source_report=_scan_report(deep=2),
        trade_date=DAY.isoformat(),
        trace_id="t2",
        created_at="t2",
    )
    with pytest.raises(FunnelTamperError, match="内容不同"):
        write_source_evidence(funnel_root=tmp_path, payload=other_day)


def test_linked_funnel_is_immutable(tmp_path: Path):
    path, _ = _emitted(tmp_path)
    report_file = _formal_report(
        tmp_path / "nightly" / "nr-20260921-01.json", report_id="nr-20260921-01"
    )
    link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_file,
    )
    linked = load_funnel_snapshot(path)
    assert linked["published_report_id"] == "nr-20260921-01"
    assert linked["published_report_sha256"]
    with pytest.raises(FunnelTamperError, match="已链接"):
        emit_funnel_snapshot(funnel_root=tmp_path, payload=linked)


def test_link_rejects_semantically_mismatched_report(tmp_path: Path):
    """FUNNEL-2：report_id / trade_date / report_kind / scan_status 不符必须拒绝。"""
    _emitted(tmp_path)
    cases = [
        ("nr-other", DAY.isoformat(), "formal", "completed"),
        ("nr-20260921-01", "2026-09-20", "formal", "completed"),
        ("nr-20260921-01", DAY.isoformat(), "replay", "completed"),
        ("nr-20260921-01", DAY.isoformat(), "formal", "blocked"),
    ]
    for index, (report_id, trade_date, report_kind, scan_status) in enumerate(cases):
        report_file = tmp_path / f"report_{index}.json"
        report_file.write_text(
            json.dumps(
                {
                    "report_id": report_id,
                    "report_kind": report_kind,
                    "trade_date": trade_date,
                    "scan_status": scan_status,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(FunnelVerificationError, match="语义不符"):
            link_funnel_to_report(
                funnel_root=tmp_path,
                trade_date=DAY.isoformat(),
                report_id="nr-20260921-01",
                report_path=report_file,
            )


def test_link_requires_existing_artifact_and_rejects_relink(tmp_path: Path):
    with pytest.raises(FunnelNotFoundError):
        link_funnel_to_report(
            funnel_root=tmp_path,
            trade_date=DAY.isoformat(),
            report_id="nr-20260921-01",
            report_path=tmp_path / "missing.json",
        )
    path, _ = _emitted(tmp_path)
    report_a = _formal_report(tmp_path / "a.json", report_id="nr-20260921-01")
    link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_a,
    )
    assert (
        link_funnel_to_report(
            funnel_root=tmp_path,
            trade_date=DAY.isoformat(),
            report_id="nr-20260921-01",
            report_path=report_a,
        )
        == path
    )
    with pytest.raises(FunnelTamperError):
        link_funnel_to_report(
            funnel_root=tmp_path,
            trade_date=DAY.isoformat(),
            report_id="nr-20260921-02",
            report_path=report_a,
        )


# ---------------------------------------------------------------------------
# 捕获侧硬门
# ---------------------------------------------------------------------------


def test_verify_accepts_wellformed_funnel(tmp_path: Path):
    view = _verified(_payload(tmp_path), tmp_path)
    assert [m["symbol"] for m in view["deep_members"]] == ["q000", "q001", "q002"]
    assert view["deep_rank_by_symbol"]["q001"]["rank"] == 2
    assert view["source_night_scan_artifact_path"]


def test_verify_rejects_wrong_date(tmp_path: Path):
    payload = _payload(tmp_path)
    payload["trade_date"] = "2026-09-20"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="日期不符"):
        _verified(payload, tmp_path)


def test_verify_rejects_wrong_contract(tmp_path: Path):
    payload = _payload(tmp_path)
    payload["selection_contract_id"] = "legacy_profile_v1"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="selection_contract_id"):
        _verified(payload, tmp_path)


def test_verify_rejects_non_authoritative_selector_mode(tmp_path: Path):
    for index, mode in enumerate(("snapshot_fallback", "degraded_fallback")):
        case_dir = tmp_path / f"case_{index}"
        payload = _payload(case_dir, selector_mode=mode)
        with pytest.raises(FunnelVerificationError, match="selector_mode"):
            _verified(payload, case_dir)


def test_verify_rejects_non_production_source(tmp_path: Path):
    payload = _payload(tmp_path)
    payload["source"] = "research_proxy"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="source"):
        _verified(payload, tmp_path)


def test_funnel1_tampered_source_evidence_is_rejected(tmp_path: Path):
    """FUNNEL-1：改源证据里的 Deep 成员（不重算证据哈希）→ 捕获必须拒绝。"""
    payload = _payload(tmp_path)
    evidence_path = Path(str(payload["source_night_scan_artifact_path"]))
    raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    raw["deep_selected"] = raw["deep_selected"][:1]
    evidence_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FunnelVerificationError, match="源证据"):
        _verified(payload, tmp_path)


def test_tampered_source_evidence_with_recomputed_hash_is_still_rejected(tmp_path: Path):
    """连证据哈希一起重算也没用：funnel 与证据成员复算不一致。"""
    payload = _payload(tmp_path)
    evidence_path = Path(str(payload["source_night_scan_artifact_path"]))
    raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    raw["deep_selected"] = raw["deep_selected"][:1]
    raw["source_evidence_hash"] = source_evidence_hash(raw)
    evidence_path.write_text(json.dumps(raw), encoding="utf-8")
    # funnel 里记录的 sha 与文件不再一致 → 先被文件哈希拦下
    with pytest.raises(FunnelVerificationError, match="sha256"):
        _verified(payload, tmp_path)
    # 若把 funnel 里的 sha 也一起改（等于重签整条链），成员复算仍然拦住
    resigned = dict(payload)
    resigned["source_night_scan_artifact_sha256"] = str(raw["source_evidence_hash"])
    resigned["funnel_snapshot_hash"] = funnel_snapshot_hash(resigned)
    with pytest.raises(FunnelVerificationError, match="不一致"):
        _verified(resigned, tmp_path)


def test_verify_missing_source_pointer_is_rejected(tmp_path: Path):
    payload = _payload(tmp_path)
    payload["source_night_scan_artifact_path"] = ""
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="源证据指针"):
        _verified(payload, tmp_path)


def test_verify_rejects_broken_nesting(tmp_path: Path):
    payload = _payload(tmp_path, deep_symbols=["q003", "outside"])
    with pytest.raises(FunnelVerificationError, match="嵌套关系"):
        _verified(payload, tmp_path)


def test_verify_rejects_duplicate_symbols_and_bad_ranks(tmp_path: Path):
    """§8 契约补强：成员唯一 + rank 正整数/唯一/与顺序自洽。"""
    payload = _payload(tmp_path)
    duplicated = dict(payload)
    duplicated["deep_members"] = [
        dict(payload["deep_members"][0]),
        dict(payload["deep_members"][0]),
    ]
    duplicated["deep_count"] = 2
    duplicated["funnel_snapshot_hash"] = funnel_snapshot_hash(duplicated)
    with pytest.raises(FunnelVerificationError):
        _verified(duplicated, tmp_path)
    bad_rank = dict(payload)
    bad_rank["deep_members"] = [
        {**payload["deep_members"][0], "rank": 0},
        {**payload["deep_members"][1], "rank": 2},
    ]
    bad_rank["funnel_snapshot_hash"] = funnel_snapshot_hash(bad_rank)
    with pytest.raises(FunnelVerificationError):
        _verified(bad_rank, tmp_path)


def test_verify_rejects_count_mismatch_and_empty_deep(tmp_path: Path):
    payload = _payload(tmp_path)
    payload["deep_count"] = 2
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="deep_count"):
        _verified(payload, tmp_path)
    payload = _payload(tmp_path / "second", deep=0)
    with pytest.raises(FunnelVerificationError, match="deep_members 为空"):
        _verified(payload, tmp_path / "second")


def test_verify_requires_linked_report_in_production(tmp_path: Path):
    payload = _payload(tmp_path)
    with pytest.raises(FunnelVerificationError, match="未链接"):
        _verified(payload, tmp_path, require_linked_report=True)


def test_verify_checks_report_file_hash(tmp_path: Path):
    """Attack C：保留 report_id 但改报告内容——文件 sha256 必须抓到。"""
    path, _ = _emitted(tmp_path)
    report_file = _formal_report(
        tmp_path / "nightly" / "nr-20260921-01.json", report_id="nr-20260921-01"
    )
    link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_file,
    )
    linked = load_funnel_snapshot(path)
    assert _verified(
        linked, tmp_path, require_linked_report=True, report_root=tmp_path / "nightly"
    )["deep_members"]
    report_file.write_text(json.dumps({"report_id": "nr-20260921-01"}), encoding="utf-8")
    with pytest.raises(FunnelVerificationError, match="sha256"):
        _verified(linked, tmp_path, require_linked_report=True, report_root=tmp_path / "nightly")


def test_verify_detects_tampered_members(tmp_path: Path):
    """Attack C 变体：改成员列表但不重算 hash。"""
    path, _ = _emitted(tmp_path)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw["deep_members"] = raw["deep_members"][:1]
    raw["deep_count"] = 1
    Path(path).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FunnelVerificationError, match="funnel_snapshot_hash"):
        load_funnel_snapshot(path)


def test_missing_artifact_is_explicit_error(tmp_path: Path):
    with pytest.raises(FunnelNotFoundError):
        load_funnel_snapshot(funnel_snapshot_path(tmp_path, DAY))
