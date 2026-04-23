"""Registry for former D6 deferred items that are now delivered as research sidecars."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path


def build_deferred_items_registry() -> dict[str, object]:
    items = [
        _research_item(
            name="TabNet / FT-Transformer",
            category="tabular_deep_model",
            api_endpoint="/research/tabular-deep/report",
            cli_command="phase-d-tabular-deep",
        ),
        _research_item(
            name="TFT",
            category="temporal_deep_model",
            api_endpoint="/research/tft/report",
            cli_command="phase-d-tft",
        ),
        _research_item(
            name="FinRL",
            category="reinforcement_learning",
            api_endpoint="/research/finrl/report",
            cli_command="phase-d-finrl",
        ),
        _research_item(
            name="Heavy End-to-End TS",
            category="heavy_end_to_end_ts",
            api_endpoint="/research/heavy-ts/report",
            cli_command="phase-d-heavy-ts",
        ),
    ]
    return {
        "generated_at": datetime.now().isoformat(),
        "status": "completed_research_registry",
        "scope": "phase_d6",
        "items": items,
    }


def write_deferred_items_registry(
    *, registry: Mapping[str, object], output_path: str | Path
) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(registry), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _research_item(
    *,
    name: str,
    category: str,
    api_endpoint: str,
    cli_command: str,
) -> dict[str, object]:
    return {
        "name": name,
        "category": category,
        "status": "completed",
        "delivery_mode": "research_sidecar",
        "owner": "research_track",
        "api_endpoint": api_endpoint,
        "cli_command": cli_command,
        "notes": [
            "kept as research/shadow capability and does not take over runtime by default",
            "ready for sidecar validation, acceptance, and future backend replacement",
        ],
    }
