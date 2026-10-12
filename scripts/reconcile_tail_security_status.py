#!/usr/bin/env python3
"""Create an auditable PIT security-status copy from reviewed official evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: Any, *, date_value: bool = False) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    if date_value:
        return text.replace("-", "")
    return text


def _selector_matches(row: dict[str, Any], selector: dict[str, Any]) -> bool:
    date_fields = {"start_date", "end_date", "ann_date"}
    return all(
        _normal(row.get(key), date_value=key in date_fields)
        == _normal(value, date_value=key in date_fields)
        for key, value in selector.items()
    )


def _parse_day(value: Any) -> date | None:
    normalized = _normal(value, date_value=True)
    if normalized is None:
        return None
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError(f"invalid security-status date: {value!r}")
    return date(int(normalized[:4]), int(normalized[4:6]), int(normalized[6:8]))


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def audit_namechange_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Return PIT defects using the same inclusive interval rule as readiness."""
    missing_announcement = 0
    announcement_after_start = 0
    by_symbol: dict[str, list[tuple[date, date | None]]] = {}
    for row in rows:
        start = _parse_day(row.get("start_date"))
        if start is None:
            raise ValueError("namechange row has no effective start")
        announcement = _parse_day(row.get("ann_date"))
        end = _parse_day(row.get("end_date"))
        missing_announcement += announcement is None
        announcement_after_start += announcement is not None and announcement > start
        symbol = str(row.get("ts_code") or "").strip().upper()
        if not symbol:
            raise ValueError("namechange row has no ts_code")
        by_symbol.setdefault(symbol, []).append((start, end))

    overlaps = 0
    for intervals in by_symbol.values():
        intervals.sort(key=lambda item: item[0])
        for index, (start, end) in enumerate(intervals):
            for next_start, _ in intervals[index + 1 :]:
                if end is not None and end < next_start:
                    break
                if start < next_start and (end is None or end >= next_start):
                    overlaps += 1
    return {
        "rows": len(rows),
        "missing_announcement_date": missing_announcement,
        "announcement_after_effective_start": announcement_after_start,
        "overlap_pairs": overlaps,
    }


def apply_review(
    payload: dict[str, Any], actions: list[dict[str, Any]], evidence: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply exact row decisions; source payload remains unchanged."""
    canonical = copy.deepcopy(payload)
    source_rows = payload.get("namechange", {}).get("rows")
    canonical_rows = canonical.get("namechange", {}).get("rows")
    if not isinstance(source_rows, list) or not isinstance(canonical_rows, list):
        raise ValueError("payload must contain namechange.rows")

    remove_indexes: set[int] = set()
    decisions: list[dict[str, Any]] = []
    for action in actions:
        action_name = action.get("action")
        if action_name not in {"exclude_from_active_intervals", "set_announcement_date"}:
            raise ValueError(f"unsupported review action: {action_name!r}")
        selector = action.get("selector")
        if not isinstance(selector, dict) or not selector:
            raise ValueError("review action requires an exact non-empty selector")
        indexes = [
            index for index, row in enumerate(source_rows)
            if isinstance(row, dict) and _selector_matches(row, selector)
        ]
        if len(indexes) != 1:
            raise ValueError(
                f"review selector must match exactly one source row; got {len(indexes)}: {selector}"
            )
        index = indexes[0]
        evidence_names = action.get("evidence") or []
        if not evidence_names or any(name not in evidence for name in evidence_names):
            raise ValueError(f"review action has missing evidence: {evidence_names}")
        original_row = copy.deepcopy(source_rows[index])
        decision = {
            "action": action_name,
            "selector": selector,
            "reason_code": action.get("reason_code"),
            "reason": action.get("reason"),
            "source_row": _json_safe(original_row),
            "source_row_sha256": hashlib.sha256(
                json.dumps(
                    original_row, ensure_ascii=False, sort_keys=True, allow_nan=True
                ).encode()
            ).hexdigest(),
            "evidence": [evidence[name] for name in evidence_names],
        }
        if action_name == "exclude_from_active_intervals":
            if index in remove_indexes:
                raise ValueError(f"source row has duplicate review decisions: {selector}")
            remove_indexes.add(index)
        else:
            new_announcement = _normal(action.get("announcement_date"), date_value=True)
            if not new_announcement or _normal(source_rows[index].get("ann_date")) is not None:
                raise ValueError("announcement-date correction must fill a missing source value")
            canonical_rows[index]["ann_date"] = new_announcement
            decision["canonical_announcement_date"] = new_announcement
        decisions.append(decision)

    canonical["namechange"]["rows"] = [
        row for index, row in enumerate(canonical_rows) if index not in remove_indexes
    ]
    return canonical, decisions


def _load_verified_evidence(
    review_path: Path, review: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    evidence_manifest_path = (review_path.parent / review["evidence_manifest"]).resolve()
    expected_manifest_sha = review.get("evidence_manifest_sha256")
    actual_manifest_sha = _sha256(evidence_manifest_path)
    if actual_manifest_sha != expected_manifest_sha:
        raise ValueError("official evidence manifest SHA-256 mismatch")
    evidence_manifest = json.loads(evidence_manifest_path.read_text(encoding="utf-8"))
    evidence_root = evidence_manifest_path.parent.resolve()
    verified: dict[str, dict[str, Any]] = {}
    for item in evidence_manifest.get("files", []):
        relative = Path(item["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"evidence path must stay inside evidence directory: {relative}")
        path = (evidence_root / relative).resolve()
        if evidence_root not in path.parents:
            raise ValueError(f"evidence path escapes evidence directory: {relative}")
        if not path.is_file():
            raise ValueError(f"official evidence file is missing: {path}")
        actual_sha = _sha256(path)
        if actual_sha != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            raise ValueError(f"official evidence hash/size mismatch: {path.name}")
        verified[path.name] = {
            "file": path.name,
            "url": item.get("url"),
            "bytes": path.stat().st_size,
            "sha256": actual_sha,
            "fact": item.get("fact"),
        }
    return verified, {"path": str(evidence_manifest_path), "sha256": actual_manifest_sha}


def reconcile(
    payload_path: Path,
    collection_manifest_path: Path,
    review_path: Path,
    evidence_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("schema") != "stock-analyzer.security-status-reconciliation-review.v1":
        raise ValueError("unsupported reconciliation review schema")
    payload_sha = _sha256(payload_path)
    if payload_sha != review.get("source_payload_sha256"):
        raise ValueError("source payload SHA-256 differs from reviewed source")
    collection_sha = _sha256(collection_manifest_path)
    if collection_sha != review.get("source_collection_manifest_sha256"):
        raise ValueError("collection manifest SHA-256 differs from reviewed source")
    evidence, evidence_manifest = _load_verified_evidence(review_path, review)
    canonical, decisions = apply_review(
        payload=json.loads(payload_path.read_text(encoding="utf-8")),
        actions=review.get("actions", []), evidence=evidence
    )
    raw_rows = json.loads(payload_path.read_text(encoding="utf-8"))["namechange"]["rows"]
    canonical_rows = canonical["namechange"]["rows"]
    raw_audit = audit_namechange_rows(raw_rows)
    canonical_audit = audit_namechange_rows(canonical_rows)
    if any(canonical_audit[key] for key in (
        "missing_announcement_date", "announcement_after_effective_start", "overlap_pairs"
    )):
        raise ValueError(f"reviewed canonical intervals still fail PIT audit: {canonical_audit}")

    canonical_bytes = (
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=True) + "\n"
    ).encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=False)
    canonical_path = output_dir / "security_status_canonical.json"
    canonical_path.write_bytes(canonical_bytes)
    report = {
        "schema": "stock-analyzer.security-status-reconciliation-report.v1",
        "source_payload": {"path": str(payload_path), "sha256": payload_sha},
        "source_collection_manifest": {
            "path": str(collection_manifest_path), "sha256": collection_sha
        },
        "review_manifest": {"path": str(review_path), "sha256": _sha256(review_path)},
        "official_evidence_manifest": evidence_manifest,
        "canonical_payload": {
            "path": canonical_path.name,
            "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        },
        "raw_namechange_audit": raw_audit,
        "canonical_namechange_audit": canonical_audit,
        "decisions": decisions,
        "scope_note": (
            "Only the new isolated research payload is transformed. "
            "The raw NAS collection payload is unchanged."
        ),
    }
    (output_dir / "security_status_reconciliation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--collection-manifest", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True,
                        help="Directory containing official_evidence/ and its hash manifest")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="New output directory; existing paths are refused")
    args = parser.parse_args()
    # Keep the CLI's evidence-root argument explicit and ensure review file points inside it.
    review_doc = json.loads(args.review.read_text(encoding="utf-8"))
    expected_evidence = (args.review.parent / review_doc["evidence_manifest"]).resolve()
    if args.evidence_dir.resolve() not in expected_evidence.parents:
        raise SystemExit("review evidence manifest is outside --evidence-dir")
    report = reconcile(args.payload, args.collection_manifest, args.review,
                       args.evidence_dir, args.out_dir)
    print(json.dumps({
        "canonical_payload": report["canonical_payload"],
        "raw_namechange_audit": report["raw_namechange_audit"],
        "canonical_namechange_audit": report["canonical_namechange_audit"],
        "decisions": len(report["decisions"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
