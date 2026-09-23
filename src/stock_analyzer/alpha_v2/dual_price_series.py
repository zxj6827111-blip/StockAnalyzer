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
2. **绝不猜测**：缺失的 raw execution row 不会退回 qfq、不会用 qfq 反推——该样本直接
   不可用（见下面"有效决策集"）；
3. **身份成对封存**：``feature_data_identity`` / ``execution_data_identity`` 两条独立身份
   （库、口径、认证结论、指纹版本、source window、指纹、列、行数），两条都进工件哈希。

**有效决策集契约（2026-09-23，P3.1）**：PIT 合格池是**候选**集而不是**可交易**集——
``expected_active_lookback_days=5`` 的设计就是让"最近 5 个交易日内交易过、当天停牌"
（以及退市/长停期间仍在 history 窗口内的票）留在候选里。所以三段集合必须显式分开：

```text
decision universe       PIT eligible ∩ feature 日历（候选，可能是停牌日）
execution available     上述候选 ∩ execution 面板当日有 bar   ← 唯一可进入训练帧的集合
training frame          execution available ∩ feature frame ∩ 有 label 的行
```

裁决只有两种，且**判据是可复现的集合关系**，不是比例感觉：

- 该 ``(symbol, decision_date)`` 在 execution 面板既没有当日 bar，也**不是**"整票缺席/
  整天缺席/feature 侧有 bar"→ **合法过滤**（记 ``NO_EXECUTION_BAR_ON_DECISION_DATE``
  并写入审计：过滤前后行数、样例、按日最大占比）；
- 整票缺席（``SYMBOL_NOT_IN_EXECUTION_PANEL``）、整天不是 execution 的交易日
  （``DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL``）、或 feature 侧**有**当日 bar
  而 execution 没有（``FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE``）→ **结构缺陷，fail
  closed**：这三种都不是"停牌"，而是两份面板对同一份事实给出了不同答案——静默过滤会
  把真实的断供/截断/换库伪装成"少了几行训练样本"。
  另有两条量级闸（总体占比 / 单日占比）防止"看起来很合法"的大面积过滤。

``assert_decisions_aligned`` 保留为**零容忍**版本（任何一条不齐即抛），供研究/回放等
"必须逐条对齐"的路径使用；训练帧走 :func:`filter_decisions_by_execution_availability`。
"""

from __future__ import annotations

import math
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

#: 需要"冻结口径 = 当天实测口径"严格成立的 validation_mode 集合。
#: ``test`` 与 ``production`` 同语义（本仓库既有约定：test 只为确定性时钟存在，
#: 且它在 KPI 层是 clean-OOS 合格模式）。**只有 rehearsal 允许带标注降级**——
#: 放 test 走降级会产生"clean 证据 + 变了语义的 style 基准"，所以这里比
#: "production 一档"更严：能进 clean 证据的模式一律不许静默降级。
LIVE_STRICT_VALIDATION_MODES: tuple[str, ...] = ("production", "test")

#: rehearsal 降级时 style 来源的标注（执行侧面板冒充 feature 面板）
STYLE_SOURCE_FEATURE_PANEL = "feature_panel"
STYLE_SOURCE_EXECUTION_FALLBACK = "execution_panel_fallback_rehearsal"

#: 当天 feature 价格口径证据的 schema（capture 日清单与 scheduler 前置门共用一份）。
FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA = "alpha_v2_live_feature_price_series.v1"


def is_live_strict_mode(validation_mode: object) -> bool:
    """该 validation_mode 是否要求"冻结 feature mode == 当天实测 feature mode"。

    ``production`` / ``test`` → True；``rehearsal`` / 其它（未知值）→ False。
    未知值回退成**非严格**是有意的：严格集合是白名单，新增的未知模式不该悄悄
    获得 clean 资格（KPI 层另有 execution/身份门把它挡在 clean 之外）。
    """
    return str(validation_mode or "").strip().lower() in LIVE_STRICT_VALIDATION_MODES

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

#: **合法过滤**的唯一原因码：该 ``(symbol, decision_date)`` 在 execution 面板里没有当日
#: bar，但票与日期都在面板里（即"该票当天没交易"，而不是"面板缺这块数据"）。
FILTER_REASON_NO_EXECUTION_BAR = "NO_EXECUTION_BAR_ON_DECISION_DATE"

#: **结构缺陷**原因码（fail closed）——这些不是停牌，是面板本身不对。
DEFECT_REASON_SYMBOL_ABSENT = "SYMBOL_NOT_IN_EXECUTION_PANEL"
DEFECT_REASON_DATE_NOT_SESSION = "DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL"
DEFECT_REASON_CROSS_PANEL_DIVERGENCE = "FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE"

#: 过滤量级上限（防"看起来合法"的大面积过滤把真实断供吃掉）：
#:
#: - **总体上限 2%**：实测生产窗口（2025-06-02..2026-08-31）6602/1650654 = **0.400%**，
#:   取 2% ≈ 5 倍余量；量级不对说明窗口级的断供/截断/换库。
#: - **单日上限 50%**：判据是"这一天**过半**候选被过滤"，即该日截面被毁掉——只有这种
#:   规模才可能是"整片 bar 缺失"。**实测最大合法单日占比 = 12.25%
#:   （2025-11-17，634/5175）**：那一日 raw 与 qfq 两侧**同时**少 724 个 symbol 的
#:   当日 bar（逐票形态是"前一根 11-14、后一根 11-18、只缺这一天"），属**上游链路
#:   的覆盖率缺口**（两侧一致，不是跨面板分歧）。
#:   注意分工：**单侧**丢失（execution 缺、feature 有）由跨面板分歧检查逐键拦截，
#:   与量级无关；单日闸只兜"两侧同时大面积缺失"这一种，故阈值取"过半"而非"几个百分点"。
#: - **行数下限 50**：小窗口/小夹具里几十行就是几个百分点，比例门会误杀；而"大面积
#:   过滤"按定义是大数，行数下限不削弱任何检测能力。
DEFAULT_MAX_FILTERED_RATIO = 0.02
DEFAULT_MAX_DAILY_FILTERED_RATIO = 0.50
DEFAULT_MAX_FILTERED_ROWS_FLOOR = 50


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


def _feature_mode_from_provenance(provenance: object) -> str:
    """从冻结 provenance 的 ``feature_data_identity.price_series_mode`` 取口径。

    只认这一条路径：``config.data_source.vendor_zip_price_series_mode`` 是**当前配置**，
    不是"这份模型训练时用的口径"——拿配置猜会让"训练 qfq / 线上被改成 raw"静默通过。
    """
    if not isinstance(provenance, Mapping):
        return ""
    identity = provenance.get("feature_data_identity")
    if not isinstance(identity, Mapping):
        return ""
    return str(identity.get("price_series_mode", "") or "").strip().lower()


def feature_mode_of_frozen_model(model_manifest: Mapping[str, object] | None) -> str:
    """冻结模型 manifest → 训练时冻结的 feature 口径（缺失 = 空串，由调用方 fail closed）。"""
    manifest = model_manifest if isinstance(model_manifest, Mapping) else {}
    return _feature_mode_from_provenance(manifest.get("provenance"))


def feature_mode_of_freeze_manifest(freeze_manifest: Mapping[str, object] | None) -> str:
    """validation freeze manifest → 同一字段（路径 ``model.provenance.feature_data_identity``）。

    freeze 清单里的模型块由 ``frozen_model_identity_payload`` 派生且受
    ``freeze_manifest_hash`` 锚定，所以它是"本 epoch 用的那份模型"的权威副本；
    但**身份仍以工件为准**——capture 直接读工件，mature/scheduler 读清单副本。
    """
    freeze = freeze_manifest if isinstance(freeze_manifest, Mapping) else {}
    model = freeze.get("model")
    if not isinstance(model, Mapping):
        return ""
    mode = _feature_mode_from_provenance(model.get("provenance"))
    if mode:
        return mode
    # 兼容：frozen_model_identity_payload 同时把身份块放在顶层。
    identity = model.get("feature_data_identity")
    if isinstance(identity, Mapping):
        return str(identity.get("price_series_mode", "") or "").strip().lower()
    return ""


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
# decision ↔ execution 面板：可用性过滤（合法停牌）与结构缺陷（fail closed）
# ---------------------------------------------------------------------------


def _panel_bar_keys(panel: DailyPanel | None) -> set[tuple[str, str]]:
    """面板可用的 ``(symbol, ISO date)`` 集合（空面板/缺列 → 空集）。"""
    if panel is None:
        return set()
    bars = panel.bars
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


@dataclass(frozen=True, slots=True)
class DecisionAvailability:
    """decision 可用性裁决结果：**可进入训练帧的决策** + 审计报告。

    ``decisions`` 只含"execution 面板当日确有 bar"的决策，且保持原顺序（下游的
    基准/风格分组按集合成员决定，顺序不是语义，但保序让产物可复现）。

    三段行数是**显式字段**（不是从 ``report`` 里再取一次）：调用方要对账的是这三个
    数字，让它们带着类型出现，才不会在"新加一个审计键"时把行数口径一起改掉。
    """

    decisions: tuple[Any, ...]
    decision_rows_before: int
    filtered_rows: int
    decision_rows_after: int
    report: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return dict(self.report)


def _decision_key(item: Any) -> tuple[str, str]:
    return (
        str(getattr(item, "symbol", "") or ""),
        str(getattr(item, "decision_date", "")),
    )


def filter_decisions_by_execution_availability(
    *,
    decisions: Sequence[Any],
    execution_panel: DailyPanel,
    context: str,
    cross_check_panel: DailyPanel | None = None,
    max_filtered_ratio: float = DEFAULT_MAX_FILTERED_RATIO,
    max_daily_filtered_ratio: float = DEFAULT_MAX_DAILY_FILTERED_RATIO,
    max_filtered_rows_floor: int = DEFAULT_MAX_FILTERED_ROWS_FLOOR,
) -> DecisionAvailability:
    """把"当天没有 execution bar"的候选**过滤掉**，其余原样返回；结构缺陷则 fail closed。

    为什么不是 fail closed 一条路：PIT 候选池按设计包含"最近 5 个交易日内交易过、
    当天停牌"的票（``expected_active_lookback_days=5``），它本来就是**候选**而不是
    可交易集。这类 ``(symbol, date)`` 在 execution 面板上没有当日 bar，是**预期内**的
    事实，不是数据缺失——把它当致命错误等于让生产窗口永远冻结不了；把它静默丢掉
    则是另一个极端（真实的断供会被藏起来）。所以这里给出**显式的中间态**：

    ```text
    decision universe  ──►  execution availability 过滤  ──►  training frame
    （PIT 候选）              （当日有 bar 才留下）            （特征 ∩ label）
    ```

    三种情况必须区分（判据是集合关系，可复现、不依赖比例直觉）：

    ==========================================  ================================
    ``(symbol, date)`` 的观测                     裁决
    ==========================================  ================================
    在 execution 面板里                             保留
    票在、日在、仅当天无 bar（停牌）                 **过滤**（记原因 + 样例）
    票在整个 execution 面板都不存在                  缺陷（symbol_not_in_panel）
    该日期在 execution 面板里根本不是交易日          缺陷（date_not_a_session）
    feature 面板当天**有** bar 而 execution 没有     缺陷（跨面板分歧）
    ==========================================  ================================

    ``cross_check_panel``（生产里传 feature 面板）是跨面板一致性证据：两份面板对
    "这只票这一天有没有交易"必须给同一个答案；不一致说明其中一份缺数据，而不是停牌。
    它同时保证"被过滤的行在两侧都不可用"——否则过滤会顺带改变质量池排名分母。

    两条量级闸（总体占比、单日占比）是"合法形态但规模异常"的兜底：见
    :data:`DEFAULT_MAX_FILTERED_RATIO` / :data:`DEFAULT_MAX_DAILY_FILTERED_RATIO`。

    过渡性说明：返回的 ``report["decision_rows_before"]`` 是**候选集**规模（PIT
    eligible），不是"全市场股票数"；两者不能混用（历史上出现过把质量池裁完的 300
    当成全市场输入的取值错误）。
    """
    available = _panel_bar_keys(execution_panel)
    sessions = {day for _symbol, day in available}
    symbols = {symbol for symbol, _day in available}
    cross_keys = _panel_bar_keys(cross_check_panel)

    kept: list[Any] = []
    filtered_examples: list[str] = []
    defect_examples: dict[str, list[str]] = {}
    per_date_total: dict[str, int] = {}
    per_date_filtered: dict[str, int] = {}
    for item in decisions:
        symbol, day = _decision_key(item)
        key = (symbol, day)
        per_date_total[day] = per_date_total.get(day, 0) + 1
        if key in available:
            kept.append(item)
            continue
        reason = ""
        if symbol not in symbols:
            reason = DEFECT_REASON_SYMBOL_ABSENT
        elif day not in sessions:
            reason = DEFECT_REASON_DATE_NOT_SESSION
        elif cross_check_panel is not None and key in cross_keys:
            reason = DEFECT_REASON_CROSS_PANEL_DIVERGENCE
        if reason:
            defect_examples.setdefault(reason, []).append(f"{symbol}@{day}")
            continue
        per_date_filtered[day] = per_date_filtered.get(day, 0) + 1
        if len(filtered_examples) < 20:
            filtered_examples.append(f"{symbol}@{day}")

    total = sum(per_date_total.values())
    filtered = sum(per_date_filtered.values())
    defects = {reason: len(items) for reason, items in sorted(defect_examples.items())}
    if defects:
        defect_total = sum(defects.values())
        preview = {reason: items[:5] for reason, items in sorted(defect_examples.items())}
        raise PriceSeriesContractError(
            f"{context}: {defect_total}/{total} 条 decision 的缺失**不是**停牌口径，而是"
            f"面板结构缺陷：{defects}（例：{preview}）——（同一批里另有 {filtered} 条属"
            "合法停牌口径、本可过滤）。execution 面板必须与 feature 面板逐键覆盖同一份 "
            "PIT 候选集，结构缺陷即 target 不可用；绝不允许退回 qfq、从 qfq 反推 raw，"
            "或用过滤把断供藏起来",
            role=ROLE_EXECUTION,
        )

    required = max(0, int(max_filtered_rows_floor))
    allowed_rows = max(required, math.ceil(float(max_filtered_ratio) * max(1, total)))
    if filtered > allowed_rows:
        raise PriceSeriesContractError(
            f"{context}: {filtered}/{total} 条 decision 因'当日无 execution bar'被过滤"
            f"（{filtered / max(1, total):.4%}）超过审计上限 {allowed_rows} 行"
            f"（max_filtered_ratio={max_filtered_ratio}, floor={required}）——这个规模不是"
            f"停牌能解释的，先查 execution 面板是否断供/被截断（样例：{filtered_examples[:5]}）",
            role=ROLE_EXECUTION,
        )
    worst_date, worst_ratio = "", 0.0
    for day, count in per_date_filtered.items():
        ratio = count / max(1, per_date_total.get(day, 0))
        if ratio > worst_ratio:
            worst_date, worst_ratio = day, ratio
    allowed_daily = max(
        required,
        math.ceil(float(max_daily_filtered_ratio) * max(1, per_date_total.get(worst_date, 0))),
    )
    if per_date_filtered.get(worst_date, 0) > allowed_daily:
        raise PriceSeriesContractError(
            f"{context}: 单日过滤量异常——{worst_date} 有 "
            f"{per_date_filtered[worst_date]}/{per_date_total.get(worst_date, 0)} 条 decision "
            f"（{worst_ratio:.4%}）当天无 execution bar，超过单日上限 {allowed_daily} 行"
            f"（max_daily_filtered_ratio={max_daily_filtered_ratio}）——**过半**候选当日"
            "没有 bar 意味着这一天的截面已被毁掉，更像 execution 面板缺了这一段，而不是"
            "当天全市场停牌",
            role=ROLE_EXECUTION,
        )

    top_dates = sorted(
        (
            (
                day,
                int(count),
                int(per_date_total.get(day, 0)),
                count / max(1, per_date_total.get(day, 0)),
            )
            for day, count in per_date_filtered.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    top_dates_payload: list[dict[str, object]] = [
        {
            "decision_date": day,
            "filtered": rows,
            "decisions": day_rows,
            "ratio": round(ratio, 8),
        }
        for day, rows, day_rows, ratio in top_dates
    ]
    report: dict[str, object] = {
        "context": str(context),
        "universe": "pit_eligible_candidates",
        "decision_rows_before": int(total),
        "filtered_missing_execution_rows": int(filtered),
        "decision_rows_after": int(len(kept)),
        # ``intersection`` = decision 集合 ∩ execution 面板当日 bar（与
        # ``decision_rows_after`` 同值，两个名字都留：验收里按名字取值，
        # 同一份报告不能因为叫法不同而对不上账）。
        "intersection": int(len(kept)),
        "filtered_ratio": (round(filtered / total, 8) if total else 0.0),
        "filter_reason": FILTER_REASON_NO_EXECUTION_BAR,
        "filtered_examples": filtered_examples,
        "filtered_dates": int(len(per_date_filtered)),
        "max_daily_filtered_date": worst_date,
        "max_daily_filtered_ratio": round(worst_ratio, 8),
        # 过滤最集中的 5 天：单日量级异常（例如某天两侧同时缺一片 symbol）必须一眼可见，
        # 而不是只留一个"最大占比"数字——实测 2025-11-17 就是这种形态（12.25%）。
        "filtered_dates_top": top_dates_payload,
        "execution_panel_bars": int(len(available)),
        "execution_panel_sessions": int(len(sessions)),
        "execution_panel_symbols": int(len(symbols)),
        "cross_check_panel_source": str(getattr(cross_check_panel, "source", "") or ""),
        "cross_check_bar_keys": int(len(cross_keys)),
        "limits": {
            "max_filtered_ratio": float(max_filtered_ratio),
            "max_daily_filtered_ratio": float(max_daily_filtered_ratio),
            "max_filtered_rows_floor": required,
        },
        # ``aligned`` 保留给"本次**一条都没被过滤**"这个更强的形态（零过滤证据）；
        # 发生过过滤时它是 False，但整份裁决仍然 ``status == PASS``——
        # "有行被过滤"与"契约被破坏"是两件事（见函数文档的三段集合契约）。
        "aligned": filtered == 0,
        "status": "PASS",
    }
    return DecisionAvailability(
        decisions=tuple(kept),
        decision_rows_before=int(total),
        filtered_rows=int(filtered),
        decision_rows_after=int(len(kept)),
        report=report,
    )


def decision_alignment(
    *, decisions: Sequence[Any], execution_panel: DailyPanel
) -> dict[str, object]:
    """逐项核对每条 decision ``(symbol, decision_date)`` 是否都能在 execution 面板找到。

    "全窗口行数差不多"不是对齐证据：差的那几条恰好是停牌/退市/次新时，恰恰是最需要
    小心处理的样本。所以按 (symbol, date) 精确比对。

    这是**测量**函数（不抛错）；裁决有两个消费口：严格版
    :func:`assert_decisions_aligned` 与过滤版
    :func:`filter_decisions_by_execution_availability`。
    """
    available = _panel_bar_keys(execution_panel)
    total = 0
    missing_keys: list[str] = []
    for item in decisions:
        total += 1
        key = _decision_key(item)
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
    """**零容忍**版本：任何一条缺 raw execution row ⇒ fail closed（不得从 qfq 推测 raw）。

    训练帧不用这个（PIT 候选含合法停牌日，见
    :func:`filter_decisions_by_execution_availability`）；它保留给"必须逐条对齐"的
    路径与测试，语义与 2026-09-21 的原始守卫**完全一致**。
    """
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
    "DEFAULT_MAX_DAILY_FILTERED_RATIO",
    "DEFAULT_MAX_FILTERED_RATIO",
    "DEFAULT_MAX_FILTERED_ROWS_FLOOR",
    "DEFECT_REASON_CROSS_PANEL_DIVERGENCE",
    "DEFECT_REASON_DATE_NOT_SESSION",
    "DEFECT_REASON_SYMBOL_ABSENT",
    "FILTER_REASON_NO_EXECUTION_BAR",
    "LIVE_STRICT_VALIDATION_MODES",
    "STYLE_SOURCE_EXECUTION_FALLBACK",
    "FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA",
    "STYLE_SOURCE_FEATURE_PANEL",
    "DB_ROLE_BINDING_DUAL",
    "DB_ROLE_BINDING_LEGACY",
    "DEFAULT_MARKET_DB",
    "EXECUTION_PRICE_MODE_REQUIRED",
    "EXIT_PRICE_SERIES_CONTRACT",
    "FEATURE_PRICE_MODE_ALLOWED",
    "IDENTITY_COMPARE_KEYS",
    "IDENTITY_CONTENT_KEYS",
    "DecisionAvailability",
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
    "feature_mode_of_freeze_manifest",
    "feature_mode_of_frozen_model",
    "filter_decisions_by_execution_availability",
    "is_live_strict_mode",
    "price_series_identity_block",
    "require_certified_execution_series",
    "require_declared_feature_series",
    "resolve_db_path",
    "resolve_market_dbs",
]
