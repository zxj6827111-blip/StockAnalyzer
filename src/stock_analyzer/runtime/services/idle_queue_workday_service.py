"""Idle queue workday task workflows."""

from __future__ import annotations

import random
from datetime import datetime
from datetime import time as dt_time
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from stock_analyzer.runtime.services.idle_queue_workday_report_service import (
    RuntimeIdleQueueWorkdayReportService,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueWorkdayService:
    """Idle queue workday task workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._report_service = RuntimeIdleQueueWorkdayReportService(service)

    def _idle_task_wd_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        report = service.run_week6_data_prewarm(
            symbols=list(service._state.watchlist),
            notify_enabled=False,
            source_trace_id="idle-WD-P0-01",
            timestamp=now,
        )
        raw_quality_status = str(report.get("status", "")).strip().lower()
        status = "ok" if raw_quality_status == "healthy" else "degraded"
        fallback_source_trade_date = ""
        if raw_quality_status == "critical":
            stale = service._idle_find_latest_task_report(
                task_id="WD-P0-01",
                subdir="data_quality",
                filename="report.json",
                exclude_trade_date=trade_date,
            )
            if stale is not None:
                status = "fallback"
                fallback_source_trade_date = str(stale.get("trade_date", ""))
                stale_payload = stale.get("payload", {})
                if isinstance(stale_payload, dict):
                    report = stale_payload

        payload = {
            "task_id": "WD-P0-01",
            "trade_date": trade_date,
            "generated_at": datetime.now().isoformat(),
            "status": status,
            "quality_status": raw_quality_status,
            "fallback_source_trade_date": fallback_source_trade_date,
            "data_quality": report,
        }
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-01",
            subdir="data_quality",
            filename="report.json",
        )
        service._idle_write_json(output_path, payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "quality_status": raw_quality_status,
            "fallback_source_trade_date": fallback_source_trade_date,
        }

    def _idle_task_wd_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        analysis_days = 14
        cutoff_ts = now.timestamp() - analysis_days * 86400
        recent_runs = [
            item for item in service._run_summaries if _report_timestamp(item) >= cutoff_ts
        ]

        loss_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-02",
            subdir="failure_analysis",
            filename="loss_attribution.json",
        )
        missed_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-02",
            subdir="failure_analysis",
            filename="missed_signals.json",
        )
        kill_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-02",
            subdir="failure_analysis",
            filename="kill_rate_stats.json",
        )

        if not recent_runs:
            fallback_sources = [
                ("loss_attribution", loss_path.name, "loss_attribution.json"),
                ("missed_signals", missed_path.name, "missed_signals.json"),
                ("kill_rate_stats", kill_path.name, "kill_rate_stats.json"),
            ]
            restored = 0
            for _, _, filename in fallback_sources:
                stale = service._idle_find_latest_task_report(
                    task_id="WD-P0-02",
                    subdir="failure_analysis",
                    filename=filename,
                    exclude_trade_date=trade_date,
                )
                target = (
                    loss_path
                    if filename == loss_path.name
                    else (missed_path if filename == missed_path.name else kill_path)
                )
                stale_payload = stale.get("payload") if stale is not None else None
                if isinstance(stale_payload, dict):
                    payload = dict(stale_payload)
                    payload["status"] = "fallback"
                    payload["fallback_source_trade_date"] = (
                        str(stale.get("trade_date", "")) if stale is not None else ""
                    )
                    payload["generated_at"] = now.isoformat()
                    service._idle_write_json(target, payload)
                    restored += 1
                else:
                    service._idle_write_json(
                        target,
                        {
                            "status": "degraded",
                            "generated_at": now.isoformat(),
                            "reason": "no_recent_runtime_data",
                        },
                    )
            status = "fallback" if restored > 0 else "degraded"
            return {
                "status": status,
                "output_files": [str(loss_path), str(missed_path), str(kill_path)],
                "analysis_window_days": analysis_days,
            }

        total_runs = len(recent_runs)
        total_signals = sum(_as_int(item.get("signals"), default=0) for item in recent_runs)
        total_actionable = sum(_as_int(item.get("actionable"), default=0) for item in recent_runs)
        missed_runs = [
            {
                "timestamp": str(item.get("timestamp", "")),
                "signals": _as_int(item.get("signals"), default=0),
                "actionable": _as_int(item.get("actionable"), default=0),
                "risk_action": str(item.get("risk_action", "")),
            }
            for item in recent_runs
            if _as_int(item.get("signals"), default=0) > 0
            and _as_int(item.get("actionable"), default=0) == 0
        ]
        risk_action_counts: dict[str, int] = {}
        for item in recent_runs:
            risk_action = str(item.get("risk_action", "")).strip() or "unknown"
            risk_action_counts[risk_action] = risk_action_counts.get(risk_action, 0) + 1
        top_risk_actions = [
            {"risk_action": name, "count": count}
            for name, count in sorted(
                risk_action_counts.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )
        ][:8]
        kill_rate = (
            max(total_signals - total_actionable, 0) / total_signals if total_signals > 0 else 0.0
        )

        loss_payload = {
            "task_id": "WD-P0-02",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok",
            "analysis_window_days": analysis_days,
            "runs_analyzed": total_runs,
            "top_risk_actions": top_risk_actions,
        }
        missed_payload = {
            "task_id": "WD-P0-02",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok",
            "analysis_window_days": analysis_days,
            "missed_runs": len(missed_runs),
            "items": missed_runs[:100],
        }
        kill_payload = {
            "task_id": "WD-P0-02",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok",
            "analysis_window_days": analysis_days,
            "total_runs": total_runs,
            "total_signals": total_signals,
            "total_actionable": total_actionable,
            "kill_rate": round(kill_rate, 6),
            "avg_signals_per_run": round(total_signals / total_runs, 6) if total_runs else 0.0,
        }

        service._idle_write_json(loss_path, loss_payload)
        service._idle_write_json(missed_path, missed_payload)
        service._idle_write_json(kill_path, kill_payload)
        return {
            "status": "ok",
            "output_files": [str(loss_path), str(missed_path), str(kill_path)],
            "analysis_window_days": analysis_days,
            "runs_analyzed": total_runs,
        }

    def _idle_task_wd_p0_03(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        symbols = list(service._state.watchlist)
        feature_current: dict[str, list[float]] = {
            "ret_1d": [],
            "intraday_range": [],
            "volume_ratio_20": [],
        }
        feature_baseline: dict[str, list[float]] = {
            "ret_1d": [],
            "intraday_range": [],
            "volume_ratio_20": [],
        }
        for symbol in symbols:
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=90)
            except Exception:
                continue
            if bars.empty:
                continue
            if {"close", "high", "low", "volume"} - set(str(col) for col in bars.columns):
                continue
            close = _numeric_series(bars, "close")
            high = _numeric_series(bars, "high")
            low = _numeric_series(bars, "low")
            volume = _numeric_series(bars, "volume")
            if len(close) < 25 or len(high) < 25 or len(low) < 25 or len(volume) < 25:
                continue

            returns = close.pct_change().dropna()
            if len(returns) >= 2:
                feature_current["ret_1d"].append(float(returns.iloc[-1]))
                feature_baseline["ret_1d"].extend(
                    [float(value) for value in returns.iloc[:-1].tail(60).tolist()]
                )

            ranges = ((high - low) / close).replace([float("inf"), -float("inf")], pd.NA).dropna()
            if len(ranges) >= 2:
                feature_current["intraday_range"].append(float(ranges.iloc[-1]))
                feature_baseline["intraday_range"].extend(
                    [float(value) for value in ranges.iloc[:-1].tail(60).tolist()]
                )

            rolling_vol = volume.rolling(20).mean()
            vol_ratio = (
                (volume / rolling_vol).replace([float("inf"), -float("inf")], pd.NA).dropna()
            )
            if len(vol_ratio) >= 2:
                feature_current["volume_ratio_20"].append(float(vol_ratio.iloc[-1]))
                feature_baseline["volume_ratio_20"].extend(
                    [float(value) for value in vol_ratio.iloc[:-1].tail(60).tolist()]
                )

        feature_reports: list[dict[str, object]] = []
        for feature_name in sorted(feature_current.keys()):
            psi_value = _compute_population_stability_index(
                baseline=feature_baseline.get(feature_name, []),
                current=feature_current.get(feature_name, []),
            )
            if psi_value is None:
                feature_reports.append(
                    {
                        "feature": feature_name,
                        "status": "unavailable",
                        "baseline_samples": len(feature_baseline.get(feature_name, [])),
                        "current_samples": len(feature_current.get(feature_name, [])),
                    }
                )
                continue
            if psi_value < 0.1:
                level = "stable"
            elif psi_value <= 0.25:
                level = "warning"
            else:
                level = "critical"
            feature_reports.append(
                {
                    "feature": feature_name,
                    "status": "ok",
                    "psi": round(psi_value, 6),
                    "level": level,
                    "baseline_samples": len(feature_baseline.get(feature_name, [])),
                    "current_samples": len(feature_current.get(feature_name, [])),
                }
            )

        available_features = [
            item for item in feature_reports if str(item.get("status", "")) == "ok"
        ]
        coverage_ratio = len(available_features) / max(len(feature_reports), 1)
        if not available_features:
            status = "degraded"
            warning = "PSI_UNAVAILABLE"
        elif coverage_ratio < 0.5:
            status = "degraded"
            warning = "low_coverage"
        else:
            status = "ok"
            warning = ""

        psi_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-03",
            subdir="psi_monitor",
            filename="psi_report.json",
        )
        payload = {
            "task_id": "WD-P0-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "baseline": "auto_generated",
            "coverage_ratio": round(coverage_ratio, 6),
            "warning": warning,
            "features": feature_reports,
        }
        service._idle_write_json(psi_path, payload)
        return {
            "status": status,
            "output_files": [str(psi_path)],
            "coverage_ratio": round(coverage_ratio, 6),
        }

    def _idle_task_wd_p0_04(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        positions = service._portfolio.positions()
        industry_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-04",
            subdir="exposure_scan",
            filename="industry_concentration.json",
        )
        exposure_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-04",
            subdir="exposure_scan",
            filename="factor_exposure.json",
        )
        corr_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P0-04",
            subdir="exposure_scan",
            filename="correlation_matrix.json",
        )

        if not positions:
            payload = {
                "task_id": "WD-P0-04",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped_empty_portfolio",
            }
            service._idle_write_json(industry_path, payload)
            service._idle_write_json(exposure_path, payload)
            service._idle_write_json(corr_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped_empty_portfolio",
                "output_files": [str(industry_path), str(exposure_path), str(corr_path)],
            }

        symbol_weights: dict[str, float] = {}
        for item in positions:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            weight = _as_float(item.get("target_position"), default=0.0)
            symbol_weights[symbol] = max(weight, 0.0)
        if not symbol_weights:
            equal_weight = 1.0 / max(len(positions), 1)
            for item in positions:
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    symbol_weights[symbol] = equal_weight
        total_weight = sum(symbol_weights.values())
        if total_weight <= 0:
            total_weight = float(len(symbol_weights))
            symbol_weights = {key: 1.0 for key in symbol_weights}
        normalized_weights = {
            symbol: value / total_weight for symbol, value in symbol_weights.items()
        }

        industry_weights: dict[str, float] = {}
        returns_by_symbol: dict[str, pd.Series] = {}
        exposure_items: list[dict[str, object]] = []
        degraded_count = 0
        for symbol, weight in normalized_weights.items():
            industry = _infer_symbol_sector(symbol)
            industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=120)
            except Exception:
                degraded_count += 1
                continue
            if bars.empty or "close" not in bars.columns:
                degraded_count += 1
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 20:
                degraded_count += 1
                continue
            returns = close.pct_change().dropna().tail(60)
            if returns.empty:
                degraded_count += 1
                continue
            returns_by_symbol[symbol] = returns
            exposure_items.append(
                {
                    "symbol": symbol,
                    "industry": industry,
                    "weight": round(weight, 6),
                    "volatility_20": round(float(returns.tail(20).std(ddof=0)), 6),
                }
            )

        market_series = _build_market_return_series(returns_by_symbol)
        for item in exposure_items:
            symbol = str(item.get("symbol", ""))
            series = returns_by_symbol.get(symbol)
            beta = 0.0
            if series is not None and market_series is not None:
                aligned = pd.concat(
                    [series.rename("s"), market_series.rename("m")], axis=1
                ).dropna()
                if len(aligned) >= 5:
                    var_m = float(aligned["m"].var(ddof=0))
                    if var_m > 1e-12:
                        cov = float(aligned["s"].cov(aligned["m"]))
                        beta = cov / var_m
            item["beta"] = round(beta, 6)

        concentration_items = [
            {"industry": industry, "weight": round(weight, 6)}
            for industry, weight in sorted(
                industry_weights.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )
        ]
        corr_payload: dict[str, object] = {"matrix": {}, "symbols": []}
        if returns_by_symbol:
            frame = pd.DataFrame({key: value for key, value in returns_by_symbol.items()}).dropna(
                axis=1, how="all"
            )
            if not frame.empty:
                corr = frame.corr()
                corr_payload = {
                    "symbols": list(corr.columns),
                    "matrix": {
                        str(col): {
                            str(inner_col): round(
                                _as_float(corr.loc[col, inner_col], default=0.0),
                                6,
                            )
                            for inner_col in corr.columns
                        }
                        for col in corr.columns
                    },
                }

        status = "ok" if degraded_count == 0 else "degraded"
        industry_payload = {
            "task_id": "WD-P0-04",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "items": concentration_items,
        }
        exposure_payload = {
            "task_id": "WD-P0-04",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "items": exposure_items,
            "degraded_symbols": degraded_count,
        }
        corr_report = {
            "task_id": "WD-P0-04",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            **corr_payload,
        }
        service._idle_write_json(industry_path, industry_payload)
        service._idle_write_json(exposure_path, exposure_payload)
        service._idle_write_json(corr_path, corr_report)
        return {
            "status": status,
            "output_files": [str(industry_path), str(exposure_path), str(corr_path)],
            "degraded_symbols": degraded_count,
        }

    def _idle_task_wd_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        p0_01_status = service._idle_get_task_status("WD-P0-01", trade_date)
        if p0_01_status not in {"ok", "degraded", "fallback"}:
            return {
                "status": "skipped",
                "reason": "gate_failed:WD-P0-01",
                "output_files": [],
            }

        symbol_cap = max(
            20,
            _as_int(
                service._idle_task_manifests.get("WE-P1-03", {}).get("symbol_cap"),
                default=120,
            ),
        )
        universe = service._idle_symbol_universe(
            task_id="WE-P1-03",
            max_symbols=symbol_cap,
            min_symbols=20,
        )
        symbol_list = _string_list(universe.get("symbols", []))
        rows: list[dict[str, object]] = []
        if not symbol_list:
            return {
                "status": "degraded",
                "reason": "empty_universe",
                "output_files": [],
                "universe_source": str(universe.get("source", "")),
            }
        for idx, symbol in enumerate(symbol_list, start=1):
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=180)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 25:
                continue
            returns = close.pct_change().dropna()
            ma20 = float(close.tail(20).mean()) if len(close) >= 20 else float(close.iloc[-1])
            momentum20 = float(close.iloc[-1] / close.iloc[-20] - 1.0) if len(close) >= 20 else 0.0
            volatility20 = float(returns.tail(20).std(ddof=0)) if len(returns) >= 20 else 0.0
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "available_at": now.isoformat(),
                    "close": round(float(close.iloc[-1]), 6),
                    "ma20": round(ma20, 6),
                    "momentum20": round(momentum20, 6),
                    "volatility20": round(volatility20, 6),
                }
            )
            if idx % 500 == 0:
                service._idle_write_checkpoint(
                    task_id="WD-P1-05",
                    trade_date=trade_date,
                    phase="progress",
                    now=datetime.now(),
                    extra={
                        "processed_symbols": idx,
                        "rows": len(rows),
                        "universe_size": len(symbol_list),
                        "universe_source": str(universe.get("source", "")),
                    },
                )

        if symbol_list:
            service._idle_write_checkpoint(
                task_id="WD-P1-05",
                trade_date=trade_date,
                phase="final",
                now=datetime.now(),
                extra={
                    "processed_symbols": len(symbol_list),
                    "rows": len(rows),
                    "universe_size": len(symbol_list),
                    "universe_source": str(universe.get("source", "")),
                },
            )

        if not rows:
            return {
                "status": "degraded",
                "reason": "no_symbol_cache_generated",
                "output_files": [],
                "symbols_processed": len(symbol_list),
                "universe_source": str(universe.get("source", "")),
            }

        parquet_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-05",
            subdir="precompute",
            filename="precompute_cache.parquet",
        )
        json_fallback_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-05",
            subdir="precompute",
            filename="precompute_cache.json",
        )
        frame = pd.DataFrame(rows)
        fallback_reason = ""
        try:
            frame.to_parquet(parquet_path, index=False)
            status = "ok"
            outputs = [str(parquet_path)]
        except Exception as exc:
            fallback_reason = f"parquet_write_failed:{exc.__class__.__name__}"
            service._idle_write_json(
                json_fallback_path,
                {
                    "status": "fallback",
                    "reason": fallback_reason,
                    "rows": rows,
                },
            )
            status = "fallback"
            outputs = [str(json_fallback_path)]

        result = {
            "status": status,
            "output_files": outputs,
            "rows": len(rows),
            "symbols_processed": len(symbol_list),
            "universe_source": str(universe.get("source", "")),
        }
        if fallback_reason:
            result["reason"] = fallback_reason
        return result

    def _idle_task_wd_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        p0_01_status = service._idle_get_task_status("WD-P0-01", trade_date)
        if p0_01_status not in {"ok", "degraded", "fallback"}:
            return {
                "status": "skipped",
                "reason": "gate_failed:WD-P0-01",
                "output_files": [],
            }

        positions = service._portfolio.positions()
        if not positions:
            var_path = service._idle_output_path(
                trade_date=trade_date,
                task_id="WD-P1-06",
                subdir="monte_carlo",
                filename="var_cvar_report.json",
            )
            dist_path = service._idle_output_path(
                trade_date=trade_date,
                task_id="WD-P1-06",
                subdir="monte_carlo",
                filename="scenario_distribution.json",
            )
            payload: dict[str, object] = {
                "task_id": "WD-P1-06",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped_empty_portfolio",
            }
            service._idle_write_json(var_path, payload)
            service._idle_write_json(dist_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped_empty_portfolio",
                "output_files": [str(var_path), str(dist_path)],
            }

        symbol_weights: dict[str, float] = {}
        for item in positions:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            symbol_weights[symbol] = max(_as_float(item.get("target_position"), default=0.0), 0.0)
        if not symbol_weights:
            equal_weight = 1.0 / max(len(positions), 1)
            for item in positions:
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    symbol_weights[symbol] = equal_weight
        total_weight = sum(symbol_weights.values())
        if total_weight <= 0:
            total_weight = float(len(symbol_weights))
            symbol_weights = {key: 1.0 for key in symbol_weights}
        normalized_weights = {
            symbol: value / total_weight for symbol, value in symbol_weights.items()
        }

        returns_by_symbol: dict[str, pd.Series] = {}
        for symbol in normalized_weights:
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=180)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 40:
                continue
            series = close.pct_change().dropna().tail(120)
            if not series.empty:
                returns_by_symbol[symbol] = series

        if not returns_by_symbol:
            return {
                "status": "degraded",
                "reason": "no_return_series",
                "output_files": [],
            }

        returns_frame = pd.DataFrame(returns_by_symbol).dropna(axis=1, how="all")
        if returns_frame.empty:
            return {
                "status": "degraded",
                "reason": "empty_returns_frame",
                "output_files": [],
            }

        # Align weights to available return columns only.
        aligned_weights = {
            symbol: normalized_weights.get(symbol, 0.0)
            for symbol in returns_frame.columns
            if symbol in normalized_weights
        }
        if not aligned_weights:
            return {
                "status": "degraded",
                "reason": "no_weight_alignment",
                "output_files": [],
            }
        weight_sum = sum(aligned_weights.values())
        aligned_weights = {symbol: value / weight_sum for symbol, value in aligned_weights.items()}
        portfolio_returns = returns_frame.fillna(0.0).apply(
            lambda row: float(
                sum(
                    _as_float(row.get(symbol), default=0.0) * aligned_weights.get(symbol, 0.0)
                    for symbol in aligned_weights
                )
            ),
            axis=1,
        )
        portfolio_series = pd.to_numeric(portfolio_returns, errors="coerce").dropna()
        if len(portfolio_series) < 20:
            return {
                "status": "degraded",
                "reason": "insufficient_portfolio_returns",
                "output_files": [],
            }

        simulations = 10_000
        horizon = 20
        block_size = 5
        mode = "block_bootstrap"
        draws: list[float] = []
        returns_values = [float(value) for value in portfolio_series.tolist()]
        if len(returns_values) < 60:
            mode = "historical_simple"
            for _ in range(simulations):
                sampled = [
                    random.choice(returns_values) for _ in range(min(horizon, len(returns_values)))
                ]
                cumulative = 1.0
                for r in sampled:
                    cumulative *= 1.0 + r
                draws.append(cumulative - 1.0)
        else:
            max_start = max(0, len(returns_values) - block_size)
            for _ in range(simulations):
                sampled = []
                while len(sampled) < horizon:
                    start = random.randint(0, max_start) if max_start > 0 else 0
                    block = returns_values[start : start + block_size]
                    sampled.extend(block)
                sampled = sampled[:horizon]
                cumulative = 1.0
                for r in sampled:
                    cumulative *= 1.0 + r
                draws.append(cumulative - 1.0)

        draw_series = pd.Series(draws)
        var_99 = float(draw_series.quantile(0.01))
        tail = draw_series[draw_series <= var_99]
        cvar_99 = float(tail.mean()) if not tail.empty else var_99

        var_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-06",
            subdir="monte_carlo",
            filename="var_cvar_report.json",
        )
        dist_parquet = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-06",
            subdir="monte_carlo",
            filename="scenario_distribution.parquet",
        )
        dist_json = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-06",
            subdir="monte_carlo",
            filename="scenario_distribution.json",
        )
        report_payload = {
            "task_id": "WD-P1-06",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok",
            "method": mode,
            "simulations": simulations,
            "horizon_days": horizon,
            "var_99": round(var_99, 6),
            "cvar_99": round(cvar_99, 6),
        }
        service._idle_write_json(var_path, report_payload)

        dist_df = pd.DataFrame({"simulated_return": draws})
        try:
            dist_df.to_parquet(dist_parquet, index=False)
            status = "ok"
            outputs = [str(var_path), str(dist_parquet)]
        except Exception:
            service._idle_write_json(
                dist_json,
                {
                    "task_id": "WD-P1-06",
                    "trade_date": trade_date,
                    "generated_at": now.isoformat(),
                    "status": "fallback",
                    "method": mode,
                    "simulations": simulations,
                    "returns": [round(float(item), 8) for item in draws[:5000]],
                },
            )
            status = "fallback"
            outputs = [str(var_path), str(dist_json)]
        return {
            "status": status,
            "output_files": outputs,
            "simulations": simulations,
            "method": mode,
        }

    def _idle_task_wd_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        p0_01_status = service._idle_get_task_status("WD-P0-01", trade_date)
        if p0_01_status not in {"ok", "degraded", "fallback"}:
            return {
                "status": "skipped",
                "reason": "gate_failed:WD-P0-01",
                "output_files": [],
            }

        sector_momentum: dict[str, list[float]] = {}
        sector_volume_ratio: dict[str, list[float]] = {}
        symbol_count = 0
        universe = service._idle_symbol_universe(
            task_id="WD-P1-05",
            max_symbols=1800,
            min_symbols=20,
        )
        symbol_list = _string_list(universe.get("symbols", []))
        for symbol in symbol_list:
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=60)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns or "volume" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            volume = _numeric_series(bars, "volume")
            if len(close) < 10 or len(volume) < 10:
                continue
            sector = _infer_symbol_sector(symbol)
            momentum5 = float(close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 else 0.0
            rolling_vol = volume.rolling(10).mean()
            vol_ratio = (volume / rolling_vol).dropna()
            volume_ratio = float(vol_ratio.iloc[-1]) if not vol_ratio.empty else 1.0
            sector_momentum.setdefault(sector, []).append(momentum5)
            sector_volume_ratio.setdefault(sector, []).append(volume_ratio)
            symbol_count += 1

        sector_items = []
        sentiment_items = []
        for sector in sorted(sector_momentum.keys()):
            momentums = sector_momentum.get(sector, [])
            volumes = sector_volume_ratio.get(sector, [])
            if not momentums:
                continue
            sector_items.append(
                {
                    "sector": sector,
                    "momentum_5d": round(sum(momentums) / len(momentums), 6),
                    "sample_size": len(momentums),
                }
            )
            sentiment_items.append(
                {
                    "sector": sector,
                    "avg_volume_ratio_10": round(sum(volumes) / len(volumes), 6)
                    if volumes
                    else 1.0,
                    "sample_size": len(volumes),
                }
            )

        status = "degraded" if symbol_count > 0 else "skipped"
        if symbol_count == 0:
            status = "skipped"
        sector_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-07",
            subdir="sector_radar",
            filename="sector_rotation.json",
        )
        sentiment_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WD-P1-07",
            subdir="sector_radar",
            filename="sentiment_radar.json",
        )
        sector_payload = {
            "task_id": "WD-P1-07",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "mode": "industry_only",
            "universe_source": str(universe.get("source", "")),
            "items": sector_items,
        }
        sentiment_payload = {
            "task_id": "WD-P1-07",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "mode": "industry_only",
            "universe_source": str(universe.get("source", "")),
            "items": sentiment_items,
        }
        service._idle_write_json(sector_path, sector_payload)
        service._idle_write_json(sentiment_path, sentiment_payload)
        return {
            "status": status,
            "output_files": [str(sector_path), str(sentiment_path)],
            "symbols": symbol_count,
            "universe_source": str(universe.get("source", "")),
        }

    def _idle_validate_precompute_cache(
        self,
        *,
        path: Path,
        expected_trade_date: str,
        now: datetime,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._report_service.idle_validate_precompute_cache(
                path=path,
                expected_trade_date=expected_trade_date,
                now=now,
            ),
        )

    def _idle_task_wd_report(self, context: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], self._report_service.idle_task_wd_report(context))


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _build_market_return_series(
    returns_by_symbol: dict[str, pd.Series],
) -> pd.Series | None:
    return cast(
        pd.Series | None,
        _runtime_service_module()._build_market_return_series(returns_by_symbol),
    )


def _compute_population_stability_index(
    baseline: list[float],
    current: list[float],
) -> float | None:
    return cast(
        float | None,
        _runtime_service_module()._compute_population_stability_index(baseline, current),
    )


def _infer_symbol_sector(symbol: str) -> str:
    return cast(str, _runtime_service_module()._infer_symbol_sector(symbol))


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    return cast(pd.Series, _runtime_service_module()._numeric_series(frame, column))


def _parse_hhmmss_time(raw: str) -> dt_time:
    return cast(dt_time, _runtime_service_module()._parse_hhmmss_time(raw))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))


def _report_timestamp(report: dict[str, object]) -> float:
    return cast(float, _runtime_service_module()._report_timestamp(report))


def _string_list(value: object) -> list[str]:
    return cast(list[str], _runtime_service_module()._string_list(value))
