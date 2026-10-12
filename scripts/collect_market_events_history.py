#!/usr/bin/env python3
"""按交易日补采**第五类信息**候选（资金流 ``moneyflow`` / 龙虎榜 ``top_list``）。

为什么要这个脚本：改进计划 §3.2 允许"新闻/主题/资金流"作为特征或风险信息进入，
但前提是**有可算的输入**。本地仓库副本实测（2026-10-08，`market_copy.duckdb`）：

| 表 | 行数 | 不同符号 |
| --- | --- | --- |
| ``moneyflow`` | 113 | 2 |
| ``top_list_events`` | 40 | 2 |
| ``block_trade_events`` | 3 | 2 |
| ``margin_detail`` / ``hk_hold`` | 0 | 0 |

也就是说这两类信息在副本里只是**抽样存在**，既不能进 ``--features`` 也做不了
截面风险判定。要谈"第五类信息"必须先把符号面补到全市场。

与 ``collect_stk_limit_history.py`` 同一套纪律：

* 只依赖 stdlib + pandas + tushare，要在生产容器里跑；
* token 只按环境变量**名字**读，取值绝不打印、绝不落进 CSV/报告；
* 正好 10,000 行按**被截断**处理，不当成拉全了；
* 失败日子如实进 ``days_failed`` 并非零退出 —— 部分覆盖不得被读成补齐了；
* 只采集，不写任何库；进研究库由后续同步器负责，且**不进正式候选特征**，
  除非它在样本外自己站得住（契约要求只有 OOS 有效的特征才能进正式候选）。

用法（生产容器内）::

    python collect_market_events_history.py --api moneyflow \\
        --start 20250101 --end 20251231 \\
        --out /tmp/moneyflow_2025.csv --report /tmp/moneyflow_2025_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

#: 单次响应行数上限；正好等于它就说明被接口截断。
API_ROW_LIMIT = 10_000

#: token 所在的环境变量名。只读名字，不读值。
DEFAULT_TOKEN_ENV = "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"

#: 每个 API 的取数字段。龙虎榜刻意不带 ``trade_date`` 之外的解释性字段 ——
#: 这里要的是可核对的原始事实，不是别人算好的结论。
APIS: dict[str, dict[str, Any]] = {
    "moneyflow": {
        "method": "moneyflow",
        "fields": ",".join([
            "trade_date", "ts_code", "buy_sm_vol", "sell_sm_vol",
            "buy_md_vol", "sell_md_vol", "buy_lg_vol", "sell_lg_vol",
            "buy_elg_vol", "sell_elg_vol", "net_mf_vol", "net_mf_amount",
        ]),
    },
    "top_list": {
        "method": "top_list",
        "fields": ",".join([
            "trade_date", "ts_code", "name", "close", "pct_change",
            "turnover_rate", "amount", "l_sell", "l_buy", "net_amount",
            "reason",
        ]),
    },
}


def _resolve_token(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(
            f"环境变量 {env_name} 不存在或为空；采集 fail-closed，不做兜底填充。"
        )
    return value


def _call_with_retry(func: Any, kwargs: dict[str, Any], *, retries: int, sleep: float) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return func(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络/接口异常都必须重试后如实上报
            last = exc
            if attempt < retries:
                time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"调用失败（重试 {retries} 次后）: {type(last).__name__}: {last}")


def open_days(
    pro: Any, start: str, end: str, *, exchange: str, retries: int, sleep: float
) -> list[str]:
    cal = _call_with_retry(
        pro.trade_cal,
        {"exchange": exchange, "start_date": start, "end_date": end, "is_open": "1"},
        retries=retries,
        sleep=sleep,
    )
    days = sorted(str(item) for item in cal["cal_date"].tolist())
    if not days:
        raise SystemExit(f"交易日历在 {start}~{end} 内没有开市日，采集终止。")
    return days


def collect(
    pro: Any,
    api: str,
    days: list[str],
    out_path: str,
    *,
    retries: int,
    sleep: float,
    row_limit: int = API_ROW_LIMIT,
    progress: bool = True,
) -> dict[str, Any]:
    """逐日取一个 API 的原始行，按**列名并集**写进 ``out_path``。

    列并集而不是第一天的列：龙虎榜在个别日子会少回字段，只按首日取列会把
    后面的数据静默丢掉。缺列写空值，缺日子进 ``days_failed``。
    """
    spec = APIS[api]
    method = getattr(pro, str(spec["method"]))
    fields = str(spec["fields"])

    frames: dict[str, Any] = {}
    failed: dict[str, str] = {}
    truncated: list[str] = []
    columns: list[str] = []
    for index, day in enumerate(days, start=1):
        try:
            frame = _call_with_retry(
                method, {"trade_date": day, "fields": fields},
                retries=retries, sleep=sleep,
            )
        except Exception as exc:  # noqa: BLE001
            failed[day] = f"{type(exc).__name__}: {exc}"[:300]
            if progress:
                print(f"[{index}/{len(days)}] {day} FAILED", file=sys.stderr, flush=True)
            continue
        frames[day] = frame
        for name in list(frame.columns):
            if name not in columns:
                columns.append(name)
        if len(frame) == row_limit:
            truncated.append(day)
        if progress:
            print(f"[{index}/{len(days)}] {day} rows={len(frame)}", file=sys.stderr, flush=True)
        time.sleep(0.35)

    import pandas as pd

    keep = ("trade_date", "ts_code")
    ordered = list(keep) + [name for name in columns if name not in keep]
    pieces = [
        frame.reindex(columns=ordered) for frame in frames.values() if not frame.empty
    ]
    if pieces:
        pd.concat(pieces, axis=0, ignore_index=True)[ordered].to_csv(out_path, index=False)
    else:
        pd.DataFrame(columns=ordered).to_csv(out_path, index=False)

    counts = {day: len(frame) for day, frame in frames.items()}
    return {
        "api": api,
        "requested_days": len(days),
        "collected_days": len(counts),
        "empty_days": sorted(day for day, count in counts.items() if count == 0),
        "rows_total": int(sum(counts.values())),
        "days_failed": failed,
        "days_at_row_limit_possibly_truncated": truncated,
        "columns": ordered,
        "rows_per_day_min": min(counts.values()) if counts else 0,
        "rows_per_day_max": max(counts.values()) if counts else 0,
        "out_path": out_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, choices=sorted(APIS))
    parser.add_argument("--start", default="", help="YYYYMMDD（未给 --days 时必填）")
    parser.add_argument("--end", default="", help="YYYYMMDD（未给 --days 时必填）")
    parser.add_argument("--out", required=True, help="CSV 输出路径（容器内可写）")
    parser.add_argument("--report", default="", help="汇总 JSON 输出路径")
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--days", default="", help="逗号分隔的 YYYYMMDD，指定后忽略日历")
    parser.add_argument("--limit-days", type=int, default=0, help="只取前 N 天（权限试采用）")
    args = parser.parse_args(argv)

    # --days 与 --start/--end 两条腿必须至少有一条说得通；同 collect_stk_limit_history。
    if not args.days and not (args.start and args.end):
        parser.error("需要 --days，或者同时给 --start 与 --end")

    import tushare as ts

    pro = ts.pro_api(_resolve_token(args.token_env))

    if args.days:
        days = sorted({item.strip() for item in args.days.split(",") if item.strip()})
    else:
        days = open_days(
            pro, args.start, args.end, exchange=args.exchange,
            retries=args.retries, sleep=args.sleep,
        )
    if args.limit_days:
        days = days[: int(args.limit_days)]

    summary = collect(pro, args.api, days, args.out, retries=args.retries, sleep=args.sleep)
    summary["start"] = args.start
    summary["end"] = args.end

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)

    # 有任何日子失败 ⇒ 非零退出：调用方不能把"部分覆盖"当成"补齐了"。
    return 0 if not summary["days_failed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
