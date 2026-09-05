"""背景字段按日批量回填（修复 7/31 起裸行问题——见 docs/week5_datagate_root_cause_20260905.md）。

对 [start, end] 内每个交易日，用 Tushare Pro 按日 bulk 接口拉取并 UPDATE
market.duckdb daily_bars 的背景字段：

- ``block_trade``      → block_trade_net / block_trade_amount（当日大宗成交金额合计，元）、
                          block_trade_volume（股）；bulk 无买卖方向，net 口径=成交金额合计
                          （与旧链 akshare 净额口径的差异已在根因文档记录）。
- ``margin_detail``    → financing_balance / margin_financing_balance（融资余额 rzye，元）；
                          非两融标的 0 填（与 adapter 的 fillna(0) 语义一致）。
- ``top_list``         → dragon_tiger_flag（当日上榜 1.0 / 否则 0.0）。
- northbound_net       → 0.0 占位：逐日北向个股数据源不可得，且旧链路（≤7/30）该列
                          本就全 0——保持历史口径连续性。

同时盖章 background_data_source='tushare_pro_bulk_backfill'、
background_data_complete=True、background_missing_fields='optional:holder_count'
（holder_count 为 adapter 口径的可选字段，v1 不回填）。

限频：每次 Tushare 调用之间 sleep（config market_warehouse.request_interval_sec，
可用 --request-interval-sec 覆盖）。按日幂等：重复执行仅覆盖同日期字段。
不写 ZIP / delta / 任何其他表。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.data.tushare_provider import (  # noqa: E402
    _HttpTushareProApi,
    _resolve_tushare_token,
)

MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
BG_SOURCE = "tushare_pro_bulk_backfill"
BG_MISSING = "optional:holder_count"


def _trading_dates(con, start: date, end: date) -> list[date]:
    rows = con.execute(
        "SELECT DISTINCT date FROM daily_bars WHERE date BETWEEN ? AND ? ORDER BY 1",
        [start, end],
    ).fetchall()
    return [r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])) for r in rows]


def _call(http, api_name: str, **params: object):
    return http._call(api_name, **params)  # noqa: SLF001 - 复用内置 host 容错客户端


def _bulk_maps(http, day: date) -> dict[str, object]:
    yyyymmdd = day.strftime("%Y%m%d")

    block_amount: dict[str, float] = {}
    block_volume: dict[str, float] = {}
    frame = _call(http, "block_trade", trade_date=yyyymmdd)
    if frame is not None and not frame.empty:
        for row in frame.to_dict("records"):
            code = str(row.get("ts_code", "")).split(".")[0]
            if not code:
                continue
            amount = float(row.get("amount") or 0.0) * 1e4  # 万元→元
            volume = float(row.get("vol") or 0.0) * 1e4  # 万股→股
            block_amount[code] = block_amount.get(code, 0.0) + amount
            block_volume[code] = block_volume.get(code, 0.0) + volume

    financing: dict[str, float] = {}
    frame = _call(http, "margin_detail", trade_date=yyyymmdd)
    if frame is not None and not frame.empty:
        for row in frame.to_dict("records"):
            code = str(row.get("ts_code", "")).split(".")[0]
            if not code:
                continue
            financing[code] = float(row.get("rzye") or 0.0)  # 元

    dragon: dict[str, float] = {}
    frame = _call(http, "top_list", trade_date=yyyymmdd)
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


def _update_date(con, day: date, maps: dict[str, object], as_of: str) -> int:
    block_amount: dict[str, float] = maps["block_amount"]  # type: ignore[assignment]
    block_volume: dict[str, float] = maps["block_volume"]  # type: ignore[assignment]
    financing: dict[str, float] = maps["financing"]  # type: ignore[assignment]
    dragon: dict[str, float] = maps["dragon"]  # type: ignore[assignment]

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "UPDATE daily_bars SET block_trade_net=0.0, block_trade_amount=0.0, "
            "block_trade_volume=0.0, financing_balance=0.0, "
            "margin_financing_balance=0.0, dragon_tiger_flag=0.0, northbound_net=0.0 "
            "WHERE date = ?",
            [day],
        )
        if block_amount:
            con.register("bg_stage_block", _stage_frame(block_amount, block_volume))
            con.execute(
                "UPDATE daily_bars SET "
                "block_trade_net=stage.amount, block_trade_amount=stage.amount, "
                "block_trade_volume=stage.volume "
                "FROM bg_stage_block stage "
                "WHERE daily_bars.date = ? AND daily_bars.symbol = stage.symbol",
                [day],
            )
        if financing:
            con.register("bg_stage_fin", _stage_frame(financing, {}))
            con.execute(
                "UPDATE daily_bars SET "
                "financing_balance=stage.amount, margin_financing_balance=stage.amount "
                "FROM bg_stage_fin stage "
                "WHERE daily_bars.date = ? AND daily_bars.symbol = stage.symbol",
                [day],
            )
        if dragon:
            con.register("bg_stage_dt", _stage_frame(dragon, {}))
            con.execute(
                "UPDATE daily_bars SET dragon_tiger_flag=stage.amount "
                "FROM bg_stage_dt stage "
                "WHERE daily_bars.date = ? AND daily_bars.symbol = stage.symbol",
                [day],
            )
        con.execute(
            "UPDATE daily_bars SET background_data_source=?, background_data_complete=TRUE, "
            "background_missing_fields=?, background_as_of=? WHERE date = ?",
            [BG_SOURCE, BG_MISSING, as_of, day],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return 1


def _stage_frame(amount: dict[str, float], volume: dict[str, float]):
    import pandas as pd

    return pd.DataFrame(
        {
            "symbol": list(amount.keys()),
            "amount": [float(v) for v in amount.values()],
            "volume": [float(volume.get(k, 0.0)) for k in amount],
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--config", default="/app/config/default.yaml")
    parser.add_argument("--market-db", default=MARKET_DB)
    parser.add_argument("--request-interval-sec", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    config = load_config(args.config)

    interval = float(args.request_interval_sec)
    if interval <= 0:
        interval = 0.15
        try:
            mw = getattr(config, "market_warehouse", None)
            raw = float(getattr(mw, "request_interval_sec", 0.0) or 0.0)
            if raw > 0:
                interval = raw
        except Exception:  # noqa: BLE001
            pass

    token = _resolve_tushare_token() or str(
        getattr(config.data_source, "tushare_token", "") or ""
    )
    if not token:
        print("no tushare token resolved", flush=True)
        return 2
    http = _HttpTushareProApi(token=token, timeout_sec=30.0)

    con = duckdb.connect(args.market_db)
    days = _trading_dates(con, start, end)
    print(f"[1] trading dates in [{start}, {end}]: {len(days)}", flush=True)
    if args.dry_run:
        print("dry-run: no writes", flush=True)
        con.close()
        return 0

    updated = 0
    started = time.time()
    for index, day in enumerate(days):
        maps = _bulk_maps(http, day)
        time.sleep(interval)
        _update_date(con, day, maps, datetime.now().isoformat())
        updated += 1
        print(
            f"[2] {day} block={len(maps['block_amount'])} fin={len(maps['financing'])} "
            f"dragon={len(maps['dragon'])} ({index + 1}/{len(days)})",  # type: ignore[arg-type]
            flush=True,
        )
    con.close()
    print(
        json.dumps(
            {
                "updated_dates": updated,
                "total_seconds": round(time.time() - started, 1),
                "bg_source": BG_SOURCE,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
