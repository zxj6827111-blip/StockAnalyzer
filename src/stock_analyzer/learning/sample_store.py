"""DuckDB-backed sample store for snapshots, outcomes, and manifests."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
    DatasetManifestItem,
    DatasetSplitPlanEntry,
    FeatureCaptureMode,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.feedback_features import (
    has_feedback_feature_columns,
    merge_feedback_feature_vector,
)


class _DuckCursor(Protocol):
    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


class _DuckConnection(Protocol):
    def execute(
        self,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> _DuckCursor: ...

    def close(self) -> None: ...


class SampleStore:
    """Single-writer oriented DuckDB store for learning protocol artifacts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        connection_factory: Callable[[str], _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def write_snapshot(self, snapshot: SignalSnapshot) -> SignalSnapshot:
        """Insert one immutable signal snapshot."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            existing = conn.execute(
                "SELECT snapshot_id FROM signal_snapshots WHERE snapshot_id = ? LIMIT 1",
                [snapshot.snapshot_id],
            ).fetchone()
            if existing is not None:
                raise ValueError(f"snapshot_id already exists: {snapshot.snapshot_id}")
            conn.execute(
                (
                    "INSERT INTO signal_snapshots ("
                    "snapshot_id, schema_version, code_version, symbol, strategy, decision_time, "
                    "feature_vector_json, feature_schema_id, feature_schema_hash, "
                    "feature_capture_mode, feature_observed_at, model_outputs_json, "
                    "score_breakdown_json, risk_context_json, news_context_json, "
                    "regime_context_json, watchlist_source, data_quality_score, "
                    "sample_weight, runtime_config_hash, label_policy_id, "
                    "label_policy_hash, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _snapshot_parameters(snapshot),
            )
            return snapshot
        finally:
            conn.close()

    def get_snapshot(self, snapshot_id: str) -> SignalSnapshot | None:
        """Load one signal snapshot by id."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                f"SELECT {', '.join(_SNAPSHOT_COLUMNS)} "
                "FROM signal_snapshots WHERE snapshot_id = ? LIMIT 1",
                [snapshot_id],
            ).fetchone()
            return None if row is None else _row_to_snapshot(row)
        finally:
            conn.close()

    def enrich_snapshot_contexts(
        self,
        snapshot_id: str,
        *,
        risk_context: Mapping[str, object] | None = None,
        news_context: Mapping[str, object] | None = None,
        regime_context: Mapping[str, object] | None = None,
    ) -> SignalSnapshot | None:
        """Merge post-runtime feedback into one snapshot's JSON context columns."""

        normalized_snapshot_id = str(snapshot_id).strip()
        if not normalized_snapshot_id:
            return None

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                f"SELECT {', '.join(_SNAPSHOT_COLUMNS)} "
                "FROM signal_snapshots WHERE snapshot_id = ? LIMIT 1",
                [normalized_snapshot_id],
            ).fetchone()
            if row is None:
                return None
            snapshot = _row_to_snapshot(row)
            add_feedback_columns = has_feedback_feature_columns(snapshot.feature_vector)
            merged_risk_context = _merge_context_payload(
                snapshot.risk_context,
                risk_context,
            )
            merged_news_context = _merge_context_payload(
                snapshot.news_context,
                news_context,
            )
            merged_regime_context = _merge_context_payload(
                snapshot.regime_context,
                regime_context,
            )
            merged_snapshot = snapshot.model_copy(
                update={
                    "feature_vector": merge_feedback_feature_vector(
                        snapshot.feature_vector,
                        risk_context=merged_risk_context,
                        news_context=merged_news_context,
                        regime_context=merged_regime_context,
                        add_missing_columns=add_feedback_columns,
                    ),
                    "risk_context": merged_risk_context,
                    "news_context": merged_news_context,
                    "regime_context": merged_regime_context,
                },
                deep=True,
            )
            conn.execute(
                (
                    "UPDATE signal_snapshots SET "
                    "feature_vector_json = ?, "
                    "risk_context_json = ?, "
                    "news_context_json = ?, "
                    "regime_context_json = ? "
                    "WHERE snapshot_id = ?"
                ),
                [
                    _dump_json(merged_snapshot.feature_vector),
                    _dump_json(merged_snapshot.risk_context),
                    _dump_json(merged_snapshot.news_context),
                    _dump_json(merged_snapshot.regime_context),
                    normalized_snapshot_id,
                ],
            )
            return merged_snapshot
        finally:
            conn.close()

    def upsert_outcome(self, outcome: OutcomeRecord) -> OutcomeRecord:
        """Insert or replace one evolving outcome record."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute(
                "DELETE FROM outcome_records WHERE snapshot_id = ?",
                [outcome.snapshot_id],
            )
            conn.execute(
                (
                    "INSERT INTO outcome_records ("
                    "snapshot_id, maturity_status, label_mature_time, realized_return, "
                    "max_favorable_excursion, max_adverse_excursion, execution_fill_ratio, "
                    "realized_slippage_bp, reconcile_status, sim_vs_broker_diff, "
                    "outcome_updated_at, last_backfill_at, backfill_fidelity_tier, "
                    "backfill_source, recomputed_feature_schema_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _outcome_parameters(outcome),
            )
            return outcome
        finally:
            conn.close()

    def get_outcome(self, snapshot_id: str) -> OutcomeRecord | None:
        """Load one outcome record by snapshot id."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                f"SELECT {', '.join(_OUTCOME_COLUMNS)} "
                "FROM outcome_records WHERE snapshot_id = ? LIMIT 1",
                [snapshot_id],
            ).fetchone()
            return None if row is None else _row_to_outcome(row)
        finally:
            conn.close()

    def write_manifest(self, manifest: DatasetManifest) -> DatasetManifest:
        """Insert one immutable dataset manifest."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            existing = conn.execute(
                "SELECT dataset_manifest_id "
                "FROM dataset_manifests WHERE dataset_manifest_id = ? LIMIT 1",
                [manifest.dataset_manifest_id],
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    "dataset_manifest_id already exists: "
                    f"{manifest.dataset_manifest_id}"
                )
            conn.execute(
                (
                    "INSERT INTO dataset_manifests ("
                    "dataset_manifest_id, schema_version, source_store_version, "
                    "feature_schema_id, feature_schema_hash, label_policy_id, "
                    "label_policy_hash, sample_selection_rule, time_window_start, "
                    "time_window_end, fidelity_filter_json, included_snapshot_count, "
                    "included_outcome_count, fidelity_breakdown_json, "
                    "dropped_reason_breakdown_json, split_plan_json, generated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _manifest_parameters(manifest),
            )
            return manifest
        finally:
            conn.close()

    def get_manifest(self, dataset_manifest_id: str) -> DatasetManifest | None:
        """Load one dataset manifest by id."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                f"SELECT {', '.join(_MANIFEST_COLUMNS)} "
                "FROM dataset_manifests WHERE dataset_manifest_id = ? LIMIT 1",
                [dataset_manifest_id],
            ).fetchone()
            return None if row is None else _row_to_manifest(row)
        finally:
            conn.close()

    def list_manifests(self, limit: int = 20) -> list[DatasetManifest]:
        """Return recent manifests ordered by generation time descending."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            capped_limit = max(1, int(limit))
            rows = conn.execute(
                f"SELECT {', '.join(_MANIFEST_COLUMNS)} "
                "FROM dataset_manifests "
                "ORDER BY generated_at DESC, dataset_manifest_id DESC "
                "LIMIT ?",
                [capped_limit],
            ).fetchall()
            return [_row_to_manifest(row) for row in rows]
        finally:
            conn.close()

    def replace_manifest_items(
        self,
        dataset_manifest_id: str,
        items: Sequence[DatasetManifestItem],
    ) -> list[DatasetManifestItem]:
        """Replace one manifest's stable membership items in ordinal order."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute(
                "DELETE FROM dataset_manifest_items WHERE dataset_manifest_id = ?",
                [dataset_manifest_id],
            )
            normalized_items: list[DatasetManifestItem] = []
            for item in items:
                normalized = item
                if item.dataset_manifest_id != dataset_manifest_id:
                    normalized = item.model_copy(
                        update={"dataset_manifest_id": dataset_manifest_id}
                    )
                normalized_items.append(normalized)
                conn.execute(
                    (
                        "INSERT INTO dataset_manifest_items ("
                        "dataset_manifest_id, snapshot_id, split_name, ordinal, decision_time"
                        ") VALUES (?, ?, ?, ?, ?)"
                    ),
                    _manifest_item_parameters(normalized),
                )
            return normalized_items
        finally:
            conn.close()

    def list_manifest_items(self, dataset_manifest_id: str) -> list[DatasetManifestItem]:
        """Return stored manifest membership ordered by ordinal."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT {', '.join(_MANIFEST_ITEM_COLUMNS)} "
                "FROM dataset_manifest_items "
                "WHERE dataset_manifest_id = ? "
                "ORDER BY ordinal, decision_time, snapshot_id",
                [dataset_manifest_id],
            ).fetchall()
            return [_row_to_manifest_item(row) for row in rows]
        finally:
            conn.close()

    def list_manifest_snapshot_ids(self, dataset_manifest_id: str) -> list[str]:
        """Return ordered snapshot ids referenced by one manifest."""

        return [
            item.snapshot_id
            for item in self.list_manifest_items(dataset_manifest_id=dataset_manifest_id)
        ]

    def list_snapshots(
        self,
        *,
        snapshot_ids: Sequence[str] | None = None,
        feature_schema_id: str | None = None,
        label_policy_id: str | None = None,
        time_window_start: datetime | None = None,
        time_window_end: datetime | None = None,
    ) -> list[SignalSnapshot]:
        """List snapshots with optional protocol/time filtering."""

        conditions: list[str] = []
        parameters: list[object] = []
        if snapshot_ids:
            normalized_ids = [str(item).strip() for item in snapshot_ids if str(item).strip()]
            if not normalized_ids:
                return []
            placeholders = ", ".join("?" for _ in normalized_ids)
            conditions.append(f"snapshot_id IN ({placeholders})")
            parameters.extend(normalized_ids)
        if feature_schema_id:
            conditions.append("feature_schema_id = ?")
            parameters.append(feature_schema_id)
        if label_policy_id:
            conditions.append("label_policy_id = ?")
            parameters.append(label_policy_id)
        if time_window_start is not None:
            conditions.append("decision_time >= ?")
            parameters.append(_dump_datetime(time_window_start))
        if time_window_end is not None:
            conditions.append("decision_time <= ?")
            parameters.append(_dump_datetime(time_window_end))
        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT {', '.join(_SNAPSHOT_COLUMNS)} "
                f"FROM signal_snapshots{where_clause} "
                "ORDER BY decision_time, snapshot_id",
                parameters,
            ).fetchall()
            return [_row_to_snapshot(row) for row in rows]
        finally:
            conn.close()

    def list_outcomes(
        self,
        *,
        snapshot_ids: Sequence[str] | None = None,
    ) -> list[OutcomeRecord]:
        """List outcomes, optionally restricted to a snapshot-id set."""

        parameters: list[object] = []
        where_clause = ""
        if snapshot_ids:
            normalized_ids = [str(item).strip() for item in snapshot_ids if str(item).strip()]
            if not normalized_ids:
                return []
            placeholders = ", ".join("?" for _ in normalized_ids)
            where_clause = f" WHERE snapshot_id IN ({placeholders})"
            parameters.extend(normalized_ids)

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT {', '.join(_OUTCOME_COLUMNS)} "
                f"FROM outcome_records{where_clause} "
                "ORDER BY outcome_updated_at, snapshot_id",
                parameters,
            ).fetchall()
            return [_row_to_outcome(row) for row in rows]
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        """Return table row counts for coarse validation and health checks."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            snapshot_count = _count_rows(conn, "signal_snapshots")
            outcome_count = _count_rows(conn, "outcome_records")
            manifest_count = _count_rows(conn, "dataset_manifests")
            return {
                "signal_snapshots": snapshot_count,
                "outcome_records": outcome_count,
                "dataset_manifests": manifest_count,
            }
        finally:
            conn.close()

    def list_snapshot_ids(self) -> list[str]:
        """Return snapshot ids ordered by creation time for integration tests and audits."""

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT snapshot_id FROM signal_snapshots ORDER BY created_at, snapshot_id"
            ).fetchall()
            return [str(row[0]) for row in rows if row]
        finally:
            conn.close()

    def _connect(self) -> _DuckConnection:
        return self._connection_factory(str(self._db_path))

    def _ensure_schema(self, conn: _DuckConnection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS signal_snapshots ("
            "snapshot_id VARCHAR PRIMARY KEY, "
            "schema_version VARCHAR NOT NULL, "
            "code_version VARCHAR NOT NULL, "
            "symbol VARCHAR NOT NULL, "
            "strategy VARCHAR NOT NULL, "
            "decision_time VARCHAR NOT NULL, "
            "feature_vector_json VARCHAR NOT NULL, "
            "feature_schema_id VARCHAR NOT NULL, "
            "feature_schema_hash VARCHAR NOT NULL, "
            "feature_capture_mode VARCHAR NOT NULL, "
            "feature_observed_at VARCHAR, "
            "model_outputs_json VARCHAR NOT NULL, "
            "score_breakdown_json VARCHAR NOT NULL, "
            "risk_context_json VARCHAR NOT NULL, "
            "news_context_json VARCHAR NOT NULL, "
            "regime_context_json VARCHAR NOT NULL, "
            "watchlist_source VARCHAR NOT NULL, "
            "data_quality_score DOUBLE, "
            "sample_weight DOUBLE NOT NULL, "
            "runtime_config_hash VARCHAR NOT NULL, "
            "label_policy_id VARCHAR NOT NULL, "
            "label_policy_hash VARCHAR NOT NULL, "
            "created_at VARCHAR NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS outcome_records ("
            "snapshot_id VARCHAR PRIMARY KEY, "
            "maturity_status VARCHAR NOT NULL, "
            "label_mature_time VARCHAR, "
            "realized_return DOUBLE, "
            "max_favorable_excursion DOUBLE, "
            "max_adverse_excursion DOUBLE, "
            "execution_fill_ratio DOUBLE, "
            "realized_slippage_bp DOUBLE, "
            "reconcile_status VARCHAR NOT NULL, "
            "sim_vs_broker_diff DOUBLE, "
            "outcome_updated_at VARCHAR NOT NULL, "
            "last_backfill_at VARCHAR, "
            "backfill_fidelity_tier VARCHAR, "
            "backfill_source VARCHAR NOT NULL, "
            "recomputed_feature_schema_id VARCHAR NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dataset_manifests ("
            "dataset_manifest_id VARCHAR PRIMARY KEY, "
            "schema_version VARCHAR NOT NULL, "
            "source_store_version VARCHAR NOT NULL, "
            "feature_schema_id VARCHAR NOT NULL, "
            "feature_schema_hash VARCHAR NOT NULL, "
            "label_policy_id VARCHAR NOT NULL, "
            "label_policy_hash VARCHAR NOT NULL, "
            "sample_selection_rule VARCHAR NOT NULL, "
            "time_window_start VARCHAR, "
            "time_window_end VARCHAR, "
            "fidelity_filter_json VARCHAR NOT NULL, "
            "included_snapshot_count INTEGER NOT NULL, "
            "included_outcome_count INTEGER NOT NULL, "
            "fidelity_breakdown_json VARCHAR NOT NULL, "
            "dropped_reason_breakdown_json VARCHAR NOT NULL, "
            "split_plan_json VARCHAR NOT NULL, "
            "generated_at VARCHAR NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dataset_manifest_items ("
            "dataset_manifest_id VARCHAR NOT NULL, "
            "snapshot_id VARCHAR NOT NULL, "
            "split_name VARCHAR NOT NULL, "
            "ordinal INTEGER NOT NULL, "
            "decision_time VARCHAR NOT NULL, "
            "PRIMARY KEY(dataset_manifest_id, snapshot_id)"
            ")"
        )


_SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "schema_version",
    "code_version",
    "symbol",
    "strategy",
    "decision_time",
    "feature_vector_json",
    "feature_schema_id",
    "feature_schema_hash",
    "feature_capture_mode",
    "feature_observed_at",
    "model_outputs_json",
    "score_breakdown_json",
    "risk_context_json",
    "news_context_json",
    "regime_context_json",
    "watchlist_source",
    "data_quality_score",
    "sample_weight",
    "runtime_config_hash",
    "label_policy_id",
    "label_policy_hash",
    "created_at",
)

_OUTCOME_COLUMNS = (
    "snapshot_id",
    "maturity_status",
    "label_mature_time",
    "realized_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "execution_fill_ratio",
    "realized_slippage_bp",
    "reconcile_status",
    "sim_vs_broker_diff",
    "outcome_updated_at",
    "last_backfill_at",
    "backfill_fidelity_tier",
    "backfill_source",
    "recomputed_feature_schema_id",
)

_MANIFEST_COLUMNS = (
    "dataset_manifest_id",
    "schema_version",
    "source_store_version",
    "feature_schema_id",
    "feature_schema_hash",
    "label_policy_id",
    "label_policy_hash",
    "sample_selection_rule",
    "time_window_start",
    "time_window_end",
    "fidelity_filter_json",
    "included_snapshot_count",
    "included_outcome_count",
    "fidelity_breakdown_json",
    "dropped_reason_breakdown_json",
    "split_plan_json",
    "generated_at",
)

_MANIFEST_ITEM_COLUMNS = (
    "dataset_manifest_id",
    "snapshot_id",
    "split_name",
    "ordinal",
    "decision_time",
)


def _snapshot_parameters(snapshot: SignalSnapshot) -> list[object]:
    return [
        snapshot.snapshot_id,
        snapshot.schema_version,
        snapshot.code_version,
        snapshot.symbol,
        snapshot.strategy,
        _dump_datetime(snapshot.decision_time),
        _dump_json(snapshot.feature_vector),
        snapshot.feature_schema_id,
        snapshot.feature_schema_hash,
        snapshot.feature_capture_mode.value,
        _dump_optional_datetime(snapshot.feature_observed_at),
        _dump_json(snapshot.model_outputs),
        _dump_json(snapshot.score_breakdown),
        _dump_json(snapshot.risk_context),
        _dump_json(snapshot.news_context),
        _dump_json(snapshot.regime_context),
        snapshot.watchlist_source,
        snapshot.data_quality_score,
        snapshot.sample_weight,
        snapshot.runtime_config_hash,
        snapshot.label_policy_id,
        snapshot.label_policy_hash,
        _dump_datetime(snapshot.created_at),
    ]


def _outcome_parameters(outcome: OutcomeRecord) -> list[object]:
    return [
        outcome.snapshot_id,
        outcome.maturity_status.value,
        _dump_optional_datetime(outcome.label_mature_time),
        outcome.realized_return,
        outcome.max_favorable_excursion,
        outcome.max_adverse_excursion,
        outcome.execution_fill_ratio,
        outcome.realized_slippage_bp,
        outcome.reconcile_status,
        outcome.sim_vs_broker_diff,
        _dump_datetime(outcome.outcome_updated_at),
        _dump_optional_datetime(outcome.last_backfill_at),
        outcome.backfill_fidelity_tier.value if outcome.backfill_fidelity_tier else None,
        outcome.backfill_source,
        outcome.recomputed_feature_schema_id,
    ]


def _manifest_parameters(manifest: DatasetManifest) -> list[object]:
    return [
        manifest.dataset_manifest_id,
        manifest.schema_version,
        manifest.source_store_version,
        manifest.feature_schema_id,
        manifest.feature_schema_hash,
        manifest.label_policy_id,
        manifest.label_policy_hash,
        manifest.sample_selection_rule,
        _dump_optional_datetime(manifest.time_window_start),
        _dump_optional_datetime(manifest.time_window_end),
        _dump_json([item.value for item in manifest.fidelity_filter]),
        manifest.included_snapshot_count,
        manifest.included_outcome_count,
        _dump_json(manifest.fidelity_breakdown),
        _dump_json(manifest.dropped_reason_breakdown),
        _dump_json([item.model_dump(mode="json") for item in manifest.split_plan]),
        _dump_datetime(manifest.generated_at),
    ]


def _manifest_item_parameters(item: DatasetManifestItem) -> list[object]:
    return [
        item.dataset_manifest_id,
        item.snapshot_id,
        item.split_name,
        item.ordinal,
        _dump_datetime(item.decision_time),
    ]


def _row_to_snapshot(row: Sequence[object]) -> SignalSnapshot:
    return SignalSnapshot(
        snapshot_id=str(row[0]),
        schema_version=str(row[1]),
        code_version=str(row[2]),
        symbol=str(row[3]),
        strategy=str(row[4]),
        decision_time=_parse_datetime(row[5]),
        feature_vector=_load_json_dict_float(row[6]),
        feature_schema_id=str(row[7]),
        feature_schema_hash=str(row[8]),
        feature_capture_mode=FeatureCaptureMode(str(row[9])),
        feature_observed_at=_parse_optional_datetime(row[10]),
        model_outputs=_load_json_dict_float(row[11]),
        score_breakdown=_load_json_dict_float(row[12]),
        risk_context=_load_json_dict_any(row[13]),
        news_context=_load_json_dict_any(row[14]),
        regime_context=_load_json_dict_any(row[15]),
        watchlist_source=str(row[16]),
        data_quality_score=_optional_float(row[17]),
        sample_weight=float(row[18]),
        runtime_config_hash=str(row[19]),
        label_policy_id=str(row[20]),
        label_policy_hash=str(row[21]),
        created_at=_parse_datetime(row[22]),
    )


def _row_to_outcome(row: Sequence[object]) -> OutcomeRecord:
    fidelity_raw = row[12]
    return OutcomeRecord(
        snapshot_id=str(row[0]),
        maturity_status=MaturityStatus(str(row[1])),
        label_mature_time=_parse_optional_datetime(row[2]),
        realized_return=_optional_float(row[3]),
        max_favorable_excursion=_optional_float(row[4]),
        max_adverse_excursion=_optional_float(row[5]),
        execution_fill_ratio=_optional_float(row[6]),
        realized_slippage_bp=_optional_float(row[7]),
        reconcile_status=str(row[8]),
        sim_vs_broker_diff=_optional_float(row[9]),
        outcome_updated_at=_parse_datetime(row[10]),
        last_backfill_at=_parse_optional_datetime(row[11]),
        backfill_fidelity_tier=(
            BackfillFidelityTier(str(fidelity_raw)) if fidelity_raw is not None else None
        ),
        backfill_source=str(row[13]),
        recomputed_feature_schema_id=str(row[14]),
    )


def _row_to_manifest(row: Sequence[object]) -> DatasetManifest:
    split_plan_raw = _load_json_list(row[15])
    split_plan = [DatasetSplitPlanEntry.model_validate(item) for item in split_plan_raw]
    return DatasetManifest(
        dataset_manifest_id=str(row[0]),
        schema_version=str(row[1]),
        source_store_version=str(row[2]),
        feature_schema_id=str(row[3]),
        feature_schema_hash=str(row[4]),
        label_policy_id=str(row[5]),
        label_policy_hash=str(row[6]),
        sample_selection_rule=str(row[7]),
        time_window_start=_parse_optional_datetime(row[8]),
        time_window_end=_parse_optional_datetime(row[9]),
        fidelity_filter=[
            BackfillFidelityTier(str(item))
            for item in _load_json_list(row[10])
            if str(item).strip()
        ],
        included_snapshot_count=int(row[11]),
        included_outcome_count=int(row[12]),
        fidelity_breakdown=_load_json_dict_int(row[13]),
        dropped_reason_breakdown=_load_json_dict_int(row[14]),
        split_plan=split_plan,
        generated_at=_parse_datetime(row[16]),
    )


def _row_to_manifest_item(row: Sequence[object]) -> DatasetManifestItem:
    return DatasetManifestItem(
        dataset_manifest_id=str(row[0]),
        snapshot_id=str(row[1]),
        split_name=str(row[2]),
        ordinal=int(row[3]),
        decision_time=_parse_datetime(row[4]),
    )


def _count_rows(conn: _DuckConnection, table_name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    if row is None or not row:
        return 0
    return int(row[0])


def _merge_context_payload(
    existing: Mapping[str, Any],
    updates: Mapping[str, object] | None,
) -> dict[str, Any]:
    merged = {str(key): value for key, value in existing.items()}
    if not updates:
        return merged
    for key, value in updates.items():
        text = str(key).strip()
        if not text:
            continue
        merged[text] = value
    return merged


def _dump_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _load_json_list(value: object) -> list[object]:
    if not isinstance(value, str):
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _load_json_dict_any(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): item for key, item in payload.items()}


def _load_json_dict_float(value: object) -> dict[str, float]:
    payload = _load_json_dict_any(value)
    return {str(key): float(item) for key, item in payload.items()}


def _load_json_dict_int(value: object) -> dict[str, int]:
    payload = _load_json_dict_any(value)
    return {str(key): int(item) for key, item in payload.items()}


def _dump_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _dump_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _dump_datetime(value)


def _parse_datetime(value: object) -> datetime:
    text = str(value).strip()
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _parse_datetime(text)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return float(value)


def _default_connection_factory(database: str) -> _DuckConnection:
    duckdb_module = importlib.import_module("duckdb")
    connection = cast(_DuckConnection, duckdb_module.connect(database=database))
    return connection
