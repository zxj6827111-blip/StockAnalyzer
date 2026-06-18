"""Build read-only P0 analysis inputs from local artifacts."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.research.p0_analysis_inputs import write_p0_analysis_inputs  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build model diagnosis and cross-review P0 analysis inputs",
    )
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Config YAML path used only for current threshold values.",
    )
    parser.add_argument(
        "--analysis-dir",
        default="artifacts/analysis",
        help="Directory where analysis JSON artifacts are written.",
    )
    parser.add_argument(
        "--model-artifact",
        default="artifacts/model_v1.json",
        help="Model artifact JSON path.",
    )
    parser.add_argument(
        "--learning-manifest-glob",
        action="append",
        default=[],
        help="Glob for learning manifest/model artifact JSON files. Can be repeated.",
    )
    parser.add_argument(
        "--signal-source",
        action="append",
        default=[],
        help="Signal source JSON/JSONL path. Can be repeated.",
    )
    parser.add_argument(
        "--signal-glob",
        action="append",
        default=[],
        help="Glob for signal source JSON/JSONL files. Can be repeated.",
    )
    parser.add_argument(
        "--audit-event-source",
        action="append",
        default=[],
        help="Audit event JSON/JSONL path. Can be repeated.",
    )
    parser.add_argument(
        "--audit-event-glob",
        action="append",
        default=[],
        help="Glob for audit event JSON/JSONL files. Can be repeated.",
    )
    parser.add_argument(
        "--shadow-plan-output",
        default="",
        help="Optional output path for p0_shadow_experiment_plan_v1.json.",
    )
    parser.add_argument(
        "--skip-research-completeness-artifacts",
        action="store_true",
        help=(
            "Only write model diagnosis and cross-review artifacts; skip "
            "final_report_v3, feature family ablation and position framework."
        ),
    )
    parser.add_argument(
        "--skip-shadow-plan",
        action="store_true",
        help="Only build input artifacts, do not refresh the P0 shadow plan.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(_path(args.config))
    analysis_dir = _path(args.analysis_dir)
    learning_manifest_paths = _expand_paths(args.learning_manifest_glob)
    signal_paths = _expand_signal_paths(
        explicit_paths=args.signal_source,
        globs=args.signal_glob,
    )
    audit_event_paths = _expand_audit_event_paths(
        explicit_paths=args.audit_event_source,
        globs=args.audit_event_glob,
    )
    manifest = write_p0_analysis_inputs(
        analysis_dir=analysis_dir,
        model_artifact_path=_path(args.model_artifact),
        learning_manifest_paths=learning_manifest_paths,
        signal_source_paths=signal_paths,
        config=config,
        audit_event_paths=audit_event_paths,
        include_research_completeness_artifacts=not args.skip_research_completeness_artifacts,
    )
    if not args.skip_shadow_plan:
        output = (
            _path(args.shadow_plan_output)
            if str(args.shadow_plan_output).strip()
            else analysis_dir / "p0_shadow_experiment_plan_v1.json"
        )
        planner = importlib.import_module("stock_analyzer.research.shadow_experiment_planner")
        plan = planner.build_shadow_experiment_plan(analysis_dir=analysis_dir)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["outputs"]["shadow_plan"] = str(output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def _expand_signal_paths(
    *,
    explicit_paths: list[str],
    globs: list[str],
) -> list[Path]:
    paths = [_path(item) for item in explicit_paths if str(item).strip()]
    if globs:
        paths.extend(_expand_paths(globs))
    if not paths:
        paths.extend(
            candidate
            for candidate in (
                _path("artifacts/runtime/runtime_state.json"),
                _path("artifacts/runtime/runtime_state_history/week5_scan_history.jsonl"),
                _path("artifacts/runtime/week5_scan_latest.json"),
                _path("artifacts/week5_scan_latest.json"),
            )
            if candidate.exists()
        )
    return _dedupe_existing(paths)


def _expand_audit_event_paths(
    *,
    explicit_paths: list[str],
    globs: list[str],
) -> list[Path]:
    paths = [_path(item) for item in explicit_paths if str(item).strip()]
    if globs:
        paths.extend(_expand_paths(globs))
    if not paths:
        paths.extend(
            candidate
            for candidate in (
                _path("artifacts/runtime/runtime_state.json"),
                _path("artifacts/runtime/audit_events.jsonl"),
                _path("artifacts/runtime/runtime_state_history/audit_events.jsonl"),
                _path("artifacts/runtime/runtime_state_history/pipeline_audit_events.jsonl"),
            )
            if candidate.exists()
        )
    return _dedupe_existing(paths)


def _expand_paths(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        raw = str(pattern).strip()
        if not raw:
            continue
        if any(token in raw for token in ("*", "?", "[")):
            paths.extend(REPO_ROOT.glob(raw))
        else:
            paths.append(_path(raw))
    return _dedupe_existing(paths)


def _dedupe_existing(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def _path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


if __name__ == "__main__":
    main()
