"""Week6 state, snapshot, and report helpers extracted from the runtime service."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeWeek6StateService:
    """Manage week6 snapshots, report history, and persisted state."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def latest_week6_data_quality_report(self) -> dict[str, object] | None:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        report = service._last_week6_data_quality_report
        return report if isinstance(report, dict) else None

    def week6_data_quality_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        history_limit = max(1, service._config.week6.data_quality_history_limit)
        capped_limit = max(1, min(limit, history_limit))
        recent = service._week6_data_quality_history[-capped_limit:]
        return {"records": len(recent), "reports": recent}

    def store_week6_data_quality_report(self, report: dict[str, object]) -> None:
        service = self._service
        service._last_week6_data_quality_report = report
        service._week6_data_quality_history.append(report)
        history_limit = max(1, service._config.week6.data_quality_history_limit)
        if len(service._week6_data_quality_history) > history_limit:
            overflow = len(service._week6_data_quality_history) - history_limit
            if overflow > 0:
                service._week6_data_quality_history = service._week6_data_quality_history[overflow:]
        self.persist_week6_state()

    def set_regulatory_watchlist(
        self,
        entries: list[dict[str, object]],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        normalized: dict[str, dict[str, object]] = {}
        for item in entries:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            tag = str(item.get("tag", "")).strip().lower()
            note = str(item.get("note", "")).strip()
            normalized[symbol] = {
                "symbol": symbol,
                "tag": tag,
                "note": note,
                "updated_at": datetime.now().isoformat(),
            }
        service._regulatory_watchlist = normalized
        self.persist_week6_state()
        service._record_audit_event(
            event_type="week6_regulatory_watchlist",
            trace_id=source_trace_id,
            payload={"size": len(normalized), "symbols": sorted(normalized.keys())},
        )
        watchlist_payload = self.regulatory_watchlist()
        return {
            "records": len(normalized),
            "watchlist": watchlist_payload["watchlist"],
        }

    def regulatory_watchlist(self) -> dict[str, object]:
        service = self._service
        items = sorted(service._regulatory_watchlist.values(), key=lambda item: str(item["symbol"]))
        return {"records": len(items), "watchlist": items}

    def latest_week6_report(self) -> dict[str, object] | None:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        report = service._last_week6_report
        return report if isinstance(report, dict) else None

    def week6_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        max_limit = max(1, service._config.week6.history_limit)
        capped_limit = max(1, min(limit, max_limit))
        recent = service._week6_history[-capped_limit:]
        return {"records": len(recent), "reports": recent}

    def store_week6_report(self, report: dict[str, object]) -> None:
        service = self._service
        service._last_week6_report = report
        service._week6_history.append(report)
        history_limit = max(1, service._config.week6.history_limit)
        if len(service._week6_history) > history_limit:
            overflow = len(service._week6_history) - history_limit
            if overflow > 0:
                service._week6_history = service._week6_history[overflow:]

    def update_global_market_snapshot(
        self,
        snapshot: dict[str, object],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        normalized = {
            "us_index_change_pct": _as_float(
                snapshot.get("us_index_change_pct"),
                default=0.0,
            ),
            "a50_change_pct": _as_float(snapshot.get("a50_change_pct"), default=0.0),
            "usd_cnh_change_pct": _as_float(
                snapshot.get("usd_cnh_change_pct"),
                default=0.0,
            ),
            "commodity_change_pct": _as_float(
                snapshot.get("commodity_change_pct"),
                default=0.0,
            ),
            "a_share_correlation": _as_float(
                snapshot.get("a_share_correlation"),
                default=0.60,
            ),
        }
        service._global_market_snapshot = normalized
        record: dict[str, object] = {
            "timestamp": datetime.now().isoformat(),
            "source_trace_id": source_trace_id,
            "snapshot": dict(normalized),
        }
        service._global_market_history.append(record)
        history_limit = max(1, service._config.week6.history_limit)
        if len(service._global_market_history) > history_limit:
            overflow = len(service._global_market_history) - history_limit
            if overflow > 0:
                service._global_market_history = service._global_market_history[overflow:]
        self.persist_week6_state()
        service._record_audit_event(
            event_type="week6_global_snapshot",
            trace_id=source_trace_id,
            payload={"snapshot": normalized},
        )
        return {"snapshot": normalized, "history_count": len(service._global_market_history)}

    def global_market_snapshot(self) -> dict[str, object]:
        return {"snapshot": dict(self._service._global_market_snapshot)}

    def global_market_history(self, limit: int = 50) -> dict[str, object]:
        service = self._service
        capped_limit = max(1, min(limit, max(1, service._config.week6.history_limit)))
        recent = service._global_market_history[-capped_limit:]
        return {"records": len(recent), "history": recent}

    def resolve_week6_state_file(self) -> Path:
        project_root = Path(__file__).resolve().parents[3]
        return project_root / "artifacts" / "week6" / "week6_state.json"

    def persist_week6_state(self) -> None:
        service = self._service
        if not type(service)._week6_persistence_enabled():
            return
        payload = {
            "global_market_snapshot": service._global_market_snapshot,
            "global_market_history": service._global_market_history,
            "regulatory_watchlist": service._regulatory_watchlist,
            "week6_data_quality_latest": service._last_week6_data_quality_report,
            "week6_data_quality_history": service._week6_data_quality_history,
        }
        try:
            state_file = service._week6_state_file
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with state_file.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            service._record_audit_event(
                event_type="week6_state_persist_failed",
                level="warn",
                message=str(exc),
            )

    def load_week6_state(self) -> None:
        service = self._service
        if not type(service)._week6_persistence_enabled():
            return
        state_file = service._week6_state_file
        if not state_file.exists():
            return
        try:
            with state_file.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except Exception as exc:
            service._record_audit_event(
                event_type="week6_state_load_failed",
                level="warn",
                message=str(exc),
            )
            return
        if not isinstance(payload, dict):
            return

        raw_snapshot = payload.get("global_market_snapshot")
        if isinstance(raw_snapshot, dict):
            service._global_market_snapshot = {
                "us_index_change_pct": _as_float(
                    raw_snapshot.get("us_index_change_pct"),
                    default=0.0,
                ),
                "a50_change_pct": _as_float(raw_snapshot.get("a50_change_pct"), default=0.0),
                "usd_cnh_change_pct": _as_float(
                    raw_snapshot.get("usd_cnh_change_pct"),
                    default=0.0,
                ),
                "commodity_change_pct": _as_float(
                    raw_snapshot.get("commodity_change_pct"),
                    default=0.0,
                ),
                "a_share_correlation": _as_float(
                    raw_snapshot.get("a_share_correlation"),
                    default=0.60,
                ),
            }

        history: list[dict[str, object]] = []
        raw_history = payload.get("global_market_history")
        if isinstance(raw_history, list):
            for item in raw_history:
                if isinstance(item, dict):
                    history.append(item)
        history_limit = max(1, service._config.week6.history_limit)
        if len(history) > history_limit:
            history = history[-history_limit:]
        service._global_market_history = history

        watchlist: dict[str, dict[str, object]] = {}
        raw_watchlist = payload.get("regulatory_watchlist")
        if isinstance(raw_watchlist, dict):
            for symbol, item in raw_watchlist.items():
                normalized_symbol = str(symbol).strip()
                if not normalized_symbol or not isinstance(item, dict):
                    continue
                watchlist[normalized_symbol] = {
                    "symbol": normalized_symbol,
                    "tag": str(item.get("tag", "")).strip().lower(),
                    "note": str(item.get("note", "")).strip(),
                    "updated_at": str(item.get("updated_at", "")),
                }
        service._regulatory_watchlist = watchlist

        quality_history: list[dict[str, object]] = []
        raw_quality_history = payload.get("week6_data_quality_history")
        if isinstance(raw_quality_history, list):
            for item in raw_quality_history:
                if isinstance(item, dict):
                    quality_history.append(item)
        quality_history_limit = max(1, service._config.week6.data_quality_history_limit)
        if len(quality_history) > quality_history_limit:
            quality_history = quality_history[-quality_history_limit:]
        service._week6_data_quality_history = quality_history

        raw_quality_latest = payload.get("week6_data_quality_latest")
        if isinstance(raw_quality_latest, dict):
            service._last_week6_data_quality_report = raw_quality_latest
        elif quality_history:
            service._last_week6_data_quality_report = quality_history[-1]


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))
