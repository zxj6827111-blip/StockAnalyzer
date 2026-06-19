from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_p1_nas_rebuild_wrapper_forces_advisory_compose_override() -> None:
    script = (REPO_ROOT / "scripts" / "p1_nas_rebuild_and_collect.sh").read_text(
        encoding="utf-8",
    )

    assert "docker-compose.advisory.yml" in script
    assert "docker-compose.runtime.yml" in script
    assert "docker-compose.runtime.localvol.yml" in script
    assert "rsync -av --delete" in script
    assert "git rev-parse HEAD > .build_commit" in script
    assert "STOCK_ANALYZER_BUILD_COMMIT" in script
    assert "stock_analyzer_runtime_artifacts" in script
    assert "--runtime-state \"$runtime_state\"" in script
    assert "--model-artifact \"$model_artifact\"" in script
    assert "compose build api" in script
    assert "compose up -d --no-build --force-recreate api scheduler" in script
    assert "p1_capture_nas_environment.py" in script
    assert "p1_run_nas_advisory_collection.py" in script
    assert "p1_accept_nas_advisory_collection.py" in script
    assert "p1_audit_goal_completion.py" in script
    assert "--confirm-run" in script
    assert "runtime.get(\"advisory_only\") is not True" in script
    assert "runtime.get(\"training_enabled\") is not False" in script
    assert "collection will not start" in script
