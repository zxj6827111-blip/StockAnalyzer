from __future__ import annotations

from scripts import p1_capture_nas_environment as env_capture


def test_environment_report_passes_when_branch_head_and_health_match(
    monkeypatch,
) -> None:
    def _fake_git(args: list[str]) -> str:
        if args == ["branch", "--show-current"]:
            return "codex/p1-shadow-calibration-data-quality"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        if args == ["rev-parse", "origin/codex/p1-shadow-calibration-data-quality"]:
            return "abc123"
        raise AssertionError(args)

    monkeypatch.setattr(env_capture, "_git", _fake_git)
    monkeypatch.setattr(
        env_capture,
        "_health",
        lambda api_base: {"runtime": {"advisory_only": True, "training_enabled": False}},
    )

    report = env_capture.build_environment_report(
        api_base="http://127.0.0.1:18001",
        expected_branch="codex/p1-shadow-calibration-data-quality",
    )

    assert report["status"] == "pass"
    assert all(item["passed"] for item in report["checks"])


def test_environment_report_fails_when_health_is_not_advisory(monkeypatch) -> None:
    monkeypatch.setattr(
        env_capture,
        "_git",
        lambda args: "codex/p1-shadow-calibration-data-quality"
        if args == ["branch", "--show-current"]
        else "abc123",
    )
    monkeypatch.setattr(
        env_capture,
        "_health",
        lambda api_base: {"runtime": {"advisory_only": False, "training_enabled": True}},
    )

    report = env_capture.build_environment_report(
        api_base="http://127.0.0.1:18001",
        expected_branch="codex/p1-shadow-calibration-data-quality",
    )

    assert report["status"] == "fail"
    failed_codes = {item["code"] for item in report["checks"] if not item["passed"]}
    assert "health_advisory_only" in failed_codes
    assert "health_training_disabled" in failed_codes
