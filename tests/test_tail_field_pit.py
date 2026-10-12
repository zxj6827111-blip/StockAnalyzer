from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stock_analyzer.research import tail_field_pit
from stock_analyzer.research.tail_field_pit import (
    SCHEMA,
    audit_field_pit,
    field_inventory,
    load_and_audit_field_pit,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_payload(tmp_path: Path, monkeypatch, *, scope: str = "full_chain") -> tuple[dict, str]:
    source = tmp_path / "source.json"
    snapshot = tmp_path / "snapshot.json"
    source.write_text('{"source":"vendor-response"}\n', encoding="utf-8")
    snapshot.write_text('{"rows":1}\n', encoding="utf-8")
    inventory = {
        "scope": scope,
        "fields": ["price", "suspended"],
        "field_count": 2,
        "sha256": "b" * 64,
    }
    monkeypatch.setattr(
        tail_field_pit, "field_inventory", lambda requested="full_chain": inventory.copy()
    )
    sha = (
        _sha(tmp_path / "confirmation.jsonl")
        if (tmp_path / "confirmation.jsonl").exists()
        else "a" * 64
    )
    source_ref = {
        "path": "source.json",
        "sha256": _sha(source),
        "version": "vendor-v1",
        "available_at": "2026-01-02T14:50:00+08:00",
        "captured_at": "2026-01-02T14:51:00+08:00",
    }
    fields = {
        name: {
            "input_rows": 1,
            "missing_inputs": 0,
            "unknown_imputations": 0,
            "latest_input_at": "2026-01-02T14:45:00+08:00",
            "computed_at": "2026-01-02T14:55:00+08:00",
            "sources": [source_ref],
        }
        for name in inventory["fields"]
    }
    return (
        {
            "schema": SCHEMA,
            "scope": scope,
            "input_sha256": sha,
            "inventory_sha256": inventory["sha256"],
            "records": [
                {
                    "trade_date": "2026-01-02",
                    "arm_id": "candidate",
                    "capture_mode": "observed_snapshot",
                    "decision_at": "2026-01-02T15:00:00+08:00",
                    "input_symbol_count": 1,
                    "input_snapshot": {"path": "snapshot.json", "sha256": _sha(snapshot)},
                    "fields": fields,
                }
            ],
        },
        sha,
    )


def _audit(tmp_path: Path, payload: dict, sha: str | None = None) -> dict:
    return audit_field_pit(
        payload,
        asset_root=tmp_path,
        expected_input_sha256=sha or payload["input_sha256"],
        expected_sessions=[("2026-01-02", "candidate")],
    )


def test_complete_field_pit_is_audited_but_never_grants_release(
    tmp_path: Path, monkeypatch
) -> None:
    payload, sha = _valid_payload(tmp_path, monkeypatch)

    report = _audit(tmp_path, payload, sha)

    assert report["status"] == "passed"
    assert report["pit_evidence_checks_passed"] is True
    assert report["release_eligible"] is False
    assert report["fields"]["price"]["status"] == "passed"
    assert report["source_files_hashed"] == 2


def test_inventory_return_is_defensive_copy() -> None:
    first = field_inventory("stage2")
    original_hash = first["sha256"]
    first["fields"].clear()
    first["roles"].clear()

    second = field_inventory("stage2")
    assert second["fields"]
    assert second["sha256"] == original_hash


def test_input_and_inventory_binding_fail_closed(tmp_path: Path, monkeypatch) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)

    wrong_input = _audit(tmp_path, payload, "c" * 64)
    assert wrong_input["status"] == "blocked"
    assert "pit_evidence_input_hash_mismatch" in wrong_input["blockers"]
    assert all(item["status"] == "blocked" for item in wrong_input["fields"].values())

    payload["inventory_sha256"] = "d" * 64
    wrong_inventory = _audit(tmp_path, payload)
    assert "pit_field_inventory_mismatch" in wrong_inventory["blockers"]


def test_missing_rows_and_unknown_imputation_block_field(tmp_path: Path, monkeypatch) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    payload["records"][0]["fields"]["price"]["missing_inputs"] = 1
    payload["records"][0]["fields"]["suspended"]["unknown_imputations"] = True

    report = _audit(tmp_path, payload)

    assert report["status"] == "blocked"
    assert report["fields"]["price"]["blockers"]["missing_inputs"] == 1
    assert report["fields"]["suspended"]["blockers"]["unknown_imputations"] == 1


def test_future_or_timezone_free_clocks_block_field(tmp_path: Path, monkeypatch) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    payload["records"][0]["fields"]["price"]["computed_at"] = "2026-01-02T15:01:00+08:00"
    payload["records"][0]["fields"]["suspended"]["sources"][0]["captured_at"] = (
        "2026-01-02T14:52:00"
    )

    report = _audit(tmp_path, payload)

    assert "field_clock_after_decision_or_out_of_order" in report["fields"]["price"]["blockers"]
    assert "field_source_clock_invalid" in report["fields"]["suspended"]["blockers"]


def test_partial_coverage_duplicates_replay_and_path_escape_block(
    tmp_path: Path, monkeypatch
) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    payload["records"][0]["fields"].pop("price")
    payload["records"][0]["capture_mode"] = "replayed_recompute"
    payload["records"][0]["input_snapshot"]["path"] = "../outside.json"
    payload["records"].append(payload["records"][0].copy())

    report = _audit(tmp_path, payload)

    assert "pit_duplicate_arm_session" in report["blockers"]
    assert "pit_source_not_observed_snapshot" in report["blockers"]
    assert "pit_input_snapshot_hash_invalid" in report["blockers"]
    assert report["fields"]["price"]["status"] == "blocked"


@pytest.mark.parametrize(
    ("invalid_record_change", "expected_blocker"),
    [
        ("capture_mode", "pit_source_not_observed_snapshot"),
        ("input_symbol_count", "pit_input_universe_count_invalid"),
        ("input_snapshot", "pit_input_snapshot_hash_invalid"),
    ],
)
def test_session_level_pit_failure_blocks_every_field(
    tmp_path: Path, monkeypatch, invalid_record_change: str, expected_blocker: str
) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    record = payload["records"][0]
    if invalid_record_change == "capture_mode":
        record["capture_mode"] = "replayed_recompute"
    elif invalid_record_change == "input_symbol_count":
        record["input_symbol_count"] = 0
        for value in record["fields"].values():
            value["input_rows"] = 0
    else:
        record["input_snapshot"]["path"] = "../missing.json"

    report = _audit(tmp_path, payload)

    assert expected_blocker in report["blockers"]
    assert all(item["status"] == "blocked" for item in report["fields"].values())


def test_expected_arm_session_coverage_is_exact(tmp_path: Path, monkeypatch) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    report = audit_field_pit(
        payload,
        asset_root=tmp_path,
        expected_input_sha256=payload["input_sha256"],
        expected_sessions=[("2026-01-02", "candidate"), ("2026-01-02", "legacy")],
    )
    assert "pit_arm_session_coverage_mismatch" in report["blockers"]


def test_assets_must_not_change_during_audit(tmp_path: Path, monkeypatch) -> None:
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    original = tail_field_pit._stat_identity
    changed = False

    def mutate_after_first_hash(path: Path):
        nonlocal changed
        identity = original(path)
        if path.name == "source.json" and not changed:
            changed = True
            path.write_text('{"source":"changed"}\n', encoding="utf-8")
        return identity

    monkeypatch.setattr(tail_field_pit, "_stat_identity", mutate_after_first_hash)
    report = _audit(tmp_path, payload)
    assert "pit_source_changed_during_audit" in report["blockers"]


def test_pit_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    evidence = tmp_path / "duplicate.json"
    evidence.write_text('{"schema":"old","schema":"tail_field_pit_evidence.v1"}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key: schema"):
        load_and_audit_field_pit(evidence, expected_input_sha256="a" * 64)
