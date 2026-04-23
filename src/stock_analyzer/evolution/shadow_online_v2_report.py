"""Detailed per-sample report for shadow-online v2 runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from stock_analyzer.evolution.champion_shadow_report import (
    ChampionShadowReportBuilder,
)
from stock_analyzer.evolution.modules.m11_shadow_loader import M11ShadowObservation
from stock_analyzer.evolution.modules.m11_shadow_portfolio import evaluate_m11_shadow_portfolio
from stock_analyzer.evolution.modules.shadow_online_model_v2 import (
    run_shadow_online_model_v2,
    score_shadow_online_model_v2_record,
    shadow_online_v2_result_to_dict,
)
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.registry import ModelRegistry

_EFFECTIVE_MATURE_STATUSES = {"reconciled", "fully_matured"}


@dataclass(slots=True)
class ShadowOnlineV2ReportRow:
    """One per-sample v2 evaluation row."""

    snapshot_id: str
    symbol: str
    trade_date: str
    split_name: str
    label: float
    realized_return: float
    execution_fill_ratio: float | None
    realized_slippage_bp: float | None
    champion_probability: float
    shadow_probability: float
    shadow_v2_probability: float
    champion_signal: int
    shadow_signal: int
    shadow_v2_signal: int
    champion_shadow_return: float
    shadow_shadow_return: float
    shadow_v2_return: float
    champion_abs_error: float
    shadow_abs_error: float
    shadow_v2_abs_error: float
    shadow_v2_calibration_gain: float
    shadow_v2_brier_gain: float

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "split_name": self.split_name,
            "label": self.label,
            "realized_return": self.realized_return,
            "execution_fill_ratio": self.execution_fill_ratio,
            "realized_slippage_bp": self.realized_slippage_bp,
            "champion_probability": self.champion_probability,
            "shadow_probability": self.shadow_probability,
            "shadow_v2_probability": self.shadow_v2_probability,
            "champion_signal": self.champion_signal,
            "shadow_signal": self.shadow_signal,
            "shadow_v2_signal": self.shadow_v2_signal,
            "champion_shadow_return": self.champion_shadow_return,
            "shadow_shadow_return": self.shadow_shadow_return,
            "shadow_v2_return": self.shadow_v2_return,
            "champion_abs_error": self.champion_abs_error,
            "shadow_abs_error": self.shadow_abs_error,
            "shadow_v2_abs_error": self.shadow_v2_abs_error,
            "shadow_v2_calibration_gain": self.shadow_v2_calibration_gain,
            "shadow_v2_brier_gain": self.shadow_v2_brier_gain,
        }

    def preview_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "split_name": self.split_name,
            "label": self.label,
            "shadow_probability": self.shadow_probability,
            "shadow_v2_probability": self.shadow_v2_probability,
            "shadow_v2_calibration_gain": self.shadow_v2_calibration_gain,
            "shadow_v2_return": self.shadow_v2_return,
        }


@dataclass(slots=True)
class ShadowOnlineV2Report:
    """Detailed v2 report with return, calibration, and execution summaries."""

    report_id: str
    champion_model_id: str
    shadow_model_id: str
    comparison_report_id: str
    dataset_manifest_id: str
    status: str
    engine: str
    generated_at: str
    signal_threshold: float
    rows: list[ShadowOnlineV2ReportRow] = field(default_factory=list)
    run_result: dict[str, object] = field(default_factory=dict)
    return_summary: dict[str, float] = field(default_factory=dict)
    calibration_summary: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    execution_summary: dict[str, float] = field(default_factory=dict)
    m11_v2_report: dict[str, object] = field(default_factory=dict)

    def to_dict(
        self,
        *,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        payload = {
            "report_id": self.report_id,
            "champion_model_id": self.champion_model_id,
            "shadow_model_id": self.shadow_model_id,
            "comparison_report_id": self.comparison_report_id,
            "dataset_manifest_id": self.dataset_manifest_id,
            "status": self.status,
            "engine": self.engine,
            "generated_at": self.generated_at,
            "signal_threshold": self.signal_threshold,
            "row_count": len(self.rows),
            "preview": [row.preview_dict() for row in self.rows[: max(1, int(preview_limit))]],
            "run_result": dict(self.run_result),
            "return_summary": dict(self.return_summary),
            "calibration_summary": dict(self.calibration_summary),
            "execution_summary": dict(self.execution_summary),
            "m11_v2_report": dict(self.m11_v2_report),
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


class ShadowOnlineV2ReportBuilder:
    """Build one detailed v2 report on top of champion-vs-shadow comparison rows."""

    def __init__(
        self,
        *,
        store: SampleStore,
        model_registry: ModelRegistry,
        feature_schema_registry: FeatureSchemaRegistry | None = None,
        label_policy_registry: LabelPolicyRegistry | None = None,
    ) -> None:
        self._comparison_builder = ChampionShadowReportBuilder(
            store=store,
            model_registry=model_registry,
            feature_schema_registry=feature_schema_registry,
            label_policy_registry=label_policy_registry,
        )

    def build_report(
        self,
        *,
        shadow_model_id: str,
        champion_model_id: str = "",
        now: datetime | None = None,
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        previous_state: Mapping[str, object] | None = None,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        preview_limit: int = 5,
    ) -> ShadowOnlineV2Report:
        run_now = _as_utc_datetime(now or datetime.now(UTC))
        comparison_report = self._comparison_builder.build_report(
            shadow_model_id=shadow_model_id,
            champion_model_id=champion_model_id,
            split_names=split_names,
            max_rows=max_rows,
            signal_threshold=signal_threshold,
        )

        ordered_rows = _order_comparison_rows(comparison_report.rows, now=run_now)
        if max_samples is not None and int(max_samples) > 0:
            ordered_rows = ordered_rows[: int(max_samples)]
        raw_records = [row.to_dict() for row in ordered_rows]
        run_result = run_shadow_online_model_v2(
            records=raw_records,
            now=run_now,
            previous_state=previous_state,
            max_samples=len(raw_records) if raw_records else 1,
            min_samples=max(1, int(min_samples)),
            learning_rate=learning_rate,
            preview_limit=preview_limit,
            signal_threshold=signal_threshold,
        )
        run_payload = shadow_online_v2_result_to_dict(run_result)

        rows: list[ShadowOnlineV2ReportRow] = []
        observations: list[M11ShadowObservation] = []
        for item in ordered_rows[: run_result.samples_used]:
            raw_record = item.to_dict()
            shadow_v2_probability = score_shadow_online_model_v2_record(
                record=raw_record,
                state=run_result.state,
            )
            champion_probability = float(item.champion_scores.get("p_meta", 0.5))
            shadow_probability = float(item.shadow_scores.get("p_meta", 0.5))
            champion_signal = int(champion_probability >= signal_threshold)
            shadow_signal = int(shadow_probability >= signal_threshold)
            shadow_v2_signal = int(shadow_v2_probability >= signal_threshold)
            realized_return = float(item.realized_return)
            label = float(item.label)
            row = ShadowOnlineV2ReportRow(
                snapshot_id=item.snapshot_id,
                symbol=item.symbol,
                trade_date=item.trade_date,
                split_name=item.split_name,
                label=label,
                realized_return=realized_return,
                execution_fill_ratio=item.execution_fill_ratio,
                realized_slippage_bp=item.realized_slippage_bp,
                champion_probability=round(champion_probability, 6),
                shadow_probability=round(shadow_probability, 6),
                shadow_v2_probability=round(shadow_v2_probability, 6),
                champion_signal=champion_signal,
                shadow_signal=shadow_signal,
                shadow_v2_signal=shadow_v2_signal,
                champion_shadow_return=round(realized_return * champion_signal, 6),
                shadow_shadow_return=round(realized_return * shadow_signal, 6),
                shadow_v2_return=round(realized_return * shadow_v2_signal, 6),
                champion_abs_error=round(abs(champion_probability - label), 6),
                shadow_abs_error=round(abs(shadow_probability - label), 6),
                shadow_v2_abs_error=round(abs(shadow_v2_probability - label), 6),
                shadow_v2_calibration_gain=round(
                    abs(shadow_probability - label) - abs(shadow_v2_probability - label),
                    6,
                ),
                shadow_v2_brier_gain=round(
                    (shadow_probability - label) ** 2 - (shadow_v2_probability - label) ** 2,
                    6,
                ),
            )
            rows.append(row)
            observations.append(
                M11ShadowObservation(
                    symbol=row.symbol,
                    champion_shadow_return=row.champion_shadow_return,
                    challenger_shadow_return=row.shadow_v2_return,
                    champion_signal=row.champion_signal,
                    challenger_signal=row.shadow_v2_signal,
                )
            )

        m11_result = evaluate_m11_shadow_portfolio(shadow_observations=observations)
        report_id = _build_report_id(
            comparison_report_id=comparison_report.comparison_report_id,
            engine=run_result.engine,
            rows=rows,
        )
        return ShadowOnlineV2Report(
            report_id=report_id,
            champion_model_id=comparison_report.champion_model_id,
            shadow_model_id=comparison_report.shadow_model_id,
            comparison_report_id=comparison_report.comparison_report_id,
            dataset_manifest_id=comparison_report.dataset_manifest_id,
            status=run_result.status,
            engine=run_result.engine,
            generated_at=run_now.isoformat(),
            signal_threshold=float(signal_threshold),
            rows=rows,
            run_result=run_payload,
            return_summary=_build_return_summary(rows),
            calibration_summary=_build_calibration_summary(rows),
            execution_summary=_build_execution_summary(rows),
            m11_v2_report={
                "score": float(m11_result.score),
                "status": m11_result.status,
                "redlines": dict(m11_result.redlines),
                "metrics": {
                    "valid_samples": int(m11_result.metrics.valid_samples),
                    "champion_cum_return": float(m11_result.metrics.champion_cum_return),
                    "challenger_cum_return": float(m11_result.metrics.challenger_cum_return),
                    "drawdown_delta": float(m11_result.metrics.drawdown_delta),
                    "tail_loss_delta": float(m11_result.metrics.tail_loss_delta),
                    "execution_divergence_ratio": float(
                        m11_result.metrics.execution_divergence_ratio
                    ),
                },
            },
        )


def _order_comparison_rows(
    rows: Sequence[object],
    *,
    now: datetime,
) -> list[object]:
    filtered = []
    normalized_now = _normalize_datetime(now)
    for row in rows:
        if not _row_is_effectively_mature(row=row, normalized_now=normalized_now):
            continue
        filtered.append(row)
    return sorted(
        filtered,
        key=lambda item: (
            str(getattr(item, "label_mature_time", "")),
            str(getattr(item, "trade_date", "")),
            str(getattr(item, "symbol", "")),
        ),
    )


def _row_is_effectively_mature(
    *,
    row: object,
    normalized_now: datetime,
) -> bool:
    maturity_status = str(getattr(row, "maturity_status", "")).strip().lower()
    if maturity_status in _EFFECTIVE_MATURE_STATUSES:
        return True
    mature_text = getattr(row, "label_mature_time", "")
    mature_dt = _parse_datetime_text(mature_text)
    if mature_dt is None:
        return True
    return mature_dt <= normalized_now


def _build_return_summary(rows: Sequence[ShadowOnlineV2ReportRow]) -> dict[str, float]:
    champion_returns = [row.champion_shadow_return for row in rows]
    shadow_returns = [row.shadow_shadow_return for row in rows]
    v2_returns = [row.shadow_v2_return for row in rows]
    return {
        "champion_cum_return": round(_compound_returns(champion_returns), 6),
        "shadow_cum_return": round(_compound_returns(shadow_returns), 6),
        "shadow_v2_cum_return": round(_compound_returns(v2_returns), 6),
        "shadow_v2_minus_shadow_return": round(
            _compound_returns(v2_returns) - _compound_returns(shadow_returns),
            6,
        ),
        "shadow_v2_minus_champion_return": round(
            _compound_returns(v2_returns) - _compound_returns(champion_returns),
            6,
        ),
    }


def _build_calibration_summary(
    rows: Sequence[ShadowOnlineV2ReportRow],
) -> dict[str, list[dict[str, object]]]:
    buckets = {
        "champion": _calibration_buckets(
            probabilities=[row.champion_probability for row in rows],
            labels=[row.label for row in rows],
        ),
        "shadow": _calibration_buckets(
            probabilities=[row.shadow_probability for row in rows],
            labels=[row.label for row in rows],
        ),
        "shadow_v2": _calibration_buckets(
            probabilities=[row.shadow_v2_probability for row in rows],
            labels=[row.label for row in rows],
        ),
    }
    return buckets


def _calibration_buckets(
    *,
    probabilities: Sequence[float],
    labels: Sequence[float],
    bucket_count: int = 5,
) -> list[dict[str, object]]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(max(1, int(bucket_count)))]
    for probability, label in zip(probabilities, labels, strict=False):
        index = min(len(buckets) - 1, max(0, int(float(probability) * len(buckets))))
        buckets[index].append((float(probability), float(label)))
    summary: list[dict[str, object]] = []
    for index, items in enumerate(buckets):
        if not items:
            summary.append(
                {
                    "bucket": index,
                    "count": 0,
                    "avg_probability": 0.0,
                    "observed_rate": 0.0,
                }
            )
            continue
        summary.append(
            {
                "bucket": index,
                "count": len(items),
                "avg_probability": round(sum(item[0] for item in items) / len(items), 6),
                "observed_rate": round(sum(item[1] for item in items) / len(items), 6),
            }
        )
    return summary


def _build_execution_summary(rows: Sequence[ShadowOnlineV2ReportRow]) -> dict[str, float]:
    champion_exec = [row for row in rows if row.champion_signal > 0]
    shadow_exec = [row for row in rows if row.shadow_signal > 0]
    v2_exec = [row for row in rows if row.shadow_v2_signal > 0]
    changed_exec = [row for row in rows if row.shadow_signal != row.shadow_v2_signal]
    return {
        "champion_avg_fill_ratio": _avg_optional([row.execution_fill_ratio for row in champion_exec]),
        "shadow_avg_fill_ratio": _avg_optional([row.execution_fill_ratio for row in shadow_exec]),
        "shadow_v2_avg_fill_ratio": _avg_optional([row.execution_fill_ratio for row in v2_exec]),
        "changed_trade_avg_fill_ratio": _avg_optional(
            [row.execution_fill_ratio for row in changed_exec]
        ),
        "champion_avg_slippage_bp": _avg_optional(
            [row.realized_slippage_bp for row in champion_exec]
        ),
        "shadow_avg_slippage_bp": _avg_optional([row.realized_slippage_bp for row in shadow_exec]),
        "shadow_v2_avg_slippage_bp": _avg_optional([row.realized_slippage_bp for row in v2_exec]),
        "changed_trade_avg_slippage_bp": _avg_optional(
            [row.realized_slippage_bp for row in changed_exec]
        ),
        "shadow_v2_signal_divergence_ratio": round(
            (
                sum(1.0 for row in rows if row.shadow_signal != row.shadow_v2_signal) / len(rows)
                if rows
                else 0.0
            ),
            6,
        ),
    }


def _avg_optional(values: Sequence[float | None]) -> float:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return 0.0
    return round(sum(numeric) / len(numeric), 6)


def _compound_returns(values: Sequence[float]) -> float:
    equity = 1.0
    for value in values:
        equity *= 1.0 + float(value)
    return equity - 1.0


def _build_report_id(
    *,
    comparison_report_id: str,
    engine: str,
    rows: Sequence[ShadowOnlineV2ReportRow],
) -> str:
    payload = {
        "comparison_report_id": comparison_report_id,
        "engine": engine,
        "rows": [
            {
                "snapshot_id": row.snapshot_id,
                "trade_date": row.trade_date,
            }
            for row in rows
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"shadow_online_v2_report_v1_{digest[:12]}"


def _parse_datetime_text(value: object) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(value.tzinfo).replace(tzinfo=None)


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
