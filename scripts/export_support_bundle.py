from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    from stock_analyzer.ops.support_bundle import export_support_bundle

    parser = argparse.ArgumentParser(
        description="Export a NAS support bundle for remote diagnostics.",
    )
    parser.add_argument(
        "--mode",
        choices=("host", "container"),
        default="host",
        help="Collection namespace. Production NAS diagnostics should use host.",
    )
    parser.add_argument(
        "--output",
        default="artifacts/support/nas_support_bundle.json",
        help="Target JSON file path.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8001",
        help="Base URL of the runtime API.",
    )
    parser.add_argument(
        "--api-container",
        default="stock-analyzer-api",
        help="API container name.",
    )
    parser.add_argument(
        "--scheduler-container",
        default="stock-analyzer-scheduler",
        help="Scheduler container name.",
    )
    parser.add_argument(
        "--redis-container",
        default="stock-analyzer-redis",
        help="Redis container name.",
    )
    parser.add_argument(
        "--log-tail",
        type=int,
        default=120,
        help="How many recent log lines to keep for each container.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=5.0,
        help="HTTP timeout in seconds.",
    )
    args = parser.parse_args()

    output_path = export_support_bundle(
        project_root=ROOT,
        output_path=ROOT / args.output,
        base_url=args.base_url,
        api_container=args.api_container,
        scheduler_container=args.scheduler_container,
        redis_container=args.redis_container,
        log_tail=args.log_tail,
        timeout_sec=args.timeout_sec,
        mode=args.mode,
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
