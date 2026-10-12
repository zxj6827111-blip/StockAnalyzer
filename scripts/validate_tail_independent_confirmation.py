#!/usr/bin/env python3
"""Run the fixed four-arm confirmation protocol on frozen, field-audited inputs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_confirmatory import (  # noqa: E402
    ConfirmatorySession,
    estimate_development_power_sample_size,
    evaluate_independent_confirmation,
)
from stock_analyzer.research.tail_field_pit import (  # noqa: E402
    field_inventory,
    json_loads_unique,
    load_and_audit_field_pit,
    sha256_file,
)


def _load_sessions(path: Path) -> tuple[list[ConfirmatorySession], str]:
    rows: list[ConfirmatorySession] = []
    modes: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json_loads_unique(line)
        if not isinstance(payload, dict):
            raise ValueError(f"session line {line_number} must be a JSON object")
        mode = str(payload.get("capture_mode") or "").strip()
        if mode not in {"observed_snapshot", "replayed_recompute"}:
            raise ValueError(f"session line {line_number} has invalid capture_mode")
        modes.add(mode)
        returns = payload.get("net_returns")
        if not isinstance(returns, list):
            raise ValueError(f"session line {line_number} net_returns must be a list")
        rows.append(
            ConfirmatorySession(
                trade_date=date.fromisoformat(str(payload["trade_date"])[:10]),
                arm_id=str(payload["arm_id"]),
                fold_id=int(payload["fold_id"]),
                session_index=int(payload["session_index"]),
                net_returns=tuple(float(value) for value in returns),
                account_daily_return=float(payload["account_daily_return"]),
            )
        )
    if not rows or len(modes) != 1:
        raise ValueError("session file must contain exactly one capture_mode")
    return rows, next(iter(modes))


def _load_calendar(path: Path) -> list[date]:
    dates: set[date] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "is_open" not in reader.fieldnames:
            raise ValueError("calendar CSV requires is_open")
        date_column = next(
            (name for name in ("trade_date", "date") if name in reader.fieldnames), None
        )
        if date_column is None:
            raise ValueError("calendar CSV requires trade_date or date")
        for line_number, row in enumerate(reader, 2):
            day = date.fromisoformat(str(row.get(date_column) or "")[:10])
            flag = str(row.get("is_open") or "").strip().lower()
            if flag in {"1", "true", "yes", "y", "open"}:
                dates.add(day)
            elif flag not in {"0", "false", "no", "n", "closed"}:
                raise ValueError(f"calendar line {line_number} has invalid is_open={flag!r}")
    if not dates:
        raise ValueError("calendar contains no open sessions")
    return sorted(dates)


def _input_manifest(
    development_path: Path,
    confirmation_path: Path,
    calendar_path: Path,
    development_mode: str,
    confirmation_mode: str,
    development: list[ConfirmatorySession],
    confirmation: list[ConfirmatorySession],
    calendar: list[date],
    power_control_arm: str,
) -> dict[str, Any]:
    return {
        "development_sessions": {
            "path": str(development_path.resolve()),
            "sha256": sha256_file(development_path),
            "capture_mode": development_mode,
            "rows": len(development),
            "power_control_arm": power_control_arm,
        },
        "confirmation_sessions": {
            "path": str(confirmation_path.resolve()),
            "sha256": sha256_file(confirmation_path),
            "capture_mode": confirmation_mode,
            "rows": len(confirmation),
        },
        "trading_calendar": {
            "path": str(calendar_path.resolve()),
            "sha256": sha256_file(calendar_path),
            "open_sessions": len(calendar),
        },
    }


def _blocked_report(blockers: list[str]) -> dict[str, Any]:
    return {
        "schema": "tail_independent_confirmation_cli.v2",
        "state": "engineering_blocked",
        "passed": False,
        "scope": "independent_confirmatory_full_chain_pit",
        "release_eligible": False,
        "blockers": sorted(set(blockers)),
    }


def _write_new_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-sessions", required=True)
    parser.add_argument("--power-control-arm", required=True)
    parser.add_argument("--confirmation-sessions", required=True)
    parser.add_argument("--trading-calendar", required=True)
    parser.add_argument("--candidate-frozen-date", required=True)
    parser.add_argument("--candidate-artifact-sha256", required=True)
    parser.add_argument("--pit-evidence", help="full-chain field PIT evidence JSON")
    parser.add_argument("--unopened-window-attested", action="store_true")
    parser.add_argument("--draws", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    development_path = Path(args.development_sessions).expanduser()
    confirmation_path = Path(args.confirmation_sessions).expanduser()
    calendar_path = Path(args.trading_calendar).expanduser()
    pit_path = Path(args.pit_evidence).expanduser() if args.pit_evidence else None
    output_path = Path(args.out).expanduser()
    if output_path.exists():
        print("confirmation refuses to overwrite existing output", file=sys.stderr)
        return 5

    try:
        # Capture hashes before parsing, then compare again before writing the report.
        paths = [development_path, confirmation_path, calendar_path]
        if pit_path is not None:
            paths.append(pit_path)
        initial_hashes = {path: sha256_file(path) for path in paths}
        development, development_mode = _load_sessions(development_path)
        confirmation, confirmation_mode = _load_sessions(confirmation_path)
        calendar = _load_calendar(calendar_path)
        frozen_date = date.fromisoformat(args.candidate_frozen_date)
        power_source = [row for row in development if row.arm_id == args.power_control_arm]
        candidate_source = [row for row in development if row.arm_id == "candidate"]
        if not candidate_source or not power_source:
            raise ValueError("development file lacks candidate or requested power-control arm")
        if max(row.trade_date for row in development) >= frozen_date:
            raise ValueError("development sessions must end before candidate freeze date")
        power = estimate_development_power_sample_size(candidate_source, power_source)
        report = _blocked_report([])
        report["power_analysis"] = power
        report["candidate_artifact_sha256"] = str(args.candidate_artifact_sha256).lower()
        report["candidate_frozen_date"] = frozen_date.isoformat()
        report["unopened_window_attested"] = bool(args.unopened_window_attested)
        report["inputs"] = _input_manifest(
            development_path,
            confirmation_path,
            calendar_path,
            development_mode,
            confirmation_mode,
            development,
            confirmation,
            calendar,
            args.power_control_arm,
        )

        blockers: list[str] = []
        if power.get("status") != "estimated":
            blockers.append("development_power_sample_size_unavailable")
        if confirmation_mode != "observed_snapshot":
            blockers.append("confirmation_sessions_not_observed_snapshot")
        if not args.unopened_window_attested:
            blockers.append("independent_window_not_attested_unopened")
        if pit_path is None:
            blockers.append("field_level_pit_evidence_missing")
            inventory = field_inventory("full_chain")
            report["field_pit_audit"] = {
                "status": "blocked",
                "pit_evidence_checks_passed": False,
                "release_eligible": False,
                "blockers": {"field_level_evidence_not_supplied": 1},
                "fields": {
                    name: {
                        "status": "blocked",
                        "blockers": {"field_level_evidence_not_supplied": 1},
                    }
                    for name in inventory["fields"]
                },
                "inventory": inventory,
            }
        else:
            field_pit = load_and_audit_field_pit(
                pit_path,
                expected_input_sha256=initial_hashes[confirmation_path],
                expected_sessions=[
                    (row.trade_date.isoformat(), row.arm_id) for row in confirmation
                ],
                scope="full_chain",
            )
            report["field_pit_audit"] = field_pit
            if not field_pit["pit_evidence_checks_passed"]:
                blockers.append("field_level_pit_audit_failed")
                blockers.extend(
                    f"field_pit:{name}:{count}"
                    for name, count in field_pit.get("blockers", {}).items()
                )

        if not blockers:
            statistical = evaluate_independent_confirmation(
                sessions=confirmation,
                trading_calendar=calendar,
                candidate_frozen_date=frozen_date,
                unopened_window_attested=True,
                candidate_artifact_sha256=str(args.candidate_artifact_sha256).lower(),
                required_power_sessions=int(power["required_sessions"]),
                bootstrap_draws=int(args.draws),
                seed=int(args.seed),
            )
            report.update(statistical)
            report["schema"] = "tail_independent_confirmation_cli.v2"
            report["scope"] = "independent_confirmatory_full_chain_pit"
            report["field_pit_audit"] = field_pit
            report["power_analysis"] = power
            report["inputs"] = _input_manifest(
                development_path,
                confirmation_path,
                calendar_path,
                development_mode,
                confirmation_mode,
                development,
                confirmation,
                calendar,
                args.power_control_arm,
            )
            report["release_eligible"] = report.get("state") == "quality_passed"
        else:
            report["blockers"] = sorted(set(blockers))

        changed = [path for path in paths if sha256_file(path) != initial_hashes[path]]
        if changed:
            report["state"] = "engineering_blocked"
            report["passed"] = False
            report["release_eligible"] = False
            report["blockers"] = sorted(
                set(report.get("blockers", [])) | {"input_changed_during_confirmation"}
            )
            report["changed_inputs"] = [str(path.resolve()) for path in changed]

        _write_new_report(output_path, report)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"confirmation input blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5

    print(
        json.dumps(
            {
                "state": report.get("state"),
                "passed": report.get("passed"),
                "release_eligible": report.get("release_eligible"),
                "out": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if report.get("state") == "quality_passed":
        return 0
    if report.get("state") == "quality_failed":
        return 4
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
