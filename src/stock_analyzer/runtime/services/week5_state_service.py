"""Week5 report state and watchlist sync helpers extracted from the runtime service."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeWeek5StateService:
    """Manage week5 report history and watchlist synchronization."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def latest_week5_scan_report(self) -> dict[str, object] | None:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        latest = service._last_week5_scan_report
        return latest if isinstance(latest, dict) else None

    def week5_scan_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        max_limit = max(1, service._config.week5.history_limit)
        capped_limit = max(1, min(limit, max_limit))
        recent = service._week5_scan_history[-capped_limit:]
        return {
            "records": len(recent),
            "reports": recent,
        }

    def latest_preserved_watchlist_symbols(
        self,
        top_k_override: int | None = None,
    ) -> list[str]:
        latest = self.latest_week5_scan_report()
        if not isinstance(latest, dict):
            return []

        if top_k_override is not None and top_k_override > 0:
            top_k = max(1, _as_int(top_k_override, default=50))
        else:
            top_k = max(
                1,
                _as_int(self._service._config.week5.auto_sync_watchlist_top_k, default=50),
            )

        watchlist_sync = latest.get("watchlist_sync")
        if isinstance(watchlist_sync, dict):
            raw_symbols = watchlist_sync.get("symbols")
            if isinstance(raw_symbols, list):
                symbols = [
                    symbol
                    for symbol in (_normalize_a_share_symbol(item) for item in raw_symbols)
                    if symbol
                ]
                if symbols:
                    return _dedupe_preserve_order(symbols)[:top_k]

        signal_pool = latest.get("signal_pool")
        if isinstance(signal_pool, dict):
            ranking = signal_pool.get("ranking")
            if isinstance(ranking, dict):
                raw_selected = ranking.get("selected_symbols")
                if isinstance(raw_selected, list):
                    selected = [
                        symbol
                        for symbol in (_normalize_a_share_symbol(item) for item in raw_selected)
                        if symbol
                    ]
                    if selected:
                        return _dedupe_preserve_order(selected)[:top_k]

        derived = self.derive_watchlist_candidates_from_week5(
            report=latest,
            top_k_override=top_k_override,
        )
        return derived[:top_k]

    def derive_watchlist_candidates_from_week5(
        self,
        report: dict[str, object],
        top_k_override: int | None = None,
    ) -> list[str]:
        service = self._service
        min_score = _as_float(service._config.week5.auto_sync_watchlist_min_score, default=65.0)
        if top_k_override is not None and top_k_override > 0:
            top_k = max(1, _as_int(top_k_override, default=50))
        else:
            top_k = max(1, _as_int(service._config.week5.auto_sync_watchlist_top_k, default=50))
        allowed_actions = {
            str(item).strip().lower()
            for item in service._config.week5.auto_sync_watchlist_allowed_actions
            if str(item).strip()
        }
        if not allowed_actions:
            allowed_actions = {"buy", "watch"}

        candidates: list[tuple[float, float, str]] = []
        first_board = report.get("first_board")
        if isinstance(first_board, dict):
            for key in ("leaders", "candidates"):
                rows = first_board.get(key)
                if not isinstance(rows, list):
                    continue
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    symbol = _normalize_a_share_symbol(item.get("symbol"))
                    if not symbol:
                        continue
                    if _candidate_has_hard_blockers(item):
                        continue
                    action = str(item.get("action", "")).strip().lower()
                    if action not in allowed_actions:
                        continue
                    if bool(item.get("isolated", False)):
                        continue
                    score = _as_float(item.get("score"), default=0.0)
                    if score < min_score:
                        continue
                    leader_score = _as_float(item.get("leader_score"), default=score)
                    candidates.append((leader_score, score, symbol))

        signal_pool = report.get("signal_pool")
        if isinstance(signal_pool, dict):
            ranking = signal_pool.get("ranking")
            ranking_score_key = (
                str(ranking.get("score_key", "")).strip()
                if isinstance(ranking, dict)
                else ""
            )
            if not ranking_score_key:
                ranking_score_key = "shortlist_score"
            signal_rows = signal_pool.get("candidates")
            if isinstance(signal_rows, list):
                for item in signal_rows:
                    if not isinstance(item, dict):
                        continue
                    symbol = _normalize_a_share_symbol(item.get("symbol"))
                    if not symbol:
                        continue
                    if _candidate_has_hard_blockers(item):
                        continue
                    action = str(item.get("action", "")).strip().lower()
                    if action not in allowed_actions:
                        continue
                    ranking_score = _as_float(
                        item.get(ranking_score_key),
                        default=_as_float(
                            item.get("shortlist_score"),
                            default=_as_float(item.get("score"), default=0.0),
                        ),
                    )
                    shortlist_score = _as_float(
                        item.get("shortlist_score"),
                        default=_as_float(item.get("score"), default=0.0),
                    )
                    if ranking_score < min_score:
                        continue
                    raw_score = _as_float(item.get("score"), default=shortlist_score)
                    candidates.append((ranking_score, raw_score, symbol))

        if not candidates:
            return []
        candidates.sort(key=lambda row: (-row[0], -row[1], row[2]))
        symbols = [row[2] for row in candidates]
        deduped = _dedupe_preserve_order(symbols)
        return deduped[:top_k]

    def auto_sync_watchlist_from_week5_report(
        self,
        report: dict[str, object],
        reason: str,
        top_k_override: int | None = None,
        allow_signal_pool_fallback: bool = True,
    ) -> dict[str, object]:
        service = self._service
        previous = list(service._state.watchlist)
        selected = self.derive_watchlist_candidates_from_week5(
            report=report,
            top_k_override=top_k_override,
        )
        fallback_applied = False
        if not selected and allow_signal_pool_fallback:
            selected = self._fallback_watchlist_candidates_from_signal_pool(
                report=report,
                top_k_override=top_k_override,
            )
            fallback_applied = bool(selected)
        if not selected and not allow_signal_pool_fallback:
            return {
                "enabled": True,
                "updated": False,
                "reason": "intraday_preserve_existing",
                "watchlist_before": len(previous),
                "watchlist_after": len(previous),
                "symbols": previous,
            }
        keep_if_empty = bool(service._config.week5.auto_sync_watchlist_keep_if_empty)
        if not selected and keep_if_empty:
            empty_keep_streak = _consecutive_empty_keep_streak(service._week5_scan_history) + 1
            preserve_age_hours = _watchlist_materialization_age_hours(
                history=service._week5_scan_history,
                reference_timestamp=str(report.get("timestamp", "")).strip(),
            )
            grace_runs = max(
                0,
                _as_int(service._config.week5.auto_sync_watchlist_empty_grace_runs, default=1),
            )
            max_age_hours = max(
                0.0,
                _as_float(
                    service._config.week5.auto_sync_watchlist_preserve_max_age_hours,
                    default=18.0,
                ),
            )
            preserve_allowed = (
                bool(previous)
                and empty_keep_streak <= max(1, grace_runs)
                and (
                    preserve_age_hours is None
                    or max_age_hours <= 0.0
                    or preserve_age_hours <= max_age_hours
                )
            )
            if preserve_allowed:
                return {
                    "enabled": True,
                    "updated": False,
                    "reason": "empty_candidates_keep_existing",
                    "watchlist_before": len(previous),
                    "watchlist_after": len(previous),
                    "symbols": previous,
                    "empty_keep_streak": empty_keep_streak,
                    "preserve_age_hours": preserve_age_hours,
                }
            expired = bool(previous)
            if expired:
                service._state.watchlist = []
                service._persist_runtime_state_to_disk()
            return {
                "enabled": True,
                "updated": expired,
                "reason": "empty_candidates_expired_watchlist",
                "watchlist_before": len(previous),
                "watchlist_after": len(service._state.watchlist),
                "symbols": list(service._state.watchlist),
                "empty_keep_streak": empty_keep_streak,
                "preserve_age_hours": preserve_age_hours,
            }
        update = service._replace_watchlist(
            symbols=selected if selected else previous,
            reason=reason or "week5_auto_sync",
        )
        payload = {
            "enabled": True,
            "updated": bool(update.get("updated", False)),
            "reason": (
                f"{update.get('reason', '')}:signal_pool_fallback"
                if fallback_applied
                else str(update.get("reason", ""))
            ),
            "watchlist_before": _as_int(update.get("watchlist_before"), default=len(previous)),
            "watchlist_after": _as_int(
                update.get("watchlist_after"),
                default=len(service._state.watchlist),
            ),
            "symbols": list(service._state.watchlist),
        }
        if bool(payload["updated"]):
            service._record_audit_event(
                event_type="watchlist_auto_synced",
                payload={
                    "reason": payload["reason"],
                    "watchlist_after": payload["watchlist_after"],
                    "symbols": payload["symbols"],
                },
            )
        return payload

    def _fallback_watchlist_candidates_from_signal_pool(
        self,
        *,
        report: dict[str, object],
        top_k_override: int | None = None,
    ) -> list[str]:
        service = self._service
        if top_k_override is not None and top_k_override > 0:
            top_k = max(1, _as_int(top_k_override, default=50))
        else:
            top_k = max(1, _as_int(service._config.week5.auto_sync_watchlist_top_k, default=50))
        raw_pool = report.get("signal_pool")
        if not isinstance(raw_pool, dict):
            return []

        rows = raw_pool.get("candidates")
        row_by_symbol: dict[str, dict[str, object]] = {}
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                symbol = _normalize_a_share_symbol(item.get("symbol"))
                if symbol:
                    row_by_symbol[symbol] = item

        ranking = raw_pool.get("ranking")
        if isinstance(ranking, dict):
            raw_selected_symbols = ranking.get("selected_symbols")
            if isinstance(raw_selected_symbols, list):
                selected_symbols = [
                    symbol
                    for symbol in (
                        _normalize_a_share_symbol(item) for item in raw_selected_symbols
                    )
                    if symbol
                    and not _candidate_has_hard_blockers(row_by_symbol.get(symbol, {}))
                ]
                if selected_symbols:
                    return _dedupe_preserve_order(selected_symbols)[:top_k]

        if not isinstance(rows, list):
            return []
        fallback_symbols = [
            symbol
            for symbol in (
                _normalize_a_share_symbol(item.get("symbol"))
                for item in rows
                if isinstance(item, dict) and not _candidate_has_hard_blockers(item)
            )
            if symbol
        ]
        return _dedupe_preserve_order(fallback_symbols)[:top_k]

    def store_week5_scan_report(self, report: dict[str, object]) -> None:
        service = self._service
        service._last_week5_scan_report = report
        service._week5_scan_history.append(report)
        history_limit = max(1, service._config.week5.history_limit)
        if len(service._week5_scan_history) > history_limit:
            overflow = len(service._week5_scan_history) - history_limit
            if overflow > 0:
                service._week5_scan_history = service._week5_scan_history[overflow:]


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _normalize_a_share_symbol(value: object) -> str:
    return cast(str, _runtime_service_module()._normalize_a_share_symbol(value))


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    return cast(list[str], _runtime_service_module()._dedupe_preserve_order(items))


def _candidate_has_hard_blockers(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    reasons = [str(reason).strip().lower() for reason in item.get("reasons", []) if str(reason).strip()]
    blocking_reasons = {
        "cross_review",
        "liquidity_failed",
        "risk_gate",
        "financial_filter_block",
        "time_invariant_violation",
        "feature_empty",
    }
    blocking_prefixes = (
        "financial_filter:",
        "data_source:",
        "insufficient_history_days:",
    )
    if any(reason in blocking_reasons for reason in reasons):
        return True
    if any(reason.startswith(prefix) for reason in reasons for prefix in blocking_prefixes):
        return True
    decision_trace = item.get("decision_trace")
    if not isinstance(decision_trace, dict):
        return False
    for gate_name in ("risk_gate", "liquidity_gate", "cross_review_gate"):
        gate = decision_trace.get(gate_name)
        if isinstance(gate, dict) and "passed" in gate and not bool(gate.get("passed", False)):
            return True
    financial_gate = decision_trace.get("financial_gate")
    if isinstance(financial_gate, dict) and "allowed" in financial_gate:
        return not bool(financial_gate.get("allowed", False))
    return False


def _consecutive_empty_keep_streak(history: list[dict[str, object]]) -> int:
    streak = 0
    for report in reversed(history):
        if not isinstance(report, dict):
            break
        watchlist_sync = report.get("watchlist_sync")
        if not isinstance(watchlist_sync, dict):
            break
        if str(watchlist_sync.get("reason", "")).strip() != "empty_candidates_keep_existing":
            break
        streak += 1
    return streak


def _watchlist_materialization_age_hours(
    *,
    history: list[dict[str, object]],
    reference_timestamp: str,
) -> float | None:
    reference = _parse_iso_timestamp(reference_timestamp)
    if reference is None:
        return None
    for report in reversed(history):
        if not isinstance(report, dict):
            continue
        watchlist_sync = report.get("watchlist_sync")
        if not isinstance(watchlist_sync, dict):
            continue
        symbols = watchlist_sync.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            continue
        reason = str(watchlist_sync.get("reason", "")).strip()
        if reason in {"empty_candidates_keep_existing", "intraday_preserve_existing", "disabled"}:
            continue
        timestamp = _parse_iso_timestamp(str(report.get("timestamp", "")).strip())
        if timestamp is None:
            continue
        return round(max(0.0, (reference - timestamp).total_seconds()) / 3600.0, 4)
    return None


def _parse_iso_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
