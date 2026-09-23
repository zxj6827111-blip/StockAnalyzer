"""Alpha V2 双价格源训练帧构造（P0：feature 用 qfq，label/target 用 raw）。

**这个模块存在的原因**：冻结脚本此前是"一个 ``--market-db`` 走到底"——特征、label、
基准、MAE/MFE 全部来自同一份序列。生产 NAS 的正式库是 qfq，于是训练目标建立在复权价
之上。本模块把训练帧的构造固定成一条**单向数据流**：

```text
feature_panel (qfq)      ──► FeatureEngineer  ──► X（特征矩阵）
                              compute_style_features ──► 风格维度（仅用于基准分组）
execution_panel (raw)    ──► build_label_v2    ──► y（net/excess/up/mae/mfe）+ benchmark EW
```

三条不变量（有测试钉住，DP-1..DP-4）：

1. **守卫先于重活**：``require_certified_execution_series`` 在构造特征矩阵之前执行——
   execution 不是 raw+certified 就直接抛错，不允许跑几十分钟才失败；
2. **逐项对齐但区分两种"缺"**：每条 decision 的 ``(symbol, date)`` 若在 execution 面板
   没有当日 bar，要分**合法停牌**（PIT 候选池按设计包含"最近 5 个交易日内交易过、当天
   停牌"的票 → 过滤掉，永远不进训练帧）与**结构缺陷**（整票缺席 / 整天不是交易日 /
   feature 侧有 bar 而 execution 没有 → fail closed）。判据、量级闸与审计字段见
   :func:`~stock_analyzer.alpha_v2.dual_price_series.filter_decisions_by_execution_availability`；
3. **绝对收益只来自 raw**：``net_return_*`` / ``excess_return_*`` / ``up_*`` /
   ``mae_*`` / ``mfe_*`` 与全部基准序列都由 execution 面板的 outcome 派生；
   feature 侧只贡献特征值与风格分组维度。

三段集合必须能分别回答（``evidence["decision_alignment"]`` 就是这张账）：

```text
decision universe      PIT eligible ∩ feature 日历           = decision_rows_before
execution available    上述 ∩ execution 面板当日有 bar        = decision_rows_after
training frame         上述 ∩ feature frame ∩ 有 label 的行    = len(frame)
```

过滤发生在**构造 label / 特征 / 风格之前**，所以被过滤的行不会以"无 label 的空行"
形态混进训练矩阵（``fit_frozen_model`` 的 ``labels.notna()`` 掩码只是最后一道兜底，
不是这里的第一道）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from stock_analyzer.alpha_v2.dual_price_series import (
    ROLE_EXECUTION,
    filter_decisions_by_execution_availability,
    require_certified_execution_series,
    require_declared_feature_series,
)
from stock_analyzer.alpha_v2.research.benchmarks import (
    BenchmarkSpec,
    build_benchmark_suite,
    compute_style_features,
    merge_primary_excess,
)
from stock_analyzer.alpha_v2.research.feature_audit import safe_feature_columns
from stock_analyzer.alpha_v2.research.multi_head import build_head_targets
from stock_analyzer.alpha_v2.research.outcomes import (
    DecisionPoint,
    OutcomeRun,
    OutcomeSpec,
    build_label_v2,
)
from stock_analyzer.alpha_v2.research.panel import DailyPanel, PriceModeCertification
from stock_analyzer.alpha_v2.validation.feature_frame import daily_feature_frame
from stock_analyzer.backtest.matcher import ExecutionMatcher

# 身份列 + 掩码列：既不是特征也不是目标，交给调用方（冻结脚本）自己加。
IDENTITY_COLUMNS: tuple[str, ...] = ("decision_date", "symbol")


@dataclass
class DualPriceTrainingFrame:
    """构建结果：训练帧 + 两条口径各自的证据（全部进审计 / provenance）。"""

    frame: pd.DataFrame
    safe_feature_columns: tuple[str, ...]
    execution_run: OutcomeRun
    evidence: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "rows": int(len(self.frame)),
            "safe_feature_columns": list(self.safe_feature_columns),
            "evidence": dict(self.evidence),
        }


def build_dual_price_training_frame(
    *,
    feature_panel: DailyPanel,
    execution_panel: DailyPanel,
    decisions: Sequence[DecisionPoint],
    matcher: ExecutionMatcher,
    slippage_ratio: float,
    execution_certification: PriceModeCertification,
    feature_certification: PriceModeCertification | None = None,
    expected_feature_mode: str = "",
    spec: OutcomeSpec | None = None,
    benchmark_spec: BenchmarkSpec | None = None,
    context: str = "freeze",
) -> DualPriceTrainingFrame:
    """按双价格源契约构造训练帧（守卫 → 对齐 → label → 特征 → 目标）。

    ``decisions`` 为空（PIT 合格池在窗口内一个都没选出来）时直接报错：空训练集不是
    "没数据也能跑"，而是口径/窗口配置错了——让它在这里以明确的信息停下，而不是
    在后面的 merge 里以 ``KeyError: 'decision_date'`` 的形式冒出来。
    """
    if not decisions:
        raise ValueError(
            f"{context}: 决策集合为空（PIT 合格池在窗口内为空）——检查窗口起点、"
            "warmup_days 与面板历史长度；空训练帧不是合法产物"
        )
    # ── 1) 守卫（必须在任何重活之前）──────────────────────────────────────────
    reviewed = require_certified_execution_series(
        execution_certification,
        context=f"{context}:execution_panel",
        db=str(execution_panel.source or ""),
    )
    if feature_certification is not None:
        require_declared_feature_series(
            feature_certification,
            context=f"{context}:feature_panel",
            expected_mode=expected_feature_mode,
            db=str(feature_panel.source or ""),
        )

    # ── 2) decision 可用性过滤（合法停牌 → 过滤；面板结构缺陷 → fail closed）─────
    # PIT 候选池按设计包含"最近 5 个交易日内交易过、当天停牌"的票（含退市/长停期间
    # 仍在 history 窗口内的票），它们当天没有 execution bar 是**预期内事实**——
    # 这类行拿不到 target（T+1 入场不可成立），必须在构造 label/特征之前就出局。
    # 但"整票缺席 / 整天不是交易日 / feature 侧有 bar 而 execution 没有"是面板缺陷，
    # 由同一函数 fail closed（详见该函数文档）。cross_check_panel=feature 面板：
    # 两份面板必须对"这只票这天有没有交易"给同一个答案。
    availability = filter_decisions_by_execution_availability(
        decisions=decisions,
        execution_panel=execution_panel,
        cross_check_panel=feature_panel,
        context=f"{context}:execution_panel",
    )
    alignment = availability.report
    training_decisions = list(availability.decisions)
    if not training_decisions:
        raise ValueError(
            f"{context}: execution 可用决策集为空（{len(decisions)} 条候选全部无当日 bar）"
            "——检查两份面板的窗口与交易日历；空训练帧不是合法产物"
        )

    # ── 3) 绝对收益 / 基准：全部来自 raw execution 面板 ────────────────────────
    run = build_label_v2(
        panel=execution_panel,
        decisions=training_decisions,
        spec=spec,
        matcher=matcher,
        slippage_ratio=slippage_ratio,
        price_mode=reviewed.mode,
        price_mode_certified=reviewed.certified,
        source_meta={
            "feature_panel_source": str(feature_panel.source),
            "execution_panel_source": str(execution_panel.source),
            "execution_price_mode": str(reviewed.mode),
            "execution_role": ROLE_EXECUTION,
            "decision_rows_before_availability_filter": availability.decision_rows_before,
            "decision_rows_filtered": availability.filtered_rows,
        },
    )
    if run.frame.empty:  # pragma: no cover - 与上面的空决策检查同因，防御性兜底
        raise ValueError(f"{context}: outcome 帧为空（{len(decisions)} 条决策全部无结果）")
    # 风格维度取 feature 侧契约（分组/匹配用，不是收益来源）。
    styles = compute_style_features(panel=feature_panel, decisions=training_decisions)
    enriched = run.frame.merge(styles, on=["decision_date", "symbol"], how="left")
    suite = build_benchmark_suite(
        enriched, spec=benchmark_spec or BenchmarkSpec(style_min_peers=5)
    )
    primary = merge_primary_excess(enriched, suite)

    # ── 4) 特征矩阵：只来自 feature 面板 ──────────────────────────────────────
    features_all = daily_feature_frame(feature_panel, training_decisions)
    safe = [column for column in features_all.columns if column not in set(IDENTITY_COLUMNS)]
    safe = list(safe_feature_columns(safe))
    features = features_all[[*IDENTITY_COLUMNS, *safe]].copy()
    frame = features.merge(primary, on=list(IDENTITY_COLUMNS), how="inner")
    frame = build_head_targets(frame)

    evidence: dict[str, object] = {
        "context": str(context),
        "feature_panel_source": str(feature_panel.source),
        "execution_panel_source": str(execution_panel.source),
        "execution_price_mode": str(reviewed.mode),
        "execution_price_mode_certified": bool(reviewed.certified),
        "decision_alignment": dict(alignment),
        # 三段集合的同一张账：候选 → execution 可用 → outcome 行 → 训练帧行。
        # 末两段不再相等是正常的（未成熟/未成交的行拿不到 label），但**第一段到
        # 第二段的差**必须能被 decision_alignment 的审计字段完全解释。
        "decision_accounting": {
            "decision_universe_rows": availability.decision_rows_before,
            "execution_available_rows": availability.decision_rows_after,
            "outcome_rows": int(len(run.frame)),
            "training_frame_rows": int(len(frame)),
        },
        "benchmark_layers": sorted(suite.series),
        "benchmark_primary_layer": str(suite.primary),
        "quality_pool_source": str(suite.report.get("quality_pool_source", "research_proxy")),
        "outcome_diagnostics": {
            key: value
            for key, value in run.diagnostics.items()
            if key in {"price_mode", "price_mode_certified", "main_sample_rows", "main_sample_status"}
        },
        "execution_price_series_enforced": bool(
            run.diagnostics.get("execution_price_series_enforced", True)
        ),
    }
    return DualPriceTrainingFrame(
        frame=frame,
        safe_feature_columns=tuple(safe),
        execution_run=run,
        evidence=evidence,
    )


def select_frame_columns(
    frame: pd.DataFrame,
    *,
    safe_features: Sequence[str],
    targets: Sequence[str],
    extra_columns: Sequence[str] = IDENTITY_COLUMNS,
) -> pd.DataFrame:
    """裁剪训练矩阵：只留 身份列 + 安全特征 + 目标列（style_* 等对照列不进训练）。"""
    keep = set(extra_columns) | set(safe_features) | set(targets)
    return frame[[column for column in frame.columns if str(column) in keep]]


__all__ = [
    "IDENTITY_COLUMNS",
    "DualPriceTrainingFrame",
    "build_dual_price_training_frame",
    "select_frame_columns",
]
