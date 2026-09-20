"""Alpha V2 特征可用性与穿越审计（S14 / 原 P1-04）——**Base V2 的准入闸门**。

**它回答什么**：每个特征组在决策时点 T（收盘后 15:30）到底能不能拿到？

蓝图 §P1-04 的关键区分是：

```text
"数值所属报告期 <= as_of"   ≠   "available_at <= decision_time"
```

例如 2026-03-31 的一季报，可能到 4 月下旬才公告；融资融券余额是 T+1 晚上披露；
龙虎榜/资金流是收盘后才发布。**只看报告期会把未来信息喂进模型**，这正是
Phase 2 里"98 个特征全 NaN、63 个显著负 IC"那类问题的上游成因之一。

三条硬纪律（Gate S14 的 Blocking 项）：

1. **无证据 = 不安全**：每个组必须有 ``asof_safe`` 三态判定（``proven`` /
   ``refuted`` / ``unverified``）与判定依据；只有 ``proven`` 才允许进 Base V2；
2. **未登记列一律排除**：特征列若不匹配任何已登记组，算 ``unregistered``，
   默认不进 Base V2（要进必须先在 :data:`FEATURE_GROUPS` 登记并给出证据）；
3. **缺失不等于 0**：为高风险组生成 ``missing_<group>`` 标记列，并做
   "常数/零填充伪装"检测（一列若长期取同一值，很可能是 fillna(0) 掩盖了缺失，
   而不是"真实值为 0"）。

第一轮 Base V2 因此优先使用 :data:`DAILY_ONLY_SAFE_GROUPS`：只用日线与指数
收盘即可计算、可证明无穿越的特征。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

NOT_AVAILABLE = "not_available"

AUDIT_SCHEMA = "alpha_v2_feature_audit.v1"

# asof_safe 三态
ASOF_SAFE_PROVEN = "proven"
ASOF_SAFE_REFUTED = "refuted"
ASOF_SAFE_UNVERIFIED = "unverified"

# 组 id
GROUP_PRICE_VOLUME = "price_volume_technical"
GROUP_MARKET_RELATIVE = "market_relative"
GROUP_STATE = "market_state"
GROUP_FINANCIAL = "financial_pit"
GROUP_INTRAADAY = "intraday_summary"
GROUP_SHAREHOLDER = "shareholder_count"
GROUP_NORTHBOUND = "northbound"
GROUP_MARGIN = "margin_financing"
GROUP_BLOCK_TRADE = "block_trade"
GROUP_DRAGON_TIGER = "dragon_tiger_inst"
GROUP_MONEYFLOW = "moneyflow"
GROUP_HK_HOLD = "hk_hold"
GROUP_CALENDAR = "calendar"
GROUP_BACKGROUND_META = "background_completeness_meta"
GROUP_LEARNING = "learning_protocol_derived"
GROUP_NEWS = "news_theme_derived"
GROUP_UNREGISTERED = "unregistered"

# 身份/键列不是特征：审计时跳过它们，否则"未登记列"统计里会混进日期与代码
IDENTITY_COLUMNS: frozenset[str] = frozenset({"decision_date", "symbol", "trade_date", "date"})

MISSING_POLICY_FILL_ZERO = "fill_zero_after_shift"
MISSING_POLICY_KEEP_NAN = "keep_nan"
MISSING_POLICY_FLAG = "nan_plus_group_missing_flag"


@dataclass(frozen=True, slots=True)
class FeatureGroupSpec:
    """一个特征组的可用性元数据（审计的最小单位）。"""

    group_id: str
    source: str
    available_at_rule: str
    asof_safe: str
    asof_evidence: str
    missing_policy: str
    price_series_mode: str
    in_base_v2: bool
    prefixes: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    risk_note: str = ""

    def matches(self, column: str) -> bool:
        text = str(column)
        if text in self.names:
            return True
        if any(text == prefix for prefix in self.prefixes):
            return True
        if any(text.startswith(prefix) for prefix in self.prefixes):
            return True
        return any(re.match(pattern, text) for pattern in self.patterns)

    def to_payload(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "source": self.source,
            "available_at_rule": self.available_at_rule,
            "asof_safe": self.asof_safe,
            "asof_evidence": self.asof_evidence,
            "missing_policy": self.missing_policy,
            "price_series_mode": self.price_series_mode,
            "in_base_v2": bool(self.in_base_v2),
            "risk_note": self.risk_note,
        }


# 特征组登记表。``asof_safe`` 的判定依据必须写成**可检查的规则**，不能写"应该没问题"。
FEATURE_GROUPS: tuple[FeatureGroupSpec, ...] = (
    FeatureGroupSpec(
        group_id=GROUP_PRICE_VOLUME,
        source="market_duckdb.daily_bars（OHLCV / turnover / float_market_cap）",
        available_at_rule="T 日收盘后即可得（当日行情为收盘价，盘中不可用，盘后决策可用）",
        asof_safe=ASOF_SAFE_PROVEN,
        asof_evidence=(
            "全部由同一行及**更早行**的滚动/差分派生（rolling/shift/ewm），实现见 "
            "feature/engineer.py；不含任何未来窗口，机械检查见 mechanical_checks"
        ),
        missing_policy=MISSING_POLICY_KEEP_NAN,
        price_series_mode="raw_or_qfq（特征口径，成交口径另见 price_contract）",
        in_base_v2=True,
        prefixes=(
            "close_t1",
            "ret_",
            "log_ret_",
            "overnight_gap",
            "intraday_ret",
            "hl_range_pct",
            "body_pct",
            "upper_shadow_pct",
            "lower_shadow_pct",
            "ma",
            "ema",
            "volatility_",
            "volume_ratio_",
            "turnover_ratio_",
            "turnover_rate",
            "turnover_zscore",
            "volume_zscore",
            "price_volume_corr",
            "mfi",
            "obv_",
            "adl_",
            "pvt_",
            "vwap_gap",
            "boll_",
            "close_rank",
            "volume_rank",
            "turnover_rank",
            "amplitude_rank",
            "atr",
            "downside_vol",
            "upside_vol",
            "distance_high",
            "distance_low",
            "drawdown_",
            "macd_",
            "rsi",
            "stoch_",
            "cci",
            "williams_r",
            "trend_slope_",
            "ma_gap_",
            "ema_gap_",
            "volume",
            "turnover",
            "float_market_cap",
            "amplitude",
            "gap_",
            "realized_skew",
            "realized_kurt",
        ),
    ),
    FeatureGroupSpec(
        group_id=GROUP_MARKET_RELATIVE,
        source="index_daily（基准指数日线）",
        available_at_rule="指数 T 日收盘后可得；与个股同为盘后决策口径",
        asof_safe=ASOF_SAFE_PROVEN,
        asof_evidence=(
            "只使用 index close 的 ≤T 滚动统计（excess_ret / rs_ma / beta 族）；"
            "指数与个股同日收盘发布，无先后穿越"
        ),
        missing_policy=MISSING_POLICY_KEEP_NAN,
        price_series_mode="index_close",
        in_base_v2=True,
        prefixes=(
            "excess_ret_",
            "excess_vol_",
            "rs_",
            "beta_",
            "rolling_beta",
            "market_",
            "index_",
            "rel_",
            "relative_strength",
        ),
    ),
    FeatureGroupSpec(
        group_id=GROUP_CALENDAR,
        source="交易日历（年月日/星期）",
        available_at_rule="决策日当天已知（纯日历量）",
        asof_safe=ASOF_SAFE_PROVEN,
        asof_evidence="由 decision_date 本身派生（sin/cos 编码），不含任何行情信息",
        missing_policy=MISSING_POLICY_KEEP_NAN,
        price_series_mode="n/a",
        in_base_v2=True,
        names=("month_cos", "month_sin", "weekday_cos", "weekday_sin"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_BACKGROUND_META,
        source="背景数据完整度元特征",
        available_at_rule="同行背景列的可得性计数",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence=(
            "取值本身只依赖同行背景列是否为空，但**背景列自身的 PIT 安全性尚未证明**"
            "（见 financial/margin/block_trade 组），因此它继承同一不确定性 → 未证明"
        ),
        missing_policy=MISSING_POLICY_KEEP_NAN,
        price_series_mode="n/a",
        in_base_v2=False,
        names=("background_completion_score",),
    ),
    FeatureGroupSpec(
        group_id=GROUP_STATE,
        source="daily_bars.is_st / is_delisting_risk / board",
        available_at_rule="当日状态位（交易所当日生效）",
        asof_safe=ASOF_SAFE_PROVEN,
        asof_evidence="状态列随当日行携带，属「当日已生效事实」，不含未来状态回填",
        missing_policy=MISSING_POLICY_KEEP_NAN,
        price_series_mode="n/a",
        in_base_v2=True,
        names=("bg_is_st", "bg_is_delisting_risk", "bg_board_code", "board_code"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_FINANCIAL,
        source="financial_snapshots（按公告日 as-of 物化进 daily_bars）",
        available_at_rule="**公告日**而非报告期；行内 financial_as_of 必须 ≤ 决策日",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence=(
            "行内值由 enrich_daily_financial_pit 按公告日 as-of 生成；但「公告日 <= T」"
            "这件事依赖上游回填链路（2026-09-05 data_gate 修复后才成立），"
            "本轮未逐行复核 → 未证明。机械检查只统计 financial_as_of > trade_date 的行"
        ),
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("bg_roe", "bg_debt_ratio", "roe_", "debt_ratio_", "bg_financial"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_INTRAADAY,
        source="market_duckdb.intraday_summary_1m/5m（分钟聚合）",
        available_at_rule="当日分钟数据聚合；需要分钟链当日健康写入",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence=(
            "分钟链 2026-04~07 存在断供与空洞（历史 coverage 不完整），"
            "无法证明「决策时刻已有当日完整聚合」→ 未证明"
        ),
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="minute_raw",
        in_base_v2=False,
        patterns=(r"^i1m_", r"^i5m_", r"^intraday_", r"^morning", r"^tail", r"^am_", r"^pm_"),
        risk_note="蓝图 §3.5：分钟链未恢复完整性前不得进入 Base Alpha",
    ),
    FeatureGroupSpec(
        group_id=GROUP_SHAREHOLDER,
        source="股东人数（定期披露）",
        available_at_rule="随定期报告披露，披露日 ≠ 报告期",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="披露时点未在行内标注可用日 → 无法证明 available_at ≤ T",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("holder_count", "bg_holder"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_NORTHBOUND,
        source="沪深股通持股（每日披露）",
        available_at_rule="当日收盘后披露，具体可见时点未核实",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="披露时点与 15:30 决策时点的先后未取证 → 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("northbound_", "bg_northbound"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_MARGIN,
        source="融资融券余额（交易所 T+1 晚间披露）",
        available_at_rule="**T+1 晚间**披露上一交易日数据",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence=(
            "披露滞后于 T 日 15:30 决策；行内对齐方式未取证（若上游按公告日平移则安全，"
            "若按报告日对齐则含未来信息）→ 未证明"
        ),
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("financing_", "margin_", "bg_margin"),
        risk_note="已知披露滞后 T+1：高风险，需逐行 as-of 证据",
    ),
    FeatureGroupSpec(
        group_id=GROUP_BLOCK_TRADE,
        source="大宗交易（盘后披露）",
        available_at_rule="当日盘后披露",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="披露时点与 15:30 决策时点的先后未取证 → 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("block_trade_", "bg_block_trade"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_DRAGON_TIGER,
        source="龙虎榜 / 机构席位（盘后披露）",
        available_at_rule="当日盘后披露",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="披露时点与 15:30 决策时点的先后未取证 → 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("dragon_tiger", "inst_net", "bg_dragon"),
    ),
    FeatureGroupSpec(
        group_id=GROUP_MONEYFLOW,
        source="资金流向（盘后披露）",
        available_at_rule="当日盘后披露",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="披露时点与 15:30 决策时点的先后未取证 → 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("moneyflow_",),
    ),
    FeatureGroupSpec(
        group_id=GROUP_HK_HOLD,
        source="陆股通持股明细",
        available_at_rule="每日披露",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="披露时点未取证 → 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("hk_hold",),
    ),
    FeatureGroupSpec(
        group_id=GROUP_LEARNING,
        source="learning protocol / 反馈特征",
        available_at_rule="依赖协议产物生成时点",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="协议派生量的生成时点与决策时点关系未取证 → 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("lp_",),
    ),
    FeatureGroupSpec(
        group_id=GROUP_NEWS,
        source="新闻 / 主题派生量",
        available_at_rule="新闻发布时间需可证明 ≤ T 15:30",
        asof_safe=ASOF_SAFE_UNVERIFIED,
        asof_evidence="新闻路径尚未完成 PIT 化（蓝图 §3.4：news 保持 shadow）→ 未证明",
        missing_policy=MISSING_POLICY_FLAG,
        price_series_mode="n/a",
        in_base_v2=False,
        prefixes=("news_", "theme_"),
    ),
)

DAILY_ONLY_SAFE_GROUPS: tuple[str, ...] = tuple(
    spec.group_id for spec in FEATURE_GROUPS if spec.in_base_v2
)

# 已知的"疑似 0 填充"列名特征（用于常数检测的提示，不代替检测）
_ZERO_FILL_POLICY_MARKERS = ("fill_zero_after_shift", "fillna(0)", "zero_filled")

# outcome / label 泄漏列黑名单。**必须先行判定**：这些列是 S11 的收益与成熟结果，
# 出现在特征矩阵里等价于把答案当特征（同一前缀还可能与行情特征撞车，例如
# ``excess_ret_5`` 是行情特征、``excess_return_5d`` 是 outcome）。
OUTCOME_LEAK_PATTERNS: tuple[str, ...] = (
    r"^label$",
    r"^label_",
    r"^fwd_return$",
    r"^(net_return|excess_return|up_net|up_excess|mae|mfe)_\d+d$",
    r"^tp8_before_sl5_\d+d$",
    r"^tp8_conflict_\d+d$",
    r"^matured_\d+d$",
    r"^maturity_date_\d+d$",
    r"^exit_no_fill_\d+d$",
    r"^(entry|exit)_",
    r"^rule_",
    r"^main_sample",
)
_OUTCOME_LEAK_REGEXES = tuple(re.compile(pattern) for pattern in OUTCOME_LEAK_PATTERNS)


def is_outcome_leak_column(column: str) -> bool:
    """判断列名是否属于 outcome/label 族（禁止作为特征进入 Base V2）。"""
    text = str(column)
    return any(regex.match(text) for regex in _OUTCOME_LEAK_REGEXES)


CONSTANT_RATIO_THRESHOLD = 0.995
NULL_RATIO_THRESHOLD = 0.98


@dataclass
class FeatureAuditReport:
    """一次特征审计的完整结论。"""

    groups: tuple[FeatureGroupSpec, ...]
    assignments: dict[str, str]
    unregistered: tuple[str, ...]
    safe_feature_columns: tuple[str, ...]
    excluded_feature_columns: tuple[str, ...]
    coverage: dict[str, dict[str, float]] = field(default_factory=dict)
    mechanical_checks: dict[str, object] = field(default_factory=dict)
    constant_suspects: tuple[str, ...] = ()
    missing_ratio: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": AUDIT_SCHEMA,
            "base_v2_feature_columns": list(self.safe_feature_columns),
            "base_v2_group_ids": [spec.group_id for spec in self.groups if spec.in_base_v2],
            "excluded_columns_count": len(self.excluded_feature_columns),
            "excluded_columns_sample": list(self.excluded_feature_columns[:100]),
            "unregistered_columns": list(self.unregistered),
            "groups": [spec.to_payload() for spec in self.groups],
            "group_coverage": self.coverage,
            "mechanical_checks": self.mechanical_checks,
            "constant_suspects": list(self.constant_suspects[:100]),
            "missing_ratio": self.missing_ratio,
            "policy": "unproven_feature_must_not_enter_base_v2",
        }


def classify_feature_columns(
    columns: Iterable[str],
    *,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
) -> tuple[dict[str, str], list[str]]:
    """把列名映射到组 id；未匹配任何组 → ``unregistered``（fail-closed 排除）。"""
    assignment: dict[str, str] = {}
    unregistered: list[str] = []
    for column in columns:
        text = str(column)
        if text.startswith("__") or text in IDENTITY_COLUMNS:
            continue
        if is_outcome_leak_column(text):
            # 黑名单优先：即使前缀与某个安全组撞车，也一律判为未登记（=不可用于 Base V2）
            assignment[text] = GROUP_UNREGISTERED
            unregistered.append(text)
            continue
        matched = GROUP_UNREGISTERED
        for spec in groups:
            if spec.matches(text):
                matched = spec.group_id
                break
        assignment[text] = matched
        if matched == GROUP_UNREGISTERED:
            unregistered.append(text)
    return assignment, sorted(unregistered)


def safe_feature_columns(
    columns: Iterable[str],
    *,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
) -> tuple[str, ...]:
    """可进 Base V2 的列（只含 ``asof_safe=proven`` 且登记为 in_base_v2 的组）。"""
    assignment, _ = classify_feature_columns(columns, groups=groups)
    safe_groups = {spec.group_id for spec in groups if spec.in_base_v2}
    return tuple(sorted(column for column, group in assignment.items() if group in safe_groups))


def assert_safe_feature_columns(
    columns: Iterable[str],
    *,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
) -> tuple[str, ...]:
    """Base V2 特征矩阵的**准入断言**：含未证明/未登记列即报错。

    这是"无法证明 PIT 的 feature 不得进入 Base V2"的可执行形式——不是文档约定，
    而是构建矩阵时的硬闸门。
    """
    requested = [str(column) for column in columns]
    safe = set(safe_feature_columns(requested, groups=groups))
    rejected = [column for column in requested if column not in safe]
    if rejected:
        assignment, _ = classify_feature_columns(rejected, groups=groups)
        detail = ", ".join(f"{column}({assignment.get(column)})" for column in rejected[:20])
        raise ValueError(
            "Base V2 特征矩阵包含未经 PIT 证明的列，fail-closed 拒绝："
            f"{detail}{' ...' if len(rejected) > 20 else ''}"
        )
    return tuple(sorted(safe))


def audit_feature_columns(
    columns: Iterable[str],
    *,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
    frame: pd.DataFrame | None = None,
    constant_ratio_threshold: float = CONSTANT_RATIO_THRESHOLD,
    null_ratio_threshold: float = NULL_RATIO_THRESHOLD,
) -> FeatureAuditReport:
    """列级审计：分组、安全集、覆盖率、常数/空值疑似伪装。"""
    assignment, unregistered = classify_feature_columns(columns, groups=groups)
    safe = safe_feature_columns(columns, groups=groups)
    safe_set = set(safe)
    excluded = tuple(sorted(column for column in assignment if column not in safe_set))

    coverage: dict[str, dict[str, float]] = {}
    missing_ratio: dict[str, float] = {}
    constant_suspects: tuple[str, ...] = ()
    if frame is not None and not frame.empty:
        for column in assignment:
            if column not in frame.columns:
                continue
            series = frame[column]
            try:
                numeric = pd.to_numeric(series, errors="coerce")
            except (TypeError, ValueError):
                continue
            total = len(numeric)
            if total == 0:
                continue
            non_null = int(numeric.notna().sum())
            ratio = non_null / total
            missing_ratio[column] = round(1.0 - ratio, 6)
            group = assignment[column]
            bucket = coverage.setdefault(group, {"columns": 0.0, "mean_non_null_ratio": 0.0})
            bucket["columns"] += 1.0
            bucket["mean_non_null_ratio"] += ratio
        for group, bucket in coverage.items():
            if bucket["columns"] > 0:
                coverage[group]["mean_non_null_ratio"] = round(
                    bucket["mean_non_null_ratio"] / bucket["columns"], 6
                )
        constant_suspects = tuple(
            sorted(
                column
                for column, ratio in missing_ratio.items()
                if ratio >= null_ratio_threshold
                or _constant_ratio(frame[column]) >= constant_ratio_threshold
            )
        )

    return FeatureAuditReport(
        groups=tuple(groups),
        assignments=assignment,
        unregistered=tuple(unregistered),
        safe_feature_columns=safe,
        excluded_feature_columns=excluded,
        coverage=coverage,
        constant_suspects=constant_suspects,
        missing_ratio=missing_ratio,
    )


def _constant_ratio(series: pd.Series) -> float:
    """众数占比：接近 1 说明这一列几乎是常数（很可能是 fillna(0) 掩盖了缺失）。"""
    numeric = pd.to_numeric(series, errors="coerce")
    numeric = numeric[np.isfinite(numeric.to_numpy(dtype=float))]
    if numeric.empty:
        return 1.0
    counts = numeric.value_counts(dropna=True)
    return float(counts.iloc[0]) / float(len(numeric))


def feature_group_missing_flags(
    frame: pd.DataFrame,
    *,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
    columns: Iterable[str] | None = None,
) -> pd.DataFrame:
    """为每组生成 ``missing_<group>`` 标记：**缺失 ≠ 真实值 0**。

    组内全部列为空（或全部不存在）→ 该行 ``missing_<group>=True``。
    """
    selected = list(columns) if columns is not None else list(frame.columns)
    assignment, _ = classify_feature_columns(selected, groups=groups)
    result = pd.DataFrame(index=frame.index)
    for spec in groups:
        members = [
            column
            for column, group in assignment.items()
            if group == spec.group_id and column in frame.columns
        ]
        if not members:
            result[f"missing_{spec.group_id}"] = True
            continue
        present = pd.DataFrame(
            {column: frame[column].notna() for column in members}, index=frame.index
        )
        result[f"missing_{spec.group_id}"] = ~present.any(axis=1)
    return result


def mechanical_checks(panel: pd.DataFrame) -> dict[str, object]:
    """能在数据上机械验证的 PIT 检查（不能验证的如实写 not_verified）。

    当前实现：

    - ``financial_asof_le_trade_date``：若存在 ``financial_as_of`` 列，统计
      ``financial_as_of > trade_date`` 的行数（>0 即穿越）；
    - ``financial_report_date_le_trade_date``：报告期不得晚于交易日（粗筛，
      报告期 ≤ 交易日并不等于 PIT 安全，只用于发现明显错误）；
    - ``price_series_mode_declared``：行内价格口径声明分布。
    """
    result: dict[str, object] = {"schema": "alpha_v2_feature_mechanical_checks.v1"}
    if panel is None or panel.empty:
        result["status"] = "no_panel"
        return result
    trade_date = pd.to_datetime(panel.get("trade_date"), errors="coerce")

    asof_column = _first_present_column(panel, ("financial_as_of",))
    if asof_column is None:
        result["financial_asof_le_trade_date"] = "column_absent"
    else:
        asof = pd.to_datetime(panel[asof_column], errors="coerce")
        violations = int((asof > trade_date).sum())
        result["financial_asof_le_trade_date"] = {
            "checked_rows": int(asof.notna().sum()),
            "violations": violations,
            "status": "ok" if violations == 0 else "violation",
        }

    report_column = _first_present_column(panel, ("financial_report_date",))
    if report_column is None:
        result["financial_report_date_le_trade_date"] = "column_absent"
    else:
        report = pd.to_datetime(panel[report_column], errors="coerce")
        violations = int((report > trade_date).sum())
        result["financial_report_date_le_trade_date"] = {
            "checked_rows": int(report.notna().sum()),
            "violations": violations,
            "status": "ok" if violations == 0 else "violation",
        }

    mode_column = _first_present_column(panel, ("price_series_mode",))
    if mode_column is None:
        result["price_series_mode_declared"] = "column_absent"
    else:
        result["price_series_mode_declared"] = {
            str(key): int(value)
            for key, value in panel[mode_column].fillna("<null>").value_counts().items()
        }
    return result


def _first_present_column(frame: pd.DataFrame, candidates: Sequence[str]) -> str | None:
    for column in candidates:
        if column in frame.columns:
            return column
    return None


def group_columns(
    columns: Iterable[str],
    *,
    group_id: str,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
) -> tuple[str, ...]:
    """取某个组的实际列名（按当前列集合匹配）。"""
    assignment, _ = classify_feature_columns(columns, groups=groups)
    return tuple(sorted(column for column, group in assignment.items() if group == group_id))


def audit_summary(report: FeatureAuditReport) -> dict[str, object]:
    """给日报/审计工件用的精简摘要。"""
    return {
        "base_v2_columns": len(report.safe_feature_columns),
        "excluded_columns": len(report.excluded_feature_columns),
        "unregistered_columns": len(report.unregistered),
        "proven_groups": [
            spec.group_id for spec in report.groups if spec.asof_safe == ASOF_SAFE_PROVEN
        ],
        "unverified_groups": [
            spec.group_id for spec in report.groups if spec.asof_safe == ASOF_SAFE_UNVERIFIED
        ],
        "constant_suspects": len(report.constant_suspects),
        "mechanical_checks": report.mechanical_checks,
        "verdict": (
            "base_v2_uses_proven_groups_only"
            if not report.unregistered
            else "unregistered_columns_present_fail_closed"
        ),
    }


def missing_group_flag_columns(groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS) -> list[str]:
    return [f"missing_{spec.group_id}" for spec in groups]


def mean_missing_ratio(report: FeatureAuditReport, *, group_id: str) -> float | str:
    values = [
        ratio
        for column, ratio in report.missing_ratio.items()
        if report.assignments.get(column) == group_id
    ]
    if not values:
        return NOT_AVAILABLE
    return float(np.mean(values))


__all__ = [
    "ASOF_SAFE_PROVEN",
    "ASOF_SAFE_REFUTED",
    "ASOF_SAFE_UNVERIFIED",
    "AUDIT_SCHEMA",
    "CONSTANT_RATIO_THRESHOLD",
    "DAILY_ONLY_SAFE_GROUPS",
    "FEATURE_GROUPS",
    "FeatureAuditReport",
    "FeatureGroupSpec",
    "GROUP_UNREGISTERED",
    "IDENTITY_COLUMNS",
    "audit_feature_columns",
    "audit_summary",
    "assert_safe_feature_columns",
    "classify_feature_columns",
    "feature_group_missing_flags",
    "group_columns",
    "is_outcome_leak_column",
    "mean_missing_ratio",
    "mechanical_checks",
    "OUTCOME_LEAK_PATTERNS",
    "missing_group_flag_columns",
    "safe_feature_columns",
]
