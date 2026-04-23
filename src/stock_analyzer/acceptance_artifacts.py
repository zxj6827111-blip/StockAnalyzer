"""Acceptance artifact builders for baseline and phased checkpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path


def build_phase_checkpoint(
    *,
    phase: str,
    baseline_report: Mapping[str, object],
    baseline_report_path: str | Path,
    project_root: str | Path | None = None,
) -> dict[str, object]:
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
    normalized_phase = phase.strip().upper()
    if normalized_phase not in {"A", "B", "C"}:
        raise ValueError("phase must be one of A/B/C")

    gates = _build_gates(
        phase=normalized_phase,
        root=root,
        baseline_report=baseline_report,
        baseline_report_path=Path(baseline_report_path),
    )
    fail_count = sum(1 for gate in gates if str(gate["status"]) == "fail")
    status = "pass" if fail_count == 0 else "hold"
    return {
        "generated_at": datetime.now().isoformat(),
        "phase": normalized_phase,
        "status": status,
        "baseline_reference": str(Path(baseline_report_path)),
        "baseline_summary": {
            "baseline_type": str(baseline_report.get("baseline_type", "")),
            "walk_forward_folds": _extract_walk_forward_folds(baseline_report),
            "background_factor_fields": sorted(
                list(_coerce_dict(baseline_report.get("background_factor_coverage")).keys())
            ),
        },
        "gates": gates,
        "decision": _decision_for_phase(phase=normalized_phase, status=status),
        "affects_runtime": normalized_phase == "C",
    }


def write_checkpoint(*, checkpoint: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(checkpoint), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _build_gates(
    *,
    phase: str,
    root: Path,
    baseline_report: Mapping[str, object],
    baseline_report_path: Path,
) -> list[dict[str, object]]:
    baseline_exists = baseline_report_path.exists()
    model_status = _coerce_dict(baseline_report.get("model_status"))
    background = _coerce_dict(baseline_report.get("background_factor_coverage"))
    folds = _extract_walk_forward_folds(baseline_report)
    runtime_service = root / "src" / "stock_analyzer" / "runtime" / "service.py"
    news_provider = root / "src" / "stock_analyzer" / "news" / "provider.py"
    intraday_factors = root / "src" / "stock_analyzer" / "feature" / "intraday_factors.py"
    shadow_online_model = (
        root / "src" / "stock_analyzer" / "evolution" / "modules" / "shadow_online_model.py"
    )

    if phase == "A":
        return [
            _gate(
                name="baseline_report_present",
                status="pass" if baseline_exists else "fail",
                detail=f"path={baseline_report_path}",
                evidence=[str(baseline_report_path)],
            ),
            _gate(
                name="model_backend_visibility",
                status=(
                    "pass"
                    if all(
                        str(model_status.get(key, "")).strip()
                        for key in ("lgbm_backend", "xgb_backend")
                    )
                    else "fail"
                ),
                detail=(
                    f"lgbm={model_status.get('lgbm_backend', '')}, "
                    f"xgb={model_status.get('xgb_backend', '')}"
                ),
                evidence=[str(runtime_service)],
            ),
            _gate(
                name="walk_forward_checkpoint",
                status="pass" if folds >= 1 else "fail",
                detail=f"folds={folds}",
                evidence=[str(runtime_service)],
            ),
        ]

    if phase == "B":
        coverage_fields = {"holder_count", "block_trade_net", "financing_balance", "northbound_net"}
        available_fields = {str(key) for key in background.keys()}
        background_ready = coverage_fields.issubset(available_fields)
        return [
            _gate(
                name="background_factor_coverage",
                status="pass" if background_ready else "fail",
                detail=f"fields={sorted(available_fields)}",
                evidence=[str(baseline_report_path)],
            ),
            _gate(
                name="intraday_factor_bridge_present",
                status="pass" if intraday_factors.exists() else "fail",
                detail=f"path={intraday_factors}",
                evidence=[str(intraday_factors)],
            ),
            _gate(
                name="news_signal_provider_present",
                status="pass" if news_provider.exists() else "fail",
                detail=f"path={news_provider}",
                evidence=[str(news_provider)],
            ),
        ]

    runtime_text = runtime_service.read_text(encoding="utf-8")
    staged_take_profit_ready = (
        "take_profit_stage_1_reached" in runtime_text
        and "take_profit_stage_2_reached" in runtime_text
    )
    hrp_shadow_ready = "hrp_shadow" in runtime_text
    return [
        _gate(
            name="hrp_shadow_available",
            status="pass" if hrp_shadow_ready else "fail",
            detail=f"path={runtime_service}",
            evidence=[str(runtime_service)],
        ),
        _gate(
            name="shadow_online_model_present",
            status="pass" if shadow_online_model.exists() else "fail",
            detail=f"path={shadow_online_model}",
            evidence=[str(shadow_online_model)],
        ),
        _gate(
            name="staged_take_profit_available",
            status="pass" if staged_take_profit_ready else "fail",
            detail=f"path={runtime_service}",
            evidence=[str(runtime_service)],
        ),
    ]


def _gate(*, name: str, status: str, detail: str, evidence: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": evidence,
    }


def _decision_for_phase(*, phase: str, status: str) -> dict[str, object]:
    if phase == "A":
        recommendation = "进入阶段 B" if status == "pass" else "停留在阶段 A 修复主链路"
    elif phase == "B":
        recommendation = "进入阶段 C" if status == "pass" else "停留在阶段 B 修复数据利用率"
    else:
        recommendation = (
            "组合层可继续 shadow/灰度观察" if status == "pass" else "保持 shadow，暂不提升正式执行"
        )
    return {
        "phase_gate_open": status == "pass",
        "recommendation": recommendation,
    }


def _extract_walk_forward_folds(report: Mapping[str, object]) -> int:
    walk_forward = _coerce_dict(report.get("walk_forward"))
    summary = _coerce_dict(walk_forward.get("summary"))
    raw = summary.get("folds", 0)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        return int(raw)
    return 0


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}
