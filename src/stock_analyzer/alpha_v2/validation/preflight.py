"""Alpha V2 M4-L / R1：Production Data Preflight（NAS 上线前只读数据体检）。

**为什么存在**：M4-H 已证明历史证据是 MIXED、volume 单位在 2025-09 后混合。
正式 ``alpha_v2_epoch_001`` 开启前必须回答"当下这份生产数据配不配训练出可用的
冻结影子模型"，并把结论钉成可审计工件；``BLOCKED`` 时**不允许**开 epoch。

本模块**只读**：不写数据库、不改特征、不修数据（数据治理是独立动作）。

R1 相对首轮新增（外部复核 BLOCKER 2-6）：

- **精确模型绑定**：``--model-dir`` 时记录完整 ``model_identity``（model_id /
  artifact_hash / feature_schema_hash / model_training_code_commit /
  provenance.window / artifact_verified）；validation freeze 逐项对账，
  杜绝"Preflight 验 Model A、Freeze 冻 Model B"。
- **训练数据内容指纹**：重算 ``training_data_fingerprint`` 并与冻结模型 provenance
  比对（同窗同数据才算同一次检查）。
- **特征 fill-zero 假健康**：复用 ``feature_diagnosis`` 的四类分类
  （UPSTREAM_NOT_POPULATED / FILL_ZERO_ARTIFACT / DATA_MISSINGNESS / REAL_CONSTANT），
  required 特征命中前三类 = BLOCKED，REAL_CONSTANT = WARN；诊断窗口取多日截面。
- **生产链前置**：``production_pipeline_prerequisites`` 检查 week5 / nightly /
  alpha_v2 开关与时间窗可达性（nightly.enabled=false ⇒ funnel 不会被链接 ⇒ 每天都
  missing，必须 BLOCKED 而不是只写在风险清单里）。
- **volume 判别价格归一化**：``unit_scale = turnover / (volume × close)``，
  ≈1 为股、≈100 为手；旧的绝对阈值 ``turnover/volume > 100`` 对高价股/低价股
  都会误判（外部复核 BLOCKER 6）。affected 计数同时给 distinct symbols 与
  symbol-month pairs，语义不再混用。

R1.1 相对 R1 新增（训练 provenance 封存）：

- **工件哈希版本门**：生产 preflight 只接受 ``artifact_hash_version=v2``
  （训练 provenance 进入受保护身份）且封存项齐备的工件；v1 一律 BLOCKED；
- **指纹契约逐项对账**：重算指纹时用**模型记录**的
  ``window / warmup_days / source_window / columns / fingerprint_version``，
  五项任一不符即 BLOCKED（不是只比 digest）。

判定分级：``PASS`` / ``WARN`` / ``BLOCKED``（BLOCKED 必须带非零 exit code）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic

PREFLIGHT_SCHEMA = "alpha_v2_production_preflight.v1"
VERDICT_PASS = "PASS"
VERDICT_WARN = "WARN"
VERDICT_BLOCKED = "BLOCKED"
_VERDICT_ORDER = {VERDICT_PASS: 0, VERDICT_WARN: 1, VERDICT_BLOCKED: 2}

# ── volume 单位（R1：价格归一化）──────────────────────────────────────────────
# unit_scale = turnover / (volume * reference_price)：以股计量 ≈ 1，以手 ≈ 100。
# 判别阈值取 10（2 与 50 的几何中点），两侧都留出宽裕区间；
# 参考价用 close（数据契约里最可信的当日价），缺失时退回 OHLC 代表价。
_UNIT_SCALE_SPLIT = 10.0
_UNIT_SCALE_REFERENCE_FALLBACK = "coalesce(close, (open + high + low) / 3.0)"
# 月内混合判定开区间（share-like 比例落在此区间 = 当月两种单位并存）。
_MIXED_MONTH_LOW = 0.2
_MIXED_MONTH_HIGH = 0.8
# 尾段残缺：最新交易日的行数低于近 20 日中位数的该比例即判"尾段不完整"。
_TAIL_FRAGMENT_RATIO = 0.5
# 特征诊断窗口：训练窗末端回溯的交易日数与抽样上限（外部复核 §3.2：
# 不能只用单日样本判断 constant）。
_FEATURE_DIAGNOSIS_TRADING_DAYS = 40
_FEATURE_DIAGNOSIS_MAX_SYMBOLS = 300


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
    """除 ``preflight_hash`` 字段本身外的 canonical JSON sha256（自锚定哈希）。"""
    body = {key: value for key, value in payload.items() if key != "preflight_hash"}
    return canonical_hash(body)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# A. Runtime / Build 身份
# ---------------------------------------------------------------------------


def check_runtime_identity(repo_root: str | Path) -> CheckResult:
    """复用 R4/R4.1 的唯一身份实现，不复制第二套逻辑。"""
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


# ---------------------------------------------------------------------------
# B. 安全开关
# ---------------------------------------------------------------------------


def check_safety_flags(config: object) -> CheckResult:
    """必须保持 shadow 安全组合（含 alpha_v2 自己的两个开关）。"""
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


# ---------------------------------------------------------------------------
# B2. 生产链前置与时间窗可达性（R1 BLOCKER 5）
# ---------------------------------------------------------------------------


def check_production_prerequisites(config: object) -> CheckResult:
    """live clean OOS 的**实际生产依赖**是否启用，以及窗口能不能接上。

    funnel → link 链条要求：Week5 全市场自动化夜扫 + 正式晚报发布。任一关闭时
    funnel 不会被链接 ⇒ capture 每天 fail-closed 记 missing ⇒ clean OOS 永远 0。
    这不能只写在风险清单里（外部复核 BLOCKER 5），必须在开 epoch 前 BLOCKED。
    """
    week5 = getattr(config, "week5", None)
    nightly = getattr(config, "nightly", None)
    scheduler = getattr(config, "scheduler", None)
    alpha = getattr(config, "alpha_v2", None)
    facts: dict[str, object] = {
        "week5_enabled": bool(getattr(week5, "enabled", False)),
        "week5_auto_run": bool(getattr(week5, "auto_run", False)),
        "full_market_automation_enabled": bool(
            getattr(week5, "full_market_automation_enabled", False)
        ),
        "nightly_enabled": bool(getattr(nightly, "enabled", False)),
        "alpha_v2_enabled": bool(getattr(alpha, "enabled", False)),
        "alpha_v2_shadow_only": bool(getattr(alpha, "shadow_only", False)),
        "alpha_v2_enforce_final_selection": bool(getattr(alpha, "enforce_final_selection", True)),
        "night_scan_start_time": str(getattr(scheduler, "week5_night_scan_time", "")),
        "nightly_last_scan_start_time": str(getattr(nightly, "last_scan_start_time", "")),
        "alpha_live_cycle_start_time": str(getattr(alpha, "live_cycle_start_time", "")),
        "alpha_live_cycle_latest_time": str(getattr(alpha, "live_cycle_latest_time", "")),
    }
    findings: list[str] = []
    if not facts["week5_enabled"]:
        findings.append("prerequisite:week5_enabled_must_be_true")
    if not facts["week5_auto_run"]:
        findings.append("prerequisite:week5_auto_run_must_be_true")
    if not facts["full_market_automation_enabled"]:
        findings.append("prerequisite:full_market_automation_enabled_must_be_true")
    if not facts["nightly_enabled"]:
        findings.append("prerequisite:nightly_enabled_must_be_true")
    # alpha_v2.enabled：config_hash 覆盖它；开 epoch 时关、之后打开会造成 runtime
    # identity 漂移（capture 每天 exit 3）。所以生产冻结/开 epoch 前就必须为 true。
    if not facts["alpha_v2_enabled"]:
        findings.append("prerequisite:alpha_v2_enabled_must_be_true")
    if not facts["alpha_v2_shadow_only"]:
        findings.append("prerequisite:alpha_v2_shadow_only_must_be_true")
    if facts["alpha_v2_enforce_final_selection"]:
        findings.append("prerequisite:alpha_v2_enforce_final_selection_must_be_false")
    # 时间窗可达性：alpha 循环最晚必须晚于夜扫最晚起跑（否则晚跑的夜扫永远赶不上
    # 当天的捕获窗口；跨零点即 backfill，当天永失 clean）。
    latest = _parse_hhmm_int(facts["alpha_live_cycle_latest_time"])
    last_scan = _parse_hhmm_int(facts["nightly_last_scan_start_time"]) or _parse_hhmm_int(
        facts["night_scan_start_time"]
    )
    if latest is None:
        findings.append("prerequisite:alpha_live_cycle_latest_time_unparsable")
    elif last_scan is not None and latest <= last_scan:
        findings.append(
            "prerequisite:alpha_cycle_window_unreachable"
            f"({facts['alpha_live_cycle_latest_time']}<={facts['nightly_last_scan_start_time']})"
        )
    facts["window_reachable"] = not any(
        finding.startswith("prerequisite:alpha_cycle_window_unreachable")
        or finding.endswith("latest_time_unparsable")
        for finding in findings
    )
    verdict = VERDICT_BLOCKED if findings else VERDICT_PASS
    return CheckResult("production_pipeline_prerequisites", verdict, facts, findings)


def _parse_hhmm_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    try:
        hour, minute = text.split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# C. Market DB
# ---------------------------------------------------------------------------


def _connect_market_db(market_db: str | Path):
    import duckdb

    return duckdb.connect(str(market_db), read_only=True)


def check_market_db(
    market_db: str | Path, *, training_start: date | None, training_end: date | None
) -> CheckResult:
    """存在/可读/最新完整交易日/广度/重复主键/尾段残缺。"""
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
        total_rows = int(connection.execute("SELECT count(*) FROM daily_bars").fetchone()[0] or 0)
        duplicates = int(
            connection.execute(
                "SELECT count(*) - count(DISTINCT (symbol, date)) FROM daily_bars"
            ).fetchone()[0]
            or 0
        )
        trailing = [
            {"date": str(row[0]), "rows": int(row[1])}
            for row in connection.execute(
                "SELECT date, count(*) AS rows FROM daily_bars GROUP BY 1 ORDER BY 1 DESC LIMIT 20"
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
    tail_fragment = bool(median_rows > 0 and latest_rows <= median_rows * _TAIL_FRAGMENT_RATIO)
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


# ---------------------------------------------------------------------------
# E. Volume unit gate（R1：价格归一化）
# ---------------------------------------------------------------------------


def check_volume_units(
    market_db: str | Path, *, training_start: date, training_end: date
) -> CheckResult:
    """窗口内是否存在两种 volume 单位（价格归一化判别）。

    ``unit_scale = turnover / (volume × reference_price)``：以股计 ≈ 1、以手 ≈ 100。
    逐自然月统计 share-like 比例（``unit_scale < 10`` 的行占比）：

    - 月内比例落 (0.2, 0.8) → 当月两种单位并存（BLOCKED）；
    - 窗口内同时存在 ≥0.8 与 ≤0.2 的月份 → 单位切换（BLOCKED）。
    """
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
    reference = _UNIT_SCALE_REFERENCE_FALLBACK
    scale_expr = f"(turnover / NULLIF(volume * ({reference}), 0))"
    share_like_expr = f"avg(CASE WHEN {scale_expr} < ? THEN 1.0 ELSE 0.0 END)"
    try:
        columns = {str(row[0]) for row in connection.execute("DESCRIBE daily_bars").fetchall()}
        for required in ("volume", "turnover", "date", "symbol", "close"):
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
                "share_like_ratio": round(float(row[3]), 6),
                "lot_like_ratio": round(1.0 - float(row[3]), 6),
                "unit_scale_median": (round(float(row[4]), 4) if row[4] is not None else None),
            }
            for row in connection.execute(
                f"SELECT strftime(date, '%Y-%m') AS month, count(*) AS rows, "
                f"count(DISTINCT symbol) AS symbols, {share_like_expr} AS share_like_ratio, "
                f"median({scale_expr}) AS unit_scale_median "
                "FROM daily_bars WHERE volume IS NOT NULL AND volume > 0 "
                "AND turnover IS NOT NULL AND date BETWEEN ? AND ? "
                "GROUP BY 1 ORDER BY 1",
                [
                    _UNIT_SCALE_SPLIT,
                    training_start.isoformat(),
                    training_end.isoformat(),
                ],
            ).fetchall()
        ]
        for item in monthly:
            ratio = float(item["share_like_ratio"])
            if ratio >= _MIXED_MONTH_HIGH:
                item["unit_status"] = "share"
            elif ratio <= _MIXED_MONTH_LOW:
                item["unit_status"] = "lot"
            else:
                item["unit_status"] = "mixed"
        intra_month_mixed = [item["month"] for item in monthly if item["unit_status"] == "mixed"]
        share_months = [item["month"] for item in monthly if item["unit_status"] == "share"]
        lot_months = [item["month"] for item in monthly if item["unit_status"] == "lot"]
        affected_symbols = 0
        affected_pairs = 0
        if intra_month_mixed:
            row = connection.execute(
                "SELECT count(DISTINCT symbol), count(*) FROM ("
                f"SELECT symbol, strftime(date, '%Y-%m') AS month, "
                f"count(DISTINCT CASE WHEN {scale_expr} < ? THEN 'share' ELSE 'lot' END) "
                "AS kinds "
                "FROM daily_bars WHERE volume IS NOT NULL AND volume > 0 "
                "AND turnover IS NOT NULL AND date BETWEEN ? AND ? "
                "GROUP BY 1, 2 HAVING kinds > 1)",
                [
                    _UNIT_SCALE_SPLIT,
                    training_start.isoformat(),
                    training_end.isoformat(),
                ],
            ).fetchone()
            affected_symbols = int(row[0] or 0)
            affected_pairs = int(row[1] or 0)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "volume_units",
            VERDICT_BLOCKED,
            {"path": str(path)},
            [f"volume_unit_check_failed:{exc.__class__.__name__}:{exc}"],
        )
    finally:
        connection.close()
    zero_rows = [item["month"] for item in monthly if item["rows"] == 0]
    regime_switch = bool(share_months and lot_months)
    findings: list[str] = []
    if not monthly:
        findings.append("volume_unit_check_impossible:no_rows_in_training_window")
    if intra_month_mixed:
        findings.append(f"mixed_volume_units_month:{intra_month_mixed[0]}")
    if regime_switch:
        findings.append(f"mixed_volume_units_regime_switch:{lot_months[0]}..{share_months[0]}")
    if zero_rows:
        findings.append(f"empty_months_in_window:{zero_rows[0]}")
    verdict = VERDICT_BLOCKED if not monthly or intra_month_mixed or regime_switch else VERDICT_PASS
    return CheckResult(
        "volume_units",
        verdict,
        {
            "formula": "turnover / (volume * reference_price)",
            "reference_price": reference,
            "unit_scale_split": _UNIT_SCALE_SPLIT,
            "mixed_month_bounds": [_MIXED_MONTH_LOW, _MIXED_MONTH_HIGH],
            "training_start": training_start.isoformat(),
            "training_end": training_end.isoformat(),
            "monthly": monthly,
            "share_like_months": share_months,
            "lot_like_months": lot_months,
            "mixed_months": intra_month_mixed,
            "unit_status_by_month": {item["month"]: item["unit_status"] for item in monthly},
            "affected_symbol_count": affected_symbols,
            "affected_symbol_month_count": affected_pairs,
            "affected_date_range": (
                [intra_month_mixed[0], intra_month_mixed[-1]] if intra_month_mixed else []
            ),
        },
        findings,
    )


# ---------------------------------------------------------------------------
# D. Feature inputs（R1：接入 feature_diagnosis 四类分类）
# ---------------------------------------------------------------------------


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
    diagnosis_days: int = _FEATURE_DIAGNOSIS_TRADING_DAYS,
) -> CheckResult:
    """冻结 schema 的每一列当天能不能算出来（含 fill-zero 假健康诊断）。

    分层判据（R1）：

    - 列完全算不出来 / 全空 → **BLOCKED**（原有）；
    - ``UPSTREAM_NOT_POPULATED`` / ``FILL_ZERO_ARTIFACT`` / ``DATA_MISSINGNESS``
      命中 **required 模型特征** → **BLOCKED**（fill-zero 会把 ``notna()`` 刷成
      100%，只看覆盖率必假 PASS）；
    - ``REAL_CONSTANT`` → **WARN**（按项目现有治理口径不判死，但要可见）；
    - 非 required 列的同名分类 → WARN（如实记录，不阻断）。
    """
    if not feature_columns:
        return CheckResult("feature_inputs", VERDICT_BLOCKED, {}, ["feature_schema_missing"])
    if skip_reason:
        return CheckResult(
            "feature_inputs",
            VERDICT_WARN,
            {"feature_column_count": len(feature_columns), "probe_skipped": True},
            [f"feature_probe_skipped:{skip_reason}"],
        )
    from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint
    from stock_analyzer.alpha_v2.research.panel import load_daily_panel
    from stock_analyzer.alpha_v2.validation.feature_diagnosis import (
        CLASS_DATA_MISSINGNESS,
        CLASS_FILL_ZERO_ARTIFACT,
        CLASS_REAL_CONSTANT,
        CLASS_UPSTREAM_NOT_POPULATED,
        diagnose_features,
        probe_market_duckdb_sources,
    )
    from stock_analyzer.alpha_v2.validation.feature_frame import daily_feature_frame

    market_path = Path(str(market_db))
    if not market_path.is_absolute():
        market_path = Path(repo_root) / market_path
    diagnosis_days = max(1, int(diagnosis_days))
    diagnosis_dates: list[date] = []
    # 诊断窗是多日截面（外部复核 §3.2：单日样本会把"当天刚好相同"误判成长期
    # constant）。面板窗口必须覆盖整段诊断窗，否则 calendar 只有一天。
    window_start = as_of - timedelta(days=int(diagnosis_days * 1.6) + 5)
    try:
        panel = load_daily_panel(
            market_db=market_path,
            window_start=window_start,
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
        calendar_index = panel.calendar_index(as_of)
        start_index = max(0, calendar_index - diagnosis_days + 1)
        diagnosis_dates = list(panel.calendar[start_index : calendar_index + 1])
        decisions = [DecisionPoint(symbol, day) for day in diagnosis_dates for symbol in eligible]
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

    # ── fill-zero 假健康诊断（多日截面）────────────────────────────────────
    diagnosis_payload: dict[str, object] = {}
    diagnosis_blocked: list[str] = []
    diagnosis_warned: list[str] = []
    try:
        upstream_probe = probe_market_duckdb_sources(market_path)
        report = diagnose_features(
            frame, columns=list(feature_columns), upstream_probe=upstream_probe
        )
        diagnosis_payload = report.to_payload()
        required = set(feature_columns)
        for row in report.rows:
            classification = row.classification
            target = diagnosis_blocked if row.column in required else diagnosis_warned
            if classification in (
                CLASS_UPSTREAM_NOT_POPULATED,
                CLASS_FILL_ZERO_ARTIFACT,
                CLASS_DATA_MISSINGNESS,
            ):
                target.append(f"{row.column}:{classification}")
            elif classification == CLASS_REAL_CONSTANT:
                diagnosis_warned.append(f"{row.column}:{CLASS_REAL_CONSTANT}")
    except Exception as exc:  # noqa: BLE001 - 诊断不可用本身按 WARN（覆盖率门仍在）
        diagnosis_warned.append(f"feature_diagnosis_failed:{exc.__class__.__name__}:{exc}")
    if diagnosis_blocked:
        findings.append(
            f"feature_health_blocked:{diagnosis_blocked[0]} (共 {len(diagnosis_blocked)} 列)"
        )
    if diagnosis_warned:
        findings.append(
            f"feature_health_warn:{diagnosis_warned[0]} (共 {len(diagnosis_warned)} 列)"
        )

    if missing or empty_columns or diagnosis_blocked:
        verdict = VERDICT_BLOCKED
    elif low_columns or diagnosis_warned:
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
            "diagnosis_days": len(diagnosis_dates),
            "missing_columns": missing,
            "all_null_columns": empty_columns,
            "low_coverage_columns": low_columns,
            "coverage_min": round(min(coverage.values()), 6) if coverage else None,
            "coverage_median": (
                round(sorted(coverage.values())[len(coverage) // 2], 6) if coverage else None
            ),
            "diagnosis": diagnosis_payload,
            "diagnosis_blocked_columns": diagnosis_blocked,
            "diagnosis_warned_columns": diagnosis_warned,
        },
        findings,
    )


# ---------------------------------------------------------------------------
# F. 模型身份与训练数据指纹（R1 BLOCKER 2/4）
# ---------------------------------------------------------------------------


def check_model_identity(model_dir: str | Path) -> CheckResult:
    """``--model-dir`` 的完整身份（含内容完整性 + 训练 provenance 封存验证）。

    R1.1：生产 preflight 只接受**封存训练 provenance**的工件（``artifact_hash_version
    = v2`` 且 window / warmup_days / source_window / training_data_fingerprint /
    rows / columns 齐备）。v1 工件（这些字段不受工件哈希保护，可事后改写）在这里
    就是 BLOCKED——"检查的数据"与"训练的数据"之间必须有一条不可篡改的链。
    """
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        ARTIFACT_HASH_VERSION_V2,
        frozen_model_identity_payload,
        load_frozen_model,
        missing_sealed_provenance_keys,
    )

    path = Path(model_dir)
    identity: dict[str, object] = {"model_dir": str(path)}
    try:
        payload = frozen_model_identity_payload(path)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "model_identity",
            VERDICT_BLOCKED,
            identity,
            [f"model_artifact_unreadable:{exc.__class__.__name__}:{exc}"],
        )
    provenance = dict(payload.get("provenance", {}) or {})
    window = provenance.get("window")
    source_window = provenance.get("source_window")
    warmup_days = provenance.get("warmup_days")
    artifact_hash_version = str(payload.get("artifact_hash_version", "") or "")
    identity.update(
        {
            "model_id": str(payload.get("model_id", "")),
            "model_artifact_hash": str(payload.get("artifact_hash", "")),
            "artifact_hash_version": artifact_hash_version,
            "feature_schema_hash": str(payload.get("feature_schema_hash", "")),
            "model_training_code_commit": str(payload.get("model_training_code_commit", "")),
            "provenance_window": (
                [str(window[0]), str(window[1])]
                if isinstance(window, (list, tuple)) and len(window) == 2
                else None
            ),
            "provenance_warmup_days": (
                int(warmup_days) if isinstance(warmup_days, (int, float)) else None
            ),
            "provenance_source_window": (
                [str(source_window[0]), str(source_window[1])]
                if isinstance(source_window, (list, tuple)) and len(source_window) == 2
                else None
            ),
            "training_data_fingerprint": str(provenance.get("training_data_fingerprint", "") or ""),
            "training_data_fingerprint_version": str(
                provenance.get("training_data_fingerprint_version", "") or ""
            ),
            "training_data_rows": provenance.get("training_data_rows"),
            "training_data_columns": list(provenance.get("training_data_columns", []) or []),
        }
    )
    findings: list[str] = []
    if not identity["model_id"] or not identity["model_artifact_hash"]:
        findings.append("model_identity_incomplete:model_id_or_artifact_hash_missing")
    if not identity["feature_schema_hash"]:
        findings.append("model_identity_incomplete:feature_schema_hash_missing")
    if not identity["model_training_code_commit"]:
        findings.append("model_identity_incomplete:model_training_code_commit_missing")
    if identity["provenance_window"] is None:
        findings.append("model_identity_incomplete:provenance_window_missing")
    # R1.1 封存门：版本 + 封存项齐备（缺一即是"训练输入身份不可证"）。
    if artifact_hash_version != ARTIFACT_HASH_VERSION_V2:
        findings.append(
            "model_artifact_unsealed_training_provenance:"
            f"artifact_hash_version={artifact_hash_version or '(缺失)'}"
            f"(要求 {ARTIFACT_HASH_VERSION_V2})"
        )
    sealed_missing = missing_sealed_provenance_keys(payload)
    if sealed_missing:
        findings.append(
            f"model_provenance_seal_incomplete:{sealed_missing[0]} (共 {len(sealed_missing)} 项)"
        )
    verified = False
    try:
        # 逐文件哈希 + 按记录版本复算 artifact_hash + 封存完整性
        load_frozen_model(path, require_sealed_provenance=True)
        verified = True
    except Exception as exc:  # noqa: BLE001
        findings.append(f"model_artifact_integrity_failed:{exc.__class__.__name__}:{exc}")
    identity["artifact_verified"] = verified
    verdict = VERDICT_BLOCKED if findings else VERDICT_PASS
    return CheckResult("model_identity", verdict, identity, findings)


def check_training_data_fingerprint(
    *,
    market_db: str | Path,
    model_identity: Mapping[str, object],
    training_start: date,
    training_end: date,
) -> CheckResult:
    """按**冻结模型记录的同一组参数**重算指纹并逐项比对（§8）。

    比对项：``training_data_fingerprint`` / ``fingerprint_version`` /
    ``source_window`` / 列清单 / 行数。任一不一致 = BLOCKED——"preflight 验过的
    数据"必须与"模型训练时读到的数据"是同一份，且是同一套契约算出来的。
    """
    from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (
        TrainingDataFingerprintError,
        compute_training_data_fingerprint,
    )

    model_fingerprint = str(model_identity.get("training_data_fingerprint", "") or "")
    model_version = str(model_identity.get("training_data_fingerprint_version", "") or "")
    model_columns = [str(item) for item in (model_identity.get("training_data_columns") or [])]
    model_source_window = model_identity.get("provenance_source_window")
    model_warmup = model_identity.get("provenance_warmup_days")
    model_rows = model_identity.get("training_data_rows")
    facts: dict[str, object] = {
        "model_training_data_fingerprint": model_fingerprint,
        "model_fingerprint_version": model_version,
        "model_source_window": model_source_window,
        "model_warmup_days": model_warmup,
        "model_training_data_columns": model_columns,
        "model_training_data_rows": model_rows,
    }
    if not model_fingerprint:
        return CheckResult(
            "training_data_fingerprint",
            VERDICT_BLOCKED,
            facts,
            ["model_provenance_missing_training_data_fingerprint"],
        )
    if model_warmup is None:
        # v1 工件（或手改 provenance）没有 warmup 身份 → 无法复算同一指纹。
        return CheckResult(
            "training_data_fingerprint",
            VERDICT_BLOCKED,
            facts,
            ["model_provenance_missing_warmup_days:无法复算训练输入指纹（v1/未封存工件）"],
        )
    if not model_columns:
        return CheckResult(
            "training_data_fingerprint",
            VERDICT_BLOCKED,
            facts,
            ["model_provenance_missing_training_data_columns"],
        )
    try:
        recomputed = compute_training_data_fingerprint(
            market_db,
            training_start=training_start,
            training_end=training_end,
            warmup_days=int(model_warmup),
        )
    except TrainingDataFingerprintError as exc:
        facts["error"] = str(exc)
        return CheckResult(
            "training_data_fingerprint",
            VERDICT_BLOCKED,
            facts,
            [f"training_data_fingerprint_uncomputable:{exc.__class__.__name__}"],
        )
    facts.update(
        {
            "recomputed_fingerprint": recomputed["fingerprint"],
            "recomputed_fingerprint_version": recomputed["fingerprint_version"],
            "recomputed_source_window": recomputed["source_window"],
            "recomputed_rows": recomputed["rows"],
            "recomputed_columns": recomputed["columns"],
            "recomputed_missing_optional_columns": recomputed["missing_optional_source_columns"],
            "columns": recomputed["columns"],
        }
    )
    mismatches: list[str] = []
    if str(recomputed["fingerprint"]) != model_fingerprint:
        mismatches.append(
            "training_data_fingerprint_mismatch:"
            f"model={model_fingerprint[:16]}… preflight={str(recomputed['fingerprint'])[:16]}…"
        )
    if model_version and str(recomputed["fingerprint_version"]) != model_version:
        mismatches.append(
            f"fingerprint_version:{model_version}!={recomputed['fingerprint_version']}"
        )
    if model_source_window is not None:
        recorded = [str(item) for item in model_source_window]
        if recorded != [str(item) for item in recomputed["source_window"]]:
            mismatches.append(f"source_window:{recorded}!={recomputed['source_window']}")
    if model_columns != [str(item) for item in recomputed["columns"]]:
        mismatches.append(
            f"training_data_columns:{len(model_columns)} 列 != {len(recomputed['columns'])} 列"
        )
    if model_rows is not None and int(model_rows) != int(recomputed["rows"]):
        mismatches.append(f"training_data_rows:{model_rows}!={recomputed['rows']}")
    if mismatches:
        return CheckResult("training_data_fingerprint", VERDICT_BLOCKED, facts, mismatches)
    return CheckResult("training_data_fingerprint", VERDICT_PASS, facts, [])


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
    max_feature_probe_symbols: int = _FEATURE_DIAGNOSIS_MAX_SYMBOLS,
    now: datetime | None = None,
) -> dict[str, object]:
    """执行全部检查并组装审计载荷（纯计算 + 只读 IO，不落盘）。"""
    if training_end < training_start:
        raise PreflightError(f"training_end({training_end}) 早于 training_start({training_start})")
    generated_at = (now or datetime.now().astimezone()).isoformat()
    checks: list[CheckResult] = [
        check_runtime_identity(repo_root),
        check_safety_flags(config),
        check_production_prerequisites(config),
        check_market_db(market_db, training_start=training_start, training_end=training_end),
        check_volume_units(market_db, training_start=training_start, training_end=training_end),
    ]
    model_identity: dict[str, object] = {}
    if model_dir:
        model_check = check_model_identity(model_dir)
        model_identity = dict(model_check.facts)
        checks.append(model_check)
        checks.append(
            check_training_data_fingerprint(
                market_db=market_db,
                model_identity=model_identity,
                training_start=training_start,
                training_end=training_end,
            )
        )
    else:
        checks.append(
            CheckResult(
                "model_identity",
                VERDICT_BLOCKED,
                {},
                ["model_dir_required:生产 preflight 必须绑定 --model-dir"],
            )
        )

    market_check = next(item for item in checks if item.name == "market_db")
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
            CheckResult("feature_inputs", VERDICT_BLOCKED, {}, ["feature_probe_as_of_unresolvable"])
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
    training_window = {"start": training_start.isoformat(), "end": training_end.isoformat()}
    volume_facts = next((item.facts for item in checks if item.name == "volume_units"), {})
    fingerprint_facts = next(
        (item.facts for item in checks if item.name == "training_data_fingerprint"), {}
    )
    data_identity = {
        "market_db": str(market_db),
        "latest_trade_date": latest_date_text,
        "total_rows": market_check.facts.get("total_rows"),
        "training_window_hash": canonical_hash(training_window),
        "training_data_fingerprint": (
            fingerprint_facts.get("recomputed_fingerprint")
            or model_identity.get("training_data_fingerprint")
        ),
        # R1.1：指纹契约身份（版本 / warmup / source 窗口）随报告落盘，供
        # validation freeze 与模型 provenance 逐项对账（§9）。优先取**本次复算**
        # 得到的值（那是 preflight 自己算出来的证据），没有复算时退回模型的声明值。
        "training_data_fingerprint_version": (
            fingerprint_facts.get("recomputed_fingerprint_version")
            or model_identity.get("training_data_fingerprint_version")
        ),
        "warmup_days": (
            fingerprint_facts.get("model_warmup_days")
            or model_identity.get("provenance_warmup_days")
        ),
        "source_window": (
            fingerprint_facts.get("recomputed_source_window")
            or model_identity.get("provenance_source_window")
        ),
        "training_data_rows": fingerprint_facts.get("recomputed_rows"),
        "training_data_columns": fingerprint_facts.get("recomputed_columns"),
        "volume_affected_symbol_count": volume_facts.get("affected_symbol_count"),
        "volume_affected_symbol_month_count": volume_facts.get("affected_symbol_month_count"),
    }
    payload: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "generated_at": generated_at,
        "verdict": verdict,
        "blocking_findings": blocking,
        "warnings": warnings,
        "facts": {check.name: check.facts for check in checks},
        "runtime_identity": checks[0].facts.get("identity", {}),
        "model_identity": model_identity,
        "data_identity": data_identity,
        "training_window": training_window,
        "checks": [{"name": c.name, "verdict": c.verdict, "findings": c.findings} for c in checks],
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
# Validation freeze 硬门（§25 + R1 §2.2：逐项绑定实际冻结对象）
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
    model_block: Mapping[str, object] | None,
    max_age_hours: float,
    accept_warn: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """校验 preflight 报告与"即将冻结的模型"逐项一致，满足才允许开 epoch。

    R1（BLOCKER 2）：不只比 verdict/年龄/commit/窗口，还要比
    ``model_id / model_artifact_hash / feature_schema_hash /
    model_training_code_commit / provenance.window / training_data_fingerprint``
    ——检查对象必须就是冻结对象。
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
    if model_block is None:
        raise PreflightError("冻结模型块缺失，无法证明 preflight 检查的就是这个模型")
    reported = dict(payload.get("model_identity") or {})
    if not reported:
        raise PreflightError(
            "preflight 报告没有 model_identity（必须用 --model-dir 绑定实际冻结模型）"
        )
    comparisons = {
        "model_id": (reported.get("model_id"), model_block.get("model_id")),
        "artifact_hash": (
            reported.get("model_artifact_hash"),
            model_block.get("artifact_hash"),
        ),
        "feature_schema_hash": (
            reported.get("feature_schema_hash"),
            model_block.get("feature_schema_hash"),
        ),
        "model_training_code_commit": (
            reported.get("model_training_code_commit"),
            model_block.get("model_training_code_commit"),
        ),
    }
    mismatches = [
        f"{key}:{left!r}!={right!r}"
        for key, (left, right) in comparisons.items()
        if str(left or "").strip() != str(right or "").strip()
    ]
    model_provenance = dict(model_block.get("provenance", {}) or {})
    model_window = model_provenance.get("window")
    expected_window = (
        [str(model_window[0]), str(model_window[1])]
        if isinstance(model_window, (list, tuple)) and len(model_window) == 2
        else None
    )
    reported_window = payload.get("training_window") or {}
    reported_pair = [
        str(reported_window.get("start", "")),
        str(reported_window.get("end", "")),
    ]
    if expected_window is None:
        mismatches.append("provenance_window_missing_in_model_block")
    elif reported_pair != expected_window:
        mismatches.append(f"training_window:{reported_pair}!={expected_window}")
    model_fingerprint = str(model_provenance.get("training_data_fingerprint", "") or "")
    reported_fingerprint = str(reported.get("training_data_fingerprint", "") or "")
    if not model_fingerprint:
        mismatches.append("training_data_fingerprint_missing_in_model_block")
    elif model_fingerprint != reported_fingerprint:
        mismatches.append(
            f"training_data_fingerprint:{reported_fingerprint[:16]}…!={model_fingerprint[:16]}…"
        )
    # R1.1：指纹契约身份（版本 / warmup / source 窗口）也逐项对账——只比 digest
    # 会漏掉"同一个 digest、不同的契约声明"这种自述与实现脱节。
    fingerprint_version = str(model_provenance.get("training_data_fingerprint_version", "") or "")
    reported_version = str(reported.get("training_data_fingerprint_version", "") or "")
    if not fingerprint_version:
        mismatches.append("training_data_fingerprint_version_missing_in_model_block")
    elif fingerprint_version != reported_version:
        mismatches.append(
            f"training_data_fingerprint_version:{reported_version!r}!={fingerprint_version!r}"
        )
    warmup_days = model_provenance.get("warmup_days")
    reported_warmup = (payload.get("data_identity") or {}).get("warmup_days")
    if warmup_days is None:
        mismatches.append("warmup_days_missing_in_model_block")
    elif reported_warmup is None or int(reported_warmup) != int(warmup_days):
        mismatches.append(f"warmup_days:{reported_warmup!r}!={warmup_days!r}")
    model_source_window = model_provenance.get("source_window")
    reported_source_window = (payload.get("data_identity") or {}).get("source_window")
    expected_source = (
        [str(model_source_window[0]), str(model_source_window[1])]
        if isinstance(model_source_window, (list, tuple)) and len(model_source_window) == 2
        else None
    )
    reported_source = (
        [str(reported_source_window[0]), str(reported_source_window[1])]
        if isinstance(reported_source_window, (list, tuple)) and len(reported_source_window) == 2
        else None
    )
    if expected_source is None:
        mismatches.append("source_window_missing_in_model_block")
    elif reported_source != expected_source:
        mismatches.append(f"source_window:{reported_source}!={expected_source}")
    if mismatches:
        raise PreflightError(
            "preflight 与本次冻结模型不一致（检查对象必须等于冻结对象）："
            + "; ".join(mismatches[:6])
        )
    return {
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path),
        "preflight_hash": str(payload.get("preflight_hash", "")),
        "verdict": verdict,
        "generated_at": generated_at,
        "training_window": reported_pair,
        "model_identity": {
            "model_id": reported.get("model_id"),
            "artifact_hash": reported.get("model_artifact_hash"),
            "feature_schema_hash": reported.get("feature_schema_hash"),
            "model_training_code_commit": reported.get("model_training_code_commit"),
            "artifact_hash_version": reported.get("artifact_hash_version"),
        },
        "training_data_fingerprint": reported_fingerprint,
        # R1.1：指纹契约身份进 production_preflight 块 → 受 freeze_manifest_hash 保护
        "training_data_fingerprint_version": fingerprint_version,
        "warmup_days": int(warmup_days) if warmup_days is not None else None,
        "source_window": expected_source,
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
    "check_model_identity",
    "check_production_prerequisites",
    "check_runtime_identity",
    "check_safety_flags",
    "check_training_data_fingerprint",
    "check_volume_units",
    "file_sha256",
    "load_preflight_report",
    "preflight_hash_of",
    "run_production_preflight",
    "write_preflight_audit",
]
