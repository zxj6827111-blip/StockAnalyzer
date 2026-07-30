"""Build the read-only local-vendor annual ZIP index."""

from __future__ import annotations

import argparse
import json

from stock_analyzer.data.vendor_zip_overlay import write_vendor_zip_daily_index


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index annual vendor daily ZIP archives without extracting them."
    )
    parser.add_argument("--root", required=True, help="Root containing the vendor history folders.")
    parser.add_argument(
        "--daily-dir",
        default="全A日K",
        help="Daily archive directory under --root.",
    )
    parser.add_argument("--output", required=True, help="Output JSON index path.")
    args = parser.parse_args()

    payload = write_vendor_zip_daily_index(
        root=args.root,
        daily_dir_name=args.daily_dir,
        output_path=args.output,
    )
    summary = {
        "status": "ok",
        "output": args.output,
        "symbols_total": payload.get("symbols_total", 0),
        "archives_total": payload.get("archives_total", 0),
        "ignored_duplicate_archives": payload.get("ignored_duplicate_archives", []),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
