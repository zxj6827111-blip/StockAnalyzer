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

**有效决策集契约（2026-09-23 P3.1 提交于 ``be2e4ef``；同日 P3.1.1 加日截面健康门）**：
PIT 合格池是**候选**集而不是**可交易**集 ——
``expected_active_lookback_days=5``（**5 个自然日**，不是 5 个交易日）按设计把"最近还
活跃、当天没有 bar"的票留在候选里。所以三段集合必须显式分开：

```text
decision universe       PIT eligible ∩ feature 日历（候选，当天未必可交易）
execution available     上述候选 ∩ execution 面板当日有 bar   ← 唯一可进入训练帧的集合
training frame          execution available ∩ feature frame ∩ 有 label 的行
```

裁决分三层，**日级健康门先于逐键裁决**：

- 【日级】决策日的 execution 截面相对面板自身基线塌陷
  （``EXECUTION_SESSION_BREADTH_COLLAPSE``），或 execution 当日截面明显低于 feature 同日
  （``EXECUTION_SESSION_BREADTH_BELOW_FEATURE``）→ **结构缺陷，fail closed**。
  这一层**完全不看 decision 集合**，只看"这一天面板里有多少根 bar"。
- 【逐键】整票缺席（``SYMBOL_NOT_IN_EXECUTION_PANEL``）、该日不是 execution 的交易日
  （``DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL``）、feature 侧**有**当日 bar 而
  execution 没有（``FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE``）→ **结构缺陷，fail closed**：
  这三种都是两份面板对同一份事实给出了不同答案。
- 【过滤】以上全不成立，即两侧同票同日都无 bar → 过滤出训练帧并记审计
  （``NO_EXECUTION_BAR_ON_DECISION_DATE``）。

⚠️ **第三种不等于"已证明停牌"**：本仓库没有可用于本链路的独立 PIT-safe 停牌真值源，
且两份面板共享同一条上游链路 —— 同一个缺陷会同时命中两侧，"双边同缺"对"停牌"与
"对称断供"**给不出不同答案**，因此它在原理上不构成证明。两条量级比例闸
（总体 / 单日）是 **provisional anomaly guard**，同样不是停牌定义。详见
:func:`filter_decisions_by_execution_availability` 与 ``.agents/notes/ADR-002``。

``assert_decisions_aligned`` 保留为**零容忍**版本（任何一条不齐即抛）。⚠️ 截至
``f2596ce`` 它**只有测试调用方**，所称"供研究/回放路径使用"尚不存在对应入口；
补调用方之前不要把它当成已生效的生产契约。
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

#: **可过滤**的唯一原因码：该 ``(symbol, decision_date)`` 在 execution 面板里没有当日
#: bar，但票与日期都在面板里（即"该票当天拿不到可成交观测"，而不是"面板缺这块数据"）。
#:
#: ⚠️ **这个名字不代表"已证明停牌"**：仓库里没有独立且 PIT-safe 的停牌真值源
#: （``daily_trade_status`` 实测 154 行 / 2 只票 / ``sum(suspended)=0``，且 alpha_v2
#: 从不读；``security_status`` 0 行 0 生产方；``daily_bars.suspended`` 全库恒 False）。
#: 双边同时无 bar **不能**推出停牌——两份面板共享同一条上游链路，同一个缺陷会同时
#: 命中两侧。该码的准确语义见 :func:`filter_decisions_by_execution_availability`。
FILTER_REASON_NO_EXECUTION_BAR = "NO_EXECUTION_BAR_ON_DECISION_DATE"

#: **结构缺陷**原因码（fail closed）——这些不是"不可交易"，是面板本身不对。
DEFECT_REASON_SYMBOL_ABSENT = "SYMBOL_NOT_IN_EXECUTION_PANEL"
DEFECT_REASON_DATE_NOT_SESSION = "DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL"
DEFECT_REASON_CROSS_PANEL_DIVERGENCE = "FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE"

#: **日级**结构缺陷原因码（fail closed，**先于**逐键裁决）：该决策日 execution 面板的
#: 当日 bar 数相对自身基线塌陷，或明显低于 feature 面板同日截面。
#: 这两条都不看"某条 decision 有没有 bar"，只看**这一天的截面还在不在**，
#: 所以能抓到"两侧同时缺一片票"这种跨面板一致性检查原理上抓不到的形态。
DEFECT_REASON_SESSION_BREADTH_COLLAPSE = "EXECUTION_SESSION_BREADTH_COLLAPSE"
DEFECT_REASON_SESSION_BELOW_FEATURE_BREADTH = "EXECUTION_SESSION_BREADTH_BELOW_FEATURE"

#: **provisional anomaly guard（临时异常哨兵），不是停牌定义。**
#:
#: 这两条比例闸只回答"过滤规模是否反常"，**不回答**"被过滤的行是不是停牌"。
#: 它们与 :func:`filter_decisions_by_execution_availability` 的集合关系判据是两类
#: 不同的东西，不要混用：比例不是证据，只是量级报警。
#:
#: - **总体上限 2%**：观测到生产窗口 6602/1650654 = 0.400%。⚠️ 该值**已被实测反例
#:   证伪其普适性**：同一 PIT 语义在十年面板的 2016 年窗口上是
#:   **8778/422566 = 2.0773%**（2016 年真实大面积重组停牌，抽 350 键逐票取证
#:   gap 2..180 个 session、100% 在之后复牌）——换窗口/换 universe 规模会误杀。
#: - **单日上限 50%**：判据是"这一天过半候选被过滤"。它原本是 10%，被
#:   ``be2e4ef`` 以"实测最大合法单日 12.25%（2025-11-17）"为由放宽到 50%。
#:   ⚠️ 那次放宽**把 12.25% 当成了合法值**，而它是上游链路的覆盖率缺口
#:   （见 :data:`DEFECT_REASON_SESSION_BREADTH_COLLAPSE` 为什么存在）。
#:   十年面板实测的最大**形态合法**单日过滤占比只有 **3.834%**（2016-04-22），
#:   p99 也只有 3.566%——即 10% 那条旧阈值十年间从未误杀过任何一天。
#: - **行数下限 50**：小窗口/小夹具里几十行就是几个百分点，比例门会误杀。
#:
#: 保留而不删除的理由：集合关系判据对"两侧对称缺失"原理上失效（见函数文档），
#: 比例闸是那一形态下**仅剩**的兜底。定稿前必须按 §ADR-002 用多窗口分布重设。
DEFAULT_MAX_FILTERED_RATIO = 0.02
DEFAULT_MAX_DAILY_FILTERED_RATIO = 0.50
DEFAULT_MAX_FILTERED_ROWS_FLOOR = 50

#: **日截面健康门（session/day health guard）——先于逐键 FILTER 裁决执行。**
#:
#: 与上面两条比例闸的根本区别：它**完全不看 decision 集合**，只看面板自身
#: "这一天有多少根 bar"。因此它不依赖"两份面板是否一致"，能覆盖跨面板一致性
#: 检查在原理上覆盖不到的形态（同一缺陷同时命中两侧 → 两侧截面一起塌）。
#:
#: 实测依据（本地真实十年库 ``artifacts/warehouse/market.duckdb``，2,489 个交易日）：
#:
#: ```text
#: 当日 bar 数 / 前若干 session 中位数：p01 = 0.9923，十年内无一例外 >0.99
#: 唯二低于 0.90 的日子 = 库尾被截断的 2026-04-02（0.0095）/ 04-03（0.0083）
#: 已观测缺陷形态：vendor_delta 2025-11-17 上报 4713 vs 前一日 5438 = 0.867
#: ```
#:
#: 所以 0.90 相对健康下沿（≈0.99）留了近 9 个百分点余量，又能稳稳压住观测到的缺陷。
#: 这是**当前唯一有跨窗口实测支撑的阈值**；2% / 50% 都没有。
#: ``min_baseline_rows`` 是为了不把"6 只票的夹具里停 1 只 = 掉 17%"当成截面塌陷——
#: 只有当基线本身是一个像样的截面时，比例塌陷才有意义。
DEFAULT_MIN_SESSION_BREADTH_RATIO = 0.90
DEFAULT_MIN_PANEL_BREADTH_RATIO = 0.90
DEFAULT_SESSION_BREADTH_BASELINE_SESSIONS = 20
DEFAULT_SESSION_BREADTH_MIN_BASELINE_ROWS = 100


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
# decision ↔ execution 面板：日截面健康门 → 可用性过滤（当日无 execution bar）→ 结构缺陷
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


def _session_bar_counts(panel: DailyPanel | None) -> dict[str, int]:
    """面板里每个交易日的 bar 数（``ISO date -> rows``）。

    这就是**日截面广度**——只描述"这一天面板里有多少根 bar"，与任何 decision
    存不存在无关。形状与 ``preflight.check_market_db`` 的
    ``SELECT date, count(*) FROM daily_bars GROUP BY 1`` 同源，区别是那边只看最新
    一天、阈值 0.5（尾段残缺探测），这里要看**每一个决策日**、并按面板自身基线判定。
    """
    if panel is None:
        return {}
    bars = panel.bars
    if bars.empty or "trade_date" not in set(bars.columns):
        return {}
    import pandas as pd

    # ``str(date)`` 对 ``datetime.date`` 与 ``date.isoformat()`` 逐字符等价，
    # 且与 ``_panel_bar_keys`` / ``_decision_key`` 用的是同一个键格式。
    dates = pd.to_datetime(bars["trade_date"], errors="coerce").dt.date
    return {str(day): int(rows) for day, rows in dates.dropna().value_counts().to_dict().items()}


def _breadth_baseline(baseline: Sequence[int], count: int) -> float:
    """``count`` 之前若干 session 的中位数（中位数为 0 时返回 0 表示不可判）。"""
    if not baseline:
        return 0.0
    ordered = sorted(int(rows) for rows in baseline)
    return float(ordered[len(ordered) // 2])


def assess_decision_session_health(
    *,
    decision_dates: Sequence[str],
    execution_counts: Mapping[str, int],
    feature_counts: Mapping[str, int] | None = None,
    min_session_breadth_ratio: float = DEFAULT_MIN_SESSION_BREADTH_RATIO,
    min_panel_breadth_ratio: float = DEFAULT_MIN_PANEL_BREADTH_RATIO,
    baseline_sessions: int = DEFAULT_SESSION_BREADTH_BASELINE_SESSIONS,
    min_baseline_rows: int = DEFAULT_SESSION_BREADTH_MIN_BASELINE_ROWS,
) -> tuple[dict[str, object], list[tuple[str, str, str]]]:
    """逐个决策日判定 execution 面板的**日截面是否健康**（不抛错，返回裁决 + 缺陷清单）。

    两条独立判据，都用面板**自身**的历史 session 做基线（不假设"每天都该有全市场
    bar"，也不引入交易日历）：

    1. ``EXECUTION_SESSION_BREADTH_COLLAPSE``：当日 bar 数 / 前面若干 session 的中位数
       低于 ``min_session_breadth_ratio`` —— 这一天自己的截面塌了。
    2. ``EXECUTION_SESSION_BREADTH_BELOW_FEATURE``：execution 当日截面明显低于 feature
       当日截面。方向是**单边**的：raw 侧比 qfq 侧多symbol 是设计内（qfq 因子缺失的
       票会被跳过，见 ``scripts/alpha_v2_raw_delta_coverage.py`` §8.5），反过来才是异常。

    刻意**不判**的情形（否则会误杀，也更准）：

    - 该日根本不是 execution 面板的 session（``count`` 缺失）——交给逐键裁决用
      ``DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL`` 报，原因更准确；
    - 基线截面太小（``< min_baseline_rows``）——几个票的夹具里"停一只"就是十几个
      百分点，比例塌陷在这个尺度上没有意义；此时如实记 ``judged=false``，不假装通过。
    """
    ordered_sessions = sorted(execution_counts)
    position = {day: index for index, day in enumerate(ordered_sessions)}

    defects: list[tuple[str, str, str]] = []
    judged = skipped = 0
    worst_day, worst_ratio = "", float("inf")
    unjudgeable: list[str] = []
    panel_ratios: dict[str, float] = {}
    for day in sorted(set(str(item) for item in decision_dates)):
        if day not in execution_counts:
            continue  # 不是 execution 的 session：由逐键的 DATE_NOT_SESSION 负责
        count = int(execution_counts[day])
        index = position[day]
        window_start = max(0, index - int(baseline_sessions))
        baseline = [execution_counts[item] for item in ordered_sessions[window_start:index]]
        if not baseline:
            # **面板的第一个 session 不判**，不能用"其余 session 的中位数"兜底：
            # 十年真实面板的截面本身在增长（2016-01-04 只有 2,364 只 vs 全期中位 3,982），
            # 兜底会把"窗口起点正好是面板首日"的真实 freeze 误判成截面塌陷（实测复现）。
            # 没有前序 session 就没有"塌陷"可言——如实记 unjudgeable，让逐键裁决继续工作。
            skipped += 1
            if len(unjudgeable) < 10:
                unjudgeable.append(f"{day}(no_preceding_session)")
            continue
        baseline_median = _breadth_baseline(baseline, count)
        if baseline_median < float(min_baseline_rows):
            skipped += 1
            if len(unjudgeable) < 10:
                unjudgeable.append(f"{day}(baseline={int(baseline_median)})")
            continue
        judged += 1
        ratio = count / baseline_median
        if ratio < worst_ratio:
            worst_day, worst_ratio = day, ratio
        if ratio < float(min_session_breadth_ratio):
            defects.append(
                (
                    day,
                    DEFECT_REASON_SESSION_BREADTH_COLLAPSE,
                    f"{count} bars vs baseline median {int(baseline_median)} = {ratio:.4%}",
                )
            )
            continue
        if feature_counts and day in feature_counts:
            feature_count = int(feature_counts[day])
            # 同日两侧直接比（不是跟 feature 的历史中位比）：这一条要回答的是
            # "两份面板对'这一天有多少票可交易'是否给了同一个量级的答案"。
            if feature_count >= float(min_baseline_rows):
                panel_ratio = count / feature_count
                panel_ratios[day] = round(panel_ratio, 8)
                if panel_ratio < float(min_panel_breadth_ratio):
                    defects.append(
                        (
                            day,
                            DEFECT_REASON_SESSION_BELOW_FEATURE_BREADTH,
                            f"execution {count} bars vs feature {feature_count} 同日 = "
                            f"{panel_ratio:.4%}",
                        )
                    )
    payload: dict[str, object] = {
        "guard": "session_day_health",
        "decision_dates_checked": int(len(set(str(item) for item in decision_dates))),
        "judged_dates": int(judged),
        "unjudgeable_dates": int(skipped),
        "unjudgeable_examples": unjudgeable,
        "worst_breadth_date": worst_day,
        "worst_breadth_ratio": (round(worst_ratio, 8) if judged else None),
        # 两侧同日截面对比（execution/feature）：只在两侧都够大且自身健康时才记。
        "min_panel_breadth_ratio_observed": (min(panel_ratios.values()) if panel_ratios else None),
        "panel_breadth_ratio_worst_date": (
            min(panel_ratios.items(), key=lambda item: item[1])[0] if panel_ratios else ""
        ),
        "execution_breadth_median_rows": int(
            _breadth_baseline([execution_counts[item] for item in ordered_sessions], 0)
        ),
        "limits": {
            "min_session_breadth_ratio": float(min_session_breadth_ratio),
            "min_panel_breadth_ratio": float(min_panel_breadth_ratio),
            "baseline_sessions": int(baseline_sessions),
            "min_baseline_rows": int(min_baseline_rows),
        },
    }
    return payload, defects


def filter_decisions_by_execution_availability(
    *,
    decisions: Sequence[Any],
    execution_panel: DailyPanel,
    context: str,
    cross_check_panel: DailyPanel | None = None,
    max_filtered_ratio: float = DEFAULT_MAX_FILTERED_RATIO,
    max_daily_filtered_ratio: float = DEFAULT_MAX_DAILY_FILTERED_RATIO,
    max_filtered_rows_floor: int = DEFAULT_MAX_FILTERED_ROWS_FLOOR,
    min_session_breadth_ratio: float = DEFAULT_MIN_SESSION_BREADTH_RATIO,
    min_panel_breadth_ratio: float = DEFAULT_MIN_PANEL_BREADTH_RATIO,
    session_breadth_min_baseline_rows: int = DEFAULT_SESSION_BREADTH_MIN_BASELINE_ROWS,
    enforce_session_health_guard: bool = True,
) -> DecisionAvailability:
    """裁决哪些 PIT 候选有权进入训练帧：**当日无 execution bar** 的过滤，结构缺陷 fail closed。

    为什么不是 fail closed 一条路：PIT 候选池是**候选**而不是可交易集 ——
    ``expected_active_lookback_days=5``（⚠️ 5 个**自然日**，见
    ``asof_universe.build_pit_stats``，不是"5 个交易日"）按设计把"最近还活跃、当天
    没有 bar"的票留在候选里。这类 ``(symbol, date)`` 拿不到 T+1 入场，硬要它们进训练帧
    等于让生产窗口永远冻结不了；静默丢掉则是另一个极端（真实断供被藏起来）。所以这里
    给出**显式的中间态**：

    ```text
    decision universe  ──►  execution availability 过滤  ──►  training frame
    （PIT 候选）              （当日有 bar 才留下）            （特征 ∩ label）
    ```

    ⚠️ **FILTER 不等于"已证明停牌"。** 本仓库不存在可用于 Alpha V2 freeze 的、独立且
    PIT-safe 的停牌真值源（``daily_trade_status`` 实测 154 行 / 2 只票 /
    ``sum(suspended)=0`` 且 alpha_v2 从不读；``security_status`` 0 行 0 生产方；
    ``daily_bars.suspended`` 全库恒 False）。而且**两份面板共享同一条上游链路**，
    同一个缺陷会同时命中两侧 —— "两边都没有 bar"这个观测对"停牌"和"对称断供"**给不出
    不同答案**，因此它在原理上不构成证明。FILTER 的准确语义是：

    > 当前证据下无法形成有效 execution observation，但没有发现足以认定为数据契约破坏的证据。

    裁决分三层，**日级健康门先于逐键裁决**：

    ================================================  ==============================
    观测                                              裁决
    ================================================  ==============================
    【日级】决策日截面相对面板自身基线塌陷            缺陷（session_breadth_collapse）
    【日级】execution 当日截面明显低于 feature 同日    缺陷（session_below_feature_breadth）
    【逐键】``(symbol,date)`` 在 execution 面板里      保留
    【逐键】票整体不在 execution 面板                  缺陷（symbol_not_in_panel）
    【逐键】该日期在 execution 面板里不是交易日        缺陷（date_not_a_session）
    【逐键】feature 有当日 bar 而 execution 没有        缺陷（跨面板分歧）
    【兜底】以上都不成立 → 两侧同日同票都无 bar         **过滤**（记原因 + 样例）
    ================================================  ==============================

    两条比例闸（总体 / 单日）是 **provisional anomaly guard，不是停牌定义**：
    它们只在"集合关系对对称缺失原理上失效"这一种形态下作量级兜底，阈值待按多窗口
    分布重设（现状：总体 2% 已被十年面板 2016 年窗口的 2.0773% 实测反例证伪普适性）。
    与之相对，日级广度门是**当前唯一有跨窗口实测支撑**的判据（健康面板 p01=0.9923，
    十年无一例外 <0.99；已知缺陷形态 0.867 / 尾部截断 0.0095）。

    ``cross_check_panel``（生产里传 feature 面板）是跨面板一致性证据：两份面板对
    "这只票这一天有没有交易"必须给同一个答案。它同时保证"被过滤的行在两侧都不可用"
    ——否则过滤会顺带改变质量池排名分母。

    ``enforce_session_health_guard=False`` 只为**已有夹具**（几只票、停一只就掉 17%
    的小截面）保留逃生口；生产入口不传该参数。关掉它会让日级门只做统计不做裁决。
    """
    available = _panel_bar_keys(execution_panel)
    sessions = {day for _symbol, day in available}
    symbols = {symbol for symbol, _day in available}
    cross_keys = _panel_bar_keys(cross_check_panel)

    decision_dates = {_decision_key(item)[1] for item in decisions}
    execution_counts = _session_bar_counts(execution_panel)
    feature_counts = (
        _session_bar_counts(cross_check_panel) if cross_check_panel is not None else None
    )
    session_health, session_defects = assess_decision_session_health(
        decision_dates=sorted(decision_dates),
        execution_counts=execution_counts,
        feature_counts=feature_counts,
        min_session_breadth_ratio=min_session_breadth_ratio,
        min_panel_breadth_ratio=min_panel_breadth_ratio,
        baseline_sessions=DEFAULT_SESSION_BREADTH_BASELINE_SESSIONS,
        min_baseline_rows=session_breadth_min_baseline_rows,
    )
    session_health["enforced"] = bool(enforce_session_health_guard)
    if session_defects and enforce_session_health_guard:
        defect_preview = "; ".join(
            f"{day} {reason}（{detail}）" for day, reason, detail in session_defects[:5]
        )
        raise PriceSeriesContractError(
            f"{context}: {len(session_defects)} 个决策日的 execution **日截面**不健康"
            f"：{defect_preview}"
            "——这一天面板自身的 bar 数相对基线塌陷，属**日级数据完整性异常**，"
            "不是逐票的不可交易；此时把缺失的 decision 全部 FILTER 掉，等于用一次训练帧"
            "少几行来给上游断供/截断记账。先修数据链路，再来 freeze"
            f"（判定 {session_health.get('judged_dates')} 日，"
            f"unjudgeable {session_health.get('unjudgeable_dates')} 日）",
            role=ROLE_EXECUTION,
        )

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
            f"{context}: {defect_total}/{total} 条 decision 的缺失**不能**按'当日无 execution "
            f"bar'过滤，而是面板结构缺陷：{defects}（例：{preview}）——（同一批里另有 {filtered} 条"
            "两侧都无 bar、本可过滤，但它们的缺失**同样未被证明是停牌**）。execution 面板必须与 "
            "feature 面板逐键覆盖同一份 PIT 候选集，结构缺陷即 target 不可用；绝不允许退回 qfq、"
            "从 qfq 反推 raw，或用过滤把断供藏起来",
            role=ROLE_EXECUTION,
        )

    required = max(0, int(max_filtered_rows_floor))
    allowed_rows = max(required, math.ceil(float(max_filtered_ratio) * max(1, total)))
    if filtered > allowed_rows:
        raise PriceSeriesContractError(
            f"{context}: {filtered}/{total} 条 decision 因'当日无 execution bar'被过滤"
            f"（{filtered / max(1, total):.4%}）超过审计上限 {allowed_rows} 行"
            f"（max_filtered_ratio={max_filtered_ratio}, floor={required}）——这个规模不是"
            "逐票不可交易能解释的，先查 execution 面板是否断供/被截断"
            f"（样例：{filtered_examples[:5]}）",
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
            "当天一半股票同时不可交易",
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
        # 这一行是**给下一个读审计的人看的**：过滤原因码里带 "NO_EXECUTION_BAR"，
        # 但它不代表停牌。仓库没有独立 PIT-safe 停牌真值源，且两侧面板共享上游链路，
        # "双边同缺"对停牌与对称断供给不出不同答案（见函数文档）。
        "filter_reason_semantics": "execution_observation_unavailable_NOT_PROVEN_SUSPENDED",
        "filtered_examples": filtered_examples,
        "filtered_dates": int(len(per_date_filtered)),
        "max_daily_filtered_date": worst_date,
        "max_daily_filtered_ratio": round(worst_ratio, 8),
        # 过滤最集中的 5 天：日级异常必须一眼可见，而不是只留一个"最大占比"数字。
        "filtered_dates_top": top_dates_payload,
        # 日截面健康门（**先于**上面的逐键裁决执行）：不依赖 decision 集合的那一层证据。
        "session_health": dict(session_health),
        "execution_panel_bars": int(len(available)),
        "execution_panel_sessions": int(len(sessions)),
        "execution_panel_symbols": int(len(symbols)),
        "cross_check_panel_source": str(getattr(cross_check_panel, "source", "") or ""),
        "cross_check_bar_keys": int(len(cross_keys)),
        "limits": {
            # 两条比例闸 = provisional anomaly guard（量级报警），不是停牌定义。
            "max_filtered_ratio": float(max_filtered_ratio),
            "max_daily_filtered_ratio": float(max_daily_filtered_ratio),
            "max_filtered_rows_floor": required,
            "ratio_gates_are_suspension_definition": False,
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

    训练帧不用这个（PIT 候选里本来就含"当日拿不到 execution bar"的票，见
    :func:`filter_decisions_by_execution_availability`）；它保留给"必须逐条对齐"的
    路径与测试，语义与 2026-09-21 的原始守卫**完全一致**。

    ⚠️ 截至 ``f2596ce`` **只有测试在调用它**——所称的研究/回放生产入口不存在。
    要么补上调用方，要么删掉这段定位（否则又是一处"文档承诺未兑现"）。
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
    "DEFAULT_MIN_PANEL_BREADTH_RATIO",
    "DEFAULT_MIN_SESSION_BREADTH_RATIO",
    "DEFAULT_SESSION_BREADTH_BASELINE_SESSIONS",
    "DEFAULT_SESSION_BREADTH_MIN_BASELINE_ROWS",
    "DEFECT_REASON_CROSS_PANEL_DIVERGENCE",
    "DEFECT_REASON_DATE_NOT_SESSION",
    "DEFECT_REASON_SESSION_BREADTH_COLLAPSE",
    "DEFECT_REASON_SESSION_BELOW_FEATURE_BREADTH",
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
    "assess_decision_session_health",
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
