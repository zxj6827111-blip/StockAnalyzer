"""market.duckdb daily_bars 富集共享逻辑（背景 bulk + PIT 财务）。

背景：7/31 起 daily_bars 由 sync 裸行链路写入，财务/背景字段全空（见
docs/week5_datagate_root_cause_20260905.md）。本模块把两条富集路径收敛为
一处，供以下调用方共用：

- ``scripts/backfill_background_fields.py``（历史区间一次性回填）；
- ``scripts/sync_market_duckdb.py``（每日 cron 同步后的当日富集钩子）。

口径说明（与既有链路对齐）：
- northbound_net 以 0.0 占位（逐日北向个股披露不可得，且 ≤7/30 的历史行
  本就全 0——保持特征口径连续性）；
- block_trade_net = 当日大宗成交金额合计（bulk 端点无买卖方向，与旧
  akshare 净额口径的差异记录于根因文档）；
- 非两融标的融资余额 0 填、未上榜 dragon_tiger_flag=0（与 adapter 的
  fillna(0) 语义一致）。
"""

from __future__ import annotations

import time
from datetime import date as _date
from typing import Any

import pandas as pd

from stock_analyzer.data.financial_pit import apply_financial_snapshots_asof_batch
from stock_analyzer.data.tushare_provider import _HttpTushareProApi

BACKGROUND_SOURCE_BULK = "tushare_pro_bulk_backfill"
BACKGROUND_MISSING_OPTIONAL_HOLDER = "optional:holder_count"


def fetch_bulk_background_maps(http: _HttpTushareProApi, day: _date) -> dict[str, dict[str, float]]:
    """拉取单个交易日的三个 bulk 端点并按 symbol 聚合。"""

    yyyymmdd = day.strftime("%Y%m%d")

    block_amount: dict[str, float] = {}
    block_volume: dict[str, float] = {}
    frame = http._call("block_trade", trade_date=yyyymmdd)  # noqa: SLF001
    if frame is not None and not frame.empty:
        for row in frame.to_dict("records"):
            code = str(row.get("ts_code", "")).split(".")[0]
            if not code:
                continue
            block_amount[code] = block_amount.get(code, 0.0) + float(row.get("amount") or 0.0) * 1e4
            block_volume[code] = block_volume.get(code, 0.0) + float(row.get("vol") or 0.0) * 1e4

    financing: dict[str, float] = {}
    frame = http._call("margin_detail", trade_date=yyyymmdd)  # noqa: SLF001
    if frame is not None and not frame.empty:
        for row in frame.to_dict("records"):
            code = str(row.get("ts_code", "")).split(".")[0]
            if not code:
                continue
            financing[code] = float(row.get("rzye") or 0.0)

    dragon: dict[str, float] = {}
    frame = http._call("top_list", trade_date=yyyymmdd)  # noqa: SLF001
    if frame is not None and not frame.empty:
        for row in frame.to_dict("records"):
            code = str(row.get("ts_code", "")).split(".")[0]
            if code:
                dragon[code] = 1.0

    return {
        "block_amount": block_amount,
        "block_volume": block_volume,
        "financing": financing,
        "dragon": dragon,
    }


def _stage_frame(amount: dict[str, float], volume: dict[str, float] | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": list(amount.keys()),
            "amount": [float(v) for v in amount.values()],
            "volume": [float((volume or {}).get(k, 0.0)) for k in amount],
        }
    )


def update_daily_bars_background(
    con: Any,
    day: _date,
    maps: dict[str, dict[str, float]],
    *,
    as_of: str,
    source: str = BACKGROUND_SOURCE_BULK,
) -> None:
    """把单日背景字段写回 daily_bars（幂等，事务内覆盖写）。"""

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "UPDATE daily_bars SET block_trade_net=0.0, block_trade_amount=0.0, "
            "block_trade_volume=0.0, financing_balance=0.0, "
            "margin_financing_balance=0.0, dragon_tiger_flag=0.0, northbound_net=0.0 "
            "WHERE date = ?",
            [day],
        )
        block_amount = maps["block_amount"]
        if block_amount:
            con.register(
                "bg_stage_block", _stage_frame(block_amount, maps["block_volume"])
            )
            con.execute(
                "UPDATE daily_bars SET "
                "block_trade_net=stage.amount, block_trade_amount=stage.amount, "
                "block_trade_volume=stage.volume "
                "FROM bg_stage_block stage "
                "WHERE daily_bars.date = ? AND daily_bars.symbol = stage.symbol",
                [day],
            )
        financing = maps["financing"]
        if financing:
            con.register("bg_stage_fin", _stage_frame(financing))
            con.execute(
                "UPDATE daily_bars SET "
                "financing_balance=stage.amount, margin_financing_balance=stage.amount "
                "FROM bg_stage_fin stage "
                "WHERE daily_bars.date = ? AND daily_bars.symbol = stage.symbol",
                [day],
            )
        dragon = maps["dragon"]
        if dragon:
            con.register("bg_stage_dt", _stage_frame(dragon))
            con.execute(
                "UPDATE daily_bars SET dragon_tiger_flag=stage.amount "
                "FROM bg_stage_dt stage "
                "WHERE daily_bars.date = ? AND daily_bars.symbol = stage.symbol",
                [day],
            )
        con.execute(
            "UPDATE daily_bars SET background_data_source=?, background_data_complete=TRUE, "
            "background_missing_fields=?, background_as_of=? WHERE date = ?",
            [source, BACKGROUND_MISSING_OPTIONAL_HOLDER, as_of, day],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def enrich_background_for_dates(
    con: Any,
    days: list[_date],
    http: _HttpTushareProApi,
    *,
    request_interval_sec: float = 0.35,
    source: str = BACKGROUND_SOURCE_BULK,
    progress=None,
) -> int:
    """逐日 bulk 拉取 + 写回背景字段；返回成功更新的日期数。"""

    updated = 0
    for index, day in enumerate(days):
        maps = fetch_bulk_background_maps(http, day)
        time.sleep(max(0.0, request_interval_sec))
        update_daily_bars_background(
            con, day, maps, as_of=pd.Timestamp.now().isoformat(), source=source
        )
        updated += 1
        if progress is not None:
            progress(index + 1, len(days), day, maps)
    return updated


_FINANCIAL_COLUMNS = (
    "roe",
    "debt_ratio",
    "financial_data_complete",
    "financial_missing_fields",
    "financial_source",
    "financial_report_date",
    "financial_as_of",
    "financial_trust_level",
    "financial_completeness",
)


def enrich_daily_financial_pit(
    con: Any,
    days: list[_date],
) -> int:
    """对指定日期集合的 daily_bars 行做 PIT 财务物化（批量 as-of join）。

    读 financial_snapshots（同库）→ ``apply_financial_snapshots_asof_batch``
    （only_fill_pending=False：裸行/占位行一律按 ann_date 语义重算）→ 按行
    写回财务列。返回写入行数。
    """

    if not days:
        return 0
    snapshots = con.execute("SELECT * FROM financial_snapshots").fetch_df()
    if snapshots.empty:
        return 0
    placeholders = ", ".join("?" for _ in days)
    bars = con.execute(
        f"SELECT symbol, date, {', '.join(_FINANCIAL_COLUMNS)} FROM daily_bars "
        f"WHERE date IN ({placeholders})",
        list(days),
    ).fetch_df()
    if bars.empty:
        return 0
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    enriched = apply_financial_snapshots_asof_batch(
        bars, snapshots, only_fill_pending=False
    )
    rows = enriched.dropna(subset=["symbol", "date"])
    if rows.empty:
        return 0
    con.register("fin_enrich_stage", rows)
    updated = int(
        con.execute(
            "SELECT COUNT(*) FROM fin_enrich_stage"
        ).fetchone()[0]
    )
    con.execute("BEGIN TRANSACTION")
    try:
        for column in _FINANCIAL_COLUMNS:
            con.execute(
                f"UPDATE daily_bars SET {column} = stage.{column} "
                "FROM fin_enrich_stage stage "
                "WHERE daily_bars.date = stage.date AND daily_bars.symbol = stage.symbol"
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.unregister("fin_enrich_stage")
    return updated
