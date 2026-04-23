from __future__ import annotations

from pathlib import Path


def test_v13_operational_docs_and_gate_script_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    required_files = [
        root / "docs" / "v13_deployment_guide.md",
        root / "docs" / "nas_support_bundle.md",
        root / "docs" / "v13_training_and_acceptance.md",
        root / "docs" / "v13_runtime_operations.md",
        root / "docs" / "pre_release_checklist.md",
        root / "docs" / "rollback_checklist.md",
        root / "scripts" / "run_acceptance_release_gate.ps1",
    ]

    for path in required_files:
        assert path.exists() is True


def test_v13_operational_docs_reference_release_gate_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    deployment = (root / "docs" / "v13_deployment_guide.md").read_text(encoding="utf-8")
    checklist = (root / "docs" / "pre_release_checklist.md").read_text(encoding="utf-8")
    support_bundle = (root / "docs" / "nas_support_bundle.md").read_text(encoding="utf-8")

    assert "acceptance-release-gate" in deployment
    assert "run_acceptance_release_gate.ps1" in deployment
    assert "export_support_bundle.py" in deployment
    assert "acceptance-release-gate" in checklist
    assert "export_support_bundle.py" in support_bundle
