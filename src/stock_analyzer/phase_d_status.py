"""Deferred Phase D backlog status helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

_PHASE_D_ITEMS: tuple[dict[str, str], ...] = (
    {
        "id": "alphalens_sidecar",
        "title": "Alphalens sidecar validation",
        "status": "completed",
        "priority": "active_research",
        "delivery_mode": "research_sidecar",
        "api_endpoint": "/research/alphalens/report",
        "cli_command": "phase-d-alphalens",
    },
    {
        "id": "shap_sidecar",
        "title": "SHAP sidecar validation",
        "status": "completed",
        "priority": "active_research",
        "delivery_mode": "research_sidecar",
        "api_endpoint": "/research/shap/report",
        "cli_command": "phase-d-shap",
    },
    {
        "id": "catboost_shadow",
        "title": "CatBoost shadow validation",
        "status": "completed",
        "priority": "active_research",
        "delivery_mode": "research_sidecar",
        "api_endpoint": "/research/catboost-shadow/report",
        "cli_command": "phase-d-catboost-shadow",
    },
    {
        "id": "finbert_sidecar",
        "title": "FinBERT sidecar validation",
        "status": "completed",
        "priority": "active_research",
        "delivery_mode": "research_sidecar",
        "api_endpoint": "/research/finbert/report",
        "cli_command": "phase-d-finbert",
    },
    {
        "id": "qlib_bridge",
        "title": "Qlib bridge validation",
        "status": "completed",
        "priority": "active_research",
        "delivery_mode": "research_sidecar",
        "api_endpoint": "/research/qlib-bridge/report",
        "cli_command": "phase-d-qlib-bridge",
    },
)


def build_phase_d_status_report(
    *,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    normalized_overrides = overrides if isinstance(overrides, Mapping) else {}
    items: list[dict[str, object]] = []
    for item in _PHASE_D_ITEMS:
        payload: dict[str, object] = {key: value for key, value in item.items()}
        raw_override = normalized_overrides.get(item["id"], {})
        if isinstance(raw_override, Mapping):
            for key in ("status", "owner", "note", "target_release"):
                value = raw_override.get(key)
                if value is not None:
                    payload[key] = value
        items.append(payload)
    return {
        "generated_at": generated_at or datetime.now().isoformat(),
        "phase": "D",
        "overall_status": "completed",
        "items": items,
        "summary": {
            "planned": sum(1 for item in items if str(item.get("status", "")) == "planned"),
            "active": sum(1 for item in items if str(item.get("status", "")) == "active"),
            "completed": sum(1 for item in items if str(item.get("status", "")) == "completed"),
        },
    }


def persist_phase_d_status_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
