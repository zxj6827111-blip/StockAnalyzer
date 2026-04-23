"""Run layered quality gates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    from stock_analyzer.ops.quality_gate import run_quality_gate

    parser = argparse.ArgumentParser(description="Run layered quality gates")
    parser.add_argument(
        "--stage",
        default="all",
        choices=("clean-scope", "smoke", "integration", "slow-report", "all"),
        help="Quality-gate stage to run",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Return exit code 2 when any blocking command fails",
    )
    args = parser.parse_args()

    report = run_quality_gate(args.stage, project_root=ROOT)
    print(report.to_json())
    if args.fail_on_error and not report.ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

