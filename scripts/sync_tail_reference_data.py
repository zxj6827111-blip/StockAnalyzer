"""把 §3.1 要求的五类参考数据从生产仓库**只读**复制进独立研究库。

```bash
python scripts/sync_tail_reference_data.py \\
    --warehouse data/market.duckdb \\
    --out artifacts/research/tail_minute_bars.duckdb \\
    --start 2025-01-02 --end 2026-09-30 \\
    --symbols-file artifacts/research/tail_symbols.txt
```

研究库默认与分钟库同文件：尾盘标签与历史重建要同时吃分钟 bar 和日级参考数据。
生产仓库只以 ``read_only`` 打开，不会被这个命令改动。

退出码是**真实退出码**：

- ``0`` 五类来源（日历 / RAW 日线 / 精确涨跌停 / 停复牌 / 证券历史状态）都落到研究库
- ``3`` 有来源缺失（仓库里没这张表，或该表在窗口内没有行）→ 如实记为不足、继续采集；
  不得用比例近似或填 0 冒充已补齐
- ``5`` 输入不可用或口径不成立（仓库文件不存在、日线不是 raw 口径）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    REFERENCE_DB_DEFAULT,
    REFERENCE_TABLES,
    SOURCE_VENDOR_ZIP_DAILY,
    TailReferenceError,
    TailReferenceStore,
    read_vendor_daily_raw_frames,
    read_warehouse_reference_frames,
    sync_reference_from_warehouse,
)

RC_OK = 0
RC_INSUFFICIENT = 3
RC_ERROR = 5


def _load_symbols(args: argparse.Namespace) -> list[str] | None:
    if args.symbols:
        return [item.strip() for item in args.symbols.split(",") if item.strip()]
    if args.symbols_file:
        lines = Path(args.symbols_file).read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return None


#: tushare 单次调用最多回这么多行；namechange 达到这个数就是**被截断**，不是拉全了。
ROW_LIMIT = 10_000


def _six(ts_code: Any) -> str:
    return str(ts_code or "").split(".")[0].zfill(6)


def _iso(value: Any) -> str | None:
    """tushare 回的是 ``20260929`` 这种紧凑日期；落库口径统一成 ISO，空值保持空。"""
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] or None


def security_status_frame_from_payload(payload: dict[str, Any]) -> pd.DataFrame:
    """``stock_basic(L/D)`` + ``namechange`` 的补采载荷 → 带日期的证券状态区间表。

    - ``listing_period`` / ``delisting`` 给的是**存在区间**，退市史能证明就是证明了，
      幸存者偏差口径因此可以从 unknown 升一级；
    - ``name_change``（ST/*ST 史的载体）在被截断的响应里 coverage_complete 必须是 False：
      "拉到了 1,150 条"不等于"改名史完整"，拿前者当后者就是计划禁止的假装有数据；
    - symbol 一律压成 6 位代码，和研究库其它表同一口径。
    """
    truncated = int(payload.get("namechange_total") or 0) >= ROW_LIMIT
    rows: list[dict[str, Any]] = []
    for key, status_type in (("basic_L", "listing_period"), ("basic_D", "delisting")):
        for item in (payload.get(key) or {}).get("rows") or []:
            ts_code, _name, list_date, delist_date = (list(item) + [None] * 4)[:4]
            rows.append({
                "symbol": _six(ts_code),
                "status_type": status_type,
                "effective_from": _iso(list_date),
                "effective_to": _iso(delist_date),
                "status_value": str(_name or "") or None,
                "exchange": str(ts_code or "").split(".")[-1].upper() or None,
                "coverage_complete": True,
            })
    for item in (payload.get("namechange") or {}).get("rows") or []:
        ts_code, name, start_date, end_date = (list(item) + [None] * 4)[:4]
        rows.append({
            "symbol": _six(ts_code),
            "status_type": "name_change",
            "effective_from": _iso(start_date),
            "effective_to": _iso(end_date),
            "status_value": str(name or "") or None,
            "exchange": str(ts_code or "").split(".")[-1].upper() or None,
            "coverage_complete": not truncated,
        })
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="生产仓库 market.duckdb（只读）")
    parser.add_argument("--out", default=REFERENCE_DB_DEFAULT)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--symbols-file", default="")
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument(
        "--vendor-daily-root",
        default="",
        help="vendor 离线包根目录（其下需有 全A日K/*.zip）；仓库声明不了 RAW 口径时的替代日线源",
    )
    parser.add_argument("--report", default="")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--limit-prices-csv",
        default="",
        help="tushare stk_limit 补采结果 CSV：trade_date,ts_code,up_limit,down_limit",
    )
    parser.add_argument(
        "--security-status-json",
        default="",
        help="tushare stock_basic(L/D)+namechange 补采载荷 JSON：证券历史状态（退市/改名史）",
    )
    args = parser.parse_args(argv)

    try:
        start = date.fromisoformat(str(args.start)[:10])
        end = date.fromisoformat(str(args.end)[:10])
    except ValueError as exc:
        print(f"--start/--end 必须是 ISO 日期: {exc}", file=sys.stderr)
        return RC_ERROR
    if start > end:
        print("--start 晚于 --end", file=sys.stderr)
        return RC_ERROR

    try:
        symbols = _load_symbols(args)
        reading = read_warehouse_reference_frames(
            args.warehouse, start=start, end=end, symbols=symbols,
        )
        with TailReferenceStore(args.out) as store:
            result = sync_reference_from_warehouse(store, reading, exchange=args.exchange)
            if args.vendor_daily_root:
                # 仓库那条来源声明不了 RAW 口径时，vendor 全A日K 是可证明口径的替代源；
                # 它只补 daily_bars，其余四类仍以仓库为准。
                frames = read_vendor_daily_raw_frames(
                    args.vendor_daily_root, start=start, end=end, symbols=symbols,
                )
                report = {"root": str(args.vendor_daily_root), "rows": 0, "symbols": 0}
                if not frames.empty:
                    report["rows"] = store.upsert_daily_bars(
                        frames,
                        price_basis="raw",
                        source=SOURCE_VENDOR_ZIP_DAILY,
                        as_of=reading.as_of,
                    )
                    report["symbols"] = int(frames["symbol"].nunique())
                    # 可交易声明的落地：只有"当天确有 RAW 日线且成交量 > 0"才是
                    # **正向观察**到在交易，据此声明 trade_status='trading'。
                    # 缺 bar 的日子一律不写 —— 计划明令"不把缺 bar 当停牌"，
                    # 反过来把缺 bar 说成"没停牌可交易"同样是凭空声明，同样禁止。
                    trading = frames.loc[
                        frames["volume"].fillna(0.0) > 0.0, ["symbol", "date"]
                    ].copy()
                    trading["trade_date"] = trading["date"]
                    trading["suspended"] = False
                    trading["suspend_type"] = None
                    trading["trade_status"] = "trading"
                    already = store.suspended_symbol_days()
                    if already:
                        trading = trading.loc[
                            ~trading.set_index(["symbol", "trade_date"]).index.isin(already)
                        ]
                    report["trade_status_rows_landed"] = store.upsert_suspend_status(
                        trading, source="vendor_daily_bar_volume", as_of=end.isoformat(),
                    )
                    coverage = store.coverage()
                    result["coverage"] = coverage
                    result["landed_rows"]["daily_bars"] = report["rows"]
                    result["sufficient_sources"] = sorted(
                        name for name in REFERENCE_TABLES
                        if int(coverage["sources"][name]["rows"]) > 0
                    )
                    result["missing_sources"] = coverage["gaps"]
                result["vendor_daily_bars"] = report
            if args.limit_prices_csv:
                # 精确涨跌停的唯一权威来源是 tushare stk_limit(doc_id=183)：仓库那条
                # 只有 2 个符号 68 行。补采到的 CSV 在这里落库，口径由 source 说清楚，
                # 绝不用 pre_close×比例反算的近似值冒充（approximated 保持 False）。
                frame = pd.read_csv(Path(args.limit_prices_csv).expanduser())
                missing = [
                    name for name in ("trade_date", "ts_code", "up_limit", "down_limit")
                    if name not in frame.columns
                ]
                if missing:
                    print(f"--limit-prices-csv 缺少列 {missing}", file=sys.stderr)
                    return RC_ERROR
                frame = frame.assign(
                    symbol=frame["ts_code"].astype(str).str.split(".").str[0].str.zfill(6),
                    trade_date=pd.to_datetime(frame["trade_date"].astype(str),
                                              errors="coerce").dt.date,
                ).dropna(subset=["trade_date"])
                landed_limit = store.upsert_limit_prices(
                    frame, source="tushare_stk_limit", as_of=end.isoformat(),
                )
                coverage = store.coverage()
                result["landed_rows"]["limit_prices"] = landed_limit
                result["coverage"] = coverage
                result["sufficient_sources"] = sorted(
                    name for name in REFERENCE_TABLES
                    if int(coverage["sources"][name]["rows"]) > 0
                )
                result["missing_sources"] = coverage["gaps"]
                result["limit_prices_backfill"] = {
                    "csv": str(args.limit_prices_csv),
                    "rows_landed": landed_limit,
                    "dates_covered": int(frame["trade_date"].nunique()),
                    "symbols_covered": int(frame["symbol"].nunique()),
                }
            if args.security_status_json:
                # 证券历史状态（计划 §3.1）：仓库 security_status 实测 0 行，退市/改名史
                # 无从证明，幸存者偏差只能记 incomplete_or_unknown。这里落 tushare
                # stock_basic(L/D) + namechange 的补采结果；**接口只回 10,000 行时
                # namechange 是截断的**，那一类逐行 coverage_complete=False，
                # 不拿"拉到了"冒充"拉全了"。
                payload = json.loads(
                    Path(args.security_status_json).expanduser().read_text(encoding="utf-8")
                )
                frame = security_status_frame_from_payload(payload)
                landed_status = 0
                if not frame.empty:
                    landed_status = store.upsert_security_status(
                        frame, source="tushare_stock_basic_namechange",
                        as_of=end.isoformat(),
                    )
                coverage = store.coverage()
                result["landed_rows"]["security_status"] = landed_status
                result["coverage"] = coverage
                result["sufficient_sources"] = sorted(
                    name for name in REFERENCE_TABLES
                    if int(coverage["sources"][name]["rows"]) > 0
                )
                result["missing_sources"] = coverage["gaps"]
                result["security_status_backfill"] = {
                    "json": str(args.security_status_json),
                    "rows_landed": landed_status,
                    "delisted_symbols": int((frame["status_type"] == "delisting").sum())
                    if not frame.empty else 0,
                    "listed_symbols": int((frame["status_type"] == "listing_period").sum())
                    if not frame.empty else 0,
                    "name_change_rows": int((frame["status_type"] == "name_change").sum())
                    if not frame.empty else 0,
                    "name_change_truncated": int(payload.get("namechange_total") or 0) >= 10_000,
                }
    except (TailReferenceError, OSError, ValueError) as exc:
        print(f"参考数据不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    result["warehouse"] = str(args.warehouse)
    result["out"] = str(args.out)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if result["missing_sources"]:
        print(f"来源不足: {result['missing_sources']}")
        print("研究库只补到了部分参考数据；缺失的来源继续采集，不得用近似值顶替")
        return RC_INSUFFICIENT
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
