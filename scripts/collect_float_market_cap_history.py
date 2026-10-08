#!/usr/bin/env python3
"""按交易日补采流通市值真值（tushare ``daily_basic.circ_mv``），落成 CSV。

为什么需要这个脚本：仓库 ``daily_bars.float_market_cap`` 自 2026-03 中旬起被
provider 填成常数 12,000,000,000（读取路径见 ADR-004 §3），硬性资格门
``min_float_market_cap`` 因此"跑完了却谁都不淘汰"。要判断这只门在污染窗口里
本来会淘汰多少股票，必须有独立来源的市值真值。

设计约束：

* **只依赖 stdlib + pandas + tushare**，不 import 本项目模块 —— 它要在生产容器里
  跑，容器里那份代码不一定是当前分支。
* token **只从环境变量按名字读**（默认 ``SA__MARKET_WAREHOUSE__TUSHARE_TOKEN``），
  取值绝不打印、绝不写入 CSV/报告。
* 单次响应上限 10,000 行：正好 10,000 行按"被截断"处理，不能当成拉全了。
* 偶发 DNS 失败（``Errno -3``）要重试，但重试耗尽的日子必须如实列进
  ``days_failed`` —— 不静默跳过。
* 只做采集，不改写任何数据库；写研究库由 ``load_float_market_cap_research.py`` 负责。

用法（生产容器内）::

    python collect_float_market_cap_history.py \\
        --start 20260311 --end 20260717 \\
        --out /tmp/float_cap_20260311_20260717.csv \\
        --report /tmp/float_cap_report.json
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

FIELDS = "ts_code,trade_date,circ_mv"

COLUMNS = ("ts_code", "trade_date", "circ_mv")


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
    days: list[str],
    out_path: str,
    *,
    retries: int,
    sleep: float,
    row_limit: int = API_ROW_LIMIT,
    progress: bool = True,
) -> dict[str, Any]:
    """逐日取 ``daily_basic``，把结果按行写进 ``out_path``（CSV，覆盖写）。

    返回一份可直接落盘的汇总：每日子行数、失败日子、疑似截断日子。
    """
    rows_total = 0
    per_day: dict[str, int] = {}
    failed: dict[str, str] = {}
    truncated: list[str] = []
    null_circ: dict[str, int] = {}

    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(COLUMNS) + "\n")
        for index, day in enumerate(days, start=1):
            try:
                frame = _call_with_retry(
                    pro.daily_basic,
                    {"trade_date": day, "fields": FIELDS},
                    retries=retries,
                    sleep=sleep,
                )
            except Exception as exc:  # noqa: BLE001
                failed[day] = f"{type(exc).__name__}: {exc}"[:300]
                if progress:
                    print(f"[{index}/{len(days)}] {day} FAILED", file=sys.stderr, flush=True)
                continue

            count = len(frame)
            per_day[day] = count
            if count == row_limit:
                truncated.append(day)
            if "circ_mv" in frame.columns:
                null_circ[day] = int(frame["circ_mv"].isna().sum())

            if count:
                subset = frame[list(COLUMNS)]
                handle.write(subset.to_csv(index=False, header=False))
            rows_total += count
            if progress:
                print(
                    f"[{index}/{len(days)}] {day} rows={count}"
                    f" null={null_circ.get(day, 'n/a')}",
                    file=sys.stderr,
                    flush=True,
                )
            # 接口限流保护：生产容器里跑，不要把配额打满后影响夜间任务。
            time.sleep(0.35)

    return {
        "requested_days": len(days),
        "collected_days": len(per_day),
        "rows_total": rows_total,
        "days_failed": failed,
        "days_at_row_limit_possibly_truncated": truncated,
        "days_with_null_circ_mv": {k: v for k, v in null_circ.items() if v},
        "rows_per_day_min": min(per_day.values()) if per_day else 0,
        "rows_per_day_max": max(per_day.values()) if per_day else 0,
        "out_path": out_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--out", required=True, help="CSV 输出路径（容器内可写）")
    parser.add_argument("--report", default="", help="汇总 JSON 输出路径")
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--days", default="", help="逗号分隔的 YYYYMMDD，指定后忽略日历")
    args = parser.parse_args(argv)

    import pandas  # noqa: F401  # 容器内可用性检查
    import tushare as ts

    pro = ts.pro_api(_resolve_token(args.token_env))

    if args.days:
        days = sorted({item.strip() for item in args.days.split(",") if item.strip()})
    else:
        days = open_days(
            pro, args.start, args.end, exchange=args.exchange,
            retries=args.retries, sleep=args.sleep,
        )

    summary = collect(
        pro, days, args.out, retries=args.retries, sleep=args.sleep
    )
    summary["start"] = args.start
    summary["end"] = args.end
    summary["cal_days"] = len(days)

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)

    # 有任何日子失败 ⇒ 非零退出：调用方不能把"部分覆盖"当成"补齐了"。
    return 0 if not summary["days_failed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
