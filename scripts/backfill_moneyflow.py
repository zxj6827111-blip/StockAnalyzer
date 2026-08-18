"""Recoverable, rate-limited, idempotent full-market moneyflow backfill.

Writes ONLY the ``moneyflow`` table of the delta DuckDB through
``MarketWarehouse.upsert_moneyflow`` (per-stock moneyflow from Tushare via
``TushareProvider.fetch_moneyflow``). It does NOT touch ZIP archives, daily
bars, index files or named volumes.

Guarantees
----------
- Resumable: a JSON checkpoint records each symbol whose fetch AND upsert
  succeeded; ``--resume`` skips those, so an interrupted run continues where
  it stopped. Checkpoint keys include start/end date and the symbol, so
  expanding the history range invalidates stale entries instead of silently
  skipping symbols. Empty responses are NOT recorded, so a later wider
  history re-fetches them.
- Rate-limited: sleeps ``--request-interval-sec`` between API calls,
  INCLUDING after failures, so API throttling cannot turn into a fast
  failure cascade.
- Idempotent: ``upsert_moneyflow`` merges by (symbol, trade_date), so
  re-runs never duplicate and API failures never wipe prior trusted rows.
- dry-run: only reports what would be processed; never fetches, never
  writes DB, never writes checkpoint, never creates directories.

Usage:
    python scripts/backfill_moneyflow.py --db-path /data/warehouse.duckdb
        [--start-date 2021-01-01] [--end-date 2026-08-01]
        [--symbols 000001,600519] [--symbols-file symbols.txt] [--limit 3000]
        [--resume] [--checkpoint-path artifacts/moneyflow_backfill_checkpoint.json]
        [--dry-run] [--request-interval-sec 0.6] [--max-attempts 3]

Exit code: 0 only when every symbol succeeded (or dry-run). 1 when any
symbol failed. 2 when arguments are invalid or no Tushare token is set.
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


def _load_checkpoint(path: Path) -> dict[str, str]:
    """读取 checkpoint JSON，返回 marker -> 完成时间 的映射。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checkpoint(path: Path, checkpoint: dict[str, str], marker: str) -> None:
    """写入 checkpoint：只有 fetch + upsert 都成功后才调用。"""
    checkpoint[marker] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _marker_key(start_date: str, end_date: str, symbol: str) -> str:
    """构造 checkpoint key：包含日期范围与 symbol，扩展范围时自动失效旧条目。"""
    return f"{start_date}|{end_date}|{symbol}"


def _as_date(value: str, *, default: date) -> date:
    """Parse YYYY-MM-DD, preserving the script's fallback behavior."""
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def _resolve_date_range(
    start_value: str,
    end_value: str,
    *,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve one concrete range for API calls, reports, and checkpoints."""
    reference_date = today or date.today()
    end_text = str(end_value or "").strip()
    start_text = str(start_value or "").strip()
    resolved_end = _as_date(end_text, default=reference_date) if end_text else reference_date
    default_start = resolved_end - timedelta(days=365)
    resolved_start = _as_date(start_text, default=default_start) if start_text else default_start
    return resolved_start, resolved_end


def _parse_symbols(args: argparse.Namespace) -> list[str]:
    """从 --symbols / --symbols-file 解析去重后的 symbol 列表。"""
    raw: list[str] = []
    if args.symbols.strip():
        raw.extend(part.strip() for part in args.symbols.split(",") if part.strip())
    if args.symbols_file.strip():
        file_path = Path(args.symbols_file.strip()).expanduser()
        if file_path.exists():
            for line in file_path.read_text(encoding="utf-8").splitlines():
                token = line.strip()
                if token and not token.startswith("#"):
                    raw.append(token)
        else:
            print(f"symbols file not found: {file_path}", file=sys.stderr)
    # 去重且保持稳定顺序
    seen: set[str] = set()
    symbols: list[str] = []
    for item in raw:
        if item and item not in seen:
            seen.add(item)
            symbols.append(item)
    return symbols


def _resolve_coverage(warehouse: object, *, start_date: str, end_date: str) -> dict[str, object]:
    """从 DB 只读查询 moneyflow 覆盖度与重复数（dry-run 不调用）。"""
    table = "moneyflow"
    result: dict[str, object] = {
        "symbol_coverage": 0,
        "min_trade_date": "",
        "max_trade_date": "",
        "duplicate_count": 0,
    }
    try:
        table_exists_fn = getattr(warehouse, "_table_exists", None)
        if callable(table_exists_fn) and not table_exists_fn(table):
            return result
        connect_fn = getattr(warehouse, "_connect_readonly", None)
        if not callable(connect_fn):
            return result
        with connect_fn() as connection:
            # 覆盖的 symbol 数
            try:
                symbol_count = int(
                    connection.execute(
                        f"SELECT COUNT(DISTINCT symbol) FROM {table}"
                    ).fetchone()[0]
                )
            except Exception:
                symbol_count = 0
            # 日期范围（若指定了日期范围则按范围过滤）
            try:
                date_filter = ""
                params: list[object] = []
                if start_date:
                    date_filter = " WHERE trade_date >= ?"
                    params.append(start_date)
                if end_date:
                    conjunction = " AND " if date_filter else " WHERE "
                    date_filter += f"{conjunction}trade_date <= ?"
                    params.append(end_date)
                row = connection.execute(
                    f"SELECT MIN(trade_date), MAX(trade_date) FROM {table}{date_filter}",
                    params,
                ).fetchone()
                min_date = row[0] if row and row[0] is not None else ""
                max_date = row[1] if row and row[1] is not None else ""
                min_trade_date = (
                    str(min_date)[:10] if min_date else ""
                )
                max_trade_date = (
                    str(max_date)[:10] if max_date else ""
                )
            except Exception:
                min_trade_date = ""
                max_trade_date = ""
            # (symbol, trade_date) 重复行数（正常应始终为 0）
            try:
                dup_row = connection.execute(
                    f"SELECT COUNT(*) FROM ("
                    f" SELECT symbol, trade_date FROM {table} "
                    f" GROUP BY symbol, trade_date HAVING COUNT(*) > 1"
                    f") AS d"
                ).fetchone()
                dup_count = int(dup_row[0]) if dup_row else 0
            except Exception:
                dup_count = 0
        result["symbol_coverage"] = symbol_count
        result["min_trade_date"] = min_trade_date
        result["max_trade_date"] = max_trade_date
        result["duplicate_count"] = dup_count
    except Exception:
        # 查询失败不影响回填结论，仅留空报告
        pass
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        help="Delta DuckDB path (required)",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Start date YYYY-MM-DD (empty = provider default ~1 year back)",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="End date YYYY-MM-DD (empty = today)",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbol list (empty = none unless --symbols-file)",
    )
    parser.add_argument(
        "--symbols-file",
        default="",
        help="Text file with one symbol per line (empty = none)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of symbols processed this run (0 = unlimited)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip symbols already recorded in the checkpoint",
    )
    parser.add_argument(
        "--checkpoint-path",
        default=str(ROOT / "artifacts" / "moneyflow_backfill_checkpoint.json"),
        help="Checkpoint file path for resume",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would be processed; never fetch, write DB or checkpoint",
    )
    parser.add_argument(
        "--request-interval-sec",
        type=float,
        default=0.6,
        help="Sleep seconds between API calls (including after failures)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Max retry attempts for Tushare API calls",
    )
    args = parser.parse_args(argv)

    # 参数校验
    if args.max_attempts < 1:
        print("--max-attempts must be >= 1", file=sys.stderr)
        return 2
    if args.request_interval_sec < 0.0:
        print("--request-interval-sec must be >= 0", file=sys.stderr)
        return 2

    # Token 解析（双 env 回退，与 trade_status 回填模式一致）
    token = str(os.environ.get("TUSHARE_TOKEN", "") or "").strip() or str(
        os.environ.get("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN", "") or ""
    ).strip()
    if not token and not args.dry_run:
        print(
            "missing Tushare token: set TUSHARE_TOKEN or "
            "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN",
            file=sys.stderr,
        )
        return 2

    # 延迟导入：确保 sys.path 已注入 src 目录（加 noqa 避开 E402）
    from stock_analyzer.data.market_warehouse import MarketWarehouse  # noqa: E402
    from stock_analyzer.data.tushare_provider import TushareProvider  # noqa: E402

    # 构建 TushareProvider（与 trade_status 回填模式一致）
    api = TushareProvider(
        token=token,
        retry_delay_sec=max(0.0, float(args.request_interval_sec)),
        min_request_interval_sec=max(0.0, float(args.request_interval_sec)),
        max_attempts=max(1, int(args.max_attempts)),
        price_series_mode="qfq",
    )

    # dry-run 不创建 DB：仅在校验通过后再构造 warehouse（dry-run 仍需实例化以备查询，
    # 但 upsert_moneyflow 在 dry-run 路径下不会被调用）
    warehouse = MarketWarehouse(
        db_path=args.db_path,
        package_root=str(Path(args.db_path).parent / "package"),
        package_writes_enabled=False,
    )

    # 解析 symbol 列表
    symbols = _parse_symbols(args)
    if not symbols:
        print(
            "no symbols provided: use --symbols or --symbols-file",
            file=sys.stderr,
        )
        return 2

    limit = max(0, int(args.limit))
    if limit:
        symbols = symbols[:limit]

    start_date_obj, end_date_obj = _resolve_date_range(
        args.start_date,
        args.end_date,
    )
    if start_date_obj > end_date_obj:
        print("--start-date must be <= --end-date", file=sys.stderr)
        return 2
    start_date_str = start_date_obj.isoformat()
    end_date_str = end_date_obj.isoformat()

    # Checkpoint 加载
    checkpoint_path = Path(args.checkpoint_path).expanduser()
    checkpoint = _load_checkpoint(checkpoint_path)

    interval_sec = max(0.0, float(args.request_interval_sec))

    ok = 0
    failed = 0
    skipped = 0
    empty = 0
    rows_fetched = 0
    rows_stored = 0
    processed = 0
    failures: list[str] = []

    for symbol in symbols:
        marker = _marker_key(start_date_str, end_date_str, symbol)
        if args.resume and marker in checkpoint:
            skipped += 1
            continue
        processed += 1
        if args.dry_run:
            print(f"dry-run: would backfill {symbol} ({processed}/{len(symbols)})")
            ok += 1
            if interval_sec > 0 and processed < len(symbols):
                time.sleep(interval_sec)
            continue
        # 真正回填：fetch + upsert 都成功后才写 checkpoint
        try:
            frame = api.fetch_moneyflow(
                symbol=symbol,
                start_date=start_date_obj,
                end_date=end_date_obj,
            )
        except Exception as exc:
            # API 失败：不写 checkpoint，不标记成功
            failed += 1
            failures.append(f"{symbol}:{type(exc).__name__}:{exc}")
            if interval_sec > 0:
                time.sleep(interval_sec)
            continue

        if frame is None or frame.empty:
            empty += 1
        else:
            rows_fetched += len(frame)
            try:
                stored = warehouse.upsert_moneyflow(symbol=symbol, frame=frame)
            except Exception as exc:
                # upsert 失败：同样不写 checkpoint
                failed += 1
                failures.append(f"{symbol}:{type(exc).__name__}:{exc}")
                if interval_sec > 0:
                    time.sleep(interval_sec)
                continue
            rows_stored += stored
            # 只有 fetch + upsert 都成功后才记录 checkpoint
            _save_checkpoint(checkpoint_path, checkpoint, marker)
            ok += 1

        if interval_sec > 0 and processed < len(symbols):
            time.sleep(interval_sec)

    # 覆盖度/重复数查询：dry-run 不查 DB（不修改、不创建），非 dry-run 只读查询
    if args.dry_run:
        coverage = {
            "symbol_coverage": 0,
            "min_trade_date": "",
            "max_trade_date": "",
            "duplicate_count": 0,
        }
    else:
        coverage = _resolve_coverage(
            warehouse, start_date=start_date_str, end_date=end_date_str
        )

    summary = {
        "tool": "backfill_moneyflow",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(args.db_path),
        "start_date": start_date_str,
        "end_date": end_date_str,
        "requested": len(symbols),
        "processed": processed,
        "succeeded": ok,
        "failed": failed,
        "skipped": skipped,
        "empty": empty,
        "rows_fetched": rows_fetched,
        "rows_stored": rows_stored,
        "symbol_coverage": coverage["symbol_coverage"],
        "min_trade_date": coverage["min_trade_date"],
        "max_trade_date": coverage["max_trade_date"],
        "duplicate_count": coverage["duplicate_count"],
        "failure_reasons": failures[:100],
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if failed:
        return 1
    # 全部空响应也视为失败：空响应不代表回填成功，自动化不应误认为成功
    if ok == 0 and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
