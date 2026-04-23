"""Run release smoke API checks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    from stock_analyzer.ops.release_smoke import run_smoke_api

    parser = argparse.ArgumentParser(description="Run release smoke API checks")
    parser.add_argument("--base-url", default="", help="Existing base URL to probe")
    parser.add_argument("--host", default="127.0.0.1", help="Host for spawned uvicorn")
    parser.add_argument("--port", type=int, default=8011, help="Port for spawned uvicorn")
    parser.add_argument(
        "--no-start-server",
        action="store_true",
        help="Use an existing base URL instead of starting uvicorn",
    )
    parser.add_argument("--skip-ui", action="store_true", help="Skip /ui smoke")
    parser.add_argument(
        "--skip-write-checks",
        action="store_true",
        help="Skip POST smoke endpoints",
    )
    parser.add_argument(
        "--fail-on-failure",
        action="store_true",
        help="Return exit code 2 when any smoke check fails",
    )
    args = parser.parse_args()

    report = run_smoke_api(
        base_url=args.base_url or None,
        project_root=ROOT,
        host=args.host,
        port=args.port,
        start_server=not args.no_start_server,
        include_ui=not args.skip_ui,
        include_write_checks=not args.skip_write_checks,
    )
    print(report.model_dump_json(indent=2))
    if args.fail_on_failure and not report.ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
