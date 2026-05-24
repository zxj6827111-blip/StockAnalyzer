"""FastAPI application entrypoint."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    class BaseModel:
        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def Field(
        default: Any = ...,
        *,
        default_factory: Any | None = None,
        **kwargs: Any,
    ) -> Any: ...
else:
    from pydantic import BaseModel, Field

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.command.feishu_interaction import (
    FeishuMessageEvent,
    feishu_event_type,
    feishu_payload_is_encrypted,
    parse_feishu_message_event,
    parse_feishu_url_verification,
    verify_feishu_token,
)
from stock_analyzer.command.feishu_long_connection import FeishuLongConnectionRunner
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
from stock_analyzer.config import get_config
from stock_analyzer.notify.channels import FeishuAppNotifier, NotificationMessage
from stock_analyzer.runtime.service import StockAnalyzerService

_config = get_config()
_service = StockAnalyzerService(config=_config)
_dashboard_ops_enabled = _config.app.mode.strip().lower() == "simulation"
_feishu_long_connection_runner: FeishuLongConnectionRunner | None = None
_FEISHU_ACK_REPLY_TEXT = "已收到，处理中"


@asynccontextmanager
async def _app_lifespan(_app: FastAPI) -> Any:
    _prewarm_feishu_app_access_token_if_needed()
    _start_feishu_long_connection_if_needed()
    try:
        yield
    finally:
        _stop_feishu_long_connection_if_needed()


app = FastAPI(title="StockAnalyzer API", version="0.1.0", lifespan=_app_lifespan)


def _resolve_frontend_dist_dir(project_root: Path | None = None) -> Path | None:
    root = project_root or Path(__file__).resolve().parents[2]
    candidates = (
        root / "frontend_dist",
        root / "frontend" / "dist",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


_frontend_dist_dir = _resolve_frontend_dist_dir()
_frontend_assets_dir = _frontend_dist_dir / "assets" if _frontend_dist_dir is not None else None


def _as_float(value: object, default: float = 0.0) -> float:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_optional_datetime(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    return datetime.fromisoformat(value)


def _record_service_audit_event(
    *,
    event_type: str,
    trace_id: str,
    level: str = "info",
    message: str = "",
    payload: dict[str, object] | None = None,
) -> None:
    recorder = getattr(_service, "_record_audit_event", None)
    if not callable(recorder):
        return
    recorder(
        event_type=event_type,
        trace_id=trace_id,
        level=level,
        message=message,
        payload=payload or {},
    )


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
        prewarm_result = FeishuAppNotifier.prewarm_tenant_access_token(
            app_id=app_id,
            app_secret=app_secret,
            timeout_sec=_config.notifications.timeout_sec,
        )
    except Exception as exc:
        _record_service_audit_event(
            event_type="feishu_access_token_prewarm_failed",
            trace_id=trace_id,
            level="warn",
            message="feishu access token prewarm failed",
            payload={"error": str(exc)},
        )
        return
    _record_service_audit_event(
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
        _record_service_audit_event(
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
        _record_service_audit_event(
            event_type="feishu_long_connection_skipped",
            trace_id=trace_id,
            level="warn",
            message="feishu long connection skipped because credentials are missing",
            payload=_feishu_long_connection_status_payload(),
        )
        return

    runner = FeishuLongConnectionRunner(
        app_id=app_id,
        app_secret=app_secret,
        message_handler=_handle_feishu_long_connection_message,
        debug=False,
    )
    _feishu_long_connection_runner = runner
    try:
        started = runner.start()
    except Exception as exc:
        _record_service_audit_event(
            event_type="feishu_long_connection_start_failed",
            trace_id=trace_id,
            level="error",
            message="feishu long connection failed to start",
            payload={"error": str(exc)},
        )
        return

    _record_service_audit_event(
        event_type="feishu_long_connection_started" if started else "feishu_long_connection_not_started",
        trace_id=trace_id,
        level="info" if started else "warn",
        message="feishu long connection startup requested" if started else "feishu long connection already running",
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
        _record_service_audit_event(
            event_type="feishu_long_connection_stopped",
            trace_id=trace_id,
            message="feishu long connection stopped",
            payload=runner.status(),
        )
    except Exception as exc:
        _record_service_audit_event(
            event_type="feishu_long_connection_stop_failed",
            trace_id=trace_id,
            level="error",
            message="feishu long connection failed to stop cleanly",
            payload={"error": str(exc)},
        )
    finally:
        _feishu_long_connection_runner = None



if _frontend_assets_dir is not None and _frontend_assets_dir.exists():
    app.mount("/ui/assets", StaticFiles(directory=str(_frontend_assets_dir)), name="ui-assets")


if _frontend_dist_dir is not None:
    _resolved_frontend_dist_dir = _frontend_dist_dir

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    @app.get("/ui/{path:path}", include_in_schema=False)
    def frontend_ui_page(path: str = "") -> Response:
        requested = path.strip().lstrip("/")
        if requested:
            candidate = (_resolved_frontend_dist_dir / requested).resolve()
            try:
                candidate.relative_to(_resolved_frontend_dist_dir.resolve())
            except ValueError:
                return FileResponse(str(_resolved_frontend_dist_dir / "index.html"))
            if candidate.is_file():
                return FileResponse(str(candidate))
        return FileResponse(str(_resolved_frontend_dist_dir / "index.html"))

_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>StockAnalyzer Control Deck | 核心控制台</title>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #081826;
      --panel: rgba(15, 36, 52, 0.86);
      --panel-border: rgba(94, 136, 170, 0.35);
      --ink: #d5f3ff;
      --muted: #8fb6cb;
      --accent: #41d6b3;
      --warn: #ff7f50;
      --good: #4ddf7e;
      --bad: #ff6f59;
      --shadow: 0 18px 40px rgba(1, 14, 23, 0.45);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: inherit;
      background: radial-gradient(circle at 15% 20%, rgba(65, 214, 179, 0.25), transparent 35%), linear-gradient(135deg, #061420 0%, #0a2234 52%, #0f1f2a 100%);
      min-height: 100vh;
      padding: 24px 16px 40px;
    }
    .container { max-width: 1000px; margin: 0 auto; display: grid; gap: 14px; }
    .hero { background: linear-gradient(145deg, rgba(17, 43, 62, 0.9), rgba(10, 25, 37, 0.94)); border: 1px solid var(--panel-border); border-radius: 18px; padding: 20px; box-shadow: var(--shadow); }
    .hero h1 { margin: 0; font-size: 1.8rem; }
    .toolbar { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .btn { border: 0; border-radius: 999px; padding: 9px 16px; background: linear-gradient(110deg, #38d2ae, #61e1c4); color: #062437; font-weight: bold; cursor: pointer; transition: 0.2s; text-decoration: none; display: inline-flex; align-items: center; }
    .btn.danger { background: linear-gradient(110deg, #ff7f50, #ff6f59); color: white; }
    .btn:hover { transform: translateY(-1px); filter: brightness(1.05); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .status { font-family: monospace; font-size: 0.9rem; padding: 5px 10px; border-radius: 999px; border: 1px solid rgba(115, 167, 201, 0.35); background: rgba(16, 43, 61, 0.66); color: var(--muted); }
    .cards { display: grid; gap: 12px; grid-template-columns: repeat(4, 1fr); }
    .card, .panel { background: var(--panel); border: 1px solid var(--panel-border); border-radius: 14px; padding: 14px; box-shadow: var(--shadow); }
    .label { color: var(--muted); font-size: 0.85rem; letter-spacing: 0.05em; font-weight: bold;}
    .value { margin-top: 7px; font-size: 1.5rem; font-weight: 700; }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .grid { display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }
    .panel h2 { margin: 2px 0 12px; font-size: 1.1rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
    th, td { border-bottom: 1px solid rgba(108, 158, 190, 0.25); text-align: left; padding: 8px 6px; }
    th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }
    .list { max-height: 250px; overflow: auto; display: flex; flex-direction: column; gap: 8px; }
    .item { border: 1px solid rgba(108, 158, 190, 0.25); border-radius: 10px; padding: 10px; background: rgba(12, 33, 48, 0.66); font-size: 0.9rem; }
    .item .meta { margin-top: 4px; color: var(--muted); font-size: 0.8rem; }
    .trace-input { padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(108, 158, 190, 0.35); background: #0f2a3a; color: #d5f3ff; width: 140px; }
    @media (max-width: 800px) { .cards { grid-template-columns: repeat(2, 1fr); } .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main class="container">
    <section class="hero">
      <h1>StockAnalyzer Control Deck | 核心控制台</h1>
      <div class="toolbar">
        <button class="btn" id="refresh-btn">🔄 刷新数据</button>
        <span class="status" id="sync-status">就绪</span>
        <span class="status" id="ops-status">操作: 检查中</span>
        <span class="status" id="execution-mode-status">执行模式: 检查中</span>
        <a class="btn" id="stage-page-link" href="/dashboard/stage">🧭 当前阶段</a>
        <a class="btn" href="/dashboard/recommendations">推荐汇总</a>
        <button class="btn" id="ops-toggle-btn">切换操作权限</button>
      </div>
    </section>

    <!-- 核心指标 -->
    <section class="cards">
      <article class="card"><div class="label">净值 (Equity)</div><div class="value" id="card-equity">-</div></article>
      <article class="card"><div class="label">持仓数量</div><div class="value" id="card-positions">-</div></article>
      <article class="card"><div class="label">对账一致率</div><div class="value" id="card-align">-</div></article>
      <article class="card"><div class="label">是否暂停开仓</div><div class="value" id="card-week7-pause">-</div></article>
    </section>

    <section class="grid">
      <article class="panel" style="grid-column: span 2;">
        <h2>⚡ 快捷指令 (Command Deck)</h2>
        <div class="toolbar">
          <input id="set-symbol-input" class="trace-input" style="width:110px;" type="text" placeholder="代码" />
          <input id="set-price-input" class="trace-input" style="width:90px;" type="number" step="0.01" placeholder="购入单价" />
          <input id="set-vol-input" class="trace-input" style="width:90px;" type="number" step="1" placeholder="购入股数" />
          <input id="set-fee-input" class="trace-input" style="width:90px;" type="number" step="0.01" placeholder="手续费" />
          <input id="set-account-input" class="trace-input" style="width:110px;" type="text" placeholder="账户(可选)" />
          <input id="set-trade-time-input" class="trace-input" style="width:170px;" type="datetime-local" placeholder="成交时间(可选)" />
          <input id="set-total-asset-input" class="trace-input" style="width:130px;" type="number" step="1" placeholder="总资产(供计算)" title="保存在浏览器" />
          <input id="set-target-input" class="trace-input" style="width:90px;" type="number" step="0.001" placeholder="目标仓位" />
          <input id="set-note-input" class="trace-input" style="width:220px;" type="text" placeholder="备注(可选)" />
          <select id="rec-status-input" class="trace-input" style="width:140px;">
            <option value="recommended">标记推荐</option>
            <option value="bought">标记已买入</option>
            <option value="holding">标记持有</option>
            <option value="sell_alert">标记卖出提醒</option>
            <option value="closed">标记已结束</option>
            <option value="watching">标记观察</option>
            <option value="dropped">标记放弃</option>
          </select>
          <button class="btn" id="set-btn">📍 建仓/调仓</button>
          <button class="btn danger" id="close-btn">🗑️ 平仓</button>
          <button class="btn" id="rec-update-btn">🏷️ 更新跟踪状态</button>
        </div>
        <div class="toolbar" style="margin-top:10px;">
          <input id="snapshot-input" class="trace-input" type="text" style="width:300px;" placeholder="批量同步: 等同发微信 '同步持仓 600000:20%'" />
          <button class="btn" id="snapshot-btn">🔄 同步持仓</button>
          <button class="btn" id="reconcile-btn">✅ 全局对账</button>
        </div>
        <div class="toolbar" style="margin-top:10px;">
          <button class="btn danger" id="pause-btn">⏸️ 暂停新开仓</button>
          <button class="btn" id="resume-btn">▶️ 恢复新开仓</button>
        </div>
      </article>

      <!-- 持仓概览 -->
      <article class="panel">
        <h2>📊 当前持仓 (Open Positions)</h2>
        <table>
          <thead><tr><th>代码</th><th>策略</th><th>目标仓位</th><th>成本价</th><th>股数</th><th>持仓天数</th></tr></thead>
          <tbody id="positions-body"></tbody>
        </table>
      </article>
      
      <!-- 最近成交 -->
      <article class="panel">
        <h2>📜 最近成交 (Recent Trades)</h2>
        <div class="list" id="trades-list"></div>
      </article>

      <article class="panel" style="grid-column: span 2;">
        <h2>🎯 推荐跟踪 (Lifecycle)</h2>
        <div class="toolbar">
          <input id="rec-filter-symbol" class="trace-input" style="width:120px;" type="text" placeholder="代码筛选" />
          <select id="rec-filter-status" class="trace-input" style="width:130px;">
            <option value="">全部状态</option>
            <option value="recommended">recommended</option>
            <option value="bought">bought</option>
            <option value="holding">holding</option>
            <option value="sell_alert">sell_alert</option>
            <option value="closed">closed</option>
            <option value="expired">expired</option>
            <option value="watching">watching</option>
            <option value="dropped">dropped</option>
          </select>
          <select id="rec-sort-field" class="trace-input" style="width:130px;">
            <option value="updated_at">按更新时间</option>
            <option value="last_signal_score">按评分</option>
          </select>
          <button class="btn" id="rec-export-btn">导出CSV</button>
          <span class="status" id="rec-page-info">第 1 页</span>
          <button class="btn" id="rec-prev-btn">上一页</button>
          <button class="btn" id="rec-next-btn">下一页</button>
        </div>
        <div class="item" id="rec-performance-summary" style="margin-top:10px;">暂无推荐收益汇总</div>
        <table>
          <thead><tr><th>代码</th><th>状态</th><th>买入区间</th><th>止损</th><th>止盈</th><th>进入/退出</th><th>收益</th><th>更新时间</th></tr></thead>
          <tbody id="recommendation-body"></tbody>
        </table>
      </article>

      <article class="panel" style="grid-column: span 2;">
        <h2>🚨 持仓预警 (Manual Cost Basis)</h2>
        <div class="toolbar">
          <select id="holding-filter-severity" class="trace-input" style="width:130px;">
            <option value="">全部级别</option>
            <option value="warn">warn</option>
            <option value="info">info</option>
          </select>
          <button class="btn" id="holding-export-btn">导出CSV</button>
          <span class="status" id="holding-page-info">第 1 页</span>
          <button class="btn" id="holding-prev-btn">上一页</button>
          <button class="btn" id="holding-next-btn">下一页</button>
        </div>
        <div class="list" id="holding-alert-list"></div>
      </article>

      <article class="panel" style="grid-column: span 2;">
        <h2>🧭 成交偏差 (Recommendation vs Manual)</h2>
        <div class="toolbar">
          <input id="bias-filter-symbol" class="trace-input" style="width:120px;" type="text" placeholder="代码筛选" />
          <button class="btn" id="bias-export-btn">导出CSV</button>
          <span class="status" id="bias-page-info">第 1 页</span>
          <button class="btn" id="bias-prev-btn">上一页</button>
          <button class="btn" id="bias-next-btn">下一页</button>
        </div>
        <div class="item" id="bias-summary">暂无数据</div>
        <div class="list" id="bias-list" style="margin-top:10px;"></div>
      </article>

      <!-- 新闻因子快照 -->
      <article class="panel">
        <h2>📰 新闻因子快照</h2>
        <div class="item" id="news-watch-summary">暂无数据</div>
        <div class="toolbar" style="margin-top:10px;">
          <span class="status" id="news-cache-status">缓存: 加载中</span>
          <input id="news-cache-symbol-input" class="trace-input" style="width:110px;" type="text" placeholder="symbol(可选)" />
          <input id="news-cache-strategy-input" class="trace-input" style="width:110px;" type="text" placeholder="strategy(可选)" />
          <button class="btn" id="news-cache-refresh-btn">刷新缓存状态</button>
          <button class="btn danger" id="news-cache-clear-btn">清空缓存</button>
        </div>
        <div class="list" id="news-watch-list" style="margin-top:10px;"></div>
        <div class="item" id="news-history-summary" style="margin-top:10px;">历史: 暂无数据</div>
        <div class="list" id="news-history-list" style="margin-top:10px;"></div>
      </article>

      <article class="panel">
        <h2>🎯 最新扫描结果</h2>
        <div class="item" id="week5-summary">暂无扫描结果</div>
        <div class="list" id="week5-watchlist" style="margin-top:10px;"></div>
      </article>

      <article class="panel">
        <h2>🧠 最新 Week6 分析</h2>
        <div class="item" id="week6-summary">暂无 Week6 结果</div>
        <div class="list" id="week6-focus-list" style="margin-top:10px;"></div>
      </article>
      
      <!-- 闲时阻塞任务 -->
      <article class="panel" style="grid-column: span 2;">
        <h2>⏳ 被阻塞的后台任务 (需人工确认)</h2>
        <div class="toolbar">
          <input id="idle-ack-task-input" class="trace-input" type="text" placeholder="任务ID (如 WD-P0-01)" />
          <button class="btn" id="idle-ack-task-btn">✅ 批准任务</button>
          <button class="btn" id="idle-ack-all-btn">✅ 批准所有</button>
        </div>
        <div class="list" id="idle-blocked-list" style="margin-top:10px;"></div>
      </article>

      <article class="panel" style="grid-column: span 2;">
        <h2>📝 操作日志</h2>
        <div class="list" id="command-output-list"></div>
      </article>
    </section>
  </main>

  <script>
    const el = id => document.getElementById(id);
    const setHtml = (id, html) => el(id).innerHTML = html;
    const setText = (id, txt) => el(id).textContent = txt;
    
    async function postJson(url, payload) {
      const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) });
      return res.json();
    }
    
    function logResult(title, payload) {
      const node = document.createElement("div");
      node.className = "item";
      node.innerHTML = `<div><strong>${title}</strong></div><div class="meta">${new Date().toLocaleTimeString()} | ${JSON.stringify(payload)}</div>`;
      el("command-output-list").prepend(node);
    }
    const esc = str => String(str).replace(/[&<>"']/g, m => ({'&': '&amp;','<': '&lt;','>': '&gt;','"': '&quot;',"'": '&#39;'}[m]));
    const REC_PAGE_SIZE = 10, HOLDING_PAGE_SIZE = 8, BIAS_PAGE_SIZE = 8;
    let recRows = [], holdingRows = [], biasRows = [];
    let recPage = 1, holdingPage = 1, biasPage = 1;

    function downloadCsv(filename, rows, columns) {
      if (!rows.length) return alert("暂无可导出数据");
      const escCsv = val => `"${String(val ?? "").replace(/"/g, '""')}"`;
      const head = columns.map(c => escCsv(c.label)).join(",");
      const body = rows.map(r => columns.map(c => escCsv(r[c.key])).join(",")).join("\\n");
      const csv = "\\ufeff" + head + "\\n" + body;
      const blob = new Blob([csv], {type: "text/csv;charset=utf-8;"});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }

    function renderRecommendationTable() {
      const symbolFilter = (el("rec-filter-symbol").value || "").trim().toUpperCase();
      const statusFilter = (el("rec-filter-status").value || "").trim().toLowerCase();
      const sortField = (el("rec-sort-field").value || "updated_at").trim();
      let rows = recRows.filter(r => {
        const symbolOk = !symbolFilter || String(r.symbol || "").toUpperCase().includes(symbolFilter);
        const statusOk = !statusFilter || String(r.status || "").toLowerCase() === statusFilter;
        return symbolOk && statusOk;
      });
      rows = rows.sort((a, b) => {
        if (sortField === "last_signal_score") return Number(b.last_signal_score || 0) - Number(a.last_signal_score || 0);
        return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
      });
      const pages = Math.max(1, Math.ceil(rows.length / REC_PAGE_SIZE));
      recPage = Math.max(1, Math.min(recPage, pages));
      const slice = rows.slice((recPage - 1) * REC_PAGE_SIZE, recPage * REC_PAGE_SIZE);
      setText("rec-page-info", `第 ${recPage}/${pages} 页 (${rows.length}条)`);
      setHtml("recommendation-body", slice.length ? slice.map(r => {
        const plan = r.trade_plan || {};
        const range = plan.entry_range || [plan.entry_low, plan.entry_high];
        const entryRange = Number(range[0] || 0) > 0 && Number(range[1] || 0) > 0
          ? `${Number(range[0]).toFixed(2)}-${Number(range[1]).toFixed(2)}`
          : "-";
        const stop = Number(plan.stop_loss_price || 0) > 0 ? Number(plan.stop_loss_price).toFixed(2) : "-";
        const takePrices = Array.isArray(plan.take_profit_prices) ? plan.take_profit_prices : [];
        const take = takePrices.length ? takePrices.slice(0, 3).map(v => Number(v).toFixed(2)).join("/") : "-";
        const entryExit = `${Number(r.entry_price || 0) > 0 ? Number(r.entry_price).toFixed(2) : "-"} / ${Number(r.exit_price || 0) > 0 ? Number(r.exit_price).toFixed(2) : "-"}`;
        const pnl = Number(r.realized_return_pct || 0) !== 0
          ? Number(r.realized_return_pct * 100).toFixed(2) + "%"
          : Number(r.current_return_pct || 0) !== 0
          ? Number(r.current_return_pct * 100).toFixed(2) + "%"
          : "-";
        return `<tr><td>${esc(r.symbol)}</td><td>${esc(r.status)}</td><td>${esc(entryRange)}</td><td>${esc(stop)}</td><td>${esc(take)}</td><td>${esc(entryExit)}</td><td>${esc(pnl)}</td><td>${esc(r.updated_at || "")}</td></tr>`;
      }).join("") : "<tr><td colspan='8'>暂无推荐跟踪记录</td></tr>");
    }

    function renderRecommendationSummary(summary) {
      const s = summary || {};
      const winRate = Number((s.win_rate || 0) * 100).toFixed(2);
      const avgReturn = Number((s.avg_realized_return_pct || 0) * 100).toFixed(2);
      const totalReturn = Number((s.total_realized_return_pct || 0) * 100).toFixed(2);
      const openReturn = Number((s.avg_open_return_pct || 0) * 100).toFixed(2);
      setHtml(
        "rec-performance-summary",
        `<div><strong>记录:</strong> ${Number(s.records || 0)} | <strong>未结束:</strong> ${Number(s.open_records || 0)} | <strong>已结束:</strong> ${Number(s.closed_records || 0)} | <strong>胜率:</strong> ${winRate}%</div>
         <div class="meta">平均已实现 ${avgReturn}% | 累计已实现 ${totalReturn}% | 未结束平均 ${openReturn}% | 平均持有 ${Number(s.avg_holding_days || 0).toFixed(1)} 天</div>`
      );
    }

    function renderHoldingAlerts() {
      const severityFilter = (el("holding-filter-severity").value || "").trim().toLowerCase();
      let rows = holdingRows.filter(r => !severityFilter || String(r.severity || "").toLowerCase() === severityFilter);
      const pages = Math.max(1, Math.ceil(rows.length / HOLDING_PAGE_SIZE));
      holdingPage = Math.max(1, Math.min(holdingPage, pages));
      const slice = rows.slice((holdingPage - 1) * HOLDING_PAGE_SIZE, holdingPage * HOLDING_PAGE_SIZE);
      setText("holding-page-info", `第 ${holdingPage}/${pages} 页 (${rows.length}条)`);
      setHtml("holding-alert-list", slice.length ? slice.map(r => `<div class="item"><div><strong>${esc(r.symbol)}</strong> ${esc(r.reason)} (${esc(r.severity)})</div><div class="meta">P&L ${Number((r.pnl_pct || 0) * 100).toFixed(2)}% | 持仓 ${Number(r.hold_days || 0)} 天 | 最新价 ${Number(r.latest_price || 0).toFixed(2)}</div></div>`).join("") : "<div class='item'>暂无持仓预警</div>");
    }

    function renderBiasList() {
      const symbolFilter = (el("bias-filter-symbol").value || "").trim().toUpperCase();
      let rows = biasRows.filter(r => !symbolFilter || String(r.symbol || "").toUpperCase().includes(symbolFilter));
      rows = rows.sort((a, b) => Math.abs(Number(b.price_bias_pct || 0)) - Math.abs(Number(a.price_bias_pct || 0)));
      const pages = Math.max(1, Math.ceil(rows.length / BIAS_PAGE_SIZE));
      biasPage = Math.max(1, Math.min(biasPage, pages));
      const slice = rows.slice((biasPage - 1) * BIAS_PAGE_SIZE, biasPage * BIAS_PAGE_SIZE);
      setText("bias-page-info", `第 ${biasPage}/${pages} 页 (${rows.length}条)`);
      setHtml("bias-list", slice.length ? slice.map(r => `<div class="item"><div><strong>${esc(r.symbol)}</strong> 仓位偏差 ${Number(r.position_bias || 0).toFixed(4)}</div><div class="meta">${esc(r.timestamp || "")} | 价格偏差 ${Number((r.price_bias_pct || 0) * 100).toFixed(2)}% | 目标 ${Number(r.target_position || 0).toFixed(3)} vs 推荐 ${Number(r.recommended_target_position || 0).toFixed(3)}</div></div>`).join("") : "<div class='item'>暂无成交偏差记录</div>");
    }

    async function loadDash() {
      setText("sync-status", "同步中...");
      try {
        const [dashRes, opsRes, idleRes, cacheRes, historyRes, week5Res, week6Res] = await Promise.all([
          fetch("/dashboard/portfolio?days=7&trade_limit=120"),
          fetch("/dashboard/ops/state"),
          fetch("/idle/state"),
          fetch("/news/score/cache/state"),
          fetch("/news/score/history?limit=8"),
          fetch("/week5/scan/latest"),
          fetch("/week6/latest")
        ]);
        const db = await dashRes.json() || {},
              ops = await opsRes.json() || {},
              idle = await idleRes.json() || {},
              cacheState = await cacheRes.json() || {},
              newsHistory = await historyRes.json() || {},
              week5Wrap = await week5Res.json() || {},
              week6Wrap = await week6Res.json() || {};
        const week5 = week5Wrap.report || {},
              week6 = week6Wrap.report || {};
        
        // Cards
        const summ = db.summary || {}, qual = db.execution_quality || {}, wk7 = db.week7_kill_switch || {};
        const align = ((qual.reconcile_alignment_rate || 0) * 100).toFixed(2);
        setText("card-equity", Number(summ.current_equity || 0).toFixed(4));
        setText("card-positions", summ.open_positions || 0);
        setText("card-align", align + "%");
        el("card-align").className = "value " + (align >= 95 ? "good" : "bad");
        setText("card-week7-pause", wk7.pause_new_buy ? "是 (暂停)" : "否 (正常)");
        el("card-week7-pause").className = "value " + (wk7.pause_new_buy ? "bad" : "good");
        
        // Ops Mode
        const en = Boolean(ops.enabled);
        setText("ops-status", en ? "操作: 允许" : "操作: 禁用");
        el("ops-status").style.color = en ? "#4ddf7e" : "#ff6f59";
        const advisoryOnly = Boolean(ops.advisory_only);
        setText("execution-mode-status", advisoryOnly ? "执行模式: 仅建议" : "执行模式: 自动落地");
        el("execution-mode-status").style.color = advisoryOnly ? "#ffb86b" : "#4ddf7e";
        
        // Positions
        const pos = db.positions_panel || [];
        setHtml("positions-body", pos.length ? pos.map(r => `<tr><td>${esc(r.symbol)}</td><td>${esc(r.strategy)}</td><td>${Number(r.target_position).toFixed(3)}</td><td>${Number(r.entry_price || 0).toFixed(2)}</td><td>${Number(r.quantity || 0)}</td><td>${r.hold_days}</td></tr>`).join("") : "<tr><td colspan='6'>无持仓</td></tr>");
        
        // Trades
        const trds = db.recent_trades || [];
        setHtml("trades-list", trds.length ? [...trds].reverse().map(r => `<div class="item"><div><strong>${esc(r.side)}</strong> ${esc(r.symbol)} (${esc(r.strategy)})</div><div class="meta">${esc(r.timestamp)} | ${esc(r.reason)}</div></div>`).join("") : "<div class='item'>暂无记录</div>");

        // Recommendation lifecycle
        const recPanel = db.recommendation_panel || {};
        recRows = recPanel.items || [];
        renderRecommendationSummary(recPanel.summary || {});
        renderRecommendationTable();
        holdingRows = (db.holding_alerts || {}).items || [];
        renderHoldingAlerts();
        const bias = db.execution_bias || {};
        const biasSummary = bias.summary || {};
        const avgPosBias = Number(biasSummary.avg_abs_position_bias || 0).toFixed(4);
        const avgPriceBiasPct = Number((biasSummary.avg_abs_price_bias_pct || 0) * 100).toFixed(2);
        setHtml("bias-summary", `<div><strong>记录:</strong> ${Number(bias.records || 0)} | <strong>平均仓位偏差:</strong> ${avgPosBias} | <strong>平均价格偏差:</strong> ${avgPriceBiasPct}%</div>`);
        biasRows = bias.items || [];
        renderBiasList();

        // News Watchlist Preview
        const news = db.news_watchlist_preview || {};
        const newsSummary = news.summary || {};
        const newsAvg = Number(newsSummary.average_news_component || 0).toFixed(3);
        const newsRecords = Number(news.records || 0);
        const newsSource = news.source || "watchlist";
        setHtml("news-watch-summary", `<div><strong>来源:</strong> ${esc(newsSource)} | <strong>记录:</strong> ${newsRecords} | <strong>均值:</strong> ${newsAvg}</div>`);
        const newsItems = news.items || [];
        setHtml("news-watch-list", newsItems.length ? newsItems.slice(0, 6).map(r => `<div class="item"><div><strong>${esc(r.symbol || "")}</strong> 分数 ${Number(r.news_component || 0).toFixed(3)}</div><div class="meta">状态: ${esc(r.status || "unknown")} | 策略: ${esc(r.strategy || "trend")}</div></div>`).join("") : "<div class='item'>暂无新闻因子数据</div>");
        const cacheEntries = Number(cacheState.entries || 0);
        const cacheTtlSec = Number(cacheState.ttl_sec || 0);
        setText("news-cache-status", `缓存: ${cacheEntries} 条 | TTL: ${cacheTtlSec}s`);
        const historySummary = newsHistory.summary || {};
        const historyRecords = Number(newsHistory.records || 0);
        const historyAvg = Number(historySummary.average_news_component || 0).toFixed(3);
        setHtml("news-history-summary", `<div><strong>历史记录:</strong> ${historyRecords} | <strong>均值:</strong> ${historyAvg}</div>`);
        const historyItems = newsHistory.items || [];
        setHtml("news-history-list", historyItems.length ? [...historyItems].reverse().map(r => `<div class="item"><div><strong>${esc(r.symbol || "")}</strong> 分数 ${Number(r.news_component || 0).toFixed(3)} (${esc(r.strategy || "trend")})</div><div class="meta">${esc(r.timestamp || "")} | ${esc(r.status || "unknown")}</div></div>`).join("") : "<div class='item'>暂无新闻历史记录</div>");

        // Latest Week5 Scan
        const week5Summary = week5.summary || {};
        const week5Sync = week5.watchlist_sync || {};
        const week5Candidates = ((week5.signal_pool || {}).candidates || []);
        const week5Map = Object.fromEntries(week5Candidates.map(r => [String(r.symbol || ""), r]));
        const scannedCount = Number(week5.watchlist_size || 0);
        const selectedSymbols = week5Sync.symbols || [];
        const emptySignal = week5.empty_signal || {};
        setHtml(
          "week5-summary",
          week5.timestamp
            ? `<div><strong>扫描时间:</strong> ${esc(week5.timestamp)} | <strong>来源:</strong> ${esc(week5.symbol_source || "unknown")} | <strong>扫描数:</strong> ${scannedCount}</div>
               <div class="meta">观察池 ${Number(week5Sync.watchlist_after || selectedSymbols.length || 0)} 只 | 首板候选 ${Number(week5Summary.first_board_candidates || 0)} | 异常 ${Number(week5Summary.anomalies || 0)} | 空信号 ${Boolean(emptySignal.triggered) ? "是" : "否"}</div>`
            : "暂无扫描结果"
        );
        setHtml(
          "week5-watchlist",
          selectedSymbols.length
            ? selectedSymbols.map(sym => {
                const row = week5Map[String(sym)] || {};
                return `<div class="item"><div><strong>${esc(sym)}</strong> ${esc(row.grade || "-")} / ${esc(row.action || "-")}</div><div class="meta">评分 ${Number(row.score || 0).toFixed(2)} | 建议仓位 ${Number(row.suggested_position || 0).toFixed(3)}</div></div>`;
              }).join("")
            : "<div class='item'>暂无新观察池结果</div>"
        );

        // Latest Week6
        const week6Summary = week6.summary || {};
        const strategyAllocation = week6.strategy_allocation || {};
        const executionAdjustment = week6.execution_adjustment || {};
        const focusSymbols = ((week6.main_force || {}).focus_symbols || []);
        setHtml(
          "week6-summary",
          week6.timestamp
            ? `<div><strong>分析时间:</strong> ${esc(week6.timestamp)} | <strong>状态:</strong> ${esc(strategyAllocation.regime || "unknown")} | <strong>强势数:</strong> ${Number((week6.main_force || {}).strong_count || 0)}</div>
               <div class="meta">重点关注 ${Number(week6Summary.focus_symbols || 0)} 只 | 排除 ${Number(week6Summary.excluded_symbols || 0)} 只 | 门槛上调 ${Number(executionAdjustment.score_threshold_shift || 0).toFixed(4)} | 仓位倍率 ${Number(executionAdjustment.position_multiplier || 0).toFixed(4)}</div>`
            : "暂无 Week6 结果"
        );
        setHtml(
          "week6-focus-list",
          focusSymbols.length
            ? focusSymbols.map(r => `<div class="item"><div><strong>${esc(r.symbol || "")}</strong> ${esc(r.status || "normal")} / ${esc(r.regulatory_action || "normal")}</div><div class="meta">调整后评分 ${Number(r.adjusted_score || 0).toFixed(2)} | 20日趋势 ${Number(r.trend_strength_20d || 0).toFixed(4)} | 5日换手比 ${Number(r.turnover_ratio_5d || 0).toFixed(4)}</div></div>`).join("")
            : "<div class='item'>暂无重点关注标的</div>"
        );
        
        // Blocked Task
        const blocked = idle.blocked_tasks || {}; const bkeys = Object.keys(blocked);
        setHtml("idle-blocked-list", bkeys.length ? bkeys.map(k => `<div class="item"><div><strong>${k}</strong> 原因: ${esc(blocked[k].reason)}</div><div class="meta">阻塞时间: ${esc(blocked[k].blocked_since)}</div></div>`).join("") : "<div class='item'>无需批准的任务</div>");
        
        setText("sync-status", "最后更新: " + new Date().toLocaleTimeString());
      } catch (err) {
        setText("sync-status", "同步失败");
      }
    }
    
    // Commands Actions
    async function qCmd(action, payload) { try { const d = await postJson("/dashboard/command/quick", {action, payload}); logResult(action, d); loadDash(); } catch (e) { logResult(action, {error: String(e)}); } }
    
    el("refresh-btn").onclick = loadDash;
    el("news-cache-refresh-btn").onclick = loadDash;
    el("ops-toggle-btn").onclick = async () => { if(confirm("切换操作权限?")) { await postJson("/dashboard/ops/toggle", {enabled: !el("ops-status").textContent.includes("允许")}); loadDash(); } };
    el("pause-btn").onclick = () => { if(confirm("确定暂停新买入？")) qCmd("PAUSE_NEW_BUY", {}); };
    el("resume-btn").onclick = () => { if(confirm("确定恢复运行？")) qCmd("RESUME_NEW_BUY", {}); };
    el("reconcile-btn").onclick = () => { if(confirm("立即触发对账？")) qCmd("RUN_RECONCILE", {}); };
    
    // Auto-calculate target position
    const calcPos = () => {
      const price = parseFloat(el("set-price-input").value) || 0;
      const vol = parseFloat(el("set-vol-input").value) || 0;
      const total = parseFloat(el("set-total-asset-input").value) || 0;
      if (total > 0 && price > 0 && vol > 0) {
        el("set-target-input").value = (price * vol / total).toFixed(4);
      }
    };
    el("set-price-input").oninput = calcPos;
    el("set-vol-input").oninput = calcPos;
    el("set-total-asset-input").oninput = () => {
      localStorage.setItem("sa_total_asset", el("set-total-asset-input").value);
      calcPos();
    };
    
    // Restore total asset
    const savedAsset = localStorage.getItem("sa_total_asset");
    if (savedAsset) el("set-total-asset-input").value = savedAsset;

    el("set-btn").onclick = () => {
      const sym = el("set-symbol-input").value; const pt = el("set-target-input").value;
      if (!sym || !pt) return alert("请填写代码和仓位");
      const payload = {symbol: sym, target_position: Number(pt), strategy: "manual"};
      const entryPrice = parseFloat(el("set-price-input").value);
      const quantity = parseInt(el("set-vol-input").value || "0", 10);
      const fee = parseFloat(el("set-fee-input").value);
      const account = (el("set-account-input").value || "").trim();
      const tradeTime = (el("set-trade-time-input").value || "").trim();
      const note = (el("set-note-input").value || "").trim();
      if (!Number.isNaN(entryPrice) && entryPrice > 0) payload.entry_price = Number(entryPrice.toFixed(6));
      if (!Number.isNaN(quantity) && quantity > 0) payload.quantity = quantity;
      if (!Number.isNaN(fee) && fee >= 0) payload.fee = Number(fee.toFixed(6));
      if (account) payload.account = account;
      if (tradeTime) payload.trade_time = tradeTime;
      if (note) payload.note = note;
      if(confirm(`确定建仓/调仓 ${sym} 目标仓位 ${pt}?`)) qCmd("SET_POSITION", payload);
    };
    el("close-btn").onclick = () => {
      const sym = el("set-symbol-input").value;
      if (!sym) return alert("请填写代码(建仓输入框提取)");
      const payload = {symbol: sym};
      const exitPrice = parseFloat(el("set-price-input").value);
      const quantity = parseInt(el("set-vol-input").value || "0", 10);
      const fee = parseFloat(el("set-fee-input").value);
      const account = (el("set-account-input").value || "").trim();
      const tradeTime = (el("set-trade-time-input").value || "").trim();
      const note = (el("set-note-input").value || "").trim();
      if (!Number.isNaN(exitPrice) && exitPrice > 0) payload.exit_price = Number(exitPrice.toFixed(6));
      if (!Number.isNaN(quantity) && quantity > 0) payload.quantity = quantity;
      if (!Number.isNaN(fee) && fee >= 0) payload.fee = Number(fee.toFixed(6));
      if (account) payload.account = account;
      if (tradeTime) payload.trade_time = tradeTime;
      if (note) payload.note = note;
      if(confirm(`确定平仓 ${sym}?`)) qCmd("CLOSE_POSITION", payload);
    };
    el("rec-update-btn").onclick = () => {
      const sym = (el("set-symbol-input").value || "").trim();
      const status = (el("rec-status-input").value || "").trim();
      const note = (el("set-note-input").value || "").trim();
      if (!sym) return alert("请填写代码");
      if (!status) return alert("请选择跟踪状态");
      const payload = {symbol: sym, status, strategy: "manual"};
      if (note) payload.note = note;
      if (confirm(`确定更新 ${sym} 状态为 ${status}?`)) qCmd("SET_RECOMMENDATION_STATUS", payload);
    };

    // Sync Snapshot helper
    el("snapshot-btn").onclick = async () => {
      const raw = el("snapshot-input").value.trim();
      if (!raw) return alert("请填写格式 如 600000:20%,300750:15%");
      const items = raw.split(",").filter(i => i.includes(":"));
      const positions = items.map(i => { const p = i.split(":"); return {symbol: p[0], target_position: parseFloat(p[1]) / (p[1].includes("%")?100:1)}; });
      if(!positions.length) return alert("格式未识别");
      if(confirm("确定同步持仓并立刻触发对账？")) {
        try {
          const data = await postJson("/dashboard/reconcile/quick", {positions, run_reconcile: true});
          logResult("同步并对账", data); loadDash();
        } catch(e) { logResult("同步异常", {error: String(e)}); }
      }
    };

    el("news-cache-clear-btn").onclick = async () => {
      const symbol = (el("news-cache-symbol-input").value || "").trim();
      const strategy = (el("news-cache-strategy-input").value || "").trim();
      const target = symbol || strategy ? `symbol=${symbol || "*"} strategy=${strategy || "*"}` : "全部缓存";
      if (!confirm(`确定清理新闻评分缓存？目标: ${target}`)) return;
      try {
        const data = await postJson("/news/score/cache/clear", {symbol, strategy});
        logResult("清空新闻缓存", data);
        loadDash();
      } catch (e) {
        logResult("清空新闻缓存失败", {error: String(e)});
      }
    };
    
    el("idle-ack-task-btn").onclick = async () => {
      if(!el("idle-ack-task-input").value) return alert("请输入Task ID");
      if(confirm("批准该任务运行？")) { await postJson("/idle/ack", {task_id: el("idle-ack-task-input").value, clear_all: false}); loadDash(); }
    };
    el("idle-ack-all-btn").onclick = async () => {
      if(confirm("一键批准所有失败的任务重新尝试？")) { await postJson("/idle/ack", {task_id: "", clear_all: true}); loadDash(); }
    };

    // Recommendation filters / paging / export
    el("rec-filter-symbol").oninput = () => { recPage = 1; renderRecommendationTable(); };
    el("rec-filter-status").onchange = () => { recPage = 1; renderRecommendationTable(); };
    el("rec-sort-field").onchange = () => { recPage = 1; renderRecommendationTable(); };
    el("rec-prev-btn").onclick = () => { recPage = Math.max(1, recPage - 1); renderRecommendationTable(); };
    el("rec-next-btn").onclick = () => { recPage += 1; renderRecommendationTable(); };
    el("rec-export-btn").onclick = () => downloadCsv(
      "recommendation_lifecycle.csv",
      recRows,
      [
        {key: "symbol", label: "symbol"},
        {key: "status", label: "status"},
        {key: "strategy", label: "strategy"},
        {key: "last_signal_score", label: "last_signal_score"},
        {key: "entry_price", label: "entry_price"},
        {key: "exit_price", label: "exit_price"},
        {key: "realized_return_pct", label: "realized_return_pct"},
        {key: "current_return_pct", label: "current_return_pct"},
        {key: "exit_alert_reason", label: "exit_alert_reason"},
        {key: "updated_at", label: "updated_at"},
        {key: "note", label: "note"}
      ]
    );

    // Holding filters / paging / export
    el("holding-filter-severity").onchange = () => { holdingPage = 1; renderHoldingAlerts(); };
    el("holding-prev-btn").onclick = () => { holdingPage = Math.max(1, holdingPage - 1); renderHoldingAlerts(); };
    el("holding-next-btn").onclick = () => { holdingPage += 1; renderHoldingAlerts(); };
    el("holding-export-btn").onclick = () => downloadCsv(
      "holding_alerts.csv",
      holdingRows,
      [
        {key: "symbol", label: "symbol"},
        {key: "severity", label: "severity"},
        {key: "reason", label: "reason"},
        {key: "entry_price", label: "entry_price"},
        {key: "latest_price", label: "latest_price"},
        {key: "pnl_pct", label: "pnl_pct"},
        {key: "hold_days", label: "hold_days"},
        {key: "updated_at", label: "updated_at"}
      ]
    );

    // Bias filters / paging / export
    el("bias-filter-symbol").oninput = () => { biasPage = 1; renderBiasList(); };
    el("bias-prev-btn").onclick = () => { biasPage = Math.max(1, biasPage - 1); renderBiasList(); };
    el("bias-next-btn").onclick = () => { biasPage += 1; renderBiasList(); };
    el("bias-export-btn").onclick = () => downloadCsv(
      "execution_bias.csv",
      biasRows,
      [
        {key: "timestamp", label: "timestamp"},
        {key: "symbol", label: "symbol"},
        {key: "strategy", label: "strategy"},
        {key: "recommendation_id", label: "recommendation_id"},
        {key: "target_position", label: "target_position"},
        {key: "recommended_target_position", label: "recommended_target_position"},
        {key: "position_bias", label: "position_bias"},
        {key: "entry_price", label: "entry_price"},
        {key: "reference_price", label: "reference_price"},
        {key: "price_bias_pct", label: "price_bias_pct"}
      ]
    );

    loadDash();
    setInterval(loadDash, 10000);
  </script>
</body>
</html>
"""

_RUNTIME_STAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>StockAnalyzer Runtime Stage | 当前阶段</title>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #071521;
      --panel: rgba(14, 32, 49, 0.9);
      --border: rgba(98, 142, 178, 0.28);
      --text: #d8f2ff;
      --muted: #8eb8d0;
      --accent: #40d4b1;
      --warn: #ffb454;
      --bad: #ff7a6b;
      --good: #55df86;
      --shadow: 0 18px 40px rgba(1, 11, 19, 0.42);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font-family: "Space Grotesk", "Microsoft YaHei UI", sans-serif;
      background: radial-gradient(circle at 20% 10%, rgba(64, 212, 177, 0.16), transparent 32%), linear-gradient(135deg, #05111c 0%, #0a2132 52%, #122434 100%);
      min-height: 100vh;
      padding: 24px 16px 40px;
    }
    .container { max-width: 1180px; margin: 0 auto; display: grid; gap: 14px; }
    .panel, .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }
    .hero { padding: 20px; }
    .hero h1 { margin: 0; font-size: 1.9rem; }
    .hero p { margin: 8px 0 0; color: var(--muted); }
    .toolbar { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .btn {
      border: 0;
      border-radius: 999px;
      padding: 9px 16px;
      background: linear-gradient(110deg, #38d2ae, #61e1c4);
      color: #062437;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
    }
    .status-pill {
      border-radius: 999px;
      padding: 6px 12px;
      border: 1px solid rgba(115, 167, 201, 0.35);
      color: var(--muted);
      background: rgba(14, 42, 61, 0.72);
      font-family: "IBM Plex Mono", monospace;
      font-size: 0.86rem;
    }
    .cards { display: grid; gap: 12px; grid-template-columns: repeat(4, 1fr); }
    .card { padding: 14px; }
    .label { color: var(--muted); font-size: 0.82rem; letter-spacing: 0.05em; font-weight: 700; text-transform: uppercase; }
    .value { margin-top: 8px; font-size: 1.45rem; font-weight: 700; }
    .muted { color: var(--muted); }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .grid { display: grid; gap: 12px; grid-template-columns: 1.4fr 1fr; }
    .panel { padding: 16px; }
    .panel h2 { margin: 0 0 12px; font-size: 1.08rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.94rem; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid rgba(108, 158, 190, 0.2); vertical-align: top; }
    th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }
    .table-wrap { overflow: auto; }
    .kv { display: grid; gap: 10px; }
    .kv-row { padding: 10px 12px; border-radius: 12px; background: rgba(10, 27, 42, 0.7); border: 1px solid rgba(108, 158, 190, 0.16); }
    .kv-row strong { display: block; font-size: 0.84rem; color: var(--muted); margin-bottom: 4px; }
    .mono { font-family: "IBM Plex Mono", monospace; }
    @media (max-width: 960px) {
      .cards { grid-template-columns: repeat(2, 1fr); }
      .grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      .cards { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="container">
    <section class="panel hero">
      <h1>StockAnalyzer Runtime Stage | 当前阶段</h1>
      <p>聚合盘前 / 盘中 / 盘后 / 夜间阶段，以及关键定时任务的真实运行状态。</p>
      <div class="toolbar">
        <button class="btn" id="refresh-stage-btn">🔄 刷新</button>
        <a class="btn" href="/dashboard">⬅ 返回控制台</a>
        <span class="status-pill" id="stage-sync-status">就绪</span>
        <span class="status-pill" id="stage-as-of">-</span>
      </div>
    </section>

    <section class="cards">
      <article class="card"><div class="label">时间阶段</div><div class="value" id="phase-card">-</div><div class="muted" id="phase-detail">-</div></article>
      <article class="card"><div class="label">系统阶段</div><div class="value" id="system-stage-card">-</div><div class="muted" id="system-stage-detail">-</div></article>
      <article class="card"><div class="label">下一任务</div><div class="value" id="next-task-card">-</div><div class="muted" id="next-task-detail">-</div></article>
      <article class="card"><div class="label">运行模式</div><div class="value" id="mode-card">-</div><div class="muted" id="counts-card">-</div></article>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>关键任务阶段表</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>任务</th><th>类型</th><th>状态</th><th>计划时间</th><th>最近报告</th><th>详情</th></tr>
            </thead>
            <tbody id="tasks-body"></tbody>
          </table>
        </div>
      </article>

      <article class="panel">
        <h2>实时摘要</h2>
        <div class="kv">
          <div class="kv-row"><strong>Market Warehouse 进度</strong><div id="warehouse-progress" class="mono">-</div></div>
          <div class="kv-row"><strong>Idle Queue</strong><div id="idle-summary" class="mono">-</div></div>
          <div class="kv-row"><strong>调度器状态</strong><div id="scheduler-summary" class="mono">-</div></div>
        </div>
      </article>
    </section>
  </main>

  <script>
    const el = (id) => document.getElementById(id);

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function statusClass(status) {
      const s = String(status || "").toLowerCase();
      if (["done", "active"].includes(s)) return "good";
      if (["partial", "pending", "due", "expired", "skipped"].includes(s)) return "warn";
      if (["failed", "disabled"].includes(s)) return "bad";
      return "";
    }

    function fmt(value, fallback="-") {
      const text = String(value ?? "").trim();
      return text || fallback;
    }

    function renderTasks(tasks) {
      el("tasks-body").innerHTML = (tasks || []).map((task) => {
        const plan = task.type === "interval"
          ? `${fmt(task.interval_minutes)} min`
          : fmt(task.scheduled_time);
        return `
          <tr>
            <td>${esc(task.label)}<div class="muted">${esc(task.name)}</div></td>
            <td>${esc(task.type)}</td>
            <td class="${statusClass(task.status)}">${esc(task.status_label || task.status)}</td>
            <td class="mono">${esc(plan)}</td>
            <td class="mono">${esc(task.report_timestamp || task.last_run_day || task.last_slot_date || "-")}</td>
            <td>${esc(task.detail || "-")}</td>
          </tr>
        `;
      }).join("");
    }

    function renderSummary(payload) {
      const phase = payload.runtime_phase || {};
      const stage = payload.system_stage || {};
      const summary = payload.summary || {};
      const nextTask = summary.pending_next || {};
      const progress = payload.market_warehouse_progress || {};
      const idle = payload.idle_queue || {};
      const scheduler = payload.scheduler_state || {};
      const counts = summary.counts || {};
      const lastRun = scheduler.last_run || {};
      const dailyJobs = Object.keys(lastRun).length;

      el("stage-as-of").textContent = `as_of: ${fmt(payload.as_of)}`;
      el("phase-card").textContent = fmt(phase.label);
      el("phase-detail").textContent = fmt(phase.detail);
      el("system-stage-card").textContent = fmt(stage.label);
      el("system-stage-detail").textContent = fmt(stage.detail);
      el("next-task-card").textContent = fmt(nextTask.label, "无");
      el("next-task-detail").textContent = fmt(nextTask.scheduled_time || nextTask.interval_minutes, "当前无待执行");
      el("mode-card").textContent = fmt(summary.mode);
      el("counts-card").textContent = `done:${counts.done || 0} pending:${counts.pending || 0} running:${counts.running || 0}`;

      const warehouseText = Object.keys(progress).length
        ? `status=${fmt(progress.status)} phase=${fmt(progress.phase)} progress=${fmt(progress.symbols_completed, "0")}/${fmt(progress.symbols_total, "0")}`
        : "当前无在途进度文件";
      el("warehouse-progress").textContent = warehouseText;

      el("idle-summary").textContent = `enabled=${idle.enabled ? "true" : "false"} auto_run=${idle.auto_run ? "true" : "false"} blocked=${fmt(idle.blocked_tasks, "0")} pending_ack=${fmt(idle.pending_manual_ack, "0")}`;
      el("scheduler-summary").textContent = `daily_last_run=${dailyJobs} interval_last_slot=${Object.keys(scheduler.last_interval_slot || {}).length}`;
      renderTasks(payload.tasks || []);
    }

    async function loadStage() {
      el("stage-sync-status").textContent = "刷新中...";
      try {
        const response = await fetch("/runtime/stage");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        renderSummary(payload);
        el("stage-sync-status").textContent = "已同步";
      } catch (error) {
        el("stage-sync-status").textContent = `刷新失败: ${String(error)}`;
      }
    }

    el("refresh-stage-btn").onclick = loadStage;
    loadStage();
    setInterval(loadStage, 15000);
  </script>
</body>
</html>
"""


class PipelineRunRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    strategy: str = "trend"
    current_equity: float = 1.0


class NotificationRequest(BaseModel):
    title: str
    content: str
    level: str = "info"
    trace_id: str = ""


class SignalQualityAuditRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)
    include_audit_events: bool = True


class CommandRequest(BaseModel):
    command_id: str
    timestamp: int
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str


class SchedulerRunRequest(BaseModel):
    now: str | None = None


class TdxSyncRunRequest(BaseModel):
    now: str | None = None
    force: bool = False
    notify_enabled: bool | None = None
    source_trace_id: str = ""


class WarehouseSyncRunRequest(BaseModel):
    now: str | None = None
    force: bool = False
    notify_enabled: bool | None = None
    source_trace_id: str = ""
    symbols: list[str] = Field(default_factory=list)
    retry_failed_only: bool = False
    retry_report_trace_id: str = ""


class IdleQueueRunRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class IdleQueueAckRequest(BaseModel):
    task_id: str = ""
    clear_all: bool = False
    now: str | None = None


class EvolutionRunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    dry_run: bool | None = None
    now: str | None = None
    source_trace_id: str = ""


class EvolutionDrillRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = "evolution-drill"


class EvolutionM3MaintenanceRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class EvolutionM3SearchRequest(BaseModel):
    vector: list[float] = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=200)
    source_trace_id: str = ""


class EvolutionM8SuggestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=200)
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseAttemptRequest(BaseModel):
    days: int = Field(default=10, ge=1, le=60)
    min_runs: int = Field(default=5, ge=1, le=1000)
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseApprovalRequest(BaseModel):
    approver: str
    approved: bool
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketRequest(BaseModel):
    operator: str
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketExecuteRequest(BaseModel):
    executor: str
    ticket_id: str = ""
    note: str = ""
    confirm_window: bool = True
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketConfirmRequest(BaseModel):
    confirmer: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseTicketRollbackRequest(BaseModel):
    rollback_by: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class EvolutionReleaseConfirmationWatchdogRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class TrainModelsRequest(BaseModel):
    symbol: str = ""
    lookback_days: int = 600
    artifact_path: str | None = None
    full_market: bool = False
    max_symbols: int | None = None


class WalkForwardRequest(BaseModel):
    symbol: str
    lookback_days: int = 800


class BaselineReportRequest(BaseModel):
    symbol: str
    lookback_days: int = 800
    output_path: str | None = None


class PhaseCheckpointRequest(BaseModel):
    phase: str
    baseline_report_path: str | None = None
    output_path: str | None = None


class V13AcceptanceRequest(BaseModel):
    baseline_report_path: str | None = None
    output_path: str | None = None


class V13AcceptanceBundleRequest(BaseModel):
    symbol: str
    lookback_days: int = 800
    baseline_output_path: str | None = None
    v13_output_path: str | None = None
    run_week5_scan: bool = False
    week5_symbols: list[str] = Field(default_factory=list)


class BrokerPositionItem(BaseModel):
    symbol: str
    target_position: float = Field(ge=0.0)
    quantity: int | None = Field(default=None, ge=0)
    account: str = ""


class BrokerSnapshotRequest(BaseModel):
    positions: list[BrokerPositionItem] = Field(default_factory=list)
    source_trace_id: str = ""


class ReconcileRunRequest(BaseModel):
    now: str | None = None


class RuntimeArchiveRunRequest(BaseModel):
    force: bool = False
    now: str | None = None


class LearningRuntimeHistoryColdStartRequest(BaseModel):
    archive_dir: str = ""
    symbols: list[str] = Field(default_factory=list)
    build_manifest: bool = True
    calibration_ratio: float | None = None
    test_ratio: float | None = None


class LearningManifestTrainingRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    load_predictor: bool = False
    register_model: bool = False


class RegisterModelArtifactRequest(BaseModel):
    artifact_path: str
    role: str = "challenger"
    lifecycle_state: str = "trained"
    source: str = "manual_register_model_artifact"
    parent_model_id: str = ""


class BootstrapActiveChampionRequest(BaseModel):
    artifact_path: str = ""
    source: str = "manual_bootstrap_active_champion"


class ModelRegistryLifecycleRequest(BaseModel):
    model_id: str
    lifecycle_state: str
    blocked_reason: str = ""
    timestamp: str | None = None


class ModelRegistryRoleRequest(BaseModel):
    model_id: str
    role: str
    timestamp: str | None = None


class ShadowDatasetBuildRequest(BaseModel):
    model_id: str
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5


class ChampionShadowReportBuildRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    signal_threshold: float = 0.5
    include_rows: bool = False
    preview_limit: int = 5


class ShadowOnlineV2ReportBuildRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    include_rows: bool = False
    preview_limit: int = 5


class PhaseDAlphalensReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    factor_columns: list[str] = Field(default_factory=list)
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 10])
    quantiles: int = 5
    output_path: str = ""


class PhaseDShapReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    prediction_column: str = "p_meta"
    baseline_importance: dict[str, float] = Field(default_factory=dict)
    drift_threshold: float = 0.25
    top_k: int = 5
    output_path: str = ""


class PhaseDCatBoostShadowReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    label_column: str = "label"
    baseline_probability_column: str = "p_meta"
    test_ratio: float = 0.3
    random_seed: int = 2026
    output_path: str = ""


class PhaseDFinbertReportRequest(BaseModel):
    records: list[dict[str, object]] = Field(default_factory=list)
    model_path: str = ""
    include_neutral: bool = True
    output_path: str = ""


class PhaseDQlibBridgeReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    label_column: str = "label"
    train_ratio: float = 0.6
    valid_ratio: float = 0.2
    output_dir: str = ""


class PhaseDTabularDeepReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    label_column: str = "label"
    baseline_probability_column: str = "p_meta"
    test_ratio: float = 0.3
    random_seed: int = 2026
    output_path: str = ""


class PhaseDTftReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    horizon: int = 1
    encoder_length: int = 5
    train_ratio: float = 0.7
    output_path: str = ""


class PhaseDFinrlReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    feature_columns: list[str] = Field(default_factory=list)
    reward_column: str = "realized_return"
    baseline_probability_column: str = "p_meta"
    test_ratio: float = 0.3
    random_seed: int = 2026
    action_threshold: float = 0.55
    output_path: str = ""


class PhaseDHeavyTsReportRequest(BaseModel):
    model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    horizon: int = 3
    lookback: int = 8
    test_ratio: float = 0.3
    random_seed: int = 2026
    output_path: str = ""


class ExecutionRiskTrainRequest(BaseModel):
    artifact_path: str | None = None
    maturity_statuses: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    min_samples_per_target: int = 24
    calibration_ratio: float = 0.2
    test_ratio: float = 0.2
    epochs: int = 240
    learning_rate: float = 0.05
    l2: float = 1e-3
    seed: int = 42
    now: str | None = None


class ExecutionAwareReportBuildRequest(BaseModel):
    model_id: str
    execution_risk_artifact_path: str = ""
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5


class LearningManifestShadowValidationRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    load_predictor: bool = False
    mark_shadow_validated: bool = False


class LearningModelPromotionGateRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    preview_limit: int = 5
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False


class LearningManifestShadowPromotionGateRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    load_predictor: bool = False
    mark_shadow_validated: bool = True
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False


class LearningModelProposalRequest(BaseModel):
    model_id: str
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    preview_limit: int = 5
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False
    allow_warn_status: bool = True
    source_trace_id: str = ""


class LearningManifestShadowProposalRequest(BaseModel):
    dataset_manifest_id: str = ""
    artifact_path: str | None = None
    champion_model_id: str = ""
    split_names: list[str] = Field(default_factory=list)
    max_rows: int | None = None
    include_rows: bool = False
    preview_limit: int = 5
    max_samples: int | None = None
    min_samples: int = 5
    learning_rate: float = 0.1
    signal_threshold: float = 0.5
    load_predictor: bool = False
    mark_shadow_validated: bool = True
    min_shadow_v2_minus_champion_return: float = -0.02
    max_shadow_v2_brier_delta: float = 0.05
    max_shadow_v2_logloss_delta: float = 0.10
    max_signal_divergence_ratio: float | None = None
    approve_if_passed: bool = False
    block_if_failed: bool = False
    allow_warn_status: bool = True
    source_trace_id: str = ""


class LearningModelProposalApprovalRequest(BaseModel):
    approver: str
    approved: bool
    proposal_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelProposalRevokeRequest(BaseModel):
    revoked_by: str
    proposal_id: str = ""
    note: str = ""
    revoke_model: bool = True
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketRequest(BaseModel):
    operator: str
    proposal_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketExecuteRequest(BaseModel):
    executor: str
    ticket_id: str = ""
    note: str = ""
    confirm_window: bool = True
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketConfirmRequest(BaseModel):
    confirmer: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseTicketRollbackRequest(BaseModel):
    rollback_by: str
    ticket_id: str = ""
    note: str = ""
    now: str | None = None
    source_trace_id: str = ""


class LearningModelReleaseConfirmationWatchdogRequest(BaseModel):
    now: str | None = None
    source_trace_id: str = ""


class DashboardQuickCommandRequest(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    command_id: str = ""


class DashboardQuickReconcileRequest(BaseModel):
    positions: list[BrokerPositionItem] = Field(default_factory=list)
    run_reconcile: bool = True
    source_trace_id: str = ""


class DashboardOpsToggleRequest(BaseModel):
    enabled: bool


class Week4AcceptanceRunRequest(BaseModel):
    sla_recent_runs: int = Field(default=50, ge=1, le=1000)
    export_enabled: bool | None = None
    notify_enabled: bool | None = None


class Week5ScanRunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    notify_enabled: bool | None = None
    sync_watchlist: bool | None = None
    sync_reason: str = ""


class Week6RunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    notify_enabled: bool | None = None


class Week6DataQualityRunRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    lookback_days: int | None = Field(default=None, ge=20, le=500)
    notify_enabled: bool | None = None
    source_trace_id: str = ""


class Week6GlobalSnapshotRequest(BaseModel):
    us_index_change_pct: float = 0.0
    a50_change_pct: float = 0.0
    usd_cnh_change_pct: float = 0.0
    commodity_change_pct: float = 0.0
    a_share_correlation: float = 0.60
    source_trace_id: str = ""


class Week6RegulatoryEntry(BaseModel):
    symbol: str
    tag: str = ""
    note: str = ""


class Week6RegulatoryWatchlistRequest(BaseModel):
    entries: list[Week6RegulatoryEntry] = Field(default_factory=list)
    source_trace_id: str = ""


class Week7StrategyPerformanceRequest(BaseModel):
    month: str
    strategy: str
    strategy_return: float
    benchmark_return: float
    note: str = ""
    source_trace_id: str = ""


class Week7KillSwitchResetRequest(BaseModel):
    strategy: str = ""
    resume_new_buy: bool = False
    source_trace_id: str = ""


class Week7CloudBackupPingRequest(BaseModel):
    source: str = "manual"
    source_trace_id: str = ""


class Week7CloudBackupCheckRequest(BaseModel):
    now: str = ""
    source_trace_id: str = ""


class Week7FactorFeatureItem(BaseModel):
    name: str
    importance: float


class Week7FactorLifecycleRecordRequest(BaseModel):
    month: str
    strategy: str
    top_features: list[Week7FactorFeatureItem] = Field(default_factory=list)
    psr: float
    ic_mean: float = 0.0
    note: str = ""
    source_trace_id: str = ""


class Week7FactorLifecycleResetRequest(BaseModel):
    strategy: str = ""
    source_trace_id: str = ""


class Week7SimBrokerRunRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=30)
    export_enabled: bool | None = None
    notify_enabled: bool | None = None
    source_trace_id: str = ""


class NewsScoreCacheClearRequest(BaseModel):
    symbol: str = ""
    strategy: str = ""


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mode": _config.app.mode,
        "provider": _service.provider_status(),
        "runtime": _service.runtime_status(include_learning_governance=False),
    }


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
def dashboard_page() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=307)


@app.get("/dashboard/recommendations", include_in_schema=False)
def dashboard_recommendations_page() -> RedirectResponse:
    return RedirectResponse(url="/ui/recommendations", status_code=307)


@app.get("/dashboard/stage", include_in_schema=False)
def dashboard_stage_page() -> RedirectResponse:
    return RedirectResponse(url="/ui/runtime-stage", status_code=307)


def _dashboard_quick_enabled() -> bool:
    return _config.app.mode.strip().lower() == "simulation" and _dashboard_ops_enabled


def _dashboard_ops_state() -> dict[str, object]:
    advisory_only = bool(_config.app.advisory_only)
    market_warehouse = _service.market_warehouse_runtime_status()
    return {
        "mode": _config.app.mode,
        "simulation_mode": _config.app.mode.strip().lower() == "simulation",
        "enabled": _dashboard_quick_enabled(),
        "toggle_enabled": _config.app.mode.strip().lower() == "simulation",
        "advisory_only": advisory_only,
        "execution_mode": "advisory_only" if advisory_only else "portfolio_auto_apply",
        "market_warehouse": market_warehouse,
    }


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


@app.post("/run/pipeline")
def run_pipeline(request: PipelineRunRequest) -> dict[str, object]:
    return _service.run_pipeline(
        symbols=request.symbols,
        strategy=request.strategy,
        current_equity=request.current_equity,
    )


@app.get("/news/score")
def news_score_preview(
    symbol: str = Query(min_length=1),
    strategy: str = Query(default="trend"),
) -> dict[str, object]:
    return _service.preview_news_component(symbol=symbol, strategy=strategy)


@app.get("/news/score/batch")
def news_score_preview_batch(
    symbols: Annotated[list[str], Query(min_length=1)],
    strategy: str = Query(default="trend"),
) -> dict[str, object]:
    return _service.preview_news_components(symbols=symbols, strategy=strategy)


@app.get("/news/score/watchlist")
def news_score_preview_watchlist(
    strategy: str = Query(default="trend"),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return _service.preview_news_watchlist(strategy=strategy, limit=limit)


@app.get("/news/briefing/latest")
def news_briefing_latest(
    phase: str = Query(default="premarket"),
    strategy: str = Query(default="trend"),
    max_symbols: int = Query(default=6, ge=1, le=20),
    limit: int = Query(default=6, ge=1, le=20),
    force_refresh: bool = Query(default=False),
) -> dict[str, object]:
    return _service.build_live_news_briefing(
        phase=phase,
        strategy=strategy,
        max_symbols=max_symbols,
        max_items=limit,
        force_refresh=force_refresh,
    )


@app.get("/news/score/history")
def news_score_history(
    limit: int = Query(default=50, ge=1, le=500),
    symbol: str = Query(default=""),
    strategy: str = Query(default=""),
) -> dict[str, object]:
    return _service.news_score_history(
        limit=limit,
        symbol=symbol,
        strategy=strategy,
    )


@app.get("/news/score/cache/state")
def news_score_cache_state() -> dict[str, object]:
    return _service.news_score_cache_state()


@app.post("/news/score/cache/clear")
def news_score_cache_clear(request: NewsScoreCacheClearRequest) -> dict[str, object]:
    return _service.clear_news_score_cache(
        symbol=request.symbol,
        strategy=request.strategy,
    )


@app.get("/risk/status")
def risk_status() -> dict[str, object]:
    report = _service.latest_report()
    if report is None:
        return {"status": "no_run"}
    return {
        "trace_id": report.get("trace_id"),
        "degraded_mode": report.get("degraded_mode"),
        "risk": report.get("risk"),
    }


@app.get("/signals/latest")
def latest_signals() -> dict[str, object]:
    payload = _service.latest_signals_snapshot()
    if not payload.get("trace_id"):
        return {"signals": []}
    return payload


@app.post("/research/signal-quality/run")
def run_signal_quality_audit(request: SignalQualityAuditRequest) -> dict[str, object]:
    return _service.run_signal_quality_audit(
        limit=request.limit,
        include_audit_events=request.include_audit_events,
    )


@app.get("/research/signal-quality/latest")
def latest_signal_quality_audit() -> dict[str, object]:
    return _service.latest_signal_quality_audit()


@app.get("/research/signal-quality/history")
def signal_quality_audit_history(
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return _service.signal_quality_audit_history(limit=limit)


@app.post("/notify/test")
def notify_test(request: NotificationRequest) -> dict[str, object]:
    return _service.notify(
        title=request.title,
        content=request.content,
        level=request.level,
        trace_id=request.trace_id,
    )


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
        str(item).strip()
        for item in _config.wecom_interaction.allowed_users
        if str(item).strip()
    }
    if not allowed:
        return True
    return user_id in allowed


def _interactive_source_slug(source_user: str) -> str:
    return "".join(
        ch for ch in (source_user or "") if ch.isascii() and (ch.isalnum() or ch in {"-", "_"})
    )


def _interactive_pct(value: object) -> str:
    return f"{_as_float(value, default=0.0):.2%}"


def _interactive_num(value: object) -> str:
    raw = _as_float(value, default=0.0)
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
            entry_price = _as_float(manual_fill.get("entry_price"), default=0.0)
            quantity = _as_int(manual_fill.get("quantity"), default=0)
            fee = _as_float(manual_fill.get("fee"), default=0.0)
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
            exit_price = _as_float(close_fill.get("exit_price"), default=0.0)
            quantity = _as_int(close_fill.get("quantity"), default=0)
            fee = _as_float(close_fill.get("fee"), default=0.0)
            if exit_price > 0:
                parts.append(f"价格 {_interactive_num(exit_price)}")
            if quantity > 0:
                parts.append(f"数量 {quantity}")
            if fee > 0:
                parts.append(f"手续费 {_interactive_num(fee)}")
        return (
            f"已登记卖出：{symbol}\n" + " | ".join(parts)
            if parts
            else f"已登记卖出：{symbol}"
        )

    if action == "CLOSE_ALL_POSITIONS":
        closed_count = _as_int(update.get("closed_count"), default=0)
        return f"已全平持仓，共 {closed_count} 条"

    if action == "SET_BROKER_POSITIONS":
        snapshot = update.get("snapshot", {})
        if isinstance(snapshot, dict):
            broker_positions = _as_int(snapshot.get("broker_positions"), default=0)
            return f"已同步券商持仓，共 {broker_positions} 条"
        return "已同步券商持仓"

    if action == "RUN_RECONCILE":
        report = update.get("report", {})
        if isinstance(report, dict):
            status = str(report.get("status", "")).strip() or "unknown"
            mismatch = _as_int(report.get("mismatch_count"), default=0)
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

    return f"已执行：{action}"


def _normalize_interactive_command(parsed: WeComParsedCommand) -> tuple[WeComParsedCommand, str]:
    if parsed.kind != "execute" or parsed.action != "SET_POSITION":
        return parsed, ""

    payload = dict(parsed.payload)
    target_position = _as_float(payload.get("target_position"), default=0.0)
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

    entry_price = _as_float(payload.get("entry_price"), default=0.0)
    quantity = _as_int(payload.get("quantity"), default=0)
    total_asset = _as_float(payload.get("total_asset"), default=0.0)
    if total_asset <= 0.0:
        total_asset = _as_float(_config.dashboard.default_total_asset, default=0.0)
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
    result = _service.execute_command(envelope)
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
        reconcile_result = _service.execute_command(reconcile_envelope)
        reconcile_update = reconcile_result.get("command_update", {})
        if isinstance(reconcile_update, dict):
            report = reconcile_update.get("report", {})
            if isinstance(report, dict):
                status = str(report.get("status", "")).strip() or "unknown"
                mismatch = int(report.get("mismatch_count", 0))
                lines.append(f"自动对账：状态 {status}，差异 {mismatch} 项")
    return "\n".join(lines)


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
        if parsed.query == "positions":
            return format_positions_text(_service.portfolio_positions())
        if parsed.query == "trades":
            return format_trades_text(_service.portfolio_trades(limit=8))
        if parsed.query == "news_score":
            symbol = str(parsed.payload.get("symbol", "")).strip()
            strategy = str(parsed.payload.get("strategy", "trend")).strip() or "trend"
            payload = _service.preview_news_component(symbol=symbol, strategy=strategy)
            score = _as_float(payload.get("news_component", 0.5), default=0.5)
            status = str(payload.get("status", "unknown")).strip() or "unknown"
            reasons = payload.get("reasons", [])
            reason_text = ""
            if isinstance(reasons, list) and reasons:
                reason_text = str(reasons[0])
            return (
                f"news_score {symbol} {strategy}={score:.3f} status={status}"
                + (f" reason={reason_text}" if reason_text else "")
            )
        if parsed.query == "news_watchlist":
            raw_limit = parsed.payload.get("limit", 10)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            strategy = str(parsed.payload.get("strategy", "trend")).strip() or "trend"
            payload = _service.preview_news_watchlist(strategy=strategy, limit=limit)
            items = payload.get("items", [])
            source = str(payload.get("source", "watchlist")).strip() or "watchlist"
            records = _as_int(payload.get("records", 0))
            summary = payload.get("summary", {})
            avg = 0.5
            if isinstance(summary, dict):
                avg = _as_float(summary.get("average_news_component", 0.5), default=0.5)
            lines = [
                f"news_watchlist strategy={strategy} records={records} avg={avg:.3f} source={source}"
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
            payload = _service.news_score_cache_state()
            entries = _as_int(payload.get("entries", 0))
            ttl_sec = _as_int(payload.get("ttl_sec", 0))
            return f"news_cache entries={entries} ttl_sec={ttl_sec}"
        if parsed.query == "news_cache_clear":
            symbol = str(parsed.payload.get("symbol", "")).strip()
            strategy = str(parsed.payload.get("strategy", "")).strip().lower()
            payload = _service.clear_news_score_cache(symbol=symbol, strategy=strategy)
            cleared = _as_int(payload.get("cleared", 0))
            remaining = _as_int(payload.get("remaining", 0))
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
            payload = _service.news_score_history(
                limit=limit,
                symbol=symbol,
                strategy=strategy,
            )
            records = _as_int(payload.get("records", 0))
            summary = payload.get("summary", {})
            avg = 0.5
            if isinstance(summary, dict):
                avg = _as_float(summary.get("average_news_component", 0.5), default=0.5)
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
            payload = _service.recommendation_lifecycle(status=status, limit=limit)
            summary = payload.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            breakdown = summary.get("status_breakdown", {})
            if not isinstance(breakdown, dict):
                breakdown = {}
            breakdown_text = ",".join(
                f"{key}:{int(value)}" for key, value in sorted(breakdown.items())
            ) or "-"
            lines = [
                f"lifecycle records={_as_int(payload.get('records', 0))} status={status or 'all'} breakdown={breakdown_text}"
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
            payload = _service.holding_alerts(now=datetime.now())
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
            payload = _service.execution_bias_report(days=days, limit=limit)
            summary = payload.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            lines = [
                f"execution_bias days={days} records={_as_int(payload.get('records', 0))} "
                f"avg_pos={_as_float(summary.get('avg_abs_position_bias', 0.0)):.4f} "
                f"avg_price={_as_float(summary.get('avg_abs_price_bias_pct', 0.0)) * 100:.2f}% "
                f"better_rate={_as_float(summary.get('better_price_rate', 0.0)):.2%} "
                f"worse_rate={_as_float(summary.get('worse_price_rate', 0.0)):.2%}"
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
        str(item).strip()
        for item in _config.feishu_interaction.allowed_users
        if str(item).strip()
    }
    if not allowed:
        return True
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
    if stage_cache_key and _service._cache.exists(stage_cache_key):
        _record_service_audit_event(
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

    notifier = FeishuAppNotifier(
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
        _service._cache.set(
            stage_cache_key,
            "1",
            ttl_sec=max(60, int(_config.command_channel.dedup_ttl_sec)),
        )
    _record_service_audit_event(
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
        _record_service_audit_event(
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
    if dedup_key and _service._cache.exists(dedup_key):
        _record_service_audit_event(
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
        _service._cache.set(
            dedup_key,
            "1",
            ttl_sec=max(60, int(_config.command_channel.dedup_ttl_sec)),
        )

    _record_service_audit_event(
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
        _launch_feishu_message_final_reply(
            event,
            source=source_name,
            trace_id=trace_id,
        )
    except Exception as exc:
        _record_service_audit_event(
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


@app.get("/wecom/callback")
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


@app.post("/wecom/callback")
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


@app.get("/feishu/long_connection/status")
def feishu_long_connection_status() -> dict[str, object]:
    return _feishu_long_connection_status_payload()


@app.post("/feishu/callback")
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

@app.post("/command/execute")
def execute_command(request: CommandRequest) -> dict[str, object]:
    envelope = CommandEnvelope(
        command_id=request.command_id,
        timestamp=request.timestamp,
        action=request.action,
        payload=request.payload,
        signature=request.signature,
    )
    return _service.execute_command(envelope)


@app.get("/command/state")
def command_state() -> dict[str, object]:
    return {"state": _service.runtime_status().get("state", {})}


@app.post("/scheduler/run_due")
def run_scheduler(request: SchedulerRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return {"results": _service.run_due_jobs(now=now_dt)}


@app.post("/tdx/sync/run")
def tdx_sync_run(request: TdxSyncRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_tdx_offline_sync(
        timestamp=now_dt,
        notify_enabled=request.notify_enabled,
        force=request.force,
        source_trace_id=request.source_trace_id,
    )


@app.get("/tdx/sync/latest")
def tdx_sync_latest() -> dict[str, object]:
    return {"report": _service.latest_tdx_sync_report()}


@app.get("/tdx/sync/history")
def tdx_sync_history(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return _service.tdx_sync_history(limit=limit)


@app.post("/warehouse/sync/run")
def warehouse_sync_run(request: WarehouseSyncRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_market_warehouse_sync(
        timestamp=now_dt,
        notify_enabled=request.notify_enabled,
        force=request.force,
        source_trace_id=request.source_trace_id,
        symbols=request.symbols or None,
        retry_failed_only=request.retry_failed_only,
        retry_report_trace_id=request.retry_report_trace_id,
    )


@app.get("/warehouse/sync/latest")
def warehouse_sync_latest() -> dict[str, object]:
    return {"report": _service.latest_market_warehouse_report()}


@app.get("/warehouse/sync/status")
def warehouse_sync_status() -> dict[str, object]:
    return _service.market_warehouse_runtime_status()


@app.get("/warehouse/background/status")
def warehouse_background_status() -> dict[str, object]:
    return _service.market_warehouse_background_data_status()


@app.get("/warehouse/sync/history")
def warehouse_sync_history(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return _service.market_warehouse_history(limit=limit)


@app.post("/idle/run")
def idle_run(request: IdleQueueRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_idle_queue_cycle(now=now_dt, source_trace_id=request.source_trace_id)


@app.get("/idle/latest")
def idle_latest() -> dict[str, object]:
    return {"report": _service.latest_idle_queue_report()}


@app.get("/idle/history")
def idle_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.idle_queue_history(limit=limit)


@app.get("/idle/state")
def idle_state() -> dict[str, object]:
    return _service.idle_queue_state()


@app.post("/idle/ack")
def idle_ack(request: IdleQueueAckRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.idle_queue_ack_blocked(
        task_id=request.task_id,
        clear_all=request.clear_all,
        now=now_dt,
    )


@app.post("/evolution/run")
def evolution_run(request: EvolutionRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    symbols = request.symbols if request.symbols else None
    return _service.run_evolution_offhours(
        symbols=symbols,
        timestamp=now_dt,
        dry_run=request.dry_run,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/drill")
def evolution_drill(request: EvolutionDrillRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_evolution_drill(
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.get("/evolution/latest")
def evolution_latest() -> dict[str, object]:
    return {"report": _service.latest_evolution_report()}


@app.get("/evolution/history")
def evolution_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.evolution_history(limit=limit)


@app.get("/evolution/preflight")
def evolution_preflight() -> dict[str, object]:
    return _service.evolution_preflight()


@app.get("/evolution/window_report")
def evolution_window_report(
    days: int = Query(default=10, ge=1, le=60),
    min_runs: int = Query(default=5, ge=1, le=1000),
) -> dict[str, object]:
    return _service.evolution_window_report(days=days, min_runs=min_runs)


@app.post("/evolution/m3/maintenance")
def evolution_m3_maintenance(request: EvolutionM3MaintenanceRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_evolution_m3_maintenance(
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/m3/search")
def evolution_m3_search(request: EvolutionM3SearchRequest) -> dict[str, object]:
    return _service.run_evolution_m3_search(
        vector=request.vector,
        top_k=request.top_k,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/m8/suggest")
def evolution_m8_suggest(request: EvolutionM8SuggestRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    symbols = request.symbols if request.symbols else None
    return _service.run_evolution_m8_suggest(
        symbols=symbols,
        top_k=request.top_k,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/release/attempt")
def evolution_release_attempt(request: EvolutionReleaseAttemptRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.attempt_evolution_release(
        days=request.days,
        min_runs=request.min_runs,
        now=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.get("/evolution/release/latest")
def evolution_release_latest() -> dict[str, object]:
    decision = _service.latest_evolution_release_gate()
    if decision is None:
        return {"status": "no_decision"}
    return {"decision": decision}


@app.get("/evolution/release/history")
def evolution_release_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.evolution_release_gate_history(limit=limit)


@app.post("/evolution/release/approval")
def evolution_release_approval(request: EvolutionReleaseApprovalRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.record_evolution_release_approval(
        approver=request.approver,
        approved=request.approved,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.get("/evolution/release/approval/latest")
def evolution_release_approval_latest() -> dict[str, object]:
    record = _service.latest_evolution_release_approval()
    if record is None:
        return {"status": "no_record"}
    return {"record": record}


@app.get("/evolution/release/approval/history")
def evolution_release_approval_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return _service.evolution_release_approval_history(limit=limit)


@app.post("/evolution/release/ticket")
def evolution_release_ticket(request: EvolutionReleaseTicketRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.issue_evolution_release_ticket(
        operator=request.operator,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/release/ticket/execute")
def evolution_release_ticket_execute(
    request: EvolutionReleaseTicketExecuteRequest,
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.execute_evolution_release_ticket(
        executor=request.executor,
        ticket_id=request.ticket_id,
        note=request.note,
        confirm_window=request.confirm_window,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/release/ticket/confirm")
def evolution_release_ticket_confirm(
    request: EvolutionReleaseTicketConfirmRequest,
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.confirm_evolution_release_ticket(
        confirmer=request.confirmer,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/release/ticket/rollback")
def evolution_release_ticket_rollback(
    request: EvolutionReleaseTicketRollbackRequest,
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.rollback_evolution_release_ticket(
        rollback_by=request.rollback_by,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/evolution/release/confirmation/watchdog")
def evolution_release_confirmation_watchdog(
    request: EvolutionReleaseConfirmationWatchdogRequest,
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_evolution_release_confirmation_watchdog(
        now=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.get("/evolution/release/ticket/latest")
def evolution_release_ticket_latest() -> dict[str, object]:
    ticket = _service.latest_evolution_release_ticket()
    if ticket is None:
        return {"status": "no_ticket"}
    return {"ticket": ticket}


@app.get("/evolution/release/ticket/history")
def evolution_release_ticket_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return _service.evolution_release_ticket_history(limit=limit)


@app.get("/evolution/release/ticket/timeline")
def evolution_release_ticket_timeline(
    ticket_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    return _service.evolution_release_ticket_timeline(
        ticket_id=ticket_id,
        status=status,
        limit=limit,
    )


@app.post("/train/models")
def train_models(request: TrainModelsRequest) -> dict[str, object]:
    return _service.train_models(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
        artifact_path=request.artifact_path,
        full_market=request.full_market,
        max_symbols=request.max_symbols,
    )


@app.post("/train/learning-manifest")
def train_learning_manifest(request: LearningManifestTrainingRequest) -> dict[str, object]:
    return _service.train_learning_manifest(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        load_predictor=request.load_predictor,
        register_model=request.register_model,
    )


@app.post("/train/learning-manifest/shadow-validate")
def train_learning_manifest_shadow_validate(
    request: LearningManifestShadowValidationRequest,
) -> dict[str, object]:
    return _service.run_learning_manifest_shadow_validation(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        load_predictor=request.load_predictor,
        mark_shadow_validated=request.mark_shadow_validated,
    )


@app.post("/train/learning-manifest/shadow-promote")
def train_learning_manifest_shadow_promote(
    request: LearningManifestShadowPromotionGateRequest,
) -> dict[str, object]:
    return _service.run_learning_manifest_shadow_promotion_gate(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        load_predictor=request.load_predictor,
        mark_shadow_validated=request.mark_shadow_validated,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
    )


@app.post("/learning/models/proposal")
def learning_model_proposal_create(request: LearningModelProposalRequest) -> dict[str, object]:
    return _service.create_learning_model_proposal(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        preview_limit=request.preview_limit,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
        allow_warn_status=request.allow_warn_status,
        source_trace_id=request.source_trace_id,
    )


@app.post("/train/learning-manifest/shadow-proposal")
def train_learning_manifest_shadow_proposal(
    request: LearningManifestShadowProposalRequest,
) -> dict[str, object]:
    return _service.run_learning_manifest_shadow_proposal(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        load_predictor=request.load_predictor,
        mark_shadow_validated=request.mark_shadow_validated,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
        allow_warn_status=request.allow_warn_status,
        source_trace_id=request.source_trace_id,
    )


@app.get("/learning/models/proposal/latest")
def learning_model_proposal_latest() -> dict[str, object]:
    proposal = _service.latest_learning_model_proposal()
    if proposal is None:
        return {"status": "no_proposal"}
    return {"proposal": proposal}


@app.get("/learning/models/proposal/history")
def learning_model_proposal_history(
    limit: int = Query(default=20, ge=1, le=500),
    proposal_id: str = Query(default=""),
    status: str = Query(default=""),
) -> dict[str, object]:
    return _service.learning_model_proposal_history(
        limit=limit,
        proposal_id=proposal_id,
        status=status,
    )


@app.post("/learning/models/proposal/approval")
def learning_model_proposal_approval(
    request: LearningModelProposalApprovalRequest,
) -> dict[str, object]:
    return _service.record_learning_model_proposal_approval(
        approver=request.approver,
        approved=request.approved,
        proposal_id=request.proposal_id,
        note=request.note,
        timestamp=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.post("/learning/models/proposal/revoke")
def learning_model_proposal_revoke(
    request: LearningModelProposalRevokeRequest,
) -> dict[str, object]:
    return _service.revoke_learning_model_proposal(
        revoked_by=request.revoked_by,
        proposal_id=request.proposal_id,
        note=request.note,
        revoke_model=request.revoke_model,
        timestamp=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.get("/learning/models/proposal/approval/latest")
def learning_model_proposal_approval_latest() -> dict[str, object]:
    record = _service.latest_learning_model_approval()
    if record is None:
        return {"status": "no_record"}
    return {"record": record}


@app.get("/learning/models/proposal/approval/history")
def learning_model_proposal_approval_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return _service.learning_model_approval_history(limit=limit)


@app.post("/learning/models/release/ticket")
def learning_model_release_ticket_issue(
    request: LearningModelReleaseTicketRequest,
) -> dict[str, object]:
    return _service.issue_learning_model_release_ticket(
        operator=request.operator,
        proposal_id=request.proposal_id,
        note=request.note,
        timestamp=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.post("/learning/models/release/ticket/execute")
def learning_model_release_ticket_execute(
    request: LearningModelReleaseTicketExecuteRequest,
) -> dict[str, object]:
    return _service.execute_learning_model_release_ticket(
        executor=request.executor,
        ticket_id=request.ticket_id,
        note=request.note,
        confirm_window=request.confirm_window,
        timestamp=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.post("/learning/models/release/ticket/confirm")
def learning_model_release_ticket_confirm(
    request: LearningModelReleaseTicketConfirmRequest,
) -> dict[str, object]:
    return _service.confirm_learning_model_release_ticket(
        confirmer=request.confirmer,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.post("/learning/models/release/ticket/rollback")
def learning_model_release_ticket_rollback(
    request: LearningModelReleaseTicketRollbackRequest,
) -> dict[str, object]:
    return _service.rollback_learning_model_release_ticket(
        rollback_by=request.rollback_by,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.post("/learning/models/release/confirmation/watchdog")
def learning_model_release_confirmation_watchdog(
    request: LearningModelReleaseConfirmationWatchdogRequest,
) -> dict[str, object]:
    return _service.run_learning_model_release_confirmation_watchdog(
        now=_parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@app.get("/learning/models/release/ticket/latest")
def learning_model_release_ticket_latest() -> dict[str, object]:
    ticket = _service.latest_learning_model_release_ticket()
    if ticket is None:
        return {"status": "no_ticket"}
    return {"ticket": ticket}


@app.get("/learning/models/release/ticket/history")
def learning_model_release_ticket_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return _service.learning_model_release_ticket_history(limit=limit)


@app.get("/learning/models/release/ticket/timeline")
def learning_model_release_ticket_timeline(
    ticket_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    return _service.learning_model_release_ticket_timeline(
        ticket_id=ticket_id,
        status=status,
        limit=limit,
    )


@app.get("/learning/models/governance/status")
def learning_model_governance_status(
    proposal_limit: int = Query(default=20, ge=1, le=200),
    ticket_limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return _service.learning_model_governance_status(
        proposal_limit=proposal_limit,
        ticket_limit=ticket_limit,
    )


@app.post("/models/registry/promotion-gate")
def model_registry_promotion_gate(
    request: LearningModelPromotionGateRequest,
) -> dict[str, object]:
    return _service.evaluate_learning_model_promotion_gate(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        preview_limit=request.preview_limit,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
    )


@app.get("/models/registry")
def model_registry_entries(
    limit: int = Query(default=20, ge=1, le=200),
    role: str = "",
    lifecycle_state: str = "",
) -> dict[str, object]:
    return _service.model_registry_entries(
        limit=limit,
        role=role,
        lifecycle_state=lifecycle_state,
    )


@app.get("/models/registry/status")
def model_registry_status(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return _service.model_registry_status(limit=limit)


@app.post("/models/registry/register")
def register_model_artifact(request: RegisterModelArtifactRequest) -> dict[str, object]:
    return _service.register_model_artifact(
        artifact_path=request.artifact_path,
        role=request.role,
        lifecycle_state=request.lifecycle_state,
        source=request.source,
        parent_model_id=request.parent_model_id,
    )


@app.post("/models/registry/bootstrap-active-champion")
def bootstrap_active_champion(
    request: BootstrapActiveChampionRequest,
) -> dict[str, object]:
    return _service.bootstrap_active_champion_from_artifact(
        artifact_path=request.artifact_path,
        source=request.source,
    )


@app.post("/models/registry/lifecycle")
def update_model_registry_lifecycle(request: ModelRegistryLifecycleRequest) -> dict[str, object]:
    return _service.update_model_registry_lifecycle(
        model_id=request.model_id,
        lifecycle_state=request.lifecycle_state,
        blocked_reason=request.blocked_reason,
        timestamp=_parse_optional_datetime(request.timestamp or ""),
    )


@app.post("/models/registry/role")
def update_model_registry_role(request: ModelRegistryRoleRequest) -> dict[str, object]:
    return _service.update_model_registry_role(
        model_id=request.model_id,
        role=request.role,
        timestamp=_parse_optional_datetime(request.timestamp or ""),
    )


@app.get("/models/registry/{model_id}")
def model_registry_entry(model_id: str) -> dict[str, object] | None:
    return _service.model_registry_entry(model_id=model_id)


@app.post("/models/shadow-dataset")
def build_shadow_dataset(request: ShadowDatasetBuildRequest) -> dict[str, object]:
    return _service.build_shadow_dataset(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@app.post("/models/champion-shadow-report")
def build_champion_shadow_report(request: ChampionShadowReportBuildRequest) -> dict[str, object]:
    return _service.build_champion_shadow_report(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        signal_threshold=request.signal_threshold,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@app.post("/models/shadow-online-v2-report")
def build_shadow_online_v2_report(request: ShadowOnlineV2ReportBuildRequest) -> dict[str, object]:
    return _service.build_shadow_online_v2_report(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@app.get("/shadow/v2/status")
def shadow_v2_status(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return _service.shadow_v2_status(limit=limit)


@app.post("/research/alphalens/report")
def build_phase_d_alphalens_report(
    request: PhaseDAlphalensReportRequest,
) -> dict[str, object]:
    return _service.build_phase_d_alphalens_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        factor_columns=request.factor_columns or None,
        horizons=request.horizons or (1, 5, 10),
        quantiles=request.quantiles,
        output_path=request.output_path or None,
    )


@app.post("/research/shap/report")
def build_phase_d_shap_report(request: PhaseDShapReportRequest) -> dict[str, object]:
    return _service.build_phase_d_shap_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        prediction_column=request.prediction_column,
        baseline_importance=request.baseline_importance,
        drift_threshold=request.drift_threshold,
        top_k=request.top_k,
        output_path=request.output_path or None,
    )


@app.post("/research/catboost-shadow/report")
def build_phase_d_catboost_shadow_report(
    request: PhaseDCatBoostShadowReportRequest,
) -> dict[str, object]:
    return _service.build_phase_d_catboost_shadow_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        feature_columns=request.feature_columns or None,
        label_column=request.label_column,
        baseline_probability_column=request.baseline_probability_column,
        test_ratio=request.test_ratio,
        random_seed=request.random_seed,
        output_path=request.output_path or None,
    )


@app.post("/research/finbert/report")
def build_phase_d_finbert_report(request: PhaseDFinbertReportRequest) -> dict[str, object]:
    return _service.build_phase_d_finbert_report(
        records=request.records,
        model_path=request.model_path,
        include_neutral=request.include_neutral,
        output_path=request.output_path or None,
    )


@app.post("/research/qlib-bridge/report")
def build_phase_d_qlib_bridge_report(
    request: PhaseDQlibBridgeReportRequest,
) -> dict[str, object]:
    return _service.build_phase_d_qlib_bridge_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        feature_columns=request.feature_columns or None,
        label_column=request.label_column,
        train_ratio=request.train_ratio,
        valid_ratio=request.valid_ratio,
        output_dir=request.output_dir or None,
    )


@app.post("/research/tabular-deep/report")
def build_phase_d_tabular_deep_report(
    request: PhaseDTabularDeepReportRequest,
) -> dict[str, object]:
    return _service.build_phase_d_tabular_deep_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        feature_columns=request.feature_columns or None,
        label_column=request.label_column,
        baseline_probability_column=request.baseline_probability_column,
        test_ratio=request.test_ratio,
        random_seed=request.random_seed,
        output_path=request.output_path or None,
    )


@app.post("/research/tft/report")
def build_phase_d_tft_report(request: PhaseDTftReportRequest) -> dict[str, object]:
    return _service.build_phase_d_tft_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        horizon=request.horizon,
        encoder_length=request.encoder_length,
        train_ratio=request.train_ratio,
        output_path=request.output_path or None,
    )


@app.post("/research/finrl/report")
def build_phase_d_finrl_report(request: PhaseDFinrlReportRequest) -> dict[str, object]:
    return _service.build_phase_d_finrl_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        feature_columns=request.feature_columns or None,
        reward_column=request.reward_column,
        baseline_probability_column=request.baseline_probability_column,
        test_ratio=request.test_ratio,
        random_seed=request.random_seed,
        action_threshold=request.action_threshold,
        output_path=request.output_path or None,
    )


@app.post("/research/heavy-ts/report")
def build_phase_d_heavy_ts_report(
    request: PhaseDHeavyTsReportRequest,
) -> dict[str, object]:
    return _service.build_phase_d_heavy_ts_report(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        horizon=request.horizon,
        lookback=request.lookback,
        test_ratio=request.test_ratio,
        random_seed=request.random_seed,
        output_path=request.output_path or None,
    )


@app.get("/research/d6/registry")
def phase_d6_registry(output_path: str = Query(default="")) -> dict[str, object]:
    return _service.generate_phase_d6_registry_report(output_path=output_path or None)


@app.post("/train/execution-risk")
def train_execution_risk(request: ExecutionRiskTrainRequest) -> dict[str, object]:
    return _service.train_execution_risk_model(
        artifact_path=request.artifact_path,
        maturity_statuses=request.maturity_statuses or None,
        max_rows=request.max_rows,
        min_samples_per_target=request.min_samples_per_target,
        calibration_ratio=request.calibration_ratio,
        test_ratio=request.test_ratio,
        epochs=request.epochs,
        learning_rate=request.learning_rate,
        l2=request.l2,
        seed=request.seed,
        now=_parse_optional_datetime(request.now or ""),
    )


@app.get("/train/execution-risk/status")
def train_execution_risk_status() -> dict[str, object]:
    return _service.execution_risk_status()


@app.get("/train/execution-risk/history")
def train_execution_risk_history(
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return _service.execution_risk_training_history(limit=limit)


@app.post("/models/execution-aware-report")
def build_execution_aware_report(
    request: ExecutionAwareReportBuildRequest,
) -> dict[str, object]:
    return _service.build_execution_aware_report(
        model_id=request.model_id,
        execution_risk_artifact_path=request.execution_risk_artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@app.get("/train/bootstrap/status")
def train_bootstrap_status() -> dict[str, object]:
    return _service.training_bootstrap_status()


@app.post("/backtest/walk_forward")
def walk_forward(request: WalkForwardRequest) -> dict[str, object]:
    return _service.run_walk_forward(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
    )


@app.post("/acceptance/baseline")
def acceptance_baseline(request: BaselineReportRequest) -> dict[str, object]:
    return _service.generate_baseline_report(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
        output_path=request.output_path,
    )


@app.post("/acceptance/phase_checkpoint")
def acceptance_phase_checkpoint(request: PhaseCheckpointRequest) -> dict[str, object]:
    return _service.generate_phase_checkpoint(
        phase=request.phase,
        baseline_report_path=request.baseline_report_path,
        output_path=request.output_path,
    )


@app.post("/acceptance/v13")
def acceptance_v13(request: V13AcceptanceRequest) -> dict[str, object]:
    return _service.generate_v13_acceptance_report(
        baseline_report_path=request.baseline_report_path,
        output_path=request.output_path,
    )


@app.post("/acceptance/v13/bundle")
def acceptance_v13_bundle(request: V13AcceptanceBundleRequest) -> dict[str, object]:
    return _service.generate_v13_acceptance_bundle(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
        baseline_output_path=request.baseline_output_path,
        v13_output_path=request.v13_output_path,
        run_week5_scan=request.run_week5_scan,
        week5_symbols=request.week5_symbols or None,
    )


@app.post("/stress/run")
def stress_run() -> dict[str, object]:
    return _service.run_stress_tests()


@app.get("/portfolio/positions")
def portfolio_positions() -> dict[str, object]:
    return {"positions": _service.portfolio_positions()}


@app.get("/portfolio/trades")
def portfolio_trades(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, object]:
    return {"trades": _service.portfolio_trades(limit=limit)}


@app.get("/recommendations/lifecycle")
def recommendations_lifecycle(
    status: str = Query(default=""),
    limit: int = Query(default=120, ge=1, le=1000),
) -> dict[str, object]:
    return _service.recommendation_lifecycle(status=status, limit=limit)


@app.get("/portfolio/execution_bias")
def portfolio_execution_bias(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    return _service.execution_bias_report(days=days, limit=limit)


@app.post("/portfolio/broker_snapshot")
def portfolio_broker_snapshot(request: BrokerSnapshotRequest) -> dict[str, object]:
    positions = [item.model_dump() for item in request.positions]
    return _service.update_broker_snapshot(
        positions=positions,
        source_trace_id=request.source_trace_id,
    )


@app.post("/portfolio/reconcile/run")
def portfolio_reconcile_run(request: ReconcileRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return {"report": _service.run_reconciliation(timestamp=now_dt)}


@app.get("/portfolio/reconcile/latest")
def portfolio_reconcile_latest() -> dict[str, object]:
    report = _service.latest_reconcile_report()
    if report is None:
        return {"status": "no_reconcile"}
    return {"report": report}


@app.get("/portfolio/reconcile/weekly")
def portfolio_reconcile_weekly(days: int = Query(default=7, ge=1, le=30)) -> dict[str, object]:
    return _service.reconcile_weekly_report(days=days)


@app.get("/dashboard/portfolio")
def dashboard_portfolio(
    days: int = Query(default=7, ge=1, le=30),
    trade_limit: int = Query(default=120, ge=1, le=1000),
) -> dict[str, object]:
    return _service.dashboard_portfolio(days=days, trade_limit=trade_limit)


@app.get("/dashboard/training-overview")
def dashboard_training_overview(
    history_limit: int = Query(default=6, ge=1, le=20),
) -> dict[str, object]:
    return _service.training_overview(history_limit=history_limit)


@app.get("/runtime/sla")
def runtime_sla(
    recent_runs: int = Query(default=50, ge=1, le=1000),
    session_scope: str = Query(default="all"),
    job_scope: str = Query(default="all"),
    target_ms: int = Query(default=60000, ge=1),
    alert_target_ms: int = Query(default=30000, ge=1),
    max_symbol_count: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    return _service.sla_report(
        recent_runs=recent_runs,
        session_scope=session_scope,
        job_scope=job_scope,
        target_ms=target_ms,
        alert_target_ms=alert_target_ms,
        max_symbol_count=max_symbol_count,
    )


@app.get("/runtime/stage")
def runtime_stage(now: str = Query(default="")) -> dict[str, object]:
    return _service.runtime_stage_snapshot(now=_parse_optional_datetime(now))


@app.get("/runtime/history/archive/status")
def runtime_history_archive_status(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.runtime_history_archive_status(limit=limit)


@app.post("/runtime/history/archive/run")
def runtime_history_archive_run(request: RuntimeArchiveRunRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.archive_runtime_history(now=now_dt, force=request.force)


@app.post("/learning/runtime-history/bootstrap")
def learning_runtime_history_bootstrap(
    request: LearningRuntimeHistoryColdStartRequest,
) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return _service.bootstrap_learning_from_runtime_history(
        archive_dir=request.archive_dir,
        symbols=symbols,
        build_manifest=request.build_manifest,
        calibration_ratio=request.calibration_ratio,
        test_ratio=request.test_ratio,
    )


@app.get("/learning/status")
def learning_status(manifest_limit: int = Query(default=5, ge=1, le=50)) -> dict[str, object]:
    return _service.learning_protocol_status(manifest_limit=manifest_limit)


@app.get("/learning/store/status")
def learning_store_status() -> dict[str, object]:
    return _service.learning_store_status()


@app.get("/learning/store/metrics")
def learning_store_metrics() -> dict[str, object]:
    return _service.learning_store_metrics()


@app.get("/learning/manifests/status")
def learning_manifests_status(
    manifest_limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return _service.learning_manifests_status(manifest_limit=manifest_limit)


@app.get("/m3/profile/status")
def m3_profile_status() -> dict[str, object]:
    return _service.m3_profile_status()


@app.post("/acceptance/week4/run")
def acceptance_week4_run(request: Week4AcceptanceRunRequest) -> dict[str, object]:
    return _service.run_week4_acceptance(
        sla_recent_runs=request.sla_recent_runs,
        export_enabled=request.export_enabled,
        notify_enabled=request.notify_enabled,
    )


@app.get("/acceptance/week4/latest")
def acceptance_week4_latest() -> dict[str, object]:
    report = _service.latest_week4_acceptance_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@app.get("/acceptance/week4/history")
def acceptance_week4_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.week4_acceptance_history(limit=limit)


@app.post("/week5/scan/run")
def week5_scan_run(request: Week5ScanRunRequest) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return _service.run_week5_scan(
        symbols=symbols,
        notify_enabled=request.notify_enabled,
        sync_watchlist=request.sync_watchlist,
        sync_reason=request.sync_reason,
    )


@app.get("/week5/scan/latest")
def week5_scan_latest() -> dict[str, object]:
    report = _service.latest_week5_scan_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@app.get("/week5/scan/history")
def week5_scan_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.week5_scan_history(limit=limit)


@app.get("/week5/signal-pool/live")
def week5_signal_pool_live(
    limit: int = Query(default=30, ge=1, le=100),
    force_refresh: bool = Query(default=False),
) -> dict[str, object]:
    return _service.week5_signal_pool_live(limit=limit, force_refresh=force_refresh)


@app.get("/week5/signal-pool/symbol/live")
def week5_signal_pool_symbol_live(
    symbol: str = Query(default=""),
    force_refresh: bool = Query(default=False),
) -> dict[str, object]:
    return _service.week5_signal_pool_symbol_live(symbol=symbol, force_refresh=force_refresh)


@app.post("/week6/run")
def week6_run(request: Week6RunRequest) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return _service.run_week6_analysis(symbols=symbols, notify_enabled=request.notify_enabled)


@app.get("/week6/latest")
def week6_latest() -> dict[str, object]:
    report = _service.latest_week6_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@app.get("/week6/history")
def week6_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.week6_history(limit=limit)


@app.post("/week6/data-quality/run")
def week6_data_quality_run(request: Week6DataQualityRunRequest) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return _service.run_week6_data_prewarm(
        symbols=symbols,
        lookback_days=request.lookback_days,
        notify_enabled=request.notify_enabled,
        source_trace_id=request.source_trace_id,
    )


@app.get("/week6/data-quality/latest")
def week6_data_quality_latest() -> dict[str, object]:
    report = _service.latest_week6_data_quality_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@app.get("/week6/data-quality/history")
def week6_data_quality_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return _service.week6_data_quality_history(limit=limit)


@app.post("/week6/global/snapshot")
def week6_global_snapshot(request: Week6GlobalSnapshotRequest) -> dict[str, object]:
    return _service.update_global_market_snapshot(
        snapshot=request.model_dump(exclude={"source_trace_id"}),
        source_trace_id=request.source_trace_id,
    )


@app.get("/week6/global/snapshot")
def week6_global_snapshot_get() -> dict[str, object]:
    return _service.global_market_snapshot()


@app.get("/week6/global/history")
def week6_global_history(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, object]:
    return _service.global_market_history(limit=limit)


@app.post("/week6/regulatory/watchlist")
def week6_regulatory_watchlist_set(request: Week6RegulatoryWatchlistRequest) -> dict[str, object]:
    entries = [item.model_dump() for item in request.entries]
    return _service.set_regulatory_watchlist(
        entries=entries,
        source_trace_id=request.source_trace_id,
    )


@app.get("/week6/regulatory/watchlist")
def week6_regulatory_watchlist_get() -> dict[str, object]:
    return _service.regulatory_watchlist()


@app.post("/week7/kill-switch/performance")
def week7_kill_switch_performance(request: Week7StrategyPerformanceRequest) -> dict[str, object]:
    return _service.record_strategy_performance(
        month=request.month,
        strategy=request.strategy,
        strategy_return=request.strategy_return,
        benchmark_return=request.benchmark_return,
        note=request.note,
        source_trace_id=request.source_trace_id,
    )


@app.get("/week7/kill-switch/history")
def week7_kill_switch_history(
    strategy: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict[str, object]:
    return _service.strategy_kill_switch_history(strategy=strategy, limit=limit)


@app.get("/week7/kill-switch/status")
def week7_kill_switch_status(strategy: str = Query(default="")) -> dict[str, object]:
    return _service.strategy_kill_switch_status(strategy=strategy)


@app.post("/week7/kill-switch/reset")
def week7_kill_switch_reset(request: Week7KillSwitchResetRequest) -> dict[str, object]:
    return _service.reset_strategy_kill_switch(
        strategy=request.strategy,
        resume_new_buy=request.resume_new_buy,
        source_trace_id=request.source_trace_id,
    )


@app.post("/week7/cloud-backup/ping")
def week7_cloud_backup_ping(request: Week7CloudBackupPingRequest) -> dict[str, object]:
    return _service.cloud_backup_ping(
        source=request.source,
        source_trace_id=request.source_trace_id,
    )


@app.get("/week7/cloud-backup/status")
def week7_cloud_backup_status() -> dict[str, object]:
    return _service.cloud_backup_status()


@app.post("/week7/cloud-backup/check")
def week7_cloud_backup_check(request: Week7CloudBackupCheckRequest) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return _service.run_cloud_backup_check(
        now=now_dt,
        source_trace_id=request.source_trace_id,
    )


@app.post("/week7/factor-lifecycle/record")
def week7_factor_lifecycle_record(request: Week7FactorLifecycleRecordRequest) -> dict[str, object]:
    return _service.record_factor_lifecycle(
        month=request.month,
        strategy=request.strategy,
        top_features=[item.model_dump() for item in request.top_features],
        psr=request.psr,
        ic_mean=request.ic_mean,
        note=request.note,
        source_trace_id=request.source_trace_id,
    )


@app.get("/week7/factor-lifecycle/status")
def week7_factor_lifecycle_status(strategy: str = Query(default="")) -> dict[str, object]:
    return _service.factor_lifecycle_status(strategy=strategy)


@app.get("/week7/factor-lifecycle/history")
def week7_factor_lifecycle_history(
    strategy: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict[str, object]:
    return _service.factor_lifecycle_history(strategy=strategy, limit=limit)


@app.get("/week7/factor-lifecycle/graveyard")
def week7_factor_lifecycle_graveyard(
    strategy: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict[str, object]:
    return _service.factor_graveyard(strategy=strategy, limit=limit)


@app.post("/week7/factor-lifecycle/reset")
def week7_factor_lifecycle_reset(request: Week7FactorLifecycleResetRequest) -> dict[str, object]:
    return _service.reset_factor_lifecycle(
        strategy=request.strategy,
        source_trace_id=request.source_trace_id,
    )


@app.post("/week7/sim-broker/run")
def week7_sim_broker_run(request: Week7SimBrokerRunRequest) -> dict[str, object]:
    return _service.run_week7_sim_broker_weekly(
        days=request.days,
        export_enabled=request.export_enabled,
        notify_enabled=request.notify_enabled,
        source_trace_id=request.source_trace_id,
    )


@app.get("/week7/sim-broker/latest")
def week7_sim_broker_latest() -> dict[str, object]:
    report = _service.latest_week7_sim_broker_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@app.get("/week7/sim-broker/history")
def week7_sim_broker_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return _service.week7_sim_broker_history(limit=limit)


@app.get("/dashboard/ops/state")
def dashboard_ops_state() -> dict[str, object]:
    return _dashboard_ops_state()


@app.post("/dashboard/ops/toggle")
def dashboard_ops_toggle(request: DashboardOpsToggleRequest) -> dict[str, object]:
    global _dashboard_ops_enabled
    if _config.app.mode.strip().lower() != "simulation":
        return {
            "accepted": False,
            "code": "disabled",
            "message": "dashboard ops toggle only allowed in simulation mode",
            "state": _dashboard_ops_state(),
        }
    _dashboard_ops_enabled = request.enabled
    return {
        "accepted": True,
        "state": _dashboard_ops_state(),
    }


@app.post("/dashboard/command/quick")
def dashboard_quick_command(request: DashboardQuickCommandRequest) -> dict[str, object]:
    if not _dashboard_quick_enabled():
        return {
            "accepted": False,
            "code": "disabled",
            "message": "quick dashboard command only allowed in simulation mode",
        }
    envelope = _build_internal_command(
        action=request.action,
        payload=request.payload,
        command_id=request.command_id,
    )
    result = _service.execute_command(envelope)
    return {"command_id": envelope.command_id, "result": result}


@app.post("/dashboard/reconcile/quick")
def dashboard_quick_reconcile(request: DashboardQuickReconcileRequest) -> dict[str, object]:
    if not _dashboard_quick_enabled():
        return {
            "accepted": False,
            "code": "disabled",
            "message": "quick dashboard reconcile only allowed in simulation mode",
        }
    trace_id = request.source_trace_id.strip() or f"dash-reconcile-{int(time.time())}"
    snapshot = _service.update_broker_snapshot(
        positions=[item.model_dump() for item in request.positions],
        source_trace_id=trace_id,
    )
    if not request.run_reconcile:
        return {"trace_id": trace_id, "snapshot": snapshot}
    report = _service.run_reconciliation(trace_id=trace_id)
    return {"trace_id": trace_id, "snapshot": snapshot, "report": report}


@app.get("/audit/events")
def audit_events(
    limit: int = Query(default=200, ge=1, le=2000),
    event_type: str = Query(default=""),
    trace_id: str = Query(default=""),
) -> dict[str, object]:
    return _service.audit_events(
        limit=limit,
        event_type=event_type,
        trace_id=trace_id,
    )


@app.get("/audit/trace/{trace_id}")
def audit_trace(trace_id: str) -> dict[str, object]:
    return _service.trace_replay(trace_id=trace_id)
