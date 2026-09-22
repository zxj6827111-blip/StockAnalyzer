"""RAW execution delta 覆盖校验器（只读）+ 基线 bootstrap marker 生成器。

用途
----
回答**唯一**那个问题：``/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb`` 是不是
一份"能支撑候选模型 source window"的正规 raw 基线？

```text
PASS    → 可以写 bootstrap marker，之后生产 updater 才允许对它跑增量
BLOCKED → 不许写 marker；生产中该库一律 fail closed（raw_delta_baseline_missing）
```

为什么不能只看 ``--limit-days``
-------------------------------
``--limit-days`` 是**每 symbol 的行数**上限，不是自然日。一个"看起来够大"的整数既不能
证明窗口起点被覆盖，也不能证明符号集合完整。所以判据全部落在**实测事实**上：

```text
8.1 DB        : 存在 / 可读 / daily_bars 在 / (symbol,date) 无重复
8.2 Price mode: raw（复用 alpha_v2 的 certify_price_mode，不另写判据）
8.3 Window    : actual_min_date <= source_window_start 且 actual_max_date >= source_window_end
8.4 Symbols   : required = feature 库在同一窗口内的符号集合；缺一个都 BLOCKED
8.5 Rows      :（symbol,date）逐对比较，区分"停牌/无交易"与"数据缺口"
```

8.5 的做法值得说明：不引入交易日历，也不假设"每天都该有 bar"。raw 与 qfq 来自同一批
ZIP，**行集合理应一致**（口径只改价格数值，不改哪些行存在）。于是：

```text
feature 有、raw 没有 → 数据缺口（真问题，BLOCKED）
两边都没有           → 停牌 / 未上市 / 无交易（正常，不报）
raw 有、feature 没有 → 正常：qfq 侧因子缺失的 symbol 会被跳过，raw 不需要因子
```

用法（容器内 / NAS）：

    python scripts/alpha_v2_raw_delta_coverage.py \
        --raw-db  /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb \
        --feature-db /app/artifacts/vendor_delta/market_delta.duckdb \
        --index-path /app/artifacts/vendor_overlay/daily_index.json \
        --source-window-start 2024-11-14 --source-window-end 2026-08-31

加 ``--write-marker`` 才会落 ``raw_delta_bootstrap.json``（先决条件：覆盖 PASS、口径 raw）。

退出码：0 = PASS，1 = BLOCKED，2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.ops.raw_delta_baseline import (  # noqa: E402
    COVERAGE_STATUS_BLOCKED,
    COVERAGE_STATUS_PASS,
    FEATURE_DELTA_PRICE_MODE,
    RAW_BOOTSTRAP_MARKER_FILENAME,
    RAW_BOOTSTRAP_MARKER_SCHEMA,
    RAW_DELTA_PRICE_MODE,
    RawDeltaBaselineError,
    build_bootstrap_marker,
    normalize_symbols,
    sha256_file,
    symbol_set_hash,
    verify_bootstrap_marker,
    write_bootstrap_marker,
)

#: 生产候选模型的 source window（decision window 2025-06-02..2026-08-31 向前 warmup 200 自然日）。
DEFAULT_SOURCE_WINDOW_START = "2024-11-14"
DEFAULT_SOURCE_WINDOW_END = "2026-08-31"

#: certify 探针的取样窗口长度（自然日）。判据本身来自行内声明；探针只做一致性佐证，
#: 因此对全窗口（~2 年 × 5500 只）取样没有必要，也无法在 NAS 上稳定复跑。
DEFAULT_PRICE_MODE_SAMPLE_DAYS = 120

EXIT_PASS = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2


def _coerce_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            from datetime import datetime as _dt

            return _dt.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _sql_literal(value: str) -> str:
    """DuckDB 的 ATTACH/字面量里不能参数化路径，单引号按 SQL 规则翻倍。"""
    return "'" + str(value).replace("'", "''") + "'"


def _same_path(left: Path, right: Path) -> bool:
    """两条路径是否指向同一份文件（解析失败时退化成字符串比较）。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - resolve 失败只在异常文件系统上
        return str(left) == str(right)


def _table_exists(connection: Any, qualified: str) -> bool:
    row = connection.execute(
        """
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_name = 'daily_bars'
          AND table_schema NOT IN ('information_schema', 'pg_catalog')
        """
    ).fetchone()
    if not row or int(row[0] or 0) <= 0:
        return False
    try:
        connection.execute(f"SELECT 1 FROM {qualified} LIMIT 0")
    except Exception:
        return False
    return True


def _columns(connection: Any, qualified: str) -> set[str]:
    rows = connection.execute(f"DESCRIBE {qualified}").fetchall()
    return {str(row[0]) for row in rows}


def _inspect_db(
    connection: Any,
    *,
    qualified: str,
    db_path: Path,
    label: str,
    window_start: date,
    window_end: date,
) -> tuple[dict[str, Any], list[str]]:
    """8.1 DB 结构性事实（存在 / 可读 / daily_bars / 重复行）。"""
    blockers: list[str] = []
    facts: dict[str, Any] = {"path": str(db_path), "role": label}
    if not db_path.is_file():
        blockers.append(f"{label}_db_missing:{db_path}")
        facts["exists"] = False
        return facts, blockers
    facts["exists"] = True
    if not _table_exists(connection, qualified):
        blockers.append(f"{label}_daily_bars_missing:{db_path}")
        facts["daily_bars"] = False
        return facts, blockers
    facts["daily_bars"] = True
    columns = _columns(connection, qualified)
    facts["has_price_series_mode_column"] = "price_series_mode" in columns
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT symbol) AS symbols,
               MIN(date) AS min_date,
               MAX(date) AS max_date,
               COUNT(*) - COUNT(DISTINCT (symbol, date)) AS duplicates
        FROM {qualified}
        """
    ).fetchone()
    facts["rows"] = int(row[0] or 0)
    facts["symbols_total"] = int(row[1] or 0)
    facts["min_date"] = str(row[2]) if row[2] is not None else ""
    facts["max_date"] = str(row[3]) if row[3] is not None else ""
    facts["duplicate_rows"] = int(row[4] or 0)
    if facts["duplicate_rows"] > 0:
        blockers.append(f"{label}_duplicate_symbol_date:{facts['duplicate_rows']}")
    in_window = connection.execute(
        f"""
        SELECT COUNT(*) FROM {qualified}
        WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
        """,
        [window_start.isoformat(), window_end.isoformat()],
    ).fetchone()
    facts["rows_in_window"] = int(in_window[0] or 0) if in_window else 0
    return facts, blockers


def _declared_mode_histogram(
    connection: Any,
    *,
    qualified: str,
    window_start: date,
    window_end: date,
) -> dict[str, int]:
    """整个窗口内的行内口径声明分布（空字符串 = 未声明）。"""
    if "price_series_mode" not in _columns(connection, qualified):
        return {}
    rows = connection.execute(
        f"""
        SELECT COALESCE(TRIM(CAST(price_series_mode AS VARCHAR)), '') AS mode, COUNT(*)
        FROM {qualified}
        WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
        GROUP BY 1
        """,
        [window_start.isoformat(), window_end.isoformat()],
    ).fetchall()
    return {str(row[0]).strip().lower(): int(row[1] or 0) for row in rows}


def _observed_mode(histogram: dict[str, int]) -> str:
    if not histogram:
        return "unknown"
    declared = {mode: count for mode, count in histogram.items() if mode and count > 0}
    if not declared:
        return "unknown"
    return next(iter(declared)) if len(declared) == 1 else "mixed"


def _target_symbol_set(
    connection: Any,
    *,
    qualified: str,
    window_start: date,
    window_end: date,
) -> list[str]:
    rows = connection.execute(
        f"""
        SELECT DISTINCT symbol FROM {qualified}
        WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
        """,
        [window_start.isoformat(), window_end.isoformat()],
    ).fetchall()
    return normalize_symbols(row[0] for row in rows)


def _exclusive_rows(
    connection: Any,
    *,
    left: str,
    right: str,
    window_start: date,
    window_end: date,
    limit: int,
) -> tuple[int, list[str]]:
    """``left`` 有而 ``right`` 没有的 (symbol,date) 行数与样例（引擎内完成比较）。"""
    total_row = connection.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT symbol, date FROM {left}
            WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
            EXCEPT
            SELECT symbol, date FROM {right}
            WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
        )
        """,
        [
            window_start.isoformat(),
            window_end.isoformat(),
            window_start.isoformat(),
            window_end.isoformat(),
        ],
    ).fetchone()
    total = int(total_row[0] or 0) if total_row else 0
    examples: list[str] = []
    if total and limit > 0:
        rows = connection.execute(
            f"""
            SELECT symbol, date FROM (
                SELECT symbol, date FROM {left}
                WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
                EXCEPT
                SELECT symbol, date FROM {right}
                WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
            )
            ORDER BY symbol, date
            LIMIT {int(limit)}
            """,
            [
                window_start.isoformat(),
                window_end.isoformat(),
                window_start.isoformat(),
                window_end.isoformat(),
            ],
        ).fetchall()
        examples = [f"{row[0]}@{row[1]}" for row in rows]
    return total, examples


def _index_summary(index_path: Path, target_window_end: date) -> tuple[dict[str, Any], list[str]]:
    """索引侧事实：latest_date 上界、非空心符号集合。"""
    blockers: list[str] = []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(index_path), "readable": False}, [f"index_unreadable:{exc}"]
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, dict) or not symbols:
        return {"path": str(index_path), "readable": True}, ["index_has_no_symbols"]
    latest_dates: list[date] = []
    hollow: list[str] = []
    for key, item in symbols.items():
        if not isinstance(item, dict):
            continue
        parsed = _coerce_date(item.get("latest_date"))
        if parsed is None:
            continue
        entries = item.get("entries")
        if isinstance(entries, list) and not entries:
            hollow.append(str(key))
            continue
        latest_dates.append(parsed)
    index_latest = max(latest_dates) if latest_dates else None
    summary = {
        "path": str(index_path),
        "readable": True,
        "symbols_total": len(symbols),
        "symbols_with_data": len(latest_dates),
        "hollow_symbols": len(hollow),
        "latest_date": index_latest.isoformat() if index_latest else "",
    }
    if index_latest is None:
        blockers.append("index_has_no_latest_date")
    elif index_latest < target_window_end:
        # 索引自己都到不了窗口末端，raw 基线不可能覆盖该窗口。
        blockers.append(
            f"index_latest_before_source_window_end:{index_latest.isoformat()}"
            f"<{target_window_end.isoformat()}"
        )
    return summary, blockers


def _certify_price_mode(
    *,
    db_path: Path,
    window_end: date,
    sample_days: int,
    declared_by_config: str,
) -> dict[str, Any]:
    """复用 Alpha V2 的价格口径认证（探针 + 行内声明），不另写判据。"""
    from stock_analyzer.alpha_v2.research.panel import load_daily_panel

    sample_start = max(
        window_end - timedelta(days=max(1, int(sample_days))),
        date(1990, 1, 1),
    )
    panel = load_daily_panel(
        market_db=db_path,
        window_start=sample_start,
        window_end=window_end,
        warmup_days=30,
        source="alpha_v2_raw_delta_coverage",
    )
    certification = panel.certify_price_mode(declared_by_config=declared_by_config)
    return {
        "mode": str(getattr(certification, "mode", "") or ""),
        "certified": bool(getattr(certification, "certified", False)),
        "source": str(getattr(certification, "source", "") or ""),
        "sample_window": [sample_start.isoformat(), window_end.isoformat()],
        "evidence": dict(getattr(certification, "evidence", {}) or {}),
    }


def evaluate_coverage(
    *,
    raw_db: str | Path,
    feature_db: str | Path,
    index_path: str | Path,
    source_window_start: str | date,
    source_window_end: str | date,
    max_examples: int = 20,
    max_missing_rows: int = 0,
    price_mode_sample_days: int = DEFAULT_PRICE_MODE_SAMPLE_DAYS,
    skip_price_mode_certification: bool = False,
) -> dict[str, Any]:
    """执行 8.1–8.5 全部检查，返回可归档的覆盖报告（``coverage_status`` = PASS/BLOCKED）。"""
    start = _coerce_date(source_window_start)
    end = _coerce_date(source_window_end)
    if start is None or end is None:
        raise ValueError(
            "source_window_start / source_window_end must be YYYY-MM-DD: "
            f"{source_window_start!r}..{source_window_end!r}"
        )
    if start > end:
        raise ValueError(f"source window is inverted: {start.isoformat()} > {end.isoformat()}")
    raw_path = Path(str(raw_db)).expanduser()
    feature_path = Path(str(feature_db)).expanduser()
    index_file = Path(str(index_path)).expanduser()

    blockers: list[str] = []
    report: dict[str, Any] = {
        "script": "alpha_v2_raw_delta_coverage",
        "source_window": {"start": start.isoformat(), "end": end.isoformat()},
        "raw_db_path": str(raw_path),
        "feature_db_path": str(feature_path),
    }
    if _same_path(raw_path, feature_path):
        # 两个角色指向同一份文件时，"raw 覆盖 feature"这类比较会退化成恒真——整份
        # 校验看起来通过，却什么都没证明。
        blockers.append(f"raw_and_feature_db_paths_identical:{raw_path}")
    if not raw_path.is_file():
        blockers.append(f"raw_db_missing:{raw_path}")
    if not feature_path.is_file():
        blockers.append(f"feature_db_missing:{feature_path}")
    if blockers:
        report["blockers"] = blockers
        report["coverage_status"] = COVERAGE_STATUS_BLOCKED
        return report

    import duckdb

    connection = duckdb.connect(str(raw_path), read_only=True)
    try:
        connection.execute(f"ATTACH {_sql_literal(str(feature_path))} AS feature_db (READ_ONLY)")
        raw_facts, raw_blockers = _inspect_db(
            connection,
            qualified="main.daily_bars",
            db_path=raw_path,
            label="raw",
            window_start=start,
            window_end=end,
        )
        feature_facts, feature_blockers = _inspect_db(
            connection,
            qualified="feature_db.main.daily_bars",
            db_path=feature_path,
            label="feature",
            window_start=start,
            window_end=end,
        )
        report["raw_db"] = raw_facts
        report["feature_db"] = feature_facts
        blockers.extend(raw_blockers)
        blockers.extend(feature_blockers)

        # 8.3 Window：全库区间必须罩住 source window。
        raw_min = _coerce_date(raw_facts.get("min_date"))
        raw_max = _coerce_date(raw_facts.get("max_date"))
        if raw_min is None or raw_min > start:
            blockers.append(
                f"raw_window_start_not_covered:{raw_facts.get('min_date') or 'missing'}"
                f">{start.isoformat()}"
            )
        if raw_max is None or raw_max < end:
            blockers.append(
                f"raw_window_end_not_covered:{raw_facts.get('max_date') or 'missing'}"
                f"<{end.isoformat()}"
            )

        # 8.2 Price mode
        histogram = _declared_mode_histogram(
            connection,
            qualified="main.daily_bars",
            window_start=start,
            window_end=end,
        )
        observed = _observed_mode(histogram)
        price_mode: dict[str, Any] = {
            "expected": RAW_DELTA_PRICE_MODE,
            "observed": observed,
            "declared_mode_histogram": histogram,
            "certified": False,
        }
        if observed != RAW_DELTA_PRICE_MODE:
            blockers.append(f"raw_price_mode_not_raw:{observed or 'unknown'}")
        if not skip_price_mode_certification:
            certification = _certify_price_mode(
                db_path=raw_path,
                window_end=end,
                sample_days=price_mode_sample_days,
                declared_by_config=RAW_DELTA_PRICE_MODE,
            )
            price_mode["certification"] = certification
            price_mode["certified"] = bool(certification.get("certified"))
            price_mode["decision_rule"] = certification.get("evidence", {}).get("decision_rule", "")
            certified_mode = str(certification.get("mode", "") or "").strip().lower()
            if certified_mode != RAW_DELTA_PRICE_MODE:
                blockers.append(
                    f"raw_price_mode_certification_not_raw:{certified_mode or 'unknown'}"
                )
            elif certified_mode != observed:
                blockers.append(
                    f"raw_price_mode_declaration_conflicts_with_probe:{observed}!={certified_mode}"
                )
        report["price_mode_check"] = price_mode

        # feature 侧口径**取证**（不是判据）：v3 readiness 会要求 feature delta 自述
        # qfq，所以建基线这一步顺手把 feature 库的口径分布也记下来——上线前一条命令
        # 就能同时看到两侧口径，不必等到第一晚 release 才撞门。
        feature_histogram = _declared_mode_histogram(
            connection,
            qualified="feature_db.main.daily_bars",
            window_start=start,
            window_end=end,
        )
        feature_observed = _observed_mode(feature_histogram)
        feature_price_mode = {
            "expected": FEATURE_DELTA_PRICE_MODE,
            "observed": feature_observed,
            "declared_mode_histogram": feature_histogram,
            "matches_expected": feature_observed == FEATURE_DELTA_PRICE_MODE,
        }
        report["feature_price_mode_check"] = feature_price_mode
        if feature_observed != FEATURE_DELTA_PRICE_MODE:
            # 不 BLOCKED：feature 侧口径不属于本校验器的判定范围（§8.2 只管 raw 侧），
            # 但必须显式记成警告——否则它会在第一晚 readiness 上才以 fail closed 形式出现。
            report.setdefault("warnings", []).append(
                f"feature_db_price_mode_not_{FEATURE_DELTA_PRICE_MODE}:"
                f"{feature_observed or 'unknown'}"
            )

        # 8.4 Symbol coverage：required = feature 库在同一窗口内的符号集合。
        expected_symbols = _target_symbol_set(
            connection,
            qualified="feature_db.main.daily_bars",
            window_start=start,
            window_end=end,
        )
        raw_symbols = _target_symbol_set(
            connection,
            qualified="main.daily_bars",
            window_start=start,
            window_end=end,
        )
        expected_set, raw_set = set(expected_symbols), set(raw_symbols)
        missing = sorted(expected_set - raw_set)
        extra = sorted(raw_set - expected_set)
        symbol_coverage = {
            "symbols_expected": len(expected_symbols),
            "symbols_covered": len(expected_symbols) - len(missing),
            "symbols_raw_total": len(raw_symbols),
            "missing_symbols": len(missing),
            "missing_examples": missing[: max(0, int(max_examples))],
            "extra_symbols": len(extra),
            "extra_examples": extra[: max(0, int(max_examples))],
            "symbol_set_hash_expected": symbol_set_hash(expected_symbols),
            "symbol_set_hash_raw": symbol_set_hash(raw_symbols),
        }
        if missing:
            blockers.append(f"raw_missing_symbols:{len(missing)}")
        report["symbol_coverage"] = symbol_coverage

        # 8.5 Row coverage：逐 (symbol,date) 比较，区分停牌与缺口。
        missing_rows, missing_examples = _exclusive_rows(
            connection,
            left="feature_db.main.daily_bars",
            right="main.daily_bars",
            window_start=start,
            window_end=end,
            limit=max_examples,
        )
        extra_rows, extra_examples = _exclusive_rows(
            connection,
            left="main.daily_bars",
            right="feature_db.main.daily_bars",
            window_start=start,
            window_end=end,
            limit=max_examples,
        )
        row_coverage = {
            "feature_rows_in_window": feature_facts.get("rows_in_window"),
            "raw_rows_in_window": raw_facts.get("rows_in_window"),
            "missing_in_raw": missing_rows,
            "missing_examples": missing_examples,
            "extra_in_raw": extra_rows,
            "extra_examples": extra_examples,
            "max_missing_rows": int(max_missing_rows),
        }
        if missing_rows > int(max_missing_rows):
            blockers.append(f"raw_missing_rows:{missing_rows}>{int(max_missing_rows)}")
        report["row_coverage"] = row_coverage
    finally:
        connection.close()

    index_summary, index_blockers = _index_summary(index_file, end)
    report["source_index"] = index_summary
    blockers.extend(index_blockers)
    if index_file.is_file():
        report["source_index_hash"] = sha256_file(index_file)

    report["blockers"] = blockers
    report["coverage_status"] = COVERAGE_STATUS_BLOCKED if blockers else COVERAGE_STATUS_PASS
    return report


def _resolve_build_commit(explicit: str) -> str:
    for key in ("SA__BUILD_COMMIT", "BUILD_COMMIT", "GIT_COMMIT"):
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    for candidate in (ROOT / ".build_commit", Path("/app/.build_commit")):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return str(explicit or "").strip()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-db", default="", help="execution/raw delta DuckDB path")
    parser.add_argument("--feature-db", default="", help="feature/qfq delta DuckDB path")
    parser.add_argument("--index-path", default="", help="daily_index.json path")
    parser.add_argument(
        "--source-window-start",
        default=DEFAULT_SOURCE_WINDOW_START,
        help=f"source window start (default {DEFAULT_SOURCE_WINDOW_START})",
    )
    parser.add_argument(
        "--source-window-end",
        default=DEFAULT_SOURCE_WINDOW_END,
        help=f"source window end (default {DEFAULT_SOURCE_WINDOW_END})",
    )
    parser.add_argument("--max-examples", type=int, default=20)
    parser.add_argument(
        "--max-missing-rows",
        type=int,
        default=0,
        help="Tolerated (symbol,date) rows present in feature but missing in raw (default 0)",
    )
    parser.add_argument(
        "--price-mode-sample-days",
        type=int,
        default=DEFAULT_PRICE_MODE_SAMPLE_DAYS,
        help="certify 探针取样窗口长度（判据来自行内声明，探针只做一致性佐证）",
    )
    parser.add_argument(
        "--skip-price-mode-certification",
        action="store_true",
        help="只做 SQL 级口径声明检查（离线/异构环境排查用）",
    )
    parser.add_argument("--build-commit", default="")
    parser.add_argument("--json-out", default="", help="Write the report JSON to this path")
    parser.add_argument(
        "--write-marker",
        action="store_true",
        help="覆盖 PASS 时写 raw_delta_bootstrap.json（否则不写）",
    )
    parser.add_argument("--marker-path", default="", help="显式 marker 路径")
    parser.add_argument(
        "--verify-marker",
        action="store_true",
        help="只校验既有 marker 的基线身份（不重算覆盖）",
    )
    args = parser.parse_args(argv)

    # 生产信任的 marker 只能由**跑过口径认证**的那次校验产出。跳过认证是只读诊断手段
    # （离线排查、异构环境），拿它写 marker 等于把"未经认证的 raw"变成一条生产凭据。
    # 两道门：这里挡住 CLI，build_bootstrap_marker 里再挡一次（防将来别的调用方）。
    if args.write_marker and args.skip_price_mode_certification:
        print(
            "--write-marker requires price-mode certification: "
            "--skip-price-mode-certification is a read-only diagnostic and never produces a "
            "production-trust bootstrap marker",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.verify_marker:
        if not args.raw_db.strip():
            print("--verify-marker requires --raw-db", file=sys.stderr)
            return EXIT_USAGE
        try:
            marker = verify_bootstrap_marker(
                raw_db_path=args.raw_db,
                marker_path=args.marker_path or None,
            )
        except RawDeltaBaselineError as exc:
            print(
                json.dumps(
                    {
                        "script": "alpha_v2_raw_delta_coverage",
                        "mode": "verify_marker",
                        "coverage_status": COVERAGE_STATUS_BLOCKED,
                        "reason": exc.reason,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_BLOCKED
        print(
            json.dumps(
                {
                    "script": "alpha_v2_raw_delta_coverage",
                    "mode": "verify_marker",
                    "coverage_status": COVERAGE_STATUS_PASS,
                    "marker": marker,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_PASS

    missing_args = [
        name
        for name, value in (
            ("--raw-db", args.raw_db),
            ("--feature-db", args.feature_db),
            ("--index-path", args.index_path),
        )
        if not str(value).strip()
    ]
    if missing_args:
        print(f"missing required args: {', '.join(missing_args)}", file=sys.stderr)
        return EXIT_USAGE

    try:
        report = evaluate_coverage(
            raw_db=args.raw_db,
            feature_db=args.feature_db,
            index_path=args.index_path,
            source_window_start=args.source_window_start,
            source_window_end=args.source_window_end,
            max_examples=args.max_examples,
            max_missing_rows=args.max_missing_rows,
            price_mode_sample_days=args.price_mode_sample_days,
            skip_price_mode_certification=bool(args.skip_price_mode_certification),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "script": "alpha_v2_raw_delta_coverage",
                    "coverage_status": COVERAGE_STATUS_BLOCKED,
                    "error": f"{type(exc).__name__}:{exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_BLOCKED

    marker_written = ""
    if args.write_marker and report.get("coverage_status") == COVERAGE_STATUS_PASS:
        try:
            payload = build_bootstrap_marker(
                db_path=args.raw_db,
                coverage_report=report,
                source_index_path=args.index_path,
                source_index_hash=str(report.get("source_index_hash", "")),
                source_index_latest_date=str(
                    (report.get("source_index") or {}).get("latest_date", "")
                ),
                build_commit=_resolve_build_commit(args.build_commit),
            )
            marker_written = str(
                write_bootstrap_marker(
                    payload,
                    raw_db_path=args.raw_db,
                    marker_path=args.marker_path or None,
                )
            )
        except RawDeltaBaselineError as exc:
            report.setdefault("blockers", []).append(f"marker_write_refused:{exc.reason}")
            report["coverage_status"] = COVERAGE_STATUS_BLOCKED
            report["marker_error"] = str(exc)
    elif args.write_marker:
        report["marker_refused"] = "coverage_status_is_not_PASS"

    report["marker_written"] = marker_written
    if args.json_out.strip():
        out = Path(args.json_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_PASS if report.get("coverage_status") == COVERAGE_STATUS_PASS else EXIT_BLOCKED


__all__ = [
    "DEFAULT_SOURCE_WINDOW_END",
    "DEFAULT_SOURCE_WINDOW_START",
    "RAW_BOOTSTRAP_MARKER_FILENAME",
    "RAW_BOOTSTRAP_MARKER_SCHEMA",
    "EXIT_BLOCKED",
    "EXIT_PASS",
    "EXIT_USAGE",
    "evaluate_coverage",
]


if __name__ == "__main__":
    raise SystemExit(_main())
