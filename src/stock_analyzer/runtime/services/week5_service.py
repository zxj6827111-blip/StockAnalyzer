"""Week5 intraday scan and live signal-pool workflows extracted from the runtime service."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from stock_analyzer.data.intraday_summary import fetch_sina_minute_bars, read_tdx_minute_bars
from stock_analyzer.data.tdx_sync import TdxSyncError
from stock_analyzer.evolution.execution_aware_scoring import (
    combine_execution_reranked_score,
    execution_aware_score,
    is_high_execution_risk,
    normalize_execution_model_outputs,
    normalize_execution_risk_payload,
)
from stock_analyzer.learning.execution_risk_labels import build_execution_risk_feature_vector
from stock_analyzer.models.execution_risk_predictor import ExecutionRiskPredictor
from stock_analyzer.risk.board_risk import board_decision_to_dict, evaluate_board_risk
from stock_analyzer.risk.overextension import evaluate_overextension
from stock_analyzer.runtime.services.week5_notification_service import (
    RuntimeWeek5NotificationService,
)
from stock_analyzer.runtime.services.week5_state_service import RuntimeWeek5StateService

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService

logger = logging.getLogger(__name__)


class RuntimeWeek5Service:
    """Delegated week5 scan, signal-pool, and offhours refresh workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._notification_service = RuntimeWeek5NotificationService(service)
        self._state_service = RuntimeWeek5StateService(service)

    def _build_dual_track_output(
        self,
        *,
        final_selector: dict[str, object],
        signal_map: dict[str, dict[str, object]],
        trend_signal_map: dict[str, dict[str, object]],
        dual_track: bool,
        trend_pipeline_duration_ms: int = 0,
    ) -> dict[str, object]:
        """P1 双轨输出：trend_candidates（可执行候选）vs monster_watchlist（观察池）。

        - legacy 模式：维持现状输出（final_signals 单轨），dual_track 段仅
          标注 mode=legacy，供 Shadow 对比期识别；
        - dual_track 模式：final_signals 只来自 trend 轨（含 overextension/
          board_risk 门），monster 轨全部归入 monster_watchlist 且固定
          executable=false。monster_watchlist 保留高动量标的及风险原因。
        - ``trend_pipeline_duration_ms`` 记录兼容期双跑开销，供 Shadow 对比期
          量化成本；实现"基础特征只计算一次"后该字段归零。
        """
        final_symbols = [
            str(item.get("symbol", "")).strip()
            for item in final_selector.get("final_signals", [])
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        ]
        final_symbol_set = set(final_symbols)

        # 兼容期：legacy 模式保持现状输出，dual_track 段仅标注模式供
        # Shadow 对比识别，不生成新的候选/观察池结构。
        if not dual_track:
            return {
                "mode": "legacy",
                "trend_candidates": [],
                "monster_watchlist": [],
                "monster_watchlist_count": 0,
                "trend_signal_count": 0,
                "monster_signal_count": len(signal_map),
                "final_signals_source": "legacy",
                "trend_pipeline_duration_ms": 0,
            }

        trend_candidates: list[dict[str, object]] = []
        for symbol in final_symbols:
            trend_candidates.append(
                {
                    "symbol": symbol,
                    "score": _as_float(
                        next(
                            (
                                item.get("score")
                                for item in final_selector.get("final_signals", [])
                                if isinstance(item, dict)
                                and str(item.get("symbol", "")).strip() == symbol
                            ),
                            0.0,
                        ),
                        default=0.0,
                    ),
                    "action": "buy",
                    "executable": True,
                }
            )

        monster_watchlist: list[dict[str, object]] = []
        for symbol, item in signal_map.items():
            if symbol in final_symbol_set:
                continue
            reason_values = _string_list(item.get("reasons", []))
            risk_reasons = [str(reason) for reason in reason_values if str(reason).strip()]
            monster_watchlist.append(
                {
                    "symbol": symbol,
                    "score": _as_float(item.get("score"), default=0.0),
                    "executable": False,
                    "risk_reasons": risk_reasons[:8],
                }
            )
        monster_watchlist.sort(
            key=lambda item: (
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            )
        )

        # dual_track 模式：trend_candidates 为最终可操作信号，monster 轨
        # 全部进入观察池（executable=false）。
        return {
            "mode": "dual_track",
            "trend_candidates": trend_candidates,
            "monster_watchlist": monster_watchlist[:200],
            "monster_watchlist_count": len(monster_watchlist),
            "trend_signal_count": len(trend_signal_map),
            "monster_signal_count": len(signal_map),
            "final_signals_source": "trend",
            "trend_pipeline_duration_ms": max(0, int(trend_pipeline_duration_ms)),
        }

    def _resolve_week5_offhours_scan_profile(
        self,
        *,
        now: datetime,
    ) -> dict[str, object]:
        service = self._service
        weekday = now.weekday()
        watchlist_size = len(service._state.watchlist)
        no_buy_streak = service._latest_week5_no_buy_streak()
        drawdown_pct = service._latest_runtime_drawdown_pct()
        reasons: list[str] = []
        prefer_local_universe = service._prefer_local_symbol_universe()
        research_pool_top_k = max(
            1,
            _resolve_positive_int(
                service._config.week5.offhours_research_pool_top_k,
                fallback=_as_int(service._config.week5.universe_prefilter_top_k, default=500),
            ),
        )
        watchlist_sync_top_k = max(
            1,
            _resolve_positive_int(
                service._config.week5.offhours_watchlist_sync_top_k,
                fallback=_as_int(service._config.week5.auto_sync_watchlist_top_k, default=50),
            ),
        )
        friday_full_deep = (
            weekday == 4
            and bool(service._config.week5.offhours_friday_full_deep_scan_enabled)
            and _is_at_or_after_hhmm(
                now=now,
                raw_hhmm=str(service._config.evolution.offhours_time).strip(),
                default_hhmm="20:30",
            )
        )

        weekend_full_deep = weekday >= 5 and bool(
            service._config.week5.offhours_weekend_full_deep_scan_enabled
        )
        forced_full_deep = False
        if not friday_full_deep and not weekend_full_deep:
            min_watchlist = max(
                0,
                _as_int(
                    service._config.week5.offhours_force_full_deep_scan_on_watchlist_below,
                    default=25,
                ),
            )
            no_buy_threshold = max(
                0,
                _as_int(
                    service._config.week5.offhours_force_full_deep_scan_on_no_buy_streak,
                    default=5,
                ),
            )
            drawdown_threshold = max(
                0.0,
                _as_float(
                    service._config.week5.offhours_force_full_deep_scan_on_drawdown_pct,
                    default=10.0,
                ),
            )
            if min_watchlist > 0 and watchlist_size < min_watchlist:
                forced_full_deep = True
                reasons.append(f"watchlist_below_{min_watchlist}")
            if no_buy_threshold > 0 and no_buy_streak >= no_buy_threshold:
                forced_full_deep = True
                reasons.append(f"no_buy_streak>={no_buy_threshold}")
            if drawdown_threshold > 0.0 and drawdown_pct >= drawdown_threshold:
                forced_full_deep = True
                reasons.append(f"drawdown_pct>={drawdown_threshold:.2f}")

        if friday_full_deep:
            scan_profile = "offhours_friday_full_deep"
            prefilter_enabled = False
            funnel_policy = "intentional_full_deep"
            universe_max_symbols = max(
                0,
                _as_int(service._config.week5.offhours_weekend_universe_max_symbols, default=0),
            )
            reasons.append("friday_full_deep_enabled")
        elif weekend_full_deep:
            scan_profile = "offhours_weekend_full_deep"
            prefilter_enabled = False
            funnel_policy = "intentional_full_deep"
            universe_max_symbols = max(
                0,
                _as_int(service._config.week5.offhours_weekend_universe_max_symbols, default=0),
            )
            reasons.append("weekend_full_deep_enabled")
        elif forced_full_deep:
            # forced profile 保留名称与触发原因，但执行 snapshot light/deep
            # 漏斗（prefilter 打开）：最终 pipeline 输入为 deep 入选项 + pinned
            # 增量，不再隐式全量重型扫描。周五/周末的 intentional_full_deep
            # 才保留 raw 候选 -> monster_scan_max_symbols 的全量语义。
            scan_profile = "offhours_forced_full_deep"
            prefilter_enabled = True
            funnel_policy = "snapshot_funnel"
            universe_max_symbols = max(
                0,
                _as_int(service._config.week5.offhours_weekday_universe_max_symbols, default=0),
            )
        else:
            scan_profile = "offhours_weekday_light_topk_deep"
            prefilter_enabled = True
            funnel_policy = "snapshot_funnel"
            universe_max_symbols = max(
                0,
                _as_int(service._config.week5.offhours_weekday_universe_max_symbols, default=0),
            )
            reasons.append("weekday_light_topk_deep")

        return {
            "scan_profile": scan_profile,
            "prefilter_enabled": prefilter_enabled,
            "funnel_policy": funnel_policy,
            "force_universe_scan": True,
            "universe_max_symbols": universe_max_symbols,
            "prefer_local_universe": prefer_local_universe,
            "research_pool_top_k": research_pool_top_k,
            "watchlist_sync_top_k": watchlist_sync_top_k,
            "watchlist_size": watchlist_size,
            "no_buy_streak": no_buy_streak,
            "drawdown_pct": round(drawdown_pct, 4),
            "reasons": reasons,
        }

    def run_week5_offhours_refresh(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool = True,
        sync_reason: str = "offhours_refresh",
        sync_top_k_override: int | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        profile = service._resolve_week5_offhours_scan_profile(now=now)
        review_symbols = self._active_market_radar_review_symbols(now=now)
        effective_sync_top_k_override = (
            sync_top_k_override
            if sync_top_k_override is not None
            else _as_int(profile.get("watchlist_sync_top_k"), default=0)
        )
        if not bool(service._config.week5.offhours_universe_refresh_enabled):
            report = {
                "timestamp": now.isoformat(),
                "status": "skipped",
                "reason": "offhours_universe_refresh_disabled",
                "offhours_refresh_profile": profile,
                "market_radar_review": {
                    "requested_count": len(review_symbols),
                    "requested_symbols": review_symbols,
                    "cleared_count": 0,
                    "cleared_symbols": [],
                    "remaining_count": len(review_symbols),
                },
            }
            self._annotate_offhours_research_pool(
                report=report,
                profile=profile,
                supplement_symbols=review_symbols,
            )
            return report

        if bool(profile.get("prefilter_enabled", False)):
            report = cast(
                dict[str, object],
                service.run_week5_scan(
                    timestamp=now,
                    notify_enabled=notify_enabled,
                    sync_watchlist=sync_watchlist,
                    sync_reason=sync_reason,
                    sync_top_k_override=effective_sync_top_k_override,
                    force_universe_scan=bool(profile.get("force_universe_scan", True)),
                    prefilter_enabled_override=True,
                    prefilter_top_k_override=_as_int(
                        profile.get("research_pool_top_k"),
                        default=0,
                    ),
                    universe_max_symbols_override=_as_int(
                        profile.get("universe_max_symbols"),
                        default=0,
                    ),
                    pinned_symbols=review_symbols or None,
                    scan_profile=str(profile.get("scan_profile", "")),
                ),
            )
            report["offhours_refresh_profile"] = profile
            self._finalize_offhours_market_radar_review(
                report=report,
                review_symbols=review_symbols,
                now=now,
            )
            self._annotate_offhours_research_pool(
                report=report,
                profile=profile,
                supplement_symbols=review_symbols,
            )
            return report

        quality_selector_enabled = bool(service._config.week5.universe_quality_selector_enabled)
        # When the quality selector is enabled, run_week5_scan resolves the full
        # universe and runs the selector itself; the separate _quota_sample_universe
        # call here would be a wasteful duplicate and is skipped. The annotation
        # counts are derived from the returned universe_quality_selection report.
        annotate_source = "universe"
        annotate_symbol_count = 0
        if not quality_selector_enabled:
            universe = service._resolve_symbol_universe(
                max_symbols=_as_int(profile.get("universe_max_symbols"), default=0),
                allow_seed_fallback=True,
                allow_online_sources=not bool(profile.get("prefer_local_universe", False)),
            )
            annotate_source = str(universe.get("source", "universe"))
            annotate_symbol_count = len(_string_list(universe.get("symbols", [])))
        report = cast(
            dict[str, object],
            service.run_week5_scan(
                timestamp=now,
                notify_enabled=notify_enabled,
                sync_watchlist=sync_watchlist,
                sync_reason=sync_reason,
                sync_top_k_override=effective_sync_top_k_override,
                force_universe_scan=True,
                prefilter_enabled_override=False,
                prefilter_top_k_override=_as_int(
                    profile.get("research_pool_top_k"),
                    default=0,
                ),
                universe_max_symbols_override=_as_int(
                    profile.get("universe_max_symbols"),
                    default=0,
                ),
                pinned_symbols=review_symbols or None,
                scan_profile=str(profile.get("scan_profile", "")),
            ),
        )
        report["offhours_refresh_profile"] = profile
        prefilter = report.get("prefilter")
        quality_selection = (
            prefilter.get("universe_quality_selection") if isinstance(prefilter, dict) else None
        )
        selector_mode = ""
        if isinstance(prefilter, dict):
            prefilter_source = str(prefilter.get("universe_source", "")).strip()
            if prefilter_source:
                annotate_source = prefilter_source
        if isinstance(quality_selection, dict) and quality_selection:
            selector_mode = str(quality_selection.get("selector_mode", "")).strip()
            annotate_symbol_count = _as_int(
                quality_selection.get("selected_count"),
                default=annotate_symbol_count,
            )
        report["universe_quality_selector_mode"] = selector_mode
        report["symbol_source"] = f"{annotate_source}:full_deep"
        # Friday/weekend 显式 full-deep：有意绕过漏斗，raw 候选 -> cap。
        # 与 snapshot_funnel 的 deep 空回退 fail-closed 明确区分。
        report["funnel_policy"] = "intentional_full_deep"
        report["selection_source"] = "intentional_full_deep"
        if isinstance(prefilter, dict):
            prefilter["enabled"] = False
            prefilter["applied"] = False
            prefilter["reason"] = "disabled_by_offhours_full_deep_profile"
            prefilter["funnel_policy"] = "intentional_full_deep"
            prefilter["universe_source"] = annotate_source
            prefilter["universe_quality_selector_mode"] = selector_mode
            prefilter["universe_count"] = annotate_symbol_count
            prefilter["eligible_count"] = annotate_symbol_count
            prefilter["shortlisted_count"] = annotate_symbol_count
        self._finalize_offhours_market_radar_review(
            report=report,
            review_symbols=review_symbols,
            now=now,
        )
        self._annotate_offhours_research_pool(
            report=report,
            profile=profile,
            supplement_symbols=review_symbols,
        )
        return report

    def _annotate_offhours_research_pool(
        self,
        *,
        report: dict[str, object],
        profile: Mapping[str, object],
        supplement_symbols: list[str],
    ) -> None:
        service = self._service
        prefilter = report.get("prefilter")
        signal_pool = report.get("signal_pool")
        ranking = signal_pool.get("ranking") if isinstance(signal_pool, dict) else {}
        watchlist_sync = report.get("watchlist_sync")
        configured_top_k = max(
            1,
            _as_int(
                profile.get("research_pool_top_k"),
                default=_as_int(service._config.week5.universe_prefilter_top_k, default=500),
            ),
        )
        effective_top_k = configured_top_k
        if isinstance(prefilter, dict):
            effective_top_k = max(
                effective_top_k,
                _as_int(prefilter.get("top_k"), default=configured_top_k),
            )
        scan_symbol_count = _as_int(report.get("watchlist_size"), default=0)
        if isinstance(prefilter, dict):
            scan_symbol_count = max(
                scan_symbol_count,
                _as_int(prefilter.get("selected_count"), default=0),
            )
        if scan_symbol_count <= 0 and isinstance(signal_pool, dict):
            scan_symbol_count = _as_int(signal_pool.get("candidate_count"), default=0)
        report["research_pool"] = {
            "prefilter_enabled": bool(profile.get("prefilter_enabled", False)),
            "configured_top_k": configured_top_k,
            "effective_top_k": effective_top_k,
            "scan_symbol_count": scan_symbol_count,
            "candidate_count": (
                _as_int(signal_pool.get("candidate_count"), default=0)
                if isinstance(signal_pool, dict)
                else 0
            ),
            "selected_candidate_count": (
                _as_int(ranking.get("selected_count"), default=0)
                if isinstance(ranking, dict)
                else 0
            ),
            "watchlist_sync_top_k": max(
                1,
                _as_int(
                    profile.get("watchlist_sync_top_k"),
                    default=_as_int(service._config.week5.auto_sync_watchlist_top_k, default=50),
                ),
            ),
            "watchlist_after_sync": (
                _as_int(
                    watchlist_sync.get("watchlist_after"),
                    default=len(service._state.watchlist),
                )
                if isinstance(watchlist_sync, dict)
                else len(service._state.watchlist)
            ),
            "supplement_symbol_count": len(supplement_symbols),
            "supplement_symbols": list(supplement_symbols),
        }

    def _finalize_offhours_market_radar_review(
        self,
        *,
        report: dict[str, object],
        review_symbols: list[str],
        now: datetime,
    ) -> None:
        service = self._service
        status = str(report.get("status", "ok")).strip().lower()
        should_clear = bool(review_symbols) and status not in {
            "skipped",
            "blocked_bootstrap_required",
        }
        cleared_symbols: list[str] = []
        if should_clear:
            self._clear_market_radar_review_symbols(review_symbols)
            service._persist_runtime_state_to_disk()
            cleared_symbols = list(review_symbols)
        remaining_symbols = self._active_market_radar_review_symbols(now=now)
        report["market_radar_review"] = {
            "requested_count": len(review_symbols),
            "requested_symbols": review_symbols,
            "cleared_count": len(cleared_symbols),
            "cleared_symbols": cleared_symbols,
            "remaining_count": len(remaining_symbols),
            "remaining_symbols": remaining_symbols,
        }

    def run_week5_market_radar(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        trace_id = f"week5-market-radar-{now.strftime('%Y%m%d%H%M%S')}"
        if not bool(service._config.week5.market_radar_enabled):
            report = {
                "timestamp": now.isoformat(),
                "trace_id": trace_id,
                "status": "skipped",
                "reason": "market_radar_disabled",
                "watchlist_count": len(service._state.watchlist),
                "radar_hits": [],
                "review_pool_size": len(service._market_radar_review_pool),
            }
            service._last_week5_market_radar_report = report
            return report

        prefer_local_universe = service._prefer_local_symbol_universe()
        universe = service._resolve_symbol_universe(
            max_symbols=max(
                0,
                _as_int(service._config.week5.market_radar_universe_max_symbols, default=1200),
            ),
            allow_seed_fallback=True,
            allow_online_sources=not prefer_local_universe,
        )
        raw_universe_symbols = _string_list(universe.get("symbols", []))
        watchlist_symbols = {
            symbol
            for symbol in (_normalize_a_share_symbol(item) for item in service._state.watchlist)
            if symbol
        }
        universe_symbols = [
            symbol
            for symbol in (_normalize_a_share_symbol(item) for item in raw_universe_symbols)
            if symbol and symbol not in watchlist_symbols
        ]
        universe_symbols = _dedupe_preserve_order(universe_symbols)

        prefilter_raw: dict[str, object]
        prefilter_shortlisted: list[dict[str, object]]
        if universe_symbols:
            prefilter_raw = service._prefilter_week5_universe_symbols(symbols=universe_symbols)
            prefilter_shortlisted = _dict_list(prefilter_raw.get("shortlisted"))
        else:
            prefilter_raw = {
                "enabled": True,
                "applied": False,
                "eligible_count": 0,
                "shortlisted_count": 0,
                "errors": [],
            }
            prefilter_shortlisted = []

        scan_top_n = max(1, _as_int(service._config.week5.market_radar_scan_top_n, default=80))
        min_baseline_score = max(
            0.0,
            _as_float(service._config.week5.market_radar_min_baseline_score, default=55.0),
        )
        scan_candidates: list[dict[str, object]] = []
        prefilter_preview: list[dict[str, object]] = []
        for shortlist_rank, item in enumerate(prefilter_shortlisted, start=1):
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol:
                continue
            stage1 = item.get("stage1", {})
            stage1_reason_codes = (
                _string_list(stage1.get("reason_codes", [])) if isinstance(stage1, dict) else []
            )
            baseline_score = round(_as_float(item.get("baseline_score"), default=0.0), 2)
            preview_item = {
                "symbol": symbol,
                "baseline_score": baseline_score,
                "shortlist_rank": shortlist_rank,
                "reason_codes": stage1_reason_codes[:6],
            }
            if len(prefilter_preview) < 10:
                prefilter_preview.append(preview_item)
            if baseline_score < min_baseline_score:
                continue
            scan_candidates.append(preview_item)
            if len(scan_candidates) >= scan_top_n:
                break

        live_provider = service._select_provider(use_live_runtime=True)
        radar_hits: list[dict[str, object]] = []
        for candidate in scan_candidates:
            symbol = str(candidate.get("symbol", "")).strip()
            if not symbol:
                continue
            try:
                bars = live_provider.fetch_daily_bars(symbol=symbol, lookback_days=20)
            except Exception as exc:
                radar_hits.append(
                    {
                        "symbol": symbol,
                        "name": service._resolve_symbol_display_name(symbol),
                        "baseline_score": candidate.get("baseline_score", 0.0),
                        "shortlist_rank": candidate.get("shortlist_rank", 0),
                        "reason_codes": list(candidate.get("reason_codes", [])),
                        "anomaly_types": ["data_source_error"],
                        "detail": str(exc),
                    }
                )
                continue
            if len(bars) < 2:
                continue
            anomaly = service._detect_symbol_anomaly(symbol=symbol, bars=bars)
            if anomaly is None:
                continue
            radar_hits.append(
                {
                    "symbol": symbol,
                    "name": (
                        _latest_name_from_bars(bars) or service._resolve_symbol_display_name(symbol)
                    ),
                    "baseline_score": candidate.get("baseline_score", 0.0),
                    "shortlist_rank": candidate.get("shortlist_rank", 0),
                    "reason_codes": list(candidate.get("reason_codes", [])),
                    "anomaly_types": _string_list(anomaly.get("types", [])),
                    "gap_pct": round(_as_float(anomaly.get("gap_pct"), default=0.0), 4),
                    "volume_ratio_5d": round(
                        _as_float(anomaly.get("volume_ratio_5d"), default=0.0),
                        4,
                    ),
                    "upper_shadow_pct": round(
                        _as_float(anomaly.get("upper_shadow_pct"), default=0.0),
                        4,
                    ),
                    "lower_shadow_pct": round(
                        _as_float(anomaly.get("lower_shadow_pct"), default=0.0),
                        4,
                    ),
                }
            )

        radar_hits.sort(
            key=lambda item: (
                -_as_float(item.get("baseline_score"), default=0.0),
                _as_int(item.get("shortlist_rank"), default=9999),
                str(item.get("symbol", "")),
            )
        )
        review_pool_input = [
            {
                "symbol": str(item.get("symbol", "")).strip(),
                "timestamp": now.isoformat(),
                "name": str(item.get("name", "")).strip(),
                "baseline_score": round(
                    _as_float(item.get("baseline_score"), default=0.0),
                    2,
                ),
                "shortlist_rank": _as_int(item.get("shortlist_rank"), default=0),
                "reason_codes": _string_list(item.get("reason_codes", []))[:6],
                "anomaly_types": _string_list(item.get("anomaly_types", []))[:6],
                "source": "market_radar",
            }
            for item in radar_hits
            if str(item.get("symbol", "")).strip()
        ]
        queued_research = self._queue_offhours_research_records(
            records=review_pool_input,
            now=now,
            default_source="market_radar",
        )
        review_pool = _dict_list(queued_research.get("active_pool", []))
        top_notify_candidates = radar_hits[
            : max(1, _as_int(service._config.week5.market_radar_notify_top_k, default=5))
        ]
        top_notify_hits = self._filter_new_market_radar_notification_hits(
            now=now,
            hits=top_notify_candidates,
        )
        report = {
            "timestamp": now.isoformat(),
            "trace_id": trace_id,
            "status": "ok",
            "watchlist_count": len(service._state.watchlist),
            "watchlist_excluded_count": len(watchlist_symbols),
            "universe_source": str(universe.get("source", "universe")),
            "universe_count": len(raw_universe_symbols),
            "scan_universe_count": len(universe_symbols),
            "prefilter": {
                "enabled": True,
                "applied": bool(universe_symbols),
                "eligible_count": _as_int(prefilter_raw.get("eligible_count"), default=0),
                "shortlisted_count": _as_int(prefilter_raw.get("shortlisted_count"), default=0),
                "scan_top_n": scan_top_n,
                "selected_count": len(scan_candidates),
                "min_baseline_score": round(min_baseline_score, 2),
                "errors": _string_list(prefilter_raw.get("errors", []))[:10],
                "preview": prefilter_preview,
            },
            "scan_candidates": scan_candidates,
            "radar_hits": radar_hits,
            "review_pool_added": _as_int(
                queued_research.get("queued_count"),
                default=len(review_pool_input),
            ),
            "review_pool_size": _as_int(
                queued_research.get("active_count"), default=len(review_pool)
            ),
            "review_pool_symbols": [
                str(item.get("symbol", "")).strip()
                for item in review_pool
                if str(item.get("symbol", "")).strip()
            ],
            "research_queue_added": _as_int(
                queued_research.get("queued_count"),
                default=len(review_pool_input),
            ),
            "research_queue_size": _as_int(
                queued_research.get("active_count"),
                default=len(review_pool),
            ),
            "research_queue_symbols": _string_list(queued_research.get("active_symbols", [])),
            "notification_candidates": top_notify_candidates,
            "notification_targets": top_notify_hits,
            "notification_suppressed_count": max(
                0,
                len(top_notify_candidates) - len(top_notify_hits),
            ),
            "notes": [
                "market_radar_only_alert",
                "not_in_current_live_autotrade_chain",
                "queued_for_offhours_review",
            ],
        }
        service._last_week5_market_radar_report = report
        service._record_audit_event(
            event_type="week5_market_radar",
            trace_id=trace_id,
            level="warn" if radar_hits else "info",
            payload={
                "watchlist_count": len(service._state.watchlist),
                "scan_universe_count": len(universe_symbols),
                "scan_candidates": len(scan_candidates),
                "radar_hits": len(radar_hits),
                "review_pool_size": len(review_pool),
            },
        )

        use_notify = bool(service._config.week5.market_radar_notify)
        if notify_enabled is not None:
            use_notify = notify_enabled
        if use_notify and top_notify_hits:
            self._mark_market_radar_notification_hits(now=now, hits=top_notify_hits)
            service._notify_if_changed(
                dedup_key=f"notify:week5-market-radar:{now.strftime('%Y%m%d')}",
                title=_push_title(
                    priority="P1" if len(top_notify_hits) >= 3 else "P2",
                    category="week5",
                    summary="全市场异动雷达",
                ),
                content=self._build_market_radar_notification_content(
                    top_hits=top_notify_hits,
                    report=report,
                ),
                dedup_value=self._market_radar_notification_signature(top_hits=top_notify_hits),
                level="warn",
                trace_id=trace_id,
                ttl_sec=20 * 3600,
            )

        return report

    def queue_week5_research_symbols(
        self,
        *,
        symbols: list[str],
        source: str = "manual_research",
        timestamp: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_source = str(source).strip() or "manual_research"
        normalized_metadata = (
            {str(key): value for key, value in metadata.items() if str(key).strip()}
            if isinstance(metadata, Mapping)
            else {}
        )
        records = [
            {
                **normalized_metadata,
                "symbol": symbol,
                "timestamp": now.isoformat(),
                "source": normalized_source,
            }
            for symbol in _dedupe_preserve_order(
                [
                    normalized
                    for normalized in (_normalize_a_share_symbol(item) for item in symbols)
                    if normalized
                ]
            )
        ]
        payload = self._queue_offhours_research_records(
            records=records,
            now=now,
            default_source=normalized_source,
        )
        service._record_audit_event(
            event_type="week5_research_symbols_queued",
            payload={
                "source": normalized_source,
                "queued_count": _as_int(payload.get("queued_count"), default=0),
                "active_count": _as_int(payload.get("active_count"), default=0),
                "queued_symbols": _string_list(payload.get("queued_symbols", [])),
            },
        )
        return payload

    def _queue_offhours_research_records(
        self,
        *,
        records: list[Mapping[str, object]],
        now: datetime,
        default_source: str,
    ) -> dict[str, object]:
        service = self._service
        normalized_records: list[dict[str, object]] = []
        for item in records:
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol:
                continue
            normalized = {str(key): value for key, value in item.items()}
            normalized["symbol"] = symbol
            normalized["timestamp"] = str(normalized.get("timestamp") or now.isoformat())
            normalized["source"] = (
                str(normalized.get("source") or default_source).strip() or default_source
            )
            normalized_records.append(normalized)
        active_pool = self._merge_market_radar_review_pool(records=normalized_records, now=now)
        if normalized_records:
            service._persist_runtime_state_to_disk()
        return {
            "queued_count": len(normalized_records),
            "active_count": len(active_pool),
            "queued_symbols": [
                str(item.get("symbol", "")).strip()
                for item in normalized_records
                if str(item.get("symbol", "")).strip()
            ],
            "active_symbols": [
                str(item.get("symbol", "")).strip()
                for item in active_pool
                if str(item.get("symbol", "")).strip()
            ],
            "active_pool": active_pool,
        }

    def _active_market_radar_review_pool(
        self,
        *,
        now: datetime,
    ) -> list[dict[str, object]]:
        service = self._service
        retention_hours = max(
            1.0,
            _as_float(
                service._config.week5.market_radar_review_pool_retention_hours,
                default=72.0,
            ),
        )
        cutoff = now.timestamp() - retention_hours * 3600
        active: list[dict[str, object]] = []
        for item in _dict_list(service._market_radar_review_pool):
            recorded_at = _safe_datetime(item.get("timestamp"))
            if recorded_at is not None and recorded_at.timestamp() < cutoff:
                continue
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol:
                continue
            normalized = dict(item)
            normalized["symbol"] = symbol
            active.append(normalized)
        active.sort(key=_market_radar_review_sort_key)
        limit = max(
            1,
            _as_int(service._config.week5.market_radar_review_pool_max_symbols, default=80),
        )
        if len(active) > limit:
            active = active[-limit:]
        service._market_radar_review_pool = active
        return list(active)

    def _merge_market_radar_review_pool(
        self,
        *,
        records: list[dict[str, object]],
        now: datetime,
    ) -> list[dict[str, object]]:
        service = self._service
        merged: dict[str, dict[str, object]] = {}
        for item in self._active_market_radar_review_pool(now=now):
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if symbol:
                merged[symbol] = dict(item)
        for item in records:
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol:
                continue
            normalized = dict(item)
            normalized["symbol"] = symbol
            normalized["timestamp"] = str(normalized.get("timestamp") or now.isoformat())
            merged[symbol] = normalized
        values = list(merged.values())
        values.sort(key=_market_radar_review_sort_key)
        limit = max(
            1,
            _as_int(service._config.week5.market_radar_review_pool_max_symbols, default=80),
        )
        if len(values) > limit:
            values = values[-limit:]
        service._market_radar_review_pool = values
        return list(values)

    def _active_market_radar_review_symbols(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        active_pool = self._active_market_radar_review_pool(now=now or datetime.now())
        return _dedupe_preserve_order(
            [
                str(item.get("symbol", "")).strip()
                for item in active_pool
                if str(item.get("symbol", "")).strip()
            ]
        )

    def _clear_market_radar_review_symbols(self, symbols: list[str]) -> None:
        service = self._service
        normalized_symbols = {
            symbol for symbol in (_normalize_a_share_symbol(item) for item in symbols) if symbol
        }
        if not normalized_symbols:
            return
        service._market_radar_review_pool = [
            item
            for item in _dict_list(service._market_radar_review_pool)
            if _normalize_a_share_symbol(item.get("symbol")) not in normalized_symbols
        ]

    def _filter_new_market_radar_notification_hits(
        self,
        *,
        now: datetime,
        hits: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        service = self._service
        fresh_hits: list[dict[str, object]] = []
        for item in hits:
            cache_key = self._market_radar_notification_item_cache_key(now=now, item=item)
            if service._cache.exists(cache_key):
                continue
            fresh_hits.append(item)
        return fresh_hits

    def _mark_market_radar_notification_hits(
        self,
        *,
        now: datetime,
        hits: list[dict[str, object]],
    ) -> None:
        service = self._service
        for item in hits:
            cache_key = self._market_radar_notification_item_cache_key(now=now, item=item)
            service._cache.set(cache_key, "1", ttl_sec=20 * 3600)

    def _market_radar_notification_item_cache_key(
        self,
        *,
        now: datetime,
        item: dict[str, object],
    ) -> str:
        symbol = _normalize_a_share_symbol(item.get("symbol"))
        anomaly_types = sorted(
            {anomaly for anomaly in (_string_list(item.get("anomaly_types", []))[:6]) if anomaly}
        )
        anomaly_signature = "+".join(anomaly_types) if anomaly_types else "anomaly"
        return (
            "notify:week5-market-radar:item:"
            f"{now.strftime('%Y%m%d')}:{symbol or 'unknown'}:{anomaly_signature}"
        )

    def _build_market_radar_notification_content(
        self,
        *,
        top_hits: list[dict[str, object]],
        report: dict[str, object],
    ) -> str:
        service = self._service
        scan_universe_count = _as_int(report.get("scan_universe_count"), default=0)
        review_pool_size = _as_int(report.get("review_pool_size"), default=0)
        lines = [
            "全市场异动雷达补充提醒",
            (
                f"扫描池外股票 {scan_universe_count} 只，"
                f"命中 {len(top_hits)} 只，已并入晚间复盘池 {review_pool_size} 只"
            ),
            (
                "这些票当前不会触发盘中自动买卖或模拟盘自动成交，"
                "系统会在晚间复盘后再判断是否纳入次日 watchlist。"
            ),
        ]
        for index, item in enumerate(top_hits, start=1):
            symbol = str(item.get("symbol", "")).strip()
            name = str(item.get("name", "")).strip()
            symbol_label = service._format_symbol_display(symbol, name)
            anomaly_types = [
                _market_radar_anomaly_type_zh(value)
                for value in _string_list(item.get("anomaly_types", []))[:4]
            ]
            reason_codes = [
                _market_radar_reason_code_zh(value)
                for value in _string_list(item.get("reason_codes", []))[:4]
            ]
            baseline_score = _as_float(item.get("baseline_score"), default=0.0)
            detail_parts = [
                f"基线分 {baseline_score:.2f}",
                f"异动 {'、'.join(anomaly_types) if anomaly_types else '异常'}",
                f"预筛 {'、'.join(reason_codes) if reason_codes else '基础筛选'}",
            ]
            gap_pct = _as_float(item.get("gap_pct"), default=0.0)
            if abs(gap_pct) > 1e-8:
                detail_parts.append(f"跳空 {gap_pct:.2%}")
            volume_ratio_5d = _as_float(item.get("volume_ratio_5d"), default=0.0)
            if volume_ratio_5d > 0.0:
                detail_parts.append(f"量比 {volume_ratio_5d:.2f}x")
            upper_shadow_pct = _as_float(item.get("upper_shadow_pct"), default=0.0)
            if upper_shadow_pct > 0.0:
                detail_parts.append(f"上影 {upper_shadow_pct:.2%}")
            lower_shadow_pct = _as_float(item.get("lower_shadow_pct"), default=0.0)
            if lower_shadow_pct > 0.0:
                detail_parts.append(f"下影 {lower_shadow_pct:.2%}")
            lines.append(f"{index}. {symbol_label}｜" + "｜".join(detail_parts))
        return "\n".join(lines)

    def _market_radar_notification_signature(
        self,
        *,
        top_hits: list[dict[str, object]],
    ) -> str:
        payload = [
            {
                "symbol": str(item.get("symbol", "")).strip(),
                "anomaly_types": sorted(
                    {
                        anomaly
                        for anomaly in _string_list(item.get("anomaly_types", []))[:6]
                        if anomaly
                    }
                ),
            }
            for item in top_hits
        ]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _build_gate_blocked_report(
        self,
        *,
        now: datetime,
        reasons: list[str],
        data_snapshot_id: str,
        snapshot_current: bool,
        scan_profile: str = "",
        watchlist_size: int | None = None,
    ) -> dict[str, object]:
        """Fail-closed week5 scan report when the data gate is blocked."""
        resolved_size = (
            len(self._service._state.watchlist)
            if watchlist_size is None
            else watchlist_size
        )
        return {
            "timestamp": now.isoformat(),
            "trace_id": "",
            "status": "blocked_data_gate",
            "watchlist_size": resolved_size,
            "symbol_source": "blocked",
            "scan_profile": scan_profile.strip() or "default",
            "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
            "signal_pool": {"candidate_count": 0, "candidates": []},
            "anomalies": {"event_count": 0, "events": []},
            "empty_signal": {
                "triggered": True,
                "reasons": ["data_gate_blocked"] + reasons,
                "no_buy_streak": 0,
                "buy_signals": 0,
                "drawdown_pct": 0.0,
                "risk_action": "blocked",
            },
            "monster_isolation": {
                "can_open_new_position": False,
                "reasons": ["data_gate_blocked"],
                "total_monster_position": 0.0,
                "max_monster_position": 0.0,
                "sentiment_score": 0.0,
            },
            "summary": {
                "first_board_candidates": 0,
                "leaders": 0,
                "anomalies": 0,
                "empty_signal_triggered": True,
                "can_open_monster": False,
                "watchlist_synced": False,
            },
            "data_gate": {
                "status": "blocked",
                "reasons": reasons,
                "data_snapshot_id": data_snapshot_id,
                "snapshot_current": snapshot_current,
            },
        }

    def run_week5_scan(
        self,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool | None = None,
        sync_reason: str = "",
        sync_top_k_override: int | None = None,
        force_universe_scan: bool = False,
        prefilter_enabled_override: bool | None = None,
        prefilter_top_k_override: int | None = None,
        universe_max_symbols_override: int | None = None,
        deep_candidate_target_override: int | None = None,
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
        recovery_mode: bool = False,
    ) -> dict[str, object]:
        """执行一次 Week5 扫描（live context → 共享选股引擎）。

        漏斗编排全部在 :class:`Week5SelectionEngine.run()` 中；本方法只负责
        live 上下文装配（真实账户状态/调度覆盖参数/通知开关）与进度文件
        生命周期。historical 回测通过同一引擎 + historical context 复用同一
        套阶段实现（见 ``week5_historical_runner.py``）。
        """
        from stock_analyzer.runtime.services.week5_selection_engine import (
            Week5AccountState,
            Week5RunContext,
            Week5RunPolicy,
            Week5SelectionEngine,
        )
        from stock_analyzer.runtime.services.week5_selection_engine import (
            Week5EngineBackend as _Week5EngineBackend,
        )

        service = self._service
        if service._bootstrap_runtime_blocked():
            blocked = {
                "timestamp": (timestamp or datetime.now()).isoformat(),
                "trace_id": "",
                "status": "blocked_bootstrap_required",
                "watchlist_size": len(service._state.watchlist),
                "symbol_source": "blocked",
                "scan_profile": scan_profile.strip() or "default",
                "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
                "signal_pool": {"candidate_count": 0, "candidates": []},
                "anomalies": {"event_count": 0, "events": []},
                "empty_signal": {
                    "triggered": True,
                    "reasons": ["bootstrap_required"],
                    "no_buy_streak": 0,
                    "buy_signals": 0,
                    "drawdown_pct": 0.0,
                    "risk_action": "blocked",
                },
                "monster_isolation": {
                    "can_open_new_position": False,
                    "reasons": ["bootstrap_required"],
                    "total_monster_position": 0.0,
                    "max_monster_position": 0.0,
                    "sentiment_score": 0.0,
                },
                "summary": {
                    "first_board_candidates": 0,
                    "leaders": 0,
                    "anomalies": 0,
                    "empty_signal_triggered": True,
                    "can_open_monster": False,
                    "watchlist_synced": False,
                },
                "bootstrap": service.training_bootstrap_status(),
            }
            self._state_service.store_week5_scan_report(blocked)
            service._record_audit_event(
                event_type="week5_scan_blocked_bootstrap",
                level="warn",
                payload={"bootstrap": blocked["bootstrap"]},
            )
            return blocked

        # 进度文件由外层统一维护终态：正常结束写 completed，异常写 failed
        # 与受控错误摘要，且不改变异常传播与扫描结果语义。
        progress = Week5ScanProgress(
            service=service,
            scan_profile=scan_profile.strip() or "default",
        )
        progress.update(status="running", phase="quality")
        now = timestamp or datetime.now()
        context = Week5RunContext(
            mode="live",
            now=now,
            symbols=symbols,
            pinned_symbols=list(pinned_symbols or []),
            account=Week5AccountState(
                current_equity=float(service._state.current_equity),
                watchlist=list(service._state.watchlist),
                pause_new_buy=bool(getattr(service._state, "pause_new_buy", False)),
                no_buy_streak=self._run_summaries_no_buy_streak(),
                monster_positions=self._monster_positions(),
            ),
            progress=progress.update,
            sync_reason=sync_reason,
            scan_profile=scan_profile,
            force_universe_scan=bool(force_universe_scan),
            recovery_mode=bool(recovery_mode),
            sync_watchlist=sync_watchlist,
            sync_top_k_override=sync_top_k_override,
            prefilter_enabled_override=prefilter_enabled_override,
            prefilter_top_k_override=prefilter_top_k_override,
            universe_max_symbols_override=universe_max_symbols_override,
            deep_candidate_target_override=deep_candidate_target_override,
        )
        policy = Week5RunPolicy.live()
        policy.notify = (
            bool(service._config.week5.auto_notify)
            if notify_enabled is None
            else bool(notify_enabled)
        )
        engine = Week5SelectionEngine(
            backend=cast("_Week5EngineBackend", self), context=context, policy=policy
        )
        try:
            report = engine.run()
        except Exception as exc:
            progress.fail(error=exc)
            raise
        # 终态区分：data gate blocked（fail-closed 设计内结果）记为 blocked，
        # 避免外部监控把被拦截的扫描误判为成功完成。
        report_status = str(report.get("status", "")).strip()
        if report_status == "blocked_data_gate":
            progress.update(status="blocked")
        else:
            progress.update(status="completed")
        return report

    # ------------------------------------------------------------------
    # Week5EngineBackend 实现：引擎的阶段实现钩子（live/historical 共用）
    # ------------------------------------------------------------------
    @property
    def service(self) -> StockAnalyzerService:
        return self._service

    @property
    def config(self) -> Any:
        return self._service._config  # noqa: SLF001 - backend 契约

    def build_data_gate(
        self,
        *,
        snapshot_manifest: object,
        snapshot_current: bool,
        latest_trade_date: str,
        now: object,
    ) -> dict[str, object]:
        return self._service._build_data_gate(  # noqa: SLF001
            snapshot_manifest=snapshot_manifest,
            snapshot_current=snapshot_current,
            latest_trade_date=latest_trade_date,
            now=now,
        )

    def prefer_local_symbol_universe(self) -> bool:
        return self._service._prefer_local_symbol_universe()  # noqa: SLF001

    def resolve_symbol_universe(self, **kwargs: Any) -> dict[str, object]:
        return self._service._resolve_symbol_universe(**kwargs)  # noqa: SLF001

    def universe_seed_trade_date(self) -> str:
        return self._service._resolve_universe_seed_trade_date()  # noqa: SLF001

    def select_universe_quality_candidates(self, **kwargs: Any) -> dict[str, object]:
        return self._service._select_universe_quality_candidates(**kwargs)  # noqa: SLF001

    def ensure_feature_snapshot(self, *, symbols: list[str], scope: str) -> dict[str, object]:
        return self._service.ensure_week5_feature_snapshot(symbols=symbols, scope=scope)  # noqa: SLF001

    def light_stage_from_snapshot(
        self, *, frame: Any, target: int, allowed_exchanges: set[str]
    ) -> dict[str, object]:
        return self._service._light_stage_from_snapshot(  # noqa: SLF001
            frame=frame,
            target=target,
            allowed_exchanges=allowed_exchanges,
        )

    def deep_stage_from_snapshot(
        self, *, frame: Any, target: int, light_report: dict[str, object]
    ) -> dict[str, object]:
        return self._service._deep_stage_from_snapshot(  # noqa: SLF001
            frame=frame,
            target=target,
            light_report=light_report,
        )

    def prefilter_universe_symbols(
        self, *, symbols: list[str], top_k_override: int | None = None
    ) -> dict[str, object]:
        return self._service._prefilter_week5_universe_symbols(  # noqa: SLF001
            symbols=symbols,
            top_k_override=top_k_override,
        )

    def run_pipeline(self, **kwargs: Any) -> dict[str, object]:
        return self._service.run_pipeline(**kwargs)  # noqa: SLF001

    def select_live_runtime_provider(self) -> object:
        return self._service._select_provider(use_live_runtime=True)  # noqa: SLF001

    def score_signal_pool_candidate(
        self,
        *,
        signal: Any,
        prefilter_detail: Any,
    ) -> dict[str, object]:
        return self._service._score_week5_signal_pool_candidate(  # noqa: SLF001
            signal=signal,
            prefilter_detail=prefilter_detail,
        )

    def apply_execution_aware_rerank(
        self, *, candidates: list[dict[str, object]]
    ) -> dict[str, object]:
        return self._apply_execution_aware_rerank(candidates=candidates)

    def final_signal_selector(
        self,
        *,
        signals: list[dict[str, object]],
        data_gate_status: str,
        min_threshold_lift: float = 0.0,
        news_mode_override: str | None = None,
    ) -> dict[str, object]:
        return self._service._final_signal_selector(  # noqa: SLF001
            signals=cast("list[object]", signals),
            data_gate_status=data_gate_status,
            min_threshold_lift=min_threshold_lift,
            news_mode_override=news_mode_override,
        )

    def build_first_board_candidate(
        self, *, symbol: str, bars: Any, signal: dict[str, object]
    ) -> dict[str, object] | None:
        return self._service._build_first_board_candidate(  # noqa: SLF001
            symbol=symbol,
            bars=bars,
            signal=signal,
        )

    def detect_symbol_anomaly(
        self, *, symbol: str, bars: Any
    ) -> dict[str, object] | None:
        return self._service._detect_symbol_anomaly(symbol=symbol, bars=bars)  # noqa: SLF001

    def estimate_sentiment(self, *, monster_report: dict[str, object]) -> tuple[float, bool]:
        return self._service._estimate_sentiment(monster_report=monster_report)  # noqa: SLF001

    def market_breadth_gate(self, *, now: datetime) -> tuple[dict[str, object], float]:
        return self._service._apply_market_breadth_gate(now=now)  # noqa: SLF001

    def build_gate_blocked_report(
        self,
        *,
        now: datetime,
        reasons: list[str],
        data_snapshot_id: str,
        snapshot_current: bool,
        scan_profile: str,
        watchlist_size: int | None = None,
    ) -> dict[str, object]:
        resolved_size = (
            len(self._service._state.watchlist)  # noqa: SLF001
            if watchlist_size is None
            else watchlist_size
        )
        return self._build_gate_blocked_report(
            now=now,
            reasons=reasons,
            data_snapshot_id=data_snapshot_id,
            snapshot_current=snapshot_current,
            scan_profile=scan_profile,
            watchlist_size=resolved_size,
        )

    def build_dual_track_output(self, **kwargs: Any) -> dict[str, object]:
        return self._build_dual_track_output(**kwargs)

    def store_report(self, report: dict[str, object]) -> None:
        self._state_service.store_week5_scan_report(report)

    def record_audit(
        self,
        *,
        event_type: str,
        level: str = "info",
        trace_id: str = "",
        payload: dict[str, object] | None = None,
    ) -> None:
        self._service._record_audit_event(  # noqa: SLF001
            event_type=event_type,
            level=level,
            trace_id=trace_id,
            payload=payload if payload is not None else {},
        )

    def sync_watchlist_from_report(
        self,
        *,
        report: dict[str, object],
        reason: str,
        top_k_override: int | None = None,
    ) -> dict[str, object]:
        return self._state_service.auto_sync_watchlist_from_week5_report(
            report=report,
            reason=reason,
            top_k_override=top_k_override,
            allow_signal_pool_fallback=False,
        )

    def watchlist_sync_diagnostics(
        self, *, report: dict[str, object], top_k_override: int | None = None
    ) -> dict[str, object]:
        return self._state_service.build_watchlist_sync_diagnostics(
            report=report,
            top_k_override=top_k_override,
            selected=[],
            fallback_applied=False,
            allow_signal_pool_fallback=False,
        )

    def notify_scan(
        self,
        *,
        symbol_list: list[str],
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
        watchlist_sync: dict[str, object],
        runtime_mode: str,
        has_warning: bool,
        trace_id: str,
        now: datetime,
    ) -> None:
        content = self._notification_service.build_scan_notification_content(
            symbol_list=symbol_list,
            first_board_candidates=first_board_candidates,
            leaders=leaders,
            anomalies=anomalies,
            empty_signal=empty_signal,
            watchlist_sync=watchlist_sync,
            runtime_mode=runtime_mode,
        )
        self._service._notify_if_changed(  # noqa: SLF001
            dedup_key=f"notify:week5-scan:{now.strftime('%Y%m%d')}",
            title=_push_title(
                priority="P1" if has_warning else "P2",
                category="week5",
                summary="intraday scan",
            ),
            content=content,
            dedup_value=self._notification_service.week5_scan_notification_signature(
                first_board_candidates=first_board_candidates,
                leaders=leaders,
                anomalies=anomalies,
                empty_signal=empty_signal,
            ),
            level="warn" if has_warning else "info",
            trace_id=trace_id,
            ttl_sec=20 * 3600,
        )

    def notify_actionable_signals(
        self, report: dict[str, object], *, trace_id: str, title_prefix: str
    ) -> None:
        self._service._notify_actionable_signals(  # noqa: SLF001
            report,
            trace_id=trace_id,
            title_prefix=title_prefix,
        )

    def is_intraday_scheduler_scan(self, *, now: datetime, sync_reason: str) -> bool:
        return self._is_intraday_scheduler_week5_scan(now=now, sync_reason=sync_reason)

    def latest_preserved_watchlist_symbols(
        self, *, top_k_override: int | None = None
    ) -> list[str]:
        return self._state_service.latest_preserved_watchlist_symbols(
            top_k_override=top_k_override,
        )

    def market_warehouse(self) -> object:
        return self._service._market_warehouse()  # noqa: SLF001

    def provider(self) -> object:
        return self._service._provider  # noqa: SLF001

    def provider_graph(self) -> list[object]:
        return self._service._iter_market_data_provider_graph()  # noqa: SLF001

    def runtime_source_mode(self) -> str:
        return (
            "realtime_overlay"
            if self._service._realtime_pipeline is not None  # noqa: SLF001
            else "offline_only"
        )

    def _run_summaries_no_buy_streak(self) -> int:
        """从 run summaries 计算"连续无 actionable 信号"次数（empty_signal 用）。"""
        service = self._service
        no_buy_streak = 0
        for item in reversed(service._run_summaries):  # noqa: SLF001
            actionable = _as_int(item.get("actionable"), default=0)
            if actionable <= 0:
                no_buy_streak += 1
            else:
                break
        return no_buy_streak

    def _monster_positions(self) -> list[float]:
        """当前 monster 策略持仓的目标仓位列表（monster_isolation 门输入）。"""
        try:
            positions = self._service._portfolio.positions()  # noqa: SLF001
        except Exception:
            return []
        return [
            _as_float(item.get("target_position"), default=0.0)
            for item in positions
            if isinstance(item, dict)
            and str(item.get("strategy", "")).strip().lower() == "monster"
        ]

    @staticmethod
    def _final_pipeline_timing_report(
        runtime_payload: dict[str, object],
    ) -> dict[str, object]:
        """final pipeline 聚合子阶段耗时 + 最慢 5 只股票（Phase 1 可观测性）。"""
        timing: dict[str, object] = {}
        stage_ms = runtime_payload.get("pipeline_stage_ms")
        if isinstance(stage_ms, dict):
            for key in (
                "fetch_bars_ms",
                "feature_engine_ms",
                "inference_ms",
                # 子阶段细分：intraday/market-context 已含在 feature_engine_ms
                # 桶内，此处并列展示供耗时下钻。
                "intraday_ms",
                "market_context_ms",
                "cross_review_ms",
                "score_risk_ms",
                "learning_persist_ms",
                "completed_count",
            ):
                timing[key] = _as_int(stage_ms.get(key), default=0)
        parallel_transform = runtime_payload.get("pipeline_parallel_transform")
        if isinstance(parallel_transform, dict):
            timing["parallel_transform"] = dict(parallel_transform)
        raw_symbol_ms = runtime_payload.get("pipeline_symbol_ms")
        symbol_ms: list[dict[str, object]] = []
        if isinstance(raw_symbol_ms, list):
            for item in raw_symbol_ms:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    symbol_ms.append(
                        {
                            "symbol": symbol,
                            "duration_ms": _as_int(item.get("duration_ms"), default=0),
                        }
                    )
        symbol_ms.sort(
            key=lambda item: (
                -_as_int(item.get("duration_ms"), default=0),
                str(item.get("symbol", "")),
            )
        )
        timing["slowest_symbols"] = symbol_ms[:5]
        return timing

    def _apply_execution_aware_rerank(
        self,
        *,
        candidates: list[dict[str, object]],
    ) -> dict[str, object]:
        service = self._service
        candidate_count = len(candidates)
        for candidate in candidates:
            shortlist_score = round(_as_float(candidate.get("shortlist_score"), default=0.0), 2)
            candidate["execution_reranked_score"] = shortlist_score
            candidate["execution_rerank_applied"] = False
            candidate["execution_rerank_reason"] = "execution_risk_artifact_unavailable"
            candidate["execution_risk"] = {}

        if candidate_count == 0:
            return {
                "applied": False,
                "score_key": "shortlist_score",
                "candidate_count": 0,
                "applied_count": 0,
                "coverage_ratio": 0.0,
                "artifact_path": "",
                "reason": "no_candidates",
            }
        if getattr(service, "_sample_store", None) is None:
            return {
                "applied": False,
                "score_key": "shortlist_score",
                "candidate_count": candidate_count,
                "applied_count": 0,
                "coverage_ratio": 0.0,
                "artifact_path": "",
                "reason": "sample_store_unavailable",
            }

        latest_training = service.latest_execution_risk_training() or {}
        artifact_path = self._resolve_execution_risk_artifact_path(
            str(latest_training.get("artifact_path", "")).strip()
        )
        if artifact_path is None:
            return {
                "applied": False,
                "score_key": "shortlist_score",
                "candidate_count": candidate_count,
                "applied_count": 0,
                "coverage_ratio": 0.0,
                "artifact_path": "",
                "reason": "execution_risk_artifact_unavailable",
            }
        if not artifact_path.exists():
            for candidate in candidates:
                candidate["execution_rerank_reason"] = "execution_risk_artifact_missing"
            return {
                "applied": False,
                "score_key": "shortlist_score",
                "candidate_count": candidate_count,
                "applied_count": 0,
                "coverage_ratio": 0.0,
                "artifact_path": str(artifact_path),
                "reason": "execution_risk_artifact_missing",
            }

        try:
            predictor = ExecutionRiskPredictor.load(artifact_path)
        except Exception as exc:
            reason = f"execution_risk_predictor_load_failed:{exc.__class__.__name__}"
            for candidate in candidates:
                candidate["execution_rerank_reason"] = reason
            return {
                "applied": False,
                "score_key": "shortlist_score",
                "candidate_count": candidate_count,
                "applied_count": 0,
                "coverage_ratio": 0.0,
                "artifact_path": str(artifact_path),
                "reason": reason,
            }

        # 预探测：legacy 无 scaler 等 artifact 缺陷会让逐票推理全批失败
        # （2026-08-22 NAS 实测 20/20 prediction_failed:ValueError）。先用零向量
        # 试推一次，失败则整批短路，给出精确原因并留审计痕迹（fail-fast）。
        try:
            predictor.predict_features({name: 0.0 for name in predictor.feature_names})
        except Exception as exc:
            blocked_detail = _artifact_inference_blocked_detail(predictor=predictor, exc=exc)
            blocked_reason = f"artifact_inference_blocked:{blocked_detail}"
            for candidate in candidates:
                candidate["execution_rerank_reason"] = blocked_reason
            service._record_audit_event(
                event_type="execution_risk_artifact_unusable",
                level="warn",
                payload={
                    "artifact_path": str(artifact_path),
                    "qualification_status": predictor.qualification_status,
                    "skipped_prediction_failed": candidate_count,
                    "candidate_count": candidate_count,
                    "phase": "probe",
                    "detail": blocked_detail,
                },
            )
            return {
                "applied": False,
                "score_key": "shortlist_score",
                "candidate_count": candidate_count,
                "applied_count": 0,
                "coverage_ratio": 0.0,
                "artifact_path": str(artifact_path),
                "reason": blocked_reason,
                "qualification_status": predictor.qualification_status,
                "shadow_predictions": 0,
                "skipped_missing_snapshot": 0,
                "skipped_snapshot_not_found": 0,
                "skipped_snapshot_read_failed": 0,
                "skipped_prediction_failed": candidate_count,
            }

        shadow_only = not predictor.can_rerank

        applied_count = 0
        skipped_missing_snapshot = 0
        skipped_snapshot_not_found = 0
        skipped_snapshot_read_failed = 0
        skipped_prediction_failed = 0
        for candidate in candidates:
            snapshot_id = str(candidate.get("snapshot_id", "")).strip() or (
                _extract_learning_snapshot_id(candidate)
            )
            snapshot = None
            if snapshot_id:
                try:
                    snapshot = service._sample_store.get_snapshot(snapshot_id)
                except Exception as exc:
                    skipped_snapshot_read_failed += 1
                    candidate["execution_rerank_reason"] = (
                        f"snapshot_read_failed:{exc.__class__.__name__}"
                    )
                    continue
            if snapshot is None:
                symbol = _normalize_a_share_symbol(str(candidate.get("symbol", "")).strip())
                fallback_snapshot = None
                if symbol:
                    try:
                        fallback_snapshot = service._sample_store.latest_snapshot_for_symbol(
                            symbol=symbol,
                            before=datetime.now(UTC),
                        )
                    except Exception:
                        fallback_snapshot = None
                if fallback_snapshot is not None:
                    snapshot = fallback_snapshot
                    snapshot_id = snapshot.snapshot_id
                    candidate["execution_rerank_snapshot_fallback"] = True
            if snapshot is None:
                if not snapshot_id:
                    skipped_missing_snapshot += 1
                    candidate["execution_rerank_reason"] = "snapshot_id_missing"
                else:
                    skipped_snapshot_not_found += 1
                    candidate["execution_rerank_reason"] = "snapshot_not_found"
                continue

            raw_probabilities = candidate.get("probabilities")
            model_outputs = normalize_execution_model_outputs(
                raw_probabilities if isinstance(raw_probabilities, Mapping) else None
            )
            if not model_outputs:
                model_outputs = normalize_execution_model_outputs(snapshot.model_outputs)

            try:
                feature_vector = build_execution_risk_feature_vector(
                    snapshot=snapshot,
                    model_outputs=model_outputs or None,
                )
                risk = predictor.predict_features(feature_vector)
            except Exception as exc:
                skipped_prediction_failed += 1
                candidate["execution_rerank_reason"] = f"prediction_failed:{exc.__class__.__name__}"
                continue

            base_probability = self._resolve_execution_base_probability(
                candidate=candidate,
                snapshot_model_outputs=snapshot.model_outputs,
                model_outputs=model_outputs,
            )
            high_risk = is_high_execution_risk(risk)
            execution_score_value = execution_aware_score(
                base_probability=base_probability,
                risk=risk,
            )
            candidate["snapshot_id"] = snapshot_id
            candidate["execution_probability"] = round(base_probability, 6)
            candidate["execution_aware_score"] = round(execution_score_value, 6)
            candidate["execution_high_risk"] = high_risk
            candidate["execution_risk"] = normalize_execution_risk_payload(risk)
            candidate["execution_risk_mode"] = "shadow_only" if shadow_only else "qualified"
            if shadow_only:
                candidate["execution_reranked_score"] = _as_float(
                    candidate.get("shortlist_score"), default=0.0
                )
                candidate["execution_rerank_applied"] = False
                candidate["execution_rerank_reason"] = "artifact_shadow_only"
            else:
                candidate["execution_reranked_score"] = combine_execution_reranked_score(
                    shortlist_score=_as_float(candidate.get("shortlist_score"), default=0.0),
                    execution_aware_score_value=execution_score_value,
                    high_execution_risk=high_risk,
                )
                candidate["execution_rerank_applied"] = True
                candidate["execution_rerank_reason"] = "applied"
            applied_count += 1

        # 残余逐票型推理失败（预探测已通过但个别候选仍失败）也要留痕，
        # 避免静默退化只体现在计数器上。
        if skipped_prediction_failed > 0:
            residual_reason_counts: dict[str, int] = {}
            for candidate in candidates:
                residual_reason = str(candidate.get("execution_rerank_reason", "")).strip()
                if not residual_reason:
                    continue
                residual_reason_counts[residual_reason] = (
                    residual_reason_counts.get(residual_reason, 0) + 1
                )
            service._record_audit_event(
                event_type="execution_risk_artifact_unusable",
                level="warn",
                payload={
                    "artifact_path": str(artifact_path),
                    "qualification_status": predictor.qualification_status,
                    "skipped_prediction_failed": skipped_prediction_failed,
                    "candidate_count": candidate_count,
                    "phase": "residual",
                    "reason_counts": residual_reason_counts,
                },
            )

        applied = applied_count > 0 and not shadow_only
        return {
            "applied": applied,
            "score_key": "execution_reranked_score" if applied else "shortlist_score",
            "candidate_count": candidate_count,
            "applied_count": applied_count,
            "coverage_ratio": round(applied_count / max(1, candidate_count), 6),
            "artifact_path": str(artifact_path),
            "reason": (
                "applied"
                if applied
                else "artifact_shadow_only"
                if applied_count > 0 and shadow_only
                else "no_candidate_snapshot_match"
            ),
            "qualification_status": predictor.qualification_status,
            "shadow_predictions": applied_count if shadow_only else 0,
            "skipped_missing_snapshot": skipped_missing_snapshot,
            "skipped_snapshot_not_found": skipped_snapshot_not_found,
            "skipped_snapshot_read_failed": skipped_snapshot_read_failed,
            "skipped_prediction_failed": skipped_prediction_failed,
        }

    def _resolve_execution_risk_artifact_path(self, artifact_path: str) -> Path | None:
        service = self._service
        normalized = artifact_path.strip()
        if not normalized:
            return None
        candidate = Path(normalized).expanduser()
        if candidate.is_absolute():
            return candidate
        return service._resolve_evolution_path(str(candidate))

    def _resolve_execution_base_probability(
        self,
        *,
        candidate: Mapping[str, object],
        snapshot_model_outputs: Mapping[str, object],
        model_outputs: Mapping[str, float],
    ) -> float:
        fallback_score = _clip01(
            _as_float(
                candidate.get("shortlist_score"),
                default=_as_float(candidate.get("score"), default=0.0),
            )
            / 100.0
        )
        normalized_snapshot_outputs = normalize_execution_model_outputs(snapshot_model_outputs)
        for payload in (model_outputs, normalized_snapshot_outputs):
            for key in ("p_meta", "meta", "p_lgbm", "lgbm", "p_xgb", "xgb"):
                if key in payload:
                    return _clip01(float(payload.get(key, 0.0)))
        return fallback_score

    def _build_week5_scan_notification_content(
        self,
        *,
        symbol_list: list[str],
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
        watchlist_sync: dict[str, object],
        runtime_mode: str,
    ) -> str:
        return self._notification_service.build_scan_notification_content(
            symbol_list=symbol_list,
            first_board_candidates=first_board_candidates,
            leaders=leaders,
            anomalies=anomalies,
            empty_signal=empty_signal,
            watchlist_sync=watchlist_sync,
            runtime_mode=runtime_mode,
        )

    def _week5_scan_action_hint(
        self,
        *,
        leaders: list[dict[str, object]],
        first_board_candidates: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        return self._notification_service.week5_scan_action_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )

    def _week5_scan_conclusion_hint(
        self,
        *,
        leaders: list[dict[str, object]],
        first_board_candidates: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        return self._notification_service.week5_scan_conclusion_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )

    def _week5_symbols_by_action(
        self,
        *,
        rows: list[dict[str, object]],
        action: str,
    ) -> list[str]:
        return self._notification_service.week5_symbols_by_action(
            rows=rows,
            action=action,
        )

    def _week5_scan_notification_signature(
        self,
        *,
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        return self._notification_service.week5_scan_notification_signature(
            first_board_candidates=first_board_candidates,
            leaders=leaders,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )

    def latest_week5_scan_report(self) -> dict[str, object] | None:
        return self._state_service.latest_week5_scan_report()

    def week5_scan_history(self, limit: int = 20) -> dict[str, object]:
        return self._state_service.week5_scan_history(limit=limit)

    def week5_signal_pool_live(
        self,
        limit: int = 30,
        force_refresh: bool = False,
    ) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        capped_limit = max(1, min(limit, 100))
        latest = (
            service._last_week5_scan_report
            if isinstance(service._last_week5_scan_report, dict)
            else {}
        )
        signal_pool = latest.get("signal_pool", {}) if isinstance(latest, dict) else {}
        raw_candidates = signal_pool.get("candidates", []) if isinstance(signal_pool, dict) else []
        candidates = [item for item in raw_candidates if isinstance(item, dict)]
        online_top_k = min(5, capped_limit)
        items: list[dict[str, object]] = []
        source_breakdown = {
            "intraday_1m": 0,
            "intraday_5m": 0,
            "daily": 0,
            "unknown": 0,
        }
        max_depth_symbols = (
            max(1, int(service._config.market_depth.max_symbols_per_poll))
            if service._market_depth_provider is not None
            else capped_limit
        )
        selected_candidates = candidates[: min(capped_limit, max_depth_symbols)]
        depth_snapshots = service._fetch_market_depth_snapshots(
            symbols=[
                str(item.get("symbol", "")).strip()
                for item in selected_candidates
                if str(item.get("symbol", "")).strip()
            ],
            scope="signal_pool",
            force_refresh=force_refresh,
        )
        ordered_results: dict[int, dict[str, object]] = {}
        max_workers = min(8, max(1, len(selected_candidates)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map: dict[Future[dict[str, object]], tuple[int, dict[str, object]]] = {}
            for index, candidate in enumerate(selected_candidates):
                symbol = str(candidate.get("symbol", "")).strip()
                if not symbol:
                    continue
                future = executor.submit(
                    service._build_week5_signal_pool_live_item,
                    symbol=symbol,
                    candidate=candidate,
                    force_refresh=force_refresh,
                    prefer_online=index < online_top_k,
                    depth_snapshot=depth_snapshots.get(symbol, {}),
                )
                future_map[future] = (index, candidate)
            for future in as_completed(future_map):
                index, candidate = future_map[future]
                try:
                    ordered_results[index] = future.result()
                except Exception:
                    ordered_results[index] = service._build_week5_signal_pool_fallback_item(
                        candidate=candidate,
                    )

        for index in sorted(ordered_results):
            item = ordered_results[index]
            source = str(item.get("trend_source", "")).strip().lower()
            if source == "1m":
                source_breakdown["intraday_1m"] += 1
            elif source == "5m":
                source_breakdown["intraday_5m"] += 1
            elif source == "daily":
                source_breakdown["daily"] += 1
            else:
                source_breakdown["unknown"] += 1
            items.append(item)

        return {
            "generated_at": datetime.now().isoformat(),
            "records": len(items),
            "limit": capped_limit,
            "report_timestamp": str(latest.get("timestamp", "")),
            "items": items,
            "source_breakdown": source_breakdown,
            "depth_enabled": service._market_depth_provider is not None,
            "depth_scope": "signal_pool",
        }

    def week5_signal_pool_symbol_live(
        self,
        *,
        symbol: str,
        force_refresh: bool = False,
    ) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        normalized_symbol = _normalize_a_share_symbol(symbol) or symbol.strip()
        if not normalized_symbol:
            return {"status": "empty_symbol"}

        # PLAN §4: bounded on-demand sync for off-shortlist queries.
        # Clear cache + re-check after sync; fail-closed on stale/BJ/lock.
        try:
            from stock_analyzer.data.trading_calendar import is_bj_symbol  # noqa: WPS433

            _is_bj = is_bj_symbol(normalized_symbol)
        except Exception:
            _is_bj = normalized_symbol.startswith("920") or normalized_symbol.startswith(("8", "4"))
        if _is_bj:
            return {
                "status": "unsupported_market",
                "symbol": normalized_symbol,
                "reason": "bj_unsupported_for_minute_features",
            }
        # Light shortlist is prefilter.shortlisted (light 50) / fresh eligible
        # set, not the final 5 candidates.  Using signal_pool.candidates would
        # misclassify a light-50 symbol that did not make final 5 as "off-
        # shortlist" and trigger duplicate fetches.
        latest = (
            service._last_week5_scan_report
            if isinstance(service._last_week5_scan_report, dict)
            else {}
        )
        prefilter = latest.get("prefilter", {}) if isinstance(latest, dict) else {}
        shortlisted = prefilter.get("shortlisted", []) if isinstance(prefilter, dict) else []
        # Fresh eligible symbols are the authoritative 50 minus BJ/stale.
        fresh_symbols = set()
        try:
            freshness = (
                prefilter.get("intraday_freshness", {}) if isinstance(prefilter, dict) else {}
            )
            if isinstance(freshness, dict):
                fresh_symbols = set(
                    str(s).strip() for s in freshness.get("fresh_symbols", []) if str(s).strip()
                )
        except Exception:
            pass
        light_symbols = set()
        if isinstance(shortlisted, list):
            for item in shortlisted:
                if isinstance(item, dict):
                    code = str(item.get("symbol", "")).strip()
                    if code:
                        light_symbols.add(code)
                elif isinstance(item, str) and item.strip():
                    light_symbols.add(item.strip())
        # Union of light 50 and fresh eligible is the true shortlist domain.
        in_shortlist = bool(
            normalized_symbol in light_symbols or normalized_symbol in fresh_symbols
        )
        if not in_shortlist:
            # Bounded on-demand sync (respect intraday_single_ticker_budget_sec)
            budget_sec = max(
                1, int(getattr(service._config.week5, "intraday_single_ticker_budget_sec", 5))
            )
            required_date = None
            try:
                from stock_analyzer.data.trading_calendar import (
                    resolve_required_intraday_date,  # noqa: WPS433
                )

                manifest_date = ""
                try:
                    from stock_analyzer.feature.snapshot import (
                        load_feature_snapshot,  # noqa: WPS433
                    )

                    manifest, _ = load_feature_snapshot(service._config)
                    if manifest is not None:
                        manifest_date = str(manifest.trade_date).strip()
                except Exception:
                    pass
                if manifest_date:
                    cal_source = _resolve_calendar_provider(service)
                    required_date = resolve_required_intraday_date(manifest_date, cal_source)
            except Exception:
                pass
            if required_date is not None:
                try:
                    from stock_analyzer.data.intraday_sync import (
                        sync_intraday_symbols,  # noqa: WPS433
                    )

                    warehouse = None
                    vendor_overlay = None
                    try:
                        warehouse = service._market_warehouse()
                    except Exception:
                        pass
                    try:
                        vendor_overlay = getattr(service, "_provider", None)
                    except Exception:
                        pass
                    sync_report = sync_intraday_symbols(
                        warehouse=warehouse,
                        symbols=[normalized_symbol],
                        required_trade_date=required_date,
                        primary=str(
                            getattr(service._config.week5, "intraday_sync_primary", "sina")
                        ),
                        fallback=str(
                            getattr(service._config.week5, "intraday_sync_fallback", "sina")
                        ),
                        deadline_sec=budget_sec,
                        vendor_overlay=vendor_overlay,
                        tushare_provider=(
                            cal_source
                            if callable(getattr(cal_source, "fetch_minute_bars", None))
                            else None
                        ),
                    )
                    # After sync: clear cache and re-check completeness
                    for src in (vendor_overlay, warehouse):
                        try:
                            fn = getattr(src, "clear_cache", None)
                            if callable(fn):
                                fn()
                        except Exception:
                            pass
                    if sync_report.failed > 0 or sync_report.stale_symbols:
                        return {
                            "status": "intraday_stale",
                            "symbol": normalized_symbol,
                            "required_trade_date": sync_report.target_trade_date,
                            "reason": "intraday_sync_failed_or_stale",
                            "sync_report": sync_report.to_dict(),
                        }
                    if sync_report.session_incomplete:
                        return {
                            "status": "intraday_stale",
                            "symbol": normalized_symbol,
                            "required_trade_date": sync_report.target_trade_date,
                            "reason": "session_incomplete",
                            "sync_report": sync_report.to_dict(),
                        }
                    detail = sync_report.detail
                    if detail.get("lock_busy"):
                        return {
                            "status": "intraday_stale",
                            "symbol": normalized_symbol,
                            "reason": "intraday_sync_lock_busy",
                            "sync_report": sync_report.to_dict(),
                        }
                except Exception as exc:  # pragma: no cover
                    return {
                        "status": "intraday_stale",
                        "symbol": normalized_symbol,
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
        # Re-resolve signal_pool candidates for the final item build
        # (light shortlist was used for the gate above; the market payload
        # still comes from the final signal_pool live item).
        signal_pool = latest.get("signal_pool", {}) if isinstance(latest, dict) else {}
        signal_candidates = (
            signal_pool.get("candidates", []) if isinstance(signal_pool, dict) else []
        )
        candidate = next(
            (
                item
                for item in signal_candidates
                if isinstance(item, dict)
                and str(item.get("symbol", "")).strip() == normalized_symbol
            ),
            {"symbol": normalized_symbol, "score": 0.0, "action": "watch", "reasons": []},
        )
        item = service._build_week5_signal_pool_live_item(
            symbol=normalized_symbol,
            candidate=candidate,
            force_refresh=force_refresh,
            prefer_online=True,
            depth_snapshot=service._fetch_market_depth_snapshots(
                symbols=[normalized_symbol],
                scope=(
                    "watchlist" if normalized_symbol in service._state.watchlist else "signal_pool"
                ),
                force_refresh=force_refresh,
            ).get(normalized_symbol, {}),
        )
        return {
            "generated_at": datetime.now().isoformat(),
            "item": item,
        }

    def _build_week5_signal_pool_live_item(
        self,
        *,
        symbol: str,
        candidate: dict[str, object],
        force_refresh: bool,
        prefer_online: bool,
        depth_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._service
        cache_key = f"week5:signal_pool_live:{symbol}"
        market_payload: dict[str, object] | None = None
        if not force_refresh:
            cached = service._cache.get(cache_key)
            if cached:
                try:
                    raw = json.loads(cached)
                except json.JSONDecodeError:
                    raw = None
                if isinstance(raw, dict):
                    market_payload = raw
        if market_payload is None:
            market_payload = service._build_week5_symbol_market_payload(
                symbol=symbol,
                prefer_online=prefer_online,
                depth_snapshot=depth_snapshot,
            )
            service._cache.set(
                cache_key,
                json.dumps(market_payload, ensure_ascii=False, sort_keys=True),
                ttl_sec=8,
            )

        reasons = _string_list(candidate.get("reasons", []))
        trend_points = _number_list(market_payload.get("trend_points", []))
        bid_levels = _dict_list(market_payload.get("bid_levels", []))
        ask_levels = _dict_list(market_payload.get("ask_levels", []))
        suggested_position = _as_float(candidate.get("suggested_position"), default=0.0)
        isolated = bool(candidate.get("isolated", False))
        action = str(candidate.get("action", "")).strip().lower()
        execution_risk = candidate.get("execution_risk")
        display_name = (
            _clean_display_text(market_payload.get("name"))
            or _clean_display_text(candidate.get("name"))
            or service._resolve_symbol_display_name(symbol)
        )
        return {
            "symbol": symbol,
            "name": display_name,
            "score": round(_as_float(candidate.get("score"), default=0.0), 2),
            "leader_score": round(_as_float(candidate.get("leader_score"), default=0.0), 2),
            "shortlist_score": round(_as_float(candidate.get("shortlist_score"), default=0.0), 2),
            "execution_probability": round(
                _as_float(candidate.get("execution_probability"), default=0.0),
                6,
            ),
            "execution_aware_score": round(
                _as_float(candidate.get("execution_aware_score"), default=0.0),
                6,
            ),
            "execution_reranked_score": round(
                _as_float(
                    candidate.get("execution_reranked_score"),
                    default=_as_float(candidate.get("shortlist_score"), default=0.0),
                ),
                2,
            ),
            "execution_rerank_applied": bool(candidate.get("execution_rerank_applied", False)),
            "execution_rerank_reason": str(candidate.get("execution_rerank_reason", "")).strip(),
            "execution_high_risk": bool(candidate.get("execution_high_risk", False)),
            "execution_risk": (dict(execution_risk) if isinstance(execution_risk, Mapping) else {}),
            "action": action,
            "action_label": _week5_candidate_action_zh(
                action=action,
                suggested_position=suggested_position,
                isolated=isolated,
            ),
            "suggested_position": round(suggested_position, 4),
            "isolated": isolated,
            "isolation_reason": str(candidate.get("isolation_reason", "")).strip(),
            "board_stage": str(candidate.get("board_stage", "")).strip(),
            "reasons": reasons[:8],
            "reason_summary": _format_signal_reasons_zh(reasons, max_items=4),
            "last_price": round(_as_float(market_payload.get("last_price"), default=0.0), 3),
            "prev_close": round(_as_float(market_payload.get("prev_close"), default=0.0), 3),
            "change_pct": round(_as_float(market_payload.get("change_pct"), default=0.0), 6),
            "change_amount": round(_as_float(market_payload.get("change_amount"), default=0.0), 3),
            "day_high": round(_as_float(market_payload.get("day_high"), default=0.0), 3),
            "day_low": round(_as_float(market_payload.get("day_low"), default=0.0), 3),
            "open_price": round(_as_float(market_payload.get("open_price"), default=0.0), 3),
            "volume": _as_float(market_payload.get("volume"), default=0.0),
            "turnover": _as_float(market_payload.get("turnover"), default=0.0),
            "latest_time": str(market_payload.get("latest_time", "")).strip(),
            "trend_source": str(market_payload.get("trend_source", "")).strip(),
            "trend_label": str(market_payload.get("trend_label", "")).strip(),
            "trend_points": [round(value, 4) for value in trend_points],
            "trend_change_pct": round(
                _as_float(market_payload.get("trend_change_pct"), default=0.0),
                6,
            ),
            "depth_available": bool(market_payload.get("depth_available", False)),
            "depth_source": str(market_payload.get("depth_source", "")).strip(),
            "depth_timestamp": str(market_payload.get("depth_timestamp", "")).strip(),
            "spread": round(_as_float(market_payload.get("spread"), default=0.0), 4),
            "spread_pct": round(_as_float(market_payload.get("spread_pct"), default=0.0), 6),
            "order_imbalance": round(
                _as_float(market_payload.get("order_imbalance"), default=0.0),
                6,
            ),
            "bid_total_volume": _as_float(market_payload.get("bid_total_volume"), default=0.0),
            "ask_total_volume": _as_float(market_payload.get("ask_total_volume"), default=0.0),
            "bid_levels": [
                {
                    "level": _as_int(item.get("level"), default=0),
                    "price": round(_as_float(item.get("price"), default=0.0), 4),
                    "volume": round(_as_float(item.get("volume"), default=0.0), 2),
                }
                for item in bid_levels
            ],
            "ask_levels": [
                {
                    "level": _as_int(item.get("level"), default=0),
                    "price": round(_as_float(item.get("price"), default=0.0), 4),
                    "volume": round(_as_float(item.get("volume"), default=0.0), 2),
                }
                for item in ask_levels
            ],
        }

    def _build_week5_signal_pool_fallback_item(
        self,
        *,
        candidate: dict[str, object],
    ) -> dict[str, object]:
        symbol = str(candidate.get("symbol", "")).strip()
        reasons = _string_list(candidate.get("reasons", []))
        action = str(candidate.get("action", "")).strip().lower()
        suggested_position = _as_float(candidate.get("suggested_position"), default=0.0)
        execution_risk = candidate.get("execution_risk")
        return {
            "symbol": symbol,
            "name": (
                _clean_display_text(candidate.get("name"))
                or self._service._resolve_symbol_display_name(symbol)
            ),
            "score": round(_as_float(candidate.get("score"), default=0.0), 2),
            "leader_score": round(_as_float(candidate.get("leader_score"), default=0.0), 2),
            "shortlist_score": round(_as_float(candidate.get("shortlist_score"), default=0.0), 2),
            "execution_probability": round(
                _as_float(candidate.get("execution_probability"), default=0.0),
                6,
            ),
            "execution_aware_score": round(
                _as_float(candidate.get("execution_aware_score"), default=0.0),
                6,
            ),
            "execution_reranked_score": round(
                _as_float(
                    candidate.get("execution_reranked_score"),
                    default=_as_float(candidate.get("shortlist_score"), default=0.0),
                ),
                2,
            ),
            "execution_rerank_applied": bool(candidate.get("execution_rerank_applied", False)),
            "execution_rerank_reason": str(candidate.get("execution_rerank_reason", "")).strip(),
            "execution_high_risk": bool(candidate.get("execution_high_risk", False)),
            "execution_risk": (dict(execution_risk) if isinstance(execution_risk, Mapping) else {}),
            "action": action,
            "action_label": _week5_candidate_action_zh(
                action=action,
                suggested_position=suggested_position,
                isolated=bool(candidate.get("isolated", False)),
            ),
            "suggested_position": round(suggested_position, 4),
            "isolated": bool(candidate.get("isolated", False)),
            "isolation_reason": str(candidate.get("isolation_reason", "")).strip(),
            "board_stage": str(candidate.get("board_stage", "")).strip(),
            "reasons": reasons[:8],
            "reason_summary": _format_signal_reasons_zh(reasons, max_items=4),
            "last_price": 0.0,
            "prev_close": 0.0,
            "change_pct": 0.0,
            "change_amount": 0.0,
            "day_high": 0.0,
            "day_low": 0.0,
            "open_price": 0.0,
            "volume": 0.0,
            "turnover": 0.0,
            "latest_time": "",
            "trend_source": "unknown",
            "trend_label": "market_data_unavailable",
            "trend_points": [],
            "trend_change_pct": 0.0,
            "depth_available": False,
            "depth_source": "",
            "depth_timestamp": "",
            "spread": 0.0,
            "spread_pct": 0.0,
            "order_imbalance": 0.0,
            "bid_total_volume": 0.0,
            "ask_total_volume": 0.0,
            "bid_levels": [],
            "ask_levels": [],
        }

    def _build_week5_symbol_market_payload(
        self,
        *,
        symbol: str,
        prefer_online: bool,
        depth_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._service
        try:
            data_provider = service._select_provider(use_live_runtime=prefer_online)
            daily_bars = data_provider.fetch_daily_bars(symbol=symbol, lookback_days=40)
        except Exception:
            daily_bars = pd.DataFrame()

        if daily_bars.empty:
            return {
                "name": service._resolve_symbol_display_name(symbol),
                "last_price": 0.0,
                "prev_close": 0.0,
                "change_pct": 0.0,
                "change_amount": 0.0,
                "day_high": 0.0,
                "day_low": 0.0,
                "open_price": 0.0,
                "volume": 0.0,
                "turnover": 0.0,
                "latest_time": "",
                "trend_source": "unknown",
                "trend_label": "market_data_unavailable",
                "trend_points": [],
                "trend_change_pct": 0.0,
                "depth_available": False,
                "depth_source": "",
                "depth_timestamp": "",
                "spread": 0.0,
                "spread_pct": 0.0,
                "order_imbalance": 0.0,
                "bid_total_volume": 0.0,
                "ask_total_volume": 0.0,
                "bid_levels": [],
                "ask_levels": [],
            }

        working = daily_bars.sort_index().copy()
        daily_dates = list(working.index)
        latest_daily = working.iloc[-1]
        latest_name = _clean_display_text(latest_daily.get("name")) or _latest_name_from_bars(
            working
        )
        daily_close_series = [
            _as_float(value, default=0.0)
            for value in working.get("close", pd.Series(dtype=float)).tolist()
        ]
        prev_close = (
            daily_close_series[-2] if len(daily_close_series) >= 2 else daily_close_series[-1]
        )
        last_price = daily_close_series[-1]
        latest_time = ""
        trend_points = _compress_series(daily_close_series[-30:], max_points=30)
        trend_source = "daily"
        trend_label = "daily_30d_trend"
        day_high = _as_float(latest_daily.get("high"), default=last_price)
        day_low = _as_float(latest_daily.get("low"), default=last_price)
        open_price = _as_float(latest_daily.get("open"), default=last_price)
        volume = _as_float(latest_daily.get("volume"), default=0.0)
        turnover = _as_float(latest_daily.get("turnover"), default=0.0)

        intraday_frame, intraday_interval, intraday_origin = service._load_week5_intraday_frame(
            symbol=symbol,
            prefer_online=prefer_online,
        )
        if not intraday_frame.empty:
            intraday_index = pd.DatetimeIndex(intraday_frame.index)
            normalized_intraday_sessions = intraday_index.normalize()
            latest_intraday_session = normalized_intraday_sessions.max()
            latest_intraday_date = pd.Timestamp(latest_intraday_session).date()
            session = intraday_frame.loc[
                normalized_intraday_sessions == latest_intraday_session
            ].copy()
            if not session.empty:
                last_price = _as_float(session["close"].iloc[-1], default=last_price)
                latest_time = session.index[-1].isoformat()
                day_high = _as_float(session["high"].max(), default=day_high)
                day_low = _as_float(session["low"].min(), default=day_low)
                open_price = _as_float(session["open"].iloc[0], default=open_price)
                volume = _as_float(session["volume"].sum(), default=volume)
                turnover = _as_float(
                    session.get("amount", pd.Series(dtype=float)).sum(),
                    default=turnover,
                )
                trend_points = _compress_series(
                    [
                        _as_float(value, default=0.0)
                        for value in session.get("close", pd.Series(dtype=float)).tolist()
                    ],
                    max_points=60 if intraday_interval == "1m" else 48,
                )
                trend_source = intraday_interval
                if intraday_interval == "1m":
                    trend_label = (
                        "intraday_1m" if intraday_origin == "local" else "intraday_1m_online"
                    )
                else:
                    trend_label = (
                        "intraday_5m" if intraday_origin == "local" else "intraday_5m_online"
                    )
                latest_daily_date = daily_dates[-1].date() if daily_dates else latest_intraday_date
                if latest_intraday_date > latest_daily_date:
                    prev_close = _as_float(latest_daily.get("close"), default=prev_close)
                elif latest_intraday_date == latest_daily_date and len(daily_close_series) >= 2:
                    prev_close = daily_close_series[-2]
                else:
                    prev_close = _as_float(latest_daily.get("close"), default=prev_close)

        change_amount = last_price - prev_close
        change_pct = change_amount / prev_close if prev_close > 0 else 0.0
        trend_change_pct = 0.0
        if len(trend_points) >= 2 and trend_points[0] > 0:
            trend_change_pct = trend_points[-1] / trend_points[0] - 1.0

        depth_payload = depth_snapshot if isinstance(depth_snapshot, dict) else {}
        if bool(depth_payload.get("available", False)):
            if not latest_name:
                latest_name = _clean_display_text(depth_payload.get("name"))
            if not latest_time:
                latest_time = str(depth_payload.get("timestamp", "")).strip()

        if not latest_name:
            latest_name = service._resolve_symbol_display_name(symbol)

        bid_levels = _dict_list(depth_payload.get("bid_levels", []))
        ask_levels = _dict_list(depth_payload.get("ask_levels", []))

        return {
            "name": latest_name,
            "last_price": round(last_price, 4),
            "prev_close": round(prev_close, 4),
            "change_pct": round(change_pct, 6),
            "change_amount": round(change_amount, 4),
            "day_high": round(day_high, 4),
            "day_low": round(day_low, 4),
            "open_price": round(open_price, 4),
            "volume": round(volume, 2),
            "turnover": round(turnover, 2),
            "latest_time": latest_time,
            "trend_source": trend_source,
            "trend_label": trend_label,
            "trend_origin": intraday_origin if not intraday_frame.empty else "daily",
            "trend_points": trend_points,
            "trend_change_pct": round(trend_change_pct, 6),
            "depth_available": bool(depth_payload.get("available", False)),
            "depth_source": str(depth_payload.get("source", "")).strip(),
            "depth_timestamp": str(depth_payload.get("timestamp", "")).strip(),
            "spread": round(_as_float(depth_payload.get("spread"), default=0.0), 4),
            "spread_pct": round(_as_float(depth_payload.get("spread_pct"), default=0.0), 6),
            "order_imbalance": round(_as_float(depth_payload.get("imbalance"), default=0.0), 6),
            "bid_total_volume": round(
                _as_float(depth_payload.get("bid_total_volume"), default=0.0),
                2,
            ),
            "ask_total_volume": round(
                _as_float(depth_payload.get("ask_total_volume"), default=0.0),
                2,
            ),
            "bid_levels": [dict(item) for item in bid_levels],
            "ask_levels": [dict(item) for item in ask_levels],
        }

    def _load_week5_intraday_frame(
        self,
        *,
        symbol: str,
        prefer_online: bool,
    ) -> tuple[pd.DataFrame, str, str]:
        service = self._service
        today = datetime.now().date()
        fallback_frame = pd.DataFrame()
        fallback_interval = ""
        try:
            vipdoc_root = service._resolve_tdx_sync_vipdoc_root()
        except TdxSyncError:
            vipdoc_root = None
        for interval in ("1m", "5m"):
            frame = pd.DataFrame()
            if vipdoc_root is not None:
                try:
                    frame = read_tdx_minute_bars(
                        vipdoc_root=vipdoc_root,
                        symbol=symbol,
                        interval=interval,
                    )
                except Exception:
                    frame = pd.DataFrame()
            if not frame.empty:
                latest_date = frame.index.max().date()
                if latest_date >= today:
                    return frame, interval, "local"
                if fallback_frame.empty:
                    fallback_frame = frame
                    fallback_interval = interval
            if not prefer_online:
                continue
            try:
                online_frame = fetch_sina_minute_bars(
                    symbol=symbol,
                    interval=interval,
                    timeout_sec=3,
                )
            except Exception:
                online_frame = pd.DataFrame()
            if not online_frame.empty:
                return online_frame, interval, "online"
        if not fallback_frame.empty:
            return fallback_frame, fallback_interval, "local"
        return pd.DataFrame(), "", ""

    def _derive_watchlist_candidates_from_week5(
        self,
        report: dict[str, object],
        top_k_override: int | None = None,
    ) -> list[str]:
        return self._state_service.derive_watchlist_candidates_from_week5(
            report=report,
            top_k_override=top_k_override,
        )

    def _auto_sync_watchlist_from_week5_report(
        self,
        report: dict[str, object],
        reason: str,
        top_k_override: int | None = None,
        allow_signal_pool_fallback: bool = True,
    ) -> dict[str, object]:
        return self._state_service.auto_sync_watchlist_from_week5_report(
            report=report,
            reason=reason,
            top_k_override=top_k_override,
            allow_signal_pool_fallback=allow_signal_pool_fallback,
        )

    def _store_week5_scan_report(self, report: dict[str, object]) -> None:
        self._state_service.store_week5_scan_report(report)

    def _is_intraday_scheduler_week5_scan(
        self,
        *,
        now: datetime,
        sync_reason: str,
    ) -> bool:
        if now.weekday() >= 5:
            return False
        if not sync_reason.strip().lower().startswith("scheduler_week5"):
            return False
        windows = list(self._service._config.week5.first_board_windows)
        return _is_within_hhmm_windows(now=now, windows=windows)


class Week5ScanProgress:
    """Week5 扫描进度文件（原子 JSON 更新）。

    字段固定为：trace_id / status / phase / started_at / updated_at /
    elapsed_ms / completed / total / current_symbol / scan_profile /
    funnel_policy；failed 终态追加受控 error_summary。
    更新为事件驱动（阶段推进、单股开始/完成），不是周期心跳——单股长时间
    卡住时 updated_at 不会刷新，current_symbol 标识当前处理对象。
    任何写入失败都静默忽略——进度文件只是可观测性，不得影响扫描语义。
    """

    def __init__(self, *, service: Any, scan_profile: str) -> None:
        raw_path = str(service._config.week5.scan_progress_path).strip()  # noqa: SLF001
        self._path = (
            Path(raw_path) if raw_path else Path("artifacts/runtime/week5_scan_progress.json")
        )
        self._scan_profile = scan_profile
        self._started_at = datetime.now(UTC)
        self._status = "running"
        self._phase = "quality"
        self._completed = 0
        self._total = 0
        self._current_symbol = ""
        self._trace_id = ""
        self._funnel_policy = ""
        self._error_summary = ""
        self._write()

    def update(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_symbol: str | None = None,
        trace_id: str | None = None,
        funnel_policy: str | None = None,
    ) -> None:
        if phase is not None:
            self._phase = phase
        if status is not None:
            self._status = status
        if completed is not None:
            self._completed = completed
        if total is not None:
            self._total = total
        if current_symbol is not None:
            self._current_symbol = current_symbol
        if trace_id is not None:
            self._trace_id = trace_id
        if funnel_policy is not None:
            self._funnel_policy = funnel_policy
        self._write()

    def fail(self, *, error: Exception) -> None:
        self._status = "failed"
        self._error_summary = f"{type(error).__name__}: {str(error)[:300]}"
        self._write()

    def _write(self) -> None:
        try:
            updated_at = datetime.now(UTC)
            payload: dict[str, object] = {
                "trace_id": self._trace_id,
                "status": self._status,
                "phase": self._phase,
                "started_at": self._started_at.isoformat(),
                "updated_at": updated_at.isoformat(),
                "elapsed_ms": int((updated_at - self._started_at).total_seconds() * 1000),
                "completed": self._completed,
                "total": self._total,
                "current_symbol": self._current_symbol,
                "scan_profile": self._scan_profile,
                "funnel_policy": self._funnel_policy,
            }
            if self._status == "failed":
                payload["error_summary"] = self._error_summary
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f"{self._path.name}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except Exception:
            pass


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _clip01(value: float) -> float:
    return cast(float, _runtime_service_module()._clip01(value))


def _normalize_a_share_symbol(value: object) -> str:
    return cast(str, _runtime_service_module()._normalize_a_share_symbol(value))


def _string_list(value: object) -> list[str]:
    return cast(list[str], _runtime_service_module()._string_list(value))


def _extract_learning_snapshot_id(source: object) -> str:
    return cast(str, _runtime_service_module()._extract_learning_snapshot_id(source))


def _is_at_or_after_hhmm(*, now: datetime, raw_hhmm: str, default_hhmm: str) -> bool:
    candidate = raw_hhmm.strip() or default_hhmm
    parts = candidate.split(":")
    if len(parts) != 2:
        parts = default_hhmm.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        default_parts = default_hhmm.split(":")
        hour = int(default_parts[0])
        minute = int(default_parts[1])
    current_total = now.hour * 60 + now.minute
    trigger_total = hour * 60 + minute
    return current_total >= trigger_total


def _is_within_hhmm_windows(*, now: datetime, windows: list[str]) -> bool:
    current_total = now.hour * 60 + now.minute
    for item in windows:
        raw_window = str(item).strip()
        if not raw_window:
            continue
        start_end = raw_window.split("@", maxsplit=1)[0]
        if "-" not in start_end:
            continue
        start_raw, end_raw = start_end.split("-", maxsplit=1)
        start_total = _hhmm_to_total_minutes(start_raw)
        end_total = _hhmm_to_total_minutes(end_raw)
        if start_total is None or end_total is None:
            continue
        if start_total <= current_total <= end_total:
            return True
    return False


def _hhmm_to_total_minutes(value: str) -> int | None:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _compress_series(values: list[float], max_points: int) -> list[float]:
    return cast(list[float], _runtime_service_module()._compress_series(values, max_points))


def _format_signal_reasons_zh(reasons: list[str], max_items: int = 3) -> str:
    return cast(str, _runtime_service_module()._format_signal_reasons_zh(reasons, max_items))


def _week5_candidate_action_zh(
    *,
    action: str,
    suggested_position: float,
    isolated: bool,
) -> str:
    return cast(
        str,
        _runtime_service_module()._week5_candidate_action_zh(
            action=action,
            suggested_position=suggested_position,
            isolated=isolated,
        ),
    )


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    return cast(list[str], _runtime_service_module()._dedupe_preserve_order(items))


def _resolve_positive_int(value: object, *, fallback: int) -> int:
    candidate = _as_int(value, default=fallback)
    if candidate > 0:
        return candidate
    return max(1, fallback)


def _push_title(priority: str, category: str, summary: str) -> str:
    return cast(str, _runtime_service_module()._push_title(priority, category, summary))


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]


def _bars_from_post_scan_enrichment(enrich: str) -> pd.DataFrame | None:
    """反序列化 post_scan_enrichment JSON 为 bars DataFrame。

    返回的行序与序列化时一致（时间正序，末行为最新）；任一解析失败返回
    None，由调用方回退 live_provider 拉取，绝不抛错影响扫描语义。
    """
    if not enrich:
        return None
    try:
        rows = json.loads(enrich)
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    try:
        frame = pd.DataFrame(rows)
    except Exception:
        return None
    if frame.empty:
        return None
    for column in ("open", "high", "low", "close", "turnover"):
        if column not in frame.columns:
            return None
    return frame


def _number_list(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    return [float(item) for item in value if isinstance(item, (int, float))]


def _latest_bar_dict(bars: pd.DataFrame) -> dict[str, object]:
    """bars 末行 → dict（P1 overextension evaluator 输入，symbol 一并携带）。"""
    ordered = bars if bars.index.is_monotonic_increasing else bars.sort_index()
    if ordered.empty:
        return {}
    row = ordered.iloc[-1]
    result: dict[str, object] = {}
    for column in ordered.columns:
        value = row.get(column)
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        result[str(column)] = value
    return result


def _overextension_decision_dict(
    row: dict[str, object],
    config: object,
) -> dict[str, object]:
    """evaluate_overextension → 可序列化 dict（写入扫描审计）。"""
    try:
        decision = evaluate_overextension(row=row, config=config)  # type: ignore[arg-type]
    except Exception:
        return {
            "level": "none",
            "penalty": 0.0,
            "reject_new_buy": False,
            "reasons": [],
            "metrics": {},
        }
    return {
        "level": decision.level,
        "penalty": decision.penalty,
        "reject_new_buy": decision.reject_new_buy,
        "reasons": list(decision.reasons),
        "metrics": dict(decision.metrics),
    }


def _board_decision_dict(
    bars: pd.DataFrame,
    *,
    symbol: str,
    config: object,
    limit_rule: object,
) -> dict[str, object]:
    """evaluate_board_risk → 可序列化 dict（写入扫描审计）。"""
    try:
        decision = evaluate_board_risk(
            bars=bars,
            symbol=symbol,
            limit_rule=limit_rule,  # type: ignore[arg-type]
            board_risk_config=config,  # type: ignore[arg-type]
        )
    except Exception:
        return {
            "consecutive_limit_up": 0,
            "current_limit_state": "none",
            "board": "",
            "reject_new_buy": False,
            "reasons": [],
        }
    return board_decision_to_dict(decision)


def _safe_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _market_radar_review_sort_key(item: dict[str, object]) -> tuple[datetime, str]:
    return (
        _safe_datetime(item.get("timestamp")) or datetime.min,
        str(item.get("symbol", "")),
    )


def _latest_name_from_bars(bars: pd.DataFrame) -> str:
    if not isinstance(bars, pd.DataFrame) or bars.empty:
        return ""
    for column in ("name", "stock_name", "symbol_name"):
        if column not in bars.columns:
            continue
        for value in reversed(bars[column].tolist()):
            candidate = _clean_display_text(value)
            if candidate:
                return candidate
    return ""


def _clean_display_text(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    text = str(value).strip()
    if not text:
        return ""
    return "" if text.lower() in {"nan", "null", "none", "undefined"} else text


def _market_radar_anomaly_type_zh(value: str) -> str:
    mapping = {
        "gap": "跳空",
        "volume_spike": "放量",
        "upper_shadow": "上影偏长",
        "lower_shadow": "下影偏长",
        "data_source_error": "数据源异常",
        "insufficient_history": "历史不足",
    }
    normalized = value.strip().lower()
    return mapping.get(normalized, value or "异常")


def _market_radar_reason_code_zh(value: str) -> str:
    mapping = {
        "trend_above_ma60": "站上60日线",
        "ret60_positive": "60日趋势为正",
        "capital_flow_support": "资金流支持",
        "price_volume_support": "价量共振",
        "liquidity_ok": "流动性达标",
        "risk_penalty_high": "风险惩罚偏高",
        "financial_data_partial": "财务数据缺口",
        "background_data_partial": "背景数据缺口",
        "trend": "趋势优势",
        "capital_flow": "资金流优势",
        "price_volume": "价量优势",
        "liquidity": "流动性优势",
        "baseline": "基础筛选",
    }
    normalized = value.strip().lower()
    return mapping.get(normalized, value or "基础筛选")


# ---------------------------------------------------------------------------
# PLAN Section 3: intraday freshness helpers (per-symbol freshness + deep frame)
# ---------------------------------------------------------------------------


def _is_bj_symbol(symbol: str) -> bool:
    """Single source of truth: delegates to trading_calendar.is_bj_symbol."""
    try:
        from stock_analyzer.data.trading_calendar import is_bj_symbol as _is_bj

        return bool(_is_bj(symbol))
    except Exception:
        text = str(symbol).strip().upper()
        if text.endswith(".BJ"):
            return True
        code = "".join(ch for ch in text if ch.isdigit())
        if len(code) != 6:
            return False
        if code.startswith("920"):
            return True
        if code.startswith(("4", "8")):
            return True
        return False


def _resolve_service_tushare_provider(
    service: object,
    *,
    timeout_sec: float,
) -> object | None:
    """Build one bounded Tushare provider shared by calendar and minute sync."""
    cached = getattr(service, "_week5_tushare_provider", None)
    if cached is not None:
        return cached
    config = getattr(service, "_config", None)
    market_config = getattr(config, "market_warehouse", None)
    token = str(getattr(market_config, "tushare_token", "") or "").strip()
    if not token:
        for key in ("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN", "TUSHARE_TOKEN", "TS_TOKEN"):
            token = str(os.environ.get(key, "") or "").strip()
            if token:
                break
    if not token:
        return None
    configured_timeout = float(getattr(market_config, "online_socket_timeout_sec", timeout_sec))
    request_interval = float(getattr(market_config, "request_interval_sec", 0.35))
    try:
        from stock_analyzer.data.tushare_provider import TushareProvider  # noqa: WPS433

        provider = TushareProvider(
            token=token,
            max_attempts=1,
            socket_timeout_sec=max(0.1, min(float(timeout_sec), configured_timeout)),
            retry_delay_sec=max(0.0, request_interval),
            min_request_interval_sec=max(0.0, request_interval),
        )
        try:
            service._week5_tushare_provider = provider
        except Exception:
            pass
        return provider
    except Exception:
        return None


def _resolve_calendar_provider(service: object) -> object | None:
    """Resolve a real exchange calendar from the graph or configured Tushare."""
    try:
        graph_fn = getattr(service, "_iter_market_data_provider_graph", None)
        if callable(graph_fn):
            for provider in graph_fn():
                if callable(getattr(provider, "list_open_trade_dates", None)):
                    return provider
    except Exception:
        pass
    config = getattr(service, "_config", None)
    week5_config = getattr(config, "week5", None)
    timeout_sec = float(getattr(week5_config, "intraday_sync_timeout_sec", 5))
    return _resolve_service_tushare_provider(service, timeout_sec=timeout_sec)


def _normalize_intraday_symbol(value: object) -> str:
    """Normalize SH/SZ/BJ symbols without dropping the 920 BJ prefix."""
    normalized = _normalize_a_share_symbol(value)
    if normalized:
        return normalized
    text = str(value or "").strip().upper()
    primary = text.split(".", maxsplit=1)[0]
    digits = "".join(ch for ch in primary if ch.isdigit())
    if len(digits) == 6 and _is_bj_symbol(text):
        return digits
    return ""


def _prepare_intraday_sync_symbols(
    *,
    light_symbols: list[str],
    pinned_symbols: list[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Normalize light/pinned symbols and build the bounded sync target union."""
    normalized_light = _dedupe_preserve_order(
        [
            symbol
            for symbol in (_normalize_intraday_symbol(item) for item in light_symbols)
            if symbol
        ]
    )
    normalized_pinned = _dedupe_preserve_order(
        [
            symbol
            for symbol in (_normalize_intraday_symbol(item) for item in pinned_symbols)
            if symbol
        ]
    )
    eligible = [symbol for symbol in normalized_light if not _is_bj_symbol(symbol)]
    pinned_candidates = [
        symbol for symbol in normalized_pinned if not _is_bj_symbol(symbol)
    ]
    unsupported_market = _dedupe_preserve_order(
        [
            symbol
            for symbol in [*normalized_light, *normalized_pinned]
            if _is_bj_symbol(symbol)
        ]
    )
    sync_targets = _dedupe_preserve_order([*eligible, *pinned_candidates])
    return eligible, pinned_candidates, unsupported_market, sync_targets


def _resolve_pinned_symbols_after_freshness(
    *,
    prefilter_report: dict[str, object],
    pinned_symbols: list[str],
) -> list[str]:
    """Return only pinned symbols authorized by the available freshness result."""
    if "_fresh_pinned_symbols" in prefilter_report:
        authoritative = prefilter_report.get("_fresh_pinned_symbols")
        if not isinstance(authoritative, list):
            return []
        return _dedupe_preserve_order(
            [
                symbol
                for symbol in (
                    _normalize_intraday_symbol(item) for item in authoritative
                )
                if symbol and not _is_bj_symbol(symbol)
            ]
        )

    candidates = _dedupe_preserve_order(
        [
            symbol
            for symbol in (_normalize_intraday_symbol(item) for item in pinned_symbols)
            if symbol and not _is_bj_symbol(symbol)
        ]
    )
    freshness = prefilter_report.get("intraday_freshness")
    if isinstance(freshness, dict):
        raw_fresh = freshness.get("fresh_symbols", [])
        fresh_set = {
            normalized
            for normalized in (
                _normalize_intraday_symbol(item)
                for item in raw_fresh
                if str(item).strip()
            )
            if normalized
        }
        return [symbol for symbol in candidates if symbol in fresh_set]
    return candidates


def _resolve_required_intraday_date(
    snapshot_trade_date: str,
    provider: object | None,
) -> object | None:
    """Resolve previous open trading date before snapshot date.

    Uses A股交易日历 via ``provider.list_open_trade_dates`` with 20-day
    window when available; else falls back to weekday loop.  Never uses
    ``np.busday_count`` alone.
    """
    if not str(snapshot_trade_date).strip():
        return None
    try:
        from stock_analyzer.data.trading_calendar import resolve_required_intraday_date
    except Exception:
        return None
    try:
        return resolve_required_intraday_date(snapshot_trade_date, provider)
    except Exception:
        return None


def _build_fresh_deep_frame(
    *,
    provider: object,
    warehouse: object | None,
    vendor_overlay: object | None,
    symbols: list[str],
    required_date: object,
    lookback_days: int = 250,
) -> dict[str, object]:
    """Build fresh deep frame rows with daily+intraday for eligible symbols.

    For each symbol, fetches daily bars (lookback) and intraday summaries
    (merged via warehouse/vendor_overlay) for the required date, then calls
    ``engineer.transform(bars, intraday_1m=..., intraday_5m=...)`` to produce
    one feature row.  The resulting DataFrame is used for deep ranking instead
    of the stale snapshot_frame.

    Returns ``{"frame": DataFrame, "failed": [symbols]}``.

    Light stage remains daily-only; this helper is deep-only.
    """
    from stock_analyzer.feature.engineer import FeatureEngineer

    engineer = FeatureEngineer()
    rows: list[pd.DataFrame] = []
    failed: list[str] = []
    for symbol in symbols:
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)  # type: ignore[union-attr]
        except Exception:
            failed.append(symbol)
            continue
        if not isinstance(bars, pd.DataFrame) or bars.empty:
            failed.append(symbol)
            continue
        # Fetch intraday summaries for the required date (tolerate missing)
        intraday_1m: pd.DataFrame | None = None
        intraday_5m: pd.DataFrame | None = None
        for interval, _target in (("1m", "intraday_1m"), ("5m", "intraday_5m")):
            frame: pd.DataFrame | None = None
            for src in (vendor_overlay, warehouse):
                if src is None:
                    continue
                fn = getattr(src, "fetch_intraday_summary", None)
                if not callable(fn):
                    continue
                try:
                    fetched = fn(symbol=symbol, interval=interval, lookback_days=10)
                except Exception:
                    continue
                if isinstance(fetched, pd.DataFrame) and not fetched.empty:
                    frame = fetched
                    break
            if interval == "1m":
                intraday_1m = frame
            else:
                intraday_5m = frame
        try:
            features = engineer.transform(bars, intraday_1m=intraday_1m, intraday_5m=intraday_5m)
        except Exception:
            failed.append(symbol)
            continue
        if features is None or features.empty:
            failed.append(symbol)
            continue
        row_feat = features.iloc[-1]
        payload: dict[str, object] = {"symbol": symbol, "trade_date": str(bars.index[-1].date())}
        for k, v in row_feat.to_dict().items():
            try:
                payload[str(k)] = float(v)  # type: ignore[arg-type]
            except Exception:
                payload[str(k)] = v
        rows.append(pd.DataFrame([payload]))
    frame = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return {"frame": frame, "failed": failed}


def _artifact_inference_blocked_detail(
    *,
    predictor: ExecutionRiskPredictor,
    exc: Exception,
) -> str:
    """提取推理被阻断的精确原因（如 legacy_no_scaler），供审计与 reason 使用。

    LogisticProbModel 反序列化时会把缺失 scaler 等 legacy 缺陷记入
    ``inference_blocked_reason``；优先读该字段，取不到再退回异常类名，
    避免只报 ``ValueError`` 无法区分缺陷类型。
    """
    for model in predictor.models.values():
        blocked_reason = str(getattr(model, "inference_blocked_reason", "") or "").strip()
        if blocked_reason:
            return blocked_reason
    return exc.__class__.__name__


def _record_intraday_sync_health_audits(
    *,
    service: Any,
    sync_report: Mapping[str, object],
) -> None:
    """按同步报告记录分钟源健康度审计事件。

    注意 ``source_breakdown`` 在会话完整性过滤之前计数，因此
    sina=100 / ok=96 / failed=4 属正常形态；降级判定必须用
    ``ok>0 且 tushare==0 且 sina>=ok``，不能用 ``sina==ok``。
    """
    detail = sync_report.get("detail")
    if not isinstance(detail, Mapping):
        # detail 缺失（异常兜底 dict）时无从判断主源，直接跳过避免误报。
        return
    # 单点归一化：三个来源（数据层/wrapper 兜底/调用层兜底）的大小写
    # 处理不一致，统一在消费端 lower，避免非小写配置漏匹配降级审计。
    primary = str(detail.get("primary", "")).strip().lower()
    if not primary:
        return
    sources_raw = sync_report.get("source_breakdown")
    sources = sources_raw if isinstance(sources_raw, Mapping) else {}
    payload = {
        "primary": primary,
        "fallback": str(detail.get("fallback", "")).strip().lower(),
        "ok": _as_int(sync_report.get("ok"), default=0),
        "failed": _as_int(sync_report.get("failed"), default=0),
        "source_breakdown": {
            "tushare": _as_int(sources.get("tushare"), default=0),
            "sina": _as_int(sources.get("sina"), default=0),
            "skipped": _as_int(sources.get("skipped"), default=0),
        },
        "capability_probe": sync_report.get("capability_probe")
        if isinstance(sync_report.get("capability_probe"), dict)
        else {},
    }
    ok_count = _as_int(sync_report.get("ok"), default=0)
    failed_count = _as_int(sync_report.get("failed"), default=0)
    tushare_count = _as_int(sources.get("tushare"), default=0)
    sina_count = _as_int(sources.get("sina"), default=0)
    if (
        primary == "tushare"
        and ok_count > 0
        and tushare_count == 0
        and sina_count >= ok_count
    ):
        service._record_audit_event(
            event_type="intraday_primary_degraded_to_fallback",
            level="warn",
            payload=payload,
        )
    elif ok_count == 0 and failed_count > 0:
        service._record_audit_event(
            event_type="intraday_sync_all_failed",
            level="warn",
            payload=payload,
        )


def _sync_intraday_symbols(
    *,
    warehouse: object | None,
    vendor_overlay: object | None,
    symbols: list[str],
    required_trade_date: object,
    primary: str = "tushare",
    fallback: str = "sina",
    concurrency: int = 4,
    timeout_sec: int = 5,
    deadline_sec: int = 180,
    tushare_provider: object | None = None,
) -> dict[str, object]:
    """Unified minute sync (PLAN §2) with Sina fallback and shared lock.

    Delegates to :func:`stock_analyzer.data.intraday_sync.sync_intraday_symbols`
    which handles probe / circuit-breaker / session completeness / 1m→5m
    session-aware resample / upsert_intraday_summaries / cache clearing.
    """
    try:
        from stock_analyzer.data.intraday_sync import sync_intraday_symbols  # noqa: WPS433

        report = sync_intraday_symbols(
            warehouse=warehouse,
            symbols=symbols,
            required_trade_date=required_trade_date,  # type: ignore[arg-type]
            primary=primary,
            fallback=fallback,
            deadline_sec=deadline_sec,
            concurrency=concurrency,
            timeout_sec=timeout_sec,
            vendor_overlay=vendor_overlay,
            tushare_provider=tushare_provider,
        )
        return report.to_dict()
    except Exception as exc:  # pragma: no cover - defensive surface
        return {
            "symbols_total": len([s for s in symbols if str(s).strip()]),
            "ok": 0,
            "failed": len([s for s in symbols if str(s).strip()]),
            "skipped": 0,
            "failed_symbols": [str(s).strip() for s in symbols if str(s).strip()][:20],
            "error": f"{type(exc).__name__}:{exc}",
            # 异常兜底也保留主备源，让健康度审计可判定 all_failed。
            "detail": {
                "primary": str(primary or "").strip().lower(),
                "fallback": str(fallback or "").strip().lower(),
            },
        }


def _build_intraday_freshness_blocked_report(
    *,
    now: datetime,
    scan_profile: str,
    freshness_report: dict[str, object],
    funnel_policy: str = "snapshot_funnel",
) -> dict[str, object]:
    """Fail-closed blocked report for intraday_freshness_insufficient."""
    watchlist_size = int(freshness_report.get("fresh_count", 0))
    return {
        "timestamp": now.isoformat(),
        "trace_id": "",
        "status": "blocked_data_gate",
        "watchlist_size": watchlist_size,
        "symbol_source": "blocked",
        "scan_profile": scan_profile.strip() or "default",
        "funnel_policy": funnel_policy,
        "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
        "signal_pool": {"candidate_count": 0, "candidates": []},
        "anomalies": {"event_count": 0, "events": []},
        "empty_signal": {
            "triggered": True,
            "reasons": ["intraday_freshness_insufficient"],
            "no_buy_streak": 0,
            "buy_signals": 0,
            "drawdown_pct": 0.0,
            "risk_action": "blocked",
        },
        "monster_isolation": {
            "can_open_new_position": False,
            "reasons": ["intraday_freshness_insufficient"],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
        "summary": {
            "first_board_candidates": 0,
            "leaders": 0,
            "anomalies": 0,
            "empty_signal_triggered": True,
            "can_open_monster": False,
            "watchlist_synced": False,
        },
        "data_gate": {
            "status": "blocked",
            "reasons": ["intraday_freshness_insufficient"],
            "data_snapshot_id": "",
            "snapshot_current": False,
        },
        "intraday_freshness": freshness_report,
    }
