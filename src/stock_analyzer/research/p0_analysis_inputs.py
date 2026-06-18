"""Build read-only P0 analysis inputs from runtime and model artifacts."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from statistics import mean

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.signal.cross_review import evaluate_cross_review

_PROBABILITY_KEYS = ("lgbm", "xgb", "meta")


def build_model_diagnosis_final(
    *,
    model_artifact_path: Path,
    learning_manifest_paths: Sequence[Path] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Summarize trainability and label health without retraining a model."""
    model = _load_json(model_artifact_path)
    manifests = [_load_json(path) for path in learning_manifest_paths or []]
    manifests = [item for item in manifests if item]
    training_metrics = _mapping(model.get("training_metrics"))
    metadata = _mapping(model.get("metadata"))
    train_samples = _int(metadata.get("train_samples"))
    calibration_samples = _int(
        metadata.get("calibration_samples"), fallback=training_metrics.get("calibration_samples")
    )
    test_samples = _int(metadata.get("test_samples"), fallback=training_metrics.get("test_samples"))
    test_positive_rate = _float(training_metrics.get("positive_rate"))
    test_positive = _positive_count(samples=test_samples, positive_rate=test_positive_rate)
    status = _label_health_status(
        samples=test_samples,
        positive=test_positive,
        rate=test_positive_rate,
    )
    return {
        "report_type": "model_diagnosis_final",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": status,
        "source": {
            "model_artifact_path": str(model_artifact_path),
            "learning_manifest_paths": [str(path) for path in learning_manifest_paths or []],
        },
        "model_artifact": {
            "present": bool(model),
            "version": str(model.get("version", "")).strip(),
            "created_at": str(model.get("created_at", "")).strip()
            or str(metadata.get("artifact_created_at", "")).strip(),
            "feature_schema_id": str(model.get("feature_schema_id", "")).strip(),
            "label_policy_id": str(model.get("label_policy_id", "")).strip(),
            "dataset_manifest_id": str(model.get("dataset_manifest_id", "")).strip(),
            "feature_count": len(_list(model.get("feature_columns"))),
            "training_metrics": training_metrics,
            "metadata": metadata,
            "backend": {
                "lgbm": str(_mapping(model.get("lgbm_model")).get("backend", "")).strip(),
                "xgb": str(_mapping(model.get("xgb_model")).get("backend", "")).strip(),
                "degraded_model_mode": bool(metadata.get("degraded_model_mode", False)),
            },
        },
        "label_distribution": {
            "train": {"samples": train_samples},
            "calibration": {"samples": calibration_samples},
            "test": {
                "samples": test_samples,
                "positive": test_positive,
                "positive_rate": test_positive_rate,
            },
        },
        "probability_summary": {
            "meta_mean_prob": _float(training_metrics.get("meta_mean_prob")),
            "lgbm_mean_prob": _float(training_metrics.get("lgbm_mean_prob")),
            "xgb_mean_prob": _float(training_metrics.get("xgb_mean_prob")),
            "mean_prob_spread": _float(training_metrics.get("mean_prob_spread")),
        },
        "learning_manifest_summary": _manifest_summary(manifests),
        "recommended_next_actions": _model_diagnosis_actions(
            status=status,
            metadata=metadata,
            manifests=manifests,
        ),
    }


def build_cross_review_failure_analysis(
    *,
    signals: Sequence[Mapping[str, object]],
    config: StockAnalyzerConfig,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Replay cross-review thresholds against signal snapshots."""
    rows = [_normalize_signal(item) for item in signals]
    rows = [item for item in rows if item.get("symbol")]
    stats = Counter()
    reason_counts: Counter[str] = Counter()
    probability_values: dict[str, list[float]] = {key: [] for key in _PROBABILITY_KEYS}
    near_misses: list[dict[str, object]] = []
    thresholds = {
        "p_lgbm_min": float(config.models.cross_review.p_lgbm_min),
        "p_xgb_min": float(config.models.cross_review.p_xgb_min),
        "p_meta_min": float(config.models.cross_review.p_meta_min),
        "max_diff": float(config.models.cross_review.max_diff),
        "decision_threshold": float(config.walk_forward.decision_threshold),
    }

    for row in rows:
        probabilities = _mapping(row.get("probabilities"))
        parsed = {key: _float(probabilities.get(key)) for key in _PROBABILITY_KEYS}
        if all(value is not None for value in parsed.values()):
            stats["model_output_available"] += 1
            for key, value in parsed.items():
                if value is not None:
                    probability_values[key].append(value)
        else:
            stats["probability_missing"] += 1

        meta = parsed.get("meta")
        if meta is not None and meta >= thresholds["decision_threshold"]:
            stats["total_decision_threshold_pass"] += 1

        if any(value is None for value in parsed.values()):
            continue
        lgbm = float(parsed["lgbm"] or 0.0)
        xgb = float(parsed["xgb"] or 0.0)
        meta = float(parsed["meta"] or 0.0)
        review = evaluate_cross_review(
            lgbm_prob=lgbm,
            xgb_prob=xgb,
            meta_prob=meta,
            config=config.models.cross_review,
        )
        if review.passed:
            stats["total_cross_review_pass"] += 1
        else:
            stats["total_cross_review_fail"] += 1
        for reason in review.reasons:
            reason_counts[reason] += 1

        gaps = _cross_review_gaps(
            lgbm=lgbm,
            xgb=xgb,
            meta=meta,
            thresholds=thresholds,
        )
        if gaps["any_gap"]:
            near_misses.append(
                {
                    "symbol": row["symbol"],
                    "timestamp": row.get("timestamp", ""),
                    "action": row.get("action", ""),
                    "score": _float(row.get("score")),
                    "probabilities": {"lgbm": lgbm, "xgb": xgb, "meta": meta},
                    "model_diff": round(abs(lgbm - xgb), 6),
                    "gap_score": gaps["gap_score"],
                    "reasons": list(review.reasons),
                }
            )

    near_misses = sorted(
        near_misses,
        key=lambda item: (
            float(item.get("gap_score") or 0.0),
            -float(item.get("score") or 0.0),
        ),
    )[:20]
    total = len(rows)
    return {
        "report_type": "p4_cross_review_failure_analysis_v1",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": "ok" if total else "empty",
        "cross_review_analysis": {
            "thresholds": thresholds,
            "gate_statistics": {
                "total_evaluated_rows": total,
                "model_output_available": int(stats["model_output_available"]),
                "probability_missing": int(stats["probability_missing"]),
                "total_decision_threshold_pass": int(stats["total_decision_threshold_pass"]),
                "total_cross_review_pass": int(stats["total_cross_review_pass"]),
                "total_cross_review_fail": int(stats["total_cross_review_fail"]),
                "total_incremental_cross_review_rejection": int(
                    stats["total_decision_threshold_pass"] - stats["total_cross_review_pass"]
                )
                if stats["total_decision_threshold_pass"] >= stats["total_cross_review_pass"]
                else 0,
            },
            "reason_counts": dict(reason_counts),
            "probability_distribution": {
                key: _distribution(values, pass_threshold=_probability_threshold(key, thresholds))
                for key, values in probability_values.items()
            },
            "near_misses": near_misses,
        },
        "recommended_next_actions": [
            {
                "priority": "P0",
                "code": "repair_label_split_before_threshold_change",
                "detail": "Keep production cross-review unchanged until trainability improves.",
            },
            {
                "priority": "P0",
                "code": "shadow_probability_calibration",
                "detail": "Replay meta/xgb calibration buckets offline before gate tuning.",
            },
        ],
    }


def collect_signal_rows(paths: Sequence[Path]) -> list[dict[str, object]]:
    """Collect signal-like rows from latest_signals, runtime_state, week5 reports and jsonl."""
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in paths:
        for payload in _iter_json_payloads(path):
            for row in _extract_signal_rows(payload, source_path=path):
                key = (
                    str(row.get("symbol", "")).strip(),
                    str(row.get("timestamp", "")).strip(),
                    str(row.get("source_path", "")).strip(),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def write_p0_analysis_inputs(
    *,
    analysis_dir: Path,
    model_artifact_path: Path,
    learning_manifest_paths: Sequence[Path],
    signal_source_paths: Sequence[Path],
    config: StockAnalyzerConfig,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Write model diagnosis and cross-review inputs for the P0 shadow planner."""
    generated_at = generated_at or datetime.now()
    analysis_dir.mkdir(parents=True, exist_ok=True)
    signals = collect_signal_rows(signal_source_paths)
    model_diagnosis = build_model_diagnosis_final(
        model_artifact_path=model_artifact_path,
        learning_manifest_paths=learning_manifest_paths,
        generated_at=generated_at,
    )
    cross_review = build_cross_review_failure_analysis(
        signals=signals,
        config=config,
        generated_at=generated_at,
    )
    outputs = {
        "model_diagnosis_final": analysis_dir / "model_diagnosis_final.json",
        "cross_review_failure": analysis_dir / "p4_cross_review_failure_analysis_v1.json",
    }
    _write_json(outputs["model_diagnosis_final"], model_diagnosis)
    _write_json(outputs["cross_review_failure"], cross_review)
    manifest = {
        "report_type": "p0_analysis_inputs_manifest",
        "generated_at": generated_at.isoformat(),
        "production_change_allowed": False,
        "inputs": {
            "model_artifact_path": str(model_artifact_path),
            "learning_manifest_paths": [str(path) for path in learning_manifest_paths],
            "signal_source_paths": [str(path) for path in signal_source_paths],
            "signal_rows": len(signals),
        },
        "outputs": {name: str(path) for name, path in outputs.items()},
        "remaining_expected_inputs": [
            "final_report_v3.json",
            "p4_feature_family_ablation_v1.json",
            "p5_position/position_framework_analysis.json",
        ],
    }
    manifest_path = analysis_dir / "p0_analysis_inputs_manifest.json"
    _write_json(manifest_path, manifest)
    manifest["outputs"]["manifest"] = str(manifest_path)
    return manifest


def _extract_signal_rows(
    payload: Mapping[str, object],
    *,
    source_path: Path,
) -> list[dict[str, object]]:
    source_path_text = str(source_path)
    timestamp = str(payload.get("timestamp", "")).strip()
    rows: list[dict[str, object]] = []

    for item in _list(payload.get("signals")):
        row = _normalize_signal(_mapping(item))
        row.setdefault("timestamp", timestamp)
        row["source_path"] = source_path_text
        rows.append(row)

    latest_signals = _mapping(payload.get("latest_signals"))
    for item in _list(latest_signals.get("signals")):
        row = _normalize_signal(_mapping(item))
        row.setdefault("timestamp", str(latest_signals.get("timestamp", "")).strip() or timestamp)
        row["source_path"] = source_path_text
        rows.append(row)

    for report_key in ("week5_scan_latest", "week5_report"):
        week5 = _mapping(payload.get(report_key))
        if week5:
            rows.extend(_extract_week5_rows(week5, source_path=source_path))

    rows.extend(_extract_week5_rows(payload, source_path=source_path))
    return [item for item in rows if item.get("symbol")]


def _extract_week5_rows(
    payload: Mapping[str, object],
    *,
    source_path: Path,
) -> list[dict[str, object]]:
    timestamp = str(payload.get("timestamp", "")).strip()
    source_path_text = str(source_path)
    raw_rows: list[Mapping[str, object]] = []
    first_board = _mapping(payload.get("first_board"))
    raw_rows.extend(_mapping(item) for item in _list(first_board.get("leaders")))
    raw_rows.extend(_mapping(item) for item in _list(first_board.get("candidates")))
    signal_pool = _mapping(payload.get("signal_pool"))
    raw_rows.extend(_mapping(item) for item in _list(signal_pool.get("candidates")))

    rows: list[dict[str, object]] = []
    for item in raw_rows:
        row = _normalize_signal(item)
        row.setdefault("timestamp", timestamp)
        row["source_path"] = source_path_text
        rows.append(row)
    return rows


def _normalize_signal(signal: Mapping[str, object]) -> dict[str, object]:
    row = dict(signal)
    row["symbol"] = str(row.get("symbol", "")).strip()
    row["action"] = str(row.get("action", "")).strip().lower()
    row["probabilities"] = _mapping(row.get("probabilities"))
    if not row["probabilities"]:
        row["probabilities"] = _mapping(_mapping(row.get("decision_trace")).get("probabilities"))
    if "score" not in row and "shortlist_score" in row:
        row["score"] = row.get("shortlist_score")
    return row


def _iter_json_payloads(path: Path) -> Iterable[Mapping[str, object]]:
    if not path.exists() or not path.is_file():
        return
    if path.suffix.lower() == ".jsonl":
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, Mapping):
                        yield payload
        except OSError:
            return
        return
    payload = _load_json(path)
    if payload:
        yield payload


def _manifest_summary(manifests: Sequence[Mapping[str, object]]) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for manifest in manifests:
        metrics = _mapping(manifest.get("training_metrics"))
        metadata = _mapping(manifest.get("metadata"))
        items.append(
            {
                "created_at": str(manifest.get("created_at", "")).strip()
                or str(metadata.get("artifact_created_at", "")).strip(),
                "dataset_manifest_id": str(manifest.get("dataset_manifest_id", "")).strip(),
                "label_policy_id": str(manifest.get("label_policy_id", "")).strip(),
                "train_samples": _int(metadata.get("train_samples")),
                "calibration_samples": _int(
                    metadata.get("calibration_samples"),
                    fallback=metrics.get("calibration_samples"),
                ),
                "test_samples": _int(
                    metadata.get("test_samples"),
                    fallback=metrics.get("test_samples"),
                ),
                "positive_rate": _float(metrics.get("positive_rate")),
                "auc": _float(metrics.get("auc")),
                "meta_mean_prob": _float(metrics.get("meta_mean_prob")),
                "lgbm_mean_prob": _float(metrics.get("lgbm_mean_prob")),
                "xgb_mean_prob": _float(metrics.get("xgb_mean_prob")),
                "degraded_model_mode": bool(metadata.get("degraded_model_mode", False)),
            }
        )
    return {
        "count": len(items),
        "items": sorted(items, key=lambda item: str(item.get("created_at", ""))),
    }


def _model_diagnosis_actions(
    *,
    status: str,
    metadata: Mapping[str, object],
    manifests: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if status != "usable":
        actions.append(
            {
                "priority": "P0",
                "code": "label_split_trainability_shadow",
                "detail": "Require enough positive labels in test folds before threshold tuning.",
            }
        )
    if bool(metadata.get("degraded_model_mode", False)):
        actions.append(
            {
                "priority": "P0",
                "code": "model_backend_dependency_check",
                "detail": (
                    "Production artifact used fallback model backends; verify "
                    "LightGBM/XGBoost availability before retraining."
                ),
            }
        )
    if manifests:
        manifest_rates = [
            _float(_mapping(item.get("training_metrics")).get("positive_rate"))
            for item in manifests
        ]
        if all((rate or 0.0) <= 0.0 for rate in manifest_rates):
            actions.append(
                {
                    "priority": "P0",
                    "code": "manifest_positive_label_audit",
                    "detail": "Learning manifests also show no positive test labels.",
                }
            )
    return actions


def _cross_review_gaps(
    *,
    lgbm: float,
    xgb: float,
    meta: float,
    thresholds: Mapping[str, float],
) -> dict[str, object]:
    lgbm_gap = max(0.0, float(thresholds["p_lgbm_min"]) - lgbm)
    xgb_gap = max(0.0, float(thresholds["p_xgb_min"]) - xgb)
    meta_gap = max(0.0, float(thresholds["p_meta_min"]) - meta)
    diff_gap = max(0.0, abs(lgbm - xgb) - float(thresholds["max_diff"]))
    gap_score = round(lgbm_gap + xgb_gap + meta_gap + diff_gap, 6)
    return {
        "lgbm_below_min": round(lgbm_gap, 6),
        "xgb_below_min": round(xgb_gap, 6),
        "meta_below_min": round(meta_gap, 6),
        "model_diff_excess": round(diff_gap, 6),
        "gap_score": gap_score,
        "any_gap": gap_score > 0,
    }


def _distribution(values: Sequence[float], *, pass_threshold: float) -> dict[str, object]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"n": 0, "pass": 0}
    return {
        "n": len(ordered),
        "min": round(ordered[0], 6),
        "p10": round(_quantile(ordered, 0.10), 6),
        "p25": round(_quantile(ordered, 0.25), 6),
        "median": round(_quantile(ordered, 0.50), 6),
        "mean": round(mean(ordered), 6),
        "p75": round(_quantile(ordered, 0.75), 6),
        "p90": round(_quantile(ordered, 0.90), 6),
        "max": round(ordered[-1], 6),
        "pass": sum(1 for value in ordered if value >= pass_threshold),
        "pass_threshold": pass_threshold,
    }


def _probability_threshold(key: str, thresholds: Mapping[str, float]) -> float:
    if key == "lgbm":
        return float(thresholds["p_lgbm_min"])
    if key == "xgb":
        return float(thresholds["p_xgb_min"])
    return float(thresholds["p_meta_min"])


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    pos = (len(values) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return float(values[lower])
    weight = pos - lower
    return float(values[lower]) * (1.0 - weight) + float(values[upper]) * weight


def _label_health_status(
    *,
    samples: int,
    positive: int,
    rate: float | None,
) -> str:
    if samples <= 0:
        return "missing_input"
    if rate is None or rate < 0.03 or positive < 5:
        return "needs_label_split_repair"
    return "usable"


def _positive_count(*, samples: int, positive_rate: float | None) -> int:
    if samples <= 0 or positive_rate is None:
        return 0
    return int(round(samples * max(0.0, positive_rate)))


def _load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: object, *, fallback: object | None = None) -> int:
    parsed = _float(value)
    if parsed is None and fallback is not None:
        parsed = _float(fallback)
    return int(parsed) if parsed is not None else 0
