"""Alpha V2 **双价格序列契约**（P0：feature 可以 qfq，execution 必须 raw）。

**为什么必须成对出现**：项目价格契约（``stock_analyzer.backtest.price_contract``）写得很
清楚：

```text
Feature Series may be QFQ
Execution Series must be RAW
```

但 Alpha V2 的冻结/成熟链路此前只有一个 ``--market-db``。生产 NAS 的正式库是
``/app/artifacts/vendor_delta/market_delta.duckdb``，``price_series_mode=qfq``——于是**同一
份 qfq 序列同时喂给了特征、label、成交价、MAE/MFE 与超额**。这不是"口径不够精确"，
而是把复权价当成了当天挂单能成交的价：除权日的 -50% 跳变会被写成"真实亏损"，
涨跌停判定、可成交性、净收益、超额基准全部失真，且训练目标本身就被污染。

本模块把两个角色**显式化**，并给出唯一的一组守卫：

```text
execution 面板：必须 price_mode == raw 且 price_mode_certified == true，否则 FAIL CLOSED
feature   面板：模式必须可证（qfq / raw 之一），并且必须等于冻结模型声明的 feature mode
```

三条纪律：

1. **守卫在重活之前**：freeze 在构造完整特征矩阵之前就要拒绝，不允许"跑 90 分钟才报错"；
2. **绝不猜测**：缺失的 raw execution row 不会退回 qfq、不会用 qfq 反推——直接不可用并
   fail closed（见 :func:`assert_decisions_aligned`）；
3. **身份成对封存**：``feature_data_identity`` / ``execution_data_identity`` 两条独立身份
   （库、口径、认证结论、指纹版本、source window、指纹、列、行数），两条都进工件哈希。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stock_analyzer.alpha_v2.research.panel import (
    PRICE_MODE_QFQ,
    PRICE_MODE_RAW,
    DailyPanel,
    PriceModeCertification,
)

ROLE_FEATURE = "feature"
ROLE_EXECUTION = "execution"

#: execution 侧唯一允许的价格口径。
EXECUTION_PRICE_MODE_REQUIRED = PRICE_MODE_RAW
#: feature 侧允许的口径（qfq 是**正确**的 feature 口径，不是妥协；raw 也可）。
FEATURE_PRICE_MODE_ALLOWED: tuple[str, ...] = (PRICE_MODE_QFQ, PRICE_MODE_RAW)

#: 契约违例的 CLI 退出码。与 validation freeze 的 ``assert_execution_price_raw``
#: （exit_code=4）保持同一编号语义：4 = '执行价格口径不是 raw'。
EXIT_PRICE_SERIES_CONTRACT = 4

#: 两个角色由**同一份库**承担（旧单库形态）：允许，但必须在 provenance 里自曝。
DB_ROLE_BINDING_LEGACY = "legacy_single_db"
DB_ROLE_BINDING_DUAL = "dual_source"

DEFAULT_MARKET_DB = "artifacts/warehouse/market.duckdb"

# 认证证据里进审计的身份字段（固定顺序 + 白名单）：provenance 是自由字典，整块塞进去
# 会让"探针多打一个统计量"变成"换一份工件身份"。
CERT_EVIDENCE_AUDIT_KEYS: tuple[str, ...] = (
    "decision_rule",
    "panel_declared_modes",
    "config_declared_execution_mode",
    "probe_passed",
    "probe_reason",
    "probe_sample",
    "probe_violations",
    "probe_violation_ratio",
    "probe_max_violation_ratio",
)

# 两条数据身份逐项对账的键（preflight 与冻结模型 provenance 用同一套）。
# ``price_series_mode`` 只在**双方都记录**时才可比：``compute_training_data_fingerprint``
# 的返回值是"内容指纹"，不含口径（口径由 certify 探针单独给证据），所以内容对账用
# :data:`IDENTITY_CONTENT_KEYS`，口径对账另走 certify 检查。
IDENTITY_CONTENT_KEYS: tuple[str, ...] = (
    "fingerprint_version",
    "source_window",
    "warmup_days",
    "fingerprint",
    "rows",
    "columns",
)
IDENTITY_COMPARE_KEYS: tuple[str, ...] = (
    "price_series_mode",
    *IDENTITY_CONTENT_KEYS,
)


class PriceSeriesContractError(RuntimeError):
    """价格序列契约违例（execution 不是 raw / feature 模式不可证 / 逐项对不齐）。

    调用方按 ``exit_code`` 退出（默认 4）；生产路径一律 fail closed，不存在"仅告警"。
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = EXIT_PRICE_SERIES_CONTRACT,
        role: str = ROLE_EXECUTION,
    ) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)
        self.role = str(role)


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def certification_from_declaration(
    *, price_mode: str, certified: bool, source: str = "caller_declared"
) -> PriceModeCertification:
    """把 ``(price_mode, certified)`` 两个裸参包成认证书，走同一套守卫实现。

    ``build_label_v2`` / ``mature_epoch_outcomes`` 收的是裸参（历史签名），但判据
    必须只有一份——所以在这里归一，而不是各写一遍字符串比较。
    """
    return PriceModeCertification(
        mode=str(price_mode or "").strip().lower(),
        source=str(source),
        certified=bool(certified),
        evidence={
            "caller_declared_price_mode": str(price_mode or ""),
            "caller_certified": bool(certified),
        },
    )


def require_certified_execution_series(
    certification: PriceModeCertification | None,
    *,
    context: str,
    db: str = "",
) -> PriceModeCertification:
    """execution 侧硬门：``mode == raw`` 且 ``certified is True``，否则抛错。

    这是"能否进入 label / 可成交性 / 净收益 / MAE/MFE / 超额"的**唯一**判据。
    ``2026-09-21`` 之前的实现只在未认证时打 warning，于是生产 NAS 的 qfq 库一路
    走到训练目标里——本函数就是那个缺口的封堵点。
    """
    mode = str(getattr(certification, "mode", "") or "").strip().lower() if certification else ""
    certified = bool(getattr(certification, "certified", False)) if certification else False
    if mode == EXECUTION_PRICE_MODE_REQUIRED and certified:
        return certification  # type: ignore[return-value]
    raise PriceSeriesContractError(
        f"{context}: execution 价格序列必须是 {EXECUTION_PRICE_MODE_REQUIRED} 且已认证"
        f"（实测 mode={mode or 'unknown'}，certified={certified}，db={db or '(未给)'}）"
        "——拒绝继续：复权价不得作为成交价进入 label / 可成交性 / 净收益 / 超额 / MAE/MFE，"
        "也不得从 qfq 反推 raw",
        role=ROLE_EXECUTION,
    )


def require_declared_feature_series(
    certification: PriceModeCertification | None,
    *,
    context: str,
    expected_mode: str = "",
    db: str = "",
) -> PriceModeCertification:
    """feature 侧硬门：模式必须可证（qfq / raw），且与冻结模型声明一致（如给了）。

    feature 用 qfq 是设计内；**不可证**（``unknown`` / 一点样本都拿不到）才是问题——
    那意味着"这份模型的特征到底用什么口径算的"没有答案。
    """
    mode = str(getattr(certification, "mode", "") or "").strip().lower() if certification else ""
    if mode not in FEATURE_PRICE_MODE_ALLOWED:
        raise PriceSeriesContractError(
            f"{context}: feature 价格口径不可证（mode={mode or 'unknown'}，db={db or '(未给)'}）"
            f"——feature 允许 {'/'.join(FEATURE_PRICE_MODE_ALLOWED)}，但必须有可证的声明或探针证据",
            role=ROLE_FEATURE,
        )
    wanted = str(expected_mode or "").strip().lower()
    if wanted and wanted != mode:
        raise PriceSeriesContractError(
            f"{context}: feature 价格口径 {mode} 与冻结模型声明的 {wanted} 不一致"
            f"（db={db or '(未给)'}）——换口径等于换特征，必须重新冻结",
            role=ROLE_FEATURE,
        )
    return certification  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 数据身份块（两条，独立）
# ---------------------------------------------------------------------------


def certification_evidence_block(
    certification: PriceModeCertification | None,
) -> dict[str, object]:
    """认证证据的可审计子集（固定键序；只取白名单里实际存在的键）。"""
    evidence = dict(getattr(certification, "evidence", {}) or {}) if certification else {}
    return {key: evidence[key] for key in CERT_EVIDENCE_AUDIT_KEYS if key in evidence}


def price_series_identity_block(
    *,
    role: str,
    db: str,
    certification: PriceModeCertification | None,
    fingerprint: Mapping[str, object] | None = None,
    context: str = "",
) -> dict[str, object]:
    """组装一条数据身份（feature 或 execution），是 provenance / preflight 的公共形态。"""
    block: dict[str, object] = {
        "role": str(role),
        "db": str(db),
        "price_series_mode": (
            str(getattr(certification, "mode", "") or "unknown").strip().lower()
            if certification
            else "unknown"
        ),
        "price_series_certified": bool(getattr(certification, "certified", False)),
        "certification_source": str(getattr(certification, "source", "") or ""),
        "certification_context": str(context or ""),
        "certification_evidence": certification_evidence_block(certification),
    }
    if fingerprint is not None:
        block.update(
            {
                "fingerprint_version": str(fingerprint.get("fingerprint_version", "") or ""),
                "fingerprint": str(fingerprint.get("fingerprint", "") or ""),
                "source_window": [str(item) for item in (fingerprint.get("source_window") or [])],
                "warmup_days": fingerprint.get("warmup_days"),
                "columns": [str(item) for item in (fingerprint.get("columns") or [])],
                "rows": fingerprint.get("rows"),
            }
        )
    return block


def compare_price_series_identity(
    recorded: Mapping[str, object] | None,
    recomputed: Mapping[str, object] | None,
    *,
    role: str,
    keys: Sequence[str] = IDENTITY_COMPARE_KEYS,
) -> list[str]:
    """逐项对账两条数据身份；返回不一致说明（空列表 = 完全一致）。

    **不只是比 digest**：只比 fingerprint 会漏掉"同一个 digest、不同的口径/窗口声明"，
    那是自述与实现脱节。版本 / source_window / warmup / 列 / 行数逐项都要相等。

    ``keys`` 默认含 ``price_series_mode``；与"不含口径的指纹载荷"对账时传
    :data:`IDENTITY_CONTENT_KEYS`（口径另有 certify 探针给出证据）。
    """
    problems: list[str] = []
    left = dict(recorded or {})
    right = dict(recomputed or {})
    if not left:
        problems.append(f"{role}_data_identity_missing:模型 provenance 未封存{role}数据身份")
        return problems
    if not right:
        problems.append(f"{role}_data_identity_unavailable:本次未能复算{role}数据身份")
        return problems
    for key in keys:
        before = left.get(key)
        after = right.get(key)
        if key == "columns":
            before = sorted(str(item) for item in (before or []))
            after = sorted(str(item) for item in (after or []))
        if key == "source_window":
            before = [str(item) for item in (before or [])]
            after = [str(item) for item in (after or [])]
        if key == "rows" and before is not None and after is not None:
            try:
                before, after = int(before), int(after)
            except (TypeError, ValueError):
                pass
        if before != after:
            problems.append(f"{role}.{key}:{_preview(before)}!={_preview(after)}")
    if left.get("price_series_mode") == EXECUTION_PRICE_MODE_REQUIRED and not bool(
        left.get("price_series_certified")
    ):
        problems.append(
            f"{role}.price_series_certified:False（声明 raw 但未认证：声明必须与实测一致）"
        )
    return problems


def _preview(value: object) -> str:
    text = str(value)
    return text if len(text) <= 48 else f"{text[:24]}…{text[-12:]}"


# ---------------------------------------------------------------------------
# 行情库解析（feature / execution 两个角色）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketDbResolution:
    """feature / execution 两个行情库的解析结果（含各自的来源，供审计）。"""

    feature_db: str
    execution_db: str
    feature_source: str
    execution_source: str
    db_role_binding: str

    @property
    def dual_source(self) -> bool:
        return self.db_role_binding == DB_ROLE_BINDING_DUAL

    @property
    def same_db(self) -> bool:
        return bool(self.feature_db) and self.feature_db == self.execution_db

    def to_payload(self) -> dict[str, object]:
        return {
            "feature_market_db": self.feature_db,
            "feature_market_db_source": self.feature_source,
            "execution_market_db": self.execution_db,
            "execution_market_db_source": self.execution_source,
            "db_role_binding": self.db_role_binding,
        }


def resolve_market_dbs(
    config: Any,
    *,
    feature_db: str = "",
    execution_db: str = "",
    legacy_market_db: str | None = None,
) -> MarketDbResolution:
    """解析两个角色各自的行情库路径。

    优先级（角色独立）：

    ```text
    feature:   --feature-market-db > alpha_v2.feature_market_db >
               --market-db（旧单库形态） > market_warehouse.db_path（仓库默认）
    execution: --execution-market-db > alpha_v2.execution_market_db >
               --market-db（旧单库形态） > （空 = 未配置，生产一律拒绝）
    ```

    ``legacy_market_db`` 只在**调用方显式给了旧参数**时非 None；它让两个角色绑到同一份
    库，``db_role_binding`` 如实标 ``legacy_single_db``——语义含糊的那一半必须自曝，
    不能靠默认值静默发生。
    """
    alpha = getattr(config, "alpha_v2", None)
    warehouse = getattr(config, "market_warehouse", None)
    cfg_feature = str(getattr(alpha, "feature_market_db", "") or "").strip()
    cfg_execution = str(getattr(alpha, "execution_market_db", "") or "").strip()
    warehouse_db = str(getattr(warehouse, "db_path", "") or "").strip() or DEFAULT_MARKET_DB
    legacy = str(legacy_market_db or "").strip()

    explicit_feature = str(feature_db or "").strip()
    explicit_execution = str(execution_db or "").strip()

    if explicit_feature:
        resolved_feature, feature_source = explicit_feature, "cli_feature_market_db"
    elif cfg_feature:
        resolved_feature, feature_source = cfg_feature, "config_alpha_v2_feature_market_db"
    elif legacy:
        resolved_feature, feature_source = legacy, "cli_market_db_legacy"
    else:
        resolved_feature, feature_source = warehouse_db, "market_warehouse_db_path"

    if explicit_execution:
        resolved_execution, execution_source = explicit_execution, "cli_execution_market_db"
    elif cfg_execution:
        resolved_execution, execution_source = cfg_execution, "config_alpha_v2_execution_market_db"
    elif legacy:
        resolved_execution, execution_source = legacy, "cli_market_db_legacy"
    else:
        resolved_execution, execution_source = "", "unset"

    binding = (
        DB_ROLE_BINDING_LEGACY
        if execution_source == "cli_market_db_legacy"
        else DB_ROLE_BINDING_DUAL
    )
    return MarketDbResolution(
        feature_db=resolved_feature,
        execution_db=resolved_execution,
        feature_source=feature_source,
        execution_source=execution_source,
        db_role_binding=binding,
    )


# ---------------------------------------------------------------------------
# decision ↔ execution 面板逐项对齐
# ---------------------------------------------------------------------------


def _execution_index(execution_panel: DailyPanel) -> set[tuple[str, str]]:
    """execution 面板可用的 ``(symbol, ISO date)`` 集合。"""
    bars = execution_panel.bars
    if bars.empty or not {"symbol", "trade_date"}.issubset(set(bars.columns)):
        return set()
    import pandas as pd

    dates = pd.to_datetime(bars["trade_date"], errors="coerce").dt.date
    index: set[tuple[str, str]] = set()
    for symbol, day in zip(bars["symbol"], dates, strict=True):
        if day is None or day != day:  # NaT 自比不相等
            continue
        index.add((str(symbol), day.isoformat()))
    return index


def decision_alignment(
    *, decisions: Sequence[Any], execution_panel: DailyPanel
) -> dict[str, object]:
    """逐项核对每条 decision ``(symbol, decision_date)`` 是否都能在 execution 面板找到。

    "全窗口行数差不多"不是对齐证据：差的那几条恰好是停牌/退市/次新时，恰恰是最需要
    小心处理的样本。所以按 (symbol, date) 精确比对。
    """
    available = _execution_index(execution_panel)
    total = 0
    missing_keys: list[str] = []
    for item in decisions:
        total += 1
        key = (str(getattr(item, "symbol", "") or ""), str(getattr(item, "decision_date", "")))
        if key not in available:
            missing_keys.append(f"{key[0]}@{key[1]}")
    return {
        "decisions": int(total),
        "execution_rows": int(len(available)),
        "missing": int(len(missing_keys)),
        "missing_examples": missing_keys[:20],
        "aligned": not missing_keys,
    }


def assert_decisions_aligned(
    *,
    decisions: Sequence[Any],
    execution_panel: DailyPanel,
    context: str,
) -> dict[str, object]:
    """缺 raw execution row ⇒ fail closed（**不得从 qfq 推测 raw**）。"""
    payload = decision_alignment(decisions=decisions, execution_panel=execution_panel)
    total = int(payload["decisions"])
    missing = int(payload["missing"])
    payload["aligned"] = missing == 0
    if missing:
        raise PriceSeriesContractError(
            f"{context}: {missing}/{total} 条 decision 在 execution 面板里找不到对应 bar"
            f"（例：{payload['missing_examples'][:5]}）——缺失即 target 不可用，"
            "绝不允许退回 qfq 或从 qfq 反推 raw",
            role=ROLE_EXECUTION,
        )
    return payload


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def resolve_db_path(repo_root: str | Path, db: str) -> Path:
    """把（可能是相对的）库路径解析成绝对路径；``repo_root`` 为空时原样返回。"""
    text = str(db or "").strip()
    path = Path(text)
    if path.is_absolute() or not str(repo_root or "").strip():
        return path
    return Path(repo_root) / path


__all__ = [
    "CERT_EVIDENCE_AUDIT_KEYS",
    "DB_ROLE_BINDING_DUAL",
    "DB_ROLE_BINDING_LEGACY",
    "DEFAULT_MARKET_DB",
    "EXECUTION_PRICE_MODE_REQUIRED",
    "EXIT_PRICE_SERIES_CONTRACT",
    "FEATURE_PRICE_MODE_ALLOWED",
    "IDENTITY_COMPARE_KEYS",
    "IDENTITY_CONTENT_KEYS",
    "MarketDbResolution",
    "PRICE_MODE_QFQ",
    "PRICE_MODE_RAW",
    "PriceSeriesContractError",
    "ROLE_EXECUTION",
    "ROLE_FEATURE",
    "assert_decisions_aligned",
    "certification_evidence_block",
    "certification_from_declaration",
    "compare_price_series_identity",
    "decision_alignment",
    "price_series_identity_block",
    "require_certified_execution_series",
    "require_declared_feature_series",
    "resolve_db_path",
    "resolve_market_dbs",
]
