"""M4-L：Production Funnel Snapshot 契约测试（§27 专项）。

覆盖：成员/名次提取、pinned 分离、写入纪律（幂等 / 冲突拒绝 / linked 不可变）、
捕获侧硬门（日期 / 契约 / 来源 / selector_mode / 嵌套 / 计数 / 未链接 / 篡改）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from stock_analyzer.alpha_v2.validation.production_funnel import (
    FUNNEL_SCHEMA,
    FUNNEL_SOURCE,
    FunnelNotFoundError,
    FunnelTamperError,
    FunnelVerificationError,
    emit_funnel_snapshot,
    extract_funnel_from_scan_report,
    funnel_snapshot_hash,
    funnel_snapshot_path,
    link_funnel_to_report,
    load_funnel_snapshot,
    verify_funnel_for_capture,
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
    light_members = [
        {"symbol": f"q{i:03d}", "baseline_score": 80 - i} for i in range(light)
    ]
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


def _payload(**kwargs: object) -> dict[str, object]:
    payload = extract_funnel_from_scan_report(
        source_report=_scan_report(**kwargs),  # type: ignore[arg-type]
        trace_id="week5-night-scan-20260921220000",
        scan_status="night_scan_completed",
        created_at=f"{DAY.isoformat()}T22:05:00+08:00",
    )
    payload["signal_date"] = DAY.isoformat()
    payload["trade_date"] = DAY.isoformat()
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    return payload


def _emitted(tmp_path: Path, **kwargs: object) -> tuple[Path, dict[str, object]]:
    payload = _payload(**kwargs)
    path = emit_funnel_snapshot(funnel_root=tmp_path, payload=payload)
    return path, payload


def _verified(payload: dict[str, object], **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "signal_date": DAY,
        "selection_contract_id": CONTRACT_ID,
        "require_linked_report": False,
        "report_root": None,
    }
    kwargs.update(overrides)
    return verify_funnel_for_capture(payload, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 提取与写入
# ---------------------------------------------------------------------------


def test_extract_members_ranks_and_pinned_separation():
    payload = _payload(quality=5, light=4, deep=3, pinned=["999999", "600000"])
    assert payload["schema"] == FUNNEL_SCHEMA
    assert payload["source"] == FUNNEL_SOURCE
    assert payload["selection_contract_id"] == CONTRACT_ID
    assert [m["symbol"] for m in payload["deep_members"]] == ["q000", "q001", "q002"]
    assert [m["rank"] for m in payload["deep_members"]] == [1, 2, 3]
    assert (payload["quality_count"], payload["light_count"], payload["deep_count"]) == (5, 4, 3)
    # pinned 单独成列，绝不进成员列表（§6）
    assert payload["pinned_override_members"] == [{"symbol": "999999"}, {"symbol": "600000"}]
    assert all(
        m["symbol"] not in {"999999", "600000"}
        for m in payload["deep_members"] + payload["light_members"] + payload["quality_members"]
    )


def test_emit_is_idempotent_for_same_content(tmp_path: Path):
    path, payload = _emitted(tmp_path)
    again = emit_funnel_snapshot(funnel_root=tmp_path, payload=dict(payload))
    assert again == path
    assert load_funnel_snapshot(path)["deep_count"] == 3


def test_emit_rejects_conflicting_same_day_content(tmp_path: Path):
    """同一天两份互斥的"真实漏斗"是事故，不是新证据。"""
    _emitted(tmp_path)
    conflicting = _payload(deep=2)
    with pytest.raises(FunnelTamperError, match="实质内容不同"):
        emit_funnel_snapshot(funnel_root=tmp_path, payload=conflicting)


def test_linked_funnel_is_immutable(tmp_path: Path):
    path, _ = _emitted(tmp_path)
    report_file = tmp_path / "nightly" / f"{DAY.isoformat()}" / "nr-20260921-01.json"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(json.dumps({"report_id": "nr-20260921-01"}), encoding="utf-8")
    link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_file,
    )
    linked = load_funnel_snapshot(path)
    assert linked["night_scan_report_id"] == "nr-20260921-01"
    assert linked["source_artifact_sha256"]
    # linked 之后再 emit 任何内容（哪怕相同）都必须拒绝
    with pytest.raises(FunnelTamperError, match="已链接"):
        emit_funnel_snapshot(funnel_root=tmp_path, payload=linked)


def test_link_requires_existing_artifact_and_rejects_relink(tmp_path: Path):
    with pytest.raises(FunnelNotFoundError):
        link_funnel_to_report(
            funnel_root=tmp_path,
            trade_date=DAY.isoformat(),
            report_id="nr-20260921-01",
            report_path=tmp_path / "missing.json",
        )
    path, _ = _emitted(tmp_path)
    report_a = tmp_path / "a.json"
    report_a.write_text("{}", encoding="utf-8")
    link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_a,
    )
    # 同 report_id 重复链接 = 幂等；换 report_id = 拒绝
    assert link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_a,
    ) == path
    with pytest.raises(FunnelTamperError):
        link_funnel_to_report(
            funnel_root=tmp_path,
            trade_date=DAY.isoformat(),
            report_id="nr-20260921-02",
            report_path=report_a,
        )


# ---------------------------------------------------------------------------
# 捕获侧硬门（§7/§27）
# ---------------------------------------------------------------------------


def test_verify_accepts_wellformed_funnel():
    view = _verified(_payload())
    assert [m["symbol"] for m in view["deep_members"]] == ["q000", "q001", "q002"]
    assert view["deep_rank_by_symbol"]["q001"]["rank"] == 2


def test_verify_rejects_wrong_date():
    payload = _payload()
    payload["trade_date"] = "2026-09-20"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="日期不符"):
        _verified(payload)


def test_verify_rejects_wrong_contract():
    payload = _payload()
    payload["selection_contract_id"] = "legacy_profile_v1"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="selection_contract_id"):
        _verified(payload)


def test_verify_rejects_non_authoritative_selector_mode():
    payload = _payload(selector_mode="snapshot_fallback")
    with pytest.raises(FunnelVerificationError, match="selector_mode"):
        _verified(payload)
    payload = _payload(selector_mode="degraded_fallback")
    with pytest.raises(FunnelVerificationError, match="selector_mode"):
        _verified(payload)


def test_verify_rejects_non_production_source():
    payload = _payload()
    payload["source"] = "research_proxy"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="source"):
        _verified(payload)


def test_verify_rejects_broken_nesting():
    """deep 成员不在 light/quality 里 = 成员资格被伪造（§7）。"""
    payload = _payload(deep_symbols=["q003", "outside"])  # outside 不在任何池里
    with pytest.raises(FunnelVerificationError, match="嵌套关系"):
        _verified(payload)


def test_verify_rejects_count_mismatch_and_empty_deep():
    payload = _payload()
    payload["deep_count"] = 2  # 与 3 个成员不符
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    with pytest.raises(FunnelVerificationError, match="deep_count"):
        _verified(payload)
    payload = _payload(deep=0)
    with pytest.raises(FunnelVerificationError, match="deep_members 为空"):
        _verified(payload)


def test_verify_requires_linked_report_in_production():
    payload = _payload()
    with pytest.raises(FunnelVerificationError, match="未链接"):
        _verified(payload, require_linked_report=True)


def test_verify_checks_report_file_hash(tmp_path: Path):
    """Attack C：保留 report_id 但改报告内容——文件 sha256 必须抓到。"""
    path, _ = _emitted(tmp_path)
    report_file = tmp_path / "nightly" / "nr-20260921-01.json"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(json.dumps({"v": 1}), encoding="utf-8")
    link_funnel_to_report(
        funnel_root=tmp_path,
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_path=report_file,
    )
    linked = load_funnel_snapshot(path)
    assert (
        _verified(
            linked,
            require_linked_report=True,
            report_root=tmp_path / "nightly",
        )["deep_members"]
    )
    # 事后改报告文件内容 → 哈希不符
    report_file.write_text(json.dumps({"v": 2}), encoding="utf-8")
    with pytest.raises(FunnelVerificationError, match="sha256"):
        _verified(linked, require_linked_report=True, report_root=tmp_path / "nightly")


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
