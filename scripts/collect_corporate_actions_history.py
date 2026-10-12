#!/usr/bin/env python3
"""采集逐交易日已实施公司行动；只落离线 CSV，不写任何数据库。

Tushare ``dividend`` 以除权除息日查询全市场。此脚本对历史区间的每个开市日都保存
覆盖行，包括零事件日；失败日或达到接口 2,000 行上限的日子均标记为不完整。
公司行动现金与每股送转字段仍保留原始值，研究库导入和退出账本另行复核。

用法（有 Tushare 凭据的采集环境）::

    python scripts/collect_corporate_actions_history.py \\
        --start 20250101 --end 20260731 \\
        --events-out /tmp/corporate_actions.csv \\
        --coverage-out /tmp/corporate_action_coverage.csv \\
        --report /tmp/corporate_actions_report.json

Token 只从环境变量读取，不打印、不写入输出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from typing import Any

API_ROW_LIMIT = 2_000
DEFAULT_TOKEN_ENV = "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"
SOURCE = "tushare_dividend_ex_date"
FIELDS = ",".join((
    "ts_code", "end_date", "ann_date", "div_proc", "stk_div", "stk_bo_rate",
    "stk_co_rate", "cash_div", "cash_div_tax", "record_date", "ex_date",
    "pay_date", "div_listdate", "imp_ann_date", "base_date", "base_share",
))
EVENT_COLUMNS = tuple(FIELDS.split(",")) + ("source", "as_of", "coverage_complete")
COVERAGE_COLUMNS = (
    "ex_date", "source", "as_of", "rows_returned", "coverage_complete",
    "truncated", "error", "response_sha256",
)


def _resolve_token(env_name: str) -> str:
    token = str(os.environ.get(env_name, "") or "").strip()
    if not token:
        raise SystemExit(f"环境变量 {env_name} 不存在或为空；采集停止。")
    return token


def _call_with_retry(func: Any, kwargs: dict[str, Any], *, retries: int, sleep: float) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return func(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 不把网络失败伪装成无事件日
            last = exc
            if attempt < retries:
                time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"接口调用失败（重试 {retries} 次）: {type(last).__name__}: {last}")


def open_days(pro: Any, start: str, end: str, *, retries: int, sleep: float) -> list[str]:
    cal = _call_with_retry(
        pro.trade_cal,
        {"exchange": "SSE", "start_date": start, "end_date": end, "is_open": "1"},
        retries=retries,
        sleep=sleep,
    )
    days = sorted(str(value) for value in cal["cal_date"].tolist())
    if not days:
        raise SystemExit(f"交易日历在 {start}~{end} 内没有开市日；采集停止。")
    return days


def collect(
    pro: Any,
    days: list[str],
    *,
    retries: int = 3,
    sleep: float = 2.0,
    progress: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """为每个除权日留一行采集覆盖证据，错误与截断都不能判成完整。"""
    events: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    truncated: list[str] = []
    as_of = datetime.now().astimezone().isoformat(timespec="seconds")
    for index, day in enumerate(days, start=1):
        error = ""
        frame = None
        try:
            frame = _call_with_retry(
                pro.dividend,
                {"ex_date": day, "fields": FIELDS},
                retries=retries,
                sleep=sleep,
            )
            missing = sorted(set(EVENT_COLUMNS[:len(FIELDS.split(","))]) - set(frame.columns))
            if missing:
                raise ValueError(f"response missing columns {missing}")
        except Exception as exc:  # noqa: BLE001 - 避免异常文本回显请求凭据
            error = type(exc).__name__
            failed[day] = error

        rows_returned = int(len(frame)) if frame is not None else 0
        is_truncated = rows_returned >= API_ROW_LIMIT
        if is_truncated:
            truncated.append(day)
        complete = not error and not is_truncated
        digest = ""
        if frame is not None:
            normalized = frame.reindex(columns=EVENT_COLUMNS[:len(FIELDS.split(","))])
            digest = hashlib.sha256(
                normalized.to_csv(index=False, lineterminator="\n").encode("utf-8")
            ).hexdigest()
            for item in normalized.to_dict("records"):
                events.append({
                    **item,
                    "source": SOURCE,
                    "as_of": as_of,
                    "coverage_complete": complete,
                })
        coverage.append({
            "ex_date": day,
            "source": SOURCE,
            "as_of": as_of,
            "rows_returned": rows_returned,
            "coverage_complete": complete,
            "truncated": is_truncated,
            "error": error,
            "response_sha256": digest,
        })
        if progress:
            state = "FAILED" if error else ("TRUNCATED" if is_truncated else "ok")
            print(f"[{index}/{len(days)}] {day} rows={rows_returned} {state}",
                  file=sys.stderr, flush=True)
        time.sleep(0.35)
    summary = {
        "source": SOURCE,
        "requested_days": len(days),
        "successful_days": sum(not bool(item["error"]) for item in coverage),
        "completed_days": sum(bool(item["coverage_complete"]) for item in coverage),
        "rows_total": len(events),
        "days_failed": failed,
        "days_at_row_limit_possibly_truncated": truncated,
        "coverage_complete": bool(days) and not failed and not truncated,
        "as_of": as_of,
    }
    return events, coverage, summary


def _write_csv(path: str, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    import pandas as pd

    pd.DataFrame(rows, columns=list(columns)).to_csv(path, index=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--events-out", required=True)
    parser.add_argument("--coverage-out", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--quiet", action="store_true", help="只输出最终汇总")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--start must be <= --end")
    try:
        import tushare as ts
        pro = ts.pro_api(_resolve_token(args.token_env))
        days = open_days(pro, args.start, args.end, retries=args.retries, sleep=args.sleep)
        events, coverage, summary = collect(
            pro, days, retries=args.retries, sleep=args.sleep, progress=not args.quiet,
        )
        _write_csv(args.events_out, events, EVENT_COLUMNS)
        _write_csv(args.coverage_out, coverage, COVERAGE_COLUMNS)
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001 - CLI 报告失败本身必须非零
        print(f"公司行动采集失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["coverage_complete"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
