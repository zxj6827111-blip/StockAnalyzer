"""CLI entrypoint for running analyzer tasks."""

# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

import typer

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import get_config
from stock_analyzer.evolution.llm_semantic import OpenAICompatibleSemanticJudge
from stock_analyzer.runtime.service import StockAnalyzerService

app = typer.Typer(help="StockAnalyzer CLI")

_DEFAULT_LLM_COMPARE_MODELS: tuple[tuple[str, str], ...] = (
    ("GLM-5", "ZhipuAI/GLM-5"),
    ("Kimi-K2.5", "moonshotai/Kimi-K2.5"),
    ("MiniMax-M2.5", "MiniMax/MiniMax-M2.5"),
    ("DeepSeek-V3.2", "deepseek-ai/DeepSeek-V3.2"),
)


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


@app.callback()
def main() -> None:
    """StockAnalyzer command group."""


@app.command("run")
def run_pipeline(
    symbols: str = typer.Option(..., help="Comma separated stock symbols, e.g. 600000,000001"),
    strategy: str = typer.Option("trend", help="Strategy profile: trend/monster/multi"),
    current_equity: float = typer.Option(1.0, help="Current equity ratio, 1.0 means baseline"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [value.strip() for value in symbols.split(",") if value.strip()]
    report = service.run_pipeline(
        symbols=symbol_list, strategy=strategy, current_equity=current_equity
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("news-score")
def news_score(
    symbol: str = typer.Option(..., help="Single symbol, e.g. 600000"),
    strategy: str = typer.Option("trend", help="Strategy profile"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.preview_news_component(symbol=symbol, strategy=strategy)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("news-score-batch")
def news_score_batch(
    symbols: str = typer.Option(..., help="Comma separated symbols, e.g. 600000,000001"),
    strategy: str = typer.Option("trend", help="Strategy profile"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()]
    payload = service.preview_news_components(symbols=symbol_list, strategy=strategy)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("news-score-watchlist")
def news_score_watchlist(
    strategy: str = typer.Option("trend", help="Strategy profile"),
    limit: int = typer.Option(20, min=1, max=200, help="Max symbols to preview"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.preview_news_watchlist(strategy=strategy, limit=limit)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("news-score-history")
def news_score_history(
    limit: int = typer.Option(50, min=1, max=500, help="Max records"),
    symbol: str = typer.Option("", help="Optional symbol filter"),
    strategy: str = typer.Option("", help="Optional strategy filter"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.news_score_history(
        limit=limit,
        symbol=symbol,
        strategy=strategy,
    )
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("news-score-cache-state")
def news_score_cache_state() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.news_score_cache_state()
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("news-score-cache-clear")
def news_score_cache_clear(
    symbol: str = typer.Option("", help="Optional symbol filter"),
    strategy: str = typer.Option("", help="Optional strategy filter"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.clear_news_score_cache(symbol=symbol, strategy=strategy)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("m7-live-news-sync")
def m7_live_news_sync(
    symbols: str = typer.Option("", help="Optional comma separated symbols"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    force_refresh: bool = typer.Option(False, help="Ignore cache and refetch"),
    enable_ai_review: bool = typer.Option(False, help="Enable AI-assisted review for this run"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()]
    now_dt = datetime.fromisoformat(now) if now else None
    payload = service.run_m7_live_news_sync(
        symbols=symbol_list if symbol_list else None,
        timestamp=now_dt,
        force_refresh=force_refresh,
        enable_ai_review=enable_ai_review,
    )
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("scheduler-run-due")
def scheduler_run_due(now: str = typer.Option("", help="Optional ISO datetime")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    results = service.run_due_jobs(now=now_dt)
    typer.echo(json.dumps({"results": results}, ensure_ascii=False, indent=2))


@app.command("tdx-sync-run")
def tdx_sync_run(
    now: str = typer.Option("", help="Optional ISO datetime"),
    force: bool = typer.Option(False, help="Force rebuild even when source seems unchanged"),
    notify_enabled: bool = typer.Option(False, help="Whether to send sync notification"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_tdx_offline_sync(
        timestamp=now_dt,
        notify_enabled=notify_enabled,
        force=force,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("tdx-sync-latest")
def tdx_sync_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_tdx_sync_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("tdx-sync-history")
def tdx_sync_history(
    limit: int = typer.Option(20, help="Max number of TDX sync reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.tdx_sync_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("warehouse-sync-run")
def warehouse_sync_run(
    now: str = typer.Option("", help="Optional ISO datetime"),
    force: bool = typer.Option(False, help="Force sync even when already initialized"),
    notify_enabled: bool = typer.Option(False, help="Whether to send sync notification"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    symbols: str = typer.Option("", help="Optional comma separated symbols"),
    retry_failed_only: bool = typer.Option(
        False,
        help="Retry only failed symbols from the latest or selected warehouse sync report",
    ),
    retry_report_trace_id: str = typer.Option(
        "",
        help="Optional source trace id for failed-only retry",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()]
    report = service.run_market_warehouse_sync(
        timestamp=now_dt,
        notify_enabled=notify_enabled,
        force=force,
        source_trace_id=source_trace_id,
        symbols=symbol_list if symbol_list else None,
        retry_failed_only=retry_failed_only,
        retry_report_trace_id=retry_report_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("warehouse-bootstrap-run")
def warehouse_bootstrap_run(
    now: str = typer.Option("", help="Optional ISO datetime"),
    notify_enabled: bool = typer.Option(False, help="Whether to send sync notification"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    max_symbols: int = typer.Option(0, help="Optional symbol cap for initial bootstrap"),
    daily_only: bool = typer.Option(True, help="Skip intraday sync during initial bootstrap"),
) -> None:
    config = get_config()
    if max_symbols > 0:
        config.market_warehouse.max_symbols = max_symbols
    if daily_only:
        config.market_warehouse.intraday_sync_enabled = False
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_market_warehouse_sync(
        timestamp=now_dt,
        notify_enabled=notify_enabled,
        force=False,
        source_trace_id=source_trace_id or "warehouse-bootstrap",
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("warehouse-sync-latest")
def warehouse_sync_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_market_warehouse_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("warehouse-sync-history")
def warehouse_sync_history(
    limit: int = typer.Option(20, help="Max number of warehouse sync reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.market_warehouse_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("warehouse-sync-status")
def warehouse_sync_status() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.market_warehouse_runtime_status()
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("warehouse-background-status")
def warehouse_background_status() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.market_warehouse_background_data_status()
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("warehouse-sync-progress")
def warehouse_sync_progress() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_market_warehouse_progress()
    typer.echo(json.dumps({"progress": report}, ensure_ascii=False, indent=2))


@app.command("idle-run")
def idle_run(
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_idle_queue_cycle(now=now_dt, source_trace_id=source_trace_id)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("idle-latest")
def idle_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_idle_queue_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("idle-history")
def idle_history(limit: int = typer.Option(20, help="Max number of idle dispatch reports")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.idle_queue_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("idle-state")
def idle_state() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.idle_queue_state()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("idle-ack")
def idle_ack(
    task_id: str = typer.Option("", help="Task id to ack, e.g. WD-P0-01"),
    clear_all: bool = typer.Option(False, help="Ack all currently blocked tasks"),
    now: str = typer.Option("", help="Optional ISO datetime"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.idle_queue_ack_blocked(task_id=task_id, clear_all=clear_all, now=now_dt)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-run")
def evolution_run(
    symbols: str = typer.Option(
        "",
        help="Optional comma separated symbols, empty means runtime watchlist",
    ),
    dry_run: bool = typer.Option(True, help="Whether to run in dry-run mode"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else None
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_evolution_offhours(
        symbols=symbol_list,
        timestamp=now_dt,
        dry_run=dry_run,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-drill")
def evolution_drill(now: str = typer.Option("", help="Optional ISO datetime")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_evolution_drill(timestamp=now_dt)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-latest")
def evolution_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_evolution_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("evolution-history")
def evolution_history(
    limit: int = typer.Option(20, help="Max number of evolution reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.evolution_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-preflight")
def evolution_preflight(
    fail_on_not_ready: bool = typer.Option(
        False,
        help="Return exit code 2 when preflight is not ready",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.evolution_preflight()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_not_ready and not bool(report.get("ready", False)):
        raise typer.Exit(code=2)


@app.command("evolution-window-report")
def evolution_window_report(
    days: int = typer.Option(10, help="Validation window in days"),
    min_runs: int = typer.Option(5, help="Minimum required runs in window"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    fail_on_fail: bool = typer.Option(
        False,
        help="Return exit code 2 when overall result is fail",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.evolution_window_report(days=days, min_runs=min_runs, now=now_dt)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_fail and str(report.get("overall", "")) == "fail":
        raise typer.Exit(code=2)


@app.command("evolution-m3-maintain")
def evolution_m3_maintain(now: str = typer.Option("", help="Optional ISO datetime")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_evolution_m3_maintenance(timestamp=now_dt)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-m3-search")
def evolution_m3_search(
    vector: str = typer.Option(
        ...,
        help='JSON float array with dim=5, e.g. "[10.0,10.2,9.9,10.1,14.1]"',
    ),
    top_k: int = typer.Option(5, help="Nearest-neighbor count"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    query_vector = _parse_float_vector(vector)
    report = service.run_evolution_m3_search(vector=query_vector, top_k=top_k)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-m8-suggest")
def evolution_m8_suggest(
    symbols: str = typer.Option(
        "",
        help="Optional comma separated symbols, empty means runtime watchlist",
    ),
    top_k: int | None = typer.Option(None, help="Nearest-neighbor count override"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else None
    report = service.run_evolution_m8_suggest(
        symbols=symbol_list,
        top_k=top_k,
        timestamp=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-llm-compare")
def evolution_llm_compare(
    symbol: str = typer.Option(
        "600000.SH",
        help="Candidate symbol for same-task comparison",
    ),
    headline: str = typer.Option(
        "公司发布积极经营公告，资金面与情绪改善，观察是否具备持续性。",
        help="Shared headline/task text sent to all models",
    ),
    open_px: float = typer.Option(10.0, "--open", help="Candidate open price"),
    high_px: float = typer.Option(10.3, "--high", help="Candidate high price"),
    low_px: float = typer.Option(9.8, "--low", help="Candidate low price"),
    close_px: float = typer.Option(10.1, "--close", help="Candidate close price"),
    volume: float = typer.Option(2_000_000.0, help="Candidate volume"),
) -> None:
    profiles = _load_llm_compare_profiles()
    if not profiles:
        raise typer.BadParameter(
            "No model profile is configured. "
            "Please set SA_LLM_COMPARE_{N}_MODEL and API key variables."
        )

    candidate = {
        "symbol": symbol.strip() or "UNKNOWN",
        "headline": headline.strip(),
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": volume,
        "pcv_score": 0.62,
        "deflated_sharpe": 0.16,
        "fdr_p_value": 0.08,
    }
    results: list[dict[str, object]] = []
    for profile in profiles:
        judge = OpenAICompatibleSemanticJudge(
            api_key=str(profile["api_key"]),
            model=str(profile["model"]),
            base_url=str(profile["base_url"]),
            timeout_sec=_as_int(profile["timeout_sec"]),
            temperature=_as_float(profile["temperature"]),
            max_tokens=_as_int(profile["max_tokens"]),
        )
        started = time.perf_counter()
        decision = judge.judge(candidate=candidate)
        latency_ms = int((time.perf_counter() - started) * 1000)
        quality_score = _score_llm_compare_result(
            error=decision.error,
            verdict=decision.verdict,
            confidence=decision.confidence,
            reason=decision.reason,
            latency_ms=latency_ms,
        )
        results.append(
            {
                "name": profile["name"],
                "model": profile["model"],
                "base_url": profile["base_url"],
                "latency_ms": latency_ms,
                "verdict": decision.verdict,
                "confidence": round(float(decision.confidence), 4),
                "reason": decision.reason,
                "error": decision.error,
                "quality_score": quality_score,
            }
        )

    ranked = sorted(
        results,
        key=lambda item: (
            -_as_float(item.get("quality_score", 0.0)),
            _as_float(item.get("latency_ms", 0.0)),
        ),
    )
    payload = {
        "task": candidate,
        "results": results,
        "ranking": [item.get("name", "") for item in ranked],
        "recommended": ranked[0] if ranked else None,
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("evolution-release-attempt")
def evolution_release_attempt(
    days: int = typer.Option(10, help="Validation window in days"),
    min_runs: int = typer.Option(5, help="Minimum required runs in window"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    fail_on_blocked: bool = typer.Option(
        False,
        help="Return exit code 2 when release gate is blocked",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.attempt_evolution_release(
        days=days,
        min_runs=min_runs,
        now=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_blocked and not bool(report.get("accepted", False)):
        raise typer.Exit(code=2)


@app.command("evolution-release-latest")
def evolution_release_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_evolution_release_gate()
    typer.echo(json.dumps({"decision": report}, ensure_ascii=False, indent=2))


@app.command("evolution-release-history")
def evolution_release_history(
    limit: int = typer.Option(20, help="Max number of release gate decisions"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.evolution_release_gate_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-release-approve")
def evolution_release_approve(
    approver: str = typer.Option(..., help="Approval reviewer id"),
    approved: bool = typer.Option(True, help="Whether approval is granted"),
    note: str = typer.Option("", help="Optional note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    fail_on_rejected: bool = typer.Option(
        False,
        help="Return exit code 2 when approval request is not accepted",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.record_evolution_release_approval(
        approver=approver,
        approved=approved,
        note=note,
        timestamp=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_rejected and not bool(report.get("accepted", False)):
        raise typer.Exit(code=2)


@app.command("evolution-release-approval-latest")
def evolution_release_approval_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_evolution_release_approval()
    typer.echo(json.dumps({"record": report}, ensure_ascii=False, indent=2))


@app.command("evolution-release-approval-history")
def evolution_release_approval_history(
    limit: int = typer.Option(20, help="Max number of release approvals"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.evolution_release_approval_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-release-ticket-issue")
def evolution_release_ticket_issue(
    operator: str = typer.Option(..., help="Release operator id"),
    note: str = typer.Option("", help="Optional execution note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    fail_on_blocked: bool = typer.Option(
        False,
        help="Return exit code 2 when ticket issuance is blocked",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.issue_evolution_release_ticket(
        operator=operator,
        note=note,
        timestamp=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_blocked and not bool(report.get("accepted", False)):
        raise typer.Exit(code=2)


@app.command("evolution-release-ticket-execute")
def evolution_release_ticket_execute(
    executor: str = typer.Option(..., help="Release execution owner id"),
    ticket_id: str = typer.Option("", help="Optional target ticket id, default latest"),
    note: str = typer.Option("", help="Optional close-out note"),
    confirm_window: bool = typer.Option(
        True,
        help="Execution window confirmation flag (must be true to close out)",
    ),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    fail_on_blocked: bool = typer.Option(
        False,
        help="Return exit code 2 when ticket execution close-out is blocked",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.execute_evolution_release_ticket(
        executor=executor,
        ticket_id=ticket_id,
        note=note,
        confirm_window=confirm_window,
        timestamp=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_blocked and not bool(report.get("accepted", False)):
        raise typer.Exit(code=2)


@app.command("evolution-release-ticket-confirm")
def evolution_release_ticket_confirm(
    confirmer: str = typer.Option(..., help="Manual confirmation reviewer id"),
    ticket_id: str = typer.Option("", help="Optional target ticket id, default latest"),
    note: str = typer.Option("", help="Optional confirmation note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    fail_on_blocked: bool = typer.Option(
        False,
        help="Return exit code 2 when ticket confirmation is blocked",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.confirm_evolution_release_ticket(
        confirmer=confirmer,
        ticket_id=ticket_id,
        note=note,
        timestamp=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_blocked and not bool(report.get("accepted", False)):
        raise typer.Exit(code=2)


@app.command("evolution-release-ticket-rollback")
def evolution_release_ticket_rollback(
    rollback_by: str = typer.Option(..., help="Rollback reviewer/operator id"),
    ticket_id: str = typer.Option("", help="Optional target ticket id, default latest"),
    note: str = typer.Option("", help="Optional rollback note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
    fail_on_blocked: bool = typer.Option(
        False,
        help="Return exit code 2 when ticket rollback is blocked",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.rollback_evolution_release_ticket(
        rollback_by=rollback_by,
        ticket_id=ticket_id,
        note=note,
        timestamp=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_blocked and not bool(report.get("accepted", False)):
        raise typer.Exit(code=2)


@app.command("evolution-release-confirmation-watchdog")
def evolution_release_confirmation_watchdog(
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.run_evolution_release_confirmation_watchdog(
        now=now_dt,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-release-ticket-latest")
def evolution_release_ticket_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_evolution_release_ticket()
    typer.echo(json.dumps({"ticket": report}, ensure_ascii=False, indent=2))


@app.command("evolution-release-ticket-history")
def evolution_release_ticket_history(
    limit: int = typer.Option(20, help="Max number of release tickets"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.evolution_release_ticket_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("evolution-release-ticket-timeline")
def evolution_release_ticket_timeline(
    ticket_id: str = typer.Option("", help="Optional exact ticket id filter"),
    status: str = typer.Option("", help="Optional latest status filter"),
    limit: int = typer.Option(200, help="Max timeline snapshots"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.evolution_release_ticket_timeline(
        ticket_id=ticket_id,
        status=status,
        limit=limit,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("train-models")
def train_models(
    symbol: str = typer.Option("", help="Stock symbol, e.g. 600000"),
    lookback_days: int = typer.Option(600, help="Historical bars used for training"),
    artifact_path: str = typer.Option("", help="Optional artifact output path"),
    full_market: bool = typer.Option(
        False,
        "--full-market",
        help="Train with full A-share universe bootstrap dataset",
    ),
    max_symbols: int = typer.Option(
        0,
        help="Optional cap for full-market symbols; 0 means use config/default",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.train_models(
        symbol=symbol,
        lookback_days=lookback_days,
        artifact_path=artifact_path or None,
        full_market=full_market,
        max_symbols=max_symbols if max_symbols > 0 else None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("train-learning-manifest")
def train_learning_manifest(
    dataset_manifest_id: str = typer.Option(
        "",
        help="Optional dataset manifest id; empty uses latest manifest",
    ),
    artifact_path: str = typer.Option("", help="Optional artifact output path"),
    load_predictor: bool = typer.Option(
        False,
        help="Reload runtime predictor from the trained manifest artifact",
    ),
    register_model: bool = typer.Option(
        False,
        help="Register the trained manifest artifact into model registry governance",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.train_learning_manifest(
        dataset_manifest_id=dataset_manifest_id,
        artifact_path=artifact_path or None,
        load_predictor=load_predictor,
        register_model=register_model,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("train-learning-manifest-shadow-validate")
def train_learning_manifest_shadow_validate(
    dataset_manifest_id: str = typer.Option(
        "",
        help="Optional dataset manifest id; empty uses latest manifest",
    ),
    artifact_path: str = typer.Option("", help="Optional artifact output path"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option(
        "",
        help="Optional comma separated split names; empty defaults to test",
    ),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    include_rows: bool = typer.Option(False, help="Include full rows in nested report payloads"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
    max_samples: int = typer.Option(0, help="Optional max shadow online samples; 0 uses all"),
    min_samples: int = typer.Option(5, min=1, help="Minimum samples for shadow online v2"),
    learning_rate: float = typer.Option(0.1, help="Shadow online v2 learning rate"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    load_predictor: bool = typer.Option(
        False,
        help="Reload runtime predictor from the trained manifest artifact",
    ),
    mark_shadow_validated: bool = typer.Option(
        False,
        help="Advance the new registry entry to shadow_validated if the full bundle succeeds",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.run_learning_manifest_shadow_validation(
        dataset_manifest_id=dataset_manifest_id,
        artifact_path=artifact_path or None,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        include_rows=include_rows,
        preview_limit=preview_limit,
        max_samples=max_samples if max_samples > 0 else None,
        min_samples=min_samples,
        learning_rate=learning_rate,
        signal_threshold=signal_threshold,
        load_predictor=load_predictor,
        mark_shadow_validated=mark_shadow_validated,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("train-learning-manifest-shadow-promote")
def train_learning_manifest_shadow_promote(
    dataset_manifest_id: str = typer.Option(
        "",
        help="Optional dataset manifest id; empty uses latest manifest",
    ),
    artifact_path: str = typer.Option("", help="Optional artifact output path"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option(
        "",
        help="Optional comma separated split names; empty defaults to test",
    ),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    include_rows: bool = typer.Option(False, help="Include full rows in nested report payloads"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
    max_samples: int = typer.Option(0, help="Optional max shadow online samples; 0 uses all"),
    min_samples: int = typer.Option(5, min=1, help="Minimum samples for shadow online v2"),
    learning_rate: float = typer.Option(0.1, help="Shadow online v2 learning rate"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    load_predictor: bool = typer.Option(
        False,
        help="Reload runtime predictor from the trained manifest artifact",
    ),
    mark_shadow_validated: bool = typer.Option(
        True,
        help="Advance the new registry entry to shadow_validated before gate evaluation",
    ),
    min_shadow_v2_minus_champion_return: float = typer.Option(
        -0.02,
        help="Minimum allowed shadow_v2 minus champion cumulative return",
    ),
    max_shadow_v2_brier_delta: float = typer.Option(
        0.05,
        help="Warning threshold for shadow_v2 brier delta",
    ),
    max_shadow_v2_logloss_delta: float = typer.Option(
        0.10,
        help="Warning threshold for shadow_v2 logloss delta",
    ),
    max_signal_divergence_ratio: float = typer.Option(
        -1.0,
        help="Optional hard limit for shadow_v2 signal divergence ratio; negative uses config",
    ),
    approve_if_passed: bool = typer.Option(
        False,
        help="Advance lifecycle to approved when the promotion gate passes",
    ),
    block_if_failed: bool = typer.Option(
        False,
        help="Advance lifecycle to blocked when the promotion gate fails",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.run_learning_manifest_shadow_promotion_gate(
        dataset_manifest_id=dataset_manifest_id,
        artifact_path=artifact_path or None,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        include_rows=include_rows,
        preview_limit=preview_limit,
        max_samples=max_samples if max_samples > 0 else None,
        min_samples=min_samples,
        learning_rate=learning_rate,
        signal_threshold=signal_threshold,
        load_predictor=load_predictor,
        mark_shadow_validated=mark_shadow_validated,
        min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=(
            max_signal_divergence_ratio if max_signal_divergence_ratio >= 0.0 else None
        ),
        approve_if_passed=approve_if_passed,
        block_if_failed=block_if_failed,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-create")
def learning_model_proposal_create(
    model_id: str = typer.Option(..., help="Registered challenger/shadow model id"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    max_samples: int = typer.Option(0, help="Optional max shadow online samples; 0 uses all"),
    min_samples: int = typer.Option(5, min=1, help="Minimum samples required by the gate"),
    learning_rate: float = typer.Option(0.1, help="Shadow online v2 learning rate"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
    min_shadow_v2_minus_champion_return: float = typer.Option(
        -0.02,
        help="Minimum allowed shadow_v2 minus champion cumulative return",
    ),
    max_shadow_v2_brier_delta: float = typer.Option(
        0.05,
        help="Warning threshold for shadow_v2 brier delta",
    ),
    max_shadow_v2_logloss_delta: float = typer.Option(
        0.10,
        help="Warning threshold for shadow_v2 logloss delta",
    ),
    max_signal_divergence_ratio: float = typer.Option(
        -1.0,
        help="Optional hard limit for shadow_v2 signal divergence ratio; negative uses config",
    ),
    approve_if_passed: bool = typer.Option(
        False,
        help="Advance lifecycle to approved when the promotion gate passes",
    ),
    block_if_failed: bool = typer.Option(
        False,
        help="Advance lifecycle to blocked when the promotion gate fails",
    ),
    allow_warn_status: bool = typer.Option(
        True,
        help="Allow warn status proposals to be materialized",
    ),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.create_learning_model_proposal(
        model_id=model_id,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        max_samples=max_samples if max_samples > 0 else None,
        min_samples=min_samples,
        learning_rate=learning_rate,
        signal_threshold=signal_threshold,
        preview_limit=preview_limit,
        min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=(
            max_signal_divergence_ratio if max_signal_divergence_ratio >= 0.0 else None
        ),
        approve_if_passed=approve_if_passed,
        block_if_failed=block_if_failed,
        allow_warn_status=allow_warn_status,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("train-learning-manifest-shadow-proposal")
def train_learning_manifest_shadow_proposal(
    dataset_manifest_id: str = typer.Option(
        "",
        help="Optional dataset manifest id; empty uses latest manifest",
    ),
    artifact_path: str = typer.Option("", help="Optional artifact output path"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option(
        "",
        help="Optional comma separated split names; empty defaults to test",
    ),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    include_rows: bool = typer.Option(False, help="Include full rows in nested report payloads"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
    max_samples: int = typer.Option(0, help="Optional max shadow online samples; 0 uses all"),
    min_samples: int = typer.Option(5, min=1, help="Minimum samples for shadow online v2"),
    learning_rate: float = typer.Option(0.1, help="Shadow online v2 learning rate"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    load_predictor: bool = typer.Option(
        False,
        help="Reload runtime predictor from the trained manifest artifact",
    ),
    mark_shadow_validated: bool = typer.Option(
        True,
        help="Advance the new registry entry to shadow_validated before proposal generation",
    ),
    min_shadow_v2_minus_champion_return: float = typer.Option(
        -0.02,
        help="Minimum allowed shadow_v2 minus champion cumulative return",
    ),
    max_shadow_v2_brier_delta: float = typer.Option(
        0.05,
        help="Warning threshold for shadow_v2 brier delta",
    ),
    max_shadow_v2_logloss_delta: float = typer.Option(
        0.10,
        help="Warning threshold for shadow_v2 logloss delta",
    ),
    max_signal_divergence_ratio: float = typer.Option(
        -1.0,
        help="Optional hard limit for shadow_v2 signal divergence ratio; negative uses config",
    ),
    approve_if_passed: bool = typer.Option(
        False,
        help="Advance lifecycle to approved when the promotion gate passes",
    ),
    block_if_failed: bool = typer.Option(
        False,
        help="Advance lifecycle to blocked when the promotion gate fails",
    ),
    allow_warn_status: bool = typer.Option(
        True,
        help="Allow warn status proposals to be materialized",
    ),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.run_learning_manifest_shadow_proposal(
        dataset_manifest_id=dataset_manifest_id,
        artifact_path=artifact_path or None,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        include_rows=include_rows,
        preview_limit=preview_limit,
        max_samples=max_samples if max_samples > 0 else None,
        min_samples=min_samples,
        learning_rate=learning_rate,
        signal_threshold=signal_threshold,
        load_predictor=load_predictor,
        mark_shadow_validated=mark_shadow_validated,
        min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=(
            max_signal_divergence_ratio if max_signal_divergence_ratio >= 0.0 else None
        ),
        approve_if_passed=approve_if_passed,
        block_if_failed=block_if_failed,
        allow_warn_status=allow_warn_status,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-latest")
def learning_model_proposal_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_learning_model_proposal()
    typer.echo(json.dumps({"proposal": report}, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-history")
def learning_model_proposal_history(
    limit: int = typer.Option(20, help="Max number of proposal snapshots"),
    proposal_id: str = typer.Option("", help="Optional exact proposal id filter"),
    status: str = typer.Option("", help="Optional status filter"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.learning_model_proposal_history(
        limit=limit,
        proposal_id=proposal_id,
        status=status,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-approve")
def learning_model_proposal_approve(
    approver: str = typer.Option(..., help="Approval reviewer id"),
    approved: bool = typer.Option(True, help="Whether approval is granted"),
    proposal_id: str = typer.Option("", help="Optional target proposal id, default latest"),
    note: str = typer.Option("", help="Optional note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.record_learning_model_proposal_approval(
        approver=approver,
        approved=approved,
        proposal_id=proposal_id,
        note=note,
        timestamp=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-revoke")
def learning_model_proposal_revoke(
    revoked_by: str = typer.Option(..., help="Revocation actor id"),
    proposal_id: str = typer.Option("", help="Optional target proposal id, default latest"),
    note: str = typer.Option("", help="Optional note"),
    revoke_model: bool = typer.Option(True, help="Whether to revoke the bound model record"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.revoke_learning_model_proposal(
        revoked_by=revoked_by,
        proposal_id=proposal_id,
        note=note,
        revoke_model=revoke_model,
        timestamp=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-approval-latest")
def learning_model_proposal_approval_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_learning_model_approval()
    typer.echo(json.dumps({"record": report}, ensure_ascii=False, indent=2))


@app.command("learning-model-proposal-approval-history")
def learning_model_proposal_approval_history(
    limit: int = typer.Option(20, help="Max number of approval records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.learning_model_approval_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-issue")
def learning_model_release_ticket_issue(
    operator: str = typer.Option(..., help="Release operator id"),
    proposal_id: str = typer.Option("", help="Optional target proposal id, default latest"),
    note: str = typer.Option("", help="Optional execution note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.issue_learning_model_release_ticket(
        operator=operator,
        proposal_id=proposal_id,
        note=note,
        timestamp=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-execute")
def learning_model_release_ticket_execute(
    executor: str = typer.Option(..., help="Release execution owner id"),
    ticket_id: str = typer.Option("", help="Optional target ticket id, default latest"),
    note: str = typer.Option("", help="Optional close-out note"),
    confirm_window: bool = typer.Option(True, help="Whether manual release window is confirmed"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.execute_learning_model_release_ticket(
        executor=executor,
        ticket_id=ticket_id,
        note=note,
        confirm_window=confirm_window,
        timestamp=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-confirm")
def learning_model_release_ticket_confirm(
    confirmer: str = typer.Option(..., help="Manual confirmation reviewer id"),
    ticket_id: str = typer.Option("", help="Optional target ticket id, default latest"),
    note: str = typer.Option("", help="Optional confirmation note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.confirm_learning_model_release_ticket(
        confirmer=confirmer,
        ticket_id=ticket_id,
        note=note,
        timestamp=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-rollback")
def learning_model_release_ticket_rollback(
    rollback_by: str = typer.Option(..., help="Rollback reviewer/operator id"),
    ticket_id: str = typer.Option("", help="Optional target ticket id, default latest"),
    note: str = typer.Option("", help="Optional rollback note"),
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.rollback_learning_model_release_ticket(
        rollback_by=rollback_by,
        ticket_id=ticket_id,
        note=note,
        timestamp=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-confirmation-watchdog")
def learning_model_release_confirmation_watchdog(
    now: str = typer.Option("", help="Optional ISO datetime"),
    source_trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.run_learning_model_release_confirmation_watchdog(
        now=datetime.fromisoformat(now) if now else None,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-latest")
def learning_model_release_ticket_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_learning_model_release_ticket()
    typer.echo(json.dumps({"ticket": report}, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-history")
def learning_model_release_ticket_history(
    limit: int = typer.Option(20, help="Max number of release tickets"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.learning_model_release_ticket_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-release-ticket-timeline")
def learning_model_release_ticket_timeline(
    ticket_id: str = typer.Option("", help="Optional exact ticket id filter"),
    status: str = typer.Option("", help="Optional latest status filter"),
    limit: int = typer.Option(200, help="Max timeline snapshots"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.learning_model_release_ticket_timeline(
        ticket_id=ticket_id,
        status=status,
        limit=limit,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-model-governance-status")
def learning_model_governance_status(
    proposal_limit: int = typer.Option(20, min=1, max=200, help="Recent proposals to summarize"),
    ticket_limit: int = typer.Option(20, min=1, max=200, help="Recent tickets to summarize"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.learning_model_governance_status(
        proposal_limit=proposal_limit,
        ticket_limit=ticket_limit,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("model-registry-promotion-gate")
def model_registry_promotion_gate(
    model_id: str = typer.Option(..., help="Registered challenger/shadow model id"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    max_samples: int = typer.Option(0, help="Optional max shadow online samples; 0 uses all"),
    min_samples: int = typer.Option(5, min=1, help="Minimum samples required by the gate"),
    learning_rate: float = typer.Option(0.1, help="Shadow online v2 learning rate"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
    min_shadow_v2_minus_champion_return: float = typer.Option(
        -0.02,
        help="Minimum allowed shadow_v2 minus champion cumulative return",
    ),
    max_shadow_v2_brier_delta: float = typer.Option(
        0.05,
        help="Warning threshold for shadow_v2 brier delta",
    ),
    max_shadow_v2_logloss_delta: float = typer.Option(
        0.10,
        help="Warning threshold for shadow_v2 logloss delta",
    ),
    max_signal_divergence_ratio: float = typer.Option(
        -1.0,
        help="Optional hard limit for shadow_v2 signal divergence ratio; negative uses config",
    ),
    approve_if_passed: bool = typer.Option(
        False,
        help="Advance lifecycle to approved when the promotion gate passes",
    ),
    block_if_failed: bool = typer.Option(
        False,
        help="Advance lifecycle to blocked when the promotion gate fails",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.evaluate_learning_model_promotion_gate(
        model_id=model_id,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        max_samples=max_samples if max_samples > 0 else None,
        min_samples=min_samples,
        learning_rate=learning_rate,
        signal_threshold=signal_threshold,
        preview_limit=preview_limit,
        min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=(
            max_signal_divergence_ratio if max_signal_divergence_ratio >= 0.0 else None
        ),
        approve_if_passed=approve_if_passed,
        block_if_failed=block_if_failed,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("model-registry-list")
def model_registry_list(
    limit: int = typer.Option(20, min=1, max=200, help="Max number of model registry rows"),
    role: str = typer.Option("", help="Optional role filter"),
    lifecycle_state: str = typer.Option("", help="Optional lifecycle state filter"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.model_registry_entries(
        limit=limit,
        role=role,
        lifecycle_state=lifecycle_state,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("model-registry-entry")
def model_registry_entry(
    model_id: str = typer.Option(..., help="Model id to inspect"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.model_registry_entry(model_id=model_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("model-registry-register")
def model_registry_register(
    artifact_path: str = typer.Option(..., help="Artifact path to register"),
    role: str = typer.Option("challenger", help="Registry role"),
    lifecycle_state: str = typer.Option("trained", help="Lifecycle state"),
    source: str = typer.Option("manual_register_model_artifact", help="Registration source tag"),
    parent_model_id: str = typer.Option("", help="Optional parent champion model id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.register_model_artifact(
        artifact_path=artifact_path,
        role=role,
        lifecycle_state=lifecycle_state,
        source=source,
        parent_model_id=parent_model_id,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("model-registry-set-lifecycle")
def model_registry_set_lifecycle(
    model_id: str = typer.Option(..., help="Model id to update"),
    lifecycle_state: str = typer.Option(..., help="New lifecycle state"),
    blocked_reason: str = typer.Option("", help="Optional blocked reason"),
    timestamp: str = typer.Option("", help="Optional ISO timestamp"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.update_model_registry_lifecycle(
        model_id=model_id,
        lifecycle_state=lifecycle_state,
        blocked_reason=blocked_reason,
        timestamp=datetime.fromisoformat(timestamp) if timestamp else None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("model-registry-set-role")
def model_registry_set_role(
    model_id: str = typer.Option(..., help="Model id to update"),
    role: str = typer.Option(..., help="New governance role"),
    timestamp: str = typer.Option("", help="Optional ISO timestamp"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.update_model_registry_role(
        model_id=model_id,
        role=role,
        timestamp=datetime.fromisoformat(timestamp) if timestamp else None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("shadow-dataset-build")
def shadow_dataset_build(
    model_id: str = typer.Option(..., help="Shadow model id"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    include_rows: bool = typer.Option(False, help="Include full rows in payload"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.build_shadow_dataset(
        model_id=model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        include_rows=include_rows,
        preview_limit=preview_limit,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("champion-shadow-report-build")
def champion_shadow_report_build(
    model_id: str = typer.Option(..., help="Shadow model id"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    include_rows: bool = typer.Option(False, help="Include full rows in payload"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.build_champion_shadow_report(
        model_id=model_id,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        signal_threshold=signal_threshold,
        include_rows=include_rows,
        preview_limit=preview_limit,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("shadow-online-v2-report-build")
def shadow_online_v2_report_build(
    model_id: str = typer.Option(..., help="Shadow model id"),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    max_samples: int = typer.Option(0, help="Optional max samples; 0 uses config behavior"),
    min_samples: int = typer.Option(5, min=1, help="Minimum samples before update"),
    learning_rate: float = typer.Option(0.1, help="Online learner rate"),
    signal_threshold: float = typer.Option(0.5, help="Signal threshold"),
    include_rows: bool = typer.Option(False, help="Include full rows in payload"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.build_shadow_online_v2_report(
        model_id=model_id,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        max_samples=max_samples if max_samples > 0 else None,
        min_samples=min_samples,
        learning_rate=learning_rate,
        signal_threshold=signal_threshold,
        include_rows=include_rows,
        preview_limit=preview_limit,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("train-execution-risk")
def train_execution_risk(
    artifact_path: str = typer.Option("", help="Optional output artifact path"),
    maturity_statuses: str = typer.Option(
        "",
        help="Optional comma separated maturity statuses, e.g. reconciled,fully_matured",
    ),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all eligible rows"),
    min_samples_per_target: int = typer.Option(24, help="Minimum rows per target"),
    calibration_ratio: float = typer.Option(0.2, help="Calibration split ratio"),
    test_ratio: float = typer.Option(0.2, help="Test split ratio"),
    epochs: int = typer.Option(240, help="Training epochs"),
    learning_rate: float = typer.Option(0.05, help="Logistic learning rate"),
    l2: float = typer.Option(1e-3, help="L2 regularization"),
    seed: int = typer.Option(42, help="Training seed"),
    now: str = typer.Option("", help="Optional ISO datetime"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    maturity_list = [item.strip() for item in maturity_statuses.split(",") if item.strip()]
    now_dt = datetime.fromisoformat(now) if now else None
    result = service.train_execution_risk_model(
        artifact_path=artifact_path or None,
        maturity_statuses=maturity_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        min_samples_per_target=min_samples_per_target,
        calibration_ratio=calibration_ratio,
        test_ratio=test_ratio,
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
        seed=seed,
        now=now_dt,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("train-execution-risk-status")
def train_execution_risk_status() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    typer.echo(json.dumps(service.execution_risk_status(), ensure_ascii=False, indent=2))


@app.command("train-execution-risk-history")
def train_execution_risk_history(
    limit: int = typer.Option(20, min=1, max=200, help="Recent training records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    typer.echo(
        json.dumps(service.execution_risk_training_history(limit=limit), ensure_ascii=False, indent=2)
    )


@app.command("execution-aware-report-build")
def execution_aware_report_build(
    model_id: str = typer.Option(..., help="Shadow model id"),
    execution_risk_artifact_path: str = typer.Option(
        "",
        help="Optional execution risk artifact path; default uses latest trained sidecar",
    ),
    champion_model_id: str = typer.Option("", help="Optional explicit champion model id"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional max rows; 0 uses all rows"),
    include_rows: bool = typer.Option(False, help="Include full rows in payload"),
    preview_limit: int = typer.Option(5, min=1, max=100, help="Preview rows"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    split_list = [item.strip() for item in split_names.split(",") if item.strip()]
    result = service.build_execution_aware_report(
        model_id=model_id,
        execution_risk_artifact_path=execution_risk_artifact_path,
        champion_model_id=champion_model_id,
        split_names=split_list or None,
        max_rows=max_rows if max_rows > 0 else None,
        include_rows=include_rows,
        preview_limit=preview_limit,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("training-bootstrap-status")
def training_bootstrap_status() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    typer.echo(json.dumps(service.training_bootstrap_status(), ensure_ascii=False, indent=2))


@app.command("walk-forward")
def walk_forward(
    symbol: str = typer.Option(..., help="Stock symbol, e.g. 600000"),
    lookback_days: int = typer.Option(800, help="Historical bars used for walk-forward"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.run_walk_forward(symbol=symbol, lookback_days=lookback_days)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("baseline-report")
def baseline_report(
    symbol: str = typer.Option(..., help="Stock symbol, e.g. 600000"),
    lookback_days: int = typer.Option(800, help="Historical bars used for baseline walk-forward"),
    output_path: str = typer.Option("", help="Optional baseline report output path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_baseline_report(
        symbol=symbol,
        lookback_days=lookback_days,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-checkpoint")
def phase_checkpoint(
    phase: str = typer.Option(..., help="Phase name: A/B/C"),
    baseline_report_path: str = typer.Option("", help="Optional baseline report path"),
    output_path: str = typer.Option("", help="Optional checkpoint output path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_phase_checkpoint(
        phase=phase,
        baseline_report_path=baseline_report_path or None,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("v13-acceptance")
def v13_acceptance(
    baseline_report_path: str = typer.Option("", help="Optional baseline report path"),
    output_path: str = typer.Option("", help="Optional v1.3 acceptance report output path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_v13_acceptance_report(
        baseline_report_path=baseline_report_path or None,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("acceptance-bundle")
def acceptance_bundle(
    symbol: str = typer.Option(..., help="Stock symbol, e.g. 600000"),
    lookback_days: int = typer.Option(800, help="Historical bars used for baseline walk-forward"),
    baseline_output_path: str = typer.Option("", help="Optional baseline report output path"),
    v13_output_path: str = typer.Option("", help="Optional v1.3 acceptance report output path"),
    run_week5_scan: bool = typer.Option(
        False, help="Run a fresh Week5 scan before building v1.3 acceptance"
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_v13_acceptance_bundle(
        symbol=symbol,
        lookback_days=lookback_days,
        baseline_output_path=baseline_output_path or None,
        v13_output_path=v13_output_path or None,
        run_week5_scan=run_week5_scan,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("acceptance-release-gate")
def acceptance_release_gate(
    v13_report_path: str = typer.Option("", help="Optional v1.3 acceptance report path"),
    output_path: str = typer.Option("", help="Optional release gate report output path"),
    closed_loop_smoke_passed: bool = typer.Option(
        False,
        help="Whether closed-loop smoke tests already passed in the current gate run",
    ),
    closed_loop_smoke_detail: str = typer.Option(
        "",
        help="Optional detail for the closed-loop smoke evidence",
    ),
    fail_on_blocked: bool = typer.Option(
        False,
        help="Return exit code 2 when the acceptance release gate is blocked",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_acceptance_release_gate_report(
        v13_report_path=v13_report_path or None,
        output_path=output_path or None,
        closed_loop_smoke_passed=closed_loop_smoke_passed,
        closed_loop_smoke_detail=closed_loop_smoke_detail,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if fail_on_blocked and str(result.get("status", "")) != "pass":
        raise typer.Exit(code=2)


@app.command("phase-d-status")
def phase_d_status(
    output_path: str = typer.Option("", help="Optional Phase D status report output path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_phase_d_status_report(output_path=output_path or None)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d6-registry")
def phase_d6_registry(
    output_path: str = typer.Option("", help="Optional Phase D6 registry output path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.generate_phase_d6_registry_report(output_path=output_path or None)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-alphalens")
def phase_d_alphalens(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    factor_columns: str = typer.Option("", help="Optional comma separated factor columns"),
    horizons: str = typer.Option("1,5,10", help="Comma separated horizons"),
    quantiles: int = typer.Option(5, help="Quantile bucket count"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_alphalens_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        factor_columns=_parse_csv_list(factor_columns) or None,
        horizons=_parse_int_csv_list(horizons) or (1, 5, 10),
        quantiles=quantiles,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-shap")
def phase_d_shap(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    prediction_column: str = typer.Option("p_meta", help="Prediction column"),
    baseline_importance: str = typer.Option("{}", help='JSON object, e.g. {"risk":0.4}'),
    drift_threshold: float = typer.Option(0.25, help="Importance drift threshold"),
    top_k: int = typer.Option(5, help="Top feature count"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_shap_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        prediction_column=prediction_column,
        baseline_importance=_parse_payload(baseline_importance),
        drift_threshold=drift_threshold,
        top_k=top_k,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-catboost-shadow")
def phase_d_catboost_shadow(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    feature_columns: str = typer.Option("", help="Optional comma separated feature columns"),
    label_column: str = typer.Option("label", help="Label column"),
    baseline_probability_column: str = typer.Option("p_meta", help="Baseline probability column"),
    test_ratio: float = typer.Option(0.3, help="Holdout ratio"),
    random_seed: int = typer.Option(2026, help="Random seed"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_catboost_shadow_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        feature_columns=_parse_csv_list(feature_columns) or None,
        label_column=label_column,
        baseline_probability_column=baseline_probability_column,
        test_ratio=test_ratio,
        random_seed=random_seed,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-finbert")
def phase_d_finbert(
    records: str = typer.Option("[]", help='JSON list with headline/source fields'),
    model_path: str = typer.Option("", help="Optional local FinBERT model path"),
    include_neutral: bool = typer.Option(True, help="Keep neutral items"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_finbert_report(
        records=_parse_payload_list(records),
        model_path=model_path,
        include_neutral=include_neutral,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-qlib-bridge")
def phase_d_qlib_bridge(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    feature_columns: str = typer.Option("", help="Optional comma separated feature columns"),
    label_column: str = typer.Option("label", help="Label column"),
    train_ratio: float = typer.Option(0.6, help="Train ratio"),
    valid_ratio: float = typer.Option(0.2, help="Validation ratio"),
    output_dir: str = typer.Option("", help="Optional export bundle directory"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_qlib_bridge_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        feature_columns=_parse_csv_list(feature_columns) or None,
        label_column=label_column,
        train_ratio=train_ratio,
        valid_ratio=valid_ratio,
        output_dir=output_dir or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-tabular-deep")
def phase_d_tabular_deep(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    feature_columns: str = typer.Option("", help="Optional comma separated feature columns"),
    label_column: str = typer.Option("label", help="Label column"),
    baseline_probability_column: str = typer.Option("p_meta", help="Baseline probability column"),
    test_ratio: float = typer.Option(0.3, help="Holdout ratio"),
    random_seed: int = typer.Option(2026, help="Random seed"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_tabular_deep_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        feature_columns=_parse_csv_list(feature_columns) or None,
        label_column=label_column,
        baseline_probability_column=baseline_probability_column,
        test_ratio=test_ratio,
        random_seed=random_seed,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-tft")
def phase_d_tft(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    encoder_length: int = typer.Option(5, help="Encoder lookback length"),
    train_ratio: float = typer.Option(0.7, help="Train ratio"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_tft_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        horizon=horizon,
        encoder_length=encoder_length,
        train_ratio=train_ratio,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-finrl")
def phase_d_finrl(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    feature_columns: str = typer.Option("", help="Optional comma separated feature columns"),
    reward_column: str = typer.Option("realized_return", help="Reward column"),
    baseline_probability_column: str = typer.Option("p_meta", help="Baseline probability column"),
    test_ratio: float = typer.Option(0.3, help="Holdout ratio"),
    random_seed: int = typer.Option(2026, help="Random seed"),
    action_threshold: float = typer.Option(0.55, help="Action probability threshold"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_finrl_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        feature_columns=_parse_csv_list(feature_columns) or None,
        reward_column=reward_column,
        baseline_probability_column=baseline_probability_column,
        test_ratio=test_ratio,
        random_seed=random_seed,
        action_threshold=action_threshold,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("phase-d-heavy-ts")
def phase_d_heavy_ts(
    model_id: str = typer.Option("", help="Optional registered model id, default active champion"),
    split_names: str = typer.Option("", help="Optional comma separated split names"),
    max_rows: int = typer.Option(0, help="Optional row cap"),
    horizon: int = typer.Option(3, help="Forecast horizon"),
    lookback: int = typer.Option(8, help="Sequence lookback window"),
    test_ratio: float = typer.Option(0.3, help="Holdout ratio"),
    random_seed: int = typer.Option(2026, help="Random seed"),
    output_path: str = typer.Option("", help="Optional output report path"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.build_phase_d_heavy_ts_report(
        model_id=model_id,
        split_names=_parse_csv_list(split_names) or None,
        max_rows=max_rows if max_rows > 0 else None,
        horizon=horizon,
        lookback=lookback,
        test_ratio=test_ratio,
        random_seed=random_seed,
        output_path=output_path or None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("stress-run")
def stress_run() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.run_stress_tests()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("portfolio-positions")
def portfolio_positions() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = {"positions": service.portfolio_positions()}
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("portfolio-trades")
def portfolio_trades(limit: int = typer.Option(100, help="Max number of recent trades")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    typer.echo(
        json.dumps(
            {"trades": service.portfolio_trades(limit=limit)},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("recommendation-lifecycle")
def recommendation_lifecycle(
    status: str = typer.Option("", help="Optional status filter"),
    limit: int = typer.Option(120, min=1, max=1000, help="Max lifecycle records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.recommendation_lifecycle(status=status, limit=limit)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("recommendation-status-set")
def recommendation_status_set(
    symbol: str = typer.Option(..., help="Stock symbol, e.g. 600000"),
    status: str = typer.Option(..., help="recommended/bought/watching/dropped"),
    strategy: str = typer.Option("manual", help="Strategy tag"),
    note: str = typer.Option("", help="Optional status note"),
    command_id: str = typer.Option("", help="Optional command id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "status": status,
        "strategy": strategy,
    }
    note_text = note.strip()
    if note_text:
        payload["note"] = note_text
    response = _execute_signed_command(
        service=service,
        secret_key=config.command_channel.secret_key,
        action="SET_RECOMMENDATION_STATUS",
        payload=payload,
        command_id=command_id,
    )
    typer.echo(json.dumps(response, ensure_ascii=False, indent=2))


@app.command("portfolio-holding-alerts")
def portfolio_holding_alerts(
    severity: str = typer.Option("", help="Optional severity filter: warn/info"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.holding_alerts()
    severity_filter = severity.strip().lower()
    if severity_filter in {"warn", "info"}:
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            filtered = [
                item
                for item in raw_items
                if isinstance(item, dict)
                and str(item.get("severity", "")).strip().lower() == severity_filter
            ]
        else:
            filtered = []
        payload["items"] = filtered
        payload["records"] = len(filtered)
        payload["severity_filter"] = severity_filter
        payload["summary"] = {
            "warn": sum(1 for item in filtered if str(item.get("severity", "")) == "warn"),
            "info": sum(1 for item in filtered if str(item.get("severity", "")) == "info"),
        }
    else:
        payload["severity_filter"] = "all"
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("portfolio-execution-bias")
def portfolio_execution_bias(
    days: int = typer.Option(30, min=1, max=3650, help="Recent days window"),
    limit: int = typer.Option(200, min=1, max=1000, help="Max records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    payload = service.execution_bias_report(days=days, limit=limit)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("broker-snapshot")
def broker_snapshot(
    positions: str = typer.Option(
        "[]",
        help='JSON list, e.g. [{"symbol":"600000","target_position":0.2}]',
    ),
    source_trace_id: str = typer.Option("", help="Optional source trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    parsed_positions = _parse_positions(positions)
    result = service.update_broker_snapshot(
        positions=parsed_positions,
        source_trace_id=source_trace_id,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("reconcile-run")
def reconcile_run(now: str = typer.Option("", help="Optional ISO datetime")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    result = {"report": service.run_reconciliation(timestamp=now_dt)}
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("reconcile-latest")
def reconcile_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    latest = service.latest_reconcile_report()
    typer.echo(json.dumps({"report": latest}, ensure_ascii=False, indent=2))


@app.command("reconcile-weekly")
def reconcile_weekly(days: int = typer.Option(7, help="Recent days for weekly summary")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.reconcile_weekly_report(days=days)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("dashboard-portfolio")
def dashboard_portfolio(
    days: int = typer.Option(7, help="Recent days for dashboard window"),
    trade_limit: int = typer.Option(120, help="Max trades in dashboard panel"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.dashboard_portfolio(days=days, trade_limit=trade_limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("runtime-sla")
def runtime_sla(
    recent_runs: int = typer.Option(50, help="How many latest runs to inspect"),
    session_scope: str = typer.Option("all", help="Session scope: all or intraday"),
    job_scope: str = typer.Option(
        "all",
        help="Job scope: all, live_runtime, scheduled, or job name",
    ),
    target_ms: int = typer.Option(60000, help="SLA target in milliseconds"),
    alert_target_ms: int = typer.Option(30000, help="Stricter alert target in milliseconds"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.sla_report(
        recent_runs=recent_runs,
        session_scope=session_scope,
        job_scope=job_scope,
        target_ms=target_ms,
        alert_target_ms=alert_target_ms,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("runtime-history-archive-status")
def runtime_history_archive_status(
    limit: int = typer.Option(20, min=1, max=500, help="How many archive files to list"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.runtime_history_archive_status(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("runtime-history-archive-run")
def runtime_history_archive_run(
    force: bool = typer.Option(False, help="Rewrite today's archive even if it already exists"),
    now: str = typer.Option("", help="Optional ISO datetime"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    report = service.archive_runtime_history(now=now_dt, force=force)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-runtime-history-bootstrap")
def learning_runtime_history_bootstrap(
    archive_dir: str = typer.Option(
        "",
        help="Runtime history archive directory, empty uses configured default",
    ),
    symbols: str = typer.Option(
        "",
        help="Optional comma separated symbols for manifest generation",
    ),
    build_manifest: bool = typer.Option(True, help="Whether to build a trainable manifest"),
    calibration_ratio: float = typer.Option(
        -1.0,
        help="Optional calibration split ratio, negative means config default",
    ),
    test_ratio: float = typer.Option(
        -1.0,
        help="Optional test split ratio, negative means config default",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else None
    report = service.bootstrap_learning_from_runtime_history(
        archive_dir=archive_dir,
        symbols=symbol_list,
        build_manifest=build_manifest,
        calibration_ratio=None if calibration_ratio < 0.0 else calibration_ratio,
        test_ratio=None if test_ratio < 0.0 else test_ratio,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("learning-status")
def learning_status(
    manifest_limit: int = typer.Option(5, min=1, max=50, help="How many recent manifests to show"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.learning_protocol_status(manifest_limit=manifest_limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("acceptance-week4-run")
def acceptance_week4_run(
    sla_recent_runs: int = typer.Option(50, help="SLA recent runs window"),
    export_enabled: bool = typer.Option(True, help="Whether to export JSON/CSV artifacts"),
    notify_enabled: bool = typer.Option(True, help="Whether to send acceptance notification"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.run_week4_acceptance(
        sla_recent_runs=sla_recent_runs,
        export_enabled=export_enabled,
        notify_enabled=notify_enabled,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("acceptance-week4-latest")
def acceptance_week4_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_week4_acceptance_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("acceptance-week4-history")
def acceptance_week4_history(
    limit: int = typer.Option(20, help="Max number of acceptance reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.week4_acceptance_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("week5-scan-run")
def week5_scan_run(
    symbols: str = typer.Option(
        "",
        help="Optional comma separated symbols, empty means runtime watchlist",
    ),
    notify_enabled: bool = typer.Option(True, help="Whether to send week5 scan notification"),
    sync_watchlist: bool = typer.Option(
        False,
        help="Whether to sync week5 selection result into runtime watchlist",
    ),
    sync_reason: str = typer.Option("", help="Optional sync reason for audit trail"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else None
    report = service.run_week5_scan(
        symbols=symbol_list,
        notify_enabled=notify_enabled,
        sync_watchlist=sync_watchlist,
        sync_reason=sync_reason,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("week5-scan-latest")
def week5_scan_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_week5_scan_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("week5-scan-history")
def week5_scan_history(
    limit: int = typer.Option(20, help="Max number of week5 scan reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.week5_scan_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("week6-run")
def week6_run(
    symbols: str = typer.Option(
        "",
        help="Optional comma separated symbols, empty means runtime watchlist",
    ),
    notify_enabled: bool = typer.Option(True, help="Whether to send week6 summary notification"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else None
    report = service.run_week6_analysis(symbols=symbol_list, notify_enabled=notify_enabled)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("week6-latest")
def week6_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_week6_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("week6-history")
def week6_history(
    limit: int = typer.Option(20, help="Max number of week6 reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.week6_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("week6-global-set")
def week6_global_set(
    us_index_change_pct: float = typer.Option(0.0, help="Overnight US index change, e.g. 0.8"),
    a50_change_pct: float = typer.Option(0.0, help="A50 futures change"),
    usd_cnh_change_pct: float = typer.Option(0.0, help="USDCNH change"),
    commodity_change_pct: float = typer.Option(0.0, help="Commodity composite change"),
    a_share_correlation: float = typer.Option(0.6, help="Recent A-share vs A50 correlation"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.update_global_market_snapshot(
        snapshot={
            "us_index_change_pct": us_index_change_pct,
            "a50_change_pct": a50_change_pct,
            "usd_cnh_change_pct": usd_cnh_change_pct,
            "commodity_change_pct": commodity_change_pct,
            "a_share_correlation": a_share_correlation,
        },
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week6-global-get")
def week6_global_get() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.global_market_snapshot()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week6-global-history")
def week6_global_history(
    limit: int = typer.Option(50, help="Max number of global snapshot records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.global_market_history(limit=limit)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week6-regulatory-set")
def week6_regulatory_set(
    entries: str = typer.Option(
        "[]",
        help='JSON list, e.g. [{"symbol":"600000","tag":"inquiry","note":"exchange inquiry"}]',
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    parsed_entries = _parse_regulatory_entries(entries)
    result = service.set_regulatory_watchlist(entries=parsed_entries)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week6-regulatory-get")
def week6_regulatory_get() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.regulatory_watchlist()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-kill-record")
def week7_kill_record(
    month: str = typer.Option(..., help="Month in YYYY-MM, e.g. 2026-01"),
    strategy: str = typer.Option(..., help="Strategy name, e.g. trend"),
    strategy_return: float = typer.Option(..., help="Strategy monthly return, e.g. -0.03"),
    benchmark_return: float = typer.Option(..., help="Benchmark monthly return, e.g. 0.01"),
    note: str = typer.Option("", help="Optional note"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.record_strategy_performance(
        month=month,
        strategy=strategy,
        strategy_return=strategy_return,
        benchmark_return=benchmark_return,
        note=note,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-kill-history")
def week7_kill_history(
    strategy: str = typer.Option("", help="Optional strategy filter"),
    limit: int = typer.Option(60, help="Max records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.strategy_kill_switch_history(strategy=strategy, limit=limit)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-kill-status")
def week7_kill_status(strategy: str = typer.Option("", help="Optional strategy filter")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.strategy_kill_switch_status(strategy=strategy)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-kill-reset")
def week7_kill_reset(
    strategy: str = typer.Option("", help="Optional strategy filter, empty means all"),
    resume_new_buy: bool = typer.Option(
        False,
        help="Set runtime pause_new_buy=false if no active kill switch",
    ),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.reset_strategy_kill_switch(
        strategy=strategy,
        resume_new_buy=resume_new_buy,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-cloud-ping")
def week7_cloud_ping(
    source: str = typer.Option("manual", help="Heartbeat source, e.g. cloud_monitor"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.cloud_backup_ping(source=source)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-cloud-status")
def week7_cloud_status() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.cloud_backup_status()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-cloud-check")
def week7_cloud_check(now: str = typer.Option("", help="Optional ISO datetime")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    now_dt = datetime.fromisoformat(now) if now else None
    result = service.run_cloud_backup_check(now=now_dt)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-factor-record")
def week7_factor_record(
    month: str = typer.Option(..., help="Month in YYYY-MM, e.g. 2026-01"),
    strategy: str = typer.Option(..., help="Strategy name, e.g. trend"),
    psr: float = typer.Option(..., help="PSR score, e.g. 0.55"),
    ic_mean: float = typer.Option(0.0, help="Mean IC value"),
    top_features: str = typer.Option(
        '[{"name":"volume_ratio","importance":0.32},{"name":"atr14","importance":0.22}]',
        help='JSON list, e.g. [{"name":"f1","importance":0.3}]',
    ),
    note: str = typer.Option("", help="Optional note"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    parsed = _parse_factor_features(top_features)
    result = service.record_factor_lifecycle(
        month=month,
        strategy=strategy,
        top_features=parsed,
        psr=psr,
        ic_mean=ic_mean,
        note=note,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-factor-status")
def week7_factor_status(strategy: str = typer.Option("", help="Optional strategy filter")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.factor_lifecycle_status(strategy=strategy)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-factor-history")
def week7_factor_history(
    strategy: str = typer.Option("", help="Optional strategy filter"),
    limit: int = typer.Option(60, help="Max records"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.factor_lifecycle_history(strategy=strategy, limit=limit)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-factor-reset")
def week7_factor_reset(strategy: str = typer.Option("", help="Optional strategy filter")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.reset_factor_lifecycle(strategy=strategy)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-sim-broker-run")
def week7_sim_broker_run(
    days: int = typer.Option(7, help="Rolling window days"),
    export_enabled: bool = typer.Option(True, help="Whether to export weekly report artifacts"),
    notify_enabled: bool = typer.Option(True, help="Whether to send weekly report notification"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    result = service.run_week7_sim_broker_weekly(
        days=days,
        export_enabled=export_enabled,
        notify_enabled=notify_enabled,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("week7-sim-broker-latest")
def week7_sim_broker_latest() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.latest_week7_sim_broker_report()
    typer.echo(json.dumps({"report": report}, ensure_ascii=False, indent=2))


@app.command("week7-sim-broker-history")
def week7_sim_broker_history(
    limit: int = typer.Option(20, help="Max number of week7 sim/broker reports"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.week7_sim_broker_history(limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("audit-events")
def audit_events(
    limit: int = typer.Option(200, help="Max number of returned events"),
    event_type: str = typer.Option("", help="Optional event type filter"),
    trace_id: str = typer.Option("", help="Optional trace id filter"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.audit_events(limit=limit, event_type=event_type, trace_id=trace_id)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("audit-trace")
def audit_trace(trace_id: str = typer.Option(..., help="Trace id to replay")) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    report = service.trace_replay(trace_id=trace_id)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("dashboard-quick-command")
def dashboard_quick_command(
    action: str = typer.Option(..., help="Command action, e.g. PAUSE_NEW_BUY"),
    payload: str = typer.Option("{}", help='JSON payload string, e.g. {"symbol":"600000"}'),
    command_id: str = typer.Option("", help="Optional command id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    command_payload = _parse_payload(payload)
    response = _execute_signed_command(
        service=service,
        secret_key=config.command_channel.secret_key,
        action=action,
        payload=command_payload,
        command_id=command_id,
    )
    typer.echo(json.dumps(response, ensure_ascii=False, indent=2))


@app.command("dashboard-quick-reconcile")
def dashboard_quick_reconcile(
    positions: str = typer.Option(
        "[]",
        help='JSON list, e.g. [{"symbol":"600000","target_position":0.2}]',
    ),
    run_reconcile: bool = typer.Option(True, help="Whether to execute reconcile immediately"),
    trace_id: str = typer.Option("", help="Optional trace id"),
) -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    parsed_positions = _parse_positions(positions)
    source_trace_id = trace_id.strip() or f"dash-reconcile-{int(time.time())}"
    snapshot = service.update_broker_snapshot(
        positions=parsed_positions,
        source_trace_id=source_trace_id,
    )
    if not run_reconcile:
        typer.echo(
            json.dumps(
                {"trace_id": source_trace_id, "snapshot": snapshot},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    report = service.run_reconciliation(trace_id=source_trace_id)
    typer.echo(
        json.dumps(
            {"trace_id": source_trace_id, "snapshot": snapshot, "report": report},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("sign-command")
def sign_command(
    action: str = typer.Option(..., help="Command action, e.g. SET_EQUITY"),
    payload: str = typer.Option("{}", help='JSON payload string, e.g. {"current_equity":0.98}'),
    command_id: str = typer.Option("", help="Optional command id, auto-generated if empty"),
    timestamp: int = typer.Option(0, help="Unix timestamp, auto-generated if 0"),
) -> None:
    config = get_config()
    command_payload = _parse_payload(payload)
    command_ts = timestamp if timestamp > 0 else int(time.time())
    command_name = command_id or f"cmd-{command_ts}"
    signature = SignedCommandProcessor.build_signature(
        secret_key=config.command_channel.secret_key,
        command_id=command_name,
        timestamp=command_ts,
        action=action,
        payload=command_payload,
    )
    typer.echo(
        json.dumps(
            {
                "command_id": command_name,
                "timestamp": command_ts,
                "action": action,
                "payload": command_payload,
                "signature": signature,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _parse_payload(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def _parse_payload_list(raw: str) -> list[dict[str, object]]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("payload must be a JSON list")
    normalized: list[dict[str, object]] = []
    for item in parsed:
        if isinstance(item, dict):
            normalized.append({str(key): value for key, value in item.items()})
    return normalized


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_int_csv_list(raw: str) -> list[int]:
    values: list[int] = []
    for item in _parse_csv_list(raw):
        values.append(int(float(item)))
    return values


def _load_llm_compare_profiles() -> list[dict[str, object]]:
    base_url_default = os.getenv(
        "SA_LLM_COMPARE_BASE_URL", "https://api-inference.modelscope.cn/v1"
    ).strip()
    shared_api_key = os.getenv("SA_LLM_COMPARE_SHARED_API_KEY", "").strip()
    timeout_sec = _env_int("SA_LLM_COMPARE_TIMEOUT_SEC", default=12)
    max_tokens = _env_int("SA_LLM_COMPARE_MAX_TOKENS", default=120)
    temperature = _env_float("SA_LLM_COMPARE_TEMPERATURE", default=0.0)
    profiles: list[dict[str, object]] = []

    for idx, defaults in enumerate(_DEFAULT_LLM_COMPARE_MODELS, start=1):
        default_name, default_model = defaults
        name = os.getenv(f"SA_LLM_COMPARE_{idx}_NAME", default_name).strip() or default_name
        model = os.getenv(f"SA_LLM_COMPARE_{idx}_MODEL", default_model).strip() or default_model
        if not model:
            continue
        base_url = (
            os.getenv(f"SA_LLM_COMPARE_{idx}_BASE_URL", base_url_default).strip()
            or base_url_default
        )
        api_key = os.getenv(f"SA_LLM_COMPARE_{idx}_API_KEY", "").strip() or shared_api_key
        profiles.append(
            {
                "index": idx,
                "name": name,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "timeout_sec": timeout_sec,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
    return profiles


def _score_llm_compare_result(
    *,
    error: str,
    verdict: str,
    confidence: float,
    reason: str,
    latency_ms: int,
) -> float:
    score = 0.0
    if not error:
        score += 55.0
    normalized_verdict = verdict.strip().lower()
    if normalized_verdict in {"approve", "review", "reject"}:
        score += 15.0
    if 0.0 <= confidence <= 1.0:
        score += 15.0
        score += min(10.0, confidence * 10.0)
    cleaned_reason = reason.strip()
    if cleaned_reason:
        score += min(5.0, len(cleaned_reason) / 24.0)
    if latency_ms <= 6000:
        score += 5.0
    elif latency_ms >= 15000:
        score -= 5.0
    return round(max(0.0, min(100.0, score)), 2)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _execute_signed_command(
    service: StockAnalyzerService,
    secret_key: str,
    action: str,
    payload: dict[str, Any],
    command_id: str,
) -> dict[str, object]:
    command_ts = int(time.time())
    normalized = action.strip()
    action_code = normalized.lower().replace(" ", "_")
    command_name = command_id.strip() or f"dash-{action_code}-{command_ts}"
    signature = SignedCommandProcessor.build_signature(
        secret_key=secret_key,
        command_id=command_name,
        timestamp=command_ts,
        action=normalized,
        payload=payload,
    )
    envelope = CommandEnvelope(
        command_id=command_name,
        timestamp=command_ts,
        action=normalized,
        payload=payload,
        signature=signature,
    )
    result = service.execute_command(envelope)
    return {"command_id": command_name, "result": result}


def _parse_positions(raw: str) -> list[dict[str, object]]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("positions must be a JSON list")
    normalized: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        target_position = float(item.get("target_position", 0.0))
        if target_position < 0:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "target_position": target_position,
            }
        )
    return normalized


def _parse_regulatory_entries(raw: str) -> list[dict[str, object]]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("entries must be a JSON list")
    normalized: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "tag": str(item.get("tag", "")).strip(),
                "note": str(item.get("note", "")).strip(),
            }
        )
    return normalized


def _parse_factor_features(raw: str) -> list[dict[str, object]]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("top_features must be a JSON list")
    normalized: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        importance = float(item.get("importance", 0.0))
        normalized.append({"name": name, "importance": importance})
    return normalized


def _parse_float_vector(raw: str) -> list[float]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("vector must be a JSON list")
    normalized: list[float] = []
    for item in parsed:
        if not isinstance(item, (int, float)):
            raise ValueError("vector must contain only numbers")
        normalized.append(float(item))
    return normalized


if __name__ == "__main__":
    app()
