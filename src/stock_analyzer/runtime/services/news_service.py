"""News and M7 runtime workflows extracted from the main runtime service."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from copy import deepcopy
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.evolution.llm_semantic import OpenAICompatibleNewsJudge
from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records


class RuntimeNewsService:
    """Delegated news-preview and M7 live-news workflows."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def preview_news_component(self, symbol: str, strategy: str = "trend") -> dict[str, object]:
        service = self._service
        payload = cast(
            dict[str, object],
            service._pipeline.preview_news_component(symbol=symbol, strategy=strategy),
        )
        service._record_audit_event(
            event_type="news_component_preview",
            payload={
                "symbol": payload.get("symbol", ""),
                "strategy": payload.get("strategy", strategy),
                "status": payload.get("status", ""),
                "news_component": payload.get("news_component", 0.5),
            },
        )
        return payload

    def preview_news_components(
        self,
        symbols: list[str],
        strategy: str = "trend",
    ) -> dict[str, object]:
        service = self._service
        payload = cast(
            dict[str, object],
            service._pipeline.preview_news_components(symbols=symbols, strategy=strategy),
        )
        service._record_audit_event(
            event_type="news_component_preview_batch",
            payload={
                "strategy": payload.get("strategy", strategy),
                "records": payload.get("records", 0),
                "ok_records": payload.get("ok_records", 0),
                "average_news_component": payload.get("average_news_component", 0.5),
            },
        )
        return payload

    def preview_news_watchlist(
        self,
        strategy: str = "trend",
        limit: int = 20,
        record_audit: bool = True,
    ) -> dict[str, object]:
        service = self._service
        normalized_limit = max(1, int(limit))
        symbols = [str(item).strip() for item in service._state.watchlist if str(item).strip()]
        source = "watchlist"
        if not symbols:
            source = "portfolio"
            portfolio_positions = service._portfolio.positions()
            symbols = [
                str(item.get("symbol", "")).strip()
                for item in portfolio_positions
                if str(item.get("symbol", "")).strip()
            ]
        deduped = list(dict.fromkeys(symbols))
        selected_symbols = deduped[:normalized_limit]
        payload = cast(
            dict[str, object],
            service._pipeline.preview_news_components(
                symbols=selected_symbols,
                strategy=strategy,
            ),
        )
        items = payload.get("items", [])
        positive_records = 0
        neutral_records = 0
        negative_records = 0
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = _as_float(item.get("news_component"), default=0.5)
                sentiment = _news_component_sentiment(value)
                if sentiment == "positive":
                    positive_records += 1
                elif sentiment == "negative":
                    negative_records += 1
                else:
                    neutral_records += 1
        payload["source"] = source
        payload["selected_symbols"] = selected_symbols
        payload["limit"] = normalized_limit
        payload["summary"] = {
            "average_news_component": _as_float(
                payload.get("average_news_component"),
                default=0.5,
            ),
            "positive_records": positive_records,
            "neutral_records": neutral_records,
            "negative_records": negative_records,
        }
        if record_audit:
            service._record_audit_event(
                event_type="news_component_preview_watchlist",
                payload={
                    "strategy": payload.get("strategy", strategy),
                    "source": source,
                    "records": payload.get("records", 0),
                    "selected_symbols": selected_symbols,
                },
            )
        return payload

    def run_m7_live_news_sync(
        self,
        *,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        force_refresh: bool = False,
        enable_ai_review: bool | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        max_symbols = max(1, int(service._config.evolution.m7_live_news_max_symbols))
        per_symbol_limit = max(1, int(service._config.evolution.m7_live_news_per_symbol_limit))
        max_age_hours = max(1.0, float(service._config.evolution.m7_live_news_max_age_hours))
        selected_symbols = cast(
            list[str],
            service._resolve_m7_live_news_symbols(
                symbols=symbols,
                max_symbols=max_symbols,
            ),
        )
        ai_review_flag = (
            bool(enable_ai_review)
            if enable_ai_review is not None
            else bool(service._config.evolution.m7_ai_review_enabled)
        )
        artifact_path = cast(Path, service._resolve_m7_news_artifact_path())
        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "status": "ok",
            "provider": str(service._config.evolution.m7_live_news_provider).strip()
            or "akshare_em",
            "selected_symbols": selected_symbols,
            "symbol_count": len(selected_symbols),
            "force_refresh": bool(force_refresh),
            "ai_review_enabled": ai_review_flag,
            "artifact_path": str(artifact_path),
            "records": 0,
            "persisted_records": 0,
            "collection": {},
            "ai_review": {
                "enabled": ai_review_flag,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
            },
            "items": [],
        }
        if not selected_symbols:
            report["status"] = "skipped"
            report["reason"] = "empty_symbols"
            return report

        records, collection_report = service._collect_live_m7_news_records(
            symbols=selected_symbols,
            now=now,
            max_age_hours=max_age_hours,
            per_symbol_limit=per_symbol_limit,
            force_refresh=force_refresh,
            enable_ai_review=ai_review_flag,
        )
        report["collection"] = collection_report
        report["records"] = len(records)
        report["ai_review"] = collection_report.get("ai_review", report["ai_review"])

        existing_records = load_m7_news_records(path=artifact_path)
        merged_records = service._merge_m7_news_records(
            current=records,
            existing=existing_records,
            max_records=max(1, int(service._config.evolution.m7_live_news_artifact_max_records)),
        )
        service._persist_m7_news_records(artifact_path=artifact_path, records=merged_records)
        report["persisted_records"] = len(merged_records)
        report["items"] = merged_records[: min(10, len(merged_records))]
        service._record_audit_event(
            event_type="m7_live_news_sync",
            payload={
                "symbol_count": len(selected_symbols),
                "records": len(records),
                "persisted_records": len(merged_records),
                "artifact_path": str(artifact_path),
                "ai_review_enabled": ai_review_flag,
            },
        )
        return report

    def build_live_news_briefing(
        self,
        *,
        phase: str = "premarket",
        strategy: str = "trend",
        max_symbols: int = 6,
        max_items: int = 6,
        max_age_hours: float = 18.0,
        force_refresh: bool = False,
        record_audit: bool = True,
    ) -> dict[str, object]:
        service = self._service
        normalized_phase = phase.strip().lower() or "premarket"
        normalized_strategy = strategy.strip().lower() or "trend"
        phase_label = _news_phase_label_zh(normalized_phase)
        now = datetime.now()
        canonical_max_symbols = max(6, max_symbols)
        canonical_max_items = max(6, max_items)
        cache_key = service._live_news_briefing_cache_key(
            phase=normalized_phase,
            strategy=normalized_strategy,
            max_age_hours=max_age_hours,
            now=now,
        )
        if not force_refresh:
            cached_payload = service._load_live_news_briefing_cache(cache_key)
            if cached_payload is not None:
                return cast(
                    dict[str, object],
                    service._slice_live_news_briefing_payload(
                        payload=cached_payload,
                        max_symbols=max_symbols,
                        max_items=max_items,
                        cache_hit=True,
                        cache_key=cache_key,
                    ),
                )
        preview = cast(
            dict[str, object],
            service.preview_news_watchlist(
                strategy=normalized_strategy,
                limit=canonical_max_symbols,
                record_audit=False,
            ),
        )
        score_map: dict[str, float] = {}
        raw_preview_items = preview.get("items", [])
        if isinstance(raw_preview_items, list):
            for item in raw_preview_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if not symbol:
                    continue
                score_map[symbol] = _as_float(item.get("news_component"), default=0.5)
        focus_symbols = cast(
            list[str],
            service._select_live_news_focus_symbols(
                preview=preview,
                max_symbols=canonical_max_symbols,
            ),
        )
        effective_max_age_hours = max(1.0, float(max_age_hours))
        items = self._collect_live_news_briefing_items(
            focus_symbols=focus_symbols,
            now=now,
            max_age_hours=effective_max_age_hours,
            per_symbol_limit=max(2, min(4, canonical_max_items)),
            force_refresh=force_refresh,
        )
        if not items and normalized_phase == "premarket":
            fallback_max_age_hours = self._fallback_live_news_max_age_hours(
                requested_max_age_hours=effective_max_age_hours,
            )
            if fallback_max_age_hours > effective_max_age_hours:
                items = self._collect_live_news_briefing_items(
                    focus_symbols=focus_symbols,
                    now=now,
                    max_age_hours=fallback_max_age_hours,
                    per_symbol_limit=max(2, min(4, canonical_max_items)),
                    force_refresh=True,
                )
                effective_max_age_hours = fallback_max_age_hours

        deduped_items: list[dict[str, object]] = []
        seen_keys: set[tuple[str, str]] = set()
        for item in items:
            symbol = str(item.get("symbol", "")).strip()
            title = str(item.get("title", "")).strip()
            if not symbol or not title:
                continue
            pair_key = (symbol, title)
            if pair_key in seen_keys:
                continue
            seen_keys.add(pair_key)
            published_at = str(item.get("published_at", "")).strip()
            published_dt = _parse_runtime_datetime(published_at)
            age_hours = (
                max(0.0, (now - published_dt).total_seconds() / 3600.0)
                if published_dt is not None
                else effective_max_age_hours
            )
            recency = 1.0 - min(1.0, age_hours / effective_max_age_hours)
            news_component = score_map.get(symbol, 0.5)
            importance = recency * 0.6 + abs(news_component - 0.5) * 0.8
            deduped_items.append(
                {
                    **item,
                    "phase": normalized_phase,
                    "phase_label": phase_label,
                    "news_component": round(news_component, 4),
                    "age_hours": round(age_hours, 2),
                    "importance": round(importance, 4),
                }
            )
        deduped_items = sorted(
            deduped_items,
            key=lambda item: (
                -_as_float(item.get("importance"), default=0.0),
                str(item.get("published_at", "")),
                str(item.get("symbol", "")),
            ),
        )[: max(1, canonical_max_items)]
        raw_records = len(deduped_items)
        rendered_items: list[dict[str, object]] = []
        rendered_symbols: set[str] = set()
        for item in deduped_items:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or symbol in rendered_symbols:
                continue
            rendered_symbols.add(symbol)
            rendered_items.append(
                {
                    **item,
                    "name": service._resolve_symbol_display_name(symbol),
                }
            )
        base_payload = {
            "phase": normalized_phase,
            "phase_label": phase_label,
            "strategy": normalized_strategy,
            "generated_at": now.isoformat(),
            "focus_symbols": focus_symbols,
            "focus_count": len(focus_symbols),
            "lookback_hours": round(effective_max_age_hours, 2),
            "raw_records": raw_records,
            "records": len(rendered_items),
            "real_news_available": bool(rendered_items),
            "items": rendered_items,
        }
        service._cache.set(
            cache_key,
            json.dumps(base_payload, ensure_ascii=False),
            ttl_sec=service._live_news_briefing_cache_ttl_sec(
                phase=normalized_phase,
                now=now,
            ),
        )
        payload = cast(
            dict[str, object],
            service._slice_live_news_briefing_payload(
                payload=base_payload,
                max_symbols=max_symbols,
                max_items=max_items,
                cache_hit=False,
                cache_key=cache_key,
            ),
        )
        if record_audit:
            service._record_audit_event(
                event_type="live_news_briefing",
                payload={
                    "phase": normalized_phase,
                    "focus_symbols": focus_symbols,
                    "lookback_hours": round(effective_max_age_hours, 2),
                    "raw_records": raw_records,
                    "records": len(rendered_items),
                    "real_news_available": bool(rendered_items),
                },
            )
        return payload

    def _collect_live_news_briefing_items(
        self,
        *,
        focus_symbols: list[str],
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        service = self._service
        items: list[dict[str, object]] = []
        if not focus_symbols:
            return items
        with ThreadPoolExecutor(max_workers=min(4, len(focus_symbols))) as executor:
            futures = [
                executor.submit(
                    service._fetch_symbol_live_news,
                    symbol=symbol,
                    now=now,
                    max_age_hours=max_age_hours,
                    per_symbol_limit=per_symbol_limit,
                    force_refresh=force_refresh,
                )
                for symbol in focus_symbols
            ]
            for future in futures:
                try:
                    rows = future.result(timeout=20)
                except FuturesTimeoutError:
                    rows = []
                except Exception:
                    rows = []
                for row in rows:
                    if isinstance(row, dict):
                        items.append(row)
        return items

    def _fallback_live_news_max_age_hours(
        self,
        *,
        requested_max_age_hours: float,
    ) -> float:
        return max(120.0, max(1.0, float(requested_max_age_hours)))

    def _live_news_briefing_cache_key(
        self,
        *,
        phase: str,
        strategy: str,
        max_age_hours: float,
        now: datetime,
    ) -> str:
        age_bucket = round(max(1.0, max_age_hours), 2)
        return (
            "live-news-briefing:"
            f"{now.strftime('%Y%m%d')}:"
            f"{phase}:"
            f"{strategy}:"
            f"{age_bucket:.2f}"
        )

    def _live_news_briefing_cache_ttl_sec(
        self,
        *,
        phase: str,
        now: datetime,
    ) -> int:
        service = self._service
        default_ttl_sec = 30 * 60
        raw_boundary = ""
        if phase == "premarket":
            raw_boundary = str(service._config.scheduler.midday_news_time).strip()
        elif phase == "midday":
            raw_boundary = str(service._config.scheduler.close_reconcile_time).strip()
        if not raw_boundary:
            return default_ttl_sec
        try:
            boundary_clock = _parse_hhmm_time(raw_boundary)
        except ValueError:
            return default_ttl_sec
        boundary_dt = datetime.combine(now.date(), boundary_clock)
        if boundary_dt <= now:
            return default_ttl_sec
        return max(10 * 60, int((boundary_dt - now).total_seconds()))

    def _load_live_news_briefing_cache(
        self,
        cache_key: str,
    ) -> dict[str, object] | None:
        service = self._service
        cached = service._cache.get(cache_key)
        if not cached:
            return None
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _slice_live_news_briefing_payload(
        self,
        *,
        payload: dict[str, object],
        max_symbols: int,
        max_items: int,
        cache_hit: bool,
        cache_key: str,
    ) -> dict[str, object]:
        normalized_max_symbols = max(1, int(max_symbols))
        normalized_max_items = max(1, int(max_items))
        focus_symbols_raw = payload.get("focus_symbols", [])
        focus_symbols = [
            str(item).strip()
            for item in focus_symbols_raw
            if str(item).strip()
        ] if isinstance(focus_symbols_raw, list) else []
        selected_focus_symbols = focus_symbols[:normalized_max_symbols]
        allowed_symbols = set(selected_focus_symbols)
        sliced_items: list[dict[str, object]] = []
        raw_items = payload.get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if allowed_symbols and symbol and symbol not in allowed_symbols:
                    continue
                sliced_items.append(deepcopy(item))
                if len(sliced_items) >= normalized_max_items:
                    break
        sliced_payload = deepcopy(payload)
        sliced_payload["focus_symbols"] = selected_focus_symbols
        sliced_payload["focus_count"] = len(selected_focus_symbols)
        sliced_payload["items"] = sliced_items
        sliced_payload["records"] = len(sliced_items)
        sliced_payload["real_news_available"] = bool(sliced_items)
        sliced_payload["cache_hit"] = cache_hit
        sliced_payload["cache_key"] = cache_key
        return sliced_payload

    def _select_live_news_focus_symbols(
        self,
        *,
        preview: dict[str, object],
        max_symbols: int,
    ) -> list[str]:
        service = self._service
        symbols: list[str] = []
        raw_items = preview.get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    symbols.append(symbol)
        latest_week5 = service._last_week5_scan_report
        if isinstance(latest_week5, dict):
            first_board = latest_week5.get("first_board", {})
            if isinstance(first_board, dict):
                for key in ("leaders", "candidates"):
                    rows = first_board.get(key)
                    if not isinstance(rows, list):
                        continue
                    for item in rows:
                        if not isinstance(item, dict):
                            continue
                        symbol = str(item.get("symbol", "")).strip()
                        if symbol:
                            symbols.append(symbol)
        symbols.extend(
            str(item).strip()
            for item in service._state.watchlist
            if str(item).strip()
        )
        return _dedupe_preserve_order(symbols)[: max(1, max_symbols)]

    def _fetch_symbol_live_news(
        self,
        *,
        symbol: str,
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
    ) -> list[dict[str, object]]:
        service = self._service
        normalized_symbol = str(symbol).strip()
        if not normalized_symbol:
            return []
        age_bucket = round(max(1.0, float(max_age_hours)), 2)
        cache_key = f"live-news:{normalized_symbol}:{age_bucket:.2f}"
        if not force_refresh:
            cached = service._cache.get(cache_key)
            if cached:
                try:
                    parsed = json.loads(cached)
                    if isinstance(parsed, list):
                        return [item for item in parsed if isinstance(item, dict)]
                except json.JSONDecodeError:
                    pass
        try:
            with pd.option_context("mode.string_storage", "python"):
                ak = service._import_akshare()
                frame: pd.DataFrame = ak.stock_news_em(symbol=normalized_symbol)
                if frame is None or frame.empty:
                    return []
                records: list[dict[str, object]] = []
                max_age_sec = max(1.0, max_age_hours) * 3600.0
                for _, row in frame.head(30).iterrows():
                    title = str(row.get("新闻标题", "")).strip()
                    content = str(row.get("新闻内容", "")).strip()
                    published_at = str(row.get("发布时间", "")).strip()
                    source = str(row.get("文章来源", "")).strip()
                    url = str(row.get("新闻链接", "")).strip()
                    title = _row_first_text(row, "新闻标题", "鏂伴椈鏍囬") or title
                    content = _row_first_text(row, "新闻内容", "鏂伴椈鍐呭") or content
                    published_at = _row_first_text(row, "发布时间", "鍙戝竷鏃堕棿") or published_at
                    source = _row_first_text(row, "文章来源", "鏂囩珷鏉ユ簮") or source
                    url = _row_first_text(row, "新闻链接", "鏂伴椈閾炬帴") or url
                    if not title:
                        continue
                    published_dt = _parse_runtime_datetime(published_at)
                    if published_dt is not None:
                        age_sec = max(0.0, (now - published_dt).total_seconds())
                        if age_sec > max_age_sec:
                            continue
                    records.append(
                        {
                            "symbol": normalized_symbol,
                            "title": title,
                            "content": content,
                            "published_at": (
                                published_dt.isoformat()
                                if published_dt is not None
                                else published_at
                            ),
                            "source": source,
                            "url": url,
                        }
                    )
                    if len(records) >= max(1, per_symbol_limit):
                        break
        except Exception as exc:
            service._record_audit_event(
                event_type="live_news_fetch_failed",
                level="warn",
                payload={
                    "symbol": normalized_symbol,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:300],
                },
            )
            return []
        service._cache.set(
            cache_key,
            json.dumps(records, ensure_ascii=False),
            ttl_sec=15 * 60,
        )
        return records

    def _resolve_m7_live_news_symbols(
        self,
        *,
        symbols: list[str] | None,
        max_symbols: int,
    ) -> list[str]:
        service = self._service
        if symbols is not None:
            selected = [
                normalized
                for normalized in (_normalize_a_share_symbol(item) for item in symbols)
                if normalized
            ]
            return _dedupe_preserve_order(selected)[: max(1, max_symbols)]

        selected = cast(
            list[str],
            service._select_live_news_focus_symbols(
                preview={"items": []},
                max_symbols=max_symbols,
            ),
        )
        if selected:
            return selected[: max(1, max_symbols)]

        seed_symbols = cast(list[str], service._bootstrap_seed_symbols(cap=max_symbols))
        return seed_symbols[: max(1, max_symbols)]

    def _resolve_m7_news_artifact_path(self) -> Path:
        service = self._service
        raw_path = str(service._config.evolution.m7_news_records_path).strip()
        if raw_path:
            return cast(Path, service._resolve_evolution_path(raw_path))
        return cast(
            Path,
            service._resolve_evolution_path("artifacts/evolution/inputs/m7_news_latest.jsonl"),
        )

    def _collect_live_m7_news_records(
        self,
        *,
        symbols: list[str],
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
        enable_ai_review: bool,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        service = self._service
        provider = (
            str(service._config.evolution.m7_live_news_provider).strip().lower()
            or "akshare_em"
        )
        summary: dict[str, object] = {
            "provider": provider,
            "symbol_count": len(symbols),
            "fetched_symbols": 0,
            "raw_items": 0,
            "records": 0,
            "ai_review": {
                "enabled": enable_ai_review,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
            },
            "errors": [],
        }
        if provider not in {"akshare_em"}:
            summary["errors"] = [f"unsupported_provider:{provider}"]
            return [], summary

        raw_items: list[dict[str, object]] = []
        max_workers = min(4, max(1, len(symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    service._fetch_symbol_live_news,
                    symbol=symbol,
                    now=now,
                    max_age_hours=max_age_hours,
                    per_symbol_limit=per_symbol_limit,
                    force_refresh=force_refresh,
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    rows = future.result(timeout=20)
                except Exception as exc:
                    errors = summary.get("errors")
                    if isinstance(errors, list):
                        errors.append(f"{symbol}:{exc.__class__.__name__}")
                    continue
                if rows:
                    summary["fetched_symbols"] = _as_int(
                        summary.get("fetched_symbols"),
                        default=0,
                    ) + 1
                for row in rows:
                    if isinstance(row, dict):
                        raw_items.append(dict(row))
        summary["raw_items"] = len(raw_items)

        normalized_records = service._normalize_live_m7_news_records(raw_items=raw_items)
        ai_review_summary = {
            "enabled": enable_ai_review,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
        }
        if normalized_records and enable_ai_review:
            normalized_records, ai_review_summary = service._enrich_m7_news_records_with_ai_review(
                records=normalized_records,
                enabled_override=enable_ai_review,
            )
        summary["ai_review"] = ai_review_summary
        summary["records"] = len(normalized_records)
        return normalized_records, summary

    def _normalize_live_m7_news_records(
        self,
        *,
        raw_items: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        service = self._service
        default_cost = max(0.01, service._config.evolution.m7_default_event_cost)
        records: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for item in raw_items:
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            title = str(item.get("title", "")).strip()
            if not symbol or not title:
                continue
            published_at = str(item.get("published_at", "")).strip()
            content = str(item.get("content", "")).strip()
            event_seed = f"{symbol}|{title}|{published_at}"
            event_id = hashlib.sha256(event_seed.encode("utf-8")).hexdigest()[:24]
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            sentiment, confidence = _estimate_news_sentiment_heuristic(title=title, content=content)
            records.append(
                {
                    "event_id": event_id,
                    "symbol": symbol,
                    "headline": title,
                    "content": content,
                    "published_at": published_at,
                    "source": str(item.get("source", "")).strip(),
                    "url": str(item.get("url", "")).strip(),
                    "sentiment": sentiment,
                    "llm_sentiment": sentiment,
                    "cost": default_cost,
                    "llm_verdict": _m7_llm_verdict_from_sentiment(sentiment),
                    "llm_confidence": confidence,
                    "source_file": "__live_akshare_em__",
                    "provider": "akshare_em",
                    "proxy_generated": False,
                }
            )
        records.sort(
            key=lambda item: (
                str(item.get("published_at", "")),
                str(item.get("symbol", "")),
                str(item.get("headline", "")),
            ),
            reverse=True,
        )
        return records

    def _build_m7_news_review_judges(
        self,
        *,
        enabled_override: bool | None = None,
    ) -> tuple[OpenAICompatibleNewsJudge | None, OpenAICompatibleNewsJudge | None]:
        service = self._service
        ai_enabled = (
            bool(enabled_override)
            if enabled_override is not None
            else bool(service._config.evolution.m7_ai_review_enabled)
        )
        if not ai_enabled:
            return None, None
        primary = service._build_m7_news_review_judge(
            provider=service._config.evolution.llm_provider,
            api_key=service._config.evolution.llm_api_key,
            model=service._config.evolution.llm_model,
            base_url=service._config.evolution.llm_base_url,
        )
        backup_base_url = (
            str(service._config.evolution.llm_backup_base_url).strip()
            or str(service._config.evolution.llm_base_url).strip()
        )
        backup = service._build_m7_news_review_judge(
            provider=service._config.evolution.llm_backup_provider,
            api_key=service._config.evolution.llm_backup_api_key,
            model=service._config.evolution.llm_backup_model,
            base_url=backup_base_url,
        )
        return primary, backup

    def _build_m7_news_review_judge(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str,
    ) -> OpenAICompatibleNewsJudge | None:
        service = self._service
        normalized_provider = str(provider).strip().lower()
        if normalized_provider not in {"openai", "openai_compatible"}:
            return None
        return OpenAICompatibleNewsJudge(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_sec=service._config.evolution.llm_timeout_sec,
            temperature=service._config.evolution.llm_temperature,
            max_tokens=max(120, service._config.evolution.llm_max_tokens),
        )

    def _enrich_m7_news_records_with_ai_review(
        self,
        *,
        records: list[dict[str, object]],
        enabled_override: bool | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        service = self._service
        ai_enabled = (
            bool(enabled_override)
            if enabled_override is not None
            else bool(service._config.evolution.m7_ai_review_enabled)
        )
        primary_judge, backup_judge = service._build_m7_news_review_judges(
            enabled_override=ai_enabled,
        )
        primary_ready = bool(primary_judge is not None and primary_judge.configured)
        backup_ready = bool(backup_judge is not None and backup_judge.configured)
        summary: dict[str, object] = {
            "enabled": ai_enabled,
            "configured": primary_ready or backup_ready,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "primary_calls": 0,
            "backup_calls": 0,
            "fallback_used": 0,
        }
        if not ai_enabled:
            summary["reason"] = "disabled"
            return records, summary
        if not summary["configured"]:
            summary["reason"] = "missing_llm_credentials_or_model"
            return records, summary

        max_calls = max(0, int(service._config.evolution.m7_ai_review_max_items_per_run))
        if max_calls <= 0:
            summary["reason"] = "m7_ai_review_max_items_per_run<=0"
            return records, summary

        enriched = [dict(item) for item in records]
        for item in enriched[:max_calls]:
            summary["attempted"] = _as_int(summary.get("attempted"), default=0) + 1
            review = None
            if primary_ready and primary_judge is not None:
                summary["primary_calls"] = _as_int(summary.get("primary_calls"), default=0) + 1
                review = primary_judge.review(item)
                if review.error and backup_ready and backup_judge is not None:
                    summary["fallback_used"] = _as_int(summary.get("fallback_used"), default=0) + 1
                    summary["backup_calls"] = _as_int(summary.get("backup_calls"), default=0) + 1
                    review = backup_judge.review(item)
            elif backup_ready and backup_judge is not None:
                summary["backup_calls"] = _as_int(summary.get("backup_calls"), default=0) + 1
                review = backup_judge.review(item)
            if review is None or review.error:
                summary["failed"] = _as_int(summary.get("failed"), default=0) + 1
                continue
            sentiment = _clamp(review.sentiment, -1.0, 1.0)
            item["sentiment"] = sentiment
            item["llm_sentiment"] = sentiment
            item["llm_confidence"] = _clamp(review.confidence, 0.0, 1.0)
            item["llm_verdict"] = _m7_llm_verdict_from_sentiment(sentiment)
            item["llm_news_verdict"] = review.verdict
            item["llm_reason"] = review.reason
            summary["succeeded"] = _as_int(summary.get("succeeded"), default=0) + 1
        return enriched, summary

    def _merge_m7_news_records(
        self,
        *,
        current: list[dict[str, object]],
        existing: list[dict[str, object]],
        max_records: int,
    ) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for item in current + existing:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id", "")).strip()
            if not event_id:
                event_id = hashlib.sha256(
                    (
                        f"{item.get('symbol', '')}|{item.get('headline', '')}|"
                        f"{item.get('published_at', '')}"
                    ).encode()
                ).hexdigest()[:24]
                item = {**item, "event_id": event_id}
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            merged.append(dict(item))
        merged.sort(
            key=lambda item: (
                str(item.get("published_at", "")),
                str(item.get("symbol", "")),
                str(item.get("headline", "")),
            ),
            reverse=True,
        )
        return merged[: max(1, max_records)]

    def _persist_m7_news_records(
        self,
        *,
        artifact_path: Path,
        records: list[dict[str, object]],
    ) -> None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        with artifact_path.open("w", encoding="utf-8") as fp:
            for item in records:
                fp.write(json.dumps(item, ensure_ascii=False) + "\n")

    def news_score_history(
        self,
        limit: int = 50,
        symbol: str = "",
        strategy: str = "",
    ) -> dict[str, object]:
        service = self._service
        capped_limit = max(1, min(int(limit), 500))
        normalized_symbol = symbol.strip()
        normalized_strategy = strategy.strip().lower()
        filtered: list[dict[str, object]] = []
        for event in service._audit_events:
            if str(event.get("event_type", "")).strip().lower() != "news_component_preview":
                continue
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            item_symbol = str(payload.get("symbol", "")).strip()
            item_strategy = str(payload.get("strategy", "")).strip().lower()
            if normalized_symbol and item_symbol != normalized_symbol:
                continue
            if normalized_strategy and item_strategy != normalized_strategy:
                continue
            score = _as_float(payload.get("news_component"), default=0.5)
            filtered.append(
                {
                    "event_id": str(event.get("event_id", "")).strip(),
                    "timestamp": str(event.get("timestamp", "")).strip(),
                    "symbol": item_symbol,
                    "strategy": item_strategy or "trend",
                    "status": str(payload.get("status", "")).strip() or "unknown",
                    "news_component": round(max(0.0, min(1.0, score)), 4),
                }
            )
        items = filtered[-capped_limit:]
        if items:
            scores = [_as_float(item["news_component"], default=0.5) for item in items]
            average_score = round(sum(scores) / len(scores), 4)
            positive_records = sum(1 for value in scores if value >= 0.67)
            negative_records = sum(1 for value in scores if value <= 0.33)
            neutral_records = len(scores) - positive_records - negative_records
        else:
            average_score = 0.5
            positive_records = 0
            negative_records = 0
            neutral_records = 0
        return {
            "records": len(items),
            "total_matched": len(filtered),
            "items": items,
            "summary": {
                "average_news_component": average_score,
                "positive_records": positive_records,
                "neutral_records": neutral_records,
                "negative_records": negative_records,
            },
            "filters": {
                "symbol": normalized_symbol,
                "strategy": normalized_strategy,
                "limit": capped_limit,
            },
        }

    def news_score_cache_state(self) -> dict[str, object]:
        service = self._service
        return cast(dict[str, object], service._pipeline.news_preview_cache_state())

    def clear_news_score_cache(
        self,
        symbol: str = "",
        strategy: str = "",
    ) -> dict[str, object]:
        service = self._service
        payload = cast(
            dict[str, object],
            service._pipeline.clear_news_preview_cache(symbol=symbol, strategy=strategy),
        )
        service._record_audit_event(
            event_type="news_component_cache_clear",
            payload={
                "symbol": payload.get("symbol", ""),
                "strategy": payload.get("strategy", ""),
                "cleared": payload.get("cleared", 0),
                "remaining": payload.get("remaining", 0),
            },
        )
        return payload


def _news_component_sentiment(news_component: float) -> str:
    if news_component >= 0.67:
        return "positive"
    if news_component <= 0.33:
        return "negative"
    return "neutral"


def _m7_llm_verdict_from_sentiment(sentiment: float) -> str:
    if sentiment >= 0.05:
        return "approve"
    if sentiment <= -0.05:
        return "reject"
    return "review"


def _estimate_news_sentiment_heuristic(title: str, content: str) -> tuple[float, float]:
    text = f"{title} {content}".lower()
    positive_tokens = [
        "利好",
        "增长",
        "中标",
        "签约",
        "回购",
        "增持",
        "扭亏",
        "预增",
        "上调",
        "突破",
        "大涨",
        "盈利",
        "获批",
        "positive",
        "upgrade",
        "beat",
        "surge",
    ]
    negative_tokens = [
        "利空",
        "下滑",
        "减持",
        "亏损",
        "预亏",
        "处罚",
        "问询",
        "暴跌",
        "下调",
        "终止",
        "违约",
        "诉讼",
        "冻结",
        "negative",
        "downgrade",
        "miss",
        "plunge",
    ]
    positive_hits = sum(1 for token in positive_tokens if token in text)
    negative_hits = sum(1 for token in negative_tokens if token in text)
    if positive_hits == negative_hits == 0:
        return 0.0, 0.45
    raw_score = (positive_hits - negative_hits) / max(1, positive_hits + negative_hits)
    sentiment = _clamp(raw_score * 0.8, -1.0, 1.0)
    confidence = _clamp(0.55 + min(0.35, 0.10 * (positive_hits + negative_hits)), 0.0, 1.0)
    return sentiment, confidence


def _row_first_text(row: pd.Series, *candidates: str) -> str:
    for candidate in candidates:
        value = row.get(candidate, "")
        if pd.isna(value):
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


def _parse_runtime_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _news_phase_label_zh(phase: str) -> str:
    mapping = {
        "premarket": "盘前",
        "midday": "午盘前",
        "manual": "手动",
    }
    normalized = phase.strip().lower()
    return mapping.get(normalized, phase.strip() or "盘前")


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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _parse_hhmm_time(raw: str) -> dt_time:
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid hh:mm: {raw}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"invalid hh:mm: {raw}")
    return dt_time(hour=hour, minute=minute)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


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
