"""Run release preflight checks."""

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
    from stock_analyzer.ops.release_preflight import run_release_preflight

    parser = argparse.ArgumentParser(description="Run environment-level release preflight")
    parser.add_argument("--config", default="", help="Optional config path")
    parser.add_argument("--bind-port", type=int, default=8010, help="Port to probe for startup")
    parser.add_argument("--bind-host", default="127.0.0.1", help="Host to probe for startup")
    parser.add_argument(
        "--fail-on-not-ready",
        action="store_true",
        help="Return exit code 2 when blockers exist",
    )
    args = parser.parse_args()

    config = get_config(args.config or None)
    report = run_release_preflight(
        config,
        project_root=ROOT,
        config_path=Path(args.config) if args.config else None,
        bind_host=args.bind_host,
        bind_port=args.bind_port,
    )
    print(report.model_dump_json(indent=2))
    if args.fail_on_not_ready and not report.ready:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
