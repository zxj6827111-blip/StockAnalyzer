"""Idle queue workday report helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from datetime import time as dt_time
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueWorkdayReportService:
    """Build and validate the workday morning brief."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_validate_precompute_cache(
        self,
        *,
        path: Path,
        expected_trade_date: str,
        now: datetime,
    ) -> dict[str, object]:
        if not path.exists() or not path.is_file():
            return {"valid": False, "reason": "missing_file", "rows": 0}
        try:
            if path.suffix.lower() == ".parquet":
                frame = pd.read_parquet(path)
            elif path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = payload.get("rows") if isinstance(payload, dict) else []
                frame = pd.DataFrame(rows if isinstance(rows, list) else [])
            else:
                return {"valid": False, "reason": "unsupported_format", "rows": 0}
        except Exception as exc:
            return {"valid": False, "reason": f"read_failed:{exc.__class__.__name__}", "rows": 0}

        if frame.empty:
            return {"valid": False, "reason": "empty_rows", "rows": 0}
        if "trade_date" not in frame.columns or "available_at" not in frame.columns:
            return {"valid": False, "reason": "missing_required_columns", "rows": len(frame)}

        trade_dates = {
            str(item).strip() for item in frame["trade_date"].tolist() if str(item).strip()
        }
        if not trade_dates:
            return {"valid": False, "reason": "empty_trade_date", "rows": len(frame)}
        if trade_dates != {expected_trade_date}:
            return {
                "valid": False,
                "reason": f"trade_date_mismatch:{sorted(trade_dates)}",
                "rows": len(frame),
            }

        available_values = pd.to_datetime(frame["available_at"], errors="coerce")
        if available_values.isna().all():
            return {"valid": False, "reason": "available_at_unparseable", "rows": len(frame)}
        max_available = available_values.max()
        if pd.isna(max_available):
            return {"valid": False, "reason": "available_at_missing", "rows": len(frame)}
        if hasattr(max_available, "to_pydatetime"):
            max_available_dt = max_available.to_pydatetime()
        else:
            return {"valid": False, "reason": "available_at_invalid_type", "rows": len(frame)}
        if max_available_dt > now + timedelta(minutes=1):
            return {"valid": False, "reason": "available_at_in_future", "rows": len(frame)}

        return {
            "valid": True,
            "reason": "",
            "rows": int(len(frame)),
            "max_available_at": max_available_dt.isoformat(),
        }

    def idle_task_wd_report(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        expected_sources = [
            ("WD-P0-01", "data_quality", "report.json"),
            ("WD-P0-02", "failure_analysis", "loss_attribution.json"),
            ("WD-P0-03", "psi_monitor", "psi_report.json"),
            ("WD-P0-04", "exposure_scan", "industry_concentration.json"),
            ("WD-P1-05", "precompute", "precompute_cache.parquet"),
            ("WD-P1-06", "monte_carlo", "var_cvar_report.json"),
            ("WD-P1-07", "sector_radar", "sector_rotation.json"),
        ]

        sections: list[dict[str, object]] = []
        missing_count = 0
        fallback_count = 0
        ok_count = 0
        fallback_provenance: list[dict[str, object]] = []
        for task_id, subdir, filename in expected_sources:
            output_path = service._idle_output_path(
                trade_date=trade_date,
                task_id=task_id,
                subdir=subdir,
                filename=filename,
            )
            exists = output_path.exists()
            validation_reason = ""
            if exists and task_id == "WD-P1-05":
                validation = self.idle_validate_precompute_cache(
                    path=output_path,
                    expected_trade_date=trade_date,
                    now=now,
                )
                if not bool(validation.get("valid", False)):
                    exists = False
                    validation_reason = f"invalid_precompute_cache:{validation.get('reason', '')}"
            fallback_trade_date = ""
            fallback_path = ""
            fallback_status = ""
            fallback_reason = ""
            if not exists:
                stale = service._idle_find_latest_task_report(
                    task_id=task_id,
                    subdir=subdir,
                    filename=filename,
                    exclude_trade_date=trade_date,
                )
                if stale is not None:
                    candidate_trade_date = str(stale.get("trade_date", ""))
                    candidate_path = str(stale.get("path", ""))
                    candidate_status = ""
                    candidate_reason = ""
                    stale_payload = stale.get("payload", {})
                    if isinstance(stale_payload, dict):
                        candidate_status = str(stale_payload.get("status", "")).strip()
                        candidate_reason = str(stale_payload.get("reason", "")).strip()

                    fallback_valid = True
                    if task_id == "WD-P1-05":
                        validation = self.idle_validate_precompute_cache(
                            path=Path(candidate_path),
                            expected_trade_date=candidate_trade_date,
                            now=now,
                        )
                        if not bool(validation.get("valid", False)):
                            fallback_valid = False
                            candidate_reason = (
                                f"invalid_fallback_cache:{validation.get('reason', '')}"
                            )
                    if fallback_valid:
                        fallback_trade_date = candidate_trade_date
                        fallback_path = candidate_path
                        fallback_status = candidate_status
                        fallback_reason = candidate_reason
                        fallback_count += 1
                        fallback_provenance.append(
                            {
                                "task_id": task_id,
                                "fallback_trade_date": fallback_trade_date,
                                "fallback_path": fallback_path,
                                "fallback_status": fallback_status,
                                "fallback_reason": fallback_reason,
                            }
                        )
                    else:
                        missing_count += 1
                else:
                    missing_count += 1
            else:
                ok_count += 1
            section_status = (
                "ok" if exists else ("fallback_prev_day" if fallback_trade_date else "missing")
            )
            sections.append(
                {
                    "task_id": task_id,
                    "status": section_status,
                    "path": str(output_path),
                    "fallback_trade_date": fallback_trade_date,
                    "fallback_path": fallback_path,
                    "fallback_status": fallback_status,
                    "fallback_reason": fallback_reason,
                    "validation_reason": validation_reason,
                }
            )

        sections_total = len(expected_sources)
        covered_sections = ok_count + fallback_count
        completeness_ratio = covered_sections / max(sections_total, 1)
        task_status = "ok" if missing_count == 0 else "degraded"
        report_deadline = _parse_hhmmss_time(
            str(context.get("effective_report_deadline", "08:23:00"))
        )
        deadline_hit = now.time() <= report_deadline
        summary = {
            "task_id": "WD-REPORT",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": task_status,
            "sections": sections,
            "effective_report_deadline": str(context.get("effective_report_deadline", "")),
            "trigger_time": str(context.get("trigger_time", "")),
            "sections_total": sections_total,
            "sections_ok": ok_count,
            "sections_fallback_prev_day": fallback_count,
            "missing_sections": missing_count,
            "completeness_ratio": round(completeness_ratio, 6),
            "deadline_hit": deadline_hit,
            "fallback_provenance": fallback_provenance,
            "degraded_all": missing_count == sections_total,
        }

        markdown_lines = [
            f"# Auto Morning Brief {trade_date}",
            "",
            f"- generated_at: {now.isoformat()}",
            f"- report_status: {task_status}",
            f"- effective_report_deadline: {summary['effective_report_deadline']}",
            f"- deadline_hit: {deadline_hit}",
            f"- completeness: {covered_sections}/{sections_total} ({completeness_ratio:.2%})",
            "",
            "| task | status | detail |",
            "|---|---|---|",
        ]
        for item in sections:
            section_task_id = str(item.get("task_id", ""))
            section_status = str(item.get("status", ""))
            fallback_trade_date = str(item.get("fallback_trade_date", ""))
            fallback_status = str(item.get("fallback_status", ""))
            fallback_reason = str(item.get("fallback_reason", ""))
            validation_reason = str(item.get("validation_reason", ""))
            detail_parts: list[str] = []
            if fallback_trade_date:
                detail_parts.append(f"from={fallback_trade_date}")
            if fallback_status:
                detail_parts.append(f"status={fallback_status}")
            if fallback_reason:
                detail_parts.append(f"reason={fallback_reason}")
            if validation_reason:
                detail_parts.append(f"validation={validation_reason}")
            detail = "; ".join(detail_parts) if detail_parts else "-"
            markdown_lines.append(f"| {section_task_id} | {section_status} | {detail} |")
        markdown = "\n".join(markdown_lines) + "\n"

        output_dir = service._idle_output_dir(
            trade_date=trade_date,
            task_id="WD-REPORT",
            subdir="morning_brief",
        )
        json_path = output_dir / "morning_brief.json"
        md_path = output_dir / "morning_brief.md"
        service._idle_write_json(json_path, summary)
        service._idle_write_text(md_path, markdown)
        return {
            "status": task_status,
            "output_files": [str(json_path), str(md_path)],
            "sections_total": sections_total,
            "missing_sections": missing_count,
            "completeness_ratio": round(completeness_ratio, 6),
            "deadline_hit": deadline_hit,
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _parse_hhmmss_time(raw: str) -> dt_time:
    return cast(dt_time, _runtime_service_module()._parse_hhmmss_time(raw))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))
