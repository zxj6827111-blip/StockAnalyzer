"""M12 主题层编排服务（抓取 → 抽取 → 确认 → 账本 → theme_state.json）。

照 ``news_service.run_m7_live_news_sync`` 模式：旁路增强任务，失败不阻断
调度器，全部产出落 artifacts（滚动文件 + 按日幂等归档 + duckdb 账本 +
原子写 state）。shadow 模式下 boost 表与注入清单为 dry-run（只记录不消费）。

依赖的运行时服务方法（由宿主 ``service`` 提供，与 news_service 同构）：
``_record_audit_event`` / ``_resolve_evolution_path``。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.market_calendar import is_a_share_trading_day
from stock_analyzer.theme.board_resolver import BoardResolver
from stock_analyzer.theme.extractor import (
    extract_theme_events_from_records,
    group_by_theme,
    theme_heat,
)
from stock_analyzer.theme.ledger import ThemeEventLedger
from stock_analyzer.theme.macro_news_adapter import MacroNewsAdapter
from stock_analyzer.theme.price_confirmation import PriceConfirmationAdapter
from stock_analyzer.theme.scorer import ThemeBoostProvider, build_theme_pool
from stock_analyzer.theme.taxonomy import ThemeTaxonomy, load_taxonomy

VALID_MODES = frozenset({"off", "shadow", "boost"})

# Phase 2 升级门槛（参照 news_risk_mode shadow 框架；shadow 期 ≥10 交易日、
# ≥30 个确认激活的主题事件、hit_rate_3d ≥ 55% 且人工一致率 ≥80% 才允许开 boost）。
SHADOW_MIN_TRADE_DAYS = 10
SHADOW_MIN_CONFIRMED_EVENTS = 30
SHADOW_MIN_HIT_RATE_3D = 0.55
SHADOW_MIN_HUMAN_AGREEMENT = 0.80


class RuntimeThemeService:
    """M12 主题层的运行时编排与预览查询。"""

    def __init__(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------------
    # 每日同步（调度回调 run_theme_daily_sync 的实现体）
    # ------------------------------------------------------------------
    def run_theme_daily_sync(
        self,
        *,
        timestamp: datetime | None = None,
        force_refresh: bool = False,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        config = service._config.theme
        mode = str(config.mode).strip().lower()
        if mode not in VALID_MODES:
            mode = "off"
        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "status": "ok",
            "mode": mode,
            "trade_date": now.date().isoformat(),
            "records": 0,
            "extractions": 0,
            "active_themes": [],
            "dry_run": mode != "boost",
        }
        steps: dict[str, object] = {}
        report["steps"] = steps
        # 总开关（照 m7_live_news_enabled 先例）：false 直接 skipped——
        # 测试/离线环境绝不打真实网络；NAS Phase 2 观察时显式置 true。
        if not bool(getattr(config, "enabled", False)):
            report["status"] = "skipped"
            report["reason"] = "theme_disabled"
            return report
        if mode == "off":
            report["status"] = "skipped"
            report["reason"] = "theme_mode_off"
            return report
        if not is_a_share_trading_day(now):
            report["status"] = "skipped"
            report["reason"] = "not_a_trading_day"
            return report

        taxonomy = self._load_taxonomy()
        if taxonomy is None:
            report["status"] = "error"
            report["reason"] = "taxonomy_load_failed"
            return report

        # 1) 抓取宏观快讯（只有实时数据；接口失败记 error 不抛）
        records: list[dict[str, object]] = []
        fetch_summary: dict[str, object] = {"provider": "cls", "count": 0}
        try:
            adapter = MacroNewsAdapter(
                provider="cls",
                max_items=500,
                ak_module=getattr(service, "_theme_ak_module", None),
            )
            fetched = adapter.fetch_latest(force_refresh=force_refresh)
            records = [record.to_dict() for record in fetched]
            fetch_summary = {
                "provider": adapter.provider,
                # 主源失败自动降级后的实际服务源（审计/排障用）
                "served_by": adapter.last_provider,
                "count": len(records),
            }
        except Exception as exc:
            fetch_summary = {
                "provider": "cls",
                "count": 0,
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:300],
            }
        report["records"] = len(records)
        steps["fetch"] = fetch_summary

        # 2) 规则抽取
        extractions = extract_theme_events_from_records(taxonomy=taxonomy, records=records)
        report["extractions"] = len(extractions)

        # 3) 主题聚合 + 价格确认（命门：未确认主题不进入 boost/注入）
        grouped = group_by_theme(extractions)
        price_adapter = PriceConfirmationAdapter(
            confirmation=taxonomy.confirmation,
            ak_module=getattr(service, "_theme_ak_module", None),
        )
        active_themes: list[dict[str, object]] = []
        proxy_price_by_theme: dict[str, float] = {}
        theme_symbols: dict[str, list[str]] = {}
        for theme in taxonomy.themes:
            theme_events = grouped.get(theme.theme_id, [])
            confirmation = price_adapter.confirm_theme(
                theme_id=theme.theme_id,
                commodities=theme.commodities,
                direction=theme.direction,
            )
            heat = theme_heat(theme_events)
            confirmed = confirmation.confirmed and heat > 0.0
            theme_entry: dict[str, object] = {
                "theme_id": theme.theme_id,
                "event_type": theme.event_type,
                "direction": theme.direction,
                "event_count": len(theme_events),
                "heat": round(heat, 4),
                "price_confirmed": confirmation.confirmed,
                "confirmed_by": list(confirmation.confirmed_by),
                "commodities": [item.to_dict() for item in confirmation.commodities],
                "active": confirmed,
            }
            # 代理价格：确认商品的最新收盘价均值（有效性统计基准价）
            closes = [
                item.last_close
                for item in confirmation.commodities
                if item.last_close is not None and item.last_close > 0
            ]
            if closes:
                proxy_price_by_theme[theme.theme_id] = sum(closes) / len(closes)
            if confirmed:
                # 板块成分解析（限频：仅确认主题才拉成分）
                board_resolver = self._board_resolver()
                symbols: list[str] = []
                board_summaries: list[dict[str, object]] = []
                for board in theme.boards:
                    constituents = board_resolver.resolve(board)
                    board_summaries.append(constituents.to_dict())
                    symbols.extend(constituents.symbols)
                symbols.extend(theme.symbols)
                seen: set[str] = set()
                deduped: list[str] = []
                for raw in symbols:
                    if raw and raw not in seen:
                        seen.add(raw)
                        deduped.append(raw)
                theme_symbols[theme.theme_id] = deduped
                theme_entry["boards"] = board_summaries
                theme_entry["symbol_count"] = len(deduped)
            active_themes.append(theme_entry)
        confirmed_theme_ids = [
            str(item["theme_id"]) for item in active_themes if bool(item.get("active", False))
        ]
        report["active_themes"] = confirmed_theme_ids
        steps["themes"] = active_themes

        # 4) 账本写入（theme 维度 dedup + 代理价格有效性）
        ledger_summary: dict[str, object] = {"inserted": 0}
        confirmation_lookup = {
            str(item["theme_id"]): bool(item.get("price_confirmed", False))
            for item in active_themes
        }
        try:
            ledger = self._ledger()
            ledger_records = [
                {
                    **extraction.to_dict(),
                    "price_confirmed": bool(
                        confirmation_lookup.get(extraction.theme_id, False)
                    ),
                }
                for extraction in extractions
            ]
            ingest = ledger.record_run(
                records=ledger_records,
                now=now,
                proxy_price_by_theme=proxy_price_by_theme,
            )
            ledger_summary = ingest.to_dict()
        except Exception as exc:
            ledger_summary = {
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:300],
            }
        steps["ledger"] = ledger_summary

        # 5) 快讯落盘（滚动 + 按日幂等归档，照 M7 双写约定）
        persist_summary = self._persist_news_records(records=records, now=now)
        steps["persist"] = persist_summary

        # 6) 产出 theme_state.json（原子写；shadow 模式 boost 表为空 = 全 dry-run）
        state = self._build_theme_state(
            now=now,
            mode=mode,
            active_themes=active_themes,
            confirmed_theme_ids=confirmed_theme_ids,
            theme_symbols=theme_symbols,
            ledger_summary=ledger_summary,
        )
        state_summary = self._write_theme_state(state=state)
        steps["state"] = state_summary
        report["theme_state"] = {
            "path": str(self._theme_state_path()),
            "active_themes": state.get("active_themes", []),
            "boost_symbol_count": len(cast(dict[str, object], state.get("boost_by_symbol", {}))),
            "pinned_pool": list(cast(list[object], state.get("pinned_pool", []))),
            "dry_run": bool(state.get("dry_run", True)),
        }

        service._record_audit_event(
            event_type="theme_daily_sync",
            payload={
                "mode": mode,
                "records": len(records),
                "extractions": len(extractions),
                "active_themes": confirmed_theme_ids,
                "dry_run": bool(state.get("dry_run", True)),
            },
        )
        return report

    # ------------------------------------------------------------------
    # 预览查询（API /theme/state、/theme/events）
    # ------------------------------------------------------------------
    def theme_state(self) -> dict[str, object]:
        """返回当前 theme_state.json 内容（缺失时返回空态）。"""
        state_path = self._theme_state_path()
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "status": "unavailable",
                "reason": "theme_state_not_found",
                "mode": str(self._service._config.theme.mode),
                "dry_run": True,
            }
        if not isinstance(payload, dict):
            return {
                "status": "unavailable",
                "reason": "theme_state_invalid",
                "mode": str(self._service._config.theme.mode),
                "dry_run": True,
            }
        return {**payload, "status": "ok"}

    def theme_events(
        self,
        *,
        status: str = "",
        limit: int = 100,
    ) -> dict[str, object]:
        """账本事件预览（/theme/events 数据源）。"""
        ledger = self._ledger()
        normalized_status = str(status).strip() or None
        rows = ledger.list_records(status=normalized_status, limit=limit)
        effectiveness = ledger.effectiveness_summary().to_dict()
        return {
            "records": len(rows),
            "items": rows,
            "effectiveness": effectiveness,
        }

    def theme_shadow_readiness(self) -> dict[str, object]:
        """Phase 2 升级门槛判定（≥10 交易日 + ≥30 确认事件 + hit_rate_3d ≥ 55%）。

        hit_rate 只统计价格确认激活的事件（ledger confirmed_hit_rate_3d）——
        门槛语义是"确认激活的主题事件有效性"，与 confirmed_events 同分母。
        """
        ledger = self._ledger()
        effectiveness = ledger.effectiveness_summary()
        trade_days = self._count_shadow_trade_days()
        confirmed_events = sum(
            _as_int(item.get("confirmed"), default=0) for item in effectiveness.by_theme
        )
        hit_rate_3d = effectiveness.confirmed_hit_rate_3d
        human_agreement = self._human_agreement_rate()
        ready = bool(
            trade_days >= SHADOW_MIN_TRADE_DAYS
            and confirmed_events >= SHADOW_MIN_CONFIRMED_EVENTS
            and hit_rate_3d is not None
            and hit_rate_3d >= SHADOW_MIN_HIT_RATE_3D
            and human_agreement is not None
            and human_agreement >= SHADOW_MIN_HUMAN_AGREEMENT
        )
        return {
            "ready_for_boost": ready,
            "trade_days": trade_days,
            "required": {
                "trade_days": SHADOW_MIN_TRADE_DAYS,
                "confirmed_events": SHADOW_MIN_CONFIRMED_EVENTS,
                "hit_rate_3d": SHADOW_MIN_HIT_RATE_3D,
                "human_agreement": SHADOW_MIN_HUMAN_AGREEMENT,
            },
            "confirmed_events": confirmed_events,
            # 确认事件专属命中率（门槛口径）；全体命中率另列供观察
            "hit_rate_3d": hit_rate_3d,
            "hit_rate_3d_all": effectiveness.hit_rate_3d,
            "human_agreement_rate": human_agreement,
        }

    # ------------------------------------------------------------------
    # 内部构件
    # ------------------------------------------------------------------
    def _load_taxonomy(self) -> ThemeTaxonomy | None:
        service = self._service
        raw_path = str(service._config.theme.taxonomy_path).strip()
        path = Path(raw_path)
        if not path.is_absolute():
            # 相对路径锚定仓库根（services 层文件深度是 parents[4]）。
            path = _project_root() / path
        try:
            return load_taxonomy(path)
        except Exception as exc:
            service._record_audit_event(
                event_type="theme_taxonomy_load_failed",
                level="warn",
                payload={
                    "path": str(path),
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:300],
                },
            )
            return None

    def _ledger(self) -> ThemeEventLedger:
        service = self._service
        config = service._config.theme
        return ThemeEventLedger(
            db_path=service._resolve_evolution_path(config.ledger_db_path),
            archive_dir=service._resolve_evolution_path(config.ledger_archive_dir),
            ttl_days=config.ledger_ttl_days,
        )

    def _board_resolver(self) -> BoardResolver:
        service = self._service
        return BoardResolver(
            cache_dir=service._resolve_evolution_path(
                "artifacts/evolution/theme_board_cache"
            ),
            ak_module=getattr(service, "_theme_ak_module", None),
        )

    def _theme_state_path(self) -> Path:
        service = self._service
        resolved = service._resolve_evolution_path(service._config.theme.state_path)
        return cast(Path, resolved)

    def _persist_news_records(
        self,
        *,
        records: list[dict[str, object]],
        now: datetime,
    ) -> dict[str, object]:
        """滚动文件（按 event id 去重）+ 按日幂等归档 + 过期清理。"""
        service = self._service
        config = service._config.theme
        report: dict[str, object] = {
            "latest_written": 0,
            "daily_written": 0,
            "removed_expired": 0,
        }
        try:
            latest_path = service._resolve_evolution_path(config.news_latest_path)
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            merged = _merge_by_id(current=records, existing=_read_jsonl(latest_path))
            _write_jsonl(latest_path, merged)
            report["latest_written"] = len(merged)

            daily_dir = service._resolve_evolution_path(config.news_daily_dir)
            daily_dir.mkdir(parents=True, exist_ok=True)
            day_path = daily_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
            merged_today = _merge_by_id(current=records, existing=_read_jsonl(day_path))
            _write_jsonl(day_path, merged_today)
            report["daily_written"] = len(merged_today)

            retention_days = max(1, int(config.news_daily_retention_days))
            cutoff = now.date().toordinal() - retention_days
            removed = 0
            for candidate in daily_dir.glob("*.jsonl"):
                try:
                    file_date = datetime.strptime(candidate.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_date.toordinal() < cutoff:
                    try:
                        candidate.unlink()
                        removed += 1
                    except OSError:
                        continue
            report["removed_expired"] = removed
        except Exception as exc:
            report["error_type"] = exc.__class__.__name__
            report["error"] = str(exc)[:300]
        return report

    def _build_theme_state(
        self,
        *,
        now: datetime,
        mode: str,
        active_themes: list[dict[str, object]],
        confirmed_theme_ids: list[str],
        theme_symbols: dict[str, list[str]],
        ledger_summary: dict[str, object],
    ) -> dict[str, object]:
        """构造 theme_state.json 载荷。

        boost 模式才生成 boost_by_symbol（主题热度 × 确认强度，衰减由
        ThemeBoostProvider 读取时按 generated_at 判断）；shadow 模式该表为空、
        pinned_pool 记 dry-run 清单供报告展示。
        """
        boost_by_symbol: dict[str, float] = {}
        if mode == "boost" and confirmed_theme_ids:
            for theme in active_themes:
                if not bool(theme.get("active", False)):
                    continue
                heat = _as_float(theme.get("heat"), default=0.0)
                # 确认强度：确认商品数 / 全部商品数（单一商品确认得 1.0 覆盖度时满）
                commodities = theme.get("commodities", [])
                confirmed_by = theme.get("confirmed_by", [])
                commodities_list = commodities if isinstance(commodities, list) else []
                confirmed_list = confirmed_by if isinstance(confirmed_by, list) else []
                strength = (
                    len(confirmed_list) / max(1, len(commodities_list))
                    if commodities_list
                    else 0.0
                )
                theme_boost = min(1.0, heat * strength)
                if theme_boost <= 0:
                    continue
                symbols = theme_symbols.get(str(theme.get("theme_id", "")), [])
                for symbol in symbols:
                    boost_by_symbol[symbol] = max(
                        theme_boost,
                        boost_by_symbol.get(symbol, 0.0),
                    )
        service = self._service
        config = service._config.theme
        pool_cap = max(1, int(config.pinned_max_per_day))
        pinned_pool = build_theme_pool(
            active_theme_symbols={
                theme_id: theme_symbols.get(theme_id, []) for theme_id in confirmed_theme_ids
            },
            pinned_max=pool_cap,
        )
        return {
            "generated_at": now.isoformat(),
            "mode": mode,
            "dry_run": mode != "boost",
            "active_themes": list(confirmed_theme_ids),
            "themes": [
                {
                    "theme_id": item.get("theme_id", ""),
                    "heat": item.get("heat", 0.0),
                    "price_confirmed": bool(item.get("price_confirmed", False)),
                    "confirmed_by": item.get("confirmed_by", []),
                    "symbol_count": item.get("symbol_count", 0),
                }
                for item in active_themes
                if bool(item.get("active", False))
            ],
            "boost_by_symbol": boost_by_symbol,
            "pinned_pool": pinned_pool,
            "ledger": ledger_summary,
        }

    def _write_theme_state(self, *, state: dict[str, object]) -> dict[str, object]:
        """原子写 theme_state.json（tmp + os.replace，照 m7 约定）。"""
        state_path = self._theme_state_path()
        report: dict[str, object] = {"path": str(state_path), "written": True}
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = state_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, state_path)
        except OSError as exc:
            report["written"] = False
            report["error_type"] = exc.__class__.__name__
            report["error"] = str(exc)[:300]
        return report

    def _count_shadow_trade_days(self) -> int:
        """统计 theme 新闻归档覆盖的交易日数（shadow 观察窗口长度）。"""
        service = self._service
        daily_dir = service._resolve_evolution_path(service._config.theme.news_daily_dir)
        if not daily_dir.exists():
            return 0
        count = 0
        for candidate in daily_dir.glob("*.jsonl"):
            try:
                file_date = datetime.strptime(candidate.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if is_a_share_trading_day(file_date):
                count += 1
        return count

    def _human_agreement_rate(self) -> float | None:
        """人工复核一致率（theme_review.jsonl：人工标注 agree/disagree）。

        自建轻量复核回路——现有人工一致率无生产数据路径（M7 传 None 的教训）。
        记录格式（每行一条）：
        ``{"theme_id": ..., "title": ..., "agree": true/false, "reviewed_at": ...}``
        无标注记录时返回 None（门槛判定按"数据不足"处理）。
        """
        service = self._service
        review_path = service._resolve_evolution_path(service._config.theme.review_path)
        if not review_path.exists():
            return None
        agreed = 0
        total = 0
        try:
            with review_path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict) or "agree" not in payload:
                        continue
                    total += 1
                    if bool(payload.get("agree", False)):
                        agreed += 1
        except OSError:
            return None
        if total <= 0:
            return None
        return float(agreed / total)

    def theme_boost_provider(self) -> ThemeBoostProvider:
        """给 pipeline 用的 boost provider（配置驱动路径）。"""
        service = self._service
        return ThemeBoostProvider(config=service._config.theme)


def _merge_by_id(
    *,
    current: list[dict[str, object]],
    existing: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in current + existing:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("id", "") or item.get("title", ""))
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        merged.append(dict(item))
    return merged


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    except OSError:
        return []
    return records


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".jsonl.tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        for item in records:
            fp.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp_path, path)


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
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


def _project_root() -> Path:
    # services 层（src/stock_analyzer/runtime/services/x.py）到仓库根是 4 层
    return Path(__file__).resolve().parents[4]
