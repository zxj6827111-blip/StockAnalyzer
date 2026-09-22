"""RAW execution delta 的**基线身份**（bootstrap marker）与符号集合摘要。

为什么需要这一层
----------------
Alpha V2 的执行侧必须是 raw（见 ``alpha_v2.dual_price_series``）：qfq 序列上的除权跳变
会被写成真实亏损。所以夜间链路要维护**第二份**物理独立的 delta：

```text
feature/qfq  : /app/artifacts/vendor_delta/market_delta.duckdb
execution/raw: /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
```

危险点不在"raw 库被写坏"，而在**它根本没被正确建起来**：

``import_vendor_zip_to_delta.py --incremental`` 对"目标库里还没有基线的 symbol"会走
``full_import_symbols`` 并用 ``--limit-days``（默认 400 **行**，不是自然日）补导。于是
一个**空路径**上的第一次生产增量，会在几分钟内"成功"造出一份只有默认浅深度的 raw
基线——它看起来是 raw、行数也像样，却覆盖不到候选模型要求的 source window
（2024-11-14 .. 2026-08-31）。这种"半基线"比缺库更难发现，因为没有任何一步报错。

本模块把"这份 raw 库是一份**经过覆盖认证的正规基线**"变成一条可校验的声明：

1. 建基线是**显式**动作（``scripts/alpha_v2_raw_delta_coverage.py --write-marker``），
   它只有在覆盖校验 PASS 时才肯落 marker；
2. 夜间增量**先验 marker** 再动手（``verify_bootstrap_marker``），不通过就 fail closed，
   绝不用增量偷偷初始化；
3. 夜间 readiness 发布前**再验一次**（release 级 fail-closed）：能发布的那份 raw 库必须
   就是被认证过的那份。

身份用**内容事实**而不是文件 SHA256
-----------------------------------
raw 库每晚都在长，对数百 MB 的 DuckDB 每晚重算一次全文件 SHA256 是纯开销。所以身份用
"内容事实"：行数 / 符号数 / 日期区间 / 符号集合摘要 / 源索引摘要。它们随每日增量**应当**
变化，因此它们不是"不变身份"，而是"建基线当刻的取证快照"——夜间校验只比对**不随增量变化**
的那几项（schema、口径、覆盖结论、库路径），取证快照如实留在 marker 里供审计复盘。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)
#: 执行侧唯一允许的价格口径（与 alpha_v2.dual_price_series.EXECUTION_PRICE_MODE_REQUIRED 同值；
#: 这里不 import 那个模块是为了让夜间链路（ops）不反向依赖研究包）。
RAW_DELTA_PRICE_MODE = "raw"
#: feature 侧生产口径（v3 readiness 里 feature delta 必须自述 qfq）。
FEATURE_DELTA_PRICE_MODE = "qfq"

ROLE_FEATURE = "feature"
ROLE_EXECUTION = "execution"

#: marker 的 schema 标识；换字段语义必须换它（否则旧 marker 会被当新证据读）。
RAW_BOOTSTRAP_MARKER_SCHEMA = "alpha_v2_raw_delta_bootstrap.v1"
RAW_BOOTSTRAP_MARKER_FILENAME = "raw_delta_bootstrap.json"

COVERAGE_STATUS_PASS = "PASS"
COVERAGE_STATUS_BLOCKED = "BLOCKED"

#: fail-closed 的原因码（updater 的 summary 与 readiness 的审计都用它，不要各写字符串）。
REASON_BASELINE_MISSING = "raw_delta_baseline_missing"
REASON_MARKER_UNREADABLE = "raw_delta_baseline_marker_unreadable"
REASON_MARKER_SCHEMA = "raw_delta_baseline_marker_schema_mismatch"
REASON_PRICE_MODE = "raw_delta_price_mode_invalid"
REASON_COVERAGE = "raw_delta_coverage_blocked"
REASON_DB_IDENTITY = "raw_delta_db_identity_mismatch"


class RawDeltaBaselineError(RuntimeError):
    """RAW 执行库不满足基线身份（缺失 / marker 不符 / 口径或覆盖不通过）。

    调用方一律 fail closed：``reason`` 直接进 summary / readiness 审计，不再二次判定。
    """

    def __init__(self, message: str, *, reason: str = REASON_BASELINE_MISSING) -> None:
        super().__init__(message)
        self.reason = str(reason)


# ---------------------------------------------------------------------------
# 符号集合摘要
# ---------------------------------------------------------------------------


def normalize_symbols(symbols: Iterable[object]) -> list[str]:
    """去空白、去重、排序——摘要与集合比较必须建立在同一套规范化之上。"""
    return sorted({str(item).strip() for item in symbols if str(item).strip()})


def symbol_set_hash(symbols: Iterable[object]) -> str:
    """符号集合的稳定摘要（排序后逐行拼接再 sha256）。

    用途是**成员锁步**而不是计数比对：``{A,B}`` 与 ``{A,C}`` 计数相同、摘要不同，
    正是"数量一样但成员不同"这一类静默故障的唯一可检出信号。
    """
    normalized = normalize_symbols(symbols)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """文件字节摘要（只用于**一次性**取证：源索引 JSON、覆盖报告等小文件）。"""
    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_iso_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Bootstrap marker
# ---------------------------------------------------------------------------


def bootstrap_marker_path(
    raw_db_path: str | Path,
    *,
    marker_path: str | Path | None = None,
) -> Path:
    """marker 路径：显式参数 > **raw 库同目录**。

    与库同目录是有意的：两者必须一起搬（同一个 artifacts 卷），分开放会让"库在、marker
    不在"变成一台机器上的持久故障。也**故意**只有一个覆盖入口（显式参数，由 CLI 与
    updater 透传）：marker 是"这份库是经认证的基线"的唯一凭据，多一个环境变量来源就
    多一处"我以为指向的是那个文件"的含糊。
    """
    if marker_path is not None and str(marker_path).strip():
        return Path(str(marker_path)).expanduser()
    return Path(str(raw_db_path)).expanduser().with_name(RAW_BOOTSTRAP_MARKER_FILENAME)


def build_bootstrap_marker(
    *,
    db_path: str | Path,
    coverage_report: dict[str, Any],
    source_index_path: str | Path,
    source_index_hash: str,
    source_index_latest_date: str,
    build_commit: str = "",
    source: str = "alpha_v2_raw_delta_coverage",
    created_at: str = "",
) -> dict[str, Any]:
    """由**已 PASS 的**覆盖报告派生 marker 载荷（不重算覆盖，不修饰结论）。

    ``coverage_status`` 直接取报告结论：BLOCKED 的报告进来会抛错而不是写下"看起来正常"
    的 marker。
    """
    status = str(coverage_report.get("coverage_status", "") or "").strip().upper()
    if status != COVERAGE_STATUS_PASS:
        raise RawDeltaBaselineError(
            "refusing to write a RAW bootstrap marker from a non-PASS coverage report: "
            f"coverage_status={status or 'missing'} "
            f"blockers={coverage_report.get('blockers', [])}",
            reason=REASON_COVERAGE,
        )
    raw_facts = dict(coverage_report.get("raw_db", {}) or {})
    window = dict(coverage_report.get("source_window", {}) or {})
    price_mode = dict(coverage_report.get("price_mode_check", {}) or {})
    symbols = dict(coverage_report.get("symbol_coverage", {}) or {})
    rows = dict(coverage_report.get("row_coverage", {}) or {})
    if str(price_mode.get("observed", "") or "").strip().lower() != RAW_DELTA_PRICE_MODE:
        raise RawDeltaBaselineError(
            "refusing to write a RAW bootstrap marker: price_mode_check.observed is not raw "
            f"({price_mode.get('observed')!r})",
            reason=REASON_PRICE_MODE,
        )
    # 口径**认证**（而不只是"行内自称 raw"）必须是 marker 的前置条件。写在函数里而不是
    # 只写在 CLI 里，是为了让将来任何新调用方也造不出"跳过认证"的 marker：
    # 生产信任的凭据只能由一次真的跑过 certify 的校验产出。
    if str(price_mode.get("expected", "") or "").strip().lower() != RAW_DELTA_PRICE_MODE:
        raise RawDeltaBaselineError(
            "refusing to write a RAW bootstrap marker: price_mode_check.expected is not raw "
            f"({price_mode.get('expected')!r})",
            reason=REASON_PRICE_MODE,
        )
    if price_mode.get("certified") is not True:
        raise RawDeltaBaselineError(
            "refusing to write a RAW bootstrap marker: price_mode_check.certified is not true "
            f"({price_mode.get('certified')!r})——未经 certify 的运行（例如 "
            "--skip-price-mode-certification）不得产出生产信任的 marker",
            reason=REASON_PRICE_MODE,
        )
    return {
        "schema": RAW_BOOTSTRAP_MARKER_SCHEMA,
        "created_at": created_at or datetime.now(UTC).isoformat(),
        "source": str(source),
        "price_series_mode": RAW_DELTA_PRICE_MODE,
        "db_path": str(Path(str(db_path)).expanduser()),
        # 内容事实身份（非文件 SHA256）：随每日增量变化，故只作取证快照。
        "db_content_identity": {
            "rows": raw_facts.get("rows"),
            "symbols_total": raw_facts.get("symbols_total"),
            "actual_min_date": raw_facts.get("min_date"),
            "actual_max_date": raw_facts.get("max_date"),
            "symbol_set_hash": symbols.get("symbol_set_hash_raw"),
        },
        "required_source_window": {
            "start": str(window.get("start", "") or ""),
            "end": str(window.get("end", "") or ""),
        },
        "actual_min_date": raw_facts.get("min_date"),
        "actual_max_date": raw_facts.get("max_date"),
        "symbols_expected": symbols.get("symbols_expected"),
        "symbols_covered": symbols.get("symbols_covered"),
        "rows": raw_facts.get("rows"),
        "price_mode_check": {
            "expected": RAW_DELTA_PRICE_MODE,
            "observed": str(price_mode.get("observed", "") or "").strip().lower(),
            "certified": bool(price_mode.get("certified", False)),
            "decision_rule": price_mode.get("decision_rule", ""),
            "evidence": price_mode.get("evidence", {}),
        },
        # feature 侧口径的取证副本（v3 readiness 会要求它是 qfq）。建基线时一并记下，
        # 是为了让"两侧口径都对"这个结论在上线前就可见，而不是留给第一晚去发现。
        "feature_price_mode_check": dict(coverage_report.get("feature_price_mode_check", {}) or {}),
        "coverage_status": COVERAGE_STATUS_PASS,
        "row_coverage": {
            "feature_rows_in_window": rows.get("feature_rows_in_window"),
            "raw_rows_in_window": rows.get("raw_rows_in_window"),
            "missing_in_raw": rows.get("missing_in_raw"),
            "extra_in_raw": rows.get("extra_in_raw"),
        },
        "source_index_path": str(source_index_path),
        "source_index_hash": str(source_index_hash),
        "source_index_latest_date": str(source_index_latest_date),
        "build_commit": str(build_commit or "").strip(),
    }


def write_bootstrap_marker(
    payload: dict[str, Any],
    *,
    raw_db_path: str | Path,
    marker_path: str | Path | None = None,
) -> Path:
    """原子写 marker（同目录 tmp + ``os.replace``），返回落盘路径。"""
    target = bootstrap_marker_path(raw_db_path, marker_path=marker_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def read_bootstrap_marker(
    *,
    raw_db_path: str | Path,
    marker_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """读 marker 载荷；缺失或不可解析返回 ``None``（判定交给调用方）。"""
    target = bootstrap_marker_path(raw_db_path, marker_path=marker_path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("RAW bootstrap marker is not valid JSON: %s", target)
        return None
    return payload if isinstance(payload, dict) else None


def verify_bootstrap_marker(
    *,
    raw_db_path: str | Path,
    marker_path: str | Path | None = None,
    check_db: bool = True,
) -> dict[str, Any]:
    """校验 RAW 库的基线身份；任何一项不成立都抛 :class:`RawDeltaBaselineError`。

    只校验**不随每日增量变化**的事实：schema、口径、覆盖结论、库路径；再加（``check_db``
    为真时）对库本身的三条单调不变量——行数/符号数不少于建基线时、行内口径仍是 raw。

    取证快照里的日期区间与符号摘要则**不比对**：它们随每日增量演变，拿它们当身份会把
    正常的推进判成漂移。
    """
    db_path = Path(str(raw_db_path)).expanduser()
    target = bootstrap_marker_path(raw_db_path, marker_path=marker_path)
    if not db_path.is_file():
        raise RawDeltaBaselineError(
            f"RAW delta 库不存在: {db_path}——生产增量不会用 --incremental 隐式初始化它，"
            "必须先用 coverage validator 显式建基线",
            reason=REASON_BASELINE_MISSING,
        )
    marker = read_bootstrap_marker(raw_db_path=raw_db_path, marker_path=marker_path)
    if marker is None:
        raise RawDeltaBaselineError(
            f"RAW delta bootstrap marker 缺失/不可读: {target}——拒绝在未经覆盖认证的库上跑生产增量",
            reason=REASON_MARKER_UNREADABLE,
        )
    schema = str(marker.get("schema", "") or "").strip()
    if schema != RAW_BOOTSTRAP_MARKER_SCHEMA:
        raise RawDeltaBaselineError(
            f"RAW bootstrap marker schema 不符: {schema or '(missing)'} "
            f"!= {RAW_BOOTSTRAP_MARKER_SCHEMA}",
            reason=REASON_MARKER_SCHEMA,
        )
    mode = str(marker.get("price_series_mode", "") or "").strip().lower()
    if mode != RAW_DELTA_PRICE_MODE:
        raise RawDeltaBaselineError(
            f"RAW bootstrap marker 声明的口径不是 raw: {mode or '(missing)'}",
            reason=REASON_PRICE_MODE,
        )
    check = marker.get("price_mode_check")
    check = dict(check) if isinstance(check, dict) else {}
    expected = str(check.get("expected", "") or "").strip().lower()
    observed = str(check.get("observed", "") or "").strip().lower()
    if expected != RAW_DELTA_PRICE_MODE or observed != RAW_DELTA_PRICE_MODE:
        raise RawDeltaBaselineError(
            "RAW bootstrap marker 的口径证据自相矛盾: "
            f"expected={expected or '(missing)'} observed={observed or '(missing)'}",
            reason=REASON_PRICE_MODE,
        )
    status = str(marker.get("coverage_status", "") or "").strip().upper()
    if status != COVERAGE_STATUS_PASS:
        raise RawDeltaBaselineError(
            f"RAW bootstrap marker 的覆盖结论不是 PASS: {status or '(missing)'}",
            reason=REASON_COVERAGE,
        )
    recorded_db = str(marker.get("db_path", "") or "").strip()
    if recorded_db and _same_path(recorded_db, db_path) is False:
        raise RawDeltaBaselineError(
            f"RAW bootstrap marker 记的库与当前目标不是同一个: {recorded_db} != {db_path}",
            reason=REASON_DB_IDENTITY,
        )
    window = marker.get("required_source_window")
    window = dict(window) if isinstance(window, dict) else {}
    start = _coerce_iso_date(window.get("start"))
    end = _coerce_iso_date(window.get("end"))
    if start is None or end is None or start > end:
        raise RawDeltaBaselineError(
            "RAW bootstrap marker 的 required_source_window 不可解析: "
            f"{window.get('start')!r}..{window.get('end')!r}",
            reason=REASON_MARKER_SCHEMA,
        )
    if check_db:
        verify_baseline_db_identity(marker=marker, db_path=db_path)
    return marker


def _same_path(left: str, right: Path) -> bool:
    """路径同一性比对：存在则用 resolve，不存在则退化成字符串规范化。"""
    try:
        return Path(left).expanduser().resolve() == right.resolve()
    except OSError:  # pragma: no cover - resolve 失败只在异常文件系统上
        return str(Path(left).expanduser()) == str(right)


def inspect_db_facts(db_path: str | Path) -> dict[str, Any]:
    """打开库读**事实**（只读）：表在不在、行数、符号数、日期区间、行内口径声明。

    这是"库还是那份库"的实测证据来源。全表聚合在数百万行上是一次索引扫描量级，
    夜间链路可以接受；不做任何写入。
    """
    import duckdb

    path = Path(str(db_path)).expanduser()
    facts: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not facts["exists"]:
        return facts
    with duckdb.connect(str(path), read_only=True) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'main' AND table_name = 'daily_bars'
            """
        ).fetchone()
        facts["daily_bars"] = bool(row and int(row[0] or 0) > 0)
        if not facts["daily_bars"]:
            return facts
        columns = {str(item[0]) for item in connection.execute("DESCRIBE daily_bars").fetchall()}
        facts["has_price_series_mode_column"] = "price_series_mode" in columns
        stats = connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date)
            FROM daily_bars
            """
        ).fetchone()
        facts["rows"] = int(stats[0] or 0) if stats else 0
        facts["symbols_total"] = int(stats[1] or 0) if stats else 0
        facts["min_date"] = str(stats[2]) if stats and stats[2] is not None else ""
        facts["max_date"] = str(stats[3]) if stats and stats[3] is not None else ""
        if facts["has_price_series_mode_column"]:
            declared = connection.execute(
                """
                SELECT COALESCE(TRIM(CAST(price_series_mode AS VARCHAR)), '') AS mode, COUNT(*)
                FROM daily_bars GROUP BY 1
                """
            ).fetchall()
            facts["declared_mode_histogram"] = {
                str(item[0]).strip().lower(): int(item[1] or 0) for item in declared
            }
        else:
            facts["declared_mode_histogram"] = {}
    return facts


def declared_mode_of(facts: dict[str, Any]) -> str:
    """事实块里的行内口径结论：唯一非空值 / ``mixed`` / ``unknown``。"""
    histogram = dict(facts.get("declared_mode_histogram", {}) or {})
    declared = {mode: count for mode, count in histogram.items() if mode and int(count) > 0}
    if not declared:
        return "unknown"
    return next(iter(declared)) if len(declared) == 1 else "mixed"


def verify_baseline_db_identity(
    *,
    marker: dict[str, Any],
    db_path: str | Path,
) -> dict[str, Any]:
    """marker 声明的基线与**当前库**是否还是同一份（实测，fail closed）。

    判据只有单调不变量，不用阈值：``daily_bars`` 在、行数与符号数**不少于**建基线时
    记录值、行内口径仍是 raw。三条一起覆盖了"库没了 / 被清空 / 被换成另一份"——
    最后一种（指向 qfq 库）由口径那条挡住。逐日增量只会让计数增长，所以正常推进
    不会碰到这些门。

    顺序有意如此：先证"还是同一份库"，再证"这份库是 raw"。反过来会让"库被清空"报成
    口径未知，把真正的故障类型（身份变了）藏在一个次要现象后面。
    """
    path = Path(str(db_path)).expanduser()
    facts = inspect_db_facts(path)
    if not facts.get("exists"):
        raise RawDeltaBaselineError(
            f"RAW delta 库不存在: {path}",
            reason=REASON_BASELINE_MISSING,
        )
    if not facts.get("daily_bars"):
        raise RawDeltaBaselineError(
            f"RAW delta 库没有 daily_bars 表（不是一份基线）: {path}",
            reason=REASON_DB_IDENTITY,
        )
    identity = marker.get("db_content_identity")
    identity = dict(identity) if isinstance(identity, dict) else {}
    baseline_rows = _coerce_int(identity.get("rows"))
    baseline_symbols = _coerce_int(identity.get("symbols_total"))
    current_rows = _coerce_int(facts.get("rows")) or 0
    current_symbols = _coerce_int(facts.get("symbols_total")) or 0
    if baseline_rows is not None and current_rows < baseline_rows:
        raise RawDeltaBaselineError(
            "RAW delta 库的行数少于建基线时记录值，疑似被清空/替换："
            f"{current_rows} < {baseline_rows}（库={path}）",
            reason=REASON_DB_IDENTITY,
        )
    if baseline_symbols is not None and current_symbols < baseline_symbols:
        raise RawDeltaBaselineError(
            "RAW delta 库的符号数少于建基线时记录值，疑似被清空/替换："
            f"{current_symbols} < {baseline_symbols}（库={path}）",
            reason=REASON_DB_IDENTITY,
        )
    observed_mode = declared_mode_of(facts)
    if observed_mode != RAW_DELTA_PRICE_MODE:
        raise RawDeltaBaselineError(
            f"RAW delta 库当前的行内口径是 {observed_mode or 'unknown'}，"
            f"不是 {RAW_DELTA_PRICE_MODE}（库={path}）——拒绝把它当执行侧序列推进",
            reason=REASON_PRICE_MODE,
        )
    return {
        "ok": True,
        "path": str(path),
        "rows": current_rows,
        "symbols_total": current_symbols,
        "declared_price_series_mode": observed_mode,
        "baseline_rows": baseline_rows,
        "baseline_symbols_total": baseline_symbols,
    }


def _coerce_int(value: object) -> int | None:
    """宽松取整（marker 是 JSON，数字可能是 int / float / 字符串）；取不到返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


__all__ = [
    "COVERAGE_STATUS_BLOCKED",
    "COVERAGE_STATUS_PASS",
    "FEATURE_DELTA_PRICE_MODE",
    "RAW_BOOTSTRAP_MARKER_FILENAME",
    "RAW_BOOTSTRAP_MARKER_SCHEMA",
    "RAW_DELTA_PRICE_MODE",
    "REASON_BASELINE_MISSING",
    "REASON_COVERAGE",
    "REASON_DB_IDENTITY",
    "REASON_MARKER_SCHEMA",
    "REASON_MARKER_UNREADABLE",
    "REASON_PRICE_MODE",
    "ROLE_EXECUTION",
    "ROLE_FEATURE",
    "RawDeltaBaselineError",
    "bootstrap_marker_path",
    "build_bootstrap_marker",
    "declared_mode_of",
    "inspect_db_facts",
    "normalize_symbols",
    "read_bootstrap_marker",
    "sha256_file",
    "symbol_set_hash",
    "verify_baseline_db_identity",
    "verify_bootstrap_marker",
    "write_bootstrap_marker",
]
