"""Week5 intraday scan and live signal-pool workflows extracted from the runtime service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from time import perf_counter
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
from stock_analyzer.feature.snapshot import (
    load_feature_snapshot,
    snapshot_is_current,
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
            risk_reasons = [
                str(reason) for reason in reason_values if str(reason).strip()
            ]
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

        quality_selector_enabled = bool(
            service._config.week5.universe_quality_selector_enabled
        )
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
            prefilter.get("universe_quality_selection")
            if isinstance(prefilter, dict)
            else None
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
        ranking = (
            signal_pool.get("ranking")
            if isinstance(signal_pool, dict)
            else {}
        )
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
                _string_list(stage1.get("reason_codes", []))
                if isinstance(stage1, dict)
                else []
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
                        _latest_name_from_bars(bars)
                        or service._resolve_symbol_display_name(symbol)
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
        normalized_metadata = {
            str(key): value
            for key, value in metadata.items()
            if str(key).strip()
        } if isinstance(metadata, Mapping) else {}
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
            {
                anomaly
                for anomaly in (_string_list(item.get("anomaly_types", []))[:6])
                if anomaly
            }
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
        service: object,
        now: datetime,
        reasons: list[str],
        data_snapshot_id: str,
        snapshot_current: bool,
        scan_profile: str = "",
    ) -> dict[str, object]:
        """Fail-closed week5 scan report when the data gate is blocked."""
        watchlist_size = len(service._state.watchlist)
        return {
            "timestamp": now.isoformat(),
            "trace_id": "",
            "status": "blocked_data_gate",
            "watchlist_size": watchlist_size,
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
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
        recovery_mode: bool = False,
    ) -> dict[str, object]:
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
        try:
            report = self._run_week5_scan_impl(
                symbols=symbols,
                timestamp=timestamp,
                notify_enabled=notify_enabled,
                sync_watchlist=sync_watchlist,
                sync_reason=sync_reason,
                sync_top_k_override=sync_top_k_override,
                force_universe_scan=force_universe_scan,
                prefilter_enabled_override=prefilter_enabled_override,
                prefilter_top_k_override=prefilter_top_k_override,
                universe_max_symbols_override=universe_max_symbols_override,
                pinned_symbols=pinned_symbols,
                scan_profile=scan_profile,
                recovery_mode=recovery_mode,
                _progress=progress,
            )
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

    def _run_week5_scan_impl(
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
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
        recovery_mode: bool = False,
        _progress: Week5ScanProgress | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        # 阶段耗时计数器必须在任何阶段执行之前初始化，否则会覆盖
        # quality_selection / light_stage 已记录的耗时。
        quality_selection_ms = 0
        light_stage_ms = 0
        deep_stage_ms = 0
        snapshot_manifest = None
        snapshot_frame = None
        snapshot_current = False
        if bool(service._config.week5.feature_snapshot_enabled):
            try:
                snapshot_manifest, snapshot_frame = load_feature_snapshot(service._config)
                if snapshot_manifest is not None and snapshot_frame is not None:
                    snapshot_current = snapshot_is_current(
                        snapshot_manifest, service._config, now
                    )
            except Exception:
                snapshot_manifest = None
                snapshot_frame = None
                snapshot_current = False
        snapshot_mode = bool(
            snapshot_manifest is not None
            and snapshot_frame is not None
            and snapshot_current
        )
        data_gate = service._build_data_gate(
            snapshot_manifest=snapshot_manifest,
            snapshot_current=snapshot_current,
            latest_trade_date=str(snapshot_manifest.trade_date) if snapshot_manifest else "",
            now=now,
        )
        gate_status = str(data_gate.get("status", "ok"))
        intraday_scheduler_mode = self._is_intraday_scheduler_week5_scan(
            now=now,
            sync_reason=sync_reason,
        )
        prefilter_enabled = (
            bool(prefilter_enabled_override)
            if prefilter_enabled_override is not None
            else bool(service._config.week5.universe_prefilter_enabled)
        )
        configured_prefilter_top_k = max(
            1,
            _resolve_positive_int(
                prefilter_top_k_override,
                fallback=_as_int(service._config.week5.universe_prefilter_top_k, default=500),
            ),
        )
        symbol_source = "watchlist"
        prefilter_report: dict[str, object] = {
            "enabled": prefilter_enabled,
            "applied": False,
            "lookback_days": max(
                120,
                _as_int(service._config.week5.universe_prefilter_lookback_days, default=240),
            ),
            "top_k": configured_prefilter_top_k,
            "shortlist_top_n": max(
                1,
                _as_int(service._config.week5.universe_prefilter_shortlist_top_n, default=50),
            ),
            "universe_count": 0,
            "eligible_count": 0,
            "shortlisted_count": 0,
            "scoring_mode": "two_stage_funnel",
            "symbols": [],
            "shortlisted": [],
            "preview": [],
            "pinned_count": 0,
            "pinned_symbols": [],
            "reason": "not_requested",
            "stages": {
                "stage1": {
                    "applied": False,
                    "status": "not_run",
                    "score_key": "baseline_score",
                    "input_count": 0,
                    "eligible_count": 0,
                    "advanced_count": 0,
                    "weights": {
                        "trend": 0.40,
                        "capital_flow": 0.25,
                        "price_volume": 0.15,
                        "liquidity": 0.10,
                        "risk_penalty": 0.10,
                    },
                    "preview": [],
                },
                "stage2": {
                    "applied": False,
                    "status": "not_run",
                    "score_key": "shortlist_score",
                    "shortlist_top_n": max(
                        1,
                        _as_int(
                            service._config.week5.universe_prefilter_shortlist_top_n,
                            default=50,
                        ),
                    ),
                    "input_count": 0,
                    "advanced_count": 0,
                    "weights": {
                        "signal": 0.35,
                        "capital_flow": 0.25,
                        "trend": 0.15,
                        "price_volume": 0.15,
                        "execution_liquidity": 0.10,
                        "risk_penalty": 0.10,
                    },
                    "preview": [],
                },
            },
        }
        prefilter_details_by_symbol: dict[str, dict[str, object]] = {}
        prefer_local_universe = service._prefer_local_symbol_universe()
        should_scan_universe = bool(force_universe_scan)
        # Locals preserved across the prefilter_report reassignment below so the
        # quality-selector audit report and universe errors are not lost when
        # _prefilter_week5_universe_symbols returns a fresh dict.
        quality_selection_report: dict[str, object] | None = None
        universe_errors_list: list[str] | None = None

        if symbols is not None and not force_universe_scan:
            raw_symbols = symbols
            symbol_source = "manual_input"
            prefilter_report["reason"] = "manual_symbols"
        else:
            raw_symbols = list(service._state.watchlist)
            if not raw_symbols and intraday_scheduler_mode and not force_universe_scan:
                raw_symbols = self._state_service.latest_preserved_watchlist_symbols(
                    top_k_override=sync_top_k_override,
                )
                if raw_symbols:
                    symbol_source = "intraday_preserved_watchlist"
                    prefilter_report["reason"] = "intraday_preserve_existing"
            if force_universe_scan or (not raw_symbols and not intraday_scheduler_mode):
                quality_selector_enabled = bool(
                    service._config.week5.universe_quality_selector_enabled
                )
                if quality_selector_enabled:
                    # Full universe first — never truncate via _quota_sample_universe
                    # before the quality selector runs.
                    universe = service._resolve_symbol_universe(
                        max_symbols=0,
                        allow_seed_fallback=True,
                        allow_online_sources=not prefer_local_universe,
                    )
                    full_universe_symbols = _string_list(universe.get("symbols", []))
                    quality_target = _resolve_positive_int(
                        universe_max_symbols_override,
                        fallback=_as_int(
                            service._config.week5.universe_quality_target_size,
                            default=300,
                        ),
                    )
                    quality_trade_date = service._resolve_universe_seed_trade_date()
                    quality_ruleset_id = str(
                        service._config.evolution.universe_spec.universe_ruleset_id
                    )
                    quality_board_scope = list(
                        service._config.evolution.universe_spec.board_scope
                    )
                    selection_started = perf_counter()
                    selection = service._select_universe_quality_candidates(
                        symbols=full_universe_symbols,
                        target_size=quality_target,
                        trade_date=quality_trade_date,
                        reference_date=now.date().isoformat(),
                        ruleset_id=quality_ruleset_id,
                        board_scope=quality_board_scope,
                    )
                    # 已执行阶段取整后至少为 1ms：极快阶段向下取整会得到 0，
                    # 与"未执行=0"的观测口径冲突，也会让测试出现 flaky。
                    quality_selection_ms = max(
                        1, int((perf_counter() - selection_started) * 1000)
                    )
                    selection_report = selection.get("report", {})
                    if not isinstance(selection_report, dict):
                        selection_report = {}
                    raw_symbols = _string_list(selection.get("selected", []))
                    selector_mode = str(
                        selection_report.get("selector_mode", "")
                    ).strip()
                    symbol_source = (
                        str(universe.get("source", "universe")) + ":quality_selector"
                    )
                    should_scan_universe = True
                    prefilter_report["reason"] = "universe_scan"
                    prefilter_report["universe_source"] = symbol_source
                    prefilter_report["universe_quality_selection"] = selection_report
                    prefilter_report["universe_quality_selector_mode"] = selector_mode
                    quality_selection_report = selection_report
                    board_quotas = selection_report.get("board_quotas")
                    universe_board_quota = {
                        "truncation_mode": "quality_ranked_board_floor",
                        "cap": quality_target,
                        "effective_cap": max(
                            0,
                            _as_int(
                                selection_report.get("selected_count"),
                                default=len(raw_symbols),
                            ),
                        ),
                        "board_scope": quality_board_scope,
                        "boards": board_quotas if isinstance(board_quotas, dict) else {},
                        "seed_trade_date": quality_trade_date,
                        "ruleset_id": quality_ruleset_id,
                        "selector_mode": selector_mode,
                    }
                    prefilter_report["universe_board_quota"] = universe_board_quota
                    raw_errors = universe.get("errors", [])
                    if isinstance(raw_errors, list):
                        universe_errors_list = [
                            str(item).strip() for item in raw_errors if str(item).strip()
                        ][:20]
                        prefilter_report["universe_errors"] = universe_errors_list
                else:
                    universe_max_symbols = (
                        max(0, _as_int(universe_max_symbols_override, default=0))
                        if universe_max_symbols_override is not None
                        else (0 if prefer_local_universe else 1200)
                    )
                    universe = service._resolve_symbol_universe(
                        max_symbols=universe_max_symbols,
                        allow_seed_fallback=True,
                        allow_online_sources=not prefer_local_universe,
                    )
                    raw_symbols = _string_list(universe.get("symbols", []))
                    symbol_source = str(universe.get("source", "universe"))
                    should_scan_universe = True
                    prefilter_report["reason"] = "universe_scan"
                    prefilter_report["universe_source"] = symbol_source
                    universe_board_quota = universe.get("board_quota")
                    if isinstance(universe_board_quota, dict):
                        prefilter_report["universe_board_quota"] = universe_board_quota
                    raw_errors = universe.get("errors", [])
                    if isinstance(raw_errors, list):
                        universe_errors_list = [
                            str(item).strip() for item in raw_errors if str(item).strip()
                        ][:20]
                        prefilter_report["universe_errors"] = universe_errors_list
            else:
                if prefilter_report.get("reason") == "not_requested":
                    prefilter_report["reason"] = "existing_watchlist"

        symbol_list = [str(item).strip() for item in raw_symbols if str(item).strip()]
        # Feature snapshot is ensured AFTER the quality selection resolved the
        # actual candidate set: the snapshot is built (or incrementally
        # refreshed) for exactly those candidates, then the snapshot state and
        # the data gate are re-evaluated so a fresh build is consumed by the
        # light/deep stages.  On build failure the snapshot state is reset to
        # missing and the gate keeps the scheduler path fail-closed.
        feature_snapshot_report: dict[str, object] | None = None
        snapshot_ensure_ms = 0
        if _progress is not None:
            _progress.update(phase="snapshot", total=len(symbol_list))
        if (
            should_scan_universe
            and symbol_list
            and bool(service._config.week5.feature_snapshot_enabled)
        ):
            snapshot_ensure_started = perf_counter()
            feature_snapshot_report = service.ensure_week5_feature_snapshot(
                symbols=symbol_list,
                scope="universe_quality",
            )
            snapshot_ensure_ms = max(
                1, int((perf_counter() - snapshot_ensure_started) * 1000)
            )
            if not bool(feature_snapshot_report.get("ok", False)):
                snapshot_manifest = None
                snapshot_frame = None
                snapshot_current = False
            else:
                try:
                    snapshot_manifest, snapshot_frame = load_feature_snapshot(
                        service._config
                    )
                    snapshot_current = bool(
                        snapshot_manifest is not None
                        and snapshot_frame is not None
                        and snapshot_is_current(snapshot_manifest, service._config, now)
                    )
                except Exception:
                    snapshot_manifest = None
                    snapshot_frame = None
                    snapshot_current = False
            snapshot_mode = bool(
                snapshot_manifest is not None
                and snapshot_frame is not None
                and snapshot_current
            )
            data_gate = service._build_data_gate(
                snapshot_manifest=snapshot_manifest,
                snapshot_current=snapshot_current,
                latest_trade_date=str(snapshot_manifest.trade_date)
                if snapshot_manifest
                else "",
                now=now,
            )
            gate_status = str(data_gate.get("status", "ok"))

        # 三类选择策略（Week5 扫描漏斗整改）：
        # - snapshot_funnel：全市场扫描（含 offhours_forced_full_deep），强制
        #   执行 light/deep，deep 空结果 fail-closed；
        # - intentional_full_deep：Friday/weekend 显式全量扫描，raw 候选 ->
        #   monster_scan_max_symbols，有意绕过漏斗；
        # - direct_non_universe：非全市场或不适用快照的既有直接扫描路径。
        scan_profile_name = scan_profile.strip() or "default"
        if scan_profile_name in ("offhours_friday_full_deep", "offhours_weekend_full_deep"):
            funnel_policy = "intentional_full_deep"
        elif should_scan_universe:
            funnel_policy = "snapshot_funnel"
        else:
            funnel_policy = "direct_non_universe"
        if _progress is not None:
            _progress.update(funnel_policy=funnel_policy)
        if _progress is not None:
            _progress.update(phase="light")

        if should_scan_universe and symbol_list and prefilter_enabled:
            if snapshot_mode:
                light_started = perf_counter()
                light_target = max(
                    1, int(service._config.week5.light_candidate_target)
                )
                allowed_exchanges_for_light = {
                    str(item).strip().upper()
                    for item in service._config.evolution.universe_spec.board_scope
                    if str(item).strip()
                }
                prefilter_report = service._light_stage_from_snapshot(
                    frame=snapshot_frame,
                    target=light_target,
                    allowed_exchanges=allowed_exchanges_for_light,
                )
                light_stage_ms = max(
                    1, int((perf_counter() - light_started) * 1000)
                )
            else:
                # 自动调度触发：scheduler_* 前缀（intraday/nightly）或
                # offhours_refresh 全市场漏斗（evolution 自动 offhours 链路）。
                # 手动恢复（CLI/API 自定义 sync_reason）不在此列，保留绕过能力。
                auto_scheduled_scan = bool(
                    sync_reason.strip().lower().startswith("scheduler_")
                    or (
                        sync_reason.strip().lower() == "offhours_refresh"
                        and funnel_policy == "snapshot_funnel"
                    )
                )
                if gate_status == "blocked" and auto_scheduled_scan:
                    # Fail-closed: the scheduled universe scan (including the
                    # evolution offhours refresh) must NOT silently fall back to
                    # the heavy direct-scan path (top_k=500 full market) when
                    # the feature snapshot is missing/stale.  Manual recovery
                    # runs override this by passing an explicit non-scheduler
                    # sync reason.
                    gate_reasons = [
                        str(item) for item in (data_gate.get("reasons") or [])
                    ]
                    blocked_payload = self._build_gate_blocked_report(
                        service=service,
                        now=now,
                        reasons=gate_reasons,
                        data_snapshot_id=str(snapshot_manifest.data_snapshot_id)
                        if snapshot_manifest
                        else "",
                        snapshot_current=snapshot_current,
                        scan_profile=scan_profile_name,
                    )
                    if feature_snapshot_report is not None:
                        blocked_payload["feature_snapshot"] = feature_snapshot_report
                    blocked_payload["funnel_policy"] = funnel_policy
                    self._state_service.store_week5_scan_report(blocked_payload)
                    service._record_audit_event(
                        event_type="week5_scan_blocked_data_gate",
                        level="warn",
                        payload={"reasons": gate_reasons},
                    )
                    return blocked_payload
                prefilter_report = service._prefilter_week5_universe_symbols(
                    symbols=symbol_list,
                    top_k_override=configured_prefilter_top_k,
                )
            prefilter_report["reason"] = "universe_scan"
            prefilter_report["universe_source"] = symbol_source
            if isinstance(universe_board_quota, dict):
                prefilter_report["universe_board_quota"] = universe_board_quota
            # Re-apply quality-selector audit report + universe errors that were
            # captured before this reassignment (prefilter returns a fresh dict).
            if quality_selection_report is not None:
                prefilter_report["universe_quality_selection"] = quality_selection_report
                prefilter_report["universe_quality_selector_mode"] = str(
                    quality_selection_report.get("selector_mode", "")
                ).strip()
            if universe_errors_list is not None:
                prefilter_report["universe_errors"] = universe_errors_list
            raw_shortlisted = prefilter_report.get("shortlisted", [])
            if isinstance(raw_shortlisted, list):
                prefilter_details_by_symbol = {
                    normalized: item
                    for item in raw_shortlisted
                    if isinstance(item, dict)
                    for normalized in [_normalize_a_share_symbol(item.get("symbol"))]
                    if normalized
                }
            prefilter_symbols = _string_list(prefilter_report.get("symbols", []))
            if not prefilter_symbols:
                prefilter_symbols = [
                    str(item.get("symbol", "")).strip()
                    for item in raw_shortlisted
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ]
            symbol_list = prefilter_symbols
            symbol_source = f"{symbol_source}:prefilter"

        deep_report: dict[str, object] = {}
        deep_stage_ran = False
        deep_symbols: list[str] = []
        # 立即固化 deep 入选项数量：symbol_list 后续会追加 pinned / 被 cap
        # 重建（引用别名），deep_selected_count 必须保持 deep 阶段原始口径。
        deep_selected_count = 0
        if _progress is not None:
            _progress.update(phase="deep")
        if snapshot_mode and funnel_policy != "intentional_full_deep":
            deep_stage_ran = True
            deep_started = perf_counter()
            deep_report = service._deep_stage_from_snapshot(
                frame=snapshot_frame,
                target=max(1, int(service._config.week5.deep_candidate_target)),
                light_report=prefilter_report,
            )
            deep_stage_ms = max(
                1, int((perf_counter() - deep_started) * 1000)
            )
            deep_symbols = [
                str(item.get("symbol", "")).strip()
                for item in deep_report.get("selected", [])
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ]
            deep_selected_count = len(deep_symbols)
            if deep_symbols:
                symbol_list = deep_symbols
                symbol_source = f"{symbol_source}:snapshot_deep"
            elif funnel_policy == "snapshot_funnel":
                # fail-closed：deep 无入选项时清空 raw 候选，仅允许随后追加
                # pinned；绝不回退至质量选择原始候选或 cap 后的截断列表。
                symbol_list = []
                symbol_source = f"{symbol_source}:snapshot_deep_empty_fail_closed"
            prefilter_report["deep_stage"] = deep_report

        # deep 空结果归因：light 为空 / 快照无匹配行 / deep 筛选为空 三态区分，
        # 与"deep 未执行"（intentional_full_deep）不可混淆。
        deep_empty_reason = ""
        if deep_stage_ran and not deep_symbols:
            if _as_int(deep_report.get("light_shortlist_count"), default=0) <= 0:
                deep_empty_reason = "light_shortlist_empty"
            elif _as_int(deep_report.get("snapshot_match_rows"), default=0) <= 0:
                deep_empty_reason = "no_snapshot_matching_rows"
            else:
                deep_empty_reason = "deep_selected_empty"

        quality_selector_mode = (
            str(quality_selection_report.get("selector_mode", "")).strip()
            if isinstance(quality_selection_report, dict)
            else ""
        )
        degraded_fail_closed = bool(
            should_scan_universe
            and not bool(prefilter_report.get("applied", False))
            and quality_selector_mode == "degraded_fallback"
        )
        if degraded_fail_closed:
            symbol_list = []
            prefilter_report["degraded_fail_closed"] = True
            prefilter_report["degraded_fail_closed_reason"] = (
                "quality_unavailable_without_snapshot"
            )

        normalized_pinned_symbols = _dedupe_preserve_order(
            [
                symbol
                for symbol in (_normalize_a_share_symbol(item) for item in pinned_symbols or [])
                if symbol
            ]
        )
        pinned_added_symbols: list[str] = []
        if normalized_pinned_symbols:
            existing_symbols = {
                symbol
                for symbol in (_normalize_a_share_symbol(item) for item in symbol_list)
                if symbol
            }
            pinned_added_symbols = [
                symbol for symbol in normalized_pinned_symbols if symbol not in existing_symbols
            ]
            if pinned_added_symbols:
                symbol_list.extend(pinned_added_symbols)
                symbol_source = f"{symbol_source}:pinned"
        prefilter_report["pinned_symbols"] = list(normalized_pinned_symbols)
        prefilter_report["pinned_count"] = len(pinned_added_symbols)
        prefilter_report["selected_count"] = len(symbol_list)
        original_monster_scan_count = len(symbol_list)
        monster_scan_cap = (
            max(
                1,
                _as_int(
                    service._config.week5.monster_scan_intraday_max_symbols,
                    default=_as_int(service._config.week5.live_runtime_max_symbols, default=15),
                ),
            )
            if intraday_scheduler_mode
            else max(0, _as_int(service._config.week5.monster_scan_max_symbols, default=120))
        )
        monster_scan_cap_applied = bool(
            monster_scan_cap > 0 and len(symbol_list) > monster_scan_cap
        )
        if degraded_fail_closed:
            ranking_mode = "degraded_fail_closed_pinned_only"
        else:
            ranking_mode = (
                "prefilter_order"
                if bool(prefilter_report.get("applied", False))
                else "input_order"
            )
        if monster_scan_cap_applied:
            pinned_set = set(normalized_pinned_symbols)
            pinned_first = [
                symbol
                for symbol in symbol_list
                if (_normalize_a_share_symbol(symbol) or symbol) in pinned_set
            ]
            non_pinned = [
                symbol
                for symbol in symbol_list
                if (_normalize_a_share_symbol(symbol) or symbol) not in pinned_set
            ]
            if (
                not bool(prefilter_report.get("applied", False))
                and isinstance(quality_selection_report, dict)
            ):
                selected_payload = quality_selection_report.get("selected", [])
                quality_score_by_symbol = {
                    normalized: _as_float(item.get("score"), default=0.0)
                    for item in selected_payload
                    if isinstance(item, dict)
                    for normalized in [_normalize_a_share_symbol(item.get("symbol"))]
                    if normalized
                } if isinstance(selected_payload, list) else {}
                selector_mode = str(
                    quality_selection_report.get("selector_mode", "")
                ).strip()
                if quality_score_by_symbol and selector_mode in {
                    "quality",
                    "quality_all_eligible",
                    "snapshot_fallback",
                }:
                    non_pinned.sort(
                        key=lambda symbol: (
                            -quality_score_by_symbol.get(
                                _normalize_a_share_symbol(symbol) or symbol,
                                0.0,
                            ),
                            _normalize_a_share_symbol(symbol) or symbol,
                        )
                    )
                    ranking_mode = "universe_quality_score"
            symbol_list = _dedupe_preserve_order([*pinned_first, *non_pinned])[:monster_scan_cap]
            symbol_source = f"{symbol_source}:monster_cap"
            prefilter_report["selected_count"] = len(symbol_list)
        # funnel 汇总计数：政策、deep 是否执行/空结果归因、deep/pinned/实际
        # pipeline 输入数。deep_symbols_empty 仅在 deep 真正执行且无入选项时为
        # True——intentional_full_deep 的"deep 未运行"不会被误判为 fail-closed。
        funnel_report: dict[str, object] = {
            "mode": "snapshot" if snapshot_mode else "direct",
            "policy": funnel_policy,
            "deep_stage_ran": deep_stage_ran,
            "deep_symbols_empty": bool(deep_stage_ran and deep_selected_count == 0),
            "deep_empty_reason": deep_empty_reason,
            "deep_selected_count": deep_selected_count,
            "pinned_added_count": len(pinned_added_symbols),
            "pipeline_input_count": len(symbol_list),
        }
        if funnel_policy == "intentional_full_deep":
            selection_source = "intentional_full_deep"
        elif deep_stage_ran and deep_selected_count:
            # deep 有入选项（含既有直接路径在快照可用时的 deep 收窄）。
            selection_source = "snapshot_deep"
        elif funnel_policy == "snapshot_funnel" and deep_stage_ran:
            # deep 执行且无入选项：fail-closed，仅 pinned。
            selection_source = "deep_empty_pinned_only"
        else:
            # deep 未执行（快照不可用等）：既有直接路径来源，不标为 deep 空回退。
            selection_source = "direct_scan"
        # snapshot_funnel 的最终 pipeline 输入上限：min(monster 上限, deep+pinned)。
        # deep 未执行时上限就是 monster_scan_cap（直接路径既有语义）。
        if funnel_policy == "snapshot_funnel" and deep_stage_ran:
            deep_plus_pinned = deep_selected_count + len(pinned_added_symbols)
            effective_input_cap = (
                min(monster_scan_cap, deep_plus_pinned)
                if monster_scan_cap > 0
                else deep_plus_pinned
            )
        else:
            effective_input_cap = monster_scan_cap
        monster_scan_controls: dict[str, object] = {
            "cap": monster_scan_cap,
            "cap_applied": monster_scan_cap_applied,
            "intraday_scheduler_mode": intraday_scheduler_mode,
            "input_count": original_monster_scan_count,
            "selected_count": len(symbol_list),
            "dropped_count": max(0, original_monster_scan_count - len(symbol_list)),
            "ranking_mode": ranking_mode,
            "selection_source": selection_source,
            "effective_input_cap": effective_input_cap,
        }
        prefilter_report["monster_scan_controls"] = monster_scan_controls

        if not symbol_list:
            # snapshot_funnel 的 deep 空回退 fail-closed 是有意的修复行为，
            # 与普通空 watchlist 区分记录，便于现场归因。
            empty_fail_closed = bool(
                funnel_policy == "snapshot_funnel"
                and deep_stage_ran
                and deep_selected_count == 0
            )
            empty_reason = (
                "snapshot_deep_empty_fail_closed" if empty_fail_closed else "empty_watchlist"
            )
            empty_report = {
                "timestamp": now.isoformat(),
                "trace_id": "",
                "watchlist_size": 0,
                "symbol_source": symbol_source,
                "scan_profile": scan_profile.strip() or "default",
                "funnel_policy": funnel_policy,
                "prefilter": prefilter_report,
                "funnel": dict(funnel_report),
                "monster_scan_controls": dict(monster_scan_controls),
                "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
                "signal_pool": {"candidate_count": 0, "candidates": []},
                "anomalies": {"event_count": 0, "events": []},
                "empty_signal": {
                    "triggered": True,
                    "reasons": [empty_reason],
                    "no_buy_streak": 0,
                    "buy_signals": 0,
                    "drawdown_pct": 0.0,
                    "risk_action": "unknown",
                },
                "monster_isolation": {
                    "can_open_new_position": False,
                    "reasons": ["empty_watchlist"],
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
            }
            self._state_service.store_week5_scan_report(empty_report)
            service._record_audit_event(
                event_type="week5_scan",
                level="warn",
                payload={"watchlist_size": 0, "reason": empty_reason},
            )
            return empty_report

        if _progress is not None:
            _progress.update(
                phase="final_pipeline",
                total=len(symbol_list),
                current_symbol="",
            )

        def _pipeline_heartbeat(symbol: str, index: int, total: int, started: bool) -> None:
            # final pipeline 单股开始/完成各更新一次（事件驱动，非周期心跳）：
            # 单只耗时数分钟时通过 current_symbol 指示当前处理股票；若某只
            # 股票长时间卡住，updated_at 不会继续刷新，这是已知语义边界。
            if _progress is not None:
                _progress.update(
                    phase="final_pipeline",
                    completed=index if started else index + 1,
                    total=total,
                    current_symbol=symbol,
                )

        live_provider = service._select_provider(use_live_runtime=True)
        output_mode = str(service._config.week5.week5_output_mode).strip().lower() or "legacy"
        dual_track = output_mode == "dual_track"
        transform_workers = max(
            1,
            int(service._config.week5.final_pipeline_transform_max_workers),
        )
        # P1 双轨：基础扫描仍以 monster 观察池为主（保留现有评分链路），
        # dual_track 时额外跑一次 trend 轨作为稳健候选。特征计算为渐进式双跑，
        # 输出契约（trend_candidates/monster_watchlist/executable）先行落地。
        monster_report = service.run_pipeline(
            symbols=symbol_list,
            strategy="monster",
            current_equity=service._state.current_equity,
            use_live_runtime=True,
            dry_run_execution=True,
            job_name="week5_scan_monster",
            on_symbol_progress=_pipeline_heartbeat,
            transform_max_workers=transform_workers,
            # 捕获每只股票的 bars 尾部快照，first-board/anomaly/overextension
            # 直接复用，不再对每只 symbol 二次 fetch_daily_bars。
            capture_post_scan_enrichment=True,
        )
        trend_report: dict[str, object] | None = None
        trend_pipeline_duration_ms = 0
        if dual_track:
            # 兼容期双跑成本：Shadow 对比期以 trend_pipeline_duration_ms 量化
            # 双 pipeline 开销；满足"基础特征只计算一次"后此字段归零。
            trend_started = perf_counter()
            trend_report = service.run_pipeline(
                symbols=symbol_list,
                strategy="trend",
                current_equity=service._state.current_equity,
                use_live_runtime=True,
                dry_run_execution=True,
                job_name="week5_scan_trend",
                on_symbol_progress=_pipeline_heartbeat,
                transform_max_workers=transform_workers,
                capture_post_scan_enrichment=True,
            )
            trend_pipeline_duration_ms = max(
                1, int((perf_counter() - trend_started) * 1000)
            )
        trace_id = str(monster_report.get("trace_id", ""))
        if _progress is not None:
            _progress.update(trace_id=trace_id)
        monster_runtime_payload: dict[str, object] = {}
        monster_runtime = monster_report.get("runtime")
        if isinstance(monster_runtime, dict):
            monster_runtime_payload = dict(monster_runtime)
        raw_signals = monster_report.get("signals")
        signal_map: dict[str, dict[str, object]] = {}
        min_history_days = max(1, int(service._config.evolution.universe_spec.min_list_days))
        first_board_scan_lookback_days = max(
            40,
            min_history_days,
            int(service._config.evolution.universe_spec.first_board_scan_lookback_days),
        )
        if isinstance(raw_signals, list):
            for item in raw_signals:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    signal_map[symbol] = item
        # dual_track 时 trend 轨 signals 单独成图：最终信号只从 trend 轨产生。
        trend_signal_map: dict[str, dict[str, object]] = {}
        if dual_track and isinstance(trend_report, dict):
            trend_raw = trend_report.get("signals")
            if isinstance(trend_raw, list):
                for item in trend_raw:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip()
                    if symbol:
                        trend_signal_map[symbol] = item

        signal_pool_candidates: list[dict[str, object]] = []
        # dual_track 时最终信号只从 trend 轨产生；monster 轨作为观察池。
        candidate_signal_map = trend_signal_map if dual_track else signal_map
        for symbol, item in candidate_signal_map.items():
            reason_values = _string_list(item.get("reasons", []))
            if any(
                str(reason).strip().startswith("insufficient_history_days:")
                for reason in reason_values
            ):
                continue
            normalized_symbol = _normalize_a_share_symbol(symbol) or symbol
            candidate = service._score_week5_signal_pool_candidate(
                signal=item,
                prefilter_detail=prefilter_details_by_symbol.get(normalized_symbol),
            )
            # P1 过热风险（overextension）+ 板块连板（board_risk）在 final
            # selector 之前计算并注入 signal；final selector 依据
            # reject_new_buy 拒绝 trend 轨新买入（monster 观察池不受影响）。
            bars = _bars_from_post_scan_enrichment(
                str(item.get("post_scan_enrichment", "")).strip()
            )
            overextension_decision: dict[str, object] = {
                "level": "none",
                "penalty": 0.0,
                "reject_new_buy": False,
                "reasons": [],
                "metrics": {},
            }
            board_decision: dict[str, object] = {
                "consecutive_limit_up": 0,
                "current_limit_state": "none",
                "board": "",
                "reject_new_buy": False,
                "reasons": [],
            }
            if bars is not None and not bars.empty:
                overextension_decision = _overextension_decision_dict(
                    row=_latest_bar_dict(bars),
                    config=service._config.overextension,
                )
                board_decision = _board_decision_dict(
                    bars=bars,
                    symbol=symbol,
                    config=service._config.board_risk,
                    limit_rule=service._config.limit_rule,
                )
            candidate["overextension"] = overextension_decision
            candidate["board_risk"] = board_decision
            # trend 轨执行质量门：overextension reject 或三连板 reject 时拒绝新买入
            candidate["reject_new_buy"] = bool(
                overextension_decision.get("reject_new_buy", False)
                or board_decision.get("reject_new_buy", False)
            )
            signal_pool_candidates.append(candidate)
        execution_rerank = self._apply_execution_aware_rerank(
            candidates=signal_pool_candidates,
        )
        ranking_score_key = str(execution_rerank.get("score_key", "shortlist_score")).strip()
        if not ranking_score_key:
            ranking_score_key = "shortlist_score"
        signal_pool_candidates = sorted(
            signal_pool_candidates,
            key=lambda item: (
                -_as_float(
                    item.get(ranking_score_key),
                    default=_as_float(item.get("shortlist_score"), default=0.0),
                ),
                -_as_float(item.get("shortlist_score"), default=0.0),
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )
        shortlist_top_n = max(
            1,
            _as_int(service._config.week5.universe_prefilter_shortlist_top_n, default=50),
        )
        for index, item in enumerate(signal_pool_candidates):
            item["shortlist_rank"] = index + 1
            item["shortlist_selected"] = index < shortlist_top_n

        # The funnel's final selector rejects signals on data-trust problems
        # (degraded/synthetic provider, low data-quality score).  A blocked
        # gate caused solely by a missing/stale snapshot is a
        # performance-contract issue: the scheduler path fail-closes at the
        # universe-scan entry.  Only an EXPLICIT recovery run may proceed
        # against direct bars, and even then it is advisory only — its output
        # never feeds final_signals or the watchlist.
        gate_reasons = [str(item) for item in (data_gate.get("reasons") or [])]
        snapshot_only_blocked = bool(gate_reasons) and all(
            reason.startswith("feature_snapshot") for reason in gate_reasons
        )
        recovery_direct_scan = bool(recovery_mode) and snapshot_only_blocked
        final_selector_gate_status = (
            "ok" if recovery_direct_scan else str(data_gate.get("status", "ok"))
        )
        final_selector = service._final_signal_selector(
            signals=signal_pool_candidates,
            data_gate_status=final_selector_gate_status,
        )
        if recovery_direct_scan:
            # Advisory only: the output is inspected, never treated as a
            # production signal source.
            advisory_signals = list(final_selector.get("final_signals", []))
            for item in advisory_signals:
                if isinstance(item, dict):
                    item["advisory"] = True
            final_selector["advisory_only"] = True
            final_selector["advisory_signals"] = advisory_signals
            final_selector["final_signals"] = []
            final_selector["selected_count"] = 0

        shortlist_preview = [
            {
                "symbol": str(item.get("symbol", "")).strip(),
                "shortlist_score": _as_float(item.get("shortlist_score"), default=0.0),
                "score": _as_float(item.get("score"), default=0.0),
                "shortlist_reasons": _string_list(item.get("shortlist_reasons", []))[:6],
                **(
                    {
                        "execution_reranked_score": _as_float(
                            item.get("execution_reranked_score"),
                            default=_as_float(item.get("shortlist_score"), default=0.0),
                        ),
                        "execution_aware_score": _as_float(
                            item.get("execution_aware_score"),
                            default=0.0,
                        ),
                        "execution_high_risk": bool(item.get("execution_high_risk", False)),
                    }
                    if ranking_score_key == "execution_reranked_score"
                    else {}
                ),
            }
            for item in signal_pool_candidates[: min(shortlist_top_n, 10)]
        ]
        raw_stages = prefilter_report.get("stages")
        if isinstance(raw_stages, dict):
            raw_stage2 = raw_stages.get("stage2")
            if isinstance(raw_stage2, dict):
                raw_stage2.update(
                    {
                        "applied": True,
                        "status": "completed" if signal_pool_candidates else "no_candidates",
                        "score_key": ranking_score_key,
                        "shortlist_top_n": shortlist_top_n,
                        "input_count": len(signal_pool_candidates),
                        "advanced_count": min(shortlist_top_n, len(signal_pool_candidates)),
                        "preview": shortlist_preview,
                        "execution_rerank": dict(execution_rerank),
                    }
                )

        if _progress is not None:
            _progress.update(phase="first_board_anomaly")

        first_board_anomaly_started = perf_counter()
        first_board_candidates: list[dict[str, object]] = []
        anomalies: list[dict[str, object]] = []
        for symbol in symbol_list:
            signal = signal_map.get(symbol, {})
            # 优先复用 pipeline 已捕获的 post_scan_enrichment（bars 尾部快照），
            # 避免对每只 symbol 二次 fetch_daily_bars；字段为空/不可解析时
            # 回退 live_provider 拉取（向后兼容）。
            bars = _bars_from_post_scan_enrichment(
                str(signal.get("post_scan_enrichment", "")).strip()
                if isinstance(signal, dict)
                else ""
            )
            if bars is None:
                try:
                    bars = live_provider.fetch_daily_bars(
                        symbol=symbol,
                        lookback_days=first_board_scan_lookback_days,
                    )
                except Exception as exc:
                    anomalies.append(
                        {
                            "symbol": symbol,
                            "types": ["data_source_error"],
                            "detail": str(exc),
                        }
                    )
                    continue
            if len(bars) < min_history_days:
                anomalies.append(
                    {
                        "symbol": symbol,
                        "types": ["insufficient_history"],
                        "history_days": len(bars),
                        "required_history_days": min_history_days,
                    }
                )
                continue
            if len(bars) < 2:
                continue
            candidate = service._build_first_board_candidate(
                symbol=symbol,
                bars=bars,
                signal=signal,
            )
            if candidate is not None:
                first_board_candidates.append(candidate)

            anomaly = service._detect_symbol_anomaly(
                symbol=symbol,
                bars=bars,
            )
            if anomaly is not None:
                anomalies.append(anomaly)
        first_board_anomaly_ms = max(
            1, int((perf_counter() - first_board_anomaly_started) * 1000)
        )

        empty_signal = service._evaluate_empty_signal(monster_report=monster_report)
        isolation = service._monster_isolation_gate(
            monster_report=monster_report,
            empty_signal=empty_signal,
        )

        max_stock_position = service._config.monster_risk.max_stock_position
        block_all = not isolation["can_open_new_position"]
        for item in first_board_candidates:
            suggested = _as_float(item.get("suggested_position"), default=0.0)
            isolated = block_all or suggested > max_stock_position
            item["isolated"] = isolated
            if suggested > max_stock_position:
                item["isolation_reason"] = f"suggested_position_exceeds_{max_stock_position:.4f}"
            elif block_all:
                isolation_reason_values = isolation.get("reasons", [])
                if isinstance(isolation_reason_values, list):
                    reason_text = ",".join(str(x) for x in isolation_reason_values)
                else:
                    reason_text = ""
                item["isolation_reason"] = reason_text
            else:
                item["isolation_reason"] = ""

        leaders = sorted(
            first_board_candidates,
            key=lambda item: (
                -_as_float(item.get("leader_score"), default=0.0),
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )[:3]

        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "trace_id": trace_id,
            "watchlist_size": len(symbol_list),
            "symbol_source": symbol_source,
            "scan_profile": scan_profile.strip() or "default",
            "emergency_direct_scan": recovery_direct_scan,
            "recovery_mode": bool(recovery_mode),
            "prefilter": prefilter_report,
            "data_snapshot_id": str(snapshot_manifest.data_snapshot_id)
            if snapshot_manifest is not None
            else "",
            "data_gate": dict(data_gate),
            "feature_snapshot": feature_snapshot_report,
            "scan_stages": {
                "quality_selection": {
                    "duration_ms": quality_selection_ms,
                    "selected_count": _as_int(
                        quality_selection_report.get("selected_count", 0),
                        default=0,
                    )
                    if isinstance(quality_selection_report, dict)
                    else 0,
                },
                "snapshot_ensure": {
                    "duration_ms": snapshot_ensure_ms,
                    "enabled": bool(service._config.week5.feature_snapshot_enabled),
                    "requested_count": (
                        _as_int(
                            feature_snapshot_report.get("requested_symbol_count", 0),
                            default=0,
                        )
                        if isinstance(feature_snapshot_report, dict)
                        else 0
                    ),
                    "published_count": (
                        _as_int(
                            feature_snapshot_report.get("published_symbol_count", 0),
                            default=0,
                        )
                        if isinstance(feature_snapshot_report, dict)
                        else 0
                    ),
                    "ok": bool(feature_snapshot_report.get("ok", False))
                    if isinstance(feature_snapshot_report, dict)
                    else False,
                },
                "light_stage": {
                    "duration_ms": light_stage_ms,
                    "mode": str(prefilter_report.get("mode", "")),
                    "shortlisted_count": _as_int(
                        prefilter_report.get("shortlisted_count", 0), default=0
                    ),
                },
                "deep_stage": {
                    "duration_ms": deep_stage_ms,
                    "mode": str(deep_report.get("mode", "")),
                    "selected_count": _as_int(
                        deep_report.get("selected_count", 0), default=0
                    ),
                },
                "final_pipeline": {
                    "duration_ms": _as_int(
                        monster_runtime_payload.get("duration_ms"), default=0
                    ),
                    "symbols": len(symbol_list),
                    **self._final_pipeline_timing_report(monster_runtime_payload),
                },
                "first_board_anomaly": {
                    "duration_ms": first_board_anomaly_ms,
                },
            },
            "funnel": {
                **funnel_report,
                "light_candidate_target": max(
                    1, int(service._config.week5.light_candidate_target)
                ),
                "deep_candidate_target": max(
                    1, int(service._config.week5.deep_candidate_target)
                ),
                "final_signal_cap": max(
                    0, int(service._config.week5.final_signal_cap)
                ),
                "allow_zero_signal": bool(service._config.week5.allow_zero_signal),
                "light_count": _as_int(
                    prefilter_report.get("shortlisted_count", 0), default=0
                ),
                "deep_count": len(symbol_list),
                "final_count": _as_int(
                    final_selector.get("selected_count", 0), default=0
                ),
                "final_selection": dict(final_selector),
            },
            "runtime_source": {
                "mode": (
                    "realtime_overlay"
                    if service._realtime_pipeline is not None
                    else "offline_only"
                ),
                "provider": str(service._config.data_source.runtime_live_provider).strip()
                or "offline",
            },
            "runtime": {
                "monster_pipeline": monster_runtime_payload,
            },
            "monster_scan_controls": dict(monster_scan_controls),
            "first_board": {
                "interval_minutes": max(1, service._config.week5.first_board_interval_min),
                "window_intervals": list(service._config.week5.first_board_window_intervals),
                "windows": list(service._config.week5.first_board_windows),
                "candidate_count": len(first_board_candidates),
                "candidates": first_board_candidates,
                "leaders": leaders,
            },
            "signal_pool": {
                "candidate_count": len(signal_pool_candidates),
                "candidates": signal_pool_candidates[:100],
                "execution_rerank": dict(execution_rerank),
                "ranking": {
                    "mode": "two_stage_funnel",
                    "score_key": ranking_score_key,
                    "shortlist_top_n": shortlist_top_n,
                    "selected_count": min(shortlist_top_n, len(signal_pool_candidates)),
                    "selected_symbols": [
                        str(item.get("symbol", "")).strip()
                        for item in signal_pool_candidates[:shortlist_top_n]
                        if str(item.get("symbol", "")).strip()
                    ],
                    "preview": shortlist_preview,
                    "execution_rerank": dict(execution_rerank),
                },
            },
            "dual_track": self._build_dual_track_output(
                final_selector=final_selector,
                signal_map=signal_map,
                trend_signal_map=trend_signal_map,
                dual_track=dual_track,
                trend_pipeline_duration_ms=trend_pipeline_duration_ms,
            ),
            "anomalies": {
                "event_count": len(anomalies),
                "events": anomalies,
            },
            "empty_signal": empty_signal,
            "monster_isolation": isolation,
            "summary": {
                "first_board_candidates": len(first_board_candidates),
                "leaders": len(leaders),
                "anomalies": len(anomalies),
                "empty_signal_triggered": bool(empty_signal.get("triggered", False)),
                "can_open_monster": bool(isolation.get("can_open_new_position", False)),
                "prefilter_applied": bool(prefilter_report.get("applied", False)),
                "prefilter_shortlisted": _as_int(
                    prefilter_report.get("shortlisted_count"),
                    default=len(symbol_list),
                ),
                "monster_scan_cap_applied": monster_scan_cap_applied,
                "monster_scan_dropped_count": max(
                    0,
                    original_monster_scan_count - len(symbol_list),
                ),
                "execution_rerank_applied": bool(execution_rerank.get("applied", False)),
            },
        }

        requested_watchlist_sync = (
            bool(sync_watchlist)
            if sync_watchlist is not None
            else (symbols is None and bool(service._config.week5.auto_sync_watchlist))
        )
        should_sync_watchlist = requested_watchlist_sync and not intraday_scheduler_mode
        # An explicit recovery run is advisory only: it must not mutate the
        # watchlist or feed production signal channels.
        if recovery_mode:
            should_sync_watchlist = False
        watchlist_sync: dict[str, object] = {
            "enabled": requested_watchlist_sync,
            "updated": False,
            "reason": (
                "intraday_preserve_existing"
                if requested_watchlist_sync and intraday_scheduler_mode
                else "disabled"
            ),
            "watchlist_before": len(service._state.watchlist),
            "watchlist_after": len(service._state.watchlist),
            "symbols": list(service._state.watchlist),
        }
        if should_sync_watchlist:
            # Watchlist must only be synced from the final selection.  When
            # the funnel yields zero final signals, the old watchlist is
            # preserved (keep_if_empty) instead of topping up from the signal
            # pool with stocks that never passed the final gates.
            watchlist_sync = self._state_service.auto_sync_watchlist_from_week5_report(
                report=report,
                reason=sync_reason or f"week5_scan:{symbol_source}",
                top_k_override=sync_top_k_override,
                allow_signal_pool_fallback=False,
            )
        else:
            watchlist_sync["diagnostics"] = (
                self._state_service.build_watchlist_sync_diagnostics(
                    report=report,
                    top_k_override=sync_top_k_override,
                    selected=[],
                    fallback_applied=False,
                    allow_signal_pool_fallback=False,
                )
            )
        report["watchlist_sync"] = watchlist_sync
        summary = report.get("summary")
        if isinstance(summary, dict):
            summary["watchlist_synced"] = bool(watchlist_sync.get("updated", False))

        self._state_service.store_week5_scan_report(report)
        has_warning = bool(empty_signal.get("triggered", False)) or len(anomalies) > 0
        service._record_audit_event(
            event_type="week5_scan",
            trace_id=trace_id,
            level="warn" if has_warning else "info",
            payload={
                "watchlist_size": len(symbol_list),
                "first_board_candidates": len(first_board_candidates),
                "anomalies": len(anomalies),
                "empty_signal_triggered": bool(empty_signal.get("triggered", False)),
                "can_open_monster": bool(isolation.get("can_open_new_position", False)),
                "watchlist_sync": watchlist_sync,
            },
        )

        use_notify = service._config.week5.auto_notify
        if notify_enabled is not None:
            use_notify = notify_enabled
        if use_notify:
            week5_content = self._notification_service.build_scan_notification_content(
                symbol_list=symbol_list,
                first_board_candidates=first_board_candidates,
                leaders=leaders,
                anomalies=anomalies,
                empty_signal=empty_signal,
                watchlist_sync=watchlist_sync,
                runtime_mode=(
                    "realtime_overlay"
                    if service._realtime_pipeline is not None
                    else "offline_only"
                ),
            )
            service._notify_if_changed(
                dedup_key=f"notify:week5-scan:{now.strftime('%Y%m%d')}",
                title=_push_title(
                    priority="P1" if has_warning else "P2",
                    category="week5",
                    summary="intraday scan",
                ),
                content=week5_content,
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
            service._notify_actionable_signals(
                monster_report,
                trace_id=trace_id,
                title_prefix="week5 scan",
            )

        return report

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
                symbol = _normalize_a_share_symbol(
                    str(candidate.get("symbol", "")).strip()
                )
                fallback_snapshot = None
                if symbol:
                    try:
                        fallback_snapshot = (
                            service._sample_store.latest_snapshot_for_symbol(
                                symbol=symbol,
                                before=datetime.now(UTC),
                            )
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
            candidate["execution_risk_mode"] = (
                "shadow_only" if shadow_only else "qualified"
            )
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

        latest = (
            service._last_week5_scan_report
            if isinstance(service._last_week5_scan_report, dict)
            else {}
        )
        signal_pool = latest.get("signal_pool", {}) if isinstance(latest, dict) else {}
        raw_candidates = signal_pool.get("candidates", []) if isinstance(signal_pool, dict) else []
        candidate = next(
            (
                item
                for item in raw_candidates
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
                    "watchlist"
                    if normalized_symbol in service._state.watchlist
                    else "signal_pool"
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
            "execution_risk": (
                dict(execution_risk) if isinstance(execution_risk, Mapping) else {}
            ),
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
            "execution_risk": (
                dict(execution_risk) if isinstance(execution_risk, Mapping) else {}
            ),
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
        latest_name = _clean_display_text(
            latest_daily.get("name")
        ) or _latest_name_from_bars(working)
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
                        "intraday_1m"
                        if intraday_origin == "local"
                        else "intraday_1m_online"
                    )
                else:
                    trend_label = (
                        "intraday_5m"
                        if intraday_origin == "local"
                        else "intraday_5m_online"
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
            "bid_levels": [
                dict(item) for item in bid_levels
            ],
            "ask_levels": [
                dict(item) for item in ask_levels
            ],
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
            Path(raw_path)
            if raw_path
            else Path("artifacts/runtime/week5_scan_progress.json")
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
