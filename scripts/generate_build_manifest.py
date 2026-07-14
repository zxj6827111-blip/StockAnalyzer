from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the immutable StockAnalyzer build manifest."
    )
    parser.add_argument("--output", default="build_manifest.json")
    parser.add_argument("--commit", default="")
    parser.add_argument("--short-commit", default="")
    parser.add_argument("--dirty", default="")
    parser.add_argument("--built-at-utc", default="")
    args = parser.parse_args()

    commit = args.commit.strip() or _git("rev-parse", "HEAD") or "unknown"
    short_commit = args.short_commit.strip() or (
        _git("rev-parse", "--short=12", "HEAD") if commit != "unknown" else "unknown"
    )
    dirty = args.dirty.strip()
    if not dirty:
        dirty = "true" if _git("status", "--porcelain") else "false"
    payload = {
        "commit": commit,
        "short_commit": short_commit or commit[:12],
        "dirty": dirty.lower() in {"1", "true", "yes"},
        "built_at_utc": args.built_at_utc.strip() or datetime.now(UTC).isoformat(),
        "config_schema": "stock-analyzer-config.v1",
        "runtime_state_schema": 9,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, check=False, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


if __name__ == "__main__":
    raise SystemExit(main())
