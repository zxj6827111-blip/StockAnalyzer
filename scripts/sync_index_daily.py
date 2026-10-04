"""每日指数日线增量（000300.SH）：tushare → delta 库 + market.duckdb 双库 upsert。

背景（2026-10-04 发现）：``index_daily`` 没有任何常驻写入方——调度器
``market_warehouse_sync`` 从未起跑（runtime_state 零条目），``sync_market_duckdb.py``
只管 bars/分钟——两库同停更于 2026-08-14，生产快照市场相对族
（excess_ret/rs_ma/beta，PR#98-#101 刚接通）因指数陈旧退化。

两个消费库都要喂：

- delta 库（``warehouse_db_path``，生产运行时 overlay `fetch_index_daily`
  的首选来源，market_sync_service 的 index 富集写入目标）；
- market.duckdb（回测面板 ``_fetch_market_index`` 的来源）。

运行（NAS cron，工作日晚间、紧随 sync_market_duckdb --daily）：
  docker exec stock-analyzer-api python3 /app/scripts/sync_index_daily.py

语义：一次 tushare 拉取（两库最旧缺口并集窗口、5 日重叠），双库
``upsert_index_daily``（delete+insert，幂等）；``--since`` 只约束空库回填起点。
读取一律走 ``MarketWarehouse`` API（本文件不出现裸 SQL）。
"""

from __future__ import annotations

import argparse
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path

import pandas as pd

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.tushare_provider import (
    TushareProvider,
    _resolve_tushare_token,
)

DELTA_DB = "/app/artifacts/vendor_delta/market_delta.duckdb"
MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
INDEX_CODE = "000300.SH"
OVERLAP_DAYS = 5


def _warehouse(db_path: str) -> MarketWarehouse:
    return MarketWarehouse(
        db_path=db_path,
        package_root=str(Path(db_path).expanduser().parent / "package"),
        package_writes_enabled=False,
    )


def _last_index_date(warehouse: MarketWarehouse) -> date_type | None:
    frame = warehouse.fetch_index_daily(index_code=INDEX_CODE)
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.max().date()


def sync_index_daily(
    since: date_type,
    provider: object | None = None,
    *,
    market_db: str = MARKET_DB,
    delta_db: str = DELTA_DB,
) -> dict[str, object]:
    """增量同步 000300.SH 指数日线到两个消费库，返回状态摘要（可测注入点）。"""
    if provider is None:
        token = _resolve_tushare_token()
        if not token:
            print("[index] no tushare token; index_daily skipped", flush=True)
            return {"status": "skipped", "reason": "no_token"}
        provider = TushareProvider(
            token=token,
            retry_delay_sec=1.0,
            min_request_interval_sec=0.35,
            max_attempts=3,
            socket_timeout_sec=30.0,
            price_series_mode="raw",
        )

    last_dates: dict[str, date_type | None] = {}
    for db_path in (delta_db, market_db):
        warehouse = _warehouse(db_path)
        warehouse.ensure_schema()
        last_dates[db_path] = _last_index_date(warehouse)

    starts = [
        (last - timedelta(days=OVERLAP_DAYS)) if last is not None else since
        for last in last_dates.values()
    ]
    start = min(starts)
    end = date_type.today()
    if start > end:
        return {"status": "skipped", "reason": "checkpoint_current"}

    frame = provider.fetch_index_daily(
        index_code=INDEX_CODE, start_date=start, end_date=end
    )
    if frame is None or frame.empty:
        print(f"[index] tushare empty for {start}~{end}", flush=True)
        return {"status": "skipped", "reason": "empty_fetch"}

    upserted = 0
    for db_path in (delta_db, market_db):
        upserted += int(_warehouse(db_path).upsert_index_daily(frame=frame))
    print(
        f"[index] {INDEX_CODE} {start}~{end}: fetched={len(frame)} upserted={upserted}",
        flush=True,
    )
    return {"status": "ok", "fetched": len(frame), "upserted": upserted}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default="2026-08-01",
        help="空库回填起点（有数据的库自动取其最新日期-5 日重叠）",
    )
    args = parser.parse_args()
    result = sync_index_daily(date_type.fromisoformat(args.since))
    print(f"[index] result: {result}", flush=True)


if __name__ == "__main__":
    main()
