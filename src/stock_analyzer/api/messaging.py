"""WeCom / Feishu interaction endpoints and command-processing machinery.

The patched globals (``_feishu_long_connection_runner``,
``_launch_feishu_message_final_reply``, ``FeishuAppNotifier``,
``FeishuLongConnectionRunner``) live on :mod:`stock_analyzer.main`, so this
module resolves them lazily through ``main_module()`` at call time to stay
monkeypatch-compatible with the test suite.
"""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from stock_analyzer.api.deps import (
    as_float,
    as_int,
    get_config,
    get_service,
    is_query_frozen,
    main_module,
    record_service_audit_event,
)
from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.command.feishu_interaction import (
    FeishuMessageEvent,
    feishu_event_type,
    feishu_payload_is_encrypted,
    parse_feishu_message_event,
    parse_feishu_url_verification,
    verify_feishu_token,
)
from stock_analyzer.command.wecom_interaction import (
    WeComCryptoError,
    WeComParsedCommand,
    build_encrypted_reply_xml,
    build_text_reply_xml,
    build_wecom_help_text,
    decrypt_wecom_payload,
    encrypt_wecom_payload,
    format_positions_text,
    format_trades_text,
    parse_text_command,
    parse_wecom_command,
    parse_wecom_xml,
    verify_wecom_signature,
)
from stock_analyzer.notify.channels import NotificationMessage
from stock_analyzer.param_freeze import PARAMS_FROZEN_CODE, freeze_window_label

router = APIRouter()

_config = get_config()
_feishu_long_connection_runner: Any | None = None
_FEISHU_ACK_REPLY_TEXT = "已收到，处理中"


def _feishu_subscription_mode() -> str:
    return _config.feishu_interaction.subscription_mode.strip().lower() or "webhook"


def _prewarm_feishu_app_access_token_if_needed() -> None:
    if not _config.feishu_interaction.enabled:
        return
    app_id = _config.notifications.feishu_app_id.strip()
    app_secret = _config.notifications.feishu_app_secret.strip()
    if not app_id or not app_secret:
        return
    trace_id = "feishu-access-token-prewarm"
    try:
        prewarm_result = main_module().FeishuAppNotifier.prewarm_tenant_access_token(
            app_id=app_id,
            app_secret=app_secret,
            timeout_sec=_config.notifications.timeout_sec,
        )
    except Exception as exc:
        record_service_audit_event(
            event_type="feishu_access_token_prewarm_failed",
            trace_id=trace_id,
            level="warn",
            message="feishu access token prewarm failed",
            payload={"error": str(exc)},
        )
        return
    record_service_audit_event(
        event_type="feishu_access_token_prewarm",
        trace_id=trace_id,
        level="info" if prewarm_result.success else "warn",
        message="feishu access token prewarm attempted",
        payload={"success": prewarm_result.success, "error": prewarm_result.error},
    )


def _feishu_long_connection_status_payload() -> dict[str, object]:
    runner = _feishu_long_connection_runner
    runner_status = (
        runner.status()
        if runner is not None
        else {
            "status": "not_started",
            "thread_alive": False,
            "started_at": "",
            "last_message_at": "",
            "last_error": "",
        }
    )
    return {
        "enabled": _config.feishu_interaction.enabled,
        "subscription_mode": _feishu_subscription_mode(),
        "credentials_ready": bool(
            _config.notifications.feishu_app_id.strip()
            and _config.notifications.feishu_app_secret.strip()
        ),
        "runner": runner_status,
    }


def _handle_feishu_long_connection_message(event: FeishuMessageEvent) -> None:
    trace_id = event.message_id or event.event_id or f"feishu-{int(time.time())}"
    try:
        _process_feishu_message_event(event, source="long_connection")
    except Exception as exc:
        record_service_audit_event(
            event_type="feishu_long_connection_message_error",
            trace_id=trace_id,
            level="error",
            message="feishu long connection message handling failed",
            payload={
                "event_id": event.event_id,
                "message_id": event.message_id,
                "chat_id": event.chat_id,
                "error": str(exc),
            },
        )


def _start_feishu_long_connection_if_needed() -> None:
    global _feishu_long_connection_runner

    cfg = _config.feishu_interaction
    if not cfg.enabled or _feishu_subscription_mode() != "long_connection":
        return
    if _feishu_long_connection_runner is not None:
        status = _feishu_long_connection_runner.status()
        if bool(status.get("thread_alive", False)):
            return

    app_id = _config.notifications.feishu_app_id.strip()
    app_secret = _config.notifications.feishu_app_secret.strip()
    trace_id = "feishu-long-connection"
    if not app_id or not app_secret:
        record_service_audit_event(
            event_type="feishu_long_connection_skipped",
            trace_id=trace_id,
            level="warn",
            message="feishu long connection skipped because credentials are missing",
            payload=_feishu_long_connection_status_payload(),
        )
        return

    runner = main_module().FeishuLongConnectionRunner(
        app_id=app_id,
        app_secret=app_secret,
        message_handler=_handle_feishu_long_connection_message,
        debug=False,
    )
    _feishu_long_connection_runner = runner
    try:
        started = runner.start()
    except Exception as exc:
        record_service_audit_event(
            event_type="feishu_long_connection_start_failed",
            trace_id=trace_id,
            level="error",
            message="feishu long connection failed to start",
            payload={"error": str(exc)},
        )
        return

    record_service_audit_event(
        event_type=(
            "feishu_long_connection_started" if started else "feishu_long_connection_not_started"
        ),
        trace_id=trace_id,
        level="info" if started else "warn",
        message=(
            "feishu long connection startup requested"
            if started
            else "feishu long connection already running"
        ),
        payload=_feishu_long_connection_status_payload(),
    )


def _stop_feishu_long_connection_if_needed() -> None:
    global _feishu_long_connection_runner

    runner = _feishu_long_connection_runner
    if runner is None:
        return

    trace_id = "feishu-long-connection"
    try:
        runner.stop()
        record_service_audit_event(
            event_type="feishu_long_connection_stopped",
            trace_id=trace_id,
            message="feishu long connection stopped",
            payload=runner.status(),
        )
    except Exception as exc:
        record_service_audit_event(
            event_type="feishu_long_connection_stop_failed",
            trace_id=trace_id,
            level="error",
            message="feishu long connection failed to stop cleanly",
            payload={"error": str(exc)},
        )
    finally:
        _feishu_long_connection_runner = None


def _wecom_signature_valid(
    *,
    provided_signature: str,
    timestamp: str,
    nonce: str,
    payload: str,
) -> bool:
    cfg = _config.wecom_interaction
    if not cfg.verify_signature:
        return True
    token = cfg.token.strip()
    if not token:
        return False
    return verify_wecom_signature(
        token=token,
        signature=provided_signature,
        timestamp=timestamp,
        nonce=nonce,
        payload=payload,
    )


def _wecom_user_allowed(user_id: str) -> bool:
    allowed = {
        str(item).strip() for item in _config.wecom_interaction.allowed_users if str(item).strip()
    }
    if not allowed:
        return bool(_config.wecom_interaction.allow_all_users_for_local_dev)
    return user_id in allowed


def _interactive_source_slug(source_user: str) -> str:
    return "".join(
        ch for ch in (source_user or "") if ch.isascii() and (ch.isalnum() or ch in {"-", "_"})
    )


def _interactive_pct(value: object) -> str:
    return f"{as_float(value, default=0.0):.2%}"


def _interactive_num(value: object) -> str:
    raw = as_float(value, default=0.0)
    if abs(raw - int(raw)) < 1e-9:
        return str(int(raw))
    return f"{raw:g}"


def _build_interactive_success_message(
    *,
    action: str,
    payload: dict[str, Any],
    update: dict[str, Any],
) -> str:
    if action == "SET_POSITION":
        symbol = str(update.get("symbol", "") or payload.get("symbol", "")).strip()
        status = str(update.get("status", "")).strip().lower()
        target = update.get("target_position", payload.get("target_position", 0.0))
        manual_fill = update.get("manual_fill")
        title = "已登记买入" if status == "opened" else "已更新持仓"
        parts = [f"仓位 {_interactive_pct(target)}"]
        if isinstance(manual_fill, dict):
            entry_price = as_float(manual_fill.get("entry_price"), default=0.0)
            quantity = as_int(manual_fill.get("quantity"), default=0)
            fee = as_float(manual_fill.get("fee"), default=0.0)
            if entry_price > 0:
                parts.append(f"价格 {_interactive_num(entry_price)}")
            if quantity > 0:
                parts.append(f"数量 {quantity}")
            if fee > 0:
                parts.append(f"手续费 {_interactive_num(fee)}")
        return f"{title}：{symbol}\n" + " | ".join(parts)

    if action == "CLOSE_POSITION":
        symbol = str(update.get("symbol", "") or payload.get("symbol", "")).strip()
        closed = bool(update.get("closed", False))
        if not closed:
            return f"未找到可平仓持仓：{symbol}"
        close_fill = update.get("close_fill")
        parts = []
        if isinstance(close_fill, dict):
            exit_price = as_float(close_fill.get("exit_price"), default=0.0)
            quantity = as_int(close_fill.get("quantity"), default=0)
            fee = as_float(close_fill.get("fee"), default=0.0)
            if exit_price > 0:
                parts.append(f"价格 {_interactive_num(exit_price)}")
            if quantity > 0:
                parts.append(f"数量 {quantity}")
            if fee > 0:
                parts.append(f"手续费 {_interactive_num(fee)}")
        return f"已登记卖出：{symbol}\n" + " | ".join(parts) if parts else f"已登记卖出：{symbol}"

    if action == "CLOSE_ALL_POSITIONS":
        closed_count = as_int(update.get("closed_count"), default=0)
        return f"已全平持仓，共 {closed_count} 条"

    if action == "SET_BROKER_POSITIONS":
        snapshot = update.get("snapshot", {})
        if isinstance(snapshot, dict):
            broker_positions = as_int(snapshot.get("broker_positions"), default=0)
            return f"已同步券商持仓，共 {broker_positions} 条"
        return "已同步券商持仓"

    if action == "RUN_RECONCILE":
        report = update.get("report", {})
        if isinstance(report, dict):
            status = str(report.get("status", "")).strip() or "unknown"
            mismatch = as_int(report.get("mismatch_count"), default=0)
            return f"对账完成：状态 {status}，差异 {mismatch} 项"
        return "对账完成"

    if action == "SET_RECOMMENDATION_STATUS":
        symbol = str(update.get("symbol", "") or payload.get("symbol", "")).strip()
        status = str(update.get("status", "") or payload.get("status", "")).strip() or "unknown"
        return f"已更新跟踪状态：{symbol} -> {status}"

    if action == "PAUSE_NEW_BUY":
        return "已暂停开仓"
    if action == "RESUME_NEW_BUY":
        return "已恢复开仓"
    if action == "RESET_SIM_ACCOUNT":
        equity = as_float(update.get("current_equity"), default=1.0)
        trades_before = as_int(update.get("trades_before"), default=0)
        return f"已重置模拟账户：净值 {equity:.4f}，清除历史成交 {trades_before} 笔"
    if action == "SET_EQUITY":
        equity = as_float(
            update.get("current_equity"),
            default=as_float(payload.get("current_equity"), default=1.0),
        )
        return f"已更新净值：{equity:.4f}"

    return f"已执行：{action}"


def _normalize_interactive_command(parsed: WeComParsedCommand) -> tuple[WeComParsedCommand, str]:
    if parsed.kind != "execute" or parsed.action != "SET_POSITION":
        return parsed, ""

    payload = dict(parsed.payload)
    target_position = as_float(payload.get("target_position"), default=0.0)
    if target_position > 0.0:
        payload["target_position"] = round(target_position, 6)
        return (
            WeComParsedCommand(
                kind=parsed.kind,
                action=parsed.action,
                payload=payload,
                query=parsed.query,
                error=parsed.error,
            ),
            "",
        )

    entry_price = as_float(payload.get("entry_price"), default=0.0)
    quantity = as_int(payload.get("quantity"), default=0)
    total_asset = as_float(payload.get("total_asset"), default=0.0)
    if total_asset <= 0.0:
        total_asset = as_float(_config.dashboard.default_total_asset, default=0.0)
    if entry_price <= 0.0 or quantity <= 0:
        return (
            parsed,
            "买入命令缺少仓位，请提供“仓位20%”，或提供“价格/数量/总资产”让系统自动推算仓位",
        )
    if total_asset <= 0.0:
        return (
            parsed,
            "无法自动推算仓位，请补充“总资产100000”，或先配置 dashboard.default_total_asset",
        )

    inferred_target = round((entry_price * quantity) / total_asset, 6)
    if inferred_target <= 0.0:
        return (parsed, "无法根据价格、数量和总资产推算有效仓位")
    if inferred_target > 1.0:
        return (parsed, "推算出的仓位超过 100%，请检查价格、数量或总资产是否填写正确")

    payload["target_position"] = inferred_target
    return (
        WeComParsedCommand(
            kind=parsed.kind,
            action=parsed.action,
            payload=payload,
            query=parsed.query,
            error=parsed.error,
        ),
        "",
    )


def _interactive_execute(
    *,
    parsed: WeComParsedCommand,
    source_user: str,
    channel_name: str,
    auto_reconcile_after_snapshot: bool,
) -> str:
    normalized_parsed, error = _normalize_interactive_command(parsed)
    if error:
        return error

    safe_user = _interactive_source_slug(source_user)
    command_id = f"{channel_name}-{safe_user or 'user'}-{int(time.time())}-{uuid4().hex[:6]}"
    envelope = _build_internal_command(
        action=normalized_parsed.action,
        payload=normalized_parsed.payload,
        command_id=command_id,
    )
    result = get_service().execute_command(envelope)
    accepted = bool(result.get("accepted", False))
    if not accepted:
        code = str(result.get("code", "")).strip() or "unknown"
        message = str(result.get("message", "")).strip() or "no message"
        return f"执行失败: {normalized_parsed.action} code={code} message={message}"

    update = result.get("command_update", {})
    if isinstance(update, dict):
        success_message = _build_interactive_success_message(
            action=normalized_parsed.action,
            payload=normalized_parsed.payload,
            update=update,
        )
    else:
        success_message = _build_interactive_success_message(
            action=normalized_parsed.action,
            payload=normalized_parsed.payload,
            update={},
        )
    lines = [success_message]
    if isinstance(update, dict):
        pass

    if normalized_parsed.action == "SET_BROKER_POSITIONS" and auto_reconcile_after_snapshot:
        reconcile_envelope = _build_internal_command(
            action="RUN_RECONCILE",
            payload={},
            command_id=f"{envelope.command_id}-reconcile",
        )
        reconcile_result = get_service().execute_command(reconcile_envelope)
        reconcile_update = reconcile_result.get("command_update", {})
        if isinstance(reconcile_update, dict):
            report = reconcile_update.get("report", {})
            if isinstance(report, dict):
                status = str(report.get("status", "")).strip() or "unknown"
                mismatch = int(report.get("mismatch_count", 0))
                lines.append(f"自动对账：状态 {status}，差异 {mismatch} 项")
    return "\n".join(lines)


def _build_internal_command(
    action: str,
    payload: dict[str, Any],
    command_id: str = "",
) -> CommandEnvelope:
    now_ts = int(time.time())
    normalized_action = action.strip()
    action_code = normalized_action.lower().replace(" ", "_")
    generated_id = command_id.strip() or f"dash-{action_code}-{now_ts}-{uuid4().hex[:8]}"
    signature = SignedCommandProcessor.build_signature(
        secret_key=_config.command_channel.secret_key,
        command_id=generated_id,
        timestamp=now_ts,
        action=normalized_action,
        payload=payload,
    )
    return CommandEnvelope(
        command_id=generated_id,
        timestamp=now_ts,
        action=normalized_action,
        payload=payload,
        signature=signature,
    )


def _wecom_execute(parsed: WeComParsedCommand, source_user: str) -> str:
    return _interactive_execute(
        parsed=parsed,
        source_user=source_user,
        channel_name="wecom",
        auto_reconcile_after_snapshot=_config.wecom_interaction.auto_reconcile_after_broker_snapshot,
    )


def _wecom_handle_command(parsed: WeComParsedCommand, source_user: str) -> str:
    if parsed.kind == "help":
        return build_wecom_help_text()
    if parsed.kind == "invalid":
        return f"无法识别命令: {parsed.error}\n{build_wecom_help_text()}"
    if parsed.kind == "query":
        service = get_service()
        if parsed.query == "positions":
            return format_positions_text(service.portfolio_positions())
        if parsed.query == "trades":
            return format_trades_text(service.portfolio_trades(limit=8))
        if parsed.query == "news_score":
            symbol = str(parsed.payload.get("symbol", "")).strip()
            strategy = str(parsed.payload.get("strategy", "trend")).strip() or "trend"
            payload = service.preview_news_component(symbol=symbol, strategy=strategy)
            score = as_float(payload.get("news_component", 0.5), default=0.5)
            status = str(payload.get("status", "unknown")).strip() or "unknown"
            reasons = payload.get("reasons", [])
            reason_text = ""
            if isinstance(reasons, list) and reasons:
                reason_text = str(reasons[0])
            return f"news_score {symbol} {strategy}={score:.3f} status={status}" + (
                f" reason={reason_text}" if reason_text else ""
            )
        if parsed.query == "news_watchlist":
            raw_limit = parsed.payload.get("limit", 10)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            strategy = str(parsed.payload.get("strategy", "trend")).strip() or "trend"
            payload = service.preview_news_watchlist(strategy=strategy, limit=limit)
            items = payload.get("items", [])
            source = str(payload.get("source", "watchlist")).strip() or "watchlist"
            records = as_int(payload.get("records", 0))
            summary = payload.get("summary", {})
            avg = 0.5
            if isinstance(summary, dict):
                avg = as_float(summary.get("average_news_component", 0.5), default=0.5)
            lines = [
                "news_watchlist strategy="
                f"{strategy} records={records} avg={avg:.3f} source={source}"
            ]
            if isinstance(items, list):
                for item in items[:5]:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip()
                    score = float(item.get("news_component", 0.5))
                    status = str(item.get("status", "")).strip() or "unknown"
                    lines.append(f"- {symbol} {score:.3f} {status}")
            return "\n".join(lines)
        if parsed.query == "news_cache_state":
            payload = service.news_score_cache_state()
            entries = as_int(payload.get("entries", 0))
            ttl_sec = as_int(payload.get("ttl_sec", 0))
            return f"news_cache entries={entries} ttl_sec={ttl_sec}"
        if parsed.query == "news_cache_clear":
            symbol = str(parsed.payload.get("symbol", "")).strip()
            strategy = str(parsed.payload.get("strategy", "")).strip().lower()
            payload = service.clear_news_score_cache(symbol=symbol, strategy=strategy)
            cleared = as_int(payload.get("cleared", 0))
            remaining = as_int(payload.get("remaining", 0))
            return (
                f"news_cache_clear symbol={symbol or '*'} strategy={strategy or '*'} "
                f"cleared={cleared} remaining={remaining}"
            )
        if parsed.query == "news_history":
            raw_limit = parsed.payload.get("limit", 10)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            symbol = str(parsed.payload.get("symbol", "")).strip()
            strategy = str(parsed.payload.get("strategy", "")).strip().lower()
            payload = service.news_score_history(
                limit=limit,
                symbol=symbol,
                strategy=strategy,
            )
            records = as_int(payload.get("records", 0))
            summary = payload.get("summary", {})
            avg = 0.5
            if isinstance(summary, dict):
                avg = as_float(summary.get("average_news_component", 0.5), default=0.5)
            lines = [
                f"news_history records={records} avg={avg:.3f} "
                f"symbol={symbol or '*'} strategy={strategy or '*'}"
            ]
            items = payload.get("items", [])
            if isinstance(items, list):
                for item in items[-5:]:
                    if not isinstance(item, dict):
                        continue
                    row_symbol = str(item.get("symbol", "")).strip()
                    row_strategy = str(item.get("strategy", "")).strip() or "trend"
                    row_score = float(item.get("news_component", 0.5))
                    lines.append(f"- {row_symbol} {row_strategy} {row_score:.3f}")
            return "\n".join(lines)
        if parsed.query == "execution_mode_state":
            advisory = bool(_config.app.advisory_only)
            mode_text = "advisory_only" if advisory else "portfolio_auto_apply"
            return f"execution_mode={mode_text} advisory_only={str(advisory).lower()}"
        if parsed.query == "execution_mode_set":
            if is_query_frozen("execution_mode_set"):
                return (
                    f"{PARAMS_FROZEN_CODE}: execution_mode_set 在交易时段冻结 "
                    f"({freeze_window_label(_config.param_freeze)})，收盘后生效"
                )
            advisory = bool(parsed.payload.get("advisory_only", False))
            _config.app.advisory_only = advisory
            mode_text = "advisory_only" if advisory else "portfolio_auto_apply"
            return f"execution_mode_set advisory_only={str(advisory).lower()} mode={mode_text}"
        if parsed.query == "recommendation_lifecycle":
            status = str(parsed.payload.get("status", "")).strip().lower()
            raw_limit = parsed.payload.get("limit", 10)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            payload = service.recommendation_lifecycle(status=status, limit=limit)
            summary = payload.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            breakdown = summary.get("status_breakdown", {})
            if not isinstance(breakdown, dict):
                breakdown = {}
            breakdown_text = (
                ",".join(f"{key}:{int(value)}" for key, value in sorted(breakdown.items())) or "-"
            )
            lines = [
                "lifecycle records="
                f"{as_int(payload.get('records', 0))} status={status or 'all'} "
                f"breakdown={breakdown_text}"
            ]
            items = payload.get("items", [])
            if isinstance(items, list):
                for item in items[:5]:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip()
                    row_status = str(item.get("status", "")).strip()
                    strategy = str(item.get("strategy", "")).strip() or "manual"
                    updated_at = str(item.get("updated_at", "")).strip()
                    lines.append(f"- {symbol} {row_status} {strategy} {updated_at}")
            return "\n".join(lines)
        if parsed.query == "holding_alerts":
            severity = str(parsed.payload.get("severity", "")).strip().lower()
            payload = service.holding_alerts(now=datetime.now())
            items = payload.get("items", [])
            if not isinstance(items, list):
                items = []
            if severity:
                items = [
                    item
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("severity", "")).strip().lower() == severity
                ]
            lines = [f"holding_alerts severity={severity or 'all'} records={len(items)}"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                reason = str(item.get("reason", "")).strip()
                level = str(item.get("severity", "")).strip()
                pnl_pct = float(item.get("pnl_pct", 0.0)) * 100.0
                lines.append(f"- {symbol} {level} {reason} pnl={pnl_pct:.2f}%")
            return "\n".join(lines)
        if parsed.query == "execution_bias":
            raw_days = parsed.payload.get("days", 7)
            raw_limit = parsed.payload.get("limit", 10)
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                days = 7
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 10
            days = max(1, min(days, 90))
            limit = max(1, min(limit, 50))
            payload = service.execution_bias_report(days=days, limit=limit)
            summary = payload.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            lines = [
                f"execution_bias days={days} records={as_int(payload.get('records', 0))} "
                f"avg_pos={as_float(summary.get('avg_abs_position_bias', 0.0)):.4f} "
                f"avg_price={as_float(summary.get('avg_abs_price_bias_pct', 0.0)) * 100:.2f}% "
                f"better_rate={as_float(summary.get('better_price_rate', 0.0)):.2%} "
                f"worse_rate={as_float(summary.get('worse_price_rate', 0.0)):.2%}"
            ]
            items = payload.get("items", [])
            if isinstance(items, list):
                for item in items[:5]:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip()
                    pos_bias = float(item.get("position_bias", 0.0))
                    price_bias = float(item.get("price_bias_pct", 0.0)) * 100.0
                    lines.append(
                        f"- {symbol} position_bias={pos_bias:.4f} price_bias={price_bias:.2f}%"
                    )
            return "\n".join(lines)
        return "unsupported query"
    if parsed.kind == "execute":
        return _wecom_execute(parsed=parsed, source_user=source_user)
    return "unsupported command kind"


def _feishu_user_allowed(event: FeishuMessageEvent) -> bool:
    allowed = {
        str(item).strip() for item in _config.feishu_interaction.allowed_users if str(item).strip()
    }
    if not allowed:
        return bool(_config.feishu_interaction.allow_all_users_for_local_dev)
    candidate_ids = {event.open_id, event.user_id, event.union_id}
    return bool({item for item in candidate_ids if item} & allowed)


def _feishu_source_user(event: FeishuMessageEvent) -> str:
    return event.open_id or event.user_id or event.union_id or event.chat_id or "user"


def _feishu_handle_command(parsed: WeComParsedCommand, source_user: str) -> str:
    if parsed.kind == "execute":
        return _interactive_execute(
            parsed=parsed,
            source_user=source_user,
            channel_name="feishu",
            auto_reconcile_after_snapshot=_config.feishu_interaction.auto_reconcile_after_broker_snapshot,
        )
    return _wecom_handle_command(parsed=parsed, source_user=source_user)


def _build_feishu_final_reply(event: FeishuMessageEvent) -> str:
    if not _feishu_user_allowed(event):
        return "当前账号没有执行权限"
    if event.message_type != "text":
        return "仅支持文本指令，输入 帮助 查看可用命令"
    parsed = parse_text_command(event.text)
    return _feishu_handle_command(parsed=parsed, source_user=_feishu_source_user(event))


def _feishu_reply_stage_cache_key(trace_id: str, stage: str) -> str:
    normalized_trace_id = trace_id.strip()
    normalized_stage = stage.strip().lower()
    if not normalized_trace_id or not normalized_stage:
        return ""
    return f"feishu:reply:{normalized_stage}:{normalized_trace_id}"


def _send_feishu_chat_reply(
    *,
    chat_id: str,
    reply: str,
    trace_id: str = "",
    stage: str = "final",
    source: str = "",
    message_id: str = "",
) -> dict[str, object]:
    normalized_stage = stage.strip().lower() or "final"
    stage_cache_key = _feishu_reply_stage_cache_key(trace_id, normalized_stage)
    service = get_service()
    if stage_cache_key and service._cache.exists(stage_cache_key):
        record_service_audit_event(
            event_type="feishu_reply_skipped_duplicate",
            trace_id=trace_id,
            message="feishu reply skipped because this stage was already sent",
            payload={
                "source": source,
                "stage": normalized_stage,
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )
        return {
            "success": False,
            "channel": "feishu_app",
            "error": "duplicate_reply_stage",
        }

    notifier = main_module().FeishuAppNotifier(
        app_id=_config.notifications.feishu_app_id,
        app_secret=_config.notifications.feishu_app_secret,
        receive_id=chat_id,
        receive_id_type="chat_id",
        timeout_sec=_config.notifications.timeout_sec,
    )
    message = NotificationMessage(
        title="",
        content=reply,
        level="info",
        trace_id=trace_id,
    )
    delivery_mode = "chat_send"
    if message_id.strip():
        result = notifier.reply_text_message(
            message_id=message_id,
            message=message,
        )
        delivery_mode = "message_reply"
    else:
        result = notifier.send(message)

    if not result.success and message_id.strip():
        fallback_result = notifier.send(message)
        if fallback_result.success:
            result = fallback_result
            delivery_mode = "chat_send_fallback"
    result_payload = result.to_dict()
    if stage_cache_key and bool(result_payload.get("success", False)):
        service._cache.set(
            stage_cache_key,
            "1",
            ttl_sec=max(60, int(_config.command_channel.dedup_ttl_sec)),
        )
    record_service_audit_event(
        event_type="feishu_message_ack_sent"
        if normalized_stage == "ack"
        else "feishu_message_replied",
        trace_id=trace_id,
        level="info" if bool(result_payload.get("success", False)) else "warn",
        message="feishu ack sent" if normalized_stage == "ack" else "feishu final reply sent",
        payload={
            "source": source,
            "stage": normalized_stage,
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_success": bool(result_payload.get("success", False)),
            "reply_channel": str(result_payload.get("channel", "")),
            "reply_error": str(result_payload.get("error", "")),
            "delivery_mode": delivery_mode,
        },
    )
    return result_payload


def _process_feishu_message_event_async(
    event: FeishuMessageEvent,
    *,
    source: str,
    trace_id: str,
) -> None:
    try:
        reply = _build_feishu_final_reply(event)
    except Exception as exc:
        record_service_audit_event(
            event_type="feishu_message_processing_error",
            trace_id=trace_id,
            level="error",
            message="feishu message processing failed",
            payload={
                "source": source,
                "event_id": event.event_id,
                "message_id": event.message_id,
                "chat_id": event.chat_id,
                "error": str(exc),
            },
        )
        reply = "处理失败，请稍后重试"

    _send_feishu_chat_reply(
        chat_id=event.chat_id,
        reply=reply,
        trace_id=trace_id,
        stage="final",
        source=source,
        message_id=event.message_id,
    )


def _launch_feishu_message_final_reply(
    event: FeishuMessageEvent,
    *,
    source: str,
    trace_id: str,
) -> None:
    worker = threading.Thread(
        target=_process_feishu_message_event_async,
        kwargs={"event": event, "source": source, "trace_id": trace_id},
        name=f"feishu-final-{trace_id[-12:]}",
        daemon=True,
    )
    worker.start()


def _process_feishu_message_event(event: FeishuMessageEvent, *, source: str) -> dict[str, object]:
    source_name = source.strip() or "unknown"
    dedup_token = event.message_id or event.event_id
    dedup_key = f"feishu:message:{dedup_token}" if dedup_token else ""
    trace_id = event.message_id or event.event_id or f"feishu-{int(time.time())}"
    service = get_service()
    if dedup_key and service._cache.exists(dedup_key):
        record_service_audit_event(
            event_type="feishu_message_duplicate",
            trace_id=trace_id,
            message="duplicate feishu message ignored",
            payload={
                "source": source_name,
                "event_id": event.event_id,
                "message_id": event.message_id,
                "chat_id": event.chat_id,
            },
        )
        return {"code": 0, "msg": "ok"}
    if dedup_key:
        service._cache.set(
            dedup_key,
            "1",
            ttl_sec=max(60, int(_config.command_channel.dedup_ttl_sec)),
        )

    record_service_audit_event(
        event_type="feishu_message_received",
        trace_id=trace_id,
        message="feishu message received",
        payload={
            "source": source_name,
            "event_id": event.event_id,
            "message_id": event.message_id,
            "chat_id": event.chat_id,
            "chat_type": event.chat_type,
            "message_type": event.message_type,
            "open_id": event.open_id,
            "user_id": event.user_id,
            "union_id": event.union_id,
        },
    )

    if event.sender_type and event.sender_type != "user":
        return {"code": 0, "msg": "ok"}

    _send_feishu_chat_reply(
        chat_id=event.chat_id,
        reply=_FEISHU_ACK_REPLY_TEXT,
        trace_id=trace_id,
        stage="ack",
        source=source_name,
        message_id=event.message_id,
    )
    try:
        main_module()._launch_feishu_message_final_reply(
            event,
            source=source_name,
            trace_id=trace_id,
        )
    except Exception as exc:
        record_service_audit_event(
            event_type="feishu_message_dispatch_error",
            trace_id=trace_id,
            level="error",
            message="feishu message async dispatch failed",
            payload={
                "source": source_name,
                "event_id": event.event_id,
                "message_id": event.message_id,
                "chat_id": event.chat_id,
                "error": str(exc),
            },
        )
        _send_feishu_chat_reply(
            chat_id=event.chat_id,
            reply="处理失败，请稍后重试",
            trace_id=trace_id,
            stage="final",
            source=source_name,
            message_id=event.message_id,
        )
    return {"code": 0, "msg": "ok"}


@router.get("/wecom/callback")
def wecom_callback_verify(
    msg_signature: str = Query(default=""),
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
) -> PlainTextResponse:
    cfg = _config.wecom_interaction
    if not cfg.enabled:
        return PlainTextResponse("wecom_interaction_disabled", status_code=403)

    provided_signature = msg_signature.strip() or signature.strip()
    if cfg.encoding_aes_key.strip():
        if not _wecom_signature_valid(
            provided_signature=provided_signature,
            timestamp=timestamp,
            nonce=nonce,
            payload=echostr,
        ):
            return PlainTextResponse("invalid_signature", status_code=403)
        try:
            plain_echo, _receive_id = decrypt_wecom_payload(
                encrypted=echostr,
                encoding_aes_key=cfg.encoding_aes_key,
                expected_receive_id=cfg.receive_id,
                enforce_receive_id=cfg.enforce_receive_id,
            )
        except WeComCryptoError:
            return PlainTextResponse("invalid_echostr", status_code=403)
        return PlainTextResponse(plain_echo or "ok")

    if not _wecom_signature_valid(
        provided_signature=provided_signature,
        timestamp=timestamp,
        nonce=nonce,
        payload=echostr,
    ):
        return PlainTextResponse("invalid_signature", status_code=403)
    return PlainTextResponse(echostr or "ok")


@router.post("/wecom/callback")
async def wecom_callback(
    request: Request,
    msg_signature: str = Query(default=""),
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
) -> Response:
    cfg = _config.wecom_interaction
    if not cfg.enabled:
        return PlainTextResponse("wecom_interaction_disabled", status_code=403)

    body = await request.body()
    xml_body = body.decode("utf-8", errors="ignore")
    try:
        outer = parse_wecom_xml(xml_body)
    except ValueError:
        return PlainTextResponse("invalid_xml", status_code=400)

    encrypted_body = str(outer.get("Encrypt", "")).strip()
    signature_payload = encrypted_body if encrypted_body else xml_body
    provided_signature = msg_signature.strip() or signature.strip()
    if not _wecom_signature_valid(
        provided_signature=provided_signature,
        timestamp=timestamp,
        nonce=nonce,
        payload=signature_payload,
    ):
        return PlainTextResponse("invalid_signature", status_code=403)

    payload: dict[str, str]
    if encrypted_body:
        if not cfg.encoding_aes_key.strip():
            return PlainTextResponse("missing_encoding_aes_key", status_code=400)
        try:
            decrypted_xml, _receive_id = decrypt_wecom_payload(
                encrypted=encrypted_body,
                encoding_aes_key=cfg.encoding_aes_key,
                expected_receive_id=cfg.receive_id,
                enforce_receive_id=cfg.enforce_receive_id,
            )
            payload = parse_wecom_xml(decrypted_xml)
        except (WeComCryptoError, ValueError):
            return PlainTextResponse("invalid_encrypted_payload", status_code=400)
    else:
        payload = outer

    from_user = str(payload.get("FromUserName", "")).strip()
    to_user = str(payload.get("ToUserName", "")).strip() or "stock-analyzer"
    msg_type = str(payload.get("MsgType", "")).strip().lower()
    content = str(payload.get("Content", "")).strip()

    if not _wecom_user_allowed(from_user):
        reply = "当前账号没有执行权限"
    elif msg_type != "text":
        reply = "仅支持文本指令，输入 帮助 查看可用命令"
    else:
        parsed = parse_wecom_command(content)
        reply = _wecom_handle_command(parsed=parsed, source_user=from_user)

    plain_reply_xml = build_text_reply_xml(
        to_user=from_user or to_user,
        from_user=to_user,
        content=reply,
    )
    if not encrypted_body:
        return Response(content=plain_reply_xml, media_type="application/xml")

    if not cfg.token.strip():
        return PlainTextResponse("missing_token_for_encrypted_reply", status_code=400)
    reply_receive_id = cfg.receive_id.strip() or to_user
    if not reply_receive_id:
        return PlainTextResponse("missing_receive_id_for_encrypted_reply", status_code=400)
    try:
        encrypted_reply = encrypt_wecom_payload(
            plain_text=plain_reply_xml,
            encoding_aes_key=cfg.encoding_aes_key,
            receive_id=reply_receive_id,
        )
    except WeComCryptoError:
        return PlainTextResponse("encrypt_reply_failed", status_code=500)

    wrapped_xml = build_encrypted_reply_xml(
        token=cfg.token,
        encrypt=encrypted_reply,
        timestamp=timestamp.strip() or None,
        nonce=nonce.strip() or None,
    )
    return Response(content=wrapped_xml, media_type="application/xml")


@router.get("/feishu/long_connection/status")
def feishu_long_connection_status() -> dict[str, object]:
    return _feishu_long_connection_status_payload()


@router.post("/feishu/callback")
async def feishu_callback(request: Request) -> Any:
    cfg = _config.feishu_interaction
    if not cfg.enabled:
        return JSONResponse(
            {"code": 403, "msg": "feishu_interaction_disabled"},
            status_code=403,
        )
    if _feishu_subscription_mode() != "webhook":
        return JSONResponse(
            {"code": 403, "msg": "feishu_webhook_mode_disabled"},
            status_code=403,
        )

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"code": 400, "msg": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"code": 400, "msg": "invalid_json"}, status_code=400)
    if feishu_payload_is_encrypted(payload):
        return JSONResponse(
            {"code": 400, "msg": "encrypted_payload_unsupported"},
            status_code=400,
        )
    if not verify_feishu_token(payload, cfg.verification_token):
        return JSONResponse(
            {"code": 403, "msg": "invalid_verification_token"},
            status_code=403,
        )

    verification = parse_feishu_url_verification(payload)
    if verification is not None:
        return {"challenge": verification.challenge}

    event_type = feishu_event_type(payload)
    if event_type and event_type != "im.message.receive_v1":
        return {"code": 0, "msg": "ignored"}

    try:
        event = parse_feishu_message_event(payload)
    except ValueError as exc:
        return JSONResponse({"code": 400, "msg": str(exc)}, status_code=400)
    return _process_feishu_message_event(event, source="webhook")
