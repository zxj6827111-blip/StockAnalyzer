from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.evolution.shadow_online_v2_metrics_store import (
    ShadowOnlineV2MetricsStore,
)


def test_shadow_online_v2_metrics_store_appends_and_lists_recent_runs(tmp_path: Path) -> None:
    path = tmp_path / "shadow_v2" / "metrics.jsonl"
    store = ShadowOnlineV2MetricsStore(path)

    store.append_run(
        result={
            "engine": "protocol_shadow_online_v2_lr",
            "status": "updated",
            "samples_considered": 12,
            "samples_used": 10,
            "metrics": {"valid_samples": 10, "updates_applied": 10},
            "reasons": ["comparison:shadow_better"],
        },
        now=datetime(2026, 3, 25, 10, 0, tzinfo=UTC),
        metadata={"run_id": "r1"},
    )
    store.append_run(
        result={
            "engine": "protocol_shadow_online_v2_lr",
            "status": "updated",
            "samples_considered": 15,
            "samples_used": 11,
            "metrics": {"valid_samples": 11, "updates_applied": 11},
            "reasons": ["comparison:shadow_flat"],
        },
        now=datetime(2026, 3, 25, 11, 0, tzinfo=UTC),
        metadata={"run_id": "r2"},
    )

    recent = store.list_recent(limit=2)
    status = store.status()

    assert len(recent) == 2
    assert recent[0]["metadata"]["run_id"] == "r2"
    assert recent[1]["metadata"]["run_id"] == "r1"
    assert status["exists"] is True
    assert status["records"] == 2
    assert status["last_valid_samples"] == 11
    assert status["last_updates_applied"] == 11


def test_shadow_online_v2_metrics_store_skips_invalid_or_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "shadow_v2" / "metrics.jsonl"
    store = ShadowOnlineV2MetricsStore(path)

    assert store.list_recent(limit=5) == []
    assert store.status()["records"] == 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{invalid}\n\n", encoding="utf-8")

    assert store.list_recent(limit=5) == []
    assert store.status()["last_valid_samples"] == 0
