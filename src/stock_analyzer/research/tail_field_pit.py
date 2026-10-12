"""字段级 PIT 记录审计：闭合字段、来源文件、覆盖和实际决策时刻。

校验采集器留存的来源/时钟事实，不从文件 mtime 或事后查询日期推定历史可用性。
来源哈希只能证明完整性；本模块不提供第三方签名或独立时间戳证明。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_analyzer.feature.engineer import FEATURE_COMPUTE_VERSION, FeatureEngineer
from stock_analyzer.feature.snapshot import RAW_SNAPSHOT_COLUMNS, _dummy_bars
from stock_analyzer.feature.trend_candidate_contract import (
    LIVE_GATE_INPUT_COLUMNS,
    REPRODUCIBLE_FEATURE_COLUMNS,
)
from stock_analyzer.research.tail_ablation import EVENT_FEATURE_COLUMNS

SCHEMA = "tail_field_pit_evidence.v1"
AUDIT_SCHEMA = "tail_field_pit_audit.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def json_loads_unique(text: str) -> Any:
    """Parse JSON while rejecting duplicate keys at every nesting level."""
    return json.loads(text, object_pairs_hook=_unique_json_object)


@lru_cache(maxsize=2)
def _cached_field_inventory(scope: str) -> dict[str, Any]:
    """枚举真实代码字段；探针只取列名，绝不作为研究样本或 PIT 证据。"""
    if scope not in {"stage2", "full_chain"}:
        raise ValueError(f"unknown field PIT scope: {scope}")
    roles = {
        "hard_gate": sorted({c for cols in LIVE_GATE_INPUT_COLUMNS.values() for c in cols}),
        "trend_model": sorted(REPRODUCIBLE_FEATURE_COLUMNS),
        "event_arms": list(EVENT_FEATURE_COLUMNS),
    }
    if scope == "full_chain":
        columns = FeatureEngineer().transform(_dummy_bars()).columns
        if len(columns) == 0:
            raise ValueError("legacy feature schema probe is empty")
        roles["legacy_model"] = sorted(str(c) for c in columns)
        roles["legacy_light_and_risk"] = sorted(RAW_SNAPSHOT_COLUMNS)
    fields = sorted({c for cols in roles.values() for c in cols})
    identity = {"scope": scope, "roles": roles, "feature_compute_version": FEATURE_COMPUTE_VERSION}
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**identity, "fields": fields, "field_count": len(fields), "sha256": digest}


def field_inventory(scope: str = "full_chain") -> dict[str, Any]:
    """Return a defensive copy so callers cannot mutate the cached contract."""
    if scope not in {"stage2", "full_chain"}:
        raise ValueError(f"unknown field PIT scope: {scope}")
    return deepcopy(_cached_field_inventory(scope))


def _clock(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires timezone and intraday precision")
    return parsed.astimezone(UTC)


def _stat_identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _asset_ok(
    spec: Mapping[str, Any],
    root: Path,
    cache: dict[Path, tuple[str, tuple[int, int, int]]],
) -> bool:
    value = spec.get("path")
    expected = spec.get("sha256")
    if (
        not isinstance(value, str)
        or not isinstance(expected, str)
        or not _SHA256.fullmatch(expected)
    ):
        return False
    relative = Path(value)
    if relative.is_absolute():
        return False
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return False
    # 同一报告常有数百个字段共用一个来源文件；只哈希一次，避免重复扫描大文件。
    try:
        identity = _stat_identity(path)
        if path not in cache:
            cache[path] = (sha256_file(path), identity)
        elif cache[path][1] != identity:
            return False
    except OSError:
        return False
    return cache[path][0] == expected


def audit_field_pit(
    payload: Mapping[str, Any],
    *,
    asset_root: Path,
    expected_input_sha256: str,
    scope: str = "full_chain",
    expected_sessions: Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    inventory = field_inventory(scope)
    required = set(inventory["fields"])
    failures: Counter[str] = Counter()
    field_failures: dict[str, Counter[str]] = {name: Counter() for name in sorted(required)}
    cache: dict[Path, tuple[str, tuple[int, int, int]]] = {}
    root = asset_root.resolve()
    if payload.get("schema") != SCHEMA:
        failures["unsupported_pit_evidence_schema"] += 1
    if payload.get("scope") != scope:
        failures["pit_evidence_scope_mismatch"] += 1
    if (
        not _SHA256.fullmatch(expected_input_sha256)
        or payload.get("input_sha256") != expected_input_sha256
    ):
        failures["pit_evidence_input_hash_mismatch"] += 1
    if payload.get("inventory_sha256") != inventory["sha256"]:
        failures["pit_field_inventory_mismatch"] += 1
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        failures["pit_decision_records_missing"] += 1
        records = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, Mapping):
            failures["pit_record_not_an_object"] += 1
            continue
        key = (str(record.get("trade_date") or ""), str(record.get("arm_id") or ""))
        if key in seen:
            failures["pit_duplicate_arm_session"] += 1
        seen.add(key)
        if record.get("capture_mode") != "observed_snapshot":
            failures["pit_source_not_observed_snapshot"] += 1
        try:
            decision = _clock(record.get("decision_at"))
            if decision.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != key[0]:
                raise ValueError("decision clock differs from trading date")
        except ValueError:
            failures["pit_decision_clock_invalid"] += 1
            decision = None
        count = record.get("input_symbol_count")
        if type(count) is not int or count <= 0:
            failures["pit_input_universe_count_invalid"] += 1
        snapshot = record.get("input_snapshot")
        if not isinstance(snapshot, Mapping) or not _asset_ok(snapshot, root, cache):
            failures["pit_input_snapshot_hash_invalid"] += 1
        fields = record.get("fields")
        if not isinstance(fields, Mapping):
            fields = {}
        for name in required:
            evidence = fields.get(name)
            reasons: list[str] = []
            if not isinstance(evidence, Mapping):
                reasons.append("field_evidence_missing")
            else:
                if type(evidence.get("input_rows")) is not int or evidence["input_rows"] != count:
                    reasons.append("field_input_coverage_mismatch")
                for flag in ("missing_inputs", "unknown_imputations"):
                    if type(evidence.get(flag)) is not int or evidence[flag] != 0:
                        reasons.append(flag)
                sources = evidence.get("sources")
                if not isinstance(sources, list) or not sources:
                    reasons.append("field_sources_missing")
                    sources = []
                try:
                    computed = _clock(evidence.get("computed_at"))
                    latest_input = _clock(evidence.get("latest_input_at"))
                    if decision is None or latest_input > computed or computed > decision:
                        reasons.append("field_clock_after_decision_or_out_of_order")
                except ValueError:
                    reasons.append("field_clock_invalid")
                    computed = None
                    latest_input = None
                for source in sources:
                    if not isinstance(source, Mapping):
                        reasons.append("field_source_invalid")
                        continue
                    if not _asset_ok(source, root, cache):
                        reasons.append("field_source_hash_invalid")
                    if not isinstance(source.get("version"), str) or not source["version"].strip():
                        reasons.append("field_source_version_missing")
                    try:
                        available = _clock(source.get("available_at"))
                        captured = _clock(source.get("captured_at"))
                        if (
                            computed is None
                            or available > captured
                            or captured > computed
                            or latest_input is None
                            or latest_input > available
                        ):
                            reasons.append("field_source_clock_after_decision_or_out_of_order")
                    except ValueError:
                        reasons.append("field_source_clock_invalid")
            for reason in set(reasons):
                failures[reason] += 1
                field_failures[name][reason] += 1
    if expected_sessions is not None:
        expected_rows = list(expected_sessions)
        expected = set(expected_rows)
        if len(expected_rows) != len(expected) or seen != expected:
            failures["pit_arm_session_coverage_mismatch"] += 1
    # A file that changes during this audit invalidates every result that relied on it.
    for path, (_, identity) in cache.items():
        try:
            if _stat_identity(path) != identity:
                failures["pit_source_changed_during_audit"] += 1
        except OSError:
            failures["pit_source_changed_during_audit"] += 1
    passed = not failures
    global_blocked = any(
        name in failures
        for name in (
            "unsupported_pit_evidence_schema",
            "pit_evidence_scope_mismatch",
            "pit_evidence_input_hash_mismatch",
            "pit_field_inventory_mismatch",
            "pit_decision_records_missing",
            "pit_record_not_an_object",
            "pit_source_not_observed_snapshot",
            "pit_decision_clock_invalid",
            "pit_input_universe_count_invalid",
            "pit_input_snapshot_hash_invalid",
            "pit_arm_session_coverage_mismatch",
            "pit_duplicate_arm_session",
            "pit_source_changed_during_audit",
        )
    )
    return {
        "schema": AUDIT_SCHEMA,
        "scope": scope,
        "status": "passed" if passed else "blocked",
        "pit_evidence_checks_passed": passed,
        "release_eligible": False,
        "inventory": inventory,
        "input_sha256": expected_input_sha256,
        "records": len(records),
        "unique_arm_sessions": len(seen),
        "blockers": dict(sorted(failures.items())),
        "fields": {
            name: {
                "status": ("blocked" if global_blocked or reasons or not records else "passed"),
                "blockers": dict(reasons),
            }
            for name, reasons in sorted(field_failures.items())
        },
        "source_files_hashed": len(cache),
        "source_file_hashes": {
            str(path.relative_to(root)): identity[0]
            for path, identity in sorted(cache.items(), key=lambda item: str(item[0]))
        },
        "trust_boundary": (
            "sha256_integrity_and_recorded_collector_clocks; "
            "no_signature_or_vendor_timestamp_attestation"
        ),
    }


def load_and_audit_field_pit(
    path: Path,
    *,
    expected_input_sha256: str,
    expected_sessions: Iterable[tuple[str, str]] | None = None,
    scope: str = "full_chain",
) -> dict[str, Any]:
    payload = json_loads_unique(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("field PIT evidence must be a JSON object")
    report = audit_field_pit(
        payload,
        asset_root=path.parent,
        expected_input_sha256=expected_input_sha256,
        expected_sessions=expected_sessions,
        scope=scope,
    )
    report["evidence_file"] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
    return report
