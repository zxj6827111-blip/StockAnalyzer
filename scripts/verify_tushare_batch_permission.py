"""Verify Tushare trade_date full-market batch permission on the NAS token.

Tushare's ``pro.daily`` / ``pro.daily_basic`` / ``pro.adj_factor`` accept a
``trade_date`` parameter that returns the whole market in one call, but that
capability is gated by the account's points level (typically 2000+). Run this
script BEFORE switching the nightly updater to batch mode:

    python scripts/verify_tushare_batch_permission.py [--token <tushare_token>] [--check-basic]

Token resolution order: ``--token``, then ``TUSHARE_TOKEN``, then
``SA__MARKET_WAREHOUSE__TUSHARE_TOKEN``. Without any token the script exits
with code 2. The token is never echoed in the output.

stdout carries one JSON document:

    {
      "ok": bool,
      "trade_cal_ok": bool,
      "trade_date": "YYYY-MM-DD",
      "daily_rows": int,
      "daily_basic_rows": int,   (-1 when --check-basic not given)
      "adj_factor_rows": int,    (-1 when --check-basic not given)
      "verdict": "full_market_batch_allowed" | "partial_permission" | "failed",
      "hint": "..."
    }

Exit code: 0 when the probe ran (verdict may be partial/failed), 2 when no
token could be resolved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd  # noqa: E402

from stock_analyzer.data.tushare_provider import (  # noqa: E402
    TushareProvider,
)

FULL_MARKET_MIN_ROWS = 3000
PROBE_BACKTRACK_DAYS = 3


def _resolve_token(cli_token: str) -> str:
    value = str(cli_token or "").strip()
    if value:
        return value
    for key in ("TUSHARE_TOKEN", "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"):
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _frame_rows(raw: object) -> int:
    if isinstance(raw, pd.DataFrame):
        return len(raw)
    return 0


def _resolve_latest_trade_date(
    pro: object,
    provider: TushareProvider,
) -> tuple[date | None, bool]:
    """Probe the SSE calendar backwards from today (up to 3 days)."""
    today = date.today()
    for offset in range(PROBE_BACKTRACK_DAYS + 1):
        candidate = today - timedelta(days=offset)
        day_s = candidate.strftime("%Y%m%d")
        try:
            raw = provider._call_with_retry(
                lambda day_s=day_s: pro.trade_cal(
                    exchange="SSE",
                    start_date=day_s,
                    end_date=day_s,
                    is_open="1",
                )
            )
            frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame()
            if not frame.empty and "cal_date" in frame.columns:
                return candidate, True
        except Exception:
            continue
    return today - timedelta(days=PROBE_BACKTRACK_DAYS), False


def _probe_trade_date_interface(
    pro: object,
    provider: TushareProvider,
    trade_date: date,
    api_name: str,
) -> tuple[int, str]:
    """One trade_date call for the given pro interface; returns (rows, error)."""
    day_s = trade_date.strftime("%Y%m%d")
    try:
        raw = provider._call_with_retry(
            lambda: getattr(pro, api_name)(trade_date=day_s)
        )
        return _frame_rows(raw), ""
    except Exception as exc:  # noqa: BLE001 - diagnostic script must never crash
        return 0, f"{type(exc).__name__}: {exc}"


def _build_hint(
    *,
    verdict: str,
    daily_rows: int,
    daily_error: str,
    trade_cal_ok: bool,
    check_basic: bool,
    basic_rows: dict[str, int],
) -> str:
    parts: list[str] = []
    if verdict == "full_market_batch_allowed":
        parts.append(
            f"pro.daily(trade_date) 返回 {daily_rows} 行，达到全市场批量规模"
            f"（阈值 {FULL_MARKET_MIN_ROWS}），可启用按 trade_date 批量补数"
        )
    elif verdict == "partial_permission":
        parts.append(
            f"pro.daily(trade_date) 仅返回 {daily_rows} 行，未达全市场规模"
            f"（阈值 {FULL_MARKET_MIN_ROWS}）。账号积分等级可能不足或接口受限："
            f"建议检查 Tushare 积分等级，或继续使用逐股模式"
            f"（update_vendor_daily_from_tushare.py 不传 --batch）"
        )
    else:
        parts.append(
            f"pro.daily(trade_date) 调用失败：{daily_error}。"
            f"建议检查积分等级与接口权限，或继续使用逐股模式"
            f"（update_vendor_daily_from_tushare.py 不传 --batch）"
        )
        if "unexpected keyword argument" in daily_error:
            parts.append(
                "当前环境未安装 tushare SDK（走 HTTP fallback），不支持 trade_date "
                "批量参数；请在安装 tushare SDK 的 NAS 容器镜像中运行本脚本"
            )
    if check_basic:
        for name in ("daily_basic", "adj_factor"):
            rows = basic_rows.get(name, -1)
            if rows < 0:
                continue
            if rows == 0:
                parts.append(f"pro.{name}(trade_date) 无返回或调用失败，同样受限")
            else:
                parts.append(f"pro.{name}(trade_date) 返回 {rows} 行")
    if not trade_cal_ok:
        parts.append("trade_cal 探测失败：SSE 交易日历不可用，批量切换前需先修复日历接口")
    return "；".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token",
        default="",
        help="Tushare token; defaults to TUSHARE_TOKEN or "
        "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN environment variable",
    )
    parser.add_argument(
        "--check-basic",
        action="store_true",
        help="Also probe pro.daily_basic and pro.adj_factor by trade_date "
        "(one call each)",
    )
    args = parser.parse_args(argv)

    token = _resolve_token(args.token)
    if not token:
        print(
            "tushare token missing; export TUSHARE_TOKEN or "
            "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN",
            file=sys.stderr,
        )
        return 2

    provider = TushareProvider(
        token=token,
        retry_delay_sec=0.5,
        min_request_interval_sec=0.5,
        max_attempts=2,
        price_series_mode="raw",
    )

    result: dict[str, object] = {
        "ok": False,
        "trade_cal_ok": False,
        "trade_date": "",
        "daily_rows": 0,
        "daily_basic_rows": -1,
        "adj_factor_rows": -1,
        "verdict": "failed",
        "hint": "",
    }
    daily_rows = 0
    daily_error = ""
    basic_rows: dict[str, int] = {}
    try:
        pro = provider._resolve_pro_api()
        trade_date, trade_cal_ok = _resolve_latest_trade_date(pro, provider)
        result["trade_cal_ok"] = bool(trade_cal_ok)
        if trade_date is not None:
            result["trade_date"] = trade_date.isoformat()
        else:
            result["trade_cal_ok"] = False

        if trade_date is None:
            raise RuntimeError("unable to resolve a candidate trade date")

        daily_rows, daily_error = _probe_trade_date_interface(
            pro, provider, trade_date, "daily"
        )
        result["daily_rows"] = daily_rows
        if args.check_basic:
            for api_name in ("daily_basic", "adj_factor"):
                rows, _ = _probe_trade_date_interface(
                    pro, provider, trade_date, api_name
                )
                basic_rows[api_name] = rows
                result[f"{api_name}_rows"] = rows
    except Exception as exc:  # noqa: BLE001 - diagnostic script must never crash
        daily_error = f"{type(exc).__name__}: {exc}"

    if daily_rows >= FULL_MARKET_MIN_ROWS:
        verdict = "full_market_batch_allowed"
    elif daily_rows > 0:
        verdict = "partial_permission"
    else:
        verdict = "failed"
    result["verdict"] = verdict
    result["ok"] = bool(result["trade_cal_ok"]) and verdict == "full_market_batch_allowed"
    result["hint"] = _build_hint(
        verdict=verdict,
        daily_rows=daily_rows,
        daily_error=daily_error,
        trade_cal_ok=bool(result["trade_cal_ok"]),
        check_basic=bool(args.check_basic),
        basic_rows=basic_rows,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
