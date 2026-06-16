"""Broker snapshot and reconcile workflows extracted from the main runtime service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from stock_analyzer.portfolio.reconcile import reconcile_positions


class RuntimeReconcileService:
    """Delegated broker-snapshot, reconcile, and reconcile-summary workflows."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def update_broker_snapshot(
        self,
        positions: list[dict[str, object]],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        parsed = _parse_broker_positions(positions)
        parsed_details = _parse_broker_position_details(positions)
        service._broker_snapshot_updated_at = datetime.now().isoformat()
        service._broker_positions = parsed
        service._broker_position_details = parsed_details
        service._state.reconcile_required = False
        service._persist_runtime_state_to_disk()
        quantity_records = sum(
            1 for item in parsed_details.values() if _as_int(item.get("quantity"), default=0) > 0
        )
        account_records = sum(
            1 for item in parsed_details.values() if str(item.get("account", "")).strip()
        )
        payload = {
            "source_trace_id": source_trace_id,
            "broker_positions": len(parsed),
            "symbols": sorted(parsed.keys()),
            "quantity_records": quantity_records,
            "account_records": account_records,
        }
        service._record_audit_event(
            event_type="broker_snapshot",
            trace_id=source_trace_id,
            payload=payload,
        )
        return payload

    def bootstrap_broker_snapshot_from_portfolio(
        self,
        *,
        source_trace_id: str = "",
        allow_empty: bool = False,
    ) -> dict[str, object]:
        service = self._service
        portfolio_positions = service._portfolio.positions()
        positions = _broker_snapshot_positions_from_portfolio(portfolio_positions)
        if not positions and not allow_empty:
            service._record_audit_event(
                event_type="broker_snapshot_from_portfolio_skipped",
                trace_id=source_trace_id,
                payload={
                    "reason": "empty_portfolio",
                    "portfolio_positions": 0,
                    "allow_empty": False,
                },
            )
            return {
                "status": "skipped_empty_portfolio",
                "source_trace_id": source_trace_id,
                "portfolio_positions": 0,
                "broker_positions": 0,
                "symbols": [],
                "allow_empty": False,
                "reason": "portfolio has no open positions; pass allow_empty to write an empty simulated broker snapshot",
            }
        snapshot = self.update_broker_snapshot(
            positions=positions,
            source_trace_id=source_trace_id,
        )
        snapshot["status"] = "ok"
        snapshot["source"] = "portfolio"
        snapshot["portfolio_positions"] = len(portfolio_positions)
        snapshot["allow_empty"] = allow_empty
        return snapshot

    def latest_reconcile_report(self) -> dict[str, object] | None:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        report = service._last_reconcile_report
        return report if isinstance(report, dict) else None

    def reconcile_weekly_report(self, days: int = 7) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        capped_days = max(1, days)
        anchor_ts = datetime.now().timestamp()
        if service._reconcile_history:
            latest_ts = _report_timestamp(service._reconcile_history[-1])
            if latest_ts > 0:
                anchor_ts = latest_ts
        cutoff = anchor_ts - capped_days * 86400
        recent = [item for item in service._reconcile_history if _report_timestamp(item) >= cutoff]
        status_breakdown: dict[str, int] = {}
        total_matched = 0
        total_mismatch = 0
        total_strategy_positions = 0
        total_broker_positions = 0
        diff_events = 0
        diff_sum = 0.0
        max_abs_diff = 0.0
        diff_symbol_totals: dict[str, float] = {}
        missing_in_strategy_counts: dict[str, int] = {}
        missing_in_broker_counts: dict[str, int] = {}
        cause_breakdown = {
            "position_diff": 0,
            "missing_in_strategy": 0,
            "missing_in_broker": 0,
            "quantity_diff": 0,
            "account_diff": 0,
        }

        for item in recent:
            status = str(item.get("status", "")).strip() or "unknown"
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
            total_matched += _as_int(item.get("matched_count"), default=0)
            total_mismatch += _as_int(item.get("mismatch_count"), default=0)
            total_strategy_positions += _as_int(item.get("strategy_positions"), default=0)
            total_broker_positions += _as_int(item.get("broker_positions"), default=0)

            raw_diffs = item.get("diffs")
            if isinstance(raw_diffs, list):
                for diff_item in raw_diffs:
                    if not isinstance(diff_item, dict):
                        continue
                    symbol = str(diff_item.get("symbol", "")).strip()
                    if not symbol:
                        continue
                    diff_value = abs(_as_float(diff_item.get("diff"), default=0.0))
                    diff_events += 1
                    diff_sum += diff_value
                    max_abs_diff = max(max_abs_diff, diff_value)
                    diff_symbol_totals[symbol] = diff_symbol_totals.get(symbol, 0.0) + diff_value
                    cause_breakdown["position_diff"] += 1

            raw_missing_strategy = item.get("missing_in_strategy")
            if isinstance(raw_missing_strategy, list):
                for symbol_item in raw_missing_strategy:
                    symbol = str(symbol_item).strip()
                    if not symbol:
                        continue
                    missing_in_strategy_counts[symbol] = (
                        missing_in_strategy_counts.get(symbol, 0) + 1
                    )
                    cause_breakdown["missing_in_strategy"] += 1

            raw_missing_broker = item.get("missing_in_broker")
            if isinstance(raw_missing_broker, list):
                for symbol_item in raw_missing_broker:
                    symbol = str(symbol_item).strip()
                    if not symbol:
                        continue
                    missing_in_broker_counts[symbol] = missing_in_broker_counts.get(symbol, 0) + 1
                    cause_breakdown["missing_in_broker"] += 1

            raw_quantity_diffs = item.get("quantity_diffs")
            if isinstance(raw_quantity_diffs, list):
                for diff_item in raw_quantity_diffs:
                    if not isinstance(diff_item, dict):
                        continue
                    symbol = str(diff_item.get("symbol", "")).strip()
                    if symbol:
                        cause_breakdown["quantity_diff"] += 1

            raw_account_diffs = item.get("account_diffs")
            if isinstance(raw_account_diffs, list):
                for diff_item in raw_account_diffs:
                    if not isinstance(diff_item, dict):
                        continue
                    symbol = str(diff_item.get("symbol", "")).strip()
                    if symbol:
                        cause_breakdown["account_diff"] += 1

        mismatch_count = len(recent) - status_breakdown.get("ok", 0)
        comparisons = total_matched + total_mismatch
        alignment_rate = (total_matched / comparisons) if comparisons > 0 else 0.0
        avg_abs_diff = (diff_sum / diff_events) if diff_events > 0 else 0.0
        record_count = len(recent)
        avg_matched_per_run = (total_matched / record_count) if record_count > 0 else 0.0
        avg_mismatch_per_run = (total_mismatch / record_count) if record_count > 0 else 0.0

        return {
            "days": capped_days,
            "records": len(recent),
            "mismatch_records": mismatch_count,
            "ok_records": status_breakdown.get("ok", 0),
            "status_breakdown": status_breakdown,
            "sim_vs_broker": {
                "alignment_rate": alignment_rate,
                "avg_matched_per_run": avg_matched_per_run,
                "avg_mismatch_per_run": avg_mismatch_per_run,
                "avg_abs_diff": avg_abs_diff,
                "max_abs_diff": max_abs_diff,
                "total_strategy_positions": total_strategy_positions,
                "total_broker_positions": total_broker_positions,
                "cause_breakdown": cause_breakdown,
                "top_diff_symbols": _top_symbol_diffs(diff_symbol_totals),
                "missing_in_strategy_top": _top_symbol_counts(missing_in_strategy_counts),
                "missing_in_broker_top": _top_symbol_counts(missing_in_broker_counts),
            },
            "latest": service._last_reconcile_report,
        }

    def run_reconciliation(
        self,
        timestamp: datetime | None = None,
        trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        if not service._config.reconcile.enabled:
            report = {
                "timestamp": now.isoformat(),
                "status": "disabled",
                "matched_count": 0,
                "mismatch_count": 0,
                "missing_in_strategy": [],
                "missing_in_broker": [],
                "diffs": [],
                "strategy_positions": len(service._portfolio.position_map()),
                "broker_positions": len(service._broker_positions),
                "quantity_matched_count": 0,
                "account_matched_count": 0,
                "quantity_mismatch_count": 0,
                "account_mismatch_count": 0,
                "detail_mismatch_count": 0,
                "quantity_diffs": [],
                "account_diffs": [],
                "note": "reconcile disabled by config",
            }
            service._store_reconcile_report(report)
            return report

        strategy_positions = _normalize_position_map(service._portfolio.position_map())
        snapshot_freshness = _broker_snapshot_freshness(
            updated_at=service._broker_snapshot_updated_at,
            now=now,
            max_age_hours=_as_float(
                service._config.reconcile.max_broker_snapshot_age_hours,
                default=18.0,
            ),
        )
        if (
            service._config.reconcile.require_broker_snapshot_at_close
            and not service._broker_positions
        ):
            if not strategy_positions:
                report = {
                    "timestamp": now.isoformat(),
                    "status": "ok",
                    "matched_count": 0,
                    "mismatch_count": 0,
                    "missing_in_strategy": [],
                    "missing_in_broker": [],
                    "diffs": [],
                    "strategy_positions": 0,
                    "broker_positions": 0,
                    "quantity_matched_count": 0,
                    "account_matched_count": 0,
                    "quantity_mismatch_count": 0,
                    "account_mismatch_count": 0,
                    "detail_mismatch_count": 0,
                    "quantity_diffs": [],
                    "account_diffs": [],
                    "note": "no positions; reconcile skipped without broker snapshot",
                }
            else:
                report = {
                    "timestamp": now.isoformat(),
                    "status": "missing_snapshot",
                    "matched_count": 0,
                    "mismatch_count": len(strategy_positions),
                    "missing_in_strategy": [],
                    "missing_in_broker": sorted(strategy_positions.keys()),
                    "diffs": [],
                    "strategy_positions": len(strategy_positions),
                    "broker_positions": 0,
                    "quantity_matched_count": 0,
                    "account_matched_count": 0,
                    "quantity_mismatch_count": 0,
                    "account_mismatch_count": 0,
                    "detail_mismatch_count": 0,
                    "quantity_diffs": [],
                    "account_diffs": [],
                    "note": "broker snapshot is required before reconcile",
                }
        elif (
            service._config.reconcile.require_broker_snapshot_at_close
            and not bool(snapshot_freshness.get("fresh", False))
        ):
            report = {
                "timestamp": now.isoformat(),
                "status": "stale_snapshot",
                "matched_count": 0,
                "mismatch_count": len(strategy_positions),
                "missing_in_strategy": [],
                "missing_in_broker": sorted(strategy_positions.keys()),
                "diffs": [],
                "strategy_positions": len(strategy_positions),
                "broker_positions": len(service._broker_positions),
                "quantity_matched_count": 0,
                "account_matched_count": 0,
                "quantity_mismatch_count": 0,
                "account_mismatch_count": 0,
                "detail_mismatch_count": 0,
                "quantity_diffs": [],
                "account_diffs": [],
                "broker_snapshot": snapshot_freshness,
                "note": "broker snapshot is stale; reconcile skipped to avoid learning pollution",
            }
        else:
            report_obj = reconcile_positions(
                strategy_positions=strategy_positions,
                broker_positions=service._broker_positions,
                timestamp=now,
                tolerance=service._config.reconcile.position_tolerance,
            )
            report = report_obj.to_dict()
            report = service._enrich_reconcile_report_with_quantity_account(
                report=report,
                strategy_snapshot=service._portfolio.positions(),
                broker_snapshot=service._broker_position_details,
            )
            report["broker_snapshot"] = snapshot_freshness

        service._store_reconcile_report(report)
        if (
            service._config.reconcile.auto_notify_on_mismatch
            and str(report.get("status", "")) != "ok"
        ):
            service.notify(
                title=_push_title(priority="P1", category="close", summary="reconcile mismatch"),
                content=_notification_message_zh(
                    trigger="模拟盘与券商持仓对账发现差异，账实一致性被破坏。",
                    impact="若不及时处理，后续持仓监控、盈亏评估和风控动作都可能基于错误头寸继续运行。",
                    action="请立即前往本地监控雷达执行手工对账，并核对券商端实际持仓、数量和权益数据。",
                    details=[
                        f"对账状态：{_reconcile_status_zh(str(report.get('status', '')))}",
                        f"差异条数：{_as_int(report.get('mismatch_count'), default=0)}",
                        f"数量不一致：{_as_int(report.get('quantity_mismatch_count'), default=0)}",
                        f"账户不一致：{_as_int(report.get('account_mismatch_count'), default=0)}",
                    ],
                ),
                level="warn",
                trace_id=trace_id,
            )
        service._record_audit_event(
            event_type="reconcile_run",
            trace_id=trace_id,
            level="warn" if str(report.get("status", "")) != "ok" else "info",
            payload={
                "status": str(report.get("status", "")),
                "mismatch_count": _as_int(report.get("mismatch_count"), default=0),
                "strategy_positions": _as_int(report.get("strategy_positions"), default=0),
                "broker_positions": _as_int(report.get("broker_positions"), default=0),
                "quantity_mismatch_count": _as_int(
                    report.get("quantity_mismatch_count"),
                    default=0,
                ),
                "account_mismatch_count": _as_int(
                    report.get("account_mismatch_count"),
                    default=0,
                ),
            },
        )
        return report

    def _enrich_reconcile_report_with_quantity_account(
        self,
        *,
        report: dict[str, object],
        strategy_snapshot: list[dict[str, object]],
        broker_snapshot: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        strategy_map: dict[str, dict[str, object]] = {}
        for item in strategy_snapshot:
            symbol = _normalize_a_share_symbol(item.get("symbol")) or str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            quantity = _as_int(item.get("quantity"), default=0)
            strategy_map[symbol] = {
                "quantity": quantity if quantity > 0 else None,
                "account": str(item.get("account", "")).strip(),
            }

        broker_map: dict[str, dict[str, object]] = {}
        for symbol, item in broker_snapshot.items():
            if not isinstance(item, dict):
                continue
            normalized_symbol = _normalize_a_share_symbol(symbol) or str(symbol).strip()
            if not normalized_symbol:
                continue
            quantity = _as_int(item.get("quantity"), default=0)
            broker_map[normalized_symbol] = {
                "quantity": quantity if quantity > 0 else None,
                "account": str(item.get("account", "")).strip(),
            }

        shared_symbols = sorted(set(strategy_map.keys()) & set(broker_map.keys()))
        quantity_diffs: list[dict[str, object]] = []
        account_diffs: list[dict[str, object]] = []
        quantity_matched = 0
        account_matched = 0

        for symbol in shared_symbols:
            strategy_item = strategy_map.get(symbol, {})
            broker_item = broker_map.get(symbol, {})
            strategy_qty_raw = strategy_item.get("quantity")
            broker_qty_raw = broker_item.get("quantity")
            strategy_qty = strategy_qty_raw if isinstance(strategy_qty_raw, int) else None
            broker_qty = broker_qty_raw if isinstance(broker_qty_raw, int) else None

            if strategy_qty is not None and broker_qty is not None:
                if strategy_qty == broker_qty:
                    quantity_matched += 1
                else:
                    quantity_diffs.append(
                        {
                            "symbol": symbol,
                            "strategy_quantity": strategy_qty,
                            "broker_quantity": broker_qty,
                            "diff": abs(strategy_qty - broker_qty),
                        }
                    )

            strategy_account = str(strategy_item.get("account", "")).strip()
            broker_account = str(broker_item.get("account", "")).strip()
            if strategy_account and broker_account:
                if strategy_account == broker_account:
                    account_matched += 1
                else:
                    account_diffs.append(
                        {
                            "symbol": symbol,
                            "strategy_account": strategy_account,
                            "broker_account": broker_account,
                        }
                    )

        quantity_mismatch_count = len(quantity_diffs)
        account_mismatch_count = len(account_diffs)
        detail_mismatch_count = quantity_mismatch_count + account_mismatch_count
        report["quantity_matched_count"] = quantity_matched
        report["account_matched_count"] = account_matched
        report["quantity_mismatch_count"] = quantity_mismatch_count
        report["account_mismatch_count"] = account_mismatch_count
        report["detail_mismatch_count"] = detail_mismatch_count
        report["quantity_diffs"] = quantity_diffs
        report["account_diffs"] = account_diffs
        if detail_mismatch_count > 0:
            report["status"] = "mismatch"
            base_mismatch = _as_int(report.get("mismatch_count"), default=0)
            report["mismatch_count"] = base_mismatch + detail_mismatch_count
            base_note = str(report.get("note", "")).strip()
            detail_note = (
                f"quantity_mismatch={quantity_mismatch_count};"
                f"account_mismatch={account_mismatch_count}"
            )
            report["note"] = f"{base_note}; {detail_note}".strip("; ").strip()
        return report

    def _store_reconcile_report(self, report: dict[str, object]) -> None:
        service = self._service
        service._last_reconcile_report = report
        service._reconcile_history.append(report)
        if len(service._reconcile_history) > service._config.reconcile.history_limit:
            overflow = len(service._reconcile_history) - service._config.reconcile.history_limit
            if overflow > 0:
                service._reconcile_history = service._reconcile_history[overflow:]
        service._state.reconcile_required = str(report.get("status", "")) != "ok"
        service._persist_runtime_state_to_disk()


def _parse_broker_positions(positions: list[dict[str, object]]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for item in positions:
        symbol = _normalize_a_share_symbol(item.get("symbol")) or str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        target = _as_float(item.get("target_position"), default=0.0)
        if target >= 0:
            parsed[symbol] = target
    return parsed


def _parse_broker_position_details(
    positions: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    parsed: dict[str, dict[str, object]] = {}
    for item in positions:
        symbol = _normalize_a_share_symbol(item.get("symbol")) or str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        target = _as_float(item.get("target_position"), default=0.0)
        if target < 0:
            continue
        quantity = _as_int(item.get("quantity"), default=0)
        account = str(item.get("account", "")).strip()
        parsed[symbol] = {
            "target_position": target,
            "quantity": quantity if quantity > 0 else None,
            "account": account,
        }
    return parsed


def _broker_snapshot_positions_from_portfolio(
    portfolio_positions: list[dict[str, object]],
) -> list[dict[str, object]]:
    positions: list[dict[str, object]] = []
    for item in portfolio_positions:
        symbol = _normalize_a_share_symbol(item.get("symbol")) or str(item.get("symbol", "")).strip()
        target_position = _as_float(item.get("target_position"), default=-1.0)
        if not symbol or target_position < 0.0:
            continue
        snapshot_item: dict[str, object] = {
            "symbol": symbol,
            "target_position": target_position,
        }
        quantity = _as_int(item.get("quantity"), default=0)
        if quantity > 0:
            snapshot_item["quantity"] = quantity
        account = str(item.get("account", "")).strip()
        if account:
            snapshot_item["account"] = account
        positions.append(snapshot_item)
    return positions


def _normalize_position_map(position_map: dict[str, float]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_symbol, raw_position in position_map.items():
        symbol = _normalize_a_share_symbol(raw_symbol) or str(raw_symbol).strip()
        if not symbol:
            continue
        normalized[symbol] = _as_float(raw_position, default=0.0)
    return normalized


def _normalize_a_share_symbol(value: object) -> str:
    text = str(value).strip().upper()
    if not text:
        return ""
    primary = text.split(".", maxsplit=1)[0]
    digits = "".join(ch for ch in primary if ch.isdigit())
    if len(digits) != 6:
        digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) > 6:
        digits = digits[-6:]
    if len(digits) != 6:
        return ""
    if digits[0] not in {"0", "3", "4", "6", "8"}:
        return ""
    return digits


def _broker_snapshot_freshness(
    *,
    updated_at: str,
    now: datetime,
    max_age_hours: float,
) -> dict[str, object]:
    normalized_updated_at = str(updated_at or "").strip()
    max_age = max(0.0, float(max_age_hours))
    if not normalized_updated_at:
        return {
            "updated_at": "",
            "fresh": False,
            "age_hours": None,
            "max_age_hours": max_age,
            "reason": "missing_broker_snapshot_timestamp",
        }
    try:
        snapshot_time = datetime.fromisoformat(normalized_updated_at)
    except ValueError:
        return {
            "updated_at": normalized_updated_at,
            "fresh": False,
            "age_hours": None,
            "max_age_hours": max_age,
            "reason": "invalid_broker_snapshot_timestamp",
        }
    if (snapshot_time.tzinfo is None) != (now.tzinfo is None):
        snapshot_time = snapshot_time.replace(tzinfo=None)
        now = now.replace(tzinfo=None)
    age_hours = max(0.0, (now - snapshot_time).total_seconds() / 3600.0)
    return {
        "updated_at": normalized_updated_at,
        "fresh": age_hours <= max_age,
        "age_hours": round(age_hours, 4),
        "max_age_hours": max_age,
        "reason": "ok" if age_hours <= max_age else "stale_broker_snapshot",
    }


def _notification_message_zh(
    *,
    trigger: str,
    impact: str,
    action: str,
    details: list[str] | tuple[str, ...] | None = None,
    detail_title: str = "详细追踪",
) -> str:
    lines = [
        f"触发事件：{trigger.strip() or '-'}",
        f"系统影响：{impact.strip() or '-'}",
        f"建议动作：{action.strip() or '-'}",
    ]
    detail_items = [str(item).strip() for item in (details or []) if str(item).strip()]
    if detail_items:
        lines.append(f"{detail_title}：")
        lines.extend(f"- {item}" for item in detail_items)
    return "\n".join(lines)


def _push_title(priority: str, category: str, summary: str) -> str:
    badge_map = {
        "P0": "【紧急】",
        "P1": "【重要】",
        "P2": "【日常】",
        "P3": "【参考】",
    }
    category_map = {
        "close": "收盘",
    }
    normalized = priority.strip().upper()
    badge = badge_map.get(normalized, "【日常】")
    category_text = category_map.get(category.strip().lower(), category.strip() or "通知")
    summary_text = summary.strip() or "-"
    return f"{badge}【{category_text}】{summary_text}"


def _reconcile_status_zh(status: str) -> str:
    mapping = {
        "ok": "正常",
        "mismatch": "存在差异",
        "missing_snapshot": "缺少持仓快照",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status or "未知")


def _top_symbol_diffs(source: dict[str, float], limit: int = 5) -> list[dict[str, object]]:
    ranked = sorted(source.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        {
            "symbol": symbol,
            "cumulative_abs_diff": value,
        }
        for symbol, value in ranked
    ]


def _top_symbol_counts(source: dict[str, int], limit: int = 5) -> list[dict[str, object]]:
    ranked = sorted(source.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        {
            "symbol": symbol,
            "count": count,
        }
        for symbol, count in ranked
    ]


def _as_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _report_timestamp(report: dict[str, object]) -> float:
    raw = report.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0
    return 0.0
