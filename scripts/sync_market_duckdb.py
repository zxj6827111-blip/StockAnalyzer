"""每日增量：把 tushare 日线与 QQ 分钟（每日 zip）同步进生产消费库。

背景：回测与生产共用的消费库 ``market.duckdb``（daily_bars + intraday_summary_1m/5m）
有两条上游供给线——tushare 日线（updater 每日更新 ZIP 与 delta 库）与 QQ 自建分钟
（/vol1/1000/股票数据/output/minute_raw/ 每日 zip）——但两条线都不会自动写入
market.duckdb（2026-07/08 的断档即由此而来）。本脚本作为两者与消费库之间的
**自动缺口同步器**：

- ``--daily``：行级对比 delta 库与 market.duckdb 的 daily_bars，缺失行按
  「ZIP raw 价格 + delta 快照/量列」组装补入（口径与存量 legacy raw 一致）。
- ``--minute``：对比 minute_raw 目录 zip 日期与 ``market.duckdb``、活动
  ``market_delta.duckdb`` 的 1m/5m 汇总覆盖；任一目标库缺日期时，逐 zip 用
  ``summarize_minute_bars``（与读取端同一聚合代码）计算，并幂等 upsert 到两个库。

两个模式均幂等（按 symbol+date DELETE+INSERT）且**自动找缺口**：任何一天漏跑，
后续运行会自动补齐；``--since`` 限制扫描起点防止历史意外差异产生巨量缺口。

运行方式（NAS，工作日晚间 cron）：
  docker exec stock-analyzer-api python3 /app/scripts/sync_market_duckdb.py --daily
  docker exec stock-analyzer-api python3 /app/scripts/sync_market_duckdb.py --minute
  （QQ CSV 无 amount 字段，分钟侧以典型价×成交量合成近似——与 2026-08-30 的
  手工回填口径一致；该值不是供应商原始成交额。）
"""

from __future__ import annotations

import argparse
import io
import re
import zipfile
from datetime import date as date_type
from pathlib import Path

import duckdb
import pandas as pd

from stock_analyzer.data.intraday_summary import summarize_minute_bars
from stock_analyzer.data.market_warehouse import MarketWarehouse

DELTA_DB = "/app/artifacts/vendor_delta/market_delta.duckdb"
MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
QQ_ZIP_ROOT = Path("/data/qq_minute_raw")

_DAILY_SNAPSHOT_COLS = [
    "volume",
    "turnover",
    "float_market_cap",
    "suspended",
    "is_st",
    "is_delisting_risk",
    "board",
]
_QQ_SYMBOL_RE = re.compile(r"([A-Za-z]{1,3})#?(\d{6})")
_ZIP_DATE_RE = re.compile(r"minute_1m_(\d{8})\.zip$")
# 分钟汇总的目标表集合：缺口检测与写入必须对同一组表——
# "写 2 张、只查 1 张"会让部分缺失静默留存（2026-09-16 修）。
MINUTE_SUMMARY_TABLES = ("intraday_summary_1m", "intraday_summary_5m")


def _missing_daily_dates(since: date_type) -> list[date_type]:
    """delta 库有而 market.duckdb 缺失的 (symbol, date) 日线缺口所涉日期。"""
    con = duckdb.connect(MARKET_DB, read_only=True)
    try:
        con.execute(f"ATTACH '{DELTA_DB}' AS delta_catalog (READ_ONLY)")
        rows = con.execute(
            """
            SELECT DISTINCT CAST(d.date AS DATE) AS date
            FROM delta_catalog.daily_bars AS d
            WHERE d.date >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM daily_bars AS m
                  WHERE m.symbol = d.symbol AND m.date = d.date
              )
            ORDER BY date
            """,
            [since],
        ).fetchall()
    finally:
        con.close()
    return [row[0] for row in rows]


def _sync_daily(
    *,
    zip_dir: Path,
    since: date_type,
) -> int:
    """同步日线缺口；返回写入行数。按缺口日期的年份自动选择年度 ZIP 包。"""
    missing_dates = _missing_daily_dates(since)
    if not missing_dates:
        print("[daily] no missing rows", flush=True)
        return 0, []
    print(f"[daily] missing dates: {[d.isoformat() for d in missing_dates]}", flush=True)

    delta_con = duckdb.connect(DELTA_DB, read_only=True)
    delta_frame = delta_con.execute(
        f"""
        SELECT date AS date, symbol, {", ".join(_DAILY_SNAPSHOT_COLS)}
        FROM daily_bars
        WHERE date >= ?
        """,
        [since],
    ).fetch_df()
    delta_con.close()
    delta_frame["symbol"] = delta_frame["symbol"].astype(str)
    delta_frame["date"] = pd.to_datetime(delta_frame["date"], errors="coerce").dt.date

    symbol_re = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
    price_rows: list[dict[str, object]] = []
    missing_set = set(missing_dates)
    for year in sorted({d.year for d in missing_dates}):
        zip_path = zip_dir / f"{year}.zip"
        if not zip_path.exists():
            print(f"[daily] {zip_path.name}: not found, skip year {year}", flush=True)
            continue
        year_dates = {d for d in missing_set if d.year == year}
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                if "__MACOSX" in member or not member.lower().endswith(".csv"):
                    continue
                match = symbol_re.match(Path(member).stem)
                if match is None:
                    continue
                symbol = match.group(1)
                try:
                    frame = pd.read_csv(io.BytesIO(archive.read(member)))
                except Exception:  # noqa: BLE001 - 单票损坏不阻塞
                    continue
                frame.columns = [str(c).strip().lower() for c in frame.columns]
                if not {"datetime", "open", "high", "low", "close"}.issubset(frame.columns):
                    continue
                frame["date"] = pd.to_datetime(frame["datetime"], errors="coerce").dt.date
                window = frame[frame["date"].isin(year_dates)]
                for _, row in window.iterrows():
                    values = {col: row[col] for col in ("open", "high", "low", "close")}
                    if any(pd.isna(v) for v in values.values()):
                        continue
                    price_rows.append(
                        {
                            "symbol": symbol,
                            "date": row["date"],
                            **{col: float(v) for col, v in values.items()},
                        }
                    )
    price_frame = pd.DataFrame(price_rows)
    if price_frame.empty:
        print("[daily] ZIP has no rows for missing dates", flush=True)
        return 0, []

    merged = price_frame.merge(delta_frame, on=["symbol", "date"], how="inner")
    if merged.empty:
        print("[daily] no overlapping rows (zip ⋈ delta)", flush=True)
        return 0, []

    warehouse = MarketWarehouse(
        db_path=MARKET_DB,
        package_root=str(Path(MARKET_DB).parent / "package"),
        package_writes_enabled=False,
    )
    warehouse.ensure_schema()
    con = duckdb.connect(MARKET_DB)
    table_cols = [c[0] for c in con.execute("DESCRIBE daily_bars").fetchall()]
    payload = pd.DataFrame(
        {col: (merged[col] if col in merged.columns else None) for col in table_cols}
    )
    payload["date"] = pd.to_datetime(payload["date"], errors="coerce").dt.date
    payload = payload.dropna(subset=["symbol", "date"])
    con.register("daily_sync_stage", payload)
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute(
            """
            DELETE FROM daily_bars
            USING daily_sync_stage AS stage
            WHERE daily_bars.symbol = stage.symbol AND daily_bars.date = stage.date
            """
        )
        con.execute(
            f"""
            INSERT INTO daily_bars ({", ".join(table_cols)})
            SELECT {", ".join(table_cols)}
            FROM daily_sync_stage
            ORDER BY symbol, date
            """
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("daily_sync_stage")
        con.close()
    print(f"[daily] upserted rows: {len(payload)}", flush=True)
    synced_dates = sorted({row for row in payload["date"].tolist() if row is not None})
    return len(payload), synced_dates


def _missing_minute_dates(zip_root: Path, since: date_type) -> list[date_type]:
    """返回任一目标库缺少 QQ 源股票日摘要的日期。

    不能只看日期是否有任意行：同一天可能只写入了部分股票。这里以每个
    1m/5m ZIP 中的股票集合分别核对两个目标库，缺一行就重放整日并 upsert
    到两个库；缺少或损坏的 5m ZIP 也会保持为缺口，最终让同步以非零退出。
    """
    zip_dates: set[date_type] = set()
    if zip_root.exists():
        for entry in zip_root.iterdir():
            match = _ZIP_DATE_RE.search(entry.name)
            if match is None:
                continue
            parsed = date_type(
                int(match.group(1)[:4]),
                int(match.group(1)[4:6]),
                int(match.group(1)[6:8]),
            )
            if parsed >= since:
                zip_dates.add(parsed)
    source_symbols: dict[date_type, dict[str, set[str]]] = {}
    missing_source_dates: set[date_type] = set()
    table_by_interval = dict(zip(("1m", "5m"), MINUTE_SUMMARY_TABLES, strict=True))
    for trade_date in zip_dates:
        source_symbols[trade_date] = {}
        ymd = trade_date.strftime("%Y%m%d")
        for interval in ("1m", "5m"):
            zip_path = zip_root / f"minute_{interval}_{ymd}.zip"
            if not zip_path.is_file():
                missing_source_dates.add(trade_date)
                continue
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    symbols = {
                        symbol
                        for member in archive.namelist()
                        if member.lower().endswith(".csv")
                        and (symbol := _qq_symbol(member))
                    }
            except Exception:  # noqa: BLE001 - 同步阶段会报告损坏源并以非零退出
                missing_source_dates.add(trade_date)
                continue
            if not symbols:
                missing_source_dates.add(trade_date)
                continue
            source_symbols[trade_date][table_by_interval[interval]] = symbols

    incomplete_dates = set(missing_source_dates)
    target_db_paths = tuple(dict.fromkeys((str(MARKET_DB), str(DELTA_DB))))
    for db_path in target_db_paths:
        con = duckdb.connect(db_path, read_only=True)
        try:
            for trade_date, symbols_by_table in source_symbols.items():
                for table, expected_symbols in symbols_by_table.items():
                    existing_symbols = {
                        row[0]
                        for row in con.execute(
                            f"SELECT DISTINCT symbol FROM {table} WHERE date = ?",
                            [trade_date],
                        ).fetchall()
                    }
                    if not expected_symbols.issubset(existing_symbols):
                        incomplete_dates.add(trade_date)
        finally:
            con.close()
    return sorted(incomplete_dates)


def _qq_symbol(name: str) -> str:
    match = _QQ_SYMBOL_RE.search(Path(name).stem)
    return match.group(2) if match else ""


def _load_symbol_frame(raw: bytes) -> pd.DataFrame | None:
    frame = pd.read_csv(io.BytesIO(raw))
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    required = {"date", "time", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return None
    frame["datetime"] = pd.to_datetime(
        frame["date"].astype(str) + " " + frame["time"].astype(str),
        errors="coerce",
    )
    frame = frame.dropna(subset=["datetime"]).set_index("datetime")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return None
    # QQ 的 CSV 不含 amount；按既有项目口径近似，不能当作供应商原始成交额。
    typical = (frame["open"] + frame["high"] + frame["low"] + frame["close"]) / 4.0
    frame["amount"] = typical * frame["volume"].fillna(0.0)
    return frame[["open", "high", "low", "close", "volume", "amount"]]


def _sync_minute(zip_root: Path, since: date_type) -> int:
    """同步分钟缺口；返回写入行数。

    源目录不存在时**拒绝空转**：2026-08-29~09-15 的故障形态正是容器里没有
    ``/data/qq_minute_raw`` 挂载——``zip_dates`` 恒为空 → 缺口恒为空 → 每天
    打印 ``no missing dates``，而 ``intraday_summary_1m/5m`` 停在 8/28 十八天。
    "没有缺口"与"看不到源"必须能区分，否则同类静默故障会再来一次。
    """
    if not zip_root.is_dir():
        raise SystemExit(
            f"[minute] 源目录不存在: {zip_root} —— 拒绝空转（检查容器挂载 "
            "/data/qq_minute_raw、或改用 --qq-zip-root；挂载由 "
            "docker-compose.vendor-overlay.yml 的 SA_QQ_MINUTE_RAW_HOST_ROOT 提供）"
        )
    missing = _missing_minute_dates(zip_root, since)
    if not missing:
        print(f"[minute] no missing dates (source={zip_root})", flush=True)
        return 0
    print(f"[minute] missing dates: {[d.isoformat() for d in missing]}", flush=True)
    print(
        "[minute] QQ source has no amount field; amount uses typical-price × volume estimate",
        flush=True,
    )

    target_db_paths = tuple(dict.fromkeys((str(MARKET_DB), str(DELTA_DB))))
    warehouses = [
        (
            db_path,
            MarketWarehouse(
                db_path=db_path,
                package_root=str(Path(db_path).parent / "package"),
                package_writes_enabled=False,
            ),
        )
        for db_path in target_db_paths
    ]
    total = 0
    failures: list[str] = []
    for target in missing:
        ymd = target.strftime("%Y%m%d")
        batches: dict[str, pd.DataFrame] = {}
        for interval in ("1m", "5m"):
            zip_path = zip_root / f"minute_{interval}_{ymd}.zip"
            if not zip_path.exists():
                failure = f"{zip_path.name}: source zip not found"
                print(f"[minute][error] {failure}", flush=True)
                failures.append(failure)
                continue
            rows: list[pd.DataFrame] = []
            source_symbols: set[str] = set()
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    for member in archive.namelist():
                        if not member.lower().endswith(".csv"):
                            continue
                        symbol = _qq_symbol(member)
                        if not symbol:
                            continue
                        source_symbols.add(symbol)
                        try:
                            frame = _load_symbol_frame(archive.read(member))
                        except Exception:  # noqa: BLE001 - 单票损坏不阻塞其他股票
                            continue
                        if frame is None or frame.empty:
                            continue
                        summary = summarize_minute_bars(frame, interval=interval)
                        if summary.empty:
                            continue
                        summary = summary.reset_index()
                        summary = summary[summary["date"] == pd.Timestamp(target)]
                        if summary.empty:
                            continue
                        summary.insert(0, "symbol", symbol)
                        rows.append(summary)
            except Exception as exc:  # noqa: BLE001 - 继续尝试其他目标库和日期
                failure = f"{zip_path.name}: {type(exc).__name__}: {exc}"
                print(f"[minute][error] {failure}", flush=True)
                failures.append(failure)
                continue
            if not rows:
                failure = f"{zip_path.name}: no valid summary rows"
                print(f"[minute][error] {failure}", flush=True)
                failures.append(failure)
                continue
            batch = pd.concat(rows, axis=0, ignore_index=True, sort=False)
            batch["date"] = pd.to_datetime(batch["date"], errors="coerce").dt.date
            missing_symbols = source_symbols - set(batch["symbol"].astype(str))
            if missing_symbols:
                sample = ", ".join(sorted(missing_symbols)[:10])
                failure = (
                    f"{zip_path.name}: {len(missing_symbols)} source symbol(s) "
                    f"have no valid summary (sample: {sample})"
                )
                print(f"[minute][error] {failure}", flush=True)
                failures.append(failure)
            batches[interval] = batch

        for interval, batch in batches.items():
            for db_path, warehouse in warehouses:
                try:
                    result = warehouse.upsert_intraday_summaries(
                        interval=interval, frame=batch
                    )
                    rows_written = int(result.get("rows", 0))
                    total += rows_written
                    print(
                        f"[minute] {target} {interval}: {db_path} upserted {rows_written} rows",
                        flush=True,
                    )
                    if rows_written == 0:
                        failure = f"{target} {interval} -> {db_path}: upsert wrote zero rows"
                        print(f"[minute][error] {failure}", flush=True)
                        failures.append(failure)
                except Exception as exc:  # noqa: BLE001 - 另一库仍可成功，失败库下次重试
                    failure = (
                        f"{target} {interval} -> {db_path}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    print(f"[minute][error] {failure}", flush=True)
                    failures.append(failure)
        print(f"[minute] {target}: done", flush=True)

    if failures:
        raise SystemExit(
            f"[minute] incomplete synchronization: {len(failures)} failure(s); "
            "the remaining gap will be retried on the next run"
        )
    print(f"[minute] upserted rows: {total}", flush=True)
    return total


def _enrich_synced_dates(dates: list[date_type]) -> None:
    """日更富集钩子（2026-09-05 修复，见 docs/week5_datagate_root_cause_20260905.md）。

    sync 裸行 upsert 后立刻补：① PIT 财务物化（本地 as-of join，无外部 API）；
    ② 背景 bulk（tushare 按日接口）。否则每日新行会持续产出财务/背景全空的
    行——week6 prewarm 覆盖率归零、data_gate 长期 blocked 的根因即此。
    每步独立容错：富集失败不阻塞同步主流程（缺口由次日重跑或手工回填补齐）。
    """

    from stock_analyzer.data.tushare_provider import (
        _HttpTushareProApi,
        _resolve_tushare_token,
    )
    from stock_analyzer.data.warehouse_enrichment import (
        enrich_background_for_dates,
        enrich_daily_financial_pit,
    )

    con = duckdb.connect(MARKET_DB)
    try:
        try:
            rows = enrich_daily_financial_pit(con, dates)
            print(f"[daily][enrich] financial PIT rows: {rows}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[daily][enrich] financial failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
        token = _resolve_tushare_token()
        if not token:
            print("[daily][enrich] no tushare token; background skipped", flush=True)
            return
        http = _HttpTushareProApi(token=token, timeout_sec=30.0)
        updated = enrich_background_for_dates(
            con, dates, http, request_interval_sec=0.35
        )
        print(f"[daily][enrich] background dates updated: {updated}", flush=True)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily", action="store_true", help="同步 tushare 日线缺口")
    parser.add_argument("--minute", action="store_true", help="同步 QQ 分钟缺口")
    parser.add_argument(
        "--since",
        default="2026-07-31",
        help="缺口扫描起点（防止历史意外差异产生巨量缺口）",
    )
    parser.add_argument(
        "--daily-zip-dir",
        default="/data/vendor_history/全A日K",
        help="日线 raw 价格来源 ZIP 目录（按缺口日期年份自动选 {year}.zip）",
    )
    parser.add_argument(
        "--qq-zip-root",
        default=str(QQ_ZIP_ROOT),
        help="QQ 每日分钟 zip 目录",
    )
    args = parser.parse_args()
    if not args.daily and not args.minute:
        parser.error("choose --daily and/or --minute")
    since = date_type.fromisoformat(args.since)

    if args.daily:
        _, synced_dates = _sync_daily(zip_dir=Path(args.daily_zip_dir), since=since)
        if synced_dates:
            _enrich_synced_dates(synced_dates)
    if args.minute:
        _sync_minute(zip_root=Path(args.qq_zip_root), since=since)


if __name__ == "__main__":
    main()
