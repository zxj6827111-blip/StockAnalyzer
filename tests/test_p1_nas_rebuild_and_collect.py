from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_p1_nas_rebuild_wrapper_forces_advisory_compose_override() -> None:
    script = (REPO_ROOT / "scripts" / "p1_nas_rebuild_and_collect.sh").read_text(
        encoding="utf-8",
    )

    assert "docker-compose.advisory.yml" in script
    assert "compose up -d --build api scheduler" in script
    assert "p1_run_nas_advisory_collection.py" in script
    assert "--confirm-run" in script
    assert "runtime.get(\"advisory_only\") is not True" in script
    assert "runtime.get(\"training_enabled\") is not False" in script
    assert "collection will not start" in script
