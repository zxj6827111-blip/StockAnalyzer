"""Validate NAS advisory pipeline evidence after a controlled runtime run."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only NAS advisory validation report from runtime artifacts.",
    )
    parser.add_argument(
        "--runtime-state",
        default="artifacts/runtime/runtime_state.json",
        help="runtime_state.json path.",
    )
    parser.add_argument(
        "--signals-latest",
        default="",
        help="Optional JSON captured from GET /signals/latest.",
    )
    parser.add_argument(
        "--audit-events",
        action="append",
        default=[],
        help="Audit event JSON/JSONL path or captured GET /audit/events JSON. Can be repeated.",
    )
    parser.add_argument(
        "--signal-quality",
        default="",
        help="Optional JSON captured from POST /research/signal-quality/run.",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts/research/p0_nas_advisory_validation",
        help="Directory for nas_advisory_validation_report.md/json.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime_state_path = _path(args.runtime_state)
    output_dir = _path(args.output_dir)
    report = build_validation_report(
        runtime_state=_load_json(runtime_state_path),
        runtime_state_path=runtime_state_path,
        signals_latest=_load_json(_path(args.signals_latest)) if args.signals_latest else {},
        audit_events=_collect_audit_events([_path(item) for item in args.audit_events]),
        signal_quality=_load_json(_path(args.signal_quality)) if args.signal_quality else {},
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "nas_advisory_validation_report.json"
    md_path = output_dir / "nas_advisory_validation_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))


def build_validation_report(
    *,
    runtime_state: Mapping[str, object],
    runtime_state_path: Path,
    signals_latest: Mapping[str, object],
    audit_events: Sequence[Mapping[str, object]],
    signal_quality: Mapping[str, object],
) -> dict[str, object]:
    latest_signals = _latest_signals_summary(runtime_state, signals_latest)
    pipeline = _latest_pipeline_summary(audit_events)
    signal_quality_summary = _signal_quality_summary(signal_quality)
    checks = _checks(
        latest_signals=latest_signals,
        pipeline=pipeline,
        signal_quality=signal_quality_summary,
    )
    return {
        "report_type": "p0_nas_advisory_validation",
        "production_change_allowed": False,
        "runtime_state_path": str(runtime_state_path),
        "latest_signals": latest_signals,
        "latest_pipeline_run": pipeline,
        "signal_quality": signal_quality_summary,
        "checks": checks,
        "status": "pass" if all(item["passed"] for item in checks) else "needs_review",
        "next_actions": _next_actions(checks),
    }


def render_markdown_report(report: Mapping[str, object]) -> str:
    checks = [item for item in _list(report.get("checks")) if isinstance(item, Mapping)]
    latest = _mapping(report.get("latest_signals"))
    pipeline = _mapping(report.get("latest_pipeline_run"))
    quality = _mapping(report.get("signal_quality"))
    lines = [
        "# NAS Advisory Validation Report",
        "",
        f"- status: {report.get('status')}",
        f"- production_change_allowed: {str(report.get('production_change_allowed')).lower()}",
        f"- latest_signals_source: {latest.get('source')}",
        f"- latest_signals_storage_source: {latest.get('storage_source')}",
        f"- latest_signals_count: {latest.get('signal_count')}",
        f"- latest_pipeline_execution_mode: {pipeline.get('execution_mode')}",
        f"- latest_pipeline_has_executions_field: {pipeline.get('has_executions_field')}",
        f"- latest_pipeline_executions_count: {pipeline.get('executions_count')}",
        f"- signal_quality_source: {quality.get('signal_source')}",
        "",
        "## Checks",
        "",
    ]
    for item in checks:
        mark = "PASS" if item.get("passed") else "REVIEW"
        lines.append(f"- {mark}: {item.get('code')} - {item.get('detail')}")
    lines.extend(["", "## Next Actions", ""])
    for item in _list(report.get("next_actions")):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _latest_signals_summary(
    runtime_state: Mapping[str, object],
    signals_latest: Mapping[str, object],
) -> dict[str, object]:
    runtime_latest = _mapping(runtime_state.get("latest_signals"))
    runtime_signals = _list(runtime_latest.get("signals"))
    api_signals = _list(signals_latest.get("signals"))
    api_source = str(signals_latest.get("source", "")).strip()
    return {
        "runtime_present": bool(runtime_latest),
        "runtime_signal_count": len(runtime_signals),
        "runtime_trace_id": str(runtime_latest.get("trace_id", "")).strip(),
        "runtime_source": str(runtime_latest.get("source", "")).strip(),
        "runtime_timestamp": str(runtime_latest.get("timestamp", "")).strip(),
        "api_present": bool(signals_latest),
        "signal_count": len(api_signals),
        "source": api_source,
        "storage_source": str(signals_latest.get("storage_source", "")).strip(),
        "api_uses_latest_signals": api_source not in {"week5_latest_candidates", "empty", ""},
    }


def _latest_pipeline_summary(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    pipeline_events = [
        event for event in events if str(event.get("event_type", "")).strip() == "pipeline_run"
    ]
    latest = sorted(pipeline_events, key=_event_sort_key)[-1] if pipeline_events else {}
    payload = _mapping(latest.get("payload"))
    update = _mapping(payload.get("portfolio_update"))
    attempts = _mapping(update.get("execution_attempts"))
    advisory_attempts = _mapping(update.get("advisory_attempts"))
    executions = update.get("executions")
    return {
        "present": bool(latest),
        "event_count": len(pipeline_events),
        "trace_id": str(latest.get("trace_id", "")).strip(),
        "execution_mode": str(payload.get("execution_mode", "")).strip(),
        "has_execution_attempts": bool(attempts),
        "has_advisory_attempts": bool(advisory_attempts),
        "execution_attempts": attempts,
        "advisory_attempts": advisory_attempts,
        "has_executions_field": isinstance(executions, list),
        "executions_count": len(executions) if isinstance(executions, list) else None,
        "portfolio_status": str(update.get("status", "")).strip(),
    }


def _signal_quality_summary(signal_quality: Mapping[str, object]) -> dict[str, object]:
    funnel = _mapping(signal_quality.get("signal_loss_funnel"))
    return {
        "present": bool(signal_quality),
        "status": str(signal_quality.get("status", "")).strip(),
        "signal_source": str(signal_quality.get("signal_source", "")).strip(),
        "signal_storage_source": str(signal_quality.get("signal_storage_source", "")).strip(),
        "source_signal_count": _int(signal_quality.get("source_signal_count")),
        "execution_attempts": _mapping(funnel.get("execution_attempts")),
        "advisory_attempts": _mapping(funnel.get("advisory_attempts")),
        "dry_run_attempts": _mapping(funnel.get("dry_run_attempts")),
        "execution_stages": _mapping(funnel.get("execution_stages")),
        "data_gaps": _list(funnel.get("data_gaps")),
    }


def _checks(
    *,
    latest_signals: Mapping[str, object],
    pipeline: Mapping[str, object],
    signal_quality: Mapping[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "code": "runtime_state_latest_signals_persisted",
            "passed": bool(latest_signals.get("runtime_present"))
            and _int(latest_signals.get("runtime_signal_count")) > 0,
            "detail": "runtime_state.latest_signals exists and contains signals",
        },
        {
            "code": "runtime_state_latest_signals_source_is_pipeline_run",
            "passed": str(latest_signals.get("runtime_source", "")).strip() == "pipeline_run",
            "detail": "persisted latest_signals came from a controlled pipeline run",
        },
        {
            "code": "signals_latest_uses_latest_not_week5_fallback",
            "passed": bool(latest_signals.get("api_uses_latest_signals"))
            and _int(latest_signals.get("signal_count")) > 0,
            "detail": "/signals/latest source is not week5_latest_candidates or empty",
        },
        {
            "code": "latest_pipeline_is_advisory_only",
            "passed": str(pipeline.get("execution_mode", "")).strip() == "advisory_only",
            "detail": "latest pipeline_run payload execution_mode is advisory_only",
        },
        {
            "code": "pipeline_has_empty_executions",
            "passed": bool(pipeline.get("has_executions_field"))
            and _int(pipeline.get("executions_count")) == 0,
            "detail": "portfolio_update.executions exists and is empty",
        },
        {
            "code": "pipeline_has_advisory_attempt_fields",
            "passed": bool(pipeline.get("has_advisory_attempts"))
            and not bool(pipeline.get("execution_attempts")),
            "detail": (
                "advisory pipeline_run contains advisory_attempts and keeps "
                "execution_attempts empty"
            ),
        },
        {
            "code": "signal_quality_keeps_advisory_out_of_execution",
            "passed": bool(signal_quality.get("present"))
            and bool(signal_quality.get("advisory_attempts"))
            and not bool(signal_quality.get("execution_attempts")),
            "detail": (
                "signal quality report does not mix advisory attempts into execution attempts"
            ),
        },
    ]


def _next_actions(checks: Sequence[Mapping[str, object]]) -> list[str]:
    failed = {str(item.get("code", "")) for item in checks if not item.get("passed")}
    actions: list[str] = []
    if "runtime_state_latest_signals_persisted" in failed:
        actions.append("Run one controlled advisory pipeline and confirm runtime_state write.")
    if "runtime_state_latest_signals_source_is_pipeline_run" in failed:
        actions.append(
            "Confirm latest_signals was refreshed by the new pipeline_run, "
            "not an older fallback snapshot."
        )
    if "signals_latest_uses_latest_not_week5_fallback" in failed:
        actions.append("Capture GET /signals/latest after advisory pipeline and inspect source.")
    if "latest_pipeline_is_advisory_only" in failed:
        actions.append("Do not interpret this run; rerun with config app.advisory_only=true.")
    if (
        "pipeline_has_empty_executions" in failed
        or "pipeline_has_advisory_attempt_fields" in failed
    ):
        actions.append(
            "Inspect latest pipeline_run audit payload shape before using funnel results."
        )
    if "signal_quality_keeps_advisory_out_of_execution" in failed:
        actions.append(
            "Run POST /research/signal-quality/run and verify advisory_attempts separation."
        )
    if not actions:
        actions.append("Proceed to compare P0 shadow reports against mature outcome coverage.")
    return actions


def _collect_audit_events(paths: Sequence[Path]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for path in paths:
        for payload in _iter_json_payloads(path):
            if str(payload.get("event_type", "")).strip():
                events.append(dict(payload))
                continue
            raw_events = _list(payload.get("events"))
            events.extend(dict(item) for item in raw_events if isinstance(item, Mapping))
            raw_audit_events = _list(payload.get("audit_events"))
            events.extend(dict(item) for item in raw_audit_events if isinstance(item, Mapping))
    return events


def _iter_json_payloads(path: Path) -> Iterable[dict[str, object]]:
    if not path.exists() or not path.is_file():
        return
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, Mapping):
                    yield dict(payload)
        return
    payload = _load_json(path)
    if payload:
        yield payload


def _event_sort_key(event: Mapping[str, object]) -> tuple[str, str]:
    timestamp = str(event.get("timestamp", "")).strip()
    event_id = str(event.get("event_id", "")).strip()
    return (timestamp, event_id)


def _load_json(path: Path) -> dict[str, object]:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


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
