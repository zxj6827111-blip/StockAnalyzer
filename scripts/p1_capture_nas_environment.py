"""Capture NAS environment evidence for the P1 advisory collection audit."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture branch, commit and /health evidence. This is read-only and "
            "does not trigger pipeline or trading actions."
        ),
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:18001")
    parser.add_argument(
        "--output-dir",
        default="artifacts/research/p1_advisory_collection_quick_rerun",
    )
    parser.add_argument(
        "--expected-branch",
        default="codex/p1-shadow-calibration-data-quality",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = _path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_environment_report(
        api_base=args.api_base,
        expected_branch=args.expected_branch,
    )
    path = output_dir / "p1_nas_environment.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "path": str(path)}, ensure_ascii=False))
    if report["status"] != "pass":
        sys.exit(1)


def build_environment_report(*, api_base: str, expected_branch: str) -> dict[str, object]:
    branch = _git(["branch", "--show-current"])
    head = _git(["rev-parse", "HEAD"])
    remote_ref = f"origin/{expected_branch}"
    remote_head = _git(["rev-parse", remote_ref])
    health = _health(api_base)
    runtime = health.get("runtime") if isinstance(health.get("runtime"), dict) else {}
    checks = [
        {
            "code": "expected_branch_checked_out",
            "passed": branch == expected_branch,
            "detail": f"branch={branch}",
        },
        {
            "code": "head_matches_remote_branch",
            "passed": bool(head) and head == remote_head,
            "detail": f"head={head[:12]} remote={remote_head[:12]}",
        },
        {
            "code": "health_advisory_only",
            "passed": runtime.get("advisory_only") is True,
            "detail": f"runtime.advisory_only={runtime.get('advisory_only')}",
        },
        {
            "code": "health_training_disabled",
            "passed": runtime.get("training_enabled") is False,
            "detail": f"runtime.training_enabled={runtime.get('training_enabled')}",
        },
    ]
    return {
        "report_type": "p1_nas_environment",
        "status": "pass" if all(bool(item["passed"]) for item in checks) else "fail",
        "expected_branch": expected_branch,
        "branch": branch,
        "head": head,
        "remote_ref": remote_ref,
        "remote_head": remote_head,
        "api_base": api_base,
        "health": health,
        "checks": checks,
    }


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _git(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return completed.stdout.strip()


def _health(api_base: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(f"{api_base.rstrip('/')}/health", timeout=10) as response:
            value = json.load(response)
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"}
    return value if isinstance(value, dict) else {"raw": value}


if __name__ == "__main__":
    main()
