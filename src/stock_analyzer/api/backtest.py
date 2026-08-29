"""历史回溯选股 + 持有期走势分析 API 端点（PLAN Task 5）。

POST /backtest/asof-scan 耗时长（标的维度并行仍需逐票取数+特征工程+推理），
经 ``ops/background_tasks.py`` 异步执行，立即返回 202 + task_id，配
``GET /tasks/{task_id}`` 轮询；结果落盘持久化后另可通过
``GET /backtest/asof-scan/latest``/``history`` 查询，容器重启不丢。

GET /market/daily-bars 是新增的单标的日线序列只读端点（当前 api/ 下此前没有
任何 K 线/ohlc 端点）。
"""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from stock_analyzer.api.deps import get_config, get_service, get_verify_api_auth
from stock_analyzer.api.models import AsofBacktestRunRequest
from stock_analyzer.ops.background_tasks import (
    submit_background_task,
    submit_background_task_with_progress,
)

router = APIRouter()


def _parse_iso_date(raw: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_date:{field_name}:{raw}",
        ) from exc


def _resolve_date_range(request: AsofBacktestRunRequest) -> tuple[date, date]:
    """校验并解析 date / start_date+end_date 的互斥组合，日期区间跨度限流。"""
    single = request.date.strip()
    start_raw = request.start_date.strip()
    end_raw = request.end_date.strip()
    if single and (start_raw or end_raw):
        raise HTTPException(
            status_code=400,
            detail="conflicting_date_params: provide either 'date' or 'start_date'+'end_date'",
        )
    if single:
        parsed = _parse_iso_date(single, field_name="date")
        return parsed, parsed
    if not start_raw or not end_raw:
        raise HTTPException(
            status_code=400,
            detail="missing_date_params: provide 'date' or both 'start_date' and 'end_date'",
        )
    start_date = _parse_iso_date(start_raw, field_name="start_date")
    end_date = _parse_iso_date(end_raw, field_name="end_date")
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    max_range_days = get_config().asof_backtest.max_date_range_days
    if (end_date - start_date).days > max_range_days:
        raise HTTPException(
            status_code=400,
            detail=(
                f"date_range_too_large: requested {(end_date - start_date).days} days, "
                f"max is {max_range_days}"
            ),
        )
    return start_date, end_date


@router.post("/backtest/asof-scan", status_code=202)
def asof_scan_run(
    request: AsofBacktestRunRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    algorithm = request.algorithm.strip().lower()
    if algorithm not in {"", "week5_daily", "legacy_trend"}:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_algorithm:{algorithm}:expected week5_daily|legacy_trend",
        )
    start_date, end_date = _resolve_date_range(request)
    service = get_service()
    if algorithm == "week5_daily":
        config = get_config()
        if not bool(config.asof_backtest.week5_daily_enabled):
            raise HTTPException(
                status_code=409,
                detail="week5_backtest_disabled:feature switch is off",
            )
        # NAS 同时只允许一个 Week5 历史任务：提交时即占位，后台任务结束释放。
        if not service.try_acquire_week5_backtest():
            raise HTTPException(status_code=409, detail="week5_backtest_busy")
        explicit_symbols: list[str] = list(request.symbols or [])
        return submit_background_task_with_progress(
            background_tasks,
            name="asof_backtest_week5_scan",
            fn=lambda progress: service.run_asof_backtest(
                symbols=None,
                start_date=start_date,
                end_date=end_date,
                top_n=None,
                horizon_days=request.horizon_days,
                algorithm=algorithm,
                holding_top_n=request.holding_top_n,
                explicit_symbols=explicit_symbols,
                progress=progress,
                release_week5_lock=True,
            ),
        )
    return submit_background_task(
        background_tasks,
        name="asof_backtest_scan",
        fn=lambda: service.run_asof_backtest(
            symbols=request.symbols or None,
            start_date=start_date,
            end_date=end_date,
            top_n=request.top_n,
            horizon_days=request.horizon_days,
        ),
    )


@router.get("/backtest/asof-scan/latest")
def asof_scan_latest() -> dict[str, object]:
    report = get_service().latest_asof_backtest_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@router.get("/backtest/asof-scan/history")
def asof_scan_history(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return get_service().asof_backtest_history(limit=limit)


@router.get("/market/daily-bars")
def market_daily_bars(
    symbol: str,
    start: str = "",
    limit: int = Query(default=250, ge=1, le=2000),
) -> dict[str, object]:
    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="missing_symbol")
    start_date: date | None = None
    if start.strip():
        start_date = _parse_iso_date(start, field_name="start")
    return get_service().daily_bars_json(symbol=normalized_symbol, start=start_date, limit=limit)


__all__ = ["router"]
