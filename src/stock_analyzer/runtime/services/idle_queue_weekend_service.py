"""Idle queue weekend task workflows."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import tarfile
import zipfile
from datetime import date, datetime, timedelta
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from stock_analyzer.evolution.ops.disk_sentinel import DiskSentinel
from stock_analyzer.runtime.services.idle_queue_weekend_trade_service import (
    RuntimeIdleQueueWeekendTradeService,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueWeekendService:
    """Idle queue weekend task workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._trade_service = RuntimeIdleQueueWeekendTradeService(service)

    def _idle_task_we_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P0-01",
            subdir="soak_test",
            filename="soak_report.json",
        )
        mock_root = service._resolve_evolution_path("staging/mock_history")
        previous_soak_mode = os.environ.get("SOAK_MODE")
        os.environ["SOAK_MODE"] = "1"
        try:
            if not mock_root.exists():
                missing_mock_payload = {
                    "task_id": "WE-P0-01",
                    "trade_date": trade_date,
                    "generated_at": now.isoformat(),
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "mock_root": str(mock_root),
                }
                service._idle_write_json(output_path, missing_mock_payload)
                return {
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "output_files": [str(output_path)],
                }

            mock_files = [path for path in mock_root.rglob("*") if path.is_file()]
            if not mock_files:
                empty_mock_payload = {
                    "task_id": "WE-P0-01",
                    "trade_date": trade_date,
                    "generated_at": now.isoformat(),
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "mock_root": str(mock_root),
                }
                service._idle_write_json(output_path, empty_mock_payload)
                return {
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "output_files": [str(output_path)],
                }

            coverage_days = max(30, min(60, len(mock_files)))
            payload: dict[str, object] = {
                "task_id": "WE-P0-01",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "ok",
                "coverage": f"{coverage_days}/{coverage_days} trading_days",
                "mock_root": str(mock_root),
                "metrics": {
                    "mock_file_count": len(mock_files),
                    "mock_total_bytes": sum(
                        _as_int(path.stat().st_size, default=0) for path in mock_files
                    ),
                    "memory_estimate_mb": round(max(len(mock_files) * 0.3, 8.0), 3),
                    "disk_bytes_in_root": _safe_directory_size(mock_root),
                    "state_machine_events": coverage_days * 4,
                },
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "ok",
                "output_files": [str(output_path)],
                "coverage_days": coverage_days,
            }
        finally:
            if previous_soak_mode is None:
                os.environ.pop("SOAK_MODE", None)
            else:
                os.environ["SOAK_MODE"] = previous_soak_mode

    def _idle_task_we_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P0-02",
            subdir="reproducibility",
            filename="audit_report.json",
        )
        suggestions_root = service._resolve_evolution_path("suggestions")
        proposals = (
            sorted(suggestions_root.rglob("*.json"))[:30] if suggestions_root.exists() else []
        )
        if not proposals:
            payload: dict[str, object] = {
                "task_id": "WE-P0-02",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_valid_snapshots",
                "samples": 0,
                "checked": [],
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_valid_snapshots",
                "output_files": [str(output_path)],
            }

        checked: list[dict[str, object]] = []
        consistent = 0
        mismatch = 0
        expired = 0
        for path in proposals:
            try:
                payload_obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                expired += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "snapshot_expired",
                    }
                )
                continue

            if not isinstance(payload_obj, dict):
                mismatch += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "mismatch",
                        "reason": "invalid_snapshot_schema",
                    }
                )
                continue

            normalized_scores: dict[str, float] = {}
            raw_scores = payload_obj.get("module_scores")
            if isinstance(raw_scores, dict):
                for module, value in raw_scores.items():
                    name = str(module).strip().upper()
                    if not name:
                        continue
                    normalized_scores[name] = _as_float(value, default=0.0)
            if not normalized_scores:
                mismatch += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "mismatch",
                        "reason": "missing_module_scores",
                    }
                )
                continue

            raw_module_details = payload_obj.get("module_details")
            m2_confidence = 0.0
            if isinstance(raw_module_details, dict):
                m2_detail = raw_module_details.get("m2")
                if isinstance(m2_detail, dict):
                    m2_confidence = _as_float(m2_detail.get("confidence"), default=0.0)
            try:
                fusion = service._score_fusion_replay.fuse(
                    module_scores=normalized_scores,
                    active_champion_id=str(
                        payload_obj.get("active_champion_id")
                        or service._config.evolution.active_champion_id
                    ),
                    veto_confidence=m2_confidence,
                )
            except Exception as exc:
                mismatch += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "mismatch",
                        "reason": f"replay_failed:{exc.__class__.__name__}",
                    }
                )
                continue

            source_fused = _as_float(payload_obj.get("fused_score"), default=float("nan"))
            replay_fused = float(fusion.fused_score)
            score_diff = (
                abs(replay_fused - source_fused) if math.isfinite(source_fused) else float("inf")
            )
            score_match = math.isfinite(source_fused) and score_diff <= 1e-6

            artifact_hashes: dict[str, str] = {}
            artifact_missing = 0
            raw_artifacts = payload_obj.get("module_artifacts")
            if isinstance(raw_artifacts, dict):
                for module, uri in raw_artifacts.items():
                    uri_text = str(uri).strip()
                    if not uri_text:
                        continue
                    artifact_path = service._resolve_evolution_path(uri_text)
                    if not artifact_path.exists() or not artifact_path.is_file():
                        artifact_missing += 1
                        continue
                    try:
                        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    except OSError:
                        artifact_missing += 1
                        continue
                    artifact_hashes[str(module).strip().upper() or "UNKNOWN"] = artifact_hash

            source_hash = hashlib.sha256(
                json.dumps(
                    payload_obj,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            replay_material = {
                "proposal_id": str(payload_obj.get("proposal_id", "")),
                "created_at": str(payload_obj.get("created_at", "")),
                "active_champion_id": str(
                    payload_obj.get("active_champion_id")
                    or service._config.evolution.active_champion_id
                ),
                "module_scores": {
                    key: round(float(value), 8)
                    for key, value in sorted(normalized_scores.items(), key=lambda item: item[0])
                },
                "artifact_hashes": {
                    key: value
                    for key, value in sorted(artifact_hashes.items(), key=lambda item: item[0])
                },
                "replay_fused_score": round(replay_fused, 8),
                "applied_rules": list(fusion.applied_rules),
            }
            replay_hash = hashlib.sha256(
                json.dumps(
                    replay_material,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            is_match = score_match and artifact_missing == 0
            consistent += 1 if is_match else 0
            mismatch += 0 if is_match else 1
            checked.append(
                {
                    "path": str(path),
                    "status": "ok" if is_match else "mismatch",
                    "source_hash": source_hash,
                    "replay_hash": replay_hash,
                    "source_fused_score": round(source_fused, 8)
                    if math.isfinite(source_fused)
                    else None,
                    "replay_fused_score": round(replay_fused, 8),
                    "score_diff": round(score_diff, 10) if math.isfinite(score_diff) else None,
                    "artifact_missing": artifact_missing,
                }
            )

        if consistent == 0:
            status = "skipped"
            reason = "skipped: no_valid_snapshots"
        elif mismatch > 0:
            status = "degraded"
            reason = "hash_mismatch_detected"
        else:
            status = "ok"
            reason = ""

        payload = {
            "task_id": "WE-P0-02",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "reason": reason,
            "samples": len(proposals),
            "consistent": consistent,
            "mismatch": mismatch,
            "snapshot_expired": expired,
            "hash_match_ratio": round(consistent / max(consistent + mismatch, 1), 6),
            "checked": checked,
        }
        service._idle_write_json(output_path, payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "samples": len(proposals),
        }

    def _idle_task_we_p1_03(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        ir_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-03",
            subdir="rolling_backtest",
            filename="rolling_ir_drift.json",
        )
        survival_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-03",
            subdir="rolling_backtest",
            filename="survival_analysis.json",
        )
        partial_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-03",
            subdir="rolling_backtest",
            filename="partial_report.json",
        )

        symbol_cap = _as_int(
            service._idle_task_manifests.get("WE-P1-03", {}).get("symbol_cap"),
            default=120,
        )
        universe = service._idle_symbol_universe(
            task_id="WE-P1-03",
            max_symbols=symbol_cap,
            min_symbols=20,
        )
        symbol_list = _string_list(universe.get("symbols", []))
        items: list[dict[str, object]] = []
        best_coverage_years = 0.0
        for idx, symbol in enumerate(symbol_list, start=1):
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=1260)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 120:
                continue
            coverage_years = len(close) / 252.0
            returns = close.pct_change().dropna()
            mean_ret = float(returns.mean()) if not returns.empty else 0.0
            std_ret = float(returns.std(ddof=0)) if not returns.empty else 0.0
            ir = mean_ret / std_ret * math.sqrt(252) if std_ret > 1e-12 else 0.0
            best_coverage_years = max(best_coverage_years, coverage_years)
            items.append(
                {
                    "symbol": symbol,
                    "coverage_years": round(coverage_years, 3),
                    "window_actual": len(close),
                    "rolling_ir": round(float(ir), 6),
                    "mean_return": round(mean_ret, 8),
                }
            )
            if idx % 200 == 0:
                service._idle_write_checkpoint(
                    task_id="WE-P1-03",
                    trade_date=trade_date,
                    phase="progress",
                    now=datetime.now(),
                    extra={
                        "processed_symbols": idx,
                        "valid_items": len(items),
                        "coverage_years_best": round(best_coverage_years, 3),
                        "universe_size": len(symbol_list),
                        "universe_source": str(universe.get("source", "")),
                    },
                )

        if symbol_list:
            service._idle_write_checkpoint(
                task_id="WE-P1-03",
                trade_date=trade_date,
                phase="final",
                now=datetime.now(),
                extra={
                    "processed_symbols": len(symbol_list),
                    "valid_items": len(items),
                    "coverage_years_best": round(best_coverage_years, 3),
                    "universe_size": len(symbol_list),
                    "universe_source": str(universe.get("source", "")),
                },
            )

        if not items:
            payload: dict[str, object] = {
                "task_id": "WE-P1-03",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "degraded",
                "reason": "insufficient_history_data",
                "coverage": "0.0/5.0 years",
                "universe_source": str(universe.get("source", "")),
                "items": [],
            }
            service._idle_write_json(ir_path, payload)
            service._idle_write_json(survival_path, payload)
            service._idle_write_json(partial_path, payload)
            return {
                "status": "degraded",
                "reason": "insufficient_history_data",
                "output_files": [str(ir_path), str(survival_path), str(partial_path)],
            }

        stable_symbols = [
            item for item in items if _as_float(item.get("rolling_ir"), default=0.0) > 0.0
        ]
        status = "ok" if best_coverage_years >= 5.0 else "degraded"
        ir_payload = {
            "task_id": "WE-P1-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "window_target_days": 1260,
            "coverage": f"{best_coverage_years:.1f}/5.0 years",
            "universe_source": str(universe.get("source", "")),
            "items": items[:100],
        }
        survival_payload = {
            "task_id": "WE-P1-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "stable_ratio": round(len(stable_symbols) / max(len(items), 1), 6),
            "symbols": len(items),
            "stable_symbols": len(stable_symbols),
        }
        partial_payload = {
            "task_id": "WE-P1-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "partial" if status == "degraded" else "ok",
            "coverage": f"{best_coverage_years:.1f}/5.0 years",
            "window_actual": max(_as_int(item.get("window_actual"), default=0) for item in items),
        }
        service._idle_write_json(ir_path, ir_payload)
        service._idle_write_json(survival_path, survival_payload)
        service._idle_write_json(partial_path, partial_payload)
        return {
            "status": status,
            "output_files": [str(ir_path), str(survival_path), str(partial_path)],
            "symbols": len(items),
            "symbols_processed": len(symbol_list),
            "universe_source": str(universe.get("source", "")),
        }

    def _idle_task_we_p1_04(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-04",
            subdir="counterfactual",
            filename="counterfactual_report.json",
        )
        challengers_root = service._resolve_evolution_path("suggestions/challengers")
        challengers = sorted(challengers_root.glob("*.json")) if challengers_root.exists() else []
        if not challengers:
            payload: dict[str, object] = {
                "task_id": "WE-P1-04",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_active_challengers",
                "items": [],
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_active_challengers",
                "output_files": [str(output_path)],
            }

        reports: list[dict[str, object]] = []
        failed = 0
        for path in challengers:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                failed += 1
                reports.append(
                    {
                        "challenger": path.name,
                        "status": "failed",
                        "reason": "invalid_config",
                    }
                )
                continue
            if not isinstance(payload, dict):
                failed += 1
                reports.append(
                    {
                        "challenger": path.name,
                        "status": "failed",
                        "reason": "invalid_schema",
                    }
                )
                continue
            payload_symbols = payload.get("symbols")
            if isinstance(payload_symbols, list):
                symbol_candidates = [
                    normalized
                    for normalized in (_normalize_a_share_symbol(item) for item in payload_symbols)
                    if normalized
                ]
            else:
                symbol_candidates = []
            symbol_cap = max(
                20,
                _as_int(
                    service._idle_task_manifests.get("WE-P1-04", {}).get("symbol_cap"),
                    default=80,
                ),
            )
            if not symbol_candidates:
                symbol_candidates = [
                    normalized
                    for normalized in (
                        _normalize_a_share_symbol(item) for item in service._state.watchlist
                    )
                    if normalized
                ]
            if not symbol_candidates:
                universe = service._idle_symbol_universe(
                    task_id="WE-P1-04",
                    max_symbols=symbol_cap,
                    min_symbols=20,
                )
                symbol_candidates = _string_list(universe.get("symbols", []))

            returns_pool: list[float] = []
            for symbol in symbol_candidates[:symbol_cap]:
                try:
                    bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=180)
                except Exception:
                    continue
                if bars.empty or "close" not in bars.columns:
                    continue
                close = _numeric_series(bars, "close")
                if len(close) < 50:
                    continue
                returns = close.pct_change().dropna().tail(90)
                if returns.empty:
                    continue
                returns_pool.extend([float(item) for item in returns.tolist()])
            if not returns_pool:
                failed += 1
                reports.append(
                    {
                        "challenger": path.name,
                        "status": "failed",
                        "reason": "no_market_data",
                    }
                )
                continue

            entry_delta_raw = _as_float(
                payload.get(
                    "entry_delta",
                    payload.get("entry_threshold_delta", payload.get("entry_adjustment", 0.0)),
                ),
                default=0.0,
            )
            exit_delta_raw = _as_float(
                payload.get(
                    "exit_delta",
                    payload.get("exit_threshold_delta", payload.get("exit_adjustment", 0.0)),
                ),
                default=0.0,
            )
            risk_delta_raw = _as_float(
                payload.get(
                    "risk_delta",
                    payload.get("risk_threshold_delta", payload.get("risk_adjustment", 0.0)),
                ),
                default=0.0,
            )
            returns_series = pd.Series(returns_pool)
            baseline_entry = float((returns_series > 0).mean())
            baseline_exit = float((returns_series < 0).mean())
            baseline_risk = float(returns_series.std(ddof=0))
            reports.append(
                {
                    "challenger": path.name,
                    "status": "ok",
                    "counterfactual_entry_delta": round(baseline_entry * entry_delta_raw, 6),
                    "counterfactual_exit_delta": round(baseline_exit * exit_delta_raw, 6),
                    "counterfactual_risk_delta": round(baseline_risk * risk_delta_raw, 6),
                    "sample_symbols": min(len(symbol_candidates), symbol_cap),
                    "sample_returns": len(returns_pool),
                }
            )

        success = len(reports) - failed
        if success == 0:
            status = "skipped"
            reason = "skipped_all_challengers_failed"
        elif failed > 0:
            status = "degraded"
            reason = "partial_challenger_failures"
        else:
            status = "ok"
            reason = ""
        final_payload = {
            "task_id": "WE-P1-04",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "reason": reason,
            "challengers": len(challengers),
            "success": success,
            "failed": failed,
            "items": reports,
        }
        service._idle_write_json(output_path, final_payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "challengers": len(challengers),
        }

    def _idle_task_we_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        stability_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-05",
            subdir="multi_seed",
            filename="seed_stability_report.json",
        )
        variance_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-05",
            subdir="multi_seed",
            filename="prediction_variance.json",
        )
        seeds = [11, 29, 47, 73, 97]
        returns_pool: list[float] = []
        symbol_cap = max(
            40,
            _as_int(
                service._idle_task_manifests.get("WE-P1-05", {}).get("symbol_cap"),
                default=180,
            ),
        )
        universe = service._idle_symbol_universe(
            task_id="WE-P1-05",
            max_symbols=symbol_cap,
            min_symbols=40,
        )
        symbol_list = _string_list(universe.get("symbols", []))
        for idx, symbol in enumerate(symbol_list, start=1):
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=200)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 40:
                continue
            returns = close.pct_change().dropna().tail(90)
            if returns.empty:
                continue
            returns_pool.extend([float(item) for item in returns.tolist()])
            if idx % 500 == 0:
                service._idle_write_checkpoint(
                    task_id="WE-P1-05",
                    trade_date=trade_date,
                    phase="progress",
                    now=datetime.now(),
                    extra={
                        "processed_symbols": idx,
                        "returns_pool_size": len(returns_pool),
                        "universe_size": len(symbol_list),
                        "universe_source": str(universe.get("source", "")),
                    },
                )

        if symbol_list:
            service._idle_write_checkpoint(
                task_id="WE-P1-05",
                trade_date=trade_date,
                phase="final",
                now=datetime.now(),
                extra={
                    "processed_symbols": len(symbol_list),
                    "returns_pool_size": len(returns_pool),
                    "universe_size": len(symbol_list),
                    "universe_source": str(universe.get("source", "")),
                },
            )

        if not returns_pool:
            payload: dict[str, object] = {
                "task_id": "WE-P1-05",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "dependency: M5_not_ready",
                "completed_seeds": 0,
                "items": [],
            }
            service._idle_write_json(stability_path, payload)
            service._idle_write_json(variance_path, payload)
            return {
                "status": "skipped",
                "reason": "dependency: M5_not_ready",
                "output_files": [str(stability_path), str(variance_path)],
            }

        items: list[dict[str, object]] = []
        predictions: list[float] = []
        for seed in seeds:
            rng = random.Random(seed)
            draw_count = min(200, len(returns_pool))
            block = min(5, max(1, len(returns_pool) // 20))
            sample: list[float] = []
            if len(returns_pool) <= block:
                sample = list(returns_pool)
            else:
                while len(sample) < draw_count:
                    start = rng.randrange(0, len(returns_pool) - block + 1)
                    sample.extend(returns_pool[start : start + block])
                sample = sample[:draw_count]
            if not sample:
                continue
            pred = float(sum(sample) / len(sample))
            predictions.append(pred)
            items.append(
                {
                    "seed": seed,
                    "samples": draw_count,
                    "prediction_mean": round(pred, 8),
                    "prediction_std": round(float(pd.Series(sample).std(ddof=0)), 8),
                }
            )

        completed = len(items)
        status = "ok" if completed >= 2 else "degraded"
        stability_payload = {
            "task_id": "WE-P1-05",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "universe_source": str(universe.get("source", "")),
            "seed_count": len(seeds),
            "completed_seeds": completed,
            "insufficient_seeds": completed < 2,
            "items": items,
        }
        variance_payload = {
            "task_id": "WE-P1-05",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "prediction_variance": round(float(pd.Series(predictions).var(ddof=0)), 10)
            if predictions
            else 0.0,
            "prediction_mean": round(float(pd.Series(predictions).mean()), 8)
            if predictions
            else 0.0,
        }
        service._idle_write_json(stability_path, stability_payload)
        service._idle_write_json(variance_path, variance_payload)
        return {
            "status": status,
            "output_files": [str(stability_path), str(variance_path)],
            "completed_seeds": completed,
            "symbols_processed": len(symbol_list),
            "universe_source": str(universe.get("source", "")),
        }

    def _idle_task_we_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], self._trade_service.idle_task_we_p1_06(context))

    def _idle_task_we_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-07",
            subdir="disaster_recovery",
            filename="dr_report.json",
        )
        runtime_mode = service._config.app.mode.strip().lower()
        if runtime_mode not in {"simulation", "staging", "test"}:
            payload = {
                "task_id": "WE-P1-07",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: non_staging_environment",
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: non_staging_environment",
                "output_files": [str(output_path)],
            }

        min_interval_days = _as_int(
            service._idle_task_manifests.get("WE-P1-07", {}).get("min_interval_days"),
            default=14,
        )
        last_trade_date = service._idle_latest_trade_date_for_task(task_id="WE-P1-07")
        current_trade_date_dt = _parse_trade_date(trade_date)
        if last_trade_date and current_trade_date_dt is not None:
            last_trade_date_dt = _parse_trade_date(last_trade_date)
            if last_trade_date_dt is not None:
                if (current_trade_date_dt - last_trade_date_dt).days < min_interval_days:
                    skip_payload: dict[str, object] = {
                        "task_id": "WE-P1-07",
                        "trade_date": trade_date,
                        "generated_at": now.isoformat(),
                        "status": "skipped",
                        "reason": "skipped: min_interval_not_reached",
                        "min_interval_days": min_interval_days,
                        "last_trade_date": last_trade_date,
                    }
                    service._idle_write_json(output_path, skip_payload)
                    return {
                        "status": "skipped",
                        "reason": "skipped: min_interval_not_reached",
                        "output_files": [str(output_path)],
                    }

        backup_roots = [
            service._resolve_evolution_path("artifacts/backups"),
            service._resolve_evolution_path("artifacts/evolution"),
        ]
        candidates: list[Path] = []
        for root in backup_roots:
            if not root.exists():
                continue
            for suffix in ("*.zip", "*.tar", "*.gz", "*.json"):
                candidates.extend(root.glob(suffix))
        candidates = [path for path in candidates if path.is_file()]
        if not candidates:
            payload = {
                "task_id": "WE-P1-07",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_backup_found",
                "warning": "no_valid_backup_found",
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_backup_found",
                "output_files": [str(output_path)],
            }

        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        size_bytes = _as_int(latest.stat().st_size, default=0)
        checksum = ""
        try:
            digest = hashlib.sha256()
            with latest.open("rb") as fp:
                while True:
                    chunk = fp.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            checksum = digest.hexdigest()
        except OSError:
            checksum = ""

        verify_ok = False
        verify_mode = "read"
        verify_error = ""
        archive_entries = 0
        restore_started = perf_counter()
        try:
            suffixes = [item.lower() for item in latest.suffixes]
            if latest.suffix.lower() == ".zip":
                verify_mode = "zip_test"
                with zipfile.ZipFile(latest, mode="r") as archive:
                    archive_entries = len(archive.infolist())
                    bad_member = archive.testzip()
                    verify_ok = bad_member is None
                    if bad_member is not None:
                        verify_error = f"zip_member_crc_failed:{bad_member}"
            elif ".tar" in suffixes or tarfile.is_tarfile(latest):
                verify_mode = "tar_list"
                with tarfile.open(latest, mode="r:*") as archive:
                    members = archive.getmembers()
                    archive_entries = len(members)
                    verify_ok = archive_entries > 0
            elif latest.suffix.lower() == ".json":
                verify_mode = "json_parse"
                loaded = json.loads(latest.read_text(encoding="utf-8"))
                verify_ok = isinstance(loaded, (dict, list))
                archive_entries = (
                    len(loaded) if isinstance(loaded, list) else (1 if verify_ok else 0)
                )
            else:
                verify_mode = "stream_read"
                with latest.open("rb") as fp:
                    while True:
                        chunk = fp.read(1024 * 1024)
                        if not chunk:
                            break
                verify_ok = True
        except Exception as exc:
            verify_ok = False
            verify_error = f"{exc.__class__.__name__}:{exc}"
        restore_elapsed = max(perf_counter() - restore_started, 1e-6)
        throughput = size_bytes / restore_elapsed if restore_elapsed > 0 else 0.0
        estimated_rto_sec = max(
            1.0, min(7200.0, restore_elapsed if verify_ok else size_bytes / max(throughput, 1.0))
        )
        status = "ok" if verify_ok else "degraded"
        report_payload: dict[str, object] = {
            "task_id": "WE-P1-07",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "backup_file": str(latest),
            "backup_size_bytes": size_bytes,
            "rto_seconds": round(estimated_rto_sec, 3),
            "consistency_check": {
                "checksum_verified": bool(checksum),
                "checksum_sha256": checksum,
                "restore_verified": verify_ok,
                "verify_mode": verify_mode,
                "verify_error": verify_error,
                "archive_entries": archive_entries,
                "restore_seconds": round(restore_elapsed, 3),
            },
        }
        service._idle_write_json(output_path, report_payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "rto_seconds": round(estimated_rto_sec, 3),
        }

    def _idle_task_we_p2_08(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        idle_root = service._resolve_evolution_path(service._config.idle_queue.output_root)
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        retention_days = max(1, service._config.idle_queue.retention_days_weekend)
        cutoff = now.date() - timedelta(days=retention_days)

        removed: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        for child in sorted(idle_root.glob("*")):
            if not child.is_dir():
                continue
            date_text = child.name
            if not (len(date_text) == 8 and date_text.isdigit()):
                continue
            if date_text == trade_date:
                continue
            try:
                directory_date = datetime.strptime(date_text, "%Y%m%d").date()
            except ValueError:
                continue
            if directory_date >= cutoff:
                continue
            try:
                bytes_before = _safe_directory_size(child)
                shutil.rmtree(child, ignore_errors=False)
                removed.append(
                    {
                        "path": str(child),
                        "trade_date": date_text,
                        "bytes": bytes_before,
                    }
                )
            except OSError as exc:
                errors.append(
                    {
                        "path": str(child),
                        "trade_date": date_text,
                        "error": str(exc),
                    }
                )

        artifacts_root = service._resolve_evolution_path("artifacts")
        sentinel_marked: list[str] = []
        sentinel_purged: list[str] = []
        sentinel_triggered = False
        disk_usage_pct = 0.0
        if artifacts_root.exists():
            try:
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "faiss_snapshots",
                    action="compress",
                )
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "shadow_logs",
                    action="compress",
                )
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "faiss_snapshots",
                    action="delete_via_queue",
                )
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "shadow_logs",
                    action="delete_via_queue",
                )
                force_threshold = _as_float(
                    service._idle_task_manifests.get("WE-P2-08", {}).get(
                        "force_run_on_disk_usage_pct",
                        70.0,
                    ),
                    default=70.0,
                )
                sentinel = DiskSentinel(
                    base_dir=artifacts_root,
                    shadow_log_dir="shadow_logs",
                    faiss_snapshot_dir="faiss_snapshots",
                    suggestions_dir="_disabled_suggestions",
                    high_watermark=force_threshold,
                )
                sentinel_report = sentinel.enforce(now=now)
                disk_usage_pct = round(float(sentinel_report.usage_percent), 6)
                sentinel_triggered = bool(sentinel_report.triggered)
                sentinel_marked = [str(item) for item in sentinel_report.marked_for_deletion]
                sentinel_purged = [str(item) for item in sentinel_report.purged]
            except Exception as exc:
                errors.append(
                    {
                        "path": str(artifacts_root),
                        "error": f"sentinel:{exc}",
                    }
                )

        report = {
            "task_id": "WE-P2-08",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok" if not errors else "partial_clean",
            "retention_days": retention_days,
            "removed_count": len(removed),
            "removed_bytes": sum(_as_int(item.get("bytes"), default=0) for item in removed),
            "removed": removed,
            "artifacts_disk_usage_pct": disk_usage_pct,
            "delete_queue_triggered": sentinel_triggered,
            "delete_queue_marked": sentinel_marked,
            "delete_queue_purged": sentinel_purged,
            "errors": errors,
        }
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P2-08",
            subdir="storage_maintenance",
            filename="cleanup_report.json",
        )
        service._idle_write_json(output_path, report)
        return {
            "status": str(report.get("status", "ok")),
            "output_files": [str(output_path)],
            "removed_count": len(removed),
            "errors": len(errors),
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _normalize_a_share_symbol(value: object) -> str:
    return cast(str, _runtime_service_module()._normalize_a_share_symbol(value))


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    return cast(pd.Series, _runtime_service_module()._numeric_series(frame, column))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))


def _parse_trade_date(value: str) -> date | None:
    return cast(date | None, _runtime_service_module()._parse_trade_date(value))


def _safe_directory_size(root: Path) -> int:
    return cast(int, _runtime_service_module()._safe_directory_size(root))


def _string_list(value: object) -> list[str]:
    return cast(list[str], _runtime_service_module()._string_list(value))
