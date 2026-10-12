from __future__ import annotations

import json

from scripts.audit_tail_field_pit import main


def test_inventory_mode_reports_missing_evidence_as_blocked(tmp_path) -> None:
    output = tmp_path / "inventory.json"

    result = main(["--scope", "stage2", "--out", str(output)])

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 3
    assert report["status"] == "blocked"
    assert report["pit_evidence_checks_passed"] is False
    assert report["release_eligible"] is False
    assert report["inventory"]["field_count"] > 0
    assert report["records"] == 0
    assert len(report["fields"]) == report["inventory"]["field_count"]
    assert all(field["status"] == "blocked" for field in report["fields"].values())

    # Evidence reports are immutable by default; reruns need a new output path.
    assert main(["--scope", "stage2", "--out", str(output)]) == 5


def test_evidence_and_input_arguments_must_be_paired(tmp_path) -> None:
    output = tmp_path / "invalid.json"
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")

    result = main(["--evidence", str(evidence), "--out", str(output)])

    assert result == 5
    assert not output.exists()
