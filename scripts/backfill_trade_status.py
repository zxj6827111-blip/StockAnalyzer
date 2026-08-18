"""批量、可恢复、幂等地回填 daily_trade_status 表（精确涨跌停价 + 停牌状态）。

本脚本只写入 delta DuckDB 的 ``daily_trade_status`` 表，通过
``TushareProvider.fetch_trade_status`` 获取每只股票在指定区间的
``stk_limit``（精确涨停价 up_limit / 跌停价 down_limit）和 ``suspend_d``
（停牌信息），再交给 ``MarketWarehouse.upsert_trade_status`` 用
DELETE-then-INSERT 的方式幂等落库。

设计原则
--------
- 自然键：``symbol + trade_date``，``upsert_trade_status`` 内部按此键
  DELETE-then-INSERT，重复运行不会产生重复行。
- 精确优先：直接写入 Tushare 提供的精确涨跌停价；缺少精确标签时必须
  标记为 unavailable/unknown，绝不用 10%/20%/30% 涨幅阈值推算值冒充。
- 可恢复：JSON checkpoint 记录每个 ``fetch + upsert`` 都成功的 symbol；
  ``--resume`` 跳过已记录的 symbol；空响应不写 checkpoint，下次可重试。
- 失败安全：API 失败时只记 ``failures`` 并 ``continue``，绝不把失败
  股票标记为成功；checkpoint 只在 fetch + upsert 都成功后才写入。
- dry-run：不创建/修改数据库、不写 checkpoint、不调用 upsert。

Usage:
    python scripts/backfill_trade_status.py --db-path path/to/warehouse.duckdb
        [--start-date 2021-01-01] [--end-date 2026-08-17]
        [--symbols 000001,600519] [--symbols-file symbols.txt] [--limit 3000]
        [--resume] [--checkpoint-path artifacts/trade_status_backfill_checkpoint.json]
        [--dry-run] [--request-interval-sec 0.6] [--max-attempts 3]

Exit code: 0 表示全部成功（或 dry-run）；1 表示存在失败；2 表示无 symbol 或
参数非法。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 导入放在 sys.path 设置之后，确保能找到 src 下的模块
from stock_analyzer.data.tushare_provider import _normalize_symbol  # noqa: E402


def _load_checkpoint(path: Path) -> dict[str, str]:
    """读取 checkpoint 文件，返回 marker -> ISO 时间戳 的字典。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checkpoint(path: Path, checkpoint: dict[str, str], marker: str) -> None:
    """把 marker 记入 checkpoint 并落盘；只在 fetch + upsert 成功后调用。"""
    checkpoint[marker] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _marker_key(start_date: str, end_date: str, symbol: str) -> str:
    """生成 checkpoint 的 marker 字符串：``{symbol}|{start_date}|{end_date}``。"""
    return f"{symbol}|{start_date}|{end_date}"


def _parse_date(value: str, *, default: date) -> date:
    """解析 YYYY-MM-DD 字符串；空或非法时返回 default。"""
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date (expected YYYY-MM-DD): {value!r}") from None


def _resolve_symbols(args: argparse.Namespace, warehouse) -> list[str]:
    """从 --symbols / --symbols-file / warehouse.list_symbols 解析 symbol 列表。

    所有 symbol 都经 ``_normalize_symbol`` 归一化并去重排序。
    """
    raw_symbols: list[str] = []

    if args.symbols.strip():
        raw_symbols = [item for item in args.symbols.split(",") if item.strip()]
    elif args.symbols_file.strip():
        file_path = Path(args.symbols_file.strip()).expanduser()
        if not file_path.exists():
            raise FileNotFoundError(f"symbols file not found: {file_path}")
        raw_symbols = [
            line.strip()
            for line in file_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        # 从仓库已存在的 daily_bars 中获取全市场 symbol 列表
        try:
            raw_symbols = list(warehouse.list_symbols())
        except Exception:
            raw_symbols = []

    normalized = sorted(
        {code for item in raw_symbols if str(item) and (code := _normalize_symbol(item))}
    )
    return normalized


def query_limit_up_stocks(db_path: str, trade_date: str) -> list[dict]:
    """Return stocks whose close equals the exact limit-up price on the date."""
    from stock_analyzer.data.market_warehouse import MarketWarehouse

    warehouse = MarketWarehouse(db_path=db_path, package_root="", read_only=True)
    if not warehouse._table_exists("daily_trade_status"):
        return []
    if not warehouse._table_exists("daily_bars"):
        return []
    with warehouse._connect_readonly() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ts.symbol, ts.trade_date, ts.up_limit, ts.down_limit, "
            "ts.source, ts.coverage_complete "
            "FROM daily_trade_status ts "
            "INNER JOIN daily_bars db "
            "ON db.symbol = ts.symbol AND db.date = ts.trade_date "
            "WHERE ts.trade_date = ? AND ts.up_limit IS NOT NULL "
            "AND db.close IS NOT NULL AND ABS(db.close - ts.up_limit) <= 0.0001 "
            "ORDER BY ts.symbol",
            [trade_date],
        ).fetchall()
    return [
        {
            "symbol": row[0],
            "trade_date": str(row[1]),
            "up_limit": row[2],
            "down_limit": row[3],
            "source": row[4],
            "coverage_complete": row[5],
        }
        for row in rows
    ]


def _build_tushare_provider(args: argparse.Namespace):
    """根据环境变量和命令行参数构造 TushareProvider。"""
    from stock_analyzer.data.tushare_provider import TushareProvider

    token = str(os.environ.get("TUSHARE_TOKEN", "") or "").strip() or str(
        os.environ.get("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN", "") or ""
    ).strip()
    return TushareProvider(
        token=token,
        retry_delay_sec=max(0.0, float(args.request_interval_sec)),
        min_request_interval_sec=max(0.0, float(args.request_interval_sec)),
        max_attempts=max(1, int(args.max_attempts)),
        price_series_mode="qfq",
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        help="Delta DuckDB 路径",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="起始日期 YYYY-MM-DD（默认：end_date 前 365 天）",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="结束日期 YYYY-MM-DD（默认：今天）",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="逗号分隔的 symbol 列表；省略则取 warehouse 全市场",
    )
    parser.add_argument(
        "--symbols-file",
        default="",
        help="每行一个 symbol 的文件；省略则取 warehouse 全市场",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="限制本次处理的 symbol 数量（0 = 不限）",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="跳过 checkpoint 中已记录成功的 symbol",
    )
    parser.add_argument(
        "--checkpoint-path",
        default=str(ROOT / "artifacts" / "trade_status_backfill_checkpoint.json"),
        help="checkpoint 文件路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告不写：不创建/修改 DB、不写 checkpoint、不调用 upsert",
    )
    parser.add_argument(
        "--request-interval-sec",
        type=float,
        default=0.6,
        help="API 调用间隔秒数（含失败后）",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="单次 API 调用最大重试次数",
    )
    args = parser.parse_args(argv)

    from stock_analyzer.data.market_warehouse import MarketWarehouse

    # 日期解析：end_date 默认今天，start_date 默认 end_date 前 365 天
    try:
        end_date = _parse_date(args.end_date, default=date.today())
        start_date = _parse_date(args.start_date, default=end_date - timedelta(days=365))
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    start_date_s = start_date.isoformat()
    end_date_s = end_date.isoformat()

    # 构造仓库；dry-run 时不创建/修改 DB，因此使用 read_only 模式解析 symbol
    # 列表（list_symbols 仅读 daily_bars，不会触发建表/写库）。
    warehouse = MarketWarehouse(
        db_path=args.db_path,
        package_root=str(Path(args.db_path).parent / "package"),
        package_writes_enabled=False,
    )

    try:
        symbols = _resolve_symbols(args, warehouse)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not symbols:
        print("empty universe: nothing to backfill", file=sys.stderr)
        return 2

    # 应用 limit 限制
    limit = max(0, int(args.limit))
    if limit:
        symbols = symbols[:limit]

    # 加载 checkpoint 并按 marker 跳过已成功 symbol
    checkpoint_path = Path(args.checkpoint_path).expanduser()
    checkpoint = _load_checkpoint(checkpoint_path)
    interval_sec = max(0.0, float(args.request_interval_sec))

    ok = 0
    failed = 0
    empty = 0
    skipped = 0
    processed = 0
    rows_stored = 0
    failures: list[str] = []

    # dry-run 模式：不构造 TushareProvider 也能报告，但无法真正 fetch；
    # 这里仍构造 provider 以便输出准确报告（不调用 API、不写库）。
    api = _build_tushare_provider(args)

    for symbol in symbols:
        marker = _marker_key(start_date_s, end_date_s, symbol)
        if args.resume and marker in checkpoint:
            skipped += 1
            continue

        processed += 1

        if args.dry_run:
            print(
                f"dry-run: would backfill {symbol} "
                f"({processed}/{len(symbols)}) range={start_date_s}~{end_date_s}"
            )
            ok += 1
            if interval_sec > 0 and processed < len(symbols):
                time.sleep(interval_sec)
            continue

        try:
            frame = api.fetch_trade_status(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            # API 失败：不标记为成功，保留 checkpoint，记录失败并继续
            failed += 1
            failures.append(f"{symbol}:{type(exc).__name__}:{exc}")
            if interval_sec > 0:
                time.sleep(interval_sec)
            continue

        coverage_complete = True
        failed_components: list[str] = []
        if frame is not None:
            attrs_complete = frame.attrs.get("coverage_complete")
            if attrs_complete is None and "coverage_complete" in frame.columns:
                attrs_complete = bool(frame["coverage_complete"].fillna(False).all())
            if attrs_complete is not None:
                coverage_complete = bool(attrs_complete)
            failed_components = [
                str(component)
                for component in frame.attrs.get("failed_components", [])
            ]

        if frame is None or frame.empty:
            if not coverage_complete:
                failed += 1
                components = ",".join(failed_components) or "unknown"
                failures.append(f"{symbol}:partial_coverage:{components}")
            else:
                empty += 1
        else:
            try:
                warehouse.upsert_trade_status(symbol=symbol, frame=frame)
            except Exception as exc:
                failed += 1
                failures.append(f"{symbol}:upsert:{type(exc).__name__}:{exc}")
                if interval_sec > 0:
                    time.sleep(interval_sec)
                continue
            rows_stored += len(frame)
            if not coverage_complete:
                failed += 1
                components = ",".join(failed_components) or "unknown"
                failures.append(f"{symbol}:partial_coverage:{components}")
            else:
                _save_checkpoint(checkpoint_path, checkpoint, marker)
                ok += 1

        if interval_sec > 0 and processed < len(symbols):
            time.sleep(interval_sec)

    report = {
        "tool": "backfill_trade_status",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(Path(args.db_path).expanduser()),
        "start_date": start_date_s,
        "end_date": end_date_s,
        "symbols_requested": len(symbols) + skipped,
        "symbols_processed": processed,
        "symbols_skipped_resume": skipped,
        "succeeded": ok,
        "failed": failed,
        "empty": empty,
        "rows_stored": rows_stored,
        "failures": failures[:100],
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failed:
        return 1
    # 全部空响应也视为失败：空响应不代表回填成功，自动化不应误认为成功
    if ok == 0 and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
