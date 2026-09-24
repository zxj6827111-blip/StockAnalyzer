"""Nightly readiness gate (PLAN Section 4).

The NAS ``stock_updater.sh`` must write one atomic JSON file
``artifacts/runtime/nightly_data_ready.json`` after the daily K + index +
delta steps all succeed.  The evolution scheduler then treats missing or
date-mismatched readiness as a hard scheduler failure
(``_scheduler_ran=true, _scheduler_success=false,
_scheduler_detail=nightly_data_not_ready``).

The file is consumed exactly once by a successful evolution/week5/final-
selector/watchlist-sync chain; on success it is atomically renamed to
``nightly_data_ready.consumed.json``.  On failure it is kept so the
scheduler backs off and retries.

The implementation deliberately avoids importing service internals so the
updater, the scheduler and tests can all call the same helpers.

Schema
------
``nightly_data_ready.json`` example (legacy single-delta, ``schema_version`` 2) ::

    {
        "schema_version": 2,
        "target_trade_date": "2026-08-19",
        "daily":   {"ok": true, "latest_trade_date": "2026-08-19"},
        "index":   {"ok": true, "symbols_on_target_date": 5541},
        "delta":   {"ok": true, "symbols_on_target_date": 5541},
        "created_at": "2026-08-19T19:48:12+08:00",
        "updater_commit": "abc1234",
        "source": "stock_updater.sh"
    }

Production dual-delta (``schema_version`` 3, written whenever the updater is given
``--sync-vendor-delta-raw``) adds the execution role and the membership lock-step ::

    {
        "schema_version": 3,
        "target_trade_date": "2026-08-19",
        "daily":  {"ok": true},
        "index":  {"ok": true, "symbol_set_hash": "..."},
        "delta":  {"ok": true, "role": "feature",   "price_series_mode": "qfq",
                   "symbol_set_hash": "..."},
        "execution_delta": {"ok": true, "role": "execution", "price_series_mode": "raw",
                            "symbol_set_hash": "..."},
        "symbol_membership": {
            "symbols_expected": 5541, "symbols_feature": 5541, "symbols_execution": 5541,
            "symbol_set_hash_expected": "...", "symbol_set_hash_feature": "...",
            "symbol_set_hash_execution": "...",
            "missing_feature": [], "missing_execution": [], "feature_not_in_execution": []
        }
    }

为什么 v3 不只比**数量**：``{A,B}`` 与 ``{A,C}`` 计数相同、成员不同。旧口径只看
``symbols_on_target_date`` 的计数，这类"数量对得上、成员对不上"的故障会静默放行。
v3 用符号集合摘要（排序后 sha256）把成员锁死。

v3 的三方关系是**包含链** ``index_expected ⊆ feature ⊆ execution``，不是三方全等：

- ``index_expected`` 是"当天应该有的票"（去掉 entries 为空的新股占位）；
- feature 缺一只 → 那天少一个决策样本，必须拦；
- execution 缺 feature 有的 → label 算不出来，必须拦；
- **execution 多出来的不算错**：raw 侧不需要复权因子，所以 qfq 侧因因子缺失被跳过的
  symbol 在 raw 侧照样有行。要求三方全等会把这条正常路径判成故障，每晚误杀。

⚠️ 但**这条容忍只覆盖"成员集合"这一个维度**，它不等于"两份库逐键一致"。
``raw 有 bar + 该票因子也取得到 + qfq 却没有这一行`` 是另一件事：那不是"raw 多出来"，
是**派生链自己把行弄丢了**（2026-07-17..07-30 实测丢了 295 个 (symbol, date)）。
它由独立的 :data:`READINESS_REASON_QFQ_PARITY` 判据拦住（``qfq_parity`` 区块，
见 :func:`_qfq_parity_for_readiness`），**阻塞发布**；而 ``factor_missing_for_raw``
（因子根本没有）继续走上面的包含链语义、只记不拦。两者是正交的两个维度，
不得互相替代，也不得合并成一个"extra execution"计数。

Only the fields inspected by the gate are ``schema_version``,
``target_trade_date``, ``daily``/``index``/``delta`` (+ ``execution_delta`` on v3)
and ``created_at``.  ``target_trade_date`` is the latest daily index date,
not the shell calendar date.

Readiness 自己开库验证，不信 updater 自述
------------------------------------------
``write_nightly_readiness`` 不接受"``execution_delta_ok=true``"这类结论入参：两个 delta
库都由本模块**只读打开**，逐项核对最新交易日、目标日成员集合、行内价格口径。updater 的
自述只进 summary 供人看，release 判定完全来自这里的实测。

Consumption
-----------
``consume_nightly_readiness`` is called by the scheduler after a
successful full scan.  It renames ``nightly_data_ready.json`` to
``nightly_data_ready.consumed.json`` atomically (``os.replace``) and
returns the payload it consumed.  A missing file before consumption is
not an error; the caller decides the fate of the schedule.

Location
--------
The host actual location is the named volume source of
``/app/artifacts`` from ``docker-compose.runtime.yml``.  At runtime the
container sees it as ``/app/artifacts/runtime/nightly_data_ready.json``.
The helper ``nightly_readiness_paths`` resolves the candidate locations
so local tests using ``artifacts/runtime`` keep working.

Single authoritative path
-------------------------
``authoritative_readiness_path()`` is the single write target.  Legacy
mirrors under ``src/artifacts/runtime`` are never written; they are only
read as fallback for backwards-compat when the authoritative file is
absent, and ``consume`` drains all mirrors.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from stock_analyzer.ops.raw_delta_baseline import (
    FEATURE_DELTA_PRICE_MODE,
    RAW_DELTA_PRICE_MODE,
    ROLE_EXECUTION,
    ROLE_FEATURE,
    RawDeltaBaselineError,
    normalize_symbols,
    symbol_set_hash,
    verify_bootstrap_marker,
)

READINESS_FILENAME = "nightly_data_ready.json"
CONSUMED_FILENAME = "nightly_data_ready.consumed.json"
#: 单 delta（feature/qfq）写入版本；没有 execution 库时的默认值，历史文件也仍是它。
READINESS_SCHEMA_VERSION = 2
#: 双 delta（feature/qfq + execution/raw）写入版本。只有显式传入 execution 库时才写。
READINESS_SCHEMA_VERSION_DUAL = 3
#: 读侧接受的版本集合。v1 及未知版本一律不 ready（fail closed）。
SUPPORTED_READINESS_SCHEMA_VERSIONS: tuple[int, ...] = (
    READINESS_SCHEMA_VERSION,
    READINESS_SCHEMA_VERSION_DUAL,
)
#: QFQ 对账失败专用原因码。不复用 ``nightly_data_not_ready``、也不复用
#: ``nightly_dual_delta_not_ready``：这三件事的处置完全不同——
#: "今晚数据没好"重试即可；"没有执行侧证据"要等 unified updater；
#: 而"两份库对同一件事给了不同答案"重试永远不会变好，必须**修数据**。
#: 调度器与 Alpha 的 waiting/missing 台账靠这个码区分。
READINESS_REASON_QFQ_PARITY = "nightly_qfq_parity_failed"
#: 夜间对账回看的 session 数。**故意不是全历史**：历史 295 个缺口由
#: ``docs/alpha_v2/p3_3_data_remediation/P3_3_REPAIR_MANIFEST.json`` 负责，
#: 每晚扫十年 bar 只是把无谓成本焊进关键路径。
QFQ_PARITY_LOOKBACK_SESSIONS = 30

#: ``require_dual_delta=True`` 且 readiness 不满足双 delta 时的稳定原因码。
#: 单独一个码而不是复用 ``nightly_data_not_ready``：调度器要靠它区分"今天数据没好"
#: （重试即可）与"今天的 release 里根本没有执行侧证据"（重试也不会变，必须等到
#: unified updater 写出 v3）。Alpha 的 waiting/missing 台账直接记这一条。
READINESS_REASON_DUAL_DELTA = "nightly_dual_delta_not_ready"

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReadinessGate:
    """Result of :func:`check_nightly_readiness`."""

    ready: bool
    reason: str
    payload: dict[str, Any]
    expected_trade_date: str

    def scheduler_triple(self) -> tuple[bool, bool, str]:
        """Return (ran, success, detail) for the scheduler contract."""
        if self.ready:
            return True, True, "ok"
        return True, False, self.reason  # ran=true, success=false


def _coerce_date(value: object) -> date | None:
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


def _same_path(left: object, right: object) -> bool:
    """两个库路径是否指向同一份文件（解析失败时退化成字符串比较）。"""
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve()
    except OSError:  # pragma: no cover - resolve 失败只在异常文件系统上
        return left_text == right_text


def authoritative_readiness_path() -> Path:
    """Return the single authoritative path for the readiness file.

    Priority:
    1. ``SA__NIGHTLY_READINESS_PATH`` env var (explicit override, e.g. tests).
    2. ``/app/artifacts/runtime/nightly_data_ready.json`` when ``/app/artifacts``
       exists (container with named volume).
    3. ``<cwd>/artifacts/runtime/nightly_data_ready.json`` otherwise.
    """
    env_path = str(os.environ.get("SA__NIGHTLY_READINESS_PATH", "") or "").strip()
    if env_path:
        return Path(env_path)
    if Path("/app/artifacts").exists():
        return Path("/app/artifacts/runtime") / READINESS_FILENAME
    return Path.cwd() / "artifacts" / "runtime" / READINESS_FILENAME


def _candidate_readiness_paths() -> list[Path]:
    """All locations that may hold the readiness file, newest first.

    Includes legacy ``src/artifacts/runtime`` mirror for backwards-compat
    reads only — writes never target it.
    """
    candidates: list[Path] = []
    # Authoritative first.
    auth = authoritative_readiness_path()
    candidates.append(auth)
    # Legacy: repo-root relative and src/artifacts (read fallback only).
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "artifacts" / "runtime" / READINESS_FILENAME
        if candidate not in candidates:
            candidates.append(candidate)
        # Explicit src/artifacts mirror (baked image artifact).
        src_candidate = parent / "src" / "artifacts" / "runtime" / READINESS_FILENAME
        if src_candidate not in candidates:
            candidates.append(src_candidate)
    # CWD relative fallback.
    cwd_candidate = Path.cwd() / "artifacts" / "runtime" / READINESS_FILENAME
    if cwd_candidate not in candidates:
        candidates.append(cwd_candidate)
    # De-duplicate while preserving order (resolved path when file exists).
    seen: set[str] = set()
    unique: list[Path] = []
    for item in candidates:
        key = str(item.resolve()) if item.exists() else str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def nightly_readiness_paths() -> list[Path]:
    return list(_candidate_readiness_paths())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _expected_trade_date_from_payload(
    payload: dict[str, Any] | None,
    fallback: str = "",
) -> date | None:
    if payload is None:
        return _coerce_date(fallback)
    # Readiness itself may carry expected date
    expected = _coerce_date(payload.get("target_trade_date"))
    if expected is not None:
        return expected
    return _coerce_date(fallback)


def _required_artifact_path(value: str | Path | None, *, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required for nightly readiness")
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    return path


def _validate_daily_index(
    *,
    index_path: str | Path | None,
    target_trade_date: date,
) -> tuple[dict[str, Any], list[str]]:
    """校验日索引，返回 (审计载荷, 目标日应出现的符号集合)。

    第二个返回值**不进 JSON**（5500 个符号塞进 readiness 文件只会让人不去读它），
    只用于 v3 的三方成员锁步；载荷里留摘要与计数。
    """
    path = _required_artifact_path(index_path, label="index_path")
    payload = _read_json(path)
    if payload is None:
        raise ValueError(f"index_path is not valid JSON: {path}")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError(f"index_path has no symbols: {path}")

    # 区分有 ZIP 数据的 symbol（entries 非空）与仅索引占位的 symbol
    # （entries 存在但为空列表）。后者典型场景是新股 IPO 当日被增量
    # 索引发现 latest_date，但上游 ZIP 尚未归档其 CSV；delta 导入无
    # CSV 可读会跳过它们。把这类 symbol 排除出 coverage 分母，避免 1
    # 只新股滞后连带让 5546 只正常票整体 readiness 失败。注意：历史
    # 索引项可能不带 entries 字段，视作有数据（向后兼容）。
    latest_dates: list[date] = []
    hollow_symbols: list[str] = []
    target_symbols: list[str] = []
    for key, item in symbols.items():
        if not isinstance(item, dict):
            continue
        parsed = _coerce_date(item.get("latest_date"))
        if parsed is None:
            continue
        entries = item.get("entries")
        if isinstance(entries, list) and not entries:
            hollow_symbols.append(str(key))
            continue
        latest_dates.append(parsed)
        if parsed == target_trade_date:
            target_symbols.append(str(key))
    if not latest_dates:
        raise ValueError(f"index_path has no latest_date values: {path}")

    index_latest = max(latest_dates)
    symbols_on_target = sum(1 for item in latest_dates if item == target_trade_date)
    if index_latest != target_trade_date:
        raise ValueError(
            "daily index latest date mismatch: "
            f"expected {target_trade_date.isoformat()}, got {index_latest.isoformat()}"
        )
    if symbols_on_target <= 0:
        raise ValueError(f"daily index has no symbols on {target_trade_date.isoformat()}")
    normalized_target = normalize_symbols(target_symbols)
    payload_out = {
        "ok": True,
        "path": str(path),
        "latest_trade_date": index_latest.isoformat(),
        "symbols_total": len(symbols),
        "symbols_on_target_date": symbols_on_target,
        "symbol_set_hash": symbol_set_hash(normalized_target),
        "hollow_symbols": hollow_symbols,
    }
    return payload_out, normalized_target


def _declared_price_series_modes(connection: Any, *, table: str = "daily_bars") -> dict[str, int]:
    """**全表**行内价格口径 → 行数（``""`` = 未声明）。

    有意不按目标日过滤：一份库里混进一行另一种口径，就意味着按这份序列算出来的
    特征/label 在跨越那一行时不连续。要拦的是"这份库能不能当那个角色的序列"，
    而不是"今天新增的行对不对"——写入口（``market_warehouse`` 的逐 symbol 口径
    门禁）负责不让新的污染进来，这里负责不让已有的污染被 release。
    """
    columns = {
        str(item[0]) for item in connection.execute(f"DESCRIBE {table}").fetchall()
    }
    if "price_series_mode" not in columns:
        return {}
    rows = connection.execute(
        f"""
        SELECT COALESCE(TRIM(CAST(price_series_mode AS VARCHAR)), '') AS mode, COUNT(*)
        FROM {table}
        GROUP BY 1
        """
    ).fetchall()
    return {str(row[0]).strip().lower(): int(row[1] or 0) for row in rows}


def _format_mode_histogram(histogram: dict[str, int]) -> str:
    """口径分布的人读形式——口径门失败时诊断价值全在这里。"""
    if not histogram:
        return "(no price_series_mode column / no rows)"
    return ", ".join(
        f"{mode or 'undeclared'}={count}" for mode, count in sorted(histogram.items())
    )


def _validate_delta_db(
    *,
    db_path: str | Path | None,
    target_trade_date: date,
    expected_symbols_on_target: int,
    role: str = ROLE_FEATURE,
    expected_price_series_mode: str = "",
    expected_symbols: Sequence[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """只读打开一份 delta 库并核对最新交易日 / 目标日覆盖 / 行内价格口径。

    ``expected_price_series_mode`` 非空时启用口径硬门（v3 双 delta 模式）：
    声明集合必须**恰好**是 ``{期望口径}``。``qfq`` / ``mixed`` / 完全无声明
    （``unknown``）都不通过——执行侧拿复权价当成交价正是 P0 要封的缺口。

    ``expected_symbols`` 非空时额外做成员锁步：给定集合必须被本库目标日的符号
    集合**包含**，返回缺失样例。缺少的就是"那天没有这份数据的票"。
    """
    path = _required_artifact_path(db_path, label=f"{role}_db_path")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - production dependency
        raise RuntimeError("duckdb is required to validate nightly readiness") from exc

    expected_symbol_set = set(normalize_symbols(expected_symbols or []))
    try:
        with duckdb.connect(str(path), read_only=True) as connection:
            table_exists = connection.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_name = 'daily_bars'
                """
            ).fetchone()
            if not table_exists or int(table_exists[0] or 0) <= 0:
                raise ValueError(f"{role} delta DB has no daily_bars table: {path}")
            row = connection.execute(
                """
                SELECT
                    MAX(date),
                    COUNT(DISTINCT symbol),
                    COUNT(DISTINCT CASE WHEN date = ? THEN symbol END)
                FROM daily_bars
                """,
                [target_trade_date.isoformat()],
            ).fetchone()
            target_rows = connection.execute(
                "SELECT DISTINCT symbol FROM daily_bars WHERE date = ?",
                [target_trade_date.isoformat()],
            ).fetchall()
            declared_modes = _declared_price_series_modes(connection)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            f"cannot validate {role} delta DB {path}: {type(exc).__name__}:{exc}"
        ) from exc

    delta_latest = _coerce_date(row[0] if row else None)
    symbols_total = int(row[1] or 0) if row else 0
    symbols_on_target = int(row[2] or 0) if row else 0
    if delta_latest != target_trade_date:
        actual = delta_latest.isoformat() if delta_latest is not None else ""
        raise ValueError(
            f"{role} delta DB latest date mismatch: "
            f"expected {target_trade_date.isoformat()}, got {actual or 'missing'}"
        )
    if symbols_on_target < expected_symbols_on_target:
        raise ValueError(
            f"{role} delta DB target-date coverage is incomplete: "
            f"{symbols_on_target}<{expected_symbols_on_target}"
        )
    target_symbols = normalize_symbols(item[0] for item in target_rows)
    missing_examples: list[str] = []
    if expected_symbol_set:
        missing_examples = sorted(expected_symbol_set - set(target_symbols))
        if missing_examples:
            raise ValueError(
                f"{role} delta DB is missing {len(missing_examples)} symbol(s) that the "
                f"target date requires (examples: {missing_examples[:10]})"
            )
    observed_mode = ""
    if expected_price_series_mode:
        wanted = str(expected_price_series_mode).strip().lower()
        declared = {mode: count for mode, count in declared_modes.items() if mode and count > 0}
        if not declared:
            raise ValueError(
                f"{role} delta DB declares no price_series_mode (unknown); expected {wanted}"
                f" [table histogram: {_format_mode_histogram(declared_modes)}]: {path}"
            )
        if set(declared) != {wanted}:
            raise ValueError(
                f"{role} delta DB price_series_mode mismatch: expected {wanted}, got "
                f"[{_format_mode_histogram(declared_modes)}]: {path}"
            )
        observed_mode = wanted
    coverage_ratio = (
        round(symbols_on_target / expected_symbols_on_target, 6)
        if expected_symbols_on_target > 0
        else 0.0
    )
    payload_out = {
        "ok": True,
        "role": role,
        "path": str(path),
        "latest_trade_date": delta_latest.isoformat(),
        "symbols_total": symbols_total,
        "symbols_on_target_date": symbols_on_target,
        "expected_symbols_on_target_date": expected_symbols_on_target,
        "symbol_set_hash": symbol_set_hash(target_symbols),
        "coverage_ratio": coverage_ratio,
    }
    if observed_mode:
        payload_out["price_series_mode"] = observed_mode
        payload_out["declared_price_series_modes"] = sorted(declared_modes)
    return payload_out, target_symbols


def _lock_step_symbol_membership(
    *,
    expected_symbols: list[str],
    feature_symbols: list[str],
    execution_symbols: list[str],
    max_examples: int = 20,
) -> tuple[dict[str, Any], list[str]]:
    """v3 三方成员锁步：``expected ⊆ feature ⊆ execution``；返回 (审计块, 违规说明)。

    包含链而不是全等，理由见模块 docstring：raw 侧不依赖复权因子，因因子缺失被
    qfq 侧跳过的 symbol 在 raw 侧**天然存在**。要求全等会把这条正常路径每晚误杀。
    """
    expected = set(expected_symbols)
    feature = set(feature_symbols)
    execution = set(execution_symbols)
    missing_feature = sorted(expected - feature)
    missing_execution = sorted(expected - execution)
    feature_not_in_execution = sorted(feature - execution)
    problems: list[str] = []
    if missing_feature:
        problems.append(
            f"feature delta is missing {len(missing_feature)} expected symbol(s) "
            f"(examples: {missing_feature[:max_examples]})"
        )
    if missing_execution:
        problems.append(
            f"execution delta is missing {len(missing_execution)} expected symbol(s) "
            f"(examples: {missing_execution[:max_examples]})"
        )
    if feature_not_in_execution:
        problems.append(
            f"execution delta is missing {len(feature_not_in_execution)} symbol(s) present "
            f"in feature (examples: {feature_not_in_execution[:max_examples]})"
        )
    audit = {
        "symbols_expected": len(expected_symbols),
        "symbols_feature": len(feature_symbols),
        "symbols_execution": len(execution_symbols),
        "symbol_set_hash_expected": symbol_set_hash(expected_symbols),
        "symbol_set_hash_feature": symbol_set_hash(feature_symbols),
        "symbol_set_hash_execution": symbol_set_hash(execution_symbols),
        "missing_feature": missing_feature[:max_examples],
        "missing_feature_count": len(missing_feature),
        "missing_execution": missing_execution[:max_examples],
        "missing_execution_count": len(missing_execution),
        "feature_not_in_execution": feature_not_in_execution[:max_examples],
        "feature_not_in_execution_count": len(feature_not_in_execution),
        "extra_execution_vs_expected": sorted(execution - expected)[:max_examples],
        "extra_execution_vs_expected_count": len(execution - expected),
        "membership_locked": not problems,
    }
    return audit, problems


def read_nightly_readiness(path: str | Path | None = None) -> dict[str, Any] | None:
    """Return the readiness JSON payload, or ``None`` when absent / unreadable."""
    if path is not None:
        return _read_json(Path(path))
    auth = authoritative_readiness_path()
    payload = _read_json(auth)
    if payload is not None:
        return payload
    # Fallback: legacy mirrors (warn so they get cleaned up).
    for candidate in _candidate_readiness_paths():
        if candidate == auth:
            continue
        candidate_payload = _read_json(candidate)
        if candidate_payload is not None:
            logger.warning(
                "readiness read from legacy mirror %s (authoritative %s missing)",
                candidate,
                auth,
            )
            return candidate_payload
    return None


def _recent_sessions(db_path: str | Path, *, up_to: date, sessions: int) -> list[str]:
    """``db`` 里 ``<= up_to`` 的最近 N 个交易日（升序 ISO）。只读。"""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT CAST(date AS VARCHAR) d FROM daily_bars WHERE date <= ? "
            "ORDER BY d DESC LIMIT ?",
            [up_to.isoformat(), int(sessions)],
        ).fetchall()
    finally:
        con.close()
    return sorted(str(row[0]) for row in rows)


def _qfq_parity_for_readiness(
    *,
    feature_db: str | Path,
    execution_db: str | Path,
    target_trade_date: date,
    factor_days: Mapping[str, Sequence[str]] | None = None,
    factor_loader: Callable[[Collection[str]], Mapping[str, Sequence[str]]] | None = None,
    window: Sequence[str] | None,
    lookback_sessions: int = QFQ_PARITY_LOOKBACK_SESSIONS,
) -> tuple[dict[str, Any], list[str]]:
    """两份 delta 库的逐键对账 -> (readiness 区块, 阻塞问题清单)。

    ⚠️ **只有 ``QFQ_DERIVATION_GAP`` 与 ``QFQ_ROW_WITHOUT_RAW`` 阻塞**：
    ``factor_missing_for_raw``（raw 有、该票根本没有可用因子、qfq 因此没有行）是
    ``_lock_step_symbol_membership`` 包含链明确容忍的既有语义（新股 / 因子未发布），
    把它判成故障会每晚误杀正常更新。它的真正出口在导入侧——那一轮 ``ok=false`` 且
    非零退出，见 ``scripts/import_vendor_zip_to_delta.py``。两个维度正交，互不替代。

    成本结构：先用一次 ``EXCEPT`` 问"有没有差异"，**没有差异就不碰复权因子包**
    （全量解析实测 279 秒，健康的夜扫描应该是 0 秒）。真有差异时才按涉及的票
    去要因子索引（``factor_loader(symbols)``），把代价限制在缺陷本身上。

    ``factor_days`` 与 ``factor_loader`` 都没有时不猜：记 ``evaluated=false``
    并且**照样把看到的 raw-only 键数写进区块**——"没归因"不等于"没问题"，
    active Alpha（``require_dual_delta=True``）因此不消费一份未证明一致的 readiness。
    """
    from stock_analyzer.data.qfq_parity import (
        REASON_DERIVATION_GAP,
        REASON_ROW_WITHOUT_RAW,
        assess_qfq_parity,
        asymmetry_keys,
        factor_availability_from_series,
    )

    win = [str(day) for day in (window or [])]
    if len(win) != 2:
        recent = _recent_sessions(execution_db, up_to=target_trade_date, sessions=lookback_sessions)
        win = [recent[0], recent[-1]] if recent else [target_trade_date.isoformat()] * 2
    raw_only, qfq_only = asymmetry_keys(
        raw_db=str(execution_db), qfq_db=str(feature_db), window=win
    )

    if not raw_only and not qfq_only:
        return (
            {
                "guard": "qfq_parity",
                "evaluated": True,
                "ok": True,
                "blocking_reasons": [],
                "reason": "",
                "window": win,
                "derivation_gap_count": 0,
                "factor_missing_count": 0,
                "qfq_present_raw_missing_count": 0,
                "raw_present_qfq_missing": 0,
                "affected_dates": [],
                "top_affected_dates": {},
                "affected_symbol_count": 0,
                "factor_index_consulted": False,
            },
            [],
        )

    days = factor_days
    if days is None and factor_loader is not None:
        days = factor_loader(sorted({symbol for symbol, _day in raw_only}))
    if days is None:
        return (
            {
                "guard": "qfq_parity",
                "evaluated": False,
                "ok": False,
                "reason": "factor_index_not_provided",
                "window": win,
                "blocking_reasons": [],
                "derivation_gap_count": None,
                "factor_missing_count": None,
                "qfq_present_raw_missing_count": len(qfq_only),
                "raw_present_qfq_missing": len(raw_only),
                "affected_dates": sorted({day for _s, day in raw_only | qfq_only})[:20],
                "top_affected_dates": {},
                "affected_symbol_count": len({symbol for symbol, _ in raw_only | qfq_only}),
                "factor_index_consulted": False,
            },
            [],
        )

    parity = assess_qfq_parity(
        raw_db=str(execution_db),
        qfq_db=str(feature_db),
        window=win,
        has_factor=factor_availability_from_series(days),
    )
    problems: list[str] = []
    if parity.derivation_gap_count:
        problems.append(f"{REASON_DERIVATION_GAP}:{parity.derivation_gap_count}")
    if parity.qfq_present_raw_missing:
        problems.append(f"{REASON_ROW_WITHOUT_RAW}:{parity.qfq_present_raw_missing}")
    block = {
        "guard": "qfq_parity",
        "evaluated": True,
        "ok": not problems,
        "blocking_reasons": problems,
        "derivation_gap_count": int(parity.derivation_gap_count),
        "factor_missing_count": int(parity.factor_missing_for_raw),
        "qfq_present_raw_missing_count": int(parity.qfq_present_raw_missing),
        "raw_present_qfq_missing": int(parity.raw_present_qfq_missing),
        "reason": parity.reason,
        "window": win,
        "affected_dates": list(parity.affected_dates),
        "top_affected_dates": dict(parity.top_affected_dates),
        "affected_symbol_count": len(parity.affected_symbols),
        "factor_index_consulted": True,
    }
    return block, problems


def write_nightly_readiness(
    *,
    target_trade_date: date | str,
    db_path: str | Path | None = None,
    index_path: str | Path | None = None,
    execution_db_path: str | Path | None = None,
    updater_commit: str = "",
    extra: dict[str, Any] | None = None,
    path: str | Path | None = None,
    verify_raw_baseline: bool = True,
    raw_baseline_marker_path: str | Path | None = None,
    qfq_factor_days: Mapping[str, Sequence[str]] | None = None,
    qfq_factor_loader: Callable[[Collection[str]], Mapping[str, Sequence[str]]] | None = None,
    qfq_parity_window: Sequence[str] | None = None,
) -> Path:
    """Atomically write ``nightly_data_ready.json`` to the authoritative path.

    Args:
        target_trade_date: latest daily index date (not shell calendar date).
        db_path / index_path: required artifacts. Both are opened and checked
            against `target_trade_date` before readiness is published.
        execution_db_path: 第二份 delta（execution/raw）。给了它才写 v3 并校验：
        目标日成员必须覆盖 feature 侧与索引侧，行内口径必须恰好是 raw，且
        bootstrap marker 必须证明它是一份**经过覆盖认证的基线**（见
        :func:`verify_bootstrap_marker`）。不给 = 保持 v2 单 delta 语义不变。
        updater_commit: the updater git commit (from ``.build_commit``).
        extra: additional keys merged into the payload.
        path: override output path; when omitted the authoritative path is used.
        verify_raw_baseline: v3 下是否校验 raw 基线身份（生产恒为真；只有构造
        夹具的测试会关掉它）。
        raw_baseline_marker_path: marker 路径覆盖（默认与 raw 库同目录）。
        qfq_factor_days: 显式给出 ``symbol -> 升序因子日期``（测试与离线审计用）。
        qfq_factor_loader: ``symbols -> 因子日期`` 的**惰性**取数回调。生产走这条：
            只有真的查出 raw-only 键时才按涉及的票去解析复权因子包，
            健康夜扫描因此不必付全量解析的 279 秒。两者都没给 = 记
            ``evaluated=false``，**不**当作通过。
        qfq_parity_window: 显式对账窗口 ``[start, end]``。省略时取 raw 库里
            ``<= target_trade_date`` 的最近 :data:`QFQ_PARITY_LOOKBACK_SESSIONS` 个交易日
            ——本轮增量导入只会追加最新日期，所以这个回看窗口必然覆盖本轮触达的键，
            同时**不必**每晚扫十年历史（历史缺口归 repair manifest 管）。

    Raises:
        ValueError: 任一完整性判据不过，包括 QFQ 对账发现派生缺口——此时**不写文件**，
            于是本轮没有可消费的 PASS（昨天的 marker 早在运行开头就被 retire 了）。

    Returns:
        The path that was written.
    """
    coerced = _coerce_date(target_trade_date)
    if coerced is None:
        raise ValueError(f"invalid target_trade_date: {target_trade_date!r}")
    index_validation, expected_symbols = _validate_daily_index(
        index_path=index_path,
        target_trade_date=coerced,
    )
    dual = bool(str(execution_db_path or "").strip())
    execution_db = str(execution_db_path or "")
    if dual and _same_path(execution_db, db_path):
        # 同一份文件承担两个角色时，成员锁步与口径门都会退化成恒真（自己比自己），
        # readiness 看起来通过但什么都没证明。生产形态是两份物理独立的库。
        raise ValueError(
            "feature and execution delta must be physically separate DuckDB files: "
            f"{execution_db}"
        )
    expected_price_mode = FEATURE_DELTA_PRICE_MODE if dual else ""
    delta_validation, feature_symbols = _validate_delta_db(
        db_path=db_path,
        target_trade_date=coerced,
        expected_symbols_on_target=int(index_validation["symbols_on_target_date"]),
        role=ROLE_FEATURE,
        expected_price_series_mode=expected_price_mode,
        expected_symbols=expected_symbols if dual else None,
    )
    execution_validation: dict[str, Any] | None = None
    symbol_membership: dict[str, Any] | None = None
    raw_baseline_block: dict[str, Any] | None = None
    if dual:
        execution_validation, execution_symbols = _validate_delta_db(
            db_path=execution_db,
            target_trade_date=coerced,
            expected_symbols_on_target=int(index_validation["symbols_on_target_date"]),
            role=ROLE_EXECUTION,
            expected_price_series_mode=RAW_DELTA_PRICE_MODE,
            expected_symbols=expected_symbols,
        )
        symbol_membership, membership_problems = _lock_step_symbol_membership(
            expected_symbols=expected_symbols,
            feature_symbols=feature_symbols,
            execution_symbols=execution_symbols,
        )
        if membership_problems:
            raise ValueError(
                "dual-delta symbol membership lock-step failed: "
                + " | ".join(membership_problems)
            )
        qfq_parity_block, qfq_parity_problems = _qfq_parity_for_readiness(
            feature_db=str(db_path or ""),
            execution_db=execution_db,
            target_trade_date=coerced,
            factor_days=qfq_factor_days,
            factor_loader=qfq_factor_loader,
            window=qfq_parity_window,
        )
        if qfq_parity_problems:
            # 发布前失败 = 不产出 marker。本轮开头已经 invalidate 过昨天的 marker，
            # 所以对账失败的自然结果就是"今晚没有任何可消费的 PASS"。
            raise ValueError(
                "QFQ parity check failed: "
                + " | ".join(qfq_parity_problems)
                + f"; window={qfq_parity_block['window']}"
                + f"; dates={qfq_parity_block['affected_dates'][:10]}"
                + f"; symbols={qfq_parity_block['affected_symbol_count']}"
                + f"; factor_missing(non-blocking)={qfq_parity_block['factor_missing_count']}"
            )
        if verify_raw_baseline:
            try:
                marker = verify_bootstrap_marker(
                    raw_db_path=execution_db,
                    marker_path=raw_baseline_marker_path,
                )
            except RawDeltaBaselineError as exc:
                raise ValueError(
                    f"execution delta is not a certified RAW baseline ({exc.reason}): {exc}"
                ) from exc
            raw_baseline_block = {
                "ok": True,
                "reason": "ok",
                "schema": marker.get("schema", ""),
                "coverage_status": marker.get("coverage_status", ""),
                "price_series_mode": marker.get("price_series_mode", ""),
                "required_source_window": marker.get("required_source_window", {}),
                "symbols_expected": marker.get("symbols_expected"),
                "symbols_covered": marker.get("symbols_covered"),
                "baseline_rows": (marker.get("db_content_identity") or {}).get("rows"),
            }
        else:
            raw_baseline_block = {"ok": True, "reason": "verification_disabled_by_caller"}

    target = Path(path) if path is not None else authoritative_readiness_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION_DUAL if dual else READINESS_SCHEMA_VERSION,
        "target_trade_date": coerced.isoformat(),
        "daily": {
            "ok": True,
            "latest_trade_date": coerced.isoformat(),
            "symbols_on_target_date": index_validation["symbols_on_target_date"],
        },
        "index": index_validation,
        "delta": delta_validation,
        "created_at": datetime.now(UTC).isoformat(),
        "updater_commit": str(updater_commit or "").strip(),
        "source": "stock_updater.sh",
        "delta_db_path": str(db_path),
        "index_path": str(index_path),
    }
    if dual:
        payload["execution_delta"] = execution_validation
        payload["symbol_membership"] = symbol_membership
        payload["raw_delta_baseline"] = raw_baseline_block
        payload["qfq_parity"] = qfq_parity_block
        payload["execution_delta_db_path"] = execution_db
    if extra:
        reserved = {
            "schema_version",
            "target_trade_date",
            "daily",
            "index",
            "delta",
            "execution_delta",
            "symbol_membership",
            "raw_delta_baseline",
            "qfq_parity",
            "created_at",
            "delta_db_path",
            "execution_delta_db_path",
            "index_path",
        }
        payload.update({key: value for key, value in extra.items() if key not in reserved})
    tmp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, target)
    return target


def check_nightly_readiness(
    *,
    expected_trade_date: str | date | datetime | None = None,
    path: str | Path | None = None,
    require_dual_delta: bool = False,
) -> ReadinessGate:
    """Evaluate the nightly readiness gate.

    When ``expected_trade_date`` is omitted it is derived from the readiness
    payload's ``target_trade_date`` (i.e. the gate checks internal
    consistency only).  Callers that know the true expected date (from the
    daily index's latest date) should pass it.

    ``require_dual_delta``（默认 ``False``）区分**两种消费者**：

    - ``False``：单 delta 语义。v2 与 v3 都算就绪——Legacy / Week5 的夜扫与
      历史 release 契约不变，Alpha V2 的引入不得收紧它们；
    - ``True``：要求 ``schema_version >= 3`` 且执行侧证据齐全。**active Alpha
      epoch 的 capture 必须走这一档**：epoch 的 label / 成交价 / 超额都取自
      execution/raw 库，拿 v2（只有 feature delta）去 capture 等于在没有执行侧
      证据的晚上照记 clean day。此时 v2 一律不 ready，原因码
      ``nightly_dual_delta_not_ready``（而不是笼统的 ``nightly_data_not_ready``）。

    Returns a :class:`ReadinessGate` whose ``scheduler_triple`` satisfies the
    scheduler contract: missing or date-mismatched readiness yields
    ``(ran=true, success=false, detail=nightly_data_not_ready)``.
    """
    payload = read_nightly_readiness(path=path)
    if payload is None:
        expected_s = str(expected_trade_date or "").strip()
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload={},
            expected_trade_date=expected_s,
        )
    schema_version = payload.get("schema_version")
    try:
        version = int(schema_version)  # type: ignore[arg-type]
    except Exception:
        version = -1
    if version not in SUPPORTED_READINESS_SCHEMA_VERSIONS:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=str(expected_trade_date or payload.get("target_trade_date", "")),
        )
    if require_dual_delta and version < READINESS_SCHEMA_VERSION_DUAL:
        # 放在"槽位检查"之前：这份文件的**形状**就不满足消费者要求，先报这一条比
        # 先报某个 slot 更准确（v2 文件再健康也不含执行侧证据）。
        return ReadinessGate(
            ready=False,
            reason=READINESS_REASON_DUAL_DELTA,
            payload=payload,
            expected_trade_date=str(expected_trade_date or payload.get("target_trade_date", "")),
        )
    # Require daily/index/delta success.
    for key in ("daily", "index", "delta"):
        slot = payload.get(key)
        if not isinstance(slot, dict) or not bool(slot.get("ok", False)):
            return ReadinessGate(
                ready=False,
                reason="nightly_data_not_ready",
                payload=payload,
                expected_trade_date=str(
                    expected_trade_date or payload.get("target_trade_date", "")
                ),
            )
    if version >= READINESS_SCHEMA_VERSION_DUAL:
        # v3 是"双 delta 已启用"的自证：执行侧证据必须齐全，缺一块都不 ready
        # ——不允许"声明 v3 却按 v2 放行"这种前后不一致的降级。
        # 原因码随消费者变化：Alpha（require_dual_delta=True）要的是"今晚没有执行侧
        # 证据"这一类结论，用同一个码；Week5 / Legacy 保持既有 nightly_data_not_ready。
        dual_reason = (
            READINESS_REASON_DUAL_DELTA if require_dual_delta else "nightly_data_not_ready"
        )

        def _dual_blocked(reason: str = "") -> ReadinessGate:
            return ReadinessGate(
                ready=False,
                reason=reason or dual_reason,
                payload=payload,
                expected_trade_date=str(
                    expected_trade_date or payload.get("target_trade_date", "")
                ),
            )

        execution = payload.get("execution_delta")
        if not isinstance(execution, dict) or not bool(execution.get("ok", False)):
            return _dual_blocked()
        membership = payload.get("symbol_membership")
        if not isinstance(membership, dict) or not bool(membership.get("membership_locked")):
            return _dual_blocked()
        baseline = payload.get("raw_delta_baseline")
        if not isinstance(baseline, dict) or not bool(baseline.get("ok", False)):
            return _dual_blocked()
        # QFQ 对账：两份库对同一 (symbol, date) 给了不同答案时，重试永远不会变好，
        # 必须修数据——所以它要有**自己的**原因码，而不是混进 nightly_data_not_ready。
        parity_block = payload.get("qfq_parity")
        if not isinstance(parity_block, dict):
            parity_block = {}
        parity_evaluated = bool(parity_block.get("evaluated", False))
        if parity_evaluated and not bool(parity_block.get("ok", False)):
            # 做过对账且不通过：所有 v3 消费者都不放行。
            return _dual_blocked(READINESS_REASON_QFQ_PARITY)
        if not parity_evaluated and require_dual_delta:
            # 没做过对账（改动上线前的旧 v3 marker，或调用方未提供因子索引）：
            # "未证明"不等于"通过"，active Alpha epoch 拒绝消费；
            # 但 Week5 / Legacy 的既有契约不在这里被悄悄收紧。
            return _dual_blocked(READINESS_REASON_QFQ_PARITY)
    readiness_date = _coerce_date(payload.get("target_trade_date"))
    if readiness_date is None:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=str(expected_trade_date or ""),
        )
    expected_date = (
        _coerce_date(expected_trade_date)
        if expected_trade_date not in (None, "")
        else readiness_date
    )
    if expected_date is None:
        expected_date = readiness_date
    if readiness_date != expected_date:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=expected_date.isoformat(),
        )
    return ReadinessGate(
        ready=True,
        reason="ok",
        payload=payload,
        expected_trade_date=readiness_date.isoformat(),
    )


def invalidate_nightly_readiness(
    *,
    stamp: str | None = None,
) -> list[Path]:
    """Atomically retire every readable readiness file as ``*.stale-*``.

    A vendor update run must invalidate the previous night's readiness
    BEFORE touching any data: if the update then fails, no stale readiness
    may survive for the off-hours selector to consume (fail-closed).
    ``read_nightly_readiness`` falls back to legacy mirrors, so ALL
    candidate locations are drained here, not just the authoritative one.

    Consumed files are not touched; stale files keep their payload and
    mtime for post-mortem auditing and are never restored automatically.

    Returns the source paths that were invalidated (they no longer exist).
    """
    marker = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    invalidated: list[Path] = []
    failures: list[str] = []
    for candidate in _candidate_readiness_paths():
        if _read_json(candidate) is None:
            continue
        # 中缀命名与 consumed 文件（nightly_data_ready.consumed.json）一致：
        # 前缀固定，按文件名排序即按失效时间排序。
        target = candidate.with_name(f"nightly_data_ready.stale-{marker}.json")
        suffix = 1
        while target.exists():
            target = candidate.with_name(f"nightly_data_ready.stale-{marker}.{suffix}.json")
            suffix += 1
        try:
            os.replace(candidate, target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{candidate}:{type(exc).__name__}:{exc}")
            continue
        invalidated.append(candidate)
    if failures:
        raise OSError(
            "failed to invalidate one or more nightly readiness files: " + " | ".join(failures)
        )
    return invalidated


def consume_nightly_readiness(
    *,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Atomically rename the readiness file(s) to the consumed name.

    When ``path`` is given, only that file is consumed.  Otherwise all
    candidate locations are drained so a stale mirror cannot be re-read
    after the authoritative file is consumed.
    """
    if path is not None:
        source = Path(path)
        payload = _read_json(source)
        if payload is None:
            return None
        target = source.with_name(CONSUMED_FILENAME)
        try:
            os.replace(source, target)
        except OSError:
            return None
        return payload
    # Drain all candidates; return the first payload found.
    first_payload: dict[str, Any] | None = None
    for candidate in _candidate_readiness_paths():
        payload = _read_json(candidate)
        if payload is None:
            continue
        if first_payload is None:
            first_payload = payload
        target = candidate.with_name(CONSUMED_FILENAME)
        # Avoid overwriting an existing consumed file from another candidate
        # with different content — keep the first consumed payload's file.
        if target.exists():
            try:
                candidate.unlink()
            except OSError:
                pass
            continue
        try:
            os.replace(candidate, target)
        except OSError:
            continue
    return first_payload
