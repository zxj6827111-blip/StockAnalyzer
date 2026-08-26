"""Full-market Week5 automation orchestration.

This module is deliberately thin: the existing Week5 scan and pipeline remain
the source of scoring and risk decisions, while this service owns candidate
state, snapshot replay, stage separation and signal-only safety.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from stock_analyzer.feature.snapshot import load_feature_snapshot, snapshot_is_current
from stock_analyzer.ops.nightly_readiness import check_nightly_readiness
from stock_analyzer.runtime.services.week5_candidate_state import CandidateStateStore
from stock_analyzer.runtime.services.week5_market_snapshot_service import (
    Week5MarketSnapshotService,
    enrich_auction_metrics,
)

# 快照时间戳允许的小幅"未来"偏差上限：now 在抓取前采样，行情源时间戳
# （抓取期间产生）晚于它属正常时延，再叠加少量时钟偏差。超过该幅度
# 的未来时间戳视为不可核实（时区误解释/时钟回拨），fail-closed。
_SNAPSHOT_FUTURE_TOLERANCE_SEC = 60.0


class RuntimeWeek5AutomationService:
    """Candidate/actionable decoupling and full-market automation workflows."""

    def __init__(self, service: Any) -> None:
        self._service = service
        config = service._config.week5
        state_path = str(
            getattr(config, "candidate_state_path", "artifacts/runtime/week5_candidate_state.json")
        ).strip()
        self._candidate_state = CandidateStateStore(service._resolve_evolution_path(state_path))
        self._market_snapshots = Week5MarketSnapshotService(service)
        self._market_radar_lock = Lock()
        self._market_radar_active = False
        self._market_radar_worker: Thread | None = None

    def candidate_state(self) -> dict[str, object]:
        return self._candidate_state.load()

    def _emit_actionable_notifications(
        self,
        report: Mapping[str, object],
        *,
        notify_enabled: bool,
        title_prefix: str,
    ) -> None:
        if not notify_enabled:
            return
        notifier = getattr(self._service, "_notify_actionable_signals", None)
        if callable(notifier):
            notifier(
                dict(report),
                trace_id=str(report.get("trace_id", report.get("snapshot_id", ""))),
                title_prefix=title_prefix,
            )

    def _automation_now(self, timestamp: datetime | None) -> datetime:
        app_config = getattr(self._service._config, "app", None)
        timezone_name = str(getattr(app_config, "timezone", "Asia/Shanghai")).strip()
        zone: tzinfo
        try:
            zone = ZoneInfo(timezone_name or "Asia/Shanghai")
        except Exception:
            zone = UTC
        if timestamp is None:
            # 行情源的“更新时间”是市场本地墙钟时间（如 09:25:00）。默认
            # 时钟必须落在市场时区：用 UTC 会让时间-only 快照时间被按错误
            # 时区解释，新鲜度探针把实际旧快照算成 0 秒年龄（假新鲜）。
            return datetime.now(zone)
        if timestamp.tzinfo is not None:
            # 显式传入的 tz-aware 时间统一换算到市场时区：绝对时刻不变，
            # 但下游墙钟语义（时间-only 快照时间解释、09:2x 竞价基线窗口、
            # 交易日切分）都以市场本地时间运行。原样返回 UTC 输入会把
            # "09:25:00" 解释成 09:25+00:00，新鲜度探针得出错误结论。
            return timestamp.astimezone(zone)
        return timestamp.replace(tzinfo=zone)

    def run_night_scan(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool = False,
        sync_watchlist: bool = True,
    ) -> dict[str, object]:
        service = self._service
        now = self._automation_now(timestamp)
        trace_id = f"week5-night-scan-{now.strftime('%Y%m%d%H%M%S')}"
        data_version_hint = self._resolve_current_data_version(now)
        existing = self._idempotent_night_scan(
            state=self.candidate_state(),
            trade_date=now.date().isoformat(),
            data_version=data_version_hint,
        )
        if existing is not None:
            return existing
        readiness = self._await_nightly_readiness()
        if not bool(readiness.get("allowed", False)):
            # Plan 验收口径：readiness 失败时回退使用前一晚候选池（带过期
            # 语义），而不是清空旧池导致次日盘中无池可用。
            return self._night_scan_fallback(
                now=now,
                trace_id=trace_id,
                reason="nightly_data_not_ready",
                allow_previous=True,
                readiness=readiness,
            )
        try:
            report = service.run_week5_scan(
                symbols=None,
                timestamp=now,
                # 夜间扫描只生成隔夜观察池，禁止进入旧 actionable 通知链。
                notify_enabled=False,
                sync_watchlist=False,
                sync_reason="night_scan_manual",
                force_universe_scan=True,
                prefilter_top_k_override=self._cfg_int("night_light_candidate_target", 100),
                universe_max_symbols_override=self._cfg_int("night_quality_target", 300),
                deep_candidate_target_override=self._cfg_int("night_deep_candidate_target", 50),
                scan_profile="night_scan",
            )
        except Exception as exc:
            return self._night_scan_fallback(
                now=now,
                trace_id=trace_id,
                reason=f"night_scan_failed:{exc.__class__.__name__}:{exc}",
            )

        rows = self._night_candidate_rows(report)
        rows = self._select_night_pool(rows)
        candidate_gate = self._candidate_gate_from_report(report, rows)
        candidate_blocked = str(candidate_gate.get("status", "")).strip() == "blocked"
        if candidate_blocked:
            rows = []
        actionable_gate = {
            "status": "blocked",
            "reasons": ["overnight_advisory_only", "realtime_gate_not_run"],
            "financial_trust_level": candidate_gate.get("financial_trust_level", "missing"),
            "actionable": False,
        }
        overnight_top5 = rows[: self._cfg_int("overnight_top_k", 5)]
        fallback: dict[str, object] = {
            "applied": False,
            "reason": "",
            "expires_at": "",
        }
        state = self._candidate_state.update(
            {
                "night_pool": rows,
                "overnight_top5": overnight_top5,
                "candidate_data_gate": candidate_gate,
                "actionable_data_gate": actionable_gate,
                "fallback": fallback,
                "night_pool_updated_at": now.isoformat() if rows else "",
                "night_pool_trade_date": now.date().isoformat() if rows else "",
                "latest_night_scan": {
                    "trace_id": trace_id,
                    "timestamp": now.isoformat(),
                    "status": str(report.get("status", "ok")),
                    "trade_date": now.date().isoformat(),
                    "data_version": str(
                        report.get("data_snapshot_id")
                        or report.get("data_version")
                        or data_version_hint
                        or "runtime"
                    ),
                    "readiness": readiness,
                    "source_report": report,
                },
            },
            updated_at=now,
            trade_date=now.date().isoformat(),
            data_version=str(
                report.get("data_snapshot_id") or report.get("data_version") or "runtime"
            ),
        )
        symbols = _symbols(rows)
        watchlist_sync: dict[str, object] = {
            "enabled": bool(sync_watchlist),
            "updated": False,
            "reason": "night_pool_empty",
            "symbols": list(service._state.watchlist),
        }
        if (
            sync_watchlist
            and symbols
            and not bool(fallback.get("applied", False))
            and str(candidate_gate.get("status", "")).strip() != "blocked"
        ):
            update = service._replace_watchlist(symbols=symbols, reason="week5_night_pool")
            watchlist_sync = {
                **update,
                "symbols": list(service._state.watchlist),
                "source": "night_pool",
            }
        result = {
            "ok": not candidate_blocked,
            "status": "blocked_data_gate" if candidate_blocked else "ok" if rows else "empty",
            "trace_id": trace_id,
            "timestamp": now.isoformat(),
            "signal_mode": "overnight_advisory",
            "actionable_signals": [],
            "actionable_count": 0,
            "night_pool": rows,
            "overnight_top5": overnight_top5,
            "candidate_data_gate": candidate_gate,
            "actionable_data_gate": actionable_gate,
            "fallback": fallback,
            "readiness": readiness,
            "watchlist_sync": watchlist_sync,
            "source_report": report,
            "state_revision": state.get("state_revision", 0),
            "notify_enabled": False,
        }
        if notify_enabled:
            result["notification"] = {
                "requested": True,
                "sent": False,
                "reason": "overnight_advisory_only",
            }
        return result

    def latest_night_scan(self) -> dict[str, object]:
        state = self.candidate_state()
        latest = _mapping(state.get("latest_night_scan"))
        if latest:
            return {
                **latest,
                "night_pool": _mapping_list(state.get("night_pool")),
                "overnight_top5": _mapping_list(state.get("overnight_top5")),
                "candidate_data_gate": _mapping(state.get("candidate_data_gate")),
                "actionable_data_gate": _mapping(state.get("actionable_data_gate")),
                "fallback": _mapping(state.get("fallback")),
                "state_revision": state.get("state_revision", 0),
            }
        return {
            "status": "no_report",
            "night_pool": state.get("night_pool", []),
            "overnight_top5": state.get("overnight_top5", []),
            "candidate_data_gate": state.get("candidate_data_gate", {}),
            "actionable_data_gate": state.get("actionable_data_gate", {}),
        }

    def run_auction(
        self,
        *,
        timestamp: datetime | None = None,
        snapshot_id: str = "",
        notify_enabled: bool = False,
    ) -> dict[str, object]:
        service = self._service
        now = self._automation_now(timestamp)
        snapshot = (
            self._market_snapshots.replay(snapshot_id)
            if snapshot_id.strip()
            else self._market_snapshots.capture(timestamp=now)
        )
        snapshot_rows = _mapping_list(snapshot.get("rows"))
        frame = _rows_to_frame(snapshot_rows)
        state = self.candidate_state()
        night_pool = _mapping_list(state.get("night_pool"))
        baseline_state = _mapping(state.get("auction_baseline"))
        auction = enrich_auction_metrics(
            frame,
            baseline=_baseline_values(baseline_state),
            now=now,
            max_age_sec=self._cfg_int("auction_snapshot_max_age_sec", 120),
            min_baseline_days=self._cfg_int("auction_baseline_min_days", 5),
        )
        if not bool(auction.get("auction_applied", False)):
            snapshot_age_sec = _snapshot_age_sec(snapshot_rows, now)
            report = {
                "ok": bool(snapshot.get("ok", False)),
                "status": str(auction.get("status", "unavailable")),
                "timestamp": now.isoformat(),
                "snapshot_id": str(snapshot.get("snapshot_id", snapshot_id)),
                "auction_applied": False,
                "opening_focus": [],
                "actionable_signals": [],
                "actionable_count": 0,
                "realtime_age_sec": snapshot_age_sec,
                "ratio_source": str(auction.get("ratio_source", "")),
                "overnight_top5": night_pool[: self._cfg_int("overnight_top_k", 5)],
                "notes": ["stale_or_unavailable_auction_only_overnight_advisory"],
            }
            baseline_patch: dict[str, object] = {}
            if snapshot_age_sec is not None and snapshot_age_sec <= self._cfg_int(
                "auction_snapshot_max_age_sec", 120
            ):
                baseline_patch["auction_baseline"] = _record_auction_baseline(
                    baseline_state, snapshot_rows, snapshot, now
                )
            self._candidate_state.update(
                {
                    "opening_focus": [],
                    "actionable_data_gate": {
                        "status": "blocked",
                        "reasons": [str(auction.get("reason", "auction_snapshot_stale"))],
                        "actionable": False,
                    },
                    "latest_auction": report,
                    **baseline_patch,
                },
                updated_at=now,
                trade_date=now.date().isoformat(),
            )
            return report

        rows = _mapping_list(auction.get("rows"))
        night_symbols = set(_symbols(night_pool))
        for row in rows:
            symbol = str(row.get("symbol", "")).strip()
            ratio = _as_float(row.get("auction_volume_ratio"))
            change = abs(_as_float(row.get("change_pct")))
            row["auction_score"] = round(
                ratio * 8.0 + change * 1.5 + (5.0 if symbol in night_symbols else 0.0),
                4,
            )
            row["source_bucket"] = "night_pool" if symbol in night_symbols else "market_auction"
        rows.sort(
            key=lambda item: (
                -_as_float(item.get("auction_score")),
                str(item.get("symbol", "")),
            )
        )
        market_top = rows[: self._cfg_int("auction_scan_top_n", 20)]
        merged_by_symbol = {
            str(item.get("symbol", "")).strip(): item
            for item in market_top
            if str(item.get("symbol", "")).strip()
        }
        for item in rows:
            symbol = str(item.get("symbol", "")).strip()
            if symbol in night_symbols and symbol:
                merged_by_symbol[symbol] = item
        rows = sorted(
            merged_by_symbol.values(),
            key=lambda item: (
                -_as_float(item.get("auction_score")),
                str(item.get("symbol", "")),
            ),
        )
        focus = rows[: self._cfg_int("auction_focus_max_symbols", 12)]
        focus_symbols = _symbols(focus)
        depth_snapshots: dict[str, dict[str, object]] = {}
        fetch_depth = getattr(service, "_fetch_market_depth_snapshots", None)
        if callable(fetch_depth) and focus_symbols:
            try:
                fetched_depth = fetch_depth(
                    symbols=focus_symbols,
                    scope="signal_pool",
                    force_refresh=True,
                )
                if isinstance(fetched_depth, Mapping):
                    depth_snapshots = {
                        str(symbol): _mapping(payload) for symbol, payload in fetched_depth.items()
                    }
            except Exception:
                depth_snapshots = {}
        for item in focus:
            symbol = str(item.get("symbol", "")).strip()
            item.update(_market_depth_fields(depth_snapshots.get(symbol, {})))
        pipeline_report: dict[str, object] = {}
        if focus_symbols:
            pipeline_report = service.run_pipeline(
                symbols=focus_symbols,
                strategy="trend",
                current_equity=service._state.current_equity,
                dry_run_execution=True,
                notify_enabled=notify_enabled,
                notify_in_dry_run=notify_enabled,
                job_name="week5_auction_signal_only",
            )
        pipeline_signals = _mapping_list(pipeline_report.get("signals"))
        actionable = _filter_actionable_rows(
            _mapping_list(pipeline_report.get("actionable_signals"))[:5],
            pipeline_signals,
        )
        financial_trust = _financial_trust_from_rows(pipeline_signals or actionable)
        realtime_age_sec = _snapshot_age_sec(snapshot_rows, now)
        gate_reasons: list[str] = []
        if financial_trust not in {"reported", "derived"}:
            gate_reasons.append("financial_trust_insufficient")
            actionable = []
        if realtime_age_sec is None or realtime_age_sec > self._cfg_int(
            "actionable_realtime_max_age_sec", 120
        ):
            gate_reasons.append("realtime_snapshot_stale")
            actionable = []
        if not actionable and not gate_reasons:
            gate_reasons.append("no_actionable_signal")
        actionable_gate = {
            "status": "ok" if actionable else "blocked" if gate_reasons else "watch_only",
            "reasons": gate_reasons,
            "financial_trust_level": financial_trust,
            "realtime_age_sec": realtime_age_sec,
            "actionable": bool(actionable),
        }
        report = {
            "ok": True,
            "status": "ok",
            "timestamp": now.isoformat(),
            "snapshot_id": str(snapshot.get("snapshot_id", "")),
            "auction_applied": True,
            "ratio_source": str(auction.get("ratio_source", "realtime_source")),
            "ratio_missing_excluded": _as_int(auction.get("missing_ratio_excluded", 0)),
            "opening_focus": focus,
            "opening_focus_target": self._cfg_int("auction_focus_target", 10),
            "depth_applied": bool(depth_snapshots),
            "depth_available_count": sum(
                1 for item in focus if bool(item.get("depth_available", False))
            ),
            "depth_missing_symbols": [
                str(item.get("symbol", "")).strip()
                for item in focus
                if not bool(item.get("depth_available", False))
            ],
            "actionable_signals": actionable,
            "actionable_count": len(actionable),
            "actionable_data_gate": actionable_gate,
            "pipeline": pipeline_report,
            "notify_enabled": bool(notify_enabled),
        }
        self._emit_actionable_notifications(
            report,
            notify_enabled=notify_enabled,
            title_prefix="week5 auction",
        )
        self._candidate_state.update(
            {
                "opening_focus": focus,
                "auction_baseline": _record_auction_baseline(
                    baseline_state, snapshot_rows, snapshot, now
                ),
                "actionable_data_gate": actionable_gate,
                "latest_auction": report,
            },
            updated_at=now,
            trade_date=now.date().isoformat(),
            data_version=str(snapshot.get("snapshot_id", "")),
        )
        return report

    def latest_auction(self) -> dict[str, object]:
        return _mapping(self.candidate_state().get("latest_auction"))

    def run_market_radar(
        self,
        *,
        timestamp: datetime | None = None,
        snapshot_id: str = "",
        notify_enabled: bool = False,
    ) -> dict[str, object]:
        now = self._automation_now(timestamp)
        with self._market_radar_lock:
            if self._market_radar_active:
                return self._market_radar_busy_report(now)
            self._market_radar_active = True

        timeout_sec = self._cfg_float("market_radar_timeout_sec", 90.0)
        if timeout_sec <= 0:
            try:
                return self._run_market_radar_sync(
                    timestamp=now,
                    snapshot_id=snapshot_id,
                    notify_enabled=notify_enabled,
                    cancel_event=None,
                )
            finally:
                with self._market_radar_lock:
                    self._market_radar_active = False

        started = monotonic()
        cancel_event = Event()
        result_queue: Queue[dict[str, object]] = Queue(maxsize=1)

        def worker() -> None:
            try:
                result = self._run_market_radar_sync(
                    timestamp=now,
                    snapshot_id=snapshot_id,
                    notify_enabled=notify_enabled,
                    cancel_event=cancel_event,
                )
                if not cancel_event.is_set():
                    result_queue.put(result)
            except Exception as exc:
                if not cancel_event.is_set():
                    result_queue.put(
                        {
                            "ok": False,
                            "status": "failed",
                            "timestamp": now.isoformat(),
                            "snapshot_id": snapshot_id.strip(),
                            "actionable_signals": [],
                            "actionable_count": 0,
                            "error": f"radar_worker:{exc.__class__.__name__}:{exc}",
                        }
                    )
            finally:
                with self._market_radar_lock:
                    self._market_radar_active = False

        thread = Thread(target=worker, name="week5-market-radar", daemon=True)
        with self._market_radar_lock:
            self._market_radar_worker = thread
        thread.start()
        thread.join(timeout=timeout_sec)
        if not thread.is_alive():
            try:
                return result_queue.get_nowait()
            except Empty:
                return {
                    "ok": False,
                    "status": "failed",
                    "timestamp": now.isoformat(),
                    "snapshot_id": snapshot_id.strip(),
                    "actionable_signals": [],
                    "actionable_count": 0,
                    "error": "market_radar_empty_worker_result",
                }

        cancel_event.set()
        state = self.candidate_state()
        trade_date = now.date().isoformat()
        existing_dynamic_pool = (
            _mapping_list(state.get("dynamic_candidate_pool"))
            if str(state.get("dynamic_candidate_pool_trade_date", "")).strip() == trade_date
            else []
        )
        timeout_report = {
            "ok": False,
            "status": "timeout",
            "timestamp": now.isoformat(),
            "snapshot_id": snapshot_id.strip(),
            "hits": [],
            "hit_count": 0,
            "promoted": [],
            "promoted_count": 0,
            "dynamic_candidate_pool": existing_dynamic_pool,
            "actionable_signals": [],
            "actionable_count": 0,
            "actionable_data_gate": {
                "status": "blocked",
                "reasons": ["market_radar_timeout"],
                "actionable": False,
            },
            "notify_enabled": False,
            "notes": ["market_radar_timeout_cancelled_worker"],
        }
        return self._commit_market_radar(
            timeout_report,
            now=now,
            started=started,
            previous_health=_mapping(state.get("market_radar_health")),
            state_patch={
                "dynamic_candidate_pool": existing_dynamic_pool,
                "dynamic_candidate_pool_trade_date": trade_date,
            },
            notify_enabled=False,
            data_version=snapshot_id.strip(),
        )

    def _run_market_radar_sync(
        self,
        *,
        timestamp: datetime | None = None,
        snapshot_id: str = "",
        notify_enabled: bool = False,
        cancel_event: Event | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = self._automation_now(timestamp)
        state = self.candidate_state()
        trade_date = now.date().isoformat()
        old_continuity = _mapping(state.get("radar_continuity"))
        existing_dynamic_pool = (
            _mapping_list(state.get("dynamic_candidate_pool"))
            if str(state.get("dynamic_candidate_pool_trade_date", "")).strip() == trade_date
            else []
        )
        if not bool(getattr(service._config.week5, "market_radar_full_market_enabled", True)):
            report = {
                "ok": True,
                "status": "disabled",
                "timestamp": now.isoformat(),
                "snapshot_id": "",
                "hits": [],
                "hit_count": 0,
                "promoted": [],
                "promoted_count": 0,
                "dynamic_candidate_pool": existing_dynamic_pool,
                "actionable_signals": [],
                "actionable_count": 0,
                "actionable_data_gate": {
                    "status": "blocked",
                    "reasons": ["market_radar_full_market_disabled"],
                    "actionable": False,
                },
                "notify_enabled": False,
                "notes": ["market_radar_full_market_disabled"],
            }
            committed = self._candidate_state.update(
                {
                    "dynamic_candidate_pool": existing_dynamic_pool,
                    "dynamic_candidate_pool_trade_date": trade_date,
                    "latest_market_radar": report,
                },
                updated_at=now,
                trade_date=trade_date,
            )
            report["state_revision"] = committed.get("state_revision", 0)
            return report

        health = _mapping(state.get("market_radar_health"))
        breaker = self._market_radar_breaker_gate(health, now)
        if bool(breaker.get("open", False)):
            report = {
                "ok": False,
                "status": "circuit_open",
                "timestamp": now.isoformat(),
                "snapshot_id": "",
                "hits": [],
                "hit_count": 0,
                "promoted": [],
                "promoted_count": 0,
                "dynamic_candidate_pool": existing_dynamic_pool,
                "actionable_signals": [],
                "actionable_count": 0,
                "actionable_data_gate": {
                    "status": "blocked",
                    "reasons": ["market_radar_circuit_open"],
                    "actionable": False,
                },
                "notify_enabled": bool(notify_enabled),
                "notes": ["market_radar_circuit_breaker"],
            }
            return self._commit_market_radar_unless_cancelled(
                report,
                cancel_event=cancel_event,
                now=now,
                started=None,
                previous_health=health,
                state_patch={
                    "dynamic_candidate_pool": existing_dynamic_pool,
                    "dynamic_candidate_pool_trade_date": trade_date,
                },
                notify_enabled=False,
                data_version="",
            )

        started = monotonic()
        try:
            snapshot = (
                self._market_snapshots.replay(snapshot_id)
                if snapshot_id.strip()
                else self._market_snapshots.capture(timestamp=now)
            )
        except Exception as exc:
            report = {
                "ok": False,
                "status": "failed",
                "timestamp": now.isoformat(),
                "snapshot_id": snapshot_id.strip(),
                "hits": [],
                "promoted": [],
                "actionable_signals": [],
                "actionable_count": 0,
                "error": f"snapshot:{exc.__class__.__name__}:{exc}",
            }
            return self._commit_market_radar_unless_cancelled(
                report,
                cancel_event=cancel_event,
                now=now,
                started=started,
                previous_health=health,
                state_patch={
                    "dynamic_candidate_pool": existing_dynamic_pool,
                    "dynamic_candidate_pool_trade_date": trade_date,
                },
                notify_enabled=False,
                data_version=snapshot_id.strip(),
            )

        rows = _mapping_list(snapshot.get("rows"))
        if not rows:
            unavailable_report: dict[str, object] = {
                "ok": False,
                "status": "unavailable",
                "timestamp": now.isoformat(),
                "snapshot_id": str(snapshot.get("snapshot_id", "")),
                "hits": [],
                "promoted": [],
                "actionable_signals": [],
                "actionable_count": 0,
                "error": str(snapshot.get("errors", ["empty_market_snapshot"])),
            }
            return self._commit_market_radar_unless_cancelled(
                unavailable_report,
                cancel_event=cancel_event,
                now=now,
                started=started,
                previous_health=health,
                state_patch={
                    "dynamic_candidate_pool": [],
                    "dynamic_candidate_pool_trade_date": trade_date,
                },
                notify_enabled=False,
                data_version=str(snapshot.get("snapshot_id", "")),
            )

        radar_age_sec = _snapshot_age_sec(rows, now)
        if radar_age_sec is None or radar_age_sec > self._cfg_int(
            "actionable_realtime_max_age_sec", 120
        ):
            stale_report: dict[str, object] = {
                "ok": False,
                "status": "stale",
                "timestamp": now.isoformat(),
                "snapshot_id": str(snapshot.get("snapshot_id", "")),
                "hits": [],
                "hit_count": 0,
                "promoted": [],
                "promoted_count": 0,
                "dynamic_candidate_pool": existing_dynamic_pool,
                "actionable_signals": [],
                "actionable_count": 0,
                "actionable_data_gate": {
                    "status": "blocked",
                    "reasons": ["realtime_snapshot_stale"],
                    "realtime_age_sec": radar_age_sec,
                    "actionable": False,
                },
                "notify_enabled": bool(notify_enabled),
                "notes": ["stale_snapshot_no_radar_promotion"],
            }
            return self._commit_market_radar_unless_cancelled(
                stale_report,
                cancel_event=cancel_event,
                now=now,
                started=started,
                previous_health=health,
                state_patch={
                    "dynamic_candidate_pool": existing_dynamic_pool,
                    "dynamic_candidate_pool_trade_date": trade_date,
                },
                notify_enabled=False,
                data_version=str(snapshot.get("snapshot_id", "")),
            )

        continuity: dict[str, dict[str, object]] = {}
        hits: list[dict[str, object]] = []
        seen_symbols: set[str] = set()
        for row in rows:
            symbol = str(row.get("symbol", "")).strip()
            if not symbol:
                continue
            seen_symbols.add(symbol)
            change = abs(_as_float(row.get("change_pct")))
            ratio = _as_float(row.get("auction_volume_ratio"))
            is_hit = change >= self._cfg_float(
                "anomaly_gap_pct", 0.08
            ) * 100.0 or ratio >= self._cfg_float("anomaly_volume_ratio", 2.5)
            previous = _mapping(old_continuity.get(symbol))
            count = _as_int(previous.get("count")) + 1 if is_hit else 0
            record = {
                "count": count,
                "hit": is_hit,
                "last_hit_at": now.isoformat() if is_hit else str(previous.get("last_hit_at", "")),
                "last_seen_at": now.isoformat(),
                "score": round(change * 10.0 + ratio * 5.0, 4),
            }
            continuity[symbol] = record
            if is_hit:
                hits.append(
                    {
                        **row,
                        "continuity_count": count,
                        "radar_score": record["score"],
                    }
                )
        for symbol, previous_state in old_continuity.items():
            if str(symbol).strip() and str(symbol) not in seen_symbols:
                continuity[str(symbol)] = {
                    **_mapping(previous_state),
                    "count": 0,
                    "hit": False,
                }
        required = max(2, self._cfg_int("market_radar_continuity_required", 2))
        promoted = [item for item in hits if _as_int(item.get("continuity_count")) >= required]
        promoted.sort(
            key=lambda item: (
                -_as_float(item.get("radar_score")),
                str(item.get("symbol", "")),
            )
        )
        promoted = promoted[: self._cfg_int("market_radar_light_top_n", 20)]
        promoted_symbols = _symbols(promoted)
        pipeline_report: dict[str, object] = {}
        if promoted_symbols:
            try:
                pipeline_report = service.run_pipeline(
                    symbols=promoted_symbols[: self._cfg_int("market_radar_light_top_n", 20)],
                    strategy="trend",
                    current_equity=service._state.current_equity,
                    dry_run_execution=True,
                    # Notifications are emitted only after the bounded worker
                    # returns and the cancellation gate has passed.
                    notify_enabled=False,
                    notify_in_dry_run=False,
                    job_name="week5_market_radar_signal_only",
                )
            except Exception as exc:
                pipeline_report = {
                    "ok": False,
                    "status": "failed",
                    "error": f"pipeline:{exc.__class__.__name__}:{exc}",
                    "signals": [],
                    "actionable_signals": [],
                }
        radar_signals = _mapping_list(pipeline_report.get("signals"))
        validated_symbols = _pipeline_validated_symbols(pipeline_report)
        validated_promoted = [
            item for item in promoted if str(item.get("symbol", "")).strip() in validated_symbols
        ]
        dynamic_pool = self._arbitrate_dynamic_pool(
            existing=existing_dynamic_pool,
            promoted=validated_promoted,
            now=now,
        )
        actionable = _filter_actionable_rows(
            _mapping_list(pipeline_report.get("actionable_signals"))[:5],
            radar_signals,
        )

        radar_financial_trust = _financial_trust_from_rows(radar_signals or actionable)
        radar_gate_reasons: list[str] = []
        if radar_financial_trust not in {"reported", "derived"}:
            radar_gate_reasons.append("financial_trust_insufficient")
            actionable = []
        report = {
            "ok": pipeline_report.get("ok", True) is not False,
            "status": "ok" if pipeline_report.get("ok", True) is not False else "failed",
            "timestamp": now.isoformat(),
            "snapshot_id": str(snapshot.get("snapshot_id", "")),
            "hits": hits[: self._cfg_int("market_radar_light_top_n", 20)],
            "hit_count": len(hits),
            "promoted": promoted,
            "promoted_count": len(promoted),
            "dynamic_candidate_pool": dynamic_pool,
            "continuity_required": required,
            "actionable_signals": actionable,
            "actionable_count": len(actionable),
            "actionable_data_gate": {
                "status": (
                    "ok" if actionable else "blocked" if radar_gate_reasons else "watch_only"
                ),
                "reasons": (
                    radar_gate_reasons or (["no_actionable_signal"] if not actionable else [])
                ),
                "financial_trust_level": radar_financial_trust,
                "realtime_age_sec": radar_age_sec,
                "actionable": bool(actionable),
            },
            "pipeline": pipeline_report,
            "notify_enabled": bool(notify_enabled),
            "notes": [
                "single_hit_is_observation_only",
                "signal_only",
            ],
        }
        return self._commit_market_radar_unless_cancelled(
            report,
            cancel_event=cancel_event,
            now=now,
            started=started,
            previous_health=health,
            state_patch={
                "radar_continuity": continuity,
                "dynamic_candidate_pool": dynamic_pool,
                "dynamic_candidate_pool_trade_date": trade_date,
            },
            notify_enabled=notify_enabled,
            data_version=str(snapshot.get("snapshot_id", "")),
        )

    def _market_radar_busy_report(self, now: datetime) -> dict[str, object]:
        state = self.candidate_state()
        trade_date = now.date().isoformat()
        dynamic_pool = (
            _mapping_list(state.get("dynamic_candidate_pool"))
            if str(state.get("dynamic_candidate_pool_trade_date", "")).strip() == trade_date
            else []
        )
        return {
            "ok": False,
            "status": "busy",
            "timestamp": now.isoformat(),
            "snapshot_id": "",
            "hits": [],
            "hit_count": 0,
            "promoted": [],
            "promoted_count": 0,
            "dynamic_candidate_pool": dynamic_pool,
            "actionable_signals": [],
            "actionable_count": 0,
            "actionable_data_gate": {
                "status": "blocked",
                "reasons": ["market_radar_previous_run_in_progress"],
                "actionable": False,
            },
            "notify_enabled": False,
        }

    def _market_radar_cancelled_report(self, report: Mapping[str, object]) -> dict[str, object]:
        cancelled = dict(report)
        cancelled.update(
            {
                "ok": False,
                "status": "cancelled",
                "actionable_signals": [],
                "actionable_count": 0,
                "notify_enabled": False,
                "actionable_data_gate": {
                    "status": "blocked",
                    "reasons": ["market_radar_cancelled"],
                    "actionable": False,
                },
            }
        )
        return cancelled

    def _commit_market_radar_unless_cancelled(
        self,
        report: Mapping[str, object],
        *,
        cancel_event: Event | None,
        now: datetime,
        started: float | None,
        previous_health: Mapping[str, object],
        state_patch: Mapping[str, object],
        notify_enabled: bool,
        data_version: str,
    ) -> dict[str, object]:
        if cancel_event is not None and cancel_event.is_set():
            return self._market_radar_cancelled_report(report)
        return self._commit_market_radar(
            report,
            now=now,
            started=started,
            previous_health=previous_health,
            state_patch=state_patch,
            notify_enabled=notify_enabled,
            data_version=data_version,
            cancel_event=cancel_event,
        )

    def _commit_market_radar(
        self,
        report: Mapping[str, object],
        *,
        now: datetime,
        started: float | None,
        previous_health: Mapping[str, object],
        state_patch: Mapping[str, object],
        notify_enabled: bool,
        data_version: str,
        cancel_event: Event | None = None,
    ) -> dict[str, object]:
        if cancel_event is not None and cancel_event.is_set():
            return self._market_radar_cancelled_report(report)
        elapsed_sec = round(max(0.0, monotonic() - started), 3) if started is not None else 0.0
        finalized = dict(report)
        timeout_sec = self._cfg_float("market_radar_timeout_sec", 90.0)
        if (
            started is not None
            and timeout_sec > 0
            and elapsed_sec >= float(timeout_sec)
            and str(finalized.get("status", "")).strip() != "circuit_open"
        ):
            finalized["ok"] = False
            finalized["status"] = "timeout"
            finalized["actionable_signals"] = []
            finalized["actionable_count"] = 0
            gate = _mapping(finalized.get("actionable_data_gate"))
            reasons = _string_list(gate.get("reasons"))
            if "market_radar_timeout" not in reasons:
                reasons.append("market_radar_timeout")
            gate.update({"status": "blocked", "reasons": reasons, "actionable": False})
            finalized["actionable_data_gate"] = gate
        health = self._update_market_radar_health(
            previous_health,
            finalized,
            now=now,
            elapsed_sec=elapsed_sec,
        )
        finalized["elapsed_sec"] = elapsed_sec
        finalized["market_radar_health"] = health
        if cancel_event is not None and cancel_event.is_set():
            return self._market_radar_cancelled_report(finalized)
        self._emit_actionable_notifications(
            finalized,
            notify_enabled=notify_enabled and str(finalized.get("status", "")).strip() == "ok",
            title_prefix="week5 market radar",
        )
        if cancel_event is not None and cancel_event.is_set():
            return self._market_radar_cancelled_report(finalized)
        committed = self._candidate_state.update(
            {
                **dict(state_patch),
                "latest_market_radar": finalized,
                "market_radar_health": health,
            },
            updated_at=now,
            trade_date=now.date().isoformat(),
            data_version=data_version,
        )
        finalized["state_revision"] = committed.get("state_revision", 0)
        return finalized

    def _market_radar_breaker_gate(
        self,
        health: Mapping[str, object],
        now: datetime,
    ) -> dict[str, bool]:
        if str(health.get("status", "closed")).strip().lower() != "open":
            return {"open": False, "probe": False}
        open_until = _parse_datetime(health.get("open_until"))
        if open_until is not None and now < open_until:
            return {"open": True, "probe": False}
        return {"open": False, "probe": True}

    def _update_market_radar_health(
        self,
        previous: Mapping[str, object],
        report: Mapping[str, object],
        *,
        now: datetime,
        elapsed_sec: float,
    ) -> dict[str, object]:
        previous_status = str(previous.get("status", "closed")).strip().lower() or "closed"
        report_status = str(report.get("status", "failed")).strip().lower()
        if report_status == "circuit_open":
            return {
                **dict(previous),
                "status": "open",
                "last_run_at": now.isoformat(),
                "last_status": report_status,
                "last_elapsed_sec": elapsed_sec,
                "last_transition": "",
            }

        pipeline = _mapping(report.get("pipeline"))
        failed = (
            report.get("ok") is False
            or report_status in {"failed", "unavailable", "stale", "timeout", "error"}
            or pipeline.get("ok") is False
        )
        slow_threshold = self._cfg_int("market_radar_slow_run_sec", 120)
        slow = slow_threshold > 0 and elapsed_sec >= float(slow_threshold)
        failures = _as_int(previous.get("consecutive_failures")) + 1 if failed else 0
        slow_runs = _as_int(previous.get("consecutive_slow_runs")) + 1 if slow else 0
        failure_limit = self._cfg_int("market_radar_circuit_breaker_failures", 3)
        slow_limit = self._cfg_int("market_radar_circuit_breaker_slow_runs", 2)
        should_open = (failure_limit > 0 and failures >= failure_limit) or (
            slow_limit > 0 and slow_runs >= slow_limit
        )
        cooldown_min = self._cfg_int("market_radar_circuit_breaker_cooldown_min", 10)
        transition = ""
        status = "closed"
        opened_at = str(previous.get("opened_at", ""))
        open_until = str(previous.get("open_until", ""))
        if should_open:
            status = "open"
            transition = "reopened" if previous_status == "open" else "opened"
            opened_at = now.isoformat()
            open_until = (now + pd.Timedelta(minutes=cooldown_min).to_pytimedelta()).isoformat()
        elif previous_status == "open":
            transition = "closed"
            failures = 0
            slow_runs = 0
            opened_at = ""
            open_until = ""

        return {
            "status": status,
            "consecutive_failures": failures,
            "consecutive_slow_runs": slow_runs,
            "opened_at": opened_at,
            "open_until": open_until,
            "last_run_at": now.isoformat(),
            "last_status": report_status,
            "last_elapsed_sec": elapsed_sec,
            "last_failure": str(report.get("error", "")),
            "last_transition": transition,
        }

    def latest_market_radar(self) -> dict[str, object]:
        return _mapping(self.candidate_state().get("latest_market_radar"))

    def run_live_runtime(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool = False,
    ) -> dict[str, object]:
        service = self._service
        now = self._automation_now(timestamp)
        state = self.candidate_state()
        pool_rows = self._live_runtime_pool(state, now)
        candidates = [
            str(row.get("symbol", "")).strip()
            for row in pool_rows
            if str(row.get("symbol", "")).strip()
        ]
        pipeline_report: dict[str, object] = {}
        snapshot: dict[str, object] = {"status": "not_requested"}
        if candidates:
            snapshot = self._market_snapshots.capture(timestamp=now)
            pipeline_report = service.run_pipeline(
                symbols=candidates,
                strategy="trend",
                current_equity=service._state.current_equity,
                use_live_runtime=True,
                dry_run_execution=True,
                notify_enabled=notify_enabled,
                notify_in_dry_run=notify_enabled,
                job_name="week5_live_runtime_signal_only",
            )
        actionable = _filter_actionable_rows(
            _mapping_list(pipeline_report.get("actionable_signals"))[:5],
            _mapping_list(pipeline_report.get("signals")),
        )
        live_signals = _mapping_list(pipeline_report.get("signals"))
        live_financial_trust = _financial_trust_from_rows(live_signals or actionable)
        snapshot_rows = _mapping_list(snapshot.get("rows")) if candidates else []
        live_age_sec = _snapshot_age_sec(snapshot_rows, now, symbols=set(candidates))
        live_gate_reasons: list[str] = []
        if live_financial_trust not in {"reported", "derived"}:
            live_gate_reasons.append("financial_trust_insufficient")
            actionable = []
        if live_age_sec is None or live_age_sec > self._cfg_int(
            "actionable_realtime_max_age_sec", 120
        ):
            live_gate_reasons.append("realtime_snapshot_stale")
            actionable = []
        report: dict[str, object] = {
            "ok": True,
            "status": "ok" if candidates else "empty",
            "timestamp": now.isoformat(),
            "symbols": candidates,
            "symbol_count": len(candidates),
            "live_runtime_cap": self._cfg_int("live_runtime_max_symbols", 8),
            "snapshot_id": str(snapshot.get("snapshot_id", "")) if candidates else "",
            "snapshot_status": (
                str(snapshot.get("status", "unavailable")) if candidates else "not_requested"
            ),
            "actionable_signals": actionable,
            "actionable_count": len(actionable),
            "actionable_data_gate": {
                "status": (
                    "ok" if actionable else "blocked" if live_gate_reasons else "watch_only"
                ),
                "reasons": (
                    live_gate_reasons or (["no_actionable_signal"] if not actionable else [])
                ),
                "financial_trust_level": live_financial_trust,
                "realtime_age_sec": live_age_sec,
                "actionable": bool(actionable),
            },
            "pipeline": pipeline_report,
            "notify_enabled": bool(notify_enabled),
            "signal_only": True,
        }
        self._emit_actionable_notifications(
            report,
            notify_enabled=notify_enabled,
            title_prefix="week5 live runtime",
        )
        self._candidate_state.update(
            {
                # 持久化完整仲裁行（含 entered_at）：每轮重写 entered_at 会让
                # 驻留计时永远归零、保护条件永远无法满足。行级持久化保证
                # 现任成员的驻留跨轮连续。
                "live_runtime": pool_rows,
                "live_runtime_trade_date": now.date().isoformat(),
                "latest_live_runtime": report,
            },
            updated_at=now,
            trade_date=now.date().isoformat(),
        )
        return report

    def latest_live_runtime(self) -> dict[str, object]:
        return _mapping(self.candidate_state().get("latest_live_runtime"))

    def run_weekend_learning(self, *, timestamp: datetime | None = None) -> dict[str, object]:
        service = self._service
        now = self._automation_now(timestamp)
        if not bool(getattr(service._config.week5, "weekend_learning_enabled", True)):
            result = {
                "ok": True,
                "status": "skipped",
                "reason": "weekend_learning_disabled",
                "timestamp": now.isoformat(),
            }
            self._candidate_state.update(
                {"latest_weekend_learning": result},
                updated_at=now,
                trade_date=now.date().isoformat(),
            )
            return result

        learning_status = service.learning_protocol_status(manifest_limit=5)
        governed_runner = getattr(service, "_idle_execute_task_with_policy", None)
        if callable(governed_runner):
            context_builder = getattr(service, "_build_idle_context", None)
            if callable(context_builder):
                context = dict(context_builder(now))
            else:
                context = {
                    "window": "weekend",
                    "trade_date": now.strftime("%Y%m%d"),
                    "now": now.isoformat(),
                }
            context["now"] = now.isoformat()
            context["source_trace_id"] = f"week5-weekend-learning-{now.strftime('%Y%m%d%H%M%S')}"
            try:
                task_payload = governed_runner("WE-LEARN-01", context)
            except Exception as exc:
                task_payload = {
                    "status": "error",
                    "reason": f"we_learn_01_failed:{exc.__class__.__name__}:{exc}",
                }
            task_status = str(task_payload.get("status", "error")).strip().lower()
            task_reason = str(task_payload.get("reason", "")).strip()
            result = {
                "ok": task_status not in {"error", "timeout"},
                "status": task_status or "error",
                "reason": task_reason,
                "task_id": "WE-LEARN-01",
                "timestamp": now.isoformat(),
                "learning_status": learning_status,
                "task": task_payload,
                "promotion": {
                    "auto_release": False,
                    "champion_changed": False,
                },
                "governed_by": "WE-LEARN-01",
            }
            self._candidate_state.update(
                {"latest_weekend_learning": result},
                updated_at=now,
                trade_date=now.date().isoformat(),
            )
            return result

        # Compatibility fallback for lightweight callers that do not expose the
        # idle-queue orchestration wrapper used by the production service.
        manifest = service.build_learning_trainable_manifest()
        if (
            not bool(manifest.get("ok", False))
            or not str(manifest.get("dataset_manifest_id", "")).strip()
        ):
            result = {
                "ok": True,
                "status": "skipped",
                "reason": "samples_not_mature",
                "learning_status": learning_status,
                "manifest": manifest,
                "timestamp": now.isoformat(),
            }
            self._candidate_state.update(
                {"latest_weekend_learning": result},
                updated_at=now,
                trade_date=now.date().isoformat(),
            )
            return result
        payload = service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            load_predictor=False,
            mark_shadow_validated=True,
            auto_approve=False,
            auto_release=False,
            source_trace_id=f"week5-weekend-learning-{now.strftime('%Y%m%d%H%M%S')}",
        )
        result = {
            "ok": bool(payload.get("ok", False)),
            "status": "completed" if bool(payload.get("ok", False)) else "shadow_failed",
            "timestamp": now.isoformat(),
            "learning_status": learning_status,
            "manifest": manifest,
            "shadow": payload,
            "promotion": {
                "auto_release": False,
                "champion_changed": False,
            },
        }
        self._candidate_state.update(
            {"latest_weekend_learning": result},
            updated_at=now,
            trade_date=now.date().isoformat(),
        )
        return result

    def _night_candidate_rows(self, report: Mapping[str, object]) -> list[dict[str, object]]:
        signal_pool = _mapping(report.get("signal_pool"))
        rows = _mapping_list(signal_pool.get("candidates"))
        if not rows:
            funnel = _mapping(report.get("funnel"))
            final_selection = _mapping(funnel.get("final_selection"))
            rows = _mapping_list(final_selection.get("final_signals"))
        normalized: list[dict[str, object]] = []
        for rank, row in enumerate(rows, start=1):
            symbol = str(row.get("symbol", "")).strip()
            score = _as_float(
                row.get("execution_reranked_score", row.get("shortlist_score", row.get("score")))
            )
            if not symbol or score < self._cfg_float("auto_sync_watchlist_min_score", 65.0):
                continue
            normalized.append(
                {
                    **row,
                    "symbol": symbol,
                    "score": round(score, 4),
                    "rank": rank,
                    "candidate_type": "overnight_advisory",
                    "actionable": False,
                    "financial_trust_level": _financial_trust_level(row) or "missing",
                }
            )
        normalized.sort(
            key=lambda item: (
                -_as_float(item.get("score")),
                str(item.get("symbol", "")),
            )
        )
        return normalized

    def _select_night_pool(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        if not rows:
            return []
        max_size = max(1, self._cfg_int("candidate_pool_max_symbols", 50))
        target = min(max_size, max(1, self._cfg_int("candidate_pool_target", 30)))
        selected = rows[:target]
        boundary_score = _as_float(selected[-1].get("score")) if selected else 0.0
        for item in rows[target:max_size]:
            if boundary_score - _as_float(item.get("score")) <= 3.0:
                selected.append(item)
        return selected[:max_size]

    def _candidate_gate_from_report(
        self,
        report: Mapping[str, object],
        rows: list[dict[str, object]],
    ) -> dict[str, object]:
        raw_gate = _mapping(report.get("data_gate"))
        reasons = _string_list(raw_gate.get("reasons"))
        blocking = [
            reason
            for reason in reasons
            if reason.startswith(("provider_hard_degraded", "data_stale", "feature_snapshot"))
        ]
        prefilter = _mapping(report.get("prefilter"))
        selection = _mapping(prefilter.get("universe_quality_selection"))
        eligible = _as_int(
            prefilter.get("eligible_count"),
            _as_int(
                selection.get("selected_count"),
                _as_int(prefilter.get("universe_count"), len(rows)),
            ),
        )
        explicit_ratio = prefilter.get("batch_coverage_ratio", prefilter.get("coverage_ratio"))
        if explicit_ratio is None:
            universe = _optional_int(
                prefilter.get("universe_count"),
                _optional_int(selection.get("universe_count"), None),
            )
            ratio = eligible / universe if universe is not None and universe > 0 else 0.0
        else:
            ratio = _as_float(explicit_ratio, default=0.0)
        ratio = min(1.0, max(0.0, ratio))
        covered = round(eligible * ratio) if eligible else len(rows)
        if ratio < 0.80:
            blocking.append("eligible_universe_coverage_below_80pct")
        elif ratio < 0.90:
            reasons.append("eligible_universe_coverage_degraded")
        freshness = _mapping(prefilter.get("intraday_freshness"))
        freshness_ratio = _optional_float(freshness.get("fresh_ratio"))
        if freshness_ratio is None:
            # 零容忍口径：freshness 报告缺失或异常（无 fresh_ratio，如周末
            # 空报告/构建失败）时不得放行，防止静默跳过新鲜度门禁。
            blocking.append("intraday_freshness_missing")
        elif freshness_ratio < 0.80:
            blocking.append("intraday_freshness_below_80pct")
        elif freshness_ratio < 0.90:
            reasons.append("intraday_freshness_degraded")
        financial = _financial_trust_from_rows(rows)
        watch_reasons = [reason for reason in reasons if reason not in blocking]
        status = (
            "blocked"
            if blocking
            else "watch_only"
            if watch_reasons or ratio < 0.90 or financial in {"heuristic", "missing"}
            else "ok"
        )
        return {
            "status": status,
            "reasons": blocking + watch_reasons,
            "eligible_universe_count": eligible,
            "covered_count": covered,
            "coverage_ratio": round(ratio, 4),
            "financial_trust_level": financial,
            "financial_missing_allowed": True,
            "watch_only": status == "watch_only",
            "actionable": False,
        }

    def _night_scan_fallback(
        self,
        *,
        now: datetime,
        trace_id: str,
        reason: str,
        allow_previous: bool = True,
        readiness: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        previous = self.candidate_state()
        pool, expires_at = self._fresh_previous_pool(previous, now) if allow_previous else ([], "")
        fallback = {
            "applied": bool(pool),
            "reason": reason,
            "expires_at": expires_at,
        }
        report: dict[str, object] = {
            "ok": False,
            "status": "fallback" if pool else "failed",
            "trace_id": trace_id,
            "timestamp": now.isoformat(),
            "night_pool": pool,
            "overnight_top5": pool[: self._cfg_int("overnight_top_k", 5)],
            "candidate_data_gate": {
                "status": "blocked",
                "reasons": [reason],
                "fallback": bool(pool),
            },
            "actionable_data_gate": {
                "status": "blocked",
                "reasons": ["fallback_not_actionable"],
            },
            "fallback": fallback,
            "readiness": dict(readiness or {}),
            "actionable_signals": [],
            "actionable_count": 0,
        }
        self._candidate_state.update(
            {
                "night_pool": pool,
                "overnight_top5": pool[: self._cfg_int("overnight_top_k", 5)],
                "night_pool_updated_at": (
                    str(previous.get("night_pool_updated_at", "")).strip() if pool else ""
                ),
                "night_pool_trade_date": (
                    str(previous.get("night_pool_trade_date", "")).strip() if pool else ""
                ),
                "candidate_data_gate": report["candidate_data_gate"],
                "actionable_data_gate": report["actionable_data_gate"],
                "fallback": fallback,
                "latest_night_scan": report,
                # 回退不是完整夜扫：显式清空 data_version，避免同晚 readiness
                # 恢复后重跑被 _idempotent_night_scan 用旧版本号误判为
                # already_ran 而跳过真正的扫描。
                "data_version": "",
            },
            updated_at=now,
            trade_date=now.date().isoformat(),
        )
        return report

    def _fresh_previous_pool(
        self, state: Mapping[str, object], now: datetime
    ) -> tuple[list[dict[str, object]], str]:
        pool = _mapping_list(state.get("night_pool"))
        updated = _parse_datetime(state.get("night_pool_updated_at"))
        if not pool or updated is None:
            return [], ""
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if updated > current:
            # 时钟回拨/未来时间戳保护：不消费"来自未来"的池。
            return [], ""
        # 跨交易日口径：夜池在 D 晚生成、D+1 盘中消费是主路径，因此不再
        # 要求 trade_date 等于当天；新鲜度完全由 night_pool_updated_at +
        # night_pool_max_age_hours（默认 30h，覆盖次日全天）决定。
        candidate_gate = _mapping(state.get("candidate_data_gate"))
        if str(candidate_gate.get("status", "")).strip().lower() == "blocked":
            return [], ""
        max_age_hours = float(
            getattr(
                self._service._config.week5,
                "night_pool_max_age_hours",
                30.0,
            )
        )
        expires = updated + pd.Timedelta(hours=max(0.0, max_age_hours)).to_pytimedelta()
        fallback = _mapping(state.get("fallback"))
        if bool(fallback.get("applied", False)):
            fallback_expires = _parse_datetime(fallback.get("expires_at"))
            if fallback_expires is None or now > fallback_expires:
                return [], (fallback_expires or expires).isoformat()
            expires = min(expires, fallback_expires)
        if now > expires:
            return [], expires.isoformat()
        return pool, expires.isoformat()

    def _await_nightly_readiness(self) -> dict[str, object]:
        """Wait for the updater readiness artifact when a real date is known."""
        resolver = getattr(self._service, "_resolve_nightly_expected_trade_date", None)
        if not callable(resolver):
            return {"status": "not_configured", "allowed": True, "waited_sec": 0.0}
        try:
            expected = resolver()
        except Exception as exc:
            return {
                "status": "error",
                "allowed": False,
                "reason": f"expected_trade_date_error:{exc.__class__.__name__}",
                "waited_sec": 0.0,
            }
        expected_text = str(expected or "").strip()
        if not expected_text or expected_text == "unresolved":
            return {
                "status": "not_configured",
                "allowed": True,
                "expected_trade_date": expected_text,
                "waited_sec": 0.0,
            }

        wait_sec = self._cfg_int("night_scan_readiness_wait_sec", 900)
        poll_sec = max(1, self._cfg_int("night_scan_readiness_poll_sec", 15))
        started = monotonic()
        last_gate = check_nightly_readiness(expected_trade_date=expected)
        while not last_gate.ready and monotonic() - started < wait_sec:
            sleep(min(poll_sec, max(0.0, wait_sec - (monotonic() - started))))
            last_gate = check_nightly_readiness(expected_trade_date=expected)
        waited = round(max(0.0, monotonic() - started), 3)
        if not last_gate.ready:
            recorder = getattr(self._service, "_record_audit_event", None)
            if callable(recorder):
                recorder(
                    event_type="week5_night_scan_blocked_readiness",
                    trace_id="week5-night-scan",
                    level="warn",
                    payload={
                        "expected_trade_date": last_gate.expected_trade_date,
                        "waited_sec": waited,
                        "readiness": last_gate.payload,
                    },
                )
        return {
            "status": "ready" if last_gate.ready else "blocked",
            "allowed": bool(last_gate.ready),
            "reason": last_gate.reason,
            "expected_trade_date": last_gate.expected_trade_date,
            "payload": last_gate.payload,
            "waited_sec": waited,
        }

    def _resolve_current_data_version(self, now: datetime) -> str:
        explicit = getattr(self._service, "current_week5_data_version", "")
        if callable(explicit):
            try:
                explicit = explicit()
            except Exception:
                explicit = ""
        if str(explicit).strip():
            return str(explicit).strip()
        try:
            manifest, frame = load_feature_snapshot(self._service._config)
            if (
                manifest is not None
                and frame is not None
                and snapshot_is_current(manifest, self._service._config, now)
            ):
                return str(manifest.data_snapshot_id).strip()
        except Exception:
            pass
        return ""

    def _idempotent_night_scan(
        self,
        *,
        state: Mapping[str, object],
        trade_date: str,
        data_version: str,
    ) -> dict[str, object] | None:
        if not data_version:
            return None
        if (
            str(state.get("trade_date", "")).strip() != trade_date
            or str(state.get("data_version", "")).strip() != data_version
        ):
            return None
        latest = _mapping(state.get("latest_night_scan"))
        return {
            "ok": True,
            "status": "already_ran",
            "idempotent": True,
            "trade_date": trade_date,
            "data_version": data_version,
            "signal_mode": "overnight_advisory",
            "actionable_signals": [],
            "actionable_count": 0,
            "night_pool": _mapping_list(state.get("night_pool")),
            "overnight_top5": _mapping_list(state.get("overnight_top5")),
            "candidate_data_gate": _mapping(state.get("candidate_data_gate")),
            "actionable_data_gate": _mapping(state.get("actionable_data_gate")),
            "fallback": _mapping(state.get("fallback")),
            "readiness": _mapping(latest.get("readiness")),
            "source_report": _mapping(latest.get("source_report")),
            "state_revision": state.get("state_revision", 0),
            "notify_enabled": False,
        }

    def _arbitrate_dynamic_pool(
        self,
        *,
        existing: list[dict[str, object]],
        promoted: list[dict[str, object]],
        now: datetime,
    ) -> list[dict[str, object]]:
        positions = {
            str(item.get("symbol", "")).strip()
            for item in self._service._portfolio.positions()
            if isinstance(item, dict)
        }

        def sort_key(item: dict[str, object]) -> tuple[float, str]:
            return (
                -_as_float(item.get("radar_score", item.get("score"))),
                str(item.get("symbol", "")),
            )

        incumbents_by_symbol: dict[str, dict[str, object]] = {
            str(item.get("symbol", "")).strip(): dict(item)
            for item in existing
            if str(item.get("symbol", "")).strip()
        }
        challengers: list[dict[str, object]] = []
        seen_promoted: set[str] = set()
        for item in promoted:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or symbol in seen_promoted:
                continue
            seen_promoted.add(symbol)
            incumbent = incumbents_by_symbol.get(symbol)
            if incumbent is not None:
                # 已在池内：只刷新行情数据，保留 entered_at 使驻留计时连续，
                # 不因重复命中重置驻留保护。
                incumbent.update(
                    {
                        **item,
                        "symbol": symbol,
                        "entered_at": str(incumbent.get("entered_at", now.isoformat())),
                        "source": "market_radar_continuity",
                    }
                )
                continue
            challengers.append(
                {
                    **item,
                    "symbol": symbol,
                    "entered_at": now.isoformat(),
                    "source": "market_radar_continuity",
                }
            )
        incumbents = list(incumbents_by_symbol.values())
        ordered_incumbents = sorted(incumbents, key=sort_key)
        ordered_challengers = sorted(challengers, key=sort_key)

        cap = max(1, self._cfg_int("live_runtime_max_symbols", 8))
        keep: list[dict[str, object]] = []
        kept: set[str] = set()

        def admit(item: dict[str, object]) -> None:
            symbol = str(item.get("symbol", "")).strip()
            if symbol and symbol not in kept:
                keep.append(item)
                kept.add(symbol)

        # 持仓成员无条件优先保留（与旧口径一致）。
        for item in sorted(incumbents + challengers, key=sort_key):
            if str(item.get("symbol", "")).strip() in positions:
                admit(item)
        # 现任成员按分数依次补足席位（驻留保护）：不会因为单轮分数偏低
        # 而在驻留时间检查之前被整体淘汰。
        for item in ordered_incumbents:
            if len(keep) >= cap:
                break
            admit(item)
        # 挑战者替换席位需同时满足两个条件：
        # 1) 分数优势 ≥ market_radar_dynamic_score_margin（3 分优势）；
        # 2) 目标席位持有者驻留已满 market_radar_min_residency_min（10 分钟）。
        margin = self._cfg_float("market_radar_dynamic_score_margin", 3.0)
        residency_sec = max(0, self._cfg_int("market_radar_min_residency_min", 10)) * 60
        for item in ordered_challengers:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or symbol in kept:
                continue
            if len(keep) < cap:
                admit(item)
                continue
            weakest = min(
                (
                    candidate
                    for candidate in keep
                    if str(candidate.get("symbol", "")) not in positions
                ),
                key=lambda candidate: _as_float(
                    candidate.get("radar_score", candidate.get("score"))
                ),
                default=None,
            )
            if weakest is None:
                break
            new_score = _as_float(item.get("radar_score", item.get("score")))
            weak_score = _as_float(weakest.get("radar_score", weakest.get("score")))
            if new_score < weak_score + margin:
                continue
            entered = _parse_datetime(weakest.get("entered_at"))
            if entered is not None and (now - entered).total_seconds() < residency_sec:
                continue
            keep.remove(weakest)
            kept.discard(str(weakest.get("symbol", "")).strip())
            admit(item)
        return keep[:cap]

    def _live_runtime_pool(
        self, state: Mapping[str, object], now: datetime
    ) -> list[dict[str, object]]:
        """运行时池仲裁：现任 = 当日持久化的 live_runtime；首轮无现任时由
        有效夜池种子入席（entered_at 锚定驻留起点），此后夜池/动态池均只作
        挑战者，动态池任何情况下都不能绕过驻留期直接夺席。"""
        positions = [
            str(item.get("symbol", "")).strip()
            for item in self._service._portfolio.positions()
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        ]
        trade_date = now.date().isoformat()
        dynamic_rows = (
            _mapping_list(state.get("dynamic_candidate_pool"))
            if str(state.get("dynamic_candidate_pool_trade_date", "")).strip() == trade_date
            else []
        )
        fresh_night_pool, _ = self._fresh_previous_pool(state, now)
        night_rows = fresh_night_pool[: self._cfg_int("overnight_top_k", 5)]
        # 现任成员 = 持久化的 live_runtime 行（含 entered_at），席位与驻留
        # 计时跨轮连续。若把动态池错当现任、夜池只当挑战者，会让 09:25 入
        # 席的夜池成员被 09:30 首次出现的更高分动态池成员立即挤掉——驻留
        # 保护失效。已有现任时，夜池与动态池在这里都只是挑战者来源；首轮
        # 无现任的情形由下方种子逻辑兜底。
        incumbents = (
            _mapping_list(state.get("live_runtime"))
            if str(state.get("live_runtime_trade_date", "")).strip() == trade_date
            else []
        )
        # 首轮缺口保护：当日 live_runtime 尚未持久化（radar 与 live runtime
        # 属不同调度进程，无执行顺序保证，radar 可能先填充动态池）时，
        # 夜池成员作为现任种子入席（entered_at 锚定本轮，即驻留计时起
        # 点），动态池行只作挑战者。若夜池与动态池都进挑战者集合，仲裁
        # 对空席位按分数直接填充，高分动态行会绕过驻留检查立即夺席。
        challengers: list[dict[str, object]] = list(dynamic_rows)
        if not incumbents and night_rows:
            incumbents = [
                {**row, "entered_at": now.isoformat(), "source": "night_pool_seed"}
                for row in night_rows
            ]
        else:
            # 已有现任（或无夜池可用）：夜池行不抢现任席位，仅以挑战者
            # 身份参与补位或按分差+驻留规则竞争。
            challengers = [*dynamic_rows, *night_rows]
        arbitrated = self._arbitrate_dynamic_pool(
            existing=incumbents,
            promoted=challengers,
            now=now,
        )
        cap = max(1, self._cfg_int("live_runtime_max_symbols", 8))
        ordered = sorted(
            arbitrated,
            key=lambda item: (
                -_as_float(item.get("radar_score", item.get("score"))),
                str(item.get("symbol", "")),
            ),
        )
        by_symbol = {
            str(item.get("symbol", "")).strip(): item
            for item in ordered
            if str(item.get("symbol", "")).strip()
        }
        pool_rows: list[dict[str, object]] = []
        seen: set[str] = set()
        # 持仓无条件在席（与旧口径一致），不在任何候选池时合成最小行。
        for symbol in positions:
            if not symbol or symbol in seen:
                continue
            row = by_symbol.get(symbol)
            if row is None:
                row = {
                    "symbol": symbol,
                    "entered_at": now.isoformat(),
                    "source": "portfolio_position",
                }
            pool_rows.append(row)
            seen.add(symbol)
        for row in ordered:
            if len(pool_rows) >= cap:
                break
            symbol = str(row.get("symbol", "")).strip()
            if symbol and symbol not in seen:
                pool_rows.append(row)
                seen.add(symbol)
        return pool_rows[:cap]

    def _live_runtime_symbols(self, state: Mapping[str, object], now: datetime) -> list[str]:
        return [
            str(row.get("symbol", "")).strip()
            for row in self._live_runtime_pool(state, now)
            if str(row.get("symbol", "")).strip()
        ]

    def _cfg_int(self, name: str, default: int) -> int:
        try:
            return max(0, int(getattr(self._service._config.week5, name, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, name: str, default: float) -> float:
        try:
            return float(getattr(self._service._config.week5, name, default))
        except (TypeError, ValueError):
            return default


def _pipeline_validated_symbols(report: Mapping[str, object]) -> set[str]:
    if report.get("ok") is False:
        return set()
    rows = _mapping_list(report.get("signals")) + _mapping_list(report.get("actionable_signals"))
    validated = set(_symbols(rows))
    failed = set(_string_list(report.get("failed_symbols")))
    return validated - failed


def _baseline_values(state: Mapping[str, object]) -> dict[str, list[float]]:
    raw = state.get("by_symbol")
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, list[float]] = {}
    for symbol, values in raw.items():
        if isinstance(values, list):
            result[str(symbol)] = [_as_float(value) for value in values if _as_float(value) > 0]
    return result


def _record_auction_baseline(
    state: Mapping[str, object],
    rows: list[dict[str, object]],
    snapshot: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    dates = _string_list(state.get("dates"))
    by_symbol = _baseline_values(state)
    source_time = _parse_datetime(snapshot.get("timestamp")) or now
    if source_time.hour != 9 or source_time.minute not in range(20, 31):
        return {"dates": dates[-20:], "by_symbol": by_symbol}
    sample_date = source_time.date().isoformat()
    if sample_date not in dates:
        dates.append(sample_date)
        for row in rows:
            symbol = str(row.get("symbol", "")).strip()
            volume = _as_float(row.get("volume"))
            if symbol and volume > 0:
                by_symbol.setdefault(symbol, []).append(volume)
        dates = dates[-20:]
        for symbol, values in list(by_symbol.items()):
            by_symbol[symbol] = values[-20:]
    return {"dates": dates, "by_symbol": by_symbol}


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _symbols(rows: list[dict[str, object]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def _market_depth_fields(payload: Mapping[str, object]) -> dict[str, object]:
    bid_levels = _mapping_list(payload.get("bid_levels"))
    ask_levels = _mapping_list(payload.get("ask_levels"))
    return {
        "depth_available": bool(payload.get("available", False)),
        "depth_source": str(payload.get("source", "")).strip(),
        "depth_timestamp": str(payload.get("timestamp", "")).strip(),
        "spread": round(_as_float(payload.get("spread")), 4),
        "spread_pct": round(_as_float(payload.get("spread_pct")), 6),
        "order_imbalance": round(
            _as_float(payload.get("imbalance", payload.get("order_imbalance"))),
            6,
        ),
        "bid_total_volume": round(_as_float(payload.get("bid_total_volume")), 2),
        "ask_total_volume": round(_as_float(payload.get("ask_total_volume")), 2),
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
    }


def _rows_to_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        number = float(value)
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return default
    else:
        return default
    return number if pd.notna(number) else default


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _optional_float(value: object) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    parsed = _as_float(value, default=float("nan"))
    return parsed if pd.notna(parsed) else None


def _optional_int(value: object, default: int | None = None) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    parsed = _as_int(value, default=-1)
    return parsed if parsed >= 0 else default


def _filter_actionable_rows(
    actionable: list[dict[str, object]],
    signals: list[dict[str, object]],
) -> list[dict[str, object]]:
    trust_by_symbol = {
        str(row.get("symbol", "")).strip(): _financial_trust_level(row)
        for row in signals
        if str(row.get("symbol", "")).strip()
    }
    filtered: list[dict[str, object]] = []
    for row in actionable:
        symbol = str(row.get("symbol", "")).strip()
        trust = _financial_trust_level(row)
        if not trust:
            trust = trust_by_symbol.get(symbol, "")
        if trust in {"reported", "derived"}:
            filtered.append(row)
    return filtered


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _financial_trust_level(row: Mapping[str, object]) -> str:
    direct = str(row.get("financial_trust_level", "")).strip().lower()
    if direct:
        return direct
    trace = _mapping(row.get("decision_trace"))
    gate = _mapping(trace.get("financial_gate"))
    return str(gate.get("trust_level", "")).strip().lower()


def _financial_trust_from_rows(rows: list[dict[str, object]]) -> str:
    levels = [_financial_trust_level(row) for row in rows]
    levels = [level for level in levels if level]
    if not levels:
        return "missing"
    if any(level == "reported" for level in levels):
        return "reported"
    if any(level == "derived" for level in levels):
        return "derived"
    if any(level == "heuristic" for level in levels):
        return "heuristic"
    return "missing"


def _snapshot_age_sec(
    rows: list[dict[str, object]],
    now: datetime,
    *,
    symbols: set[str] | None = None,
) -> float | None:
    values: list[datetime] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        if symbols is not None and symbol not in symbols:
            continue
        if symbol:
            seen.add(symbol)
        parsed = _parse_datetime(row.get("snapshot_time"))
        if parsed is not None:
            values.append(parsed)
    if symbols is not None and seen != symbols:
        return None
    if not values:
        return None
    # 最坏情况口径：取最旧时间戳计算年龄。用最新时间会把"一只刚更新、
    # 其余已过期"的快照伪装成全量新鲜。
    oldest = min(values)
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    age = (current - oldest).total_seconds()
    if age < 0:
        # 负年龄（时间戳在未来）分两类：小幅偏差来自抓取时延（now 先于
        # 抓取采样）与时钟偏差，截断为 0 保持正常路径可用；大幅未来偏差
        # （时区误解释/时钟回拨）无法核实新鲜度，fail-closed 返回 None，
        # 防止旧快照伪装成新鲜。
        if age >= -_SNAPSHOT_FUTURE_TOLERANCE_SEC:
            return 0.0
        return None
    return age


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
