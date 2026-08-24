"""Dataset-manifest builder for stable sample-store training contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta

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

# schema v2 去重契约：同一股票同一交易日只允许一条样本，保留最新快照。
# “最新”按构建前的稳定 ordinal（decision_time、created_at、snapshot_id）取最大值。
DEDUP_KEY = "symbol+trade_date"
DEDUP_RULE = "keep_max_ordinal_latest_snapshot"
# 去重丢弃占比超过该阈值视为 blocking（数据集以重复为主，训练无意义）。
_DUPLICATE_DOMINANCE_RATIO = 0.5
_MANIFEST_SCHEMA_VERSION = "2"


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
        embargo_days: int = 0,
        min_test_split_window_days: int = 0,
        min_test_split_unique_symbol_dates: int = 0,
    ) -> DatasetManifest:
        """Create or reuse one deterministic manifest and persist its membership.

        ``embargo_days`` enables trading-date-grouped splits plus a label-window
        purge: a training/calibration sample whose label matures on or after the
        next split's start is dropped, so its future price path never leaks into
        the evaluation window.  The purge prefers the outcome's real
        ``label_mature_time`` and falls back to ``decision_time + embargo_days``
        natural days only when that field is missing.  ``embargo_days <= 0``
        preserves the legacy row-count split for callers that do not pass a
        horizon.
        """

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
        deduped_pairs, dedup_stats = _deduplicate_by_trading_day(included_pairs)
        blocking_flags, warning_flags = _dedup_quality_flags(
            rows_before=dedup_stats["rows_before"],
            rows_dropped=dedup_stats["rows_dropped"],
        )
        item_blueprints, split_plan = _build_manifest_items_and_split_plan(
            included_pairs=deduped_pairs,
            calibration_ratio=calibration_ratio,
            test_ratio=test_ratio,
            embargo_days=embargo_days,
        )
        manifest_quality = build_manifest_quality_report(
            item_blueprints=item_blueprints,
            snapshots={snapshot.snapshot_id: snapshot for snapshot, _ in deduped_pairs},
            min_test_split_window_days=min_test_split_window_days,
            min_test_split_unique_symbol_dates=min_test_split_unique_symbol_dates,
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
        fidelity_breakdown = _build_fidelity_breakdown(deduped_pairs)
        manifest_id = _build_dataset_manifest_id(
            schema_version=_MANIFEST_SCHEMA_VERSION,
            dedup_key=DEDUP_KEY,
            dedup_rule=DEDUP_RULE,
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
            min_test_split_window_days=min_test_split_window_days,
            min_test_split_unique_symbol_dates=min_test_split_unique_symbol_dates,
        )
        manifest_items = [
            DatasetManifestItem(
                dataset_manifest_id=manifest_id,
                snapshot_id=item_blueprint["snapshot_id"],
                split_name=item_blueprint["split_name"],
                ordinal=_as_int(item_blueprint.get("ordinal")),
                decision_time=item_blueprint["decision_time"],
            )
            for item_blueprint in item_blueprints
        ]
        manifest = DatasetManifest(
            dataset_manifest_id=manifest_id,
            schema_version=_MANIFEST_SCHEMA_VERSION,
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
            included_outcome_count=len(deduped_pairs),
            fidelity_breakdown=fidelity_breakdown,
            dropped_reason_breakdown=dropped_reason_breakdown,
            split_plan=split_plan,
            dedup_key=DEDUP_KEY,
            dedup_rule=DEDUP_RULE,
            rows_before_dedup=dedup_stats["rows_before"],
            rows_dropped_by_dedup=dedup_stats["rows_dropped"],
            blocking_quality_flags=blocking_flags,
            warning_quality_flags=warning_flags,
            manifest_quality_flags=_as_str_list(manifest_quality.get("flags")),
            test_split_window_days=_as_int(
                manifest_quality.get("test_split_window_days")
            ),
            test_split_unique_symbol_dates=_as_int(
                manifest_quality.get("test_split_unique_symbol_dates")
            ),
        )

        existing = self._store.get_manifest(manifest.dataset_manifest_id)
        if existing is not None and existing.schema_version != manifest.schema_version:
            # v2 ID 绝不允许解析到 v1 记录（正常情况下前缀已隔离，此处兜底）。
            raise ValueError(
                "dataset manifest id collision across schema versions: "
                f"{manifest.dataset_manifest_id} stored_schema={existing.schema_version}"
            )
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
    embargo_days: int = 0,
) -> tuple[list[dict[str, object]], list[DatasetSplitPlanEntry]]:
    ordered_pairs = sorted(
        included_pairs,
        key=lambda pair: (pair[0].decision_time, pair[0].snapshot_id),
    )
    if embargo_days <= 0:
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
            items.append(_manifest_item(snapshot, split_name, ordinal))
            split_times.setdefault(split_name, []).append(snapshot.decision_time)
        return items, _build_split_plan(split_times)

    return _build_grouped_split_and_purge(
        ordered_pairs=ordered_pairs,
        calibration_ratio=calibration_ratio,
        test_ratio=test_ratio,
        embargo_days=embargo_days,
    )


def _build_grouped_split_and_purge(
    *,
    ordered_pairs: list[tuple[SignalSnapshot, OutcomeRecord]],
    calibration_ratio: float,
    test_ratio: float,
    embargo_days: int,
) -> tuple[list[dict[str, object]], list[DatasetSplitPlanEntry]]:
    """Label-maturity-grouped split with a structural label-window embargo.

    Samples are grouped by their label maturity date (the outcome's real
    ``label_mature_time``, falling back to ``decision_time + embargo_days``)
    and whole maturity dates are assigned chronologically to
    train/calibration/test.  A sample belongs to the split in which its label
    matures, so a training/calibration sample's label window can never reach
    into a later split — the embargo is structural instead of a post-hoc
    purge, and the calibration set can never be emptied by its own label
    windows.  All rows sharing one maturity date share one split (no
    same-date label leakage across the boundary).
    """
    maturity_order: list[date] = []
    maturity_rows: dict[date, list[tuple[SignalSnapshot, OutcomeRecord]]] = {}
    for snapshot, outcome in ordered_pairs:
        label_mature = outcome.label_mature_time
        maturity_date = (
            label_mature.date()
            if label_mature is not None
            else snapshot.decision_time.date() + timedelta(days=max(1, embargo_days))
        )
        if maturity_date not in maturity_rows:
            maturity_rows[maturity_date] = []
            maturity_order.append(maturity_date)
        maturity_rows[maturity_date].append((snapshot, outcome))
    maturity_order.sort()

    maturity_split = _assign_temporal_splits_by_date(
        dates=maturity_order,
        calibration_ratio=calibration_ratio,
        test_ratio=test_ratio,
    )

    items: list[dict[str, object]] = []
    split_times: dict[str, list[datetime]] = {}
    ordinal = 0
    for maturity_date in maturity_order:
        split_name = maturity_split[maturity_date]
        for snapshot, _outcome in maturity_rows[maturity_date]:
            items.append(_manifest_item(snapshot, split_name, ordinal))
            ordinal += 1
            split_times.setdefault(split_name, []).append(snapshot.decision_time)

    return items, _build_split_plan(split_times)


def _deduplicate_by_trading_day(
    included_pairs: Sequence[tuple[SignalSnapshot, OutcomeRecord]],
) -> tuple[list[tuple[SignalSnapshot, OutcomeRecord]], dict[str, int]]:
    """按 (symbol, trade_date) 去重，保留稳定 ordinal 最大的最新快照。

    先按时间与 snapshot_id 建立稳定 ordinal，再对同一 symbol-day 取最大
    ordinal。这样跨 strategy 的同日重复也会被压缩，manifest 与报告拥有同一
    个样本身份边界。
    """

    if not included_pairs:
        return [], {"rows_before": 0, "rows_dropped": 0}
    ordered_pairs = sorted(
        included_pairs,
        key=lambda pair: (
            pair[0].decision_time,
            pair[0].created_at,
            pair[0].snapshot_id,
        ),
    )
    best: dict[
        tuple[str, date],
        tuple[int, tuple[SignalSnapshot, OutcomeRecord]],
    ] = {}
    for ordinal, pair in enumerate(ordered_pairs):
        snapshot, _outcome = pair
        key = (snapshot.symbol, _decision_date_shanghai(snapshot.decision_time))
        current = best.get(key)
        if current is None or ordinal > current[0]:
            best[key] = (ordinal, pair)
    kept = sorted(
        (pair for _ordinal, pair in best.values()),
        key=lambda pair: (pair[0].decision_time, pair[0].snapshot_id),
    )
    rows_before = len(included_pairs)
    return kept, {"rows_before": rows_before, "rows_dropped": rows_before - len(kept)}


def _decision_date_shanghai(decision_time: datetime) -> date:
    return (decision_time + timedelta(hours=8)).date()


def _dedup_quality_flags(
    *,
    rows_before: int,
    rows_dropped: int,
) -> tuple[list[str], list[str]]:
    """由去重统计推导 blocking/warning 质量旗标。

    - blocking ``duplicate_dominance``：丢弃占比 > 50%，数据集以重复为主；
    - blocking ``empty_after_dedup``：去重后无样本；
    - warning ``duplicate_rows_present``：存在任意被丢弃的重复行。
    （旗标规则为按诊断证据重建的实现细节，规格原文在会话截断中丢失。）
    """

    blocking: list[str] = []
    warning: list[str] = []
    if rows_before > 0:
        if rows_before - rows_dropped <= 0:
            blocking.append("empty_after_dedup")
        elif rows_dropped / rows_before > _DUPLICATE_DOMINANCE_RATIO:
            blocking.append("duplicate_dominance")
    if rows_dropped > 0:
        warning.append("duplicate_rows_present")
    return blocking, warning


def build_manifest_quality_report(
    *,
    item_blueprints: Sequence[Mapping[str, object]],
    snapshots: Mapping[str, SignalSnapshot],
    min_test_split_window_days: int,
    min_test_split_unique_symbol_dates: int,
) -> dict[str, object]:
    """Calculate manifest-generation quality gates from the final membership."""

    test_items = [
        item
        for item in item_blueprints
        if str(item.get("split_name", "")).strip().lower() == "test"
    ]
    test_snapshots = [
        snapshots[str(item.get("snapshot_id", ""))]
        for item in test_items
        if str(item.get("snapshot_id", "")) in snapshots
    ]
    test_trade_dates = [
        _decision_date_shanghai(snapshot.decision_time)
        for snapshot in test_snapshots
    ]
    if test_trade_dates:
        window_days = (max(test_trade_dates) - min(test_trade_dates)).days + 1
    else:
        window_days = 0
    unique_symbol_dates = len(
        {
            (snapshot.symbol, _decision_date_shanghai(snapshot.decision_time))
            for snapshot in test_snapshots
        }
    )
    flags: list[str] = []
    min_window = max(0, int(min_test_split_window_days))
    min_coverage = max(0, int(min_test_split_unique_symbol_dates))
    if min_window > 0 and window_days < min_window:
        flags.append("test_window_too_narrow")
    if min_coverage > 0 and unique_symbol_dates < min_coverage:
        flags.append("test_coverage_insufficient")
    return {
        "flags": flags,
        "test_split_window_days": window_days,
        "test_split_unique_symbol_dates": unique_symbol_dates,
        "min_test_split_window_days": min_window,
        "min_test_split_unique_symbol_dates": min_coverage,
    }


def _manifest_item(snapshot: SignalSnapshot, split_name: str, ordinal: int) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "split_name": split_name,
        "ordinal": ordinal,
        "decision_time": snapshot.decision_time,
    }


def _build_split_plan(split_times: dict[str, list[datetime]]) -> list[DatasetSplitPlanEntry]:
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
    return split_plan


def _assign_temporal_splits_by_date(
    *,
    dates: Sequence[date],
    calibration_ratio: float,
    test_ratio: float,
) -> dict[date, str]:
    """Assign whole trading-date groups chronologically to train/cal/test.

    Date counts (not row counts) drive the ratios so every row of one trading
    date lands in the same split.  Small date counts degrade gracefully to keep
    all three splits non-empty where possible.
    """
    ordered = list(dates)
    total = len(ordered)
    if total == 0:
        return {}
    if total == 1:
        return {ordered[0]: "train"}
    if total == 2:
        return {ordered[0]: "train", ordered[1]: "test"}

    calibration_count = max(1, int(round(total * max(0.0, calibration_ratio))))
    test_count = max(1, int(round(total * max(0.0, test_ratio))))
    while calibration_count + test_count >= total:
        if calibration_count >= test_count and calibration_count > 1:
            calibration_count -= 1
            continue
        if test_count > 1:
            test_count -= 1
            continue
        break
    train_count = total - calibration_count - test_count
    if train_count < 1:
        # Not enough dates for three sets: shrink calibration to keep a train set.
        train_count = 1
        calibration_count = max(0, total - train_count - test_count)

    result: dict[date, str] = {}
    position = 0
    for _ in range(train_count):
        result[ordered[position]] = "train"
        position += 1
    for _ in range(calibration_count):
        result[ordered[position]] = "calibration"
        position += 1
    for _ in range(test_count):
        result[ordered[position]] = "test"
        position += 1
    return result


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
    schema_version: str,
    dedup_key: str,
    dedup_rule: str,
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
    min_test_split_window_days: int = 0,
    min_test_split_unique_symbol_dates: int = 0,
) -> str:
    payload = {
        "schema_version": schema_version,
        "dedup_key": dedup_key,
        "dedup_rule": dedup_rule,
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
        "min_test_split_window_days": max(0, int(min_test_split_window_days)),
        "min_test_split_unique_symbol_dates": max(
            0, int(min_test_split_unique_symbol_dates)
        ),
        "items": [
            {
                "snapshot_id": str(item.get("snapshot_id", "")),
                "split_name": str(item.get("split_name", "")),
                "ordinal": _as_int(item.get("ordinal")),
            }
            for item in item_blueprints
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"dataset_manifest_v{schema_version}_{digest[:12]}"


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, (int, float, str, bytes, bytearray)):
        return int(value)
    return default


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value]
    return []


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
