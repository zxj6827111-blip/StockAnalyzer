#!/usr/bin/env python3
"""Dry-run, migrate, or roll back a runtime_state.json v9 migration."""

from __future__ import annotations

import argparse
import json

from stock_analyzer.runtime.state_v9 import (
    migrate_runtime_state_v9,
    rollback_runtime_state_v9,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--backup-path", default="")
    args = parser.parse_args()
    if args.rollback:
        result = rollback_runtime_state_v9(
            args.state_path,
            backup_path=args.backup_path or None,
            dry_run=args.dry_run,
        )
    else:
        result = migrate_runtime_state_v9(args.state_path, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
