"""Dataset-manifest builder for stable sample-store training contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

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
# 真实时间隔离策略标识（A1）：决策日粒度的标签可用性 purge。
_PURGE_POLICY = "decision_day_label_availability_purge_v1"
# 判据不可满足时置入 manifest_quality_flags，trainer 据此 fail-closed。
_PURGE_INFEASIBLE_FLAG = "label_availability_purge_infeasible"
# 异常延迟成熟样本在报告中保留的样例上限（计数不受此限制）。
_LATE_MATURING_EXAMPLE_LIMIT = 20


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
        item_blueprints, split_plan, isolation_report, isolation_flags = (
            _build_manifest_items_and_split_plan(
                included_pairs=deduped_pairs,
                calibration_ratio=calibration_ratio,
                test_ratio=test_ratio,
                embargo_days=embargo_days,
            )
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
            # included_* 是 purge 之后的成员数；purge 前的配对数见
            # split_isolation_report["rows_before_purge"]，两者相减即剔除规模。
            included_outcome_count=len(manifest_items),
            fidelity_breakdown=fidelity_breakdown,
            dropped_reason_breakdown=dropped_reason_breakdown,
            split_plan=split_plan,
            dedup_key=DEDUP_KEY,
            dedup_rule=DEDUP_RULE,
            rows_before_dedup=dedup_stats["rows_before"],
            rows_dropped_by_dedup=dedup_stats["rows_dropped"],
            blocking_quality_flags=blocking_flags,
            warning_quality_flags=[
                *warning_flags,
                *(
                    ["label_availability_split_degraded"]
                    if isolation_report.get("status") == "isolated_degraded"
                    else []
                ),
            ],
            manifest_quality_flags=[
                *_as_str_list(manifest_quality.get("flags")),
                *isolation_flags,
            ],
            test_split_window_days=_as_int(
                manifest_quality.get("test_split_window_days")
            ),
            test_split_unique_symbol_dates=_as_int(
                manifest_quality.get("test_split_unique_symbol_dates")
            ),
            purged_decision_days=_as_int(isolation_report.get("purged_decision_days")),
            purged_rows=_as_int(isolation_report.get("purged_rows")),
            split_isolation_report=dict(isolation_report),
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
) -> tuple[
    list[dict[str, object]],
    list[DatasetSplitPlanEntry],
    dict[str, object],
    list[str],
]:
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
        return items, _build_split_plan(split_times), {}, []

    return _build_decision_day_split_and_purge(
        ordered_pairs=ordered_pairs,
        calibration_ratio=calibration_ratio,
        test_ratio=test_ratio,
        embargo_days=embargo_days,
    )


def _label_available_time(
    snapshot: SignalSnapshot,
    outcome: OutcomeRecord,
    embargo_days: int,
) -> datetime:
    """单行标签可用时间：优先 outcome 的真实成熟时间，缺失时按 embargo_days 推算。

    ``label_mature_time`` 缺失时只能退化到 ``decision_time + embargo_days``（与
    ``create_manifest`` 的契约一致）；该退化本身就是审计对象，由调用方记账。
    """
    mature = outcome.label_mature_time
    if mature is None:
        mature = snapshot.decision_time + timedelta(days=max(1, embargo_days))
    if mature.tzinfo is None:
        return mature.replace(tzinfo=UTC)
    return mature.astimezone(UTC)


def _decision_day_profile(
    ordered_pairs: Sequence[tuple[SignalSnapshot, OutcomeRecord]],
    *,
    embargo_days: int,
) -> tuple[
    list[date],
    dict[date, list[tuple[SignalSnapshot, OutcomeRecord]]],
    dict[date, datetime],
    dict[date, datetime],
]:
    """按上海决策日聚合截面：最小决策时间与**截面最晚标签可用时间**。

    return_rank 的标签依赖同日整个截面的收益，所以"标签可用时间"不是单行的成熟
    时间，而是该决策日截面的最晚成熟时间——只有这一刻之后，这一天的标签才定稿。
    """
    day_order: list[date] = []
    day_pairs: dict[date, list[tuple[SignalSnapshot, OutcomeRecord]]] = {}
    day_decision: dict[date, datetime] = {}
    day_available: dict[date, datetime] = {}
    for snapshot, outcome in ordered_pairs:
        key = _decision_date_shanghai(snapshot.decision_time)
        available = _label_available_time(snapshot, outcome, embargo_days)
        decision_time = snapshot.decision_time
        if decision_time.tzinfo is None:
            decision_time = decision_time.replace(tzinfo=UTC)
        if key not in day_pairs:
            day_order.append(key)
            day_pairs[key] = []
            day_decision[key] = decision_time
            day_available[key] = available
        day_pairs[key].append((snapshot, outcome))
        day_decision[key] = min(day_decision[key], decision_time)
        day_available[key] = max(day_available[key], available)
    return day_order, day_pairs, day_decision, day_available


def _build_decision_day_split_and_purge(
    *,
    ordered_pairs: list[tuple[SignalSnapshot, OutcomeRecord]],
    calibration_ratio: float,
    test_ratio: float,
    embargo_days: int,
) -> tuple[
    list[dict[str, object]],
    list[DatasetSplitPlanEntry],
    dict[str, object],
    list[str],
]:
    """按决策日切分 + 按标签可用性 purge 整日截面（A1，替代成熟日分组）。

    判据（两侧均取决策日粒度）：相邻两段之间

        max(前段各决策日的截面标签可用时间) < min(后段决策日的决策时间)

    成熟日分组只能保证 ``max(前段成熟日) < min(后段成熟日)``，**不能**保证拟合段
    的标签在预测时已经可知——对 return_rank，标签依赖同日整个截面，所以泄漏单位
    是决策日截面。这里整日剔除不满足判据的截面（不按单行删，避免截面残缺），并
    把剔除行数、剔除决策日数、额外交易日间隔、异常延迟成熟样本全部记账。

    比例（calibration_ratio/test_ratio）是**剔除后可用决策日**上的目标：先定 test，
    再向前取满足判据的 calibration，最后把剩下的合格日给 train。构造保证三段非空；
    若目标尺寸不可行则逐步缩小 test/cal 直到可行（记 ``isolated_degraded``），完全
    不可行时整体拒绝并保留原比例切分供审计（记 ``infeasible``，trainer fail-closed）。
    """
    day_order, day_pairs, day_decision, day_available = _decision_day_profile(
        ordered_pairs,
        embargo_days=embargo_days,
    )
    total_days = len(day_order)
    total_rows = len(ordered_pairs)
    late_maturing_rows, late_maturing_examples = _collect_late_maturing_rows(
        ordered_pairs,
        embargo_days=embargo_days,
    )
    if total_days < 3:
        # 少于 3 个决策日无法构造三段；沿用比例切分并显式说明未做隔离判定。
        day_split = _assign_temporal_splits_by_date(
            dates=day_order,
            calibration_ratio=calibration_ratio,
            test_ratio=test_ratio,
        )
        return _finalize_decision_day_split(
            day_order=day_order,
            day_pairs=day_pairs,
            day_split=day_split,
            day_decision=day_decision,
            day_available=day_available,
            embargo_days=embargo_days,
            total_rows=total_rows,
            late_maturing_count=late_maturing_rows,
            late_maturing_examples=late_maturing_examples,
            status="insufficient_decision_days",
            gap_days={},
            blocking_flags=[],
        )

    n_test_target = max(1, int(round(total_days * max(0.0, test_ratio))))
    n_cal_target = max(1, int(round(total_days * max(0.0, calibration_ratio))))
    while n_test_target + n_cal_target > total_days - 1:
        if n_cal_target > 1:
            n_cal_target -= 1
        elif n_test_target > 1:
            n_test_target -= 1
        else:
            break

    index_of = {day: position for position, day in enumerate(day_order)}
    assignment: dict[date, str] | None = None
    degraded = False
    for test_count in range(n_test_target, 0, -1):
        test_days = day_order[total_days - test_count :]
        test_min_decision = min(day_decision[day] for day in test_days)
        cal_pool = [
            day
            for day in day_order[: total_days - test_count]
            if day_available[day] < test_min_decision
        ]
        for cal_count in range(min(n_cal_target, len(cal_pool)), 0, -1):
            cal_days = cal_pool[len(cal_pool) - cal_count :]
            cal_min_decision = min(day_decision[day] for day in cal_days)
            train_days = [
                day
                for day in day_order[: index_of[cal_days[0]]]
                if day_available[day] < cal_min_decision
            ]
            if not train_days:
                continue
            assignment = {day: "train" for day in train_days}
            assignment.update({day: "calibration" for day in cal_days})
            assignment.update({day: "test" for day in test_days})
            degraded = test_count < n_test_target or cal_count < n_cal_target
            break
        if assignment is not None:
            break

    if assignment is None:
        # 判据在当前数据上不可满足：保留比例切分供审计，并让 trainer fail-closed。
        day_split = _assign_temporal_splits_by_date(
            dates=day_order,
            calibration_ratio=calibration_ratio,
            test_ratio=test_ratio,
        )
        return _finalize_decision_day_split(
            day_order=day_order,
            day_pairs=day_pairs,
            day_split=day_split,
            day_decision=day_decision,
            day_available=day_available,
            embargo_days=embargo_days,
            total_rows=total_rows,
            late_maturing_count=late_maturing_rows,
            late_maturing_examples=late_maturing_examples,
            status="infeasible",
            gap_days={},
            blocking_flags=[_PURGE_INFEASIBLE_FLAG],
        )

    gap_days = _boundary_gap_days(
        day_order=day_order,
        assignment=assignment,
        index_of=index_of,
    )
    return _finalize_decision_day_split(
        day_order=day_order,
        day_pairs=day_pairs,
        day_split=assignment,
        day_decision=day_decision,
        day_available=day_available,
        embargo_days=embargo_days,
        total_rows=total_rows,
        late_maturing_count=late_maturing_rows,
        late_maturing_examples=late_maturing_examples,
        status="isolated_degraded" if degraded else "isolated",
        gap_days=gap_days,
        blocking_flags=[],
    )


def _boundary_gap_days(
    *,
    day_order: Sequence[date],
    assignment: Mapping[date, str],
    index_of: Mapping[date, int],
) -> dict[str, int]:
    """相邻两段之间被剔除的交易日数（额外交易日间隔，purge 的副产品）。"""
    bounds = (("train", "calibration"), ("calibration", "test"))
    gaps: dict[str, int] = {}
    for previous, following in bounds:
        previous_days = [day for day in day_order if assignment.get(day) == previous]
        following_days = [day for day in day_order if assignment.get(day) == following]
        if not previous_days or not following_days:
            gaps[f"{previous}->{following}"] = 0
            continue
        last_previous = max(index_of[day] for day in previous_days)
        first_following = min(index_of[day] for day in following_days)
        gaps[f"{previous}->{following}"] = max(0, first_following - last_previous - 1)
    return gaps


def _finalize_decision_day_split(
    *,
    day_order: Sequence[date],
    day_pairs: Mapping[date, list[tuple[SignalSnapshot, OutcomeRecord]]],
    day_split: Mapping[date, str],
    day_decision: Mapping[date, datetime],
    day_available: Mapping[date, datetime],
    embargo_days: int,
    total_rows: int,
    late_maturing_count: int,
    late_maturing_examples: list[dict[str, object]],
    status: str,
    gap_days: Mapping[str, int],
    blocking_flags: list[str],
) -> tuple[
    list[dict[str, object]],
    list[DatasetSplitPlanEntry],
    dict[str, object],
    list[str],
]:
    items: list[dict[str, object]] = []
    split_times: dict[str, list[datetime]] = {}
    split_days: dict[str, list[date]] = {}
    ordinal = 0
    for day in day_order:
        split_name = day_split.get(day)
        if split_name is None:
            continue
        split_days.setdefault(split_name, []).append(day)
        for snapshot, _outcome in day_pairs[day]:
            items.append(_manifest_item(snapshot, split_name, ordinal))
            ordinal += 1
            split_times.setdefault(split_name, []).append(snapshot.decision_time)

    purged_days = [day for day in day_order if day not in day_split]
    purged_rows = sum(len(day_pairs[day]) for day in purged_days)
    boundaries = _boundary_isolation_checks(
        day_order=day_order,
        day_split=day_split,
        day_decision=day_decision,
        day_available=day_available,
        day_pairs=day_pairs,
        gap_days=gap_days,
        index_of={day: position for position, day in enumerate(day_order)},
    )
    report: dict[str, object] = {
        "policy": _PURGE_POLICY,
        "status": status,
        "embargo_days": int(embargo_days),
        "decision_days_total": len(day_order),
        "rows_before_purge": total_rows,
        "purged_decision_days": len(purged_days),
        "purged_rows": purged_rows,
        "embargo_gap_trading_days": dict(gap_days),
        "late_maturing_row_count": late_maturing_count,
        "late_maturing_examples": late_maturing_examples,
        "splits": _split_isolation_metrics(
            day_order=day_order,
            day_split=day_split,
            day_decision=day_decision,
            day_available=day_available,
            split_days=split_days,
        ),
        "boundaries": boundaries,
        "violations": sum(1 for item in boundaries if not item["satisfied"]),
    }
    if report["violations"] and _PURGE_INFEASIBLE_FLAG not in blocking_flags:
        # 无法证明标签可用性隔离（判据不可满足、或决策日不足三段）→ fail-closed，
        # 不允许只在报告里留痕却让训练继续。
        blocking_flags = [*blocking_flags, _PURGE_INFEASIBLE_FLAG]
    return items, _build_split_plan(split_times), report, blocking_flags


def _split_isolation_metrics(
    *,
    day_order: Sequence[date],
    day_split: Mapping[date, str],
    day_decision: Mapping[date, datetime],
    day_available: Mapping[date, datetime],
    split_days: Mapping[str, list[date]],
) -> dict[str, object]:
    """四项目报告：决策日自然日跨度、有效交易日数、成熟日范围、标签可用性边界。"""
    metrics: dict[str, object] = {}
    for split_name in ("train", "calibration", "test"):
        days = sorted(split_days.get(split_name, []))
        if not days:
            metrics[split_name] = {"effective_trading_days": 0}
            continue
        metrics[split_name] = {
            "effective_trading_days": len(days),
            "decision_span_calendar_days": (days[-1] - days[0]).days + 1,
            "min_decision_time": min(day_decision[day] for day in days).isoformat(),
            "max_decision_time": max(day_decision[day] for day in days).isoformat(),
            "maturity_range": [
                min(day_available[day] for day in days).date().isoformat(),
                max(day_available[day] for day in days).date().isoformat(),
            ],
            "max_label_available_time": max(day_available[day] for day in days).isoformat(),
        }
    return metrics


def _boundary_isolation_checks(
    *,
    day_order: Sequence[date],
    day_split: Mapping[date, str],
    day_decision: Mapping[date, datetime],
    day_available: Mapping[date, datetime],
    day_pairs: Mapping[date, list[tuple[SignalSnapshot, OutcomeRecord]]],
    gap_days: Mapping[str, int],
    index_of: Mapping[date, int],
) -> list[dict[str, object]]:
    """逐边界判据：max(前段截面标签可用时间) < min(后段决策时间)。"""
    checks: list[dict[str, object]] = []
    previous_days_by_split: dict[str, list[date]] = {"train": [], "calibration": []}
    following_days_by_split: dict[str, list[date]] = {"calibration": [], "test": []}
    for day in day_order:
        split_name = day_split.get(day)
        if split_name in previous_days_by_split:
            previous_days_by_split[split_name].append(day)
        if split_name in following_days_by_split:
            following_days_by_split[split_name].append(day)
    for previous, following in (("train", "calibration"), ("calibration", "test")):
        previous_days = previous_days_by_split[previous]
        following_days = following_days_by_split[following]
        if not previous_days or not following_days:
            checks.append(
                {
                    "name": f"{previous}->{following}",
                    "satisfied": False,
                    "reason": "empty_split",
                    "purged_decision_days": 0,
                    "purged_rows": 0,
                    "gap_trading_days": int(gap_days.get(f"{previous}->{following}", 0)),
                }
            )
            continue
        previous_max_available = max(day_available[day] for day in previous_days)
        following_min_decision = min(day_decision[day] for day in following_days)
        # 该边界负责剔除的截面：两段之间被整体剔除的决策日。
        last_previous = max(index_of[day] for day in previous_days)
        first_following = min(index_of[day] for day in following_days)
        gap_slice = day_order[last_previous + 1 : first_following]
        boundary_purged = [day for day in gap_slice if day not in day_split]
        checks.append(
            {
                "name": f"{previous}->{following}",
                "prev_max_label_available": previous_max_available.isoformat(),
                "next_min_decision": following_min_decision.isoformat(),
                "satisfied": bool(previous_max_available < following_min_decision),
                "purged_decision_days": len(boundary_purged),
                "purged_rows": sum(len(day_pairs[day]) for day in boundary_purged),
                "gap_trading_days": int(gap_days.get(f"{previous}->{following}", 0)),
            }
        )
    return checks


def _collect_late_maturing_rows(
    ordered_pairs: Sequence[tuple[SignalSnapshot, OutcomeRecord]],
    *,
    embargo_days: int,
) -> tuple[int, list[dict[str, object]]]:
    """异常延迟成熟样本：标签可用时间晚于 ``decision_time + embargo_days`` 的行。

    这类行会让"名义 horizon 决定间隔"的假设失效，必须计数并留样，否则 purge
    规模无法解释。
    """
    count = 0
    examples: list[dict[str, object]] = []
    for snapshot, outcome in ordered_pairs:
        available = _label_available_time(snapshot, outcome, embargo_days)
        nominal = snapshot.decision_time + timedelta(days=max(1, embargo_days))
        if nominal.tzinfo is None:
            nominal = nominal.replace(tzinfo=UTC)
        if available <= nominal:
            continue
        count += 1
        if len(examples) < _LATE_MATURING_EXAMPLE_LIMIT:
            examples.append(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "decision_time": snapshot.decision_time.isoformat(),
                    "label_available_time": available.isoformat(),
                    "nominal_available_time": nominal.isoformat(),
                }
            )
    return count, examples


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


def decision_date_shanghai(decision_time: datetime) -> date:
    """决策时刻所属的「上海交易日」（全链路唯一定义）。

    同时被 manifest 质量报告、A1 决策日粒度标签可用性 purge、以及行数 cap 的
    按日分层使用。三处必须共用同一个「一天」，否则 cap 认定的「一日截面」会与
    purge/split 认定的「一个决策日」错位——那正是 2026-09-14 测试窗恒窄问题的
    温床（cap 按自然日切、purge 按上海日切）。
    """
    return (decision_time + timedelta(hours=8)).date()


# 历史私有名别名：既有调用方与测试按私有名导入，保留以免破坏。
_decision_date_shanghai = decision_date_shanghai


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
