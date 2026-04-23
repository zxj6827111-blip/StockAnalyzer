"""Run the staging rehearsal suite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    from stock_analyzer.config import get_config
    from stock_analyzer.ops.staging_rehearsal import run_staging_rehearsal

    parser = argparse.ArgumentParser(description="Run staging rehearsal")
    parser.add_argument("--config", default="", help="Optional config path")
    parser.add_argument("--smoke-port", type=int, default=8012, help="Port for local smoke")
    parser.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="Return exit code 2 when rehearsal is blocked",
    )
    args = parser.parse_args()

    config = get_config(args.config or None)
    report = run_staging_rehearsal(
        config,
        project_root=ROOT,
        config_path=Path(args.config) if args.config else None,
        smoke_port=args.smoke_port,
    )
    report_dir = ROOT / "artifacts" / "release" / "staging"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"staging_rehearsal_{report.generated_at:%Y%m%d_%H%M%S}.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(report.model_dump_json(indent=2))
    print(f"\nreport_path={report_path}")
    if args.fail_on_blocked and not report.ready:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
