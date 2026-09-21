"""Alpha V2 M4-L：Production Data Preflight（NAS 上线前只读数据体检）。

**为什么存在**：M4-H 已经证明历史证据是 MIXED、且 volume 单位在 2025-09 后
发生混合。正式 ``alpha_v2_epoch_001`` 开启前必须回答"当下这份生产数据配不配
训练出可用的冻结影子模型"，并把结论钉成可审计工件；``BLOCKED`` 时**不允许**
开 epoch、也不允许"先开再查"。

本模块**只读**：检查 market.duckdb、配置安全开关、运行身份、特征 schema 可算性、
volume 单位一致性；不写数据库、不改特征、不修数据（数据治理是独立动作）。

判定分级（``verdict``）：

- ``PASS``：全部检查通过；
- ``WARN``：有非致命问题（例如训练窗外的最新交易日尾部不完整）；
- ``BLOCKED``：任一**致命**检查失败（mixed volume units / 缺特征 /
  身份不可证 / 安全开关被打开 / 数据源缺失或不可读 / 训练窗内重复主键等）。
  ``BLOCKED`` 必须带非零 exit code（CLI 层 1），且 validation freeze 的生产硬门
  会拒绝开启 epoch。

训练窗绑定（防止"检查 A 窗口、实际训练 B 窗口"）：报告里记录
``training_window``（含 ``training_window_hash`` 与 ``data_identity``），
validation freeze 会拿它与冻结模型工件的 provenance window 逐字对账。

volume 单位正式定义（train 窗内逐自然月统计 ``turnover / volume``）：

- 比值 ≈ 当日均价（几元~几十元）→ volume 以**股**计（share-like）；
- 比值 ≈ 100 × 均价 → volume 以**手**计（lot-like），判定阈值 100（与
  M4-H inventory 的 ``_unit_regime_probe`` 同源，便于跨阶段对照）。

``BLOCKED`` 判据：任一自然月 share-like 比例落在 (0.2, 0.8) 开区间（月内混合）；
或窗口内同时存在 share-like ≥ 0.8 的月份与 ≤ 0.2 的月份（窗口内单位切换）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic

PREFLIGHT_SCHEMA = "alpha_v2_production_preflight.v1"
VERDICT_PASS = "PASS"
VERDICT_WARN = "WARN"
VERDICT_BLOCKED = "BLOCKED"
_VERDICT_ORDER = {VERDICT_PASS: 0, VERDICT_WARN: 1, VERDICT_BLOCKED: 2}

# volume 单位判别阈值：turnover/volume > 100 ⇔ 手（同 M4-H inventory）。
_LOT_LIKE_RATIO_THRESHOLD = 100.0
# 月内混合判定开区间（share-like 比例落在此区间 = 当月两种单位并存）。
_MIXED_MONTH_LOW = 0.2
_MIXED_MONTH_HIGH = 0.8
# 尾段残缺：最新交易日的行数低于近 20 日中位数的该比例即判"尾段不完整"。
_TAIL_FRAGMENT_RATIO = 0.5


@dataclass
class CheckResult:
    """单项检查结论（facts 永远全量落盘，便于事后复核）。"""

    name: str
    verdict: str
    facts: dict[str, object] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)


class PreflightError(RuntimeError):
    """preflight 无法执行（参数/环境错误），与 verdict=BLOCKED 区分。"""


def canonical_hash(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def preflight_hash_of(payload: Mapping[str, object]) -> str:
    """除 ``preflight_hash`` 字段本身外的 canonical JSON sha256（自锚定哈希）。

    与 freeze manifest / funnel snapshot 同一约定：哈希字段不能参与自身计算，
    否则"写进去再读出来"永不自洽（首轮实现即踩此坑）。
    """
    body = {key: value for key, value in payload.items() if key != "preflight_hash"}
    return canonical_hash(body)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def check_runtime_identity(repo_root: str | Path) -> CheckResult:
    """A. Runtime / Build —— 复用 R4/R4.1 的唯一身份实现，不复制第二套逻辑。"""
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        resolve_runtime_code_identity,
    )

    try:
        identity = resolve_runtime_code_identity(Path(repo_root), validation_mode="production")
    except Exception as exc:  # noqa: BLE001 - 身份解析失败本身就是 BLOCKED 证据
        return CheckResult(
            name="runtime_identity",
            verdict=VERDICT_BLOCKED,
            findings=[f"identity_resolution_failed:{exc.__class__.__name__}:{exc}"],
        )
    payload = identity.to_payload()
    violations = list(identity.violations)
    verdict = VERDICT_BLOCKED if violations else VERDICT_PASS
    return CheckResult(
        name="runtime_identity",
        verdict=verdict,
        facts={"identity": payload},
        findings=[f"identity_violation:{item}" for item in violations],
    )


def check_safety_flags(config: object) -> CheckResult:
    """B. Safety flags —— 必须保持 shadow 安全组合。"""
    facts: dict[str, object] = {}
    findings: list[str] = []
    alpha = getattr(config, "alpha_v2", None)
    training = getattr(config, "training", None)
    auto_promotion = getattr(config, "auto_promotion", None)
    shadow_only = bool(getattr(alpha, "shadow_only", False))
    enforce = bool(getattr(alpha, "enforce_final_selection", True))
    training_enabled = bool(getattr(training, "enabled", True))
    auto_promo_enabled = bool(getattr(auto_promotion, "enabled", True))
    execution_price_mode = ""
    price_error = ""
    try:
        from stock_analyzer.alpha_v2.validation.runtime_identity import price_contract_block

        price = price_contract_block(config)
        execution_price_mode = str(price.get("execution_price_mode", ""))
    except Exception as exc:  # noqa: BLE001
        price_error = f"{exc.__class__.__name__}: {exc}"
    facts.update(
        {
            "shadow_only": shadow_only,
            "enforce_final_selection": enforce,
            "training_enabled": training_enabled,
            "auto_promotion_enabled": auto_promo_enabled,
            "execution_price_mode": execution_price_mode,
        }
    )
    if not shadow_only:
        findings.append("safety_flag:shadow_only_must_be_true")
    if enforce:
        findings.append("safety_flag:enforce_final_selection_must_be_false")
    if training_enabled:
        findings.append("safety_flag:training_enabled_must_be_false")
    if auto_promo_enabled:
        findings.append("safety_flag:auto_promotion_enabled_must_be_false")
    if price_error:
        findings.append(f"price_contract_unresolvable:{price_error}")
    elif execution_price_mode.strip().lower() != "raw":
        findings.append(
            f"safety_flag:execution_price_mode_must_be_raw(actual={execution_price_mode!r})"
        )
    verdict = VERDICT_BLOCKED if findings else VERDICT_PASS
    return CheckResult("safety_flags", verdict, facts, findings)


def _connect_market_db(market_db: str | Path):
    import duckdb

    return duckdb.connect(str(market_db), read_only=True)


def check_market_db(
    market_db: str | Path, *, training_start: date | None, training_end: date | None
) -> CheckResult:
    """C. Market DB —— 存在/可读/最新完整交易日/广度/重复主键/尾段残缺。"""
    path = Path(market_db)
    if not path.exists() or not path.is_file():
        return CheckResult(
            "market_db", VERDICT_BLOCKED, {"path": str(path)}, ["data_source_missing"]
        )
    try:
        connection = _connect_market_db(path)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "market_db",
            VERDICT_BLOCKED,
            {"path": str(path)},
            [f"data_source_unreadable:{exc.__class__.__name__}:{exc}"],
        )
    try:
        latest = connection.execute("SELECT max(date) FROM daily_bars").fetchone()
        latest_date = latest[0] if latest else None
        total_rows = int(
            connection.execute("SELECT count(*) FROM daily_bars").fetchone()[0] or 0
        )
        duplicates = int(
            connection.execute(
                "SELECT count(*) - count(DISTINCT (symbol, date)) FROM daily_bars"
            ).fetchone()[0]
            or 0
        )
        trailing = [
            {"date": str(row[0]), "rows": int(row[1])}
            for row in connection.execute(
                "SELECT date, count(*) AS rows FROM daily_bars "
                "GROUP BY 1 ORDER BY 1 DESC LIMIT 20"
            ).fetchall()
        ]
        latest_rows = trailing[0]["rows"] if trailing else 0
        median_rows = 0
        if trailing:
            counts = sorted(item["rows"] for item in trailing)
            median_rows = counts[len(counts) // 2]
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "market_db",
            VERDICT_BLOCKED,
            {"path": str(path)},
            [f"market_db_query_failed:{exc.__class__.__name__}:{exc}"],
        )
    finally:
        connection.close()
    # 半分位及以下 = 最新交易日的覆盖明显塌陷（含"只剩一半"这种典型残缺）。
    tail_fragment = bool(
        median_rows > 0 and latest_rows <= median_rows * _TAIL_FRAGMENT_RATIO
    )
    facts: dict[str, object] = {
        "path": str(path),
        "exists": True,
        "latest_trade_date": str(latest_date) if latest_date else "",
        "total_rows": total_rows,
        "duplicate_logical_keys": duplicates,
        "latest_date_rows": latest_rows,
        "trailing_median_rows": median_rows,
        "tail_fragment": tail_fragment,
        "trailing_days": trailing,
    }
    findings: list[str] = []
    if duplicates > 0:
        findings.append(f"duplicate_logical_keys:{duplicates}")
    if tail_fragment:
        # 分级：残缺日落在**训练窗内** = 训练会吃到残缺数据（BLOCKED）；
        # 落在窗外（窗口早已结束，残缺只属"最新一天"） = 仅 WARN。
        latest_iso = str(latest_date) if latest_date else ""
        in_window = bool(
            latest_iso
            and training_start is not None
            and training_end is not None
            and training_start.isoformat() <= latest_iso <= training_end.isoformat()
        )
        findings.append(
            "tail_fragment_in_training_window" if in_window else "tail_fragment_after_window"
        )
    verdict = VERDICT_PASS
    for finding in findings:
        if finding.startswith("duplicate_") or finding.endswith("in_training_window"):
            verdict = VERDICT_BLOCKED
            break
        verdict = VERDICT_WARN
    return CheckResult("market_db", verdict, facts, findings)


def check_volume_units(
    market_db: str | Path, *, training_start: date, training_end: date
) -> CheckResult:
    """E. Volume unit gate（§22）——窗口内是否存在两种单位。"""
    path = Path(market_db)
    if not path.exists():
        return CheckResult(
            "volume_units", VERDICT_BLOCKED, {"path": str(path)}, ["data_source_missing"]
        )
    try:
        connection = _connect_market_db(path)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "volume_units",
            VERDICT_BLOCKED,
            {"path": str(path)},
            [f"data_source_unreadable:{exc.__class__.__name__}:{exc}"],
        )
    try:
        columns = {
            str(row[0])
            for row in connection.execute("DESCRIBE daily_bars").fetchall()
        }
        for required in ("volume", "turnover", "date", "symbol"):
            if required not in columns:
                return CheckResult(
                    "volume_units",
                    VERDICT_BLOCKED,
                    {"path": str(path), "columns": sorted(columns)},
                    [f"volume_unit_check_impossible:missing_column:{required}"],
                )
        monthly = [
            {
                "month": str(row[0]),
                "rows": int(row[1]),
                "symbols": int(row[2]),
                "lot_like_ratio": round(float(row[3]), 6),
                "share_like_ratio": round(1.0 - float(row[3]), 6),
            }
            for row in connection.execute(
                "SELECT strftime(date, '%Y-%m') AS month, count(*) AS rows, "
                "count(DISTINCT symbol) AS symbols, "
                "avg(CASE WHEN turnover / NULLIF(volume, 0) > ? THEN 1.0 ELSE 0.0 END) "
                "AS lot_like_ratio "
                "FROM daily_bars WHERE volume IS NOT NULL AND volume > 0 "
                "AND turnover IS NOT NULL AND date BETWEEN ? AND ? "
                "GROUP BY 1 ORDER BY 1",
                [
                    _LOT_LIKE_RATIO_THRESHOLD,
                    training_start.isoformat(),
                    training_end.isoformat(),
                ],
            ).fetchall()
        ]
    finally:
        connection.close()
    zero_rows = [item["month"] for item in monthly if item["rows"] == 0]
    for item in monthly:
        # 每月一个显式状态标签（§22 要求的 "mixed-unit status"）
        ratio = item["share_like_ratio"]
        if ratio >= _MIXED_MONTH_HIGH:
            item["unit_status"] = "share"
        elif ratio <= _MIXED_MONTH_LOW:
            item["unit_status"] = "lot"
        else:
            item["unit_status"] = "mixed"
    intra_month_mixed = [
        item["month"] for item in monthly if item["unit_status"] == "mixed"
    ]
    share_months = [item["month"] for item in monthly if item["unit_status"] == "share"]
    lot_months = [item["month"] for item in monthly if item["unit_status"] == "lot"]
    # 受影响范围：混合月里"同一个 symbol 同时出现两种单位"的只数（§22 要求）。
    affected_symbol_count = 0
    if intra_month_mixed:
        try:
            connection = _connect_market_db(path)
            try:
                affected_symbol_count = int(
                    connection.execute(
                        "SELECT count(*) FROM ("
                        "SELECT symbol, strftime(date, '%Y-%m') AS month, "
                        "count(DISTINCT CASE WHEN turnover / NULLIF(volume, 0) > ? "
                        "THEN 'lot' ELSE 'share' END) AS kinds "
                        "FROM daily_bars WHERE volume IS NOT NULL AND volume > 0 "
                        "AND turnover IS NOT NULL AND date BETWEEN ? AND ? "
                        "GROUP BY 1, 2 HAVING kinds > 1)"
                        ,
                        [
                            _LOT_LIKE_RATIO_THRESHOLD,
                            training_start.isoformat(),
                            training_end.isoformat(),
                        ],
                    ).fetchone()[0]
                    or 0
                )
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - 诊断字段尽力而为，不改变 verdict
            affected_symbol_count = 0
    regime_switch = bool(share_months and lot_months)
    findings: list[str] = []
    if not monthly:
        findings.append("volume_unit_check_impossible:no_rows_in_training_window")
    if intra_month_mixed:
        findings.append(f"mixed_volume_units_month:{intra_month_mixed[0]}")
    if regime_switch:
        findings.append(
            f"mixed_volume_units_regime_switch:{lot_months[0]}..{share_months[0]}"
        )
    if zero_rows:
        findings.append(f"empty_months_in_window:{zero_rows[0]}")
    verdict = (
        VERDICT_BLOCKED if not monthly or intra_month_mixed or regime_switch else VERDICT_PASS
    )
    return CheckResult(
        "volume_units",
        verdict,
        {
            "training_start": training_start.isoformat(),
            "training_end": training_end.isoformat(),
            "lot_like_threshold": _LOT_LIKE_RATIO_THRESHOLD,
            "mixed_month_bounds": [_MIXED_MONTH_LOW, _MIXED_MONTH_HIGH],
            "monthly": monthly,
            "share_like_months": share_months,
            "lot_like_months": lot_months,
            "mixed_months": intra_month_mixed,
            "unit_status_by_month": {
                item["month"]: item["unit_status"] for item in monthly
            },
            "affected_symbol_count": affected_symbol_count,
            "affected_date_range": (
                [intra_month_mixed[0], intra_month_mixed[-1]] if intra_month_mixed else []
            ),
            "affected_month_count": len(intra_month_mixed) + len(lot_months) + len(share_months),
        },
        findings,
    )


def check_feature_inputs(
    *,
    feature_columns: Sequence[str],
    market_db: str | Path,
    as_of: date,
    repo_root: str | Path,
    max_symbols: int = 300,
    warmup_days: int = 260,
    skip_reason: str = "",
    coverage_warn_below: float = 0.5,
) -> CheckResult:
    """D. Feature inputs —— 冻结 schema 的每一列当天能不能算出来。

    ``skip_reason`` 非空 = 显式跳过探针（测试/离线环境），如实记 WARN，
    绝不把"没检查"写成 PASS。
    """
    if not feature_columns:
        return CheckResult(
            "feature_inputs", VERDICT_BLOCKED, {}, ["feature_schema_missing"]
        )
    if skip_reason:
        return CheckResult(
            "feature_inputs",
            VERDICT_WARN,
            {"feature_column_count": len(feature_columns), "probe_skipped": True},
            [f"feature_probe_skipped:{skip_reason}"],
        )
    from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint
    from stock_analyzer.alpha_v2.research.panel import load_daily_panel
    from stock_analyzer.alpha_v2.validation.feature_frame import daily_feature_frame

    market_path = Path(str(market_db))
    if not market_path.is_absolute():
        market_path = Path(repo_root) / market_path
    try:
        panel = load_daily_panel(
            market_db=market_path,
            window_start=as_of,
            window_end=as_of,
            warmup_days=int(warmup_days),
            max_symbols=int(max_symbols),
        )
        if as_of not in panel.calendar:
            return CheckResult(
                "feature_inputs",
                VERDICT_BLOCKED,
                {"as_of": as_of.isoformat()},
                ["feature_probe_as_of_not_trading_day"],
            )
        eligible = list(panel.pit_universe(as_of=as_of).eligible_symbols)
        if not eligible:
            return CheckResult(
                "feature_inputs",
                VERDICT_BLOCKED,
                {"as_of": as_of.isoformat()},
                ["feature_probe_universe_empty"],
            )
        decisions = [DecisionPoint(symbol, as_of) for symbol in eligible]
        frame = daily_feature_frame(panel, decisions)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "feature_inputs",
            VERDICT_BLOCKED,
            {"as_of": as_of.isoformat()},
            [f"feature_probe_failed:{exc.__class__.__name__}:{exc}"],
        )
    missing = [column for column in feature_columns if column not in frame.columns]
    coverage: dict[str, float] = {}
    if not frame.empty:
        total = float(len(frame))
        for column in feature_columns:
            if column in frame.columns:
                coverage[column] = round(float(frame[column].notna().sum()) / total, 6)
    empty_columns = sorted(column for column, value in coverage.items() if value <= 0.0)
    low_columns = sorted(
        column for column, value in coverage.items() if 0.0 < value < coverage_warn_below
    )
    findings: list[str] = []
    if missing:
        findings.append(f"feature_columns_uncomputable:{missing[0]} (共 {len(missing)} 列)")
    if empty_columns:
        findings.append(f"feature_columns_all_null:{empty_columns[0]} (共 {len(empty_columns)} 列)")
    if low_columns:
        findings.append(f"feature_columns_low_coverage:{low_columns[0]} (共 {len(low_columns)} 列)")
    if missing or empty_columns:
        verdict = VERDICT_BLOCKED
    elif low_columns:
        verdict = VERDICT_WARN
    else:
        verdict = VERDICT_PASS
    return CheckResult(
        "feature_inputs",
        verdict,
        {
            "as_of": as_of.isoformat(),
            "feature_column_count": len(feature_columns),
            "probed_rows": int(len(frame)),
            "missing_columns": missing,
            "all_null_columns": empty_columns,
            "low_coverage_columns": low_columns,
            "coverage_min": round(min(coverage.values()), 6) if coverage else None,
            "coverage_median": (
                round(sorted(coverage.values())[len(coverage) // 2], 6) if coverage else None
            ),
        },
        findings,
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def run_production_preflight(
    *,
    config: object,
    repo_root: str | Path,
    market_db: str | Path,
    training_start: date,
    training_end: date,
    feature_columns: Sequence[str],
    model_dir: str | Path | None = None,
    feature_probe_skipped_reason: str = "",
    max_feature_probe_symbols: int = 300,
    now: datetime | None = None,
) -> dict[str, object]:
    """执行全部检查并组装审计载荷（纯计算 + 只读 IO，不落盘）。"""
    if training_end < training_start:
        raise PreflightError(
            f"training_end({training_end}) 早于 training_start({training_start})"
        )
    generated_at = (now or datetime.now().astimezone()).isoformat()
    checks: list[CheckResult] = [
        check_runtime_identity(repo_root),
        check_safety_flags(config),
        check_market_db(
            market_db, training_start=training_start, training_end=training_end
        ),
        check_volume_units(market_db, training_start=training_start, training_end=training_end),
    ]
    market_check = checks[2]
    latest_date_text = str(market_check.facts.get("latest_trade_date", "") or "")
    probe_as_of: date | None = None
    if latest_date_text:
        try:
            probe_as_of = date.fromisoformat(latest_date_text[:10])
        except ValueError:
            probe_as_of = None
    elif training_end:
        probe_as_of = training_end
    if probe_as_of is None:
        checks.append(
            CheckResult(
                "feature_inputs",
                VERDICT_BLOCKED,
                {},
                ["feature_probe_as_of_unresolvable"],
            )
        )
    else:
        checks.append(
            check_feature_inputs(
                feature_columns=feature_columns,
                market_db=market_db,
                as_of=probe_as_of,
                repo_root=repo_root,
                max_symbols=int(max_feature_probe_symbols),
                skip_reason=feature_probe_skipped_reason,
            )
        )

    verdict = VERDICT_PASS
    for check in checks:
        if _VERDICT_ORDER[check.verdict] > _VERDICT_ORDER[verdict]:
            verdict = check.verdict
    blocking = [f"{c.name}:{f}" for c in checks if c.verdict == VERDICT_BLOCKED for f in c.findings]
    warnings = [f"{c.name}:{f}" for c in checks if c.verdict == VERDICT_WARN for f in c.findings]
    training_window = {
        "start": training_start.isoformat(),
        "end": training_end.isoformat(),
    }
    data_identity = {
        "market_db": str(market_db),
        "latest_trade_date": latest_date_text,
        "total_rows": market_check.facts.get("total_rows"),
        "training_window_hash": canonical_hash(training_window),
    }
    payload: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "generated_at": generated_at,
        "verdict": verdict,
        "blocking_findings": blocking,
        "warnings": warnings,
        "facts": {check.name: check.facts for check in checks},
        "runtime_identity": checks[0].facts.get("identity", {}),
        "data_identity": data_identity,
        "training_window": training_window,
        "checks": [
            {"name": c.name, "verdict": c.verdict, "findings": c.findings} for c in checks
        ],
        "model_dir": str(model_dir or ""),
        "feature_schema_identity": {
            "feature_column_count": len(feature_columns),
            "feature_columns_hash": canonical_hash({"columns": list(feature_columns)}),
        },
    }
    payload["preflight_hash"] = preflight_hash_of(payload)
    return payload


def write_preflight_audit(payload: Mapping[str, object], *, audit_root: str | Path) -> Path:
    stamp = str(payload.get("generated_at", "") or "").replace(":", "").replace("+", "_")
    target = Path(audit_root) / f"production_preflight_{stamp or 'unknown'}.json"
    return write_json_atomic(target, payload)


# ---------------------------------------------------------------------------
# Validation freeze 硬门（§25：唯一可开 epoch 的入口做 gate）
# ---------------------------------------------------------------------------


def load_preflight_report(path: str | Path) -> dict[str, object]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"preflight 报告不可读: {target}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise PreflightError(f"preflight 报告不是 JSON 对象: {target}")
    if payload.get("schema") != PREFLIGHT_SCHEMA:
        raise PreflightError(
            f"preflight schema 不符: {payload.get('schema')!r}（期望 {PREFLIGHT_SCHEMA}）"
        )
    recorded = str(payload.get("preflight_hash", "") or "")
    if not recorded or preflight_hash_of(payload) != recorded:
        raise PreflightError(
            f"preflight 报告内容与 preflight_hash 不一致（{target}）——工件被事后修改"
        )
    return payload


def assert_preflight_gate(
    *,
    report_path: str | Path,
    runtime_code_commit: str,
    training_window: Sequence[str] | None,
    max_age_hours: float,
    accept_warn: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """校验 preflight 报告满足开 epoch 的三要素：同代码、同训练窗、新鲜且未 BLOCKED。

    返回用于写入 freeze manifest 的 ``production_preflight`` 块。
    失败抛 :class:`PreflightError`（CLI 层翻译成 exit 7）。
    """
    payload = load_preflight_report(report_path)
    verdict = str(payload.get("verdict", "") or "")
    if verdict == VERDICT_BLOCKED:
        blocking = payload.get("blocking_findings") or []
        raise PreflightError(
            "Production Data Preflight = BLOCKED，禁止开启生产 epoch："
            + "; ".join(str(item) for item in blocking[:5])
        )
    if verdict not in (VERDICT_PASS, VERDICT_WARN):
        raise PreflightError(f"preflight verdict 非法: {verdict!r}")
    if verdict == VERDICT_WARN and not accept_warn:
        warnings = payload.get("warnings") or []
        raise PreflightError(
            "Production Data Preflight = WARN，需显式 --accept-preflight-warn 才允许继续："
            + "; ".join(str(item) for item in warnings[:5])
        )
    generated_at = str(payload.get("generated_at", "") or "")
    try:
        generated = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise PreflightError(f"preflight generated_at 不可解析: {generated_at!r}") from exc
    reference = now or datetime.now().astimezone()
    if generated.tzinfo is None:
        generated = generated.astimezone()
    age_hours = (reference - generated).total_seconds() / 3600.0
    if age_hours > float(max_age_hours):
        raise PreflightError(
            f"preflight 报告过旧（{age_hours:.1f}h > {max_age_hours}h）："
            "数据状态可能已变，请重跑 scripts/alpha_v2_production_preflight.py"
        )
    report_identity = (payload.get("runtime_identity") or {}).get("code_commit", "")
    if str(report_identity or "").strip() != str(runtime_code_commit or "").strip():
        raise PreflightError(
            f"preflight 报告与当前运行代码不一致: preflight={report_identity!r} "
            f"runtime={runtime_code_commit!r}——换代码必须重跑 preflight"
        )
    reported_window = payload.get("training_window") or {}
    reported_pair = [str(reported_window.get("start", "")), str(reported_window.get("end", ""))]
    if training_window is None:
        raise PreflightError(
            "冻结模型工件缺少 provenance.window，无法证明 preflight 检查的就是训练窗"
        )
    expected_pair = [str(training_window[0]), str(training_window[1])]
    if reported_pair != expected_pair:
        raise PreflightError(
            f"preflight 训练窗与冻结模型不一致: preflight={reported_pair} model={expected_pair}"
            "（检查 A 窗口、训练 B 窗口 = §23 明令禁止）"
        )
    return {
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path),
        "preflight_hash": str(payload.get("preflight_hash", "")),
        "verdict": verdict,
        "generated_at": generated_at,
        "training_window": reported_pair,
        "data_identity": payload.get("data_identity", {}),
        "warnings": list(payload.get("warnings") or []),
    }


__all__ = [
    "PREFLIGHT_SCHEMA",
    "VERDICT_BLOCKED",
    "VERDICT_PASS",
    "VERDICT_WARN",
    "CheckResult",
    "PreflightError",
    "assert_preflight_gate",
    "canonical_hash",
    "check_feature_inputs",
    "check_market_db",
    "check_runtime_identity",
    "check_safety_flags",
    "check_volume_units",
    "file_sha256",
    "load_preflight_report",
    "preflight_hash_of",
    "run_production_preflight",
    "write_preflight_audit",
]
