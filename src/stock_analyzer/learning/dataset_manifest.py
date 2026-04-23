"""Dataset-manifest builder for stable sample-store training contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime

from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
    DatasetManifestItem,
    DatasetSplitPlanEntry,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore

_DEFAULT_MATURITY_STATUSES = (
    MaturityStatus.LABEL_MATURED,
    MaturityStatus.RECONCILED,
    MaturityStatus.FULLY_MATURED,
)


class DatasetManifestBuilder:
    """Build deterministic dataset manifests from one sample store."""

    def __init__(
        self,
        store: SampleStore,
        *,
        source_store_version: str = "learning_store_v1",
        feature_schema_registry: FeatureSchemaRegistry | None = None,
    ) -> None:
        self._store = store
        self._source_store_version = source_store_version.strip() or "learning_store_v1"
        self._feature_schema_registry = feature_schema_registry

    def create_manifest(
        self,
        *,
        feature_schema_id: str,
        feature_schema_hash: str,
        label_policy_id: str,
        label_policy_hash: str,
        snapshot_ids: Sequence[str] | None = None,
        sample_selection_rule: str = "",
        time_window_start: datetime | None = None,
        time_window_end: datetime | None = None,
        fidelity_filter: Sequence[BackfillFidelityTier] | None = None,
        maturity_statuses: Sequence[MaturityStatus] | None = None,
        calibration_ratio: float = 0.1,
        test_ratio: float = 0.1,
    ) -> DatasetManifest:
        """Create or reuse one deterministic manifest and persist its membership."""

        normalized_fidelity = _normalize_fidelity_filter(fidelity_filter)
        normalized_maturity = _normalize_maturity_statuses(maturity_statuses)
        normalized_snapshot_ids = _normalize_snapshot_ids(snapshot_ids)
        allowed_feature_schemas = self._resolve_allowed_feature_schemas(
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
        )
        feature_schema_filter_id = (
            feature_schema_id
            if set(allowed_feature_schemas.keys()) == {feature_schema_id}
            else None
        )
        snapshots = self._store.list_snapshots(
            snapshot_ids=normalized_snapshot_ids or None,
            feature_schema_id=feature_schema_filter_id,
            label_policy_id=label_policy_id,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
        )
        outcome_map = {
            item.snapshot_id: item
            for item in self._store.list_outcomes(
                snapshot_ids=[snapshot.snapshot_id for snapshot in snapshots]
            )
        }
        included_pairs, dropped_reason_breakdown = _select_included_pairs(
            snapshots=snapshots,
            outcome_map=outcome_map,
            allowed_feature_schemas=allowed_feature_schemas,
            label_policy_hash=label_policy_hash,
            fidelity_filter=normalized_fidelity,
            maturity_statuses=normalized_maturity,
        )
        item_blueprints, split_plan = _build_manifest_items_and_split_plan(
            included_pairs=included_pairs,
            calibration_ratio=calibration_ratio,
            test_ratio=test_ratio,
        )
        selection_rule = (
            sample_selection_rule.strip()
            or _build_selection_rule(
                maturity_statuses=normalized_maturity,
                fidelity_filter=normalized_fidelity,
                snapshot_ids=normalized_snapshot_ids,
                time_window_start=time_window_start,
                time_window_end=time_window_end,
            )
        )
        fidelity_breakdown = _build_fidelity_breakdown(included_pairs)
        manifest_id = _build_dataset_manifest_id(
            source_store_version=self._source_store_version,
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            label_policy_id=label_policy_id,
            label_policy_hash=label_policy_hash,
            sample_selection_rule=selection_rule,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            fidelity_filter=normalized_fidelity,
            snapshot_ids=normalized_snapshot_ids,
            item_blueprints=item_blueprints,
        )
        manifest_items = [
            DatasetManifestItem(
                dataset_manifest_id=manifest_id,
                snapshot_id=item_blueprint["snapshot_id"],
                split_name=item_blueprint["split_name"],
                ordinal=int(item_blueprint["ordinal"]),
                decision_time=item_blueprint["decision_time"],
            )
            for item_blueprint in item_blueprints
        ]
        manifest = DatasetManifest(
            dataset_manifest_id=manifest_id,
            source_store_version=self._source_store_version,
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            label_policy_id=label_policy_id,
            label_policy_hash=label_policy_hash,
            sample_selection_rule=selection_rule,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            fidelity_filter=list(normalized_fidelity),
            included_snapshot_count=len(manifest_items),
            included_outcome_count=len(included_pairs),
            fidelity_breakdown=fidelity_breakdown,
            dropped_reason_breakdown=dropped_reason_breakdown,
            split_plan=split_plan,
        )

        existing = self._store.get_manifest(manifest.dataset_manifest_id)
        if existing is None:
            self._store.write_manifest(manifest)
        stored_items = self._store.list_manifest_items(manifest.dataset_manifest_id)
        if not stored_items:
            self._store.replace_manifest_items(manifest.dataset_manifest_id, manifest_items)
        return existing or manifest

    def _resolve_allowed_feature_schemas(
        self,
        *,
        feature_schema_id: str,
        feature_schema_hash: str,
    ) -> dict[str, str]:
        if self._feature_schema_registry is None:
            return {feature_schema_id: feature_schema_hash}
        target = self._feature_schema_registry.get_by_id(feature_schema_id)
        if target is None or target.feature_schema_hash != feature_schema_hash:
            return {feature_schema_id: feature_schema_hash}
        compatible_records = self._feature_schema_registry.resolve_projection_compatible_records(
            feature_schema_id
        )
        allowed = {
            record.feature_schema_id: record.feature_schema_hash
            for record in compatible_records
        }
        return allowed or {feature_schema_id: feature_schema_hash}


def _select_included_pairs(
    *,
    snapshots: Sequence[SignalSnapshot],
    outcome_map: dict[str, OutcomeRecord],
    allowed_feature_schemas: dict[str, str],
    label_policy_hash: str,
    fidelity_filter: Sequence[BackfillFidelityTier],
    maturity_statuses: Sequence[MaturityStatus],
) -> tuple[list[tuple[SignalSnapshot, OutcomeRecord]], dict[str, int]]:
    included: list[tuple[SignalSnapshot, OutcomeRecord]] = []
    dropped: dict[str, int] = {}
    allowed_fidelity = set(fidelity_filter)
    allowed_maturity = set(maturity_statuses)
    for snapshot in snapshots:
        expected_feature_schema_hash = allowed_feature_schemas.get(snapshot.feature_schema_id)
        if expected_feature_schema_hash is None:
            _increment_counter(dropped, "feature_schema_id_mismatch")
            continue
        if snapshot.feature_schema_hash != expected_feature_schema_hash:
            _increment_counter(dropped, "feature_schema_hash_mismatch")
            continue
        if snapshot.label_policy_hash != label_policy_hash:
            _increment_counter(dropped, "label_policy_hash_mismatch")
            continue
        outcome = outcome_map.get(snapshot.snapshot_id)
        if outcome is None:
            _increment_counter(dropped, "missing_outcome")
            continue
        if outcome.maturity_status not in allowed_maturity:
            _increment_counter(dropped, f"maturity_filtered:{outcome.maturity_status.value}")
            continue
        if allowed_fidelity:
            if outcome.backfill_fidelity_tier is None:
                _increment_counter(dropped, "missing_fidelity_tier")
                continue
            if outcome.backfill_fidelity_tier not in allowed_fidelity:
                _increment_counter(
                    dropped,
                    f"fidelity_filtered:{outcome.backfill_fidelity_tier.value}",
                )
                continue
        included.append((snapshot, outcome))
    return included, dropped


def _build_manifest_items_and_split_plan(
    *,
    included_pairs: Sequence[tuple[SignalSnapshot, OutcomeRecord]],
    calibration_ratio: float,
    test_ratio: float,
) -> tuple[list[dict[str, object]], list[DatasetSplitPlanEntry]]:
    ordered_pairs = sorted(
        included_pairs,
        key=lambda pair: (pair[0].decision_time, pair[0].snapshot_id),
    )
    split_names = _assign_temporal_splits(
        total_rows=len(ordered_pairs),
        calibration_ratio=calibration_ratio,
        test_ratio=test_ratio,
    )
    items: list[dict[str, object]] = []
    split_times: dict[str, list[datetime]] = {}
    for ordinal, ((snapshot, _outcome), split_name) in enumerate(
        zip(ordered_pairs, split_names, strict=False)
    ):
        item = {
            "snapshot_id": snapshot.snapshot_id,
            "split_name": split_name,
            "ordinal": ordinal,
            "decision_time": snapshot.decision_time,
        }
        items.append(item)
        split_times.setdefault(split_name, []).append(snapshot.decision_time)

    split_plan: list[DatasetSplitPlanEntry] = []
    for split_name in ("train", "calibration", "test"):
        times = split_times.get(split_name, [])
        if not times:
            continue
        split_plan.append(
            DatasetSplitPlanEntry(
                split_name=split_name,
                selector=f"manifest_items.split_name = '{split_name}'",
                row_count=len(times),
                start_time=min(times),
                end_time=max(times),
            )
        )
    return items, split_plan


def _assign_temporal_splits(
    *,
    total_rows: int,
    calibration_ratio: float,
    test_ratio: float,
) -> list[str]:
    if total_rows <= 0:
        return []
    if total_rows == 1:
        return ["train"]
    if total_rows == 2:
        return ["train", "test"]

    calibration_count = max(1, int(round(total_rows * max(0.0, calibration_ratio))))
    test_count = max(1, int(round(total_rows * max(0.0, test_ratio))))
    while calibration_count + test_count >= total_rows:
        if calibration_count >= test_count and calibration_count > 1:
            calibration_count -= 1
            continue
        if test_count > 1:
            test_count -= 1
            continue
        break

    train_count = max(1, total_rows - calibration_count - test_count)
    overflow = train_count + calibration_count + test_count - total_rows
    if overflow > 0:
        train_count = max(1, train_count - overflow)

    splits = (
        ["train"] * train_count
        + ["calibration"] * calibration_count
        + ["test"] * test_count
    )
    if len(splits) < total_rows:
        splits.extend(["train"] * (total_rows - len(splits)))
    return splits[:total_rows]


def _build_fidelity_breakdown(
    included_pairs: Sequence[tuple[SignalSnapshot, OutcomeRecord]],
) -> dict[str, int]:
    breakdown: dict[str, int] = {}
    for _snapshot, outcome in included_pairs:
        tier = outcome.backfill_fidelity_tier
        key = tier.value if tier is not None else "unknown"
        _increment_counter(breakdown, key)
    return breakdown


def _build_selection_rule(
    *,
    maturity_statuses: Sequence[MaturityStatus],
    fidelity_filter: Sequence[BackfillFidelityTier],
    snapshot_ids: Sequence[str],
    time_window_start: datetime | None,
    time_window_end: datetime | None,
) -> str:
    parts = [
        "maturity_status in ("
        + ", ".join(f"'{status.value}'" for status in maturity_statuses)
        + ")"
    ]
    if snapshot_ids:
        snapshot_scope = hashlib.sha256(
            json.dumps(list(snapshot_ids), ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        parts.append(f"snapshot_scope = 'explicit:{len(snapshot_ids)}:{snapshot_scope}'")
    if fidelity_filter:
        parts.append(
            "backfill_fidelity_tier in ("
            + ", ".join(f"'{tier.value}'" for tier in fidelity_filter)
            + ")"
        )
    if time_window_start is not None:
        parts.append(f"decision_time >= '{time_window_start.isoformat()}'")
    if time_window_end is not None:
        parts.append(f"decision_time <= '{time_window_end.isoformat()}'")
    return " and ".join(parts)


def _build_dataset_manifest_id(
    *,
    source_store_version: str,
    feature_schema_id: str,
    feature_schema_hash: str,
    label_policy_id: str,
    label_policy_hash: str,
    sample_selection_rule: str,
    time_window_start: datetime | None,
    time_window_end: datetime | None,
    fidelity_filter: Sequence[BackfillFidelityTier],
    snapshot_ids: Sequence[str],
    item_blueprints: Sequence[dict[str, object]],
) -> str:
    payload = {
        "source_store_version": source_store_version,
        "feature_schema_id": feature_schema_id,
        "feature_schema_hash": feature_schema_hash,
        "label_policy_id": label_policy_id,
        "label_policy_hash": label_policy_hash,
        "sample_selection_rule": sample_selection_rule,
        "time_window_start": time_window_start.isoformat() if time_window_start else "",
        "time_window_end": time_window_end.isoformat() if time_window_end else "",
        "fidelity_filter": [item.value for item in fidelity_filter],
        "snapshot_ids": list(snapshot_ids),
        "items": [
            {
                "snapshot_id": str(item.get("snapshot_id", "")),
                "split_name": str(item.get("split_name", "")),
                "ordinal": int(item.get("ordinal", 0)),
            }
            for item in item_blueprints
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"dataset_manifest_v1_{digest[:12]}"


def _normalize_fidelity_filter(
    fidelity_filter: Sequence[BackfillFidelityTier] | None,
) -> list[BackfillFidelityTier]:
    normalized: list[BackfillFidelityTier] = []
    seen: set[BackfillFidelityTier] = set()
    for item in fidelity_filter or ():
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _normalize_maturity_statuses(
    maturity_statuses: Sequence[MaturityStatus] | None,
) -> list[MaturityStatus]:
    normalized: list[MaturityStatus] = []
    seen: set[MaturityStatus] = set()
    for item in maturity_statuses or _DEFAULT_MATURITY_STATUSES:
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _normalize_snapshot_ids(snapshot_ids: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in snapshot_ids or ():
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _increment_counter(target: dict[str, int], key: str) -> None:
    target[key] = target.get(key, 0) + 1
