#!/usr/bin/env python3
"""枚举选股链路字段，或只读审计与真实输入绑定的字段级 PIT 记录。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from stock_analyzer.research.tail_field_pit import (  # noqa: E402
    field_inventory,
    load_and_audit_field_pit,
    sha256_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("stage2", "full_chain"), default="full_chain")
    parser.add_argument("--evidence")
    parser.add_argument("--input")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    output = Path(args.out).expanduser()
    if output.exists():
        print("PIT audit refuses to overwrite existing output", file=sys.stderr)
        return 5
    try:
        if args.evidence and args.input:
            report = load_and_audit_field_pit(
                Path(args.evidence).expanduser(),
                expected_input_sha256=sha256_file(Path(args.input).expanduser()),
                scope=args.scope,
            )
        elif args.evidence or args.input:
            raise ValueError("--evidence and --input must be supplied together")
        else:
            inventory = field_inventory(args.scope)
            report = {
                "schema": "tail_field_pit_inventory.v1",
                "status": "blocked",
                "pit_evidence_checks_passed": False,
                "release_eligible": False,
                "records": 0,
                "source_files_hashed": 0,
                "blockers": {"field_level_evidence_not_supplied": 1},
                "fields": {
                    name: {
                        "status": "blocked",
                        "blockers": {"field_level_evidence_not_supplied": 1},
                    }
                    for name in inventory["fields"]
                },
                "inventory": inventory,
                "trust_boundary": (
                    "inventory_only_no_field_pit_observations; "
                    "no_signature_or_vendor_timestamp_attestation"
                ),
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"status": report["status"], "out": str(output)}, ensure_ascii=False))
        return 0 if report["pit_evidence_checks_passed"] else 3
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"field PIT audit blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
