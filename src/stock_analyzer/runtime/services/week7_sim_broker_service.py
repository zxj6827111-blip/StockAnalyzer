"""Week 7 sim-broker reporting workflows extracted from the main runtime service."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast


class RuntimeWeek7SimBrokerService:
    """Delegated weekly sim-broker reporting and export helpers."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def run_week7_sim_broker_weekly(
        self,
        days: int = 7,
        timestamp: datetime | None = None,
        export_enabled: bool | None = None,
        notify_enabled: bool | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        if not service._config.sim_broker_weekly.enabled:
            return {
                "accepted": False,
                "code": "disabled",
                "message": "sim_broker_weekly disabled by config",
            }

        capped_days = max(1, days)
        weekly = service.reconcile_weekly_report(days=capped_days)
        sim_vs_broker = weekly.get("sim_vs_broker", {})
        if not isinstance(sim_vs_broker, dict):
            sim_vs_broker = {}
        quality = cast(
            dict[str, object],
            service._execution_quality_snapshot(
                days=capped_days,
                trades=service.portfolio_trades(limit=300),
            ),
        )

        alignment_rate = _as_float(sim_vs_broker.get("alignment_rate"), default=0.0)
        max_abs_diff = _as_float(sim_vs_broker.get("max_abs_diff"), default=0.0)
        mismatch_records = _as_int(weekly.get("mismatch_records"), default=0)
        manual_trade_ratio = _as_float(quality.get("manual_trade_ratio"), default=0.0)

        cause_breakdown = sim_vs_broker.get("cause_breakdown", {})
        if not isinstance(cause_breakdown, dict):
            cause_breakdown = {}
        position_diff_count = _as_int(cause_breakdown.get("position_diff"), default=0)
        missing_strategy_count = _as_int(cause_breakdown.get("missing_in_strategy"), default=0)
        missing_broker_count = _as_int(cause_breakdown.get("missing_in_broker"), default=0)
        total_cause = position_diff_count + missing_strategy_count + missing_broker_count
        cause_ratio = {
            "position_diff": (
                round(position_diff_count / total_cause, 4) if total_cause > 0 else 0.0
            ),
            "missing_in_strategy": (
                round(missing_strategy_count / total_cause, 4) if total_cause > 0 else 0.0
            ),
            "missing_in_broker": (
                round(missing_broker_count / total_cause, 4) if total_cause > 0 else 0.0
            ),
        }

        score_raw = (
            100.0
            - (1.0 - alignment_rate) * 120.0
            - min(max_abs_diff * 1200.0, 35.0)
            - min(manual_trade_ratio * 30.0, 20.0)
            - min(float(mismatch_records), 20.0)
        )
        score = round(_clamp(score_raw, 0.0, 100.0), 2)
        if score >= 85 and mismatch_records == 0:
            status = "healthy"
        elif score >= 70:
            status = "watch"
        else:
            status = "action_required"

        recommendations: list[str] = []
        if alignment_rate < 0.98:
            recommendations.append(
                "Increase close-time broker snapshot discipline "
                "to keep next-day risk baseline aligned"
            )
        if cause_ratio["missing_in_broker"] >= 0.4:
            recommendations.append(
                "Investigate missing broker-side positions and verify command-to-broker mapping"
            )
        if cause_ratio["missing_in_strategy"] >= 0.4:
            recommendations.append(
                "Investigate stale strategy positions and "
                "ensure closed symbols are removed promptly"
            )
        if cause_ratio["position_diff"] >= 0.5:
            recommendations.append(
                "Reduce manual rebalance amplitude and prefer standard command-channel updates"
            )
        if manual_trade_ratio >= 0.4:
            recommendations.append(
                "Lower manual intervention ratio to reduce execution-path divergence"
            )
        if not recommendations:
            recommendations.append(
                "Keep current reconcile workflow and monitor weekly drift stability"
            )

        drilldown = service._build_week7_sim_broker_drilldown(manual_trade_ratio=manual_trade_ratio)
        attribution = {
            "cause_breakdown": cause_breakdown,
            "cause_ratio": cause_ratio,
            "top_diff_symbols": sim_vs_broker.get("top_diff_symbols", []),
            "missing_in_strategy_top": sim_vs_broker.get("missing_in_strategy_top", []),
            "missing_in_broker_top": sim_vs_broker.get("missing_in_broker_top", []),
        }
        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "days": capped_days,
            "status": status,
            "score": score,
            "summary": {
                "alignment_rate": round(alignment_rate, 4),
                "mismatch_records": mismatch_records,
                "max_abs_diff": round(max_abs_diff, 6),
                "manual_trade_ratio": round(manual_trade_ratio, 4),
            },
            "weekly": weekly,
            "execution_quality": quality,
            "attribution": attribution,
            "drilldown": drilldown,
            "recommendations": recommendations,
        }
        report["trend"] = service._build_week7_sim_broker_trend_preview(report=report)

        use_export = service._config.sim_broker_weekly.export_enabled
        if export_enabled is not None:
            use_export = export_enabled
        artifact = (
            service._persist_week7_sim_broker_report(report=report, now=now)
            if use_export
            else {}
        )
        report["artifact"] = artifact
        service._last_week7_sim_broker_report = report
        service._week7_sim_broker_history.append(report)
        history_limit = max(1, service._config.sim_broker_weekly.history_limit)
        if len(service._week7_sim_broker_history) > history_limit:
            overflow = len(service._week7_sim_broker_history) - history_limit
            if overflow > 0:
                service._week7_sim_broker_history = service._week7_sim_broker_history[overflow:]

        service._record_audit_event(
            event_type="week7_sim_broker_weekly",
            trace_id=source_trace_id,
            level="warn" if status != "healthy" else "info",
            payload={
                "status": status,
                "score": score,
                "summary": report["summary"],
            },
        )

        use_notify = service._config.sim_broker_weekly.auto_notify
        if notify_enabled is not None:
            use_notify = notify_enabled
        if use_notify and status != "healthy":
            service.notify(
                title=_push_title(priority="P1", category="weekly", summary="sim broker deviation"),
                content=(
                    f"状态={_sim_broker_status_zh(status)}；得分={score:.2f}；"
                    f"一致率={alignment_rate:.2%}；差异条数={mismatch_records}；"
                    f"最大绝对偏差={max_abs_diff:.4f}"
                ),
                level="warn",
                trace_id=source_trace_id,
            )
        return report

    def latest_week7_sim_broker_report(self) -> dict[str, object] | None:
        return cast(dict[str, object] | None, self._service._last_week7_sim_broker_report)

    def week7_sim_broker_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        max_limit = max(1, service._config.sim_broker_weekly.history_limit)
        capped_limit = max(1, min(limit, max_limit))
        recent = service._week7_sim_broker_history[-capped_limit:]
        return {"records": len(recent), "reports": recent}

    def _build_week7_sim_broker_drilldown(
        self,
        manual_trade_ratio: float,
    ) -> dict[str, object]:
        service = self._service
        strategy_map = service._portfolio.position_map()
        strategy_symbols = set(strategy_map.keys())
        broker_symbols = set(service._broker_positions.keys())
        shared_symbols = strategy_symbols & broker_symbols

        strategy_exposure = sum(float(value) for value in strategy_map.values())
        broker_exposure = sum(float(value) for value in service._broker_positions.values())
        account_rows = [
            {
                "account": "strategy_book",
                "symbol_count": len(strategy_symbols),
                "exposure": round(strategy_exposure, 6),
                "shared_symbols": len(shared_symbols),
            },
            {
                "account": "broker_snapshot",
                "symbol_count": len(broker_symbols),
                "exposure": round(broker_exposure, 6),
                "shared_symbols": len(shared_symbols),
            },
        ]

        manual_by_strategy: dict[str, int] = {}
        trades_by_strategy: dict[str, int] = {}
        for item in service.portfolio_trades(limit=300):
            strategy = str(item.get("strategy", "")).strip() or "unknown"
            reason = str(item.get("reason", "")).strip()
            trades_by_strategy[strategy] = trades_by_strategy.get(strategy, 0) + 1
            if reason.startswith("manual_"):
                manual_by_strategy[strategy] = manual_by_strategy.get(strategy, 0) + 1

        tolerance = max(0.0, service._config.reconcile.position_tolerance)
        per_strategy: dict[str, dict[str, float | int | str]] = {}
        for item in service.portfolio_positions():
            symbol = str(item.get("symbol", "")).strip()
            strategy = str(item.get("strategy", "")).strip() or "unknown"
            if not symbol:
                continue
            strategy_target = _as_float(item.get("target_position"), default=0.0)
            broker_target = _as_float(service._broker_positions.get(symbol), default=0.0)
            diff = abs(strategy_target - broker_target)
            mismatch = symbol not in broker_symbols or diff > tolerance
            current = per_strategy.setdefault(
                strategy,
                {
                    "strategy": strategy,
                    "symbol_count": 0,
                    "simulated_exposure": 0.0,
                    "broker_exposure": 0.0,
                    "abs_diff_exposure": 0.0,
                    "mismatch_symbols": 0,
                },
            )
            current["symbol_count"] = _as_int(current.get("symbol_count"), default=0) + 1
            current["simulated_exposure"] = (
                _as_float(current.get("simulated_exposure"), default=0.0) + strategy_target
            )
            current["broker_exposure"] = (
                _as_float(current.get("broker_exposure"), default=0.0) + broker_target
            )
            current["abs_diff_exposure"] = (
                _as_float(current.get("abs_diff_exposure"), default=0.0) + diff
            )
            if mismatch:
                current["mismatch_symbols"] = (
                    _as_int(current.get("mismatch_symbols"), default=0) + 1
                )

        broker_only_symbols = sorted(broker_symbols - strategy_symbols)
        if broker_only_symbols:
            broker_only_entry = per_strategy.setdefault(
                "broker_only",
                {
                    "strategy": "broker_only",
                    "symbol_count": 0,
                    "simulated_exposure": 0.0,
                    "broker_exposure": 0.0,
                    "abs_diff_exposure": 0.0,
                    "mismatch_symbols": 0,
                },
            )
            for symbol in broker_only_symbols:
                broker_target = _as_float(service._broker_positions.get(symbol), default=0.0)
                broker_only_entry["symbol_count"] = (
                    _as_int(broker_only_entry.get("symbol_count"), default=0) + 1
                )
                broker_only_entry["broker_exposure"] = (
                    _as_float(broker_only_entry.get("broker_exposure"), default=0.0)
                    + broker_target
                )
                broker_only_entry["abs_diff_exposure"] = (
                    _as_float(broker_only_entry.get("abs_diff_exposure"), default=0.0)
                    + abs(broker_target)
                )
                broker_only_entry["mismatch_symbols"] = (
                    _as_int(broker_only_entry.get("mismatch_symbols"), default=0) + 1
                )

        strategy_rows: list[dict[str, object]] = []
        for strategy, strategy_metrics in per_strategy.items():
            symbol_count = _as_int(strategy_metrics.get("symbol_count"), default=0)
            mismatch_symbols = _as_int(strategy_metrics.get("mismatch_symbols"), default=0)
            trade_count = trades_by_strategy.get(strategy, 0)
            manual_count = manual_by_strategy.get(strategy, 0)
            strategy_rows.append(
                {
                    "strategy": strategy,
                    "symbol_count": symbol_count,
                    "simulated_exposure": round(
                        _as_float(strategy_metrics.get("simulated_exposure"), default=0.0),
                        6,
                    ),
                    "broker_exposure": round(
                        _as_float(strategy_metrics.get("broker_exposure"), default=0.0),
                        6,
                    ),
                    "abs_diff_exposure": round(
                        _as_float(strategy_metrics.get("abs_diff_exposure"), default=0.0),
                        6,
                    ),
                    "mismatch_symbols": mismatch_symbols,
                    "mismatch_ratio": (
                        round(mismatch_symbols / symbol_count, 4) if symbol_count > 0 else 0.0
                    ),
                    "manual_trade_ratio": (
                        round(manual_count / trade_count, 4) if trade_count > 0 else 0.0
                    ),
                }
            )
        strategy_rows.sort(
            key=lambda row: (
                -_as_float(row.get("abs_diff_exposure"), default=0.0),
                -_as_int(row.get("mismatch_symbols"), default=0),
                str(row.get("strategy", "")),
            )
        )
        return {
            "accounts": account_rows,
            "account_summary": {
                "shared_symbols": len(shared_symbols),
                "exposure_gap": round(strategy_exposure - broker_exposure, 6),
                "symbol_gap": len(strategy_symbols) - len(broker_symbols),
                "manual_trade_ratio": round(manual_trade_ratio, 4),
            },
            "strategies": strategy_rows,
        }

    def _build_week7_sim_broker_trend_preview(
        self,
        report: dict[str, object],
        limit: int = 12,
    ) -> dict[str, object]:
        service = self._service
        history_cap = max(0, limit - 1)
        source: list[dict[str, object]] = []
        if history_cap > 0:
            source.extend(service._week7_sim_broker_history[-history_cap:])
        source.append(report)

        points: list[dict[str, object]] = []
        for item in source:
            summary = item.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            points.append(
                {
                    "timestamp": str(item.get("timestamp", "")),
                    "status": str(item.get("status", "")),
                    "score": round(_as_float(item.get("score"), default=0.0), 2),
                    "alignment_rate": round(
                        _as_float(summary.get("alignment_rate"), default=0.0),
                        4,
                    ),
                    "max_abs_diff": round(
                        _as_float(summary.get("max_abs_diff"), default=0.0),
                        6,
                    ),
                    "manual_trade_ratio": round(
                        _as_float(summary.get("manual_trade_ratio"), default=0.0),
                        4,
                    ),
                }
            )
        return {"records": len(points), "points": points}

    def _persist_week7_sim_broker_report(
        self,
        report: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        service = self._service
        export_dir = service._resolve_week7_sim_broker_export_dir()
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime("%Y%m%d-%H%M%S")
            json_path = export_dir / f"week7_sim_broker_{stamp}.json"
            csv_path = export_dir / f"week7_sim_broker_{stamp}_summary.csv"
            with json_path.open("w", encoding="utf-8") as fp:
                json.dump(report, fp, ensure_ascii=False, indent=2)

            summary = report.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            drilldown = report.get("drilldown", {})
            if not isinstance(drilldown, dict):
                drilldown = {}
            account_summary = drilldown.get("account_summary", {})
            if not isinstance(account_summary, dict):
                account_summary = {}
            with csv_path.open("w", encoding="utf-8", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(
                    [
                        "timestamp",
                        "days",
                        "status",
                        "score",
                        "alignment_rate",
                        "mismatch_records",
                        "max_abs_diff",
                        "manual_trade_ratio",
                        "shared_symbols",
                        "exposure_gap",
                        "symbol_gap",
                    ]
                )
                writer.writerow(
                    [
                        str(report.get("timestamp", "")),
                        _as_int(report.get("days"), default=0),
                        str(report.get("status", "")),
                        _as_float(report.get("score"), default=0.0),
                        _as_float(summary.get("alignment_rate"), default=0.0),
                        _as_int(summary.get("mismatch_records"), default=0),
                        _as_float(summary.get("max_abs_diff"), default=0.0),
                        _as_float(summary.get("manual_trade_ratio"), default=0.0),
                        _as_int(account_summary.get("shared_symbols"), default=0),
                        _as_float(account_summary.get("exposure_gap"), default=0.0),
                        _as_int(account_summary.get("symbol_gap"), default=0),
                    ]
                )
            return {
                "dir": str(export_dir),
                "json_path": str(json_path),
                "summary_csv_path": str(csv_path),
            }
        except Exception as exc:
            return {
                "dir": str(export_dir),
                "error": str(exc),
            }

    def _resolve_week7_sim_broker_export_dir(self) -> Path:
        configured = self._service._config.sim_broker_weekly.export_dir.strip()
        export_dir = Path(configured)
        if export_dir.is_absolute():
            return export_dir
        project_root = Path(__file__).resolve().parents[4]
        return project_root / export_dir


def _push_title(priority: str, category: str, summary: str) -> str:
    badge_map = {
        "P0": "【紧急】",
        "P1": "【重要】",
        "P2": "【日常】",
        "P3": "【参考】",
    }
    category_map = {
        "weekly": "周报",
    }
    badge = badge_map.get(priority.strip().upper(), "【日常】")
    category_text = category_map.get(category.strip().lower(), category.strip() or "通知")
    return f"{badge}【{category_text}】{summary.strip() or '-'}"


def _sim_broker_status_zh(status: str) -> str:
    mapping = {
        "healthy": "健康",
        "watch": "观察",
        "action_required": "需要处理",
    }
    return mapping.get(status.strip().lower(), status or "未知")


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
