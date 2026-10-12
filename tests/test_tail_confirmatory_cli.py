import hashlib
import json
from datetime import date, timedelta

from scripts import validate_tail_independent_confirmation as confirmation_cli
from scripts.validate_tail_independent_confirmation import main
from stock_analyzer.research import tail_field_pit


def _write_rows(path, rows) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_cli_requires_field_pit_and_only_releases_after_all_gates(tmp_path, monkeypatch) -> None:
    development = []
    for index in range(20):
        day = date(2026, 1, 1) + timedelta(days=index)
        for arm in ("candidate", "legacy_same_pool"):
            development.append(
                {
                    "trade_date": day.isoformat(),
                    "arm_id": arm,
                    "fold_id": 0,
                    "session_index": index,
                    "net_returns": [0.02, -0.01] if arm == "candidate" else [0.01, -0.01],
                    "account_daily_return": 0.001,
                    "capture_mode": "replayed_recompute",
                }
            )
    development_path = tmp_path / "development.jsonl"
    _write_rows(development_path, development)

    confirmation = []
    calendar = []
    for index in range(120):
        day = date(2026, 10, 11) + timedelta(days=index)
        calendar.append({"trade_date": day.isoformat(), "is_open": 1})
        fold_id, session_index = divmod(index, 30)
        for arm in ("candidate", "same_day_style", "legacy_same_pool", "legacy_full_policy"):
            confirmation.append(
                {
                    "trade_date": day.isoformat(),
                    "arm_id": arm,
                    "fold_id": fold_id,
                    "session_index": session_index,
                    "net_returns": [0.02, 0.01, -0.01],
                    "account_daily_return": 0.001,
                    "capture_mode": "replayed_recompute",
                }
            )
    confirmation_path = tmp_path / "confirmation.jsonl"
    _write_rows(confirmation_path, confirmation)
    calendar_path = tmp_path / "calendar.csv"
    calendar_path.write_text(
        "trade_date,is_open\n"
        + "".join(f"{item['trade_date']},{item['is_open']}\n" for item in calendar),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    result = main(
        [
            "--development-sessions",
            str(development_path),
            "--power-control-arm",
            "legacy_same_pool",
            "--confirmation-sessions",
            str(confirmation_path),
            "--trading-calendar",
            str(calendar_path),
            "--candidate-frozen-date",
            "2026-10-10",
            "--candidate-artifact-sha256",
            "a" * 64,
            "--draws",
            "20",
            "--out",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 3
    assert report["state"] == "engineering_blocked"
    assert "independent_window_not_attested_unopened" in report["blockers"]
    assert "field_level_pit_evidence_missing" in report["blockers"]
    assert (
        len(report["field_pit_audit"]["fields"])
        == report["field_pit_audit"]["inventory"]["field_count"]
    )
    assert all(item["status"] == "blocked" for item in report["field_pit_audit"]["fields"].values())
    assert report["inputs"]["confirmation_sessions"]["sha256"]

    # A human window attestation cannot replace field-level PIT evidence.
    no_pit_attested = tmp_path / "no_pit_attested.json"
    args = [
        "--development-sessions",
        str(development_path),
        "--power-control-arm",
        "legacy_same_pool",
        "--confirmation-sessions",
        str(confirmation_path),
        "--trading-calendar",
        str(calendar_path),
        "--candidate-frozen-date",
        "2026-10-10",
        "--candidate-artifact-sha256",
        "a" * 64,
        "--unopened-window-attested",
        "--draws",
        "20",
        "--out",
        str(no_pit_attested),
    ]
    assert main(args) == 3
    attested_report = json.loads(no_pit_attested.read_text())
    assert "field_level_pit_evidence_missing" in attested_report["blockers"]
    assert "confirmation_sessions_not_observed_snapshot" in attested_report["blockers"]

    # Replace only the fixture capture mode, then provide a source-bound PIT bundle.
    for row in confirmation:
        row["capture_mode"] = "observed_snapshot"
    _write_rows(confirmation_path, confirmation)
    pit_dir = tmp_path / "pit_bundle"
    pit_dir.mkdir()
    snapshot = pit_dir / "confirmation_snapshot.jsonl"
    snapshot.write_bytes(confirmation_path.read_bytes())
    source = pit_dir / "vendor_source.json"
    source.write_text('{"response":"captured"}\n', encoding="utf-8")
    inventory = {"scope": "full_chain", "fields": ["price"], "field_count": 1, "sha256": "b" * 64}
    monkeypatch.setattr(tail_field_pit, "field_inventory", lambda scope="full_chain": inventory)
    monkeypatch.setattr(
        confirmation_cli,
        "evaluate_independent_confirmation",
        lambda **kwargs: {
            "state": "quality_passed",
            "passed": True,
            "release_eligible": False,
            "tests": {"mocked": {"passed": True}},
        },
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    pit_records = []
    for row in confirmation:
        day = row["trade_date"]
        pit_records.append(
            {
                "trade_date": day,
                "arm_id": row["arm_id"],
                "capture_mode": "observed_snapshot",
                "decision_at": f"{day}T15:00:00+08:00",
                "input_symbol_count": 1,
                "input_snapshot": {"path": snapshot.name, "sha256": snapshot_sha},
                "fields": {
                    "price": {
                        "input_rows": 1,
                        "missing_inputs": 0,
                        "unknown_imputations": 0,
                        "latest_input_at": f"{day}T14:45:00+08:00",
                        "computed_at": f"{day}T14:55:00+08:00",
                        "sources": [
                            {
                                "path": source.name,
                                "sha256": source_sha,
                                "version": "test-v1",
                                "available_at": f"{day}T14:50:00+08:00",
                                "captured_at": f"{day}T14:51:00+08:00",
                            }
                        ],
                    }
                },
            }
        )
    pit_evidence = pit_dir / "pit_evidence.json"
    pit_evidence.write_text(
        json.dumps(
            {
                "schema": "tail_field_pit_evidence.v1",
                "scope": "full_chain",
                "input_sha256": hashlib.sha256(confirmation_path.read_bytes()).hexdigest(),
                "inventory_sha256": inventory["sha256"],
                "records": pit_records,
            }
        ),
        encoding="utf-8",
    )
    passed_path = tmp_path / "gated_pass.json"
    assert main([*args[:-2], "--pit-evidence", str(pit_evidence), "--out", str(passed_path)]) == 0
    passed_report = json.loads(passed_path.read_text())
    assert passed_report["field_pit_audit"]["pit_evidence_checks_passed"] is True
    assert passed_report["state"] == "quality_passed"
    assert passed_report["release_eligible"] is True
