from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stock_analyzer.config import get_config
from stock_analyzer.learning.sample_schema import MaturityStatus
from stock_analyzer.runtime.service import StockAnalyzerService

STATE_PATH = Path("/app/artifacts/runtime/post_warehouse_followup_state.json")
RESULT_PATH = Path("/app/artifacts/runtime/post_warehouse_followup_result.json")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_state(
    *,
    stage: str,
    status: str,
    payload: dict[str, Any] | None = None,
) -> None:
    _write_json(
        STATE_PATH,
        {
            "updated_at": _now_iso(),
            "stage": stage,
            "status": status,
            "payload": payload or {},
        },
    )


def _pending_snapshot_ids(service: StockAnalyzerService) -> list[str]:
    return sorted(
        {
            outcome.snapshot_id
            for outcome in service._sample_store.list_outcomes()
            if outcome.maturity_status == MaturityStatus.PENDING
        }
    )


def main() -> int:
    result: dict[str, Any] = {
        "started_at": _now_iso(),
        "ok": False,
        "steps": {},
    }
    _write_state(stage="initializing", status="running")

    try:
        config = get_config()
        service = StockAnalyzerService(config=config)

        _write_state(
            stage="week5_scan",
            status="running",
            payload={"sync_top_k_override": 50, "force_universe_scan": True},
        )
        week5_payload = service.run_week5_scan(
            symbols=None,
            notify_enabled=False,
            sync_watchlist=True,
            sync_reason=f"post_warehouse_full_refresh_{datetime.now().strftime('%Y%m%d')}",
            sync_top_k_override=50,
            force_universe_scan=True,
            scan_profile="post_warehouse_full_refresh",
        )
        result["steps"]["week5_scan"] = week5_payload
        _write_state(
            stage="week5_scan",
            status="completed",
            payload={
                "watchlist_synced": True,
                "sync_top_k_override": 50,
                "signal_count": int(len(week5_payload.get("signals", []) or [])),
            },
        )

        pending_ids = _pending_snapshot_ids(service)
        _write_state(
            stage="repair_learning_backfill",
            status="running",
            payload={"pending_snapshot_count": len(pending_ids)},
        )
        if pending_ids:
            repair_payload = service.repair_learning_backfill(
                snapshot_ids=pending_ids,
                as_of=datetime.now(UTC),
                source="post_warehouse_followup",
            )
        else:
            repair_payload = {
                "ok": True,
                "mode": "repair_backfill",
                "skipped": True,
                "reason": "no_pending_snapshot_ids",
                "requested_snapshot_count": 0,
            }
        result["steps"]["repair_learning_backfill"] = repair_payload
        _write_state(
            stage="repair_learning_backfill",
            status="completed",
            payload={
                "pending_snapshot_count": len(pending_ids),
                "repaired_snapshot_count": int(repair_payload.get("repaired_snapshot_count", 0)),
                "promoted_label_matured": int(repair_payload.get("promoted_label_matured", 0)),
                "promoted_fully_matured": int(repair_payload.get("promoted_fully_matured", 0)),
            },
        )

        _write_state(stage="build_trainable_manifest", status="running")
        manifest_payload = service.build_learning_trainable_manifest()
        result["steps"]["build_trainable_manifest"] = manifest_payload

        if not bool(manifest_payload.get("ok", False)):
            _write_state(
                stage="build_trainable_manifest",
                status="fallback",
                payload={"reason": "direct_manifest_build_failed"},
            )
            bootstrap_payload = service.bootstrap_learning_from_runtime_history(build_manifest=True)
            result["steps"]["learning_runtime_history_bootstrap"] = bootstrap_payload
            manifest_payload = dict(bootstrap_payload.get("manifest", {}))
            manifest_payload.setdefault(
                "dataset_manifest_id",
                str(bootstrap_payload.get("dataset_manifest_id", "")),
            )
            manifest_payload.setdefault("ok", bool(bootstrap_payload.get("ok", False)))
        else:
            result["steps"]["learning_runtime_history_bootstrap"] = {
                "ok": True,
                "skipped": True,
                "reason": "direct_manifest_build_succeeded",
            }

        manifest_id = str(manifest_payload.get("dataset_manifest_id", "")).strip()
        if not bool(manifest_payload.get("ok", False)) or not manifest_id:
            raise RuntimeError(
                "trainable_manifest_unavailable: "
                + ",".join(str(item) for item in manifest_payload.get("errors", []) or [])
            )

        _write_state(
            stage="build_trainable_manifest",
            status="completed",
            payload={
                "dataset_manifest_id": manifest_id,
                "included_snapshot_count": int(manifest_payload.get("included_snapshot_count", 0)),
                "included_outcome_count": int(manifest_payload.get("included_outcome_count", 0)),
            },
        )

        _write_state(
            stage="train_learning_manifest",
            status="running",
            payload={"dataset_manifest_id": manifest_id},
        )
        training_payload = service.train_learning_manifest(
            dataset_manifest_id=manifest_id,
            load_predictor=True,
            register_model=True,
        )
        result["steps"]["train_learning_manifest"] = training_payload
        if not bool(training_payload.get("ok", False)):
            raise RuntimeError(
                "train_learning_manifest_failed: "
                + ",".join(str(item) for item in training_payload.get("errors", []) or [])
            )
        model_registry_payload = dict(training_payload.get("model_registry", {}) or {})
        model_id = str(model_registry_payload.get("model_id", "")).strip()
        _write_state(
            stage="train_learning_manifest",
            status="completed",
            payload={
                "dataset_manifest_id": manifest_id,
                "artifact_path": str(training_payload.get("artifact_path", "")),
                "predictor_loaded": bool(training_payload.get("predictor_loaded", False)),
                "registered_model": bool(model_registry_payload.get("registered", False)),
                "model_id": model_id,
            },
        )

        _write_state(
            stage="phase_d_tabular_deep",
            status="running",
            payload={"model_id": model_id},
        )
        if model_id:
            tabular_deep_payload = service.build_phase_d_tabular_deep_report(model_id=model_id)
        else:
            tabular_deep_payload = {
                "ok": False,
                "skipped": True,
                "reason": "model_id_missing_after_manifest_training",
            }
        result["steps"]["phase_d_tabular_deep"] = tabular_deep_payload
        _write_state(
            stage="phase_d_tabular_deep",
            status="completed" if not tabular_deep_payload.get("skipped", False) else "skipped",
            payload={
                "model_id": model_id,
                "output_path": str(tabular_deep_payload.get("output_path", "")),
                "reason": str(tabular_deep_payload.get("reason", "")),
            },
        )

        result["finished_at"] = _now_iso()
        result["ok"] = True
        _write_json(RESULT_PATH, result)
        _write_state(
            stage="completed",
            status="completed",
            payload={
                "dataset_manifest_id": manifest_id,
                "model_id": model_id,
                "training_artifact_path": str(training_payload.get("artifact_path", "")),
                "tabular_deep_output_path": str(tabular_deep_payload.get("output_path", "")),
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        result["finished_at"] = _now_iso()
        result["error"] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        _write_json(RESULT_PATH, result)
        _write_state(
            stage="failed",
            status="failed",
            payload=result["error"],
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
