"""Run a guarded NAS advisory pipeline probe and validate captured evidence."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.p0_nas_advisory_validation import (  # noqa: E402
    build_validation_report,
    render_markdown_report,
)
from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.research.p0_analysis_inputs import write_p0_analysis_inputs  # noqa: E402
from stock_analyzer.research.shadow_experiment_planner import (  # noqa: E402
    build_shadow_experiment_plan,
)

HttpRequest = Callable[[str, str, Mapping[str, object] | None], dict[str, object]]


class ProbeError(RuntimeError):
    """Raised when the advisory probe must stop before mutating runtime state."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Safely run one advisory-only pipeline probe on NAS, capture runtime evidence, "
            "and build the P0 advisory validation report."
        ),
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:18001")
    parser.add_argument("--api-token", default="", help="Optional API token for protected POSTs.")
    parser.add_argument(
        "--symbols",
        default="600000,000001",
        help="Comma-separated symbols for the controlled probe.",
    )
    parser.add_argument("--strategy", default="trend")
    parser.add_argument("--current-equity", type=float, default=1.0)
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for captured commands and validation report.",
    )
    parser.add_argument(
        "--runtime-state",
        default="artifacts/runtime/runtime_state.json",
        help="runtime_state.json path to validate after the probe.",
    )
    parser.add_argument(
        "--confirm-run",
        action="store_true",
        help="Required to trigger POST /run/pipeline. Without it the script only checks state.",
    )
    parser.add_argument("--audit-limit", type=int, default=200)
    parser.add_argument("--signal-quality-limit", type=int, default=200)
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Config YAML path used to build P0 analysis artifacts after the probe.",
    )
    parser.add_argument(
        "--model-artifact",
        default="artifacts/model_v1.json",
        help="Model artifact path used by P0 analysis artifact generation.",
    )
    parser.add_argument(
        "--skip-analysis",
        action="store_true",
        help="Only run/capture/validate the advisory probe; skip analysis artifact generation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = _path(args.output_dir) if args.output_dir else _default_output_dir()
    result = run_probe(
        api_base=args.api_base,
        api_token=args.api_token,
        symbols=_parse_symbols(args.symbols),
        strategy=args.strategy,
        current_equity=args.current_equity,
        output_dir=output_dir,
        runtime_state_path=_path(args.runtime_state),
        confirm_run=bool(args.confirm_run),
        audit_limit=max(1, int(args.audit_limit)),
        signal_quality_limit=max(1, int(args.signal_quality_limit)),
        config_path=_path(args.config),
        model_artifact_path=_path(args.model_artifact),
        build_analysis=not bool(args.skip_analysis),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def run_probe(
    *,
    api_base: str,
    api_token: str,
    symbols: list[str],
    strategy: str,
    current_equity: float,
    output_dir: Path,
    runtime_state_path: Path,
    confirm_run: bool,
    audit_limit: int = 200,
    signal_quality_limit: int = 200,
    config_path: Path | None = None,
    model_artifact_path: Path | None = None,
    build_analysis: bool = True,
    http_request: HttpRequest | None = None,
) -> dict[str, object]:
    client = http_request or _http_json_request(api_base=api_base, api_token=api_token)
    output_dir.mkdir(parents=True, exist_ok=True)
    commands_dir = output_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    ops_state = client("GET", "/dashboard/ops/state", None)
    _write_json(commands_dir / "ops_state_before.json", ops_state)
    if not bool(ops_state.get("advisory_only")):
        raise ProbeError(
            "Refusing to run pipeline because /dashboard/ops/state advisory_only is not true."
        )
    if not confirm_run:
        return {
            "status": "state_checked_no_pipeline_run",
            "output_dir": str(output_dir),
            "message": "Pass --confirm-run to trigger one advisory pipeline probe.",
        }

    pipeline_payload = {
        "symbols": symbols,
        "strategy": strategy,
        "current_equity": current_equity,
        "use_live_runtime": False,
        "dry_run_execution": False,
        "notify_enabled": False,
    }
    pipeline = client("POST", "/run/pipeline", pipeline_payload)
    _write_json(commands_dir / "pipeline_advisory.json", pipeline)
    if str(pipeline.get("execution_mode", "")).strip() != "advisory_only":
        raise ProbeError("Pipeline response was not advisory_only; validation is unsafe.")

    signals_latest = client("GET", "/signals/latest", None)
    _write_json(commands_dir / "signals_latest_after.json", signals_latest)
    audit_events = client("GET", f"/audit/events?{urlencode({'limit': audit_limit})}", None)
    _write_json(commands_dir / "audit_events_after.json", audit_events)
    signal_quality = client(
        "POST",
        "/research/signal-quality/run",
        {"limit": signal_quality_limit, "include_audit_events": True},
    )
    _write_json(commands_dir / "signal_quality_after.json", signal_quality)

    runtime_state = _load_json(runtime_state_path)
    report = build_validation_report(
        runtime_state=runtime_state,
        runtime_state_path=runtime_state_path,
        signals_latest=signals_latest,
        audit_events=_extract_events(audit_events),
        signal_quality=signal_quality,
    )
    json_path = output_dir / "nas_advisory_validation_report.json"
    md_path = output_dir / "nas_advisory_validation_report.md"
    _write_json(json_path, report)
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    analysis_result: dict[str, object] = {"status": "skipped"}
    if build_analysis:
        analysis_result = _build_probe_analysis(
            output_dir=output_dir,
            runtime_state_path=runtime_state_path,
            signals_latest_path=commands_dir / "signals_latest_after.json",
            audit_events_path=commands_dir / "audit_events_after.json",
            config_path=config_path or _path("config/default.yaml"),
            model_artifact_path=model_artifact_path or _path("artifacts/model_v1.json"),
        )
    return {
        "status": report.get("status"),
        "output_dir": str(output_dir),
        "pipeline_trace_id": pipeline.get("trace_id"),
        "validation_json": str(json_path),
        "validation_markdown": str(md_path),
        "analysis": analysis_result,
    }


def _build_probe_analysis(
    *,
    output_dir: Path,
    runtime_state_path: Path,
    signals_latest_path: Path,
    audit_events_path: Path,
    config_path: Path,
    model_artifact_path: Path,
) -> dict[str, object]:
    analysis_dir = output_dir / "analysis"
    config = load_config(config_path)
    manifest = write_p0_analysis_inputs(
        analysis_dir=analysis_dir,
        model_artifact_path=model_artifact_path,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state_path, signals_latest_path],
        audit_event_paths=[runtime_state_path, audit_events_path],
        config=config,
        generated_at=datetime.now(),
    )
    plan = build_shadow_experiment_plan(analysis_dir=analysis_dir)
    shadow_plan_path = analysis_dir / "p0_shadow_experiment_plan_v1.json"
    _write_json(shadow_plan_path, plan)
    manifest["outputs"]["shadow_plan"] = str(shadow_plan_path)
    manifest_path = analysis_dir / "p0_analysis_inputs_manifest.json"
    _write_json(manifest_path, manifest)
    return {
        "status": "generated",
        "analysis_dir": str(analysis_dir),
        "manifest": str(manifest_path),
        "shadow_plan": str(shadow_plan_path),
        "remaining_expected_inputs": manifest.get("remaining_expected_inputs", []),
        "plan_status": plan.get("status"),
        "input_completeness": plan.get("input_completeness"),
    }


def _http_json_request(*, api_base: str, api_token: str) -> HttpRequest:
    base = api_base.rstrip("/")

    def _request(method: str, path: str, payload: Mapping[str, object] | None) -> dict[str, object]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if api_token.strip():
            headers["Authorization"] = f"Bearer {api_token.strip()}"
        request = urllib.request.Request(
            base + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")
            raise ProbeError(f"HTTP {exc.code} for {method} {path}: {raw_error}") from exc
        except urllib.error.URLError as exc:
            raise ProbeError(f"Cannot reach API for {method} {path}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"Non-JSON response for {method} {path}: {raw[:200]}") from exc
        return dict(parsed) if isinstance(parsed, Mapping) else {"response": parsed}

    return _request


def _extract_events(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return []
    return [dict(item) for item in raw_events if isinstance(item, Mapping)]


def _parse_symbols(raw: str) -> list[str]:
    symbols = [item.strip() for item in raw.split(",") if item.strip()]
    if not symbols:
        raise ProbeError("At least one symbol is required.")
    return symbols


def _default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S%z")
    return REPO_ROOT / "artifacts" / "research" / f"p0_nas_advisory_probe_{stamp}"


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
