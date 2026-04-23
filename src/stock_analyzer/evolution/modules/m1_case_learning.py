"""M1 dual learning: missed-signal cases and leakage (poison) filters."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path


@dataclass(frozen=True, slots=True)
class M1LearningResult:
    """Result of M1 dual learning pass."""

    score: float
    asof_pass: bool
    poison_hits: int
    bucket_counts: dict[str, int]
    negative_case_count: int
    reason_counts: dict[str, int]
    cases_preview: list[dict[str, object]]
    shared_payload_uri: str | None


def run_m1_dual_learning(
    records: Sequence[Mapping[str, object]],
    asof_date: date | None = None,
    shared_dir: str | Path | None = None,
    now: datetime | None = None,
) -> M1LearningResult:
    """Run M1 dual learning with As-Of guard and poison filtering.

    The procedure is intentionally lightweight and framework-safe:
    1. As-Of guard checks whether any record has `available_at` later than `asof_date`.
    2. Poison filter counts records with `leak_flag=true`.
    3. Missed cases are bucketed by `realized_return` drawdown.
    4. Optional shared payload is written for downstream modules (e.g. M8).

    Args:
        records: Candidate case records.
        asof_date: Trading date for As-Of checks. Defaults to today's UTC date.
        shared_dir: Optional output directory for shared payload.
        now: Optional timestamp override.

    Returns:
        M1 learning summary with quality score.
    """
    run_now = now or datetime.now(UTC)
    asof = asof_date or run_now.date()

    poison_hits = 0
    asof_violations = 0
    bucket_counts = {"mild": 0, "medium": 0, "severe": 0}
    reason_counts = {
        "chase_high": 0,
        "liquidity_insufficient": 0,
        "high_sell_pressure": 0,
        "model_divergence": 0,
        "data_incomplete": 0,
        "unclassified_negative_case": 0,
    }
    shared_cases: list[dict[str, object]] = []

    asof_deadline = datetime.combine(asof, time(23, 59, 59), tzinfo=UTC)

    for record in records:
        leak_flag = bool(record.get("leak_flag", False))
        if leak_flag:
            poison_hits += 1

        available_at = _parse_datetime(record.get("available_at"))
        if available_at is not None and available_at > asof_deadline:
            asof_violations += 1

        realized_return = _as_float(record.get("realized_return"), default=0.0)
        bucket = _bucket_drawdown(realized_return=realized_return)
        if bucket is not None:
            reason_codes = _derive_negative_reason_codes(record=record)
            bucket_counts[bucket] += 1
            for reason_code in reason_codes:
                if reason_code not in reason_counts:
                    reason_counts[reason_code] = 0
                reason_counts[reason_code] += 1
            shared_cases.append(
                {
                    "symbol": str(record.get("symbol", "UNKNOWN")),
                    "realized_return": realized_return,
                    "bucket": bucket,
                    "reason_codes": reason_codes,
                    "fingerprint": _build_case_fingerprint(
                        record=record, realized_return=realized_return
                    ),
                    "asof_violation": available_at is not None and available_at > asof_deadline,
                    "leak_flag": leak_flag,
                }
            )

    asof_pass = asof_violations == 0
    score = 100.0
    score -= poison_hits * 20.0
    score -= bucket_counts["severe"] * 10.0
    score -= bucket_counts["medium"] * 5.0
    score -= bucket_counts["mild"] * 2.0
    if not asof_pass:
        score = min(score, 40.0)
    score = max(0.0, min(100.0, score))

    shared_payload_uri: str | None = None
    if shared_dir is not None:
        output_path = Path(shared_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        file_path = output_path / f"m1_shared_{run_now.strftime('%Y%m%d_%H%M%S')}.json"
        payload = {
            "timestamp": run_now.isoformat(),
            "asof_date": asof.isoformat(),
            "asof_pass": asof_pass,
            "poison_hits": poison_hits,
            "bucket_counts": bucket_counts,
            "negative_case_count": len(shared_cases),
            "reason_counts": reason_counts,
            "cases": shared_cases,
        }
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        shared_payload_uri = str(file_path)

    return M1LearningResult(
        score=score,
        asof_pass=asof_pass,
        poison_hits=poison_hits,
        bucket_counts=bucket_counts,
        negative_case_count=len(shared_cases),
        reason_counts=reason_counts,
        cases_preview=shared_cases[:10],
        shared_payload_uri=shared_payload_uri,
    )


def _bucket_drawdown(realized_return: float) -> str | None:
    if realized_return >= 0.0:
        return None
    if realized_return < -0.10:
        return "severe"
    if realized_return < -0.05:
        return "medium"
    return "mild"


def _parse_datetime(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _derive_negative_reason_codes(record: Mapping[str, object]) -> list[str]:
    reason_codes: list[str] = []

    heat_ratio = _as_float(record.get("heat_ratio"), default=0.0)
    ret20 = _as_float(record.get("ret_20d", record.get("ret20")), default=0.0)
    recent_high_ratio = _as_float(
        record.get("recent_high_ratio", record.get("price_to_recent_high")),
        default=0.0,
    )
    if (
        heat_ratio >= 1.25
        and ret20 >= 0.10
        and (recent_high_ratio >= 0.97 or _as_float(record.get("gap_pct"), default=0.0) >= 0.04)
    ):
        reason_codes.append("chase_high")

    avg_turnover20 = _as_float(record.get("avg_turnover_20"), default=0.0)
    float_market_cap = _as_float(record.get("float_market_cap"), default=0.0)
    liquidity_tier = str(record.get("liquidity_tier", "")).strip().lower()
    if (
        (avg_turnover20 > 0.0 and avg_turnover20 < 50_000_000.0)
        or (float_market_cap > 0.0 and float_market_cap < 8_000_000_000.0)
        or liquidity_tier == "small"
    ):
        reason_codes.append("liquidity_insufficient")

    pressure_index = _as_float(record.get("pressure_index"), default=0.0)
    bearish_ratio = _as_float(record.get("bearish_ratio"), default=0.0)
    upper_shadow_ratio = _as_float(record.get("upper_shadow_ratio"), default=0.0)
    close_position = _as_float(record.get("close_position"), default=1.0)
    if (
        pressure_index >= 0.58
        or bearish_ratio >= 0.55
        or upper_shadow_ratio >= 0.55
        or close_position <= 0.30
    ):
        reason_codes.append("high_sell_pressure")

    spread = _as_float(record.get("prediction_spread"), default=-1.0)
    lgbm_prob = _as_float(record.get("lgbm_prob"), default=-1.0)
    xgb_prob = _as_float(record.get("xgb_prob"), default=-1.0)
    meta_prob = _as_float(record.get("meta_prob"), default=-1.0)
    conflict_flag = bool(record.get("model_conflict", False))
    if (
        conflict_flag
        or spread >= 0.20
        or (lgbm_prob >= 0.0 and xgb_prob >= 0.0 and abs(lgbm_prob - xgb_prob) >= 0.20)
        or (meta_prob >= 0.0 and lgbm_prob >= 0.0 and abs(meta_prob - lgbm_prob) >= 0.18)
    ):
        reason_codes.append("model_divergence")

    if (
        not bool(record.get("financial_data_complete", True))
        or not bool(record.get("background_data_complete", True))
        or _as_float(record.get("completion_score"), default=1.0) < 0.70
        or _as_float(record.get("background_completion_score"), default=1.0) < 0.70
        or bool(record.get("leak_flag", False))
    ):
        reason_codes.append("data_incomplete")

    if not reason_codes:
        reason_codes.append("unclassified_negative_case")
    return reason_codes


def _build_case_fingerprint(
    *,
    record: Mapping[str, object],
    realized_return: float,
) -> dict[str, object]:
    return {
        "ret_20d": round(_as_float(record.get("ret_20d", record.get("ret20")), default=0.0), 6),
        "heat_ratio": round(_as_float(record.get("heat_ratio"), default=0.0), 6),
        "avg_turnover_20": round(_as_float(record.get("avg_turnover_20"), default=0.0), 2),
        "float_market_cap": round(_as_float(record.get("float_market_cap"), default=0.0), 2),
        "pressure_index": round(_as_float(record.get("pressure_index"), default=0.0), 6),
        "bearish_ratio": round(_as_float(record.get("bearish_ratio"), default=0.0), 6),
        "prediction_spread": round(_as_float(record.get("prediction_spread"), default=0.0), 6),
        "completion_score": round(_as_float(record.get("completion_score"), default=0.0), 6),
        "background_completion_score": round(
            _as_float(record.get("background_completion_score"), default=0.0),
            6,
        ),
        "realized_return": round(realized_return, 6),
    }


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
