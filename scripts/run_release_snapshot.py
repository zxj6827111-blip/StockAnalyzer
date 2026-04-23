"""Create or restore release snapshots."""

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
    from stock_analyzer.ops.release_snapshot import (
        create_release_snapshot,
        restore_release_snapshot,
    )

    parser = argparse.ArgumentParser(description="Create or restore release snapshots")
    subparsers = parser.add_subparsers(dest="action", required=True)

    create_parser = subparsers.add_parser("create", help="Create a release snapshot")
    create_parser.add_argument("--config", default="", help="Optional config path")
    create_parser.add_argument(
        "--snapshot-root",
        default="",
        help="Optional snapshot root directory",
    )

    restore_parser = subparsers.add_parser("restore", help="Restore a release snapshot")
    restore_parser.add_argument(
        "--snapshot-dir",
        required=True,
        help="Snapshot directory to restore",
    )
    restore_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only validate restore targets",
    )
    restore_parser.add_argument(
        "--no-backup-existing",
        action="store_true",
        help="Do not back up overwritten targets before restore",
    )

    args = parser.parse_args()
    if args.action == "create":
        config = get_config(args.config or None)
        report = create_release_snapshot(
            config=config,
            project_root=ROOT,
            snapshot_root=Path(args.snapshot_root) if args.snapshot_root else None,
            config_path=Path(args.config) if args.config else None,
        )
    else:
        report = restore_release_snapshot(
            snapshot_dir=Path(args.snapshot_dir),
            dry_run=bool(args.dry_run),
            backup_existing=not bool(args.no_backup_existing),
        )
    print(report.model_dump_json(indent=2))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
