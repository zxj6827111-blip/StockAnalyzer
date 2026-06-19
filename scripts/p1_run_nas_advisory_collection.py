"""Run repeated NAS advisory-only probes and summarize P1 shadow evidence."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from scripts.p0_run_nas_advisory_probe import ProbeError, run_probe  # noqa: E402
from scripts.p1_nas_shadow_validation import (  # noqa: E402
    build_p1_validation_report,
    render_markdown_report,
)

HttpRequest = Callable[[str, str, Mapping[str, object] | None], dict[str, object]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run repeated advisory-only NAS probes, keep per-run evidence, and write "
            "a P1 collection summary. This never enables live trading by itself."
        ),
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:18001")
    parser.add_argument("--api-token", default="")
    parser.add_argument("--symbols", default="600000,000001")
    parser.add_argument("--strategy", default="trend")
    parser.add_argument("--current-equity", type=float, default=1.0)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--interval-sec", type=float, default=0.0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--runtime-state", default="artifacts/runtime/runtime_state.json")
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--model-artifact", default="artifacts/model_v1.json")
    parser.add_argument("--audit-limit", type=int, default=200)
    parser.add_argument("--signal-quality-limit", type=int, default=200)
    parser.add_argument(
        "--confirm-run",
        action="store_true",
        help="Required to trigger pipeline probes. Without it only a dry plan is written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = _path(args.output_dir) if args.output_dir else _default_output_dir()
    report = run_collection(
        api_base=args.api_base,
        api_token=args.api_token,
        symbols=_parse_symbols(args.symbols),
        strategy=args.strategy,
        current_equity=float(args.current_equity),
        runs=max(1, int(args.runs)),
        interval_sec=max(0.0, float(args.interval_sec)),
        output_dir=output_dir,
        runtime_state_path=_path(args.runtime_state),
        config_path=_path(args.config),
        model_artifact_path=_path(args.model_artifact),
        audit_limit=max(1, int(args.audit_limit)),
        signal_quality_limit=max(1, int(args.signal_quality_limit)),
        confirm_run=bool(args.confirm_run),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run_collection(
    *,
    api_base: str,
    api_token: str,
    symbols: list[str],
    strategy: str,
    current_equity: float,
    runs: int,
    interval_sec: float,
    output_dir: Path,
    runtime_state_path: Path,
    config_path: Path,
    model_artifact_path: Path,
    audit_limit: int = 200,
    signal_quality_limit: int = 200,
    confirm_run: bool,
    http_request: HttpRequest | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not confirm_run:
        report = {
            "report_type": "p1_nas_advisory_collection",
            "status": "dry_plan_no_pipeline_run",
            "production_change_allowed": False,
            "output_dir": str(output_dir),
            "planned_runs": max(1, int(runs)),
            "interval_sec": max(0.0, float(interval_sec)),
            "message": "Pass --confirm-run to run repeated advisory-only probes.",
        }
        _write_collection_report(output_dir=output_dir, report=report)
        return report

    run_items: list[dict[str, object]] = []
    failed = False
    for index in range(max(1, int(runs))):
        run_dir = output_dir / f"run_{index + 1:03d}"
        try:
            result = run_probe(
                api_base=api_base,
                api_token=api_token,
                symbols=symbols,
                strategy=strategy,
                current_equity=current_equity,
                output_dir=run_dir,
                runtime_state_path=runtime_state_path,
                confirm_run=True,
                audit_limit=audit_limit,
                signal_quality_limit=signal_quality_limit,
                config_path=config_path,
                model_artifact_path=model_artifact_path,
                build_analysis=True,
                http_request=http_request,
            )
            p1_report = build_p1_validation_report(probe_dir=run_dir)
            _write_json(run_dir / "nas_validation_report.json", p1_report)
            (run_dir / "nas_validation_report.md").write_text(
                render_markdown_report(p1_report),
                encoding="utf-8",
            )
            status = str(p1_report.get("status", "")).strip() or str(
                result.get("status", "")
            ).strip()
            failed = failed or status != "pass"
            run_items.append(
                {
                    "index": index + 1,
                    "status": status,
                    "output_dir": str(run_dir),
                    "pipeline_trace_id": result.get("pipeline_trace_id"),
                    "p1_candidate_variant_count": _nested_int(
                        p1_report,
                        "p1_probability_scale_shadow_grid",
                        "candidate_variant_count",
                    ),
                    "financial_raw_fields_observed": _check_passed(
                        p1_report,
                        "financial_raw_fields_observed",
                    ),
                    "mature_return_samples": _nested_int(
                        p1_report,
                        "maturity",
                        "mature_return_samples",
                    ),
                }
            )
        except Exception as exc:
            failed = True
            run_items.append(
                {
                    "index": index + 1,
                    "status": "failed",
                    "output_dir": str(run_dir),
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        if failed:
            break
        if index + 1 < runs and interval_sec > 0:
            sleep(interval_sec)

    summary = _collection_summary(run_items)
    report = {
        "report_type": "p1_nas_advisory_collection",
        "status": "pass" if not failed and run_items else "needs_review",
        "production_change_allowed": False,
        "output_dir": str(output_dir),
        "requested_runs": max(1, int(runs)),
        "completed_runs": len(run_items),
        "interval_sec": max(0.0, float(interval_sec)),
        "runs": run_items,
        "summary": summary,
        "next_actions": _next_actions(report_status="pass" if not failed else "needs_review", summary=summary),
    }
    _write_collection_report(output_dir=output_dir, report=report)
    return report


def _collection_summary(run_items: list[Mapping[str, object]]) -> dict[str, object]:
    passed = [item for item in run_items if str(item.get("status")) == "pass"]
    return {
        "passed_runs": len(passed),
        "failed_runs": len(run_items) - len(passed),
        "max_candidate_variant_count": max(
            [_int(item.get("p1_candidate_variant_count")) for item in run_items] or [0]
        ),
        "financial_raw_fields_observed_runs": sum(
            1 for item in run_items if bool(item.get("financial_raw_fields_observed"))
        ),
        "max_mature_return_samples": max(
            [_int(item.get("mature_return_samples")) for item in run_items] or [0]
        ),
    }


def _next_actions(*, report_status: str, summary: Mapping[str, object]) -> list[str]:
    actions: list[str] = []
    if report_status != "pass":
        actions.append("Review failed run directories before using this collection as evidence.")
    if _int(summary.get("financial_raw_fields_observed_runs")) == 0:
        actions.append("Confirm latest pipeline signals include financial raw fields.")
    if _int(summary.get("max_mature_return_samples")) < 50:
        actions.append("Continue advisory_only collection until at least 50 mature samples exist.")
    if _int(summary.get("max_mature_return_samples")) < 100:
        actions.append("Do not change production thresholds before 100 mature samples.")
    if not actions:
        actions.append("Compare candidate-generating P1 variants against mature outcomes weekly.")
    return actions


def _write_collection_report(*, output_dir: Path, report: Mapping[str, object]) -> None:
    _write_json(output_dir / "p1_advisory_collection_report.json", report)
    (output_dir / "p1_advisory_collection_report.md").write_text(
        _render_collection_markdown(report),
        encoding="utf-8",
    )


def _render_collection_markdown(report: Mapping[str, object]) -> str:
    summary = _mapping(report.get("summary"))
    lines = [
        "# P1 NAS Advisory Collection Report",
        "",
        f"- status: {report.get('status')}",
        f"- production_change_allowed: {str(report.get('production_change_allowed')).lower()}",
        f"- output_dir: {report.get('output_dir')}",
        f"- requested_runs: {report.get('requested_runs', report.get('planned_runs'))}",
        f"- completed_runs: {report.get('completed_runs', 0)}",
        f"- passed_runs: {summary.get('passed_runs', 0)}",
        f"- failed_runs: {summary.get('failed_runs', 0)}",
        f"- max_candidate_variant_count: {summary.get('max_candidate_variant_count', 0)}",
        f"- financial_raw_fields_observed_runs: {summary.get('financial_raw_fields_observed_runs', 0)}",
        f"- max_mature_return_samples: {summary.get('max_mature_return_samples', 0)}",
        "",
        "## Runs",
        "",
    ]
    for item in _list(report.get("runs")):
        run = _mapping(item)
        lines.append(
            "- run {index}: status={status}, trace={trace}, financial_raw={financial_raw}, mature_samples={samples}, dir={dir}".format(
                index=run.get("index"),
                status=run.get("status"),
                trace=run.get("pipeline_trace_id", ""),
                financial_raw=run.get("financial_raw_fields_observed", ""),
                samples=run.get("mature_return_samples", ""),
                dir=run.get("output_dir", ""),
            )
        )
    lines.extend(["", "## Next Actions", ""])
    for action in _list(report.get("next_actions")):
        lines.append(f"- {action}")
    lines.append("")
    return "\n".join(lines)


def _check_passed(report: Mapping[str, object], code: str) -> bool:
    for item in _list(report.get("checks")):
        check = _mapping(item)
        if str(check.get("code", "")).strip() == code:
            return bool(check.get("passed"))
    return False


def _nested_int(report: Mapping[str, object], section: str, key: str) -> int:
    return _int(_mapping(report.get(section)).get(key))


def _parse_symbols(raw: str) -> list[str]:
    symbols = [item.strip() for item in raw.split(",") if item.strip()]
    if not symbols:
        raise ProbeError("At least one symbol is required.")
    return symbols


def _default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S%z")
    return REPO_ROOT / "artifacts" / "research" / f"p1_advisory_collection_{stamp}"


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


if __name__ == "__main__":
    main()
