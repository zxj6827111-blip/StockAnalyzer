"""Phase 2 横截面 Walk-Forward Harness（方案 §5）。

按交易日滚动：``train_window`` 训练 → ``embargo``（horizon+settlement 交易日）
→ 逐 ``step`` 日评估。数据来自 ``pit_dataset.build_pit_dataset`` 的月度
parquet 分块（PIT universe 快照、(symbol, trade_date) 逻辑键唯一）。

时间安全（方案 §5 硬口径）：
- 训练集：``label_mature_trade_date < train_end``（maturity purge）；
- 评估日：``eval_date >= train_end + embargo``（embargo 按交易日历推进）；
- lookahead 检查：fold 内出现「决策日/成熟日 ≥ 评估日」的训练样本即判违规。

指标（打分层，方案 §5）：aggregate IC、日 IC + date-block bootstrap CI、
五分位收益与 top-bottom、月度单调性、AUC/Brier、Precision@K、
universe 统计（B9 字段）。

判定（§5 放行判据，C1 修正后**真正进入 verdict**）：
``INSUFFICIENT_FOLDS``（完整 fold < 4）→ ``NO_GO``（fold 内 lookahead 违规 > 0，
指标不可用）→ ``NO_GO``（CI 上界 < 0，或 IC ≤ 0 且 CI 不跨 0：有负向证据）→
``INCONCLUSIVE``（CI 跨 0：证据不足，**不得**判 GO）→ ``GO_CANDIDATE``
（IC > 0 且 top ≥ bottom 且月度 ≥ 4/6 **且 CI 下界 > 0**）。
CI 为**连续交易日块** moving-block bootstrap（块长预设，见
``DEFAULT_BLOCK_TRADING_DAYS``），不是逐日独立重采样。
产出 JSON+MD 报告（时间戳命名不覆盖）。
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import cast

import duckdb
import numpy as np
import pandas as pd

from stock_analyzer.backtest.variants import VARIANTS, variant_definitions
from stock_analyzer.learning.scoring_eval import (
    _rankdata,
    compute_auc_brier,
    compute_quantile_returns,
    compute_rank_ic,
    date_block_bootstrap_ci,
)
from stock_analyzer.models.trainer import ModelTrainer

DEFAULT_DATASET_DIR = "/app/artifacts/phase2/pit_dataset"
DEFAULT_OUT_DIR = "/app/artifacts/phase2"

# 对照基线（2026-09-06 soup label Phase 2 NO-GO 报告）。fold 数由 plan_folds
# 按数据覆盖与窗口参数**生成**，每次运行都可能不同；基线只在两者一致时才
# 构成同口径对照，否则必须显式标"不可比"——禁止把覆盖率不同的两次运行并列
# 成"改善/退化"（C1：fold 数不得写死当作期望值）。
BASELINE_SOUP_LABEL: dict[str, object] = {
    "aggregate_ic_mean": -0.024,
    "aggregate_ic_ci95": [-0.043, -0.005],
    "folds_total": 18,
    "source": "Phase 2 NO-GO report 2026-09-06 (pit_dataset_ext v1)",
    "note": "同一 harness 配置（train=120/test=20/step=20/embargo=11）",
}


# 验证范围声明（C1 §2）：本 harness 每折重新训练，评估的是**训练流程**；
# 同一历史窗口被反复用于挑标签/特征/后，它已是"开发验证资产"，不得称最终
# 测试集，也不构成"9/13 固定工件可晋升"的证据。工件级验证必须是冻结工件在
# 未参与开发/训练/校准的**后续数据**上做 shadow 或真正前向验证（批次 D）。
VALIDATION_SCOPE_PROCESS: dict[str, object] = {
    "kind": "process",
    "statement": (
        "每折独立训练与拟合 → 评估对象是训练流程，不是固定工件回打历史；"
        "该窗口已被反复用于标签/特征选择，属开发验证资产，不得称最终测试集。"
    ),
    "artifact_level_required": (
        "冻结工件后在未参与开发/训练/校准的后续数据上做 shadow 或真正前向验证（批次 D）"
    ),
}


def baseline_comparison(current_folds: int) -> dict[str, object]:
    """基线对照写成**可判定**结果：fold 覆盖率不同即标不可比。"""

    baseline_folds = int(cast(int, BASELINE_SOUP_LABEL["folds_total"]))
    comparable = int(current_folds) == baseline_folds
    payload: dict[str, object] = dict(BASELINE_SOUP_LABEL)
    payload["comparable"] = comparable
    payload["current_folds_total"] = int(current_folds)
    payload["comparability_note"] = (
        "同口径可对照"
        if comparable
        else (
            f"fold 覆盖率不同（当前 {int(current_folds)} vs 基线 {baseline_folds}）："
            "不得直接并列比较 IC/CI"
        )
    )
    return payload


@dataclass
class FoldResult:
    fold_id: int
    train_start: str
    train_end: str
    eval_dates: list[str]
    status: str = "pending"
    invalid_reason: str = ""
    training_cutoff: str = ""
    label_mature_cutoff: str = ""
    embargo_days: int = 0
    lookahead_violations: int = 0
    daily_ic: list[tuple[str, float]] = field(default_factory=list)
    daily_top_bottom: list[tuple[str, float]] = field(default_factory=list)
    pooled_auc: float = float("nan")
    pooled_brier: float = float("nan")
    pooled_n: int = 0
    quantile_means: list[float] = field(default_factory=list)
    top_minus_bottom: float = float("nan")
    universe_stats: dict[str, float] = field(default_factory=dict)
    # fold 评估行不整帧驻留：只保留池化统计所需的紧凑数组。
    eval_scores: np.ndarray | None = None
    eval_returns: np.ndarray | None = None
    # C3 追加：分数相对反转因子的横截面诊断（逐日）。
    # 动机：C3 实测 blend 与固定反转基线 IC 难分（0.0564 vs 0.0578），
    # 所以「模型有没有额外信息」必须看扣掉反转后还剩多少，而不是看水平值。
    # daily_reversal_r2 是稳健读法（反转解释了分数排序方差的多少）；
    # daily_residual_ic 是残差 IC，仅在残差方差不可忽略的日子才有意义。
    daily_residual_ic: list[tuple[str, float]] = field(default_factory=list)
    daily_reversal_r2: list[tuple[str, float]] = field(default_factory=list)
    residual_ic_degenerate_days: int = 0
    # C6 合并实验：每日横截面内把模型分数与 −ret_20d 各自秩标准化后按权重合并。
    # 键 = ``w0.25``/``w0.50``/``w0.75`` 与 ``anchor_model``/``anchor_reversal``。
    # **五个口径共用同一个横截面 S**（见预注册 §3），故两两天然同日配对；端点也
    # 在同一次运行内算，配对差里不含跨运行训练噪声（预注册 §4）。
    merge_daily_ic: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    merge_rows_used: int = 0
    merge_rows_excluded: int = 0
    merge_skipped_days: int = 0
    # C5：逐决策日的组合级量（只有日级聚合在内存里，不驻留成分股明细）。
    # 口径说明：这里的收益是「当日前瞻收益的等权均值」，**不是**复利净值曲线，
    # 也不含持有重叠/涨跌停不可成交——只用于成本与换手对照。
    portfolio_gross: list[tuple[str, float]] = field(default_factory=list)
    portfolio_turnover: list[tuple[str, float]] = field(default_factory=list)


def _rss_mib() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return -1.0
    return -1.0


class PitDatasetStore:
    """按需查询的 PIT 数据集存储（DuckDB 临时表，替代全帧驻留）。

    240 万行 × 222 特征在 4GiB 容器内无法整帧驻留（float32 也要 ~2.1GB
    驻留 + 训练/评估副本）。改为：月度 parquet 分块逐个 ATTACH 进 DuckDB
    内存表（分批注册，峰值=单块 ~100MB），fold 训练/评估时按日期过滤查询，
    只把当前 fold 需要的行拉成 DataFrame。
    """

    def __init__(self, dataset_dir: str) -> None:
        self.root = Path(dataset_dir)
        self.chunks = sorted(self.root.glob("pit_*.parquet"))
        if not self.chunks:
            raise FileNotFoundError(f"no pit parquet chunks under {dataset_dir}")
        self._con = duckdb.connect(database=":memory:")
        # 4GiB 容器内的 DuckDB 内存上限（留出训练/评估的空间）。
        self._con.execute("SET memory_limit='1.5GB'")
        self._con.execute("SET threads=2")
        self._materialized = False
        self._feature_columns: list[str] = []

    def _ensure_materialized(self) -> None:
        """注册 parquet 视图（不物化：DuckDB 谓词下推按需扫盘，内存只驻留
        查询结果）。物化表方案在 240 万行时同样顶爆 4GiB（2026-09-06 实测）。"""

        if self._materialized:
            return
        files = [str(chunk) for chunk in self.chunks]
        file_list = ", ".join(f"'{f}'" for f in files)
        # 直接视图（不叠窗口函数）：分片生成阶段已保证 (symbol, trade_date)
        # 唯一（shard 内 drop_duplicates + 月度合并再去重），视图层去重
        # 的 ROW_NUMBER 全表排序曾把 4GiB 容器顶爆（2026-09-06 实测）。
        self._con.execute(
            f"CREATE OR REPLACE VIEW pit_dedup AS "
            f"SELECT * FROM read_parquet([{file_list}])"
        )
        self._materialized = True
        columns = [
            str(r[0]) for r in self._con.execute("DESCRIBE pit_dedup").fetchall()
        ]
        meta_columns = {
            "symbol",
            "trade_date",
            "label",
            "label_mature_trade_date",
            "fwd_return",
        }
        self._feature_columns = [c for c in columns if c not in meta_columns]
        # 注意：注册后不再立即 COUNT(*)（全扫）——行数从 pit_meta.json 读取。
        print(
            f"[store] view registered over {len(files)} chunks "
            f"(features={len(self._feature_columns)})",
            flush=True,
        )

    @property
    def feature_columns(self) -> list[str]:
        self._ensure_materialized()
        return list(self._feature_columns)

    def row_count(self) -> int:
        """行数直读 pit_meta.json——避免 COUNT(*) 触发 24 块全扫（注册期
        内存峰值的实测来源之一）。"""

        meta_path = self.root / "pit_meta.json"
        if meta_path.exists():
            try:
                return int(json.loads(meta_path.read_text(encoding="utf-8"))["rows"])
            except Exception:  # noqa: BLE001 - meta 损坏时退回查询
                pass
        self._ensure_materialized()
        return int(self._con.execute("SELECT COUNT(*) FROM pit_dedup").fetchone()[0])

    def trading_dates(self) -> list[date]:
        """逐块读取单列 DISTINCT（避免 24 块合并视图的全表 DISTINCT——
        实测该全扫是 harness OOM 爆点；单块单列峰值 < 50MB）。"""

        dates_seen: set[date] = set()
        for chunk in self.chunks:
            rows = self._con.execute(
                f"SELECT DISTINCT trade_date FROM read_parquet('{chunk}')"
            ).fetchall()
            for r in rows:
                value = r[0]
                dates_seen.add(
                    value if isinstance(value, date) else date.fromisoformat(str(value))
                )
        return sorted(dates_seen)

    def fetch_train_rows(
        self,
        *,
        start: date,
        end: date,
        require_label: bool = True,
        max_rows_per_day: int = 0,
        seed: int = 0,
    ) -> pd.DataFrame:
        """训练行：trade_date ∈ [start, end] 且 label_mature < end（maturity purge）。

        ``max_rows_per_day > 0`` 时在 SQL 内按交易日下采样（ORDER BY hash
        组内排序取前 N）——下采样必须发生在 fetch 之前：120 交易日全量
        （~50 万行 × 208 列）fetch 后的 pandas 副本 ~1.9GB 是容器 OOM 的
        实测根因（2026-09-06）。
        """

        self._ensure_materialized()
        columns = ", ".join(
            ["symbol", "trade_date", "label", "label_mature_trade_date"]
            + self._feature_columns
        )
        # trade_date 在 parquet 中为 ISO 字符串（'YYYY-MM-DD'）——字符串字典序
        # 与日期序一致，直接比较可下推到 parquet 统计信息；CAST 会退化为全表
        # 扫描（2026-09-06 实测 4GiB OOM 根因）。
        predicate = "trade_date >= ? AND trade_date <= ? AND label_mature_trade_date < ?"
        if require_label:
            predicate += " AND label IS NOT NULL"
        if max_rows_per_day > 0:
            # hash(symbol || seed) 组内伪随机且可复现（同 seed 同样本）。
            query = (
                f"SELECT {columns} FROM ("
                f"  SELECT *, ROW_NUMBER() OVER ("
                f"    PARTITION BY trade_date"
                f"    ORDER BY hash(symbol || '{int(seed)}')"
                f"  ) AS __rn"
                f"  FROM pit_dedup WHERE {predicate}"
                f") WHERE __rn <= {int(max_rows_per_day)}"
            )
        else:
            query = f"SELECT {columns} FROM pit_dedup WHERE {predicate}"
        return self._con.execute(
            query, [start.isoformat(), end.isoformat(), end.isoformat()]
        ).fetch_df()

    def fetch_eval_rows(self, *, on: date) -> pd.DataFrame:
        """评估行：单个交易日的横截面（含 fwd_return 与 label_mature）。"""

        self._ensure_materialized()
        columns = ", ".join(
            [
                "symbol",
                "trade_date",
                "label",
                "label_mature_trade_date",
                "fwd_return",
            ]
            + self._feature_columns
        )
        return self._con.execute(
            f"SELECT {columns} FROM pit_dedup WHERE trade_date = ?",
            [on.isoformat()],
        ).fetch_df()

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            pass


def plan_folds(
    *,
    trading_dates: list[date],
    dataset_first_date: date,
    dataset_last_date: date,
    train_window: int,
    test_window: int,
    step: int,
    embargo_days: int,
) -> list[dict[str, object]]:
    """按方案 §5 默认参数枚举 fold：train(train_window) → embargo → test。

    训练窗最早可从 dataset_first_date（PIT 数据可用起点）起算；测试窗
    不得越过 dataset_last_date（尾部标签未成熟的行不进评估）。
    """

    usable = [d for d in trading_dates if dataset_first_date <= d]
    if not usable:
        return []
    folds: list[dict[str, object]] = []
    fold_id = 0
    start_idx = 0
    while True:
        if start_idx + train_window >= len(usable):
            break
        train_start = usable[start_idx]
        train_end_idx = start_idx + train_window - 1
        train_end = usable[train_end_idx]
        # 方案 §5 公式：eval > mature + embargo；训练样本成熟 ≤ train_end-1
        # → test_start ≥ train_end + embargo（交易日推进）。
        test_start_idx = train_end_idx + embargo_days
        if test_start_idx >= len(usable):
            break
        test_start = usable[test_start_idx]
        test_end_idx = min(test_start_idx + test_window - 1, len(usable) - 1)
        test_dates = usable[test_start_idx : test_end_idx + 1]
        # 测试窗必须整体落在数据覆盖内（尾部标签不成熟的数据集行不消费）。
        test_dates = [d for d in test_dates if d <= dataset_last_date]
        if len(test_dates) < max(3, test_window // 2):
            break
        fold_id += 1
        folds.append(
            {
                "fold_id": fold_id,
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_dates": test_dates,
            }
        )
        start_idx += step
    return folds


def _feature_columns(data: pd.DataFrame) -> list[str]:
    skip = {
        "symbol",
        "trade_date",
        "label",
        "label_mature_trade_date",
        "fwd_return",
    }
    columns = [c for c in data.columns if c not in skip]
    return [c for c in columns if pd.api.types.is_numeric_dtype(data[c])]


def run_fold(
    *,
    fold: dict[str, object],
    store: PitDatasetStore,
    trading_dates: list[date],
    trainer: ModelTrainer,
    feature_columns: list[str],
    embargo_days: int,
    k_precision: list[int],
    variant: str = "blend",
    merge_grid: bool = False,
) -> FoldResult:
    fold_id = int(fold["fold_id"])
    train_start = cast_date(fold["train_start"])
    train_end = cast_date(fold["train_end"])
    test_dates = [cast_date(d) for d in fold["test_dates"]]  # type: ignore[arg-type]
    result = FoldResult(
        fold_id=fold_id,
        train_start=train_start.isoformat(),
        train_end=train_end.isoformat(),
        eval_dates=[d.isoformat() for d in test_dates],
        embargo_days=embargo_days,
    )

    print(f"    [fold {fold_id}] fetching train rows... rss={_rss_mib():.0f}MiB", flush=True)
    # 内存约束下采样（4GiB 容器，2026-09-06 实测）：120 交易日 × ~5000 只
    # 全量 fetch 的 pandas 副本 ~1.9GB 顶爆容器——下采样下沉到 SQL（每日
    # hash 前 800 只，seed=fold_id 可复现），fetch 后 ≈ 9.6 万行 × 208 列；
    # trainer 内部还有 aligned.copy + 三 split 副本，1,500/日实测仍超。
    train = store.fetch_train_rows(
        start=train_start,
        end=train_end,
        max_rows_per_day=500,
        seed=fold_id,
    )
    print(
        f"    [fold {fold_id}] train fetched rows={len(train):,} "
        f"rss={_rss_mib():.0f}MiB",
        flush=True,
    )
    if train.empty:
        result.status = "skipped"
        result.invalid_reason = "empty_train_after_maturity_purge"
        return result
    result.universe_stats["train_rows_sampled"] = float(len(train))

    result.training_cutoff = train_end.isoformat()
    result.label_mature_cutoff = str(
        pd.to_datetime(train["label_mature_trade_date"]).max().date()
    )
    # lookahead 检查：成熟日不得越过训练窗结束（purge 后必然满足），
    # 决策日不得越过 train_end。
    violations = int(
        (
            pd.to_datetime(train["label_mature_trade_date"])
            >= pd.Timestamp(train_end) + pd.Timedelta(days=1)
        ).sum()
    )
    result.lookahead_violations = violations

    # train_on_feature_label 内部 features.join(labels, how="inner")——两个
    # 传入帧的索引必须是行唯一对齐的。此前把 trade_date 设为索引（120 个
    # 交易日 × ~800 只 = 非唯一索引），join 退化成组内笛卡尔积：
    # 880 日期行 × 800 标签行 = 70.4 万行/日 → 7040 万行（实测 MemoryError）。
    # 修复：保持 RangeIndex（行唯一），决策日语义由 trainer 的
    # apply_time_invariants（date 索引缺失时按行拒绝）与我们的 maturity purge
    # 共同保障；对齐用同一 RangeIndex 的两帧 join 即逐行内积。
    # trainer 的 temporal split 要求索引携带交易日语义且行唯一：用
    # MultiIndex (decision_time=trade_date, row=行号)——decision_time level
    # 供 _extract_trading_dates 解析交易日，row level 保证 join 逐行对齐
    # （不产生日期组笛卡尔积）。
    row_index = pd.MultiIndex.from_arrays(
        [
            pd.to_datetime(train["trade_date"]).to_numpy(),
            np.arange(len(train)),
        ],
        names=["decision_time", "row"],
    )
    features_frame = train[feature_columns].copy()
    features_frame.index = row_index
    labels_series = pd.Series(
        train["label"].astype(float).to_numpy(), index=row_index, name="label_soup_tp_before_sl"
    )
    from stock_analyzer.backtest.variants import build_fold_scorer  # noqa: WPS433

    scorer = build_fold_scorer(
        variant=variant, trainer=trainer, feature_columns=feature_columns
    )
    try:
        print(
            f"    [fold {fold_id}] variant={variant} training aligned={len(features_frame):,} "
            f"rss={_rss_mib():.0f}MiB",
            flush=True,
        )
        scorer.fit(features=features_frame, labels=labels_series)
        print(
            f"    [fold {fold_id}] fitted rss={_rss_mib():.0f}MiB",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - fold 级失败可重跑
        result.status = "failed"
        result.invalid_reason = f"{type(exc).__name__}: {exc}"
        return result

    eval_parts: list[pd.DataFrame] = []
    previous_weights: dict[str, float] = {}
    for day in test_dates:
        day_frame = store.fetch_eval_rows(on=day)
        if day_frame.empty:
            continue
        day_frame["score"] = scorer.score(day_frame).to_numpy(dtype=float)
        eval_parts.append(day_frame)
        _accumulate_portfolio_day(
            result=result,
            day=day,
            day_frame=day_frame,
            previous_weights=previous_weights,
        )
    if not eval_parts:
        result.status = "failed"
        result.invalid_reason = "no_eval_rows"
        return result
    evaluation = pd.concat(eval_parts, ignore_index=True)
    labeled = evaluation[evaluation["fwd_return"].notna()].copy()
    # fold 级即取即算（不整帧驻留：24 fold 的评估行合计=全数据集）。
    pooled_scores = labeled["score"].to_numpy(dtype=float)
    pooled_returns = labeled["fwd_return"].to_numpy(dtype=float)
    pooled_labels_binary = (pooled_returns > 0.0).astype(float)
    result.eval_scores = pooled_scores
    result.eval_returns = pooled_returns
    result.pooled_n = int(len(labeled))
    result.universe_stats = {
        "training_universe_size": int(train["symbol"].nunique()),
        "training_symbol_date_count": int(len(train)),
        "evaluation_universe_size": int(evaluation["symbol"].nunique()),
        "evaluation_symbol_date_count": int(len(evaluation)),
        "evaluation_symbol_overlap_ratio": round(
            len(set(train["symbol"]) & set(evaluation["symbol"]))
            / max(1, len(set(evaluation["symbol"]))),
            4,
        ),
    }

    if labeled.empty:
        result.status = "completed_unlabeled"
        return result

    quantiles = compute_quantile_returns(
        pooled_scores, pooled_returns, n_quantiles=5
    )
    auc = compute_auc_brier(pooled_scores, pooled_labels_binary)
    result.daily_ic = [
        (d.isoformat(), float(v))
        for d, v in labeled.groupby("trade_date")
        .apply(
            lambda g: compute_rank_ic(
                g["score"].to_numpy(), g["fwd_return"].to_numpy()
            )["ic_spearman"],
            include_groups=False,
        )
        .items()
    ]
    result.daily_top_bottom = [
        (d.isoformat(), float(v))
        for d, v in labeled.groupby("trade_date")
        .apply(
            lambda g: compute_quantile_returns(
                g["score"].to_numpy(), g["fwd_return"].to_numpy(), n_quantiles=5
            )["top_minus_bottom"],
            include_groups=False,
        )
        .items()
    ]
    (
        result.daily_residual_ic,
        result.daily_reversal_r2,
        result.residual_ic_degenerate_days,
    ) = _daily_reversal_diagnostics(labeled)
    if merge_grid:
        (
            result.merge_daily_ic,
            result.merge_rows_used,
            result.merge_rows_excluded,
            result.merge_skipped_days,
        ) = _daily_merge_diagnostics(labeled)
    result.pooled_auc = auc["auc"]
    result.pooled_brier = auc["brier"]
    result.quantile_means = [float(q) for q in quantiles["quantile_means"]]
    result.top_minus_bottom = float(quantiles["top_minus_bottom"])
    result.status = "completed"
    # fold 间释放：trainer 内部 LightGBM/XGBoost 模型、isotonic 校准器与
    # fold 级中间帧在跨 fold 累积（RSS 实测 1.2GB → 3.0GB 后 OOM）。
    # 释放 fold 级中间体：trainer 内部适配器/校准器与 scorer 持有的 predictor
    # 都不归 Python gc 管收益，跨 fold 累积过 1.2GB→3.0GB（2026-09-06 实测）。
    del features_frame, labels_series, labeled, evaluation, train, scorer
    gc.collect()
    return result


def _accumulate_portfolio_day(
    *,
    result: FoldResult,
    day: date,
    day_frame: pd.DataFrame,
    previous_weights: dict[str, float],
) -> None:
    """累加当日的组合级量（C5 成本/换手口径，与反转基线共用同一实现）。

    只累计**日级**的两个数（毛收益均值、换手），成分股明细用完即弃——
    fold 评估行不驻留是本 harness 的内存纪律（Phase 2 记录过 5 类 OOM 根因）。

    口径提醒：毛收益 = 上尾等权组合的**当日前瞻收益均值**，不是复利净值；
    持有重叠、涨跌停不可成交、T+1 都不在此处建模，故它只用于成本与换手对照，
    不可当作可交易收益曲线。
    """
    from stock_analyzer.learning.reversal_baseline import (  # noqa: WPS433
        top_quantile_weights,
        turnover,
    )

    needed = {"symbol", "score", "fwd_return"}
    if not needed.issubset(day_frame.columns):
        return
    usable = day_frame[["symbol", "score", "fwd_return"]].dropna()
    if usable.empty:
        return
    symbols = [str(value) for value in usable["symbol"].tolist()]
    scores = dict(zip(symbols, usable["score"].astype(float).tolist(), strict=True))
    returns = dict(zip(symbols, usable["fwd_return"].astype(float).tolist(), strict=True))
    weights = top_quantile_weights(scores)
    if not weights:
        return
    gross = float(sum(weight * returns.get(symbol, 0.0) for symbol, weight in weights.items()))
    result.portfolio_gross.append((day.isoformat(), gross))
    result.portfolio_turnover.append((day.isoformat(), float(turnover(previous_weights, weights))))
    previous_weights.clear()
    previous_weights.update(weights)


# 残差方差占比低于此值时，残差的相关在数值上没有意义（分母≈0 → 会产出
# ±1 之间的随机值）。这类日子必须跳过并计数，绝不能编造一个残差 IC。
_MIN_RETAINED_RANK_VARIANCE = 1e-6


def _daily_reversal_diagnostics(
    labeled: pd.DataFrame,
) -> tuple[list[tuple[str, float]], list[tuple[str, float]], int]:
    """逐日的「分数 vs 反转因子」横截面诊断。

    返回 ``(残差IC序列, 反转R²序列, 退化日数)``。每日在横截面内：

    1. ``rank(score)`` 与 ``rank(ret_20d)`` 求含截距 OLS，得到 ``R²``；
    2. ``R²`` 即「反转因子解释了分数排序方差的多少」——稳健读法，不受共线影响；
    3. 残差 = ``rank(score) - 拟合值``；仅当残差保留了非可忽略方差时（占比
       ``1-R² > 1e-6``）才计算残差与 ``rank(fwd_return)`` 的相关，否则当日计入
       退化日数并跳过（零方差残差的相关是纯数值噪声）。
    4. 横截面样本 < 5、或 score/因子秩退化的日子同样跳过。

    自检：``reversal`` 变体的分数就是 ``-ret_20d``，其 ``R²`` 应≈1 且全是退化日。
    """
    from stock_analyzer.backtest.variants import REVERSAL_PAST_RETURN_COLUMN  # noqa: WPS433

    column = REVERSAL_PAST_RETURN_COLUMN
    if column not in labeled.columns:
        return [], [], 0
    residual_out: list[tuple[str, float]] = []
    r2_out: list[tuple[str, float]] = []
    degenerate = 0
    for day, group in labeled.groupby("trade_date"):
        score = pd.to_numeric(group["score"], errors="coerce").to_numpy(dtype=float)
        base = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=float)
        forward = pd.to_numeric(group["fwd_return"], errors="coerce").to_numpy(dtype=float)
        mask = np.isfinite(score) & np.isfinite(base) & np.isfinite(forward)
        if int(mask.sum()) < 5:
            continue
        score_r = _rankdata(score[mask])
        base_r = _rankdata(base[mask])
        forward_r = _rankdata(forward[mask])
        score_var = float(np.nanvar(score_r))
        base_var = float(np.nanvar(base_r))
        if score_var == 0.0 or base_var == 0.0:
            continue
        design = np.column_stack([np.ones_like(base_r), base_r])
        try:
            coef, *_ = np.linalg.lstsq(design, score_r, rcond=None)
        except np.linalg.LinAlgError:
            continue
        residual = score_r - design @ coef
        residual_var = float(np.nanvar(residual))
        r2 = max(0.0, min(1.0, 1.0 - residual_var / score_var))
        r2_out.append((str(day), r2))
        if residual_var <= _MIN_RETAINED_RANK_VARIANCE * score_var:
            degenerate += 1
            continue
        with np.errstate(invalid="ignore"):
            value = float(np.corrcoef(residual, forward_r)[0, 1])
        if math.isfinite(value):
            residual_out.append((str(day), value))
    return residual_out, r2_out, degenerate


def _rank_pct(values: np.ndarray) -> np.ndarray:
    """横截面秩标准化：average 秩 → ``(r - 0.5) / n``，落在 (0,1)。

    均匀秩分数**单调不变**（对任何严格单调变换结果相同），所以合并是纯秩空间的操作、
    不受分数刻度影响；端点 ``w=1`` 的 IC 因此必须与原始分数的 IC 完全一致（测试钉死）。
    用均匀秩而不是标准正态分数：后者要多选一个变换，属未注册的自由度。
    """
    ranks = _rankdata(values)
    return (ranks - 0.5) / float(len(ranks))


def _merge_labels() -> list[str]:
    from stock_analyzer.backtest.variants import (  # noqa: WPS433
        MERGE_WEIGHTS,
        merge_anchor_labels,
        merge_weight_label,
    )

    anchors = merge_anchor_labels()
    return [merge_weight_label(w) for w in MERGE_WEIGHTS] + [anchors["model"], anchors["reversal"]]


def _daily_merge_diagnostics(
    labeled: pd.DataFrame,
) -> tuple[dict[str, list[tuple[str, float]]], int, int, int]:
    """C6：逐日的「模型分数 × 反转因子」秩空间合并诊断。

    返回 ``(各口径逐日IC, 使用行数, 被额外剔除的行数, 跳过的日数)``。

    五个口径（三个权重 + 两个端点）**共用同一个当段横截面 S** = score/ret_20d/fwd_return
    三者都有限的行，``|S| < MERGE_MIN_CROSS_SECTION`` 的日子跳过。端点也在这段代码里
    算（而不是复用别处的运行结果），配对差里就不含跨运行训练噪声——见预注册 §4。

    ``rows_excluded`` 是 S 相对主 IC 口径（score/fwd_return 有限）额外剔除的行数：
    它不为 0 时合并口径的横截面比主口径小，结论必须带着这个差异读。
    """
    from stock_analyzer.backtest.variants import (  # noqa: WPS433
        MERGE_MIN_CROSS_SECTION,
        MERGE_WEIGHTS,
        REVERSAL_PAST_RETURN_COLUMN,
        merge_anchor_labels,
        merge_weight_label,
    )

    labels = _merge_labels()
    out: dict[str, list[tuple[str, float]]] = {label: [] for label in labels}
    column = REVERSAL_PAST_RETURN_COLUMN
    if column not in labeled.columns:
        return out, 0, 0, 0
    anchors = merge_anchor_labels()
    used = excluded = skipped = 0
    for day, group in labeled.groupby("trade_date"):
        score = pd.to_numeric(group["score"], errors="coerce").to_numpy(dtype=float)
        base = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=float)
        forward = pd.to_numeric(group["fwd_return"], errors="coerce").to_numpy(dtype=float)
        main_mask = np.isfinite(score) & np.isfinite(forward)
        mask = main_mask & np.isfinite(base)
        excluded += int(main_mask.sum()) - int(mask.sum())
        if int(mask.sum()) < MERGE_MIN_CROSS_SECTION:
            skipped += 1
            continue
        kept_score = score[mask]
        z_model = _rank_pct(kept_score)
        z_rev = _rank_pct(-base[mask])
        kept_forward = forward[mask]
        used += int(mask.sum())
        values: dict[str, np.ndarray] = {
            anchors["model"]: kept_score,
            anchors["reversal"]: -base[mask],
        }
        for weight in MERGE_WEIGHTS:
            merged = float(weight) * z_model + (1.0 - float(weight)) * z_rev
            values[merge_weight_label(weight)] = merged
        for label, series in values.items():
            ic = compute_rank_ic(series, kept_forward)["ic_spearman"]
            if math.isfinite(ic):
                out[label].append((str(day), float(ic)))
    return out, used, excluded, skipped


def _paired_delta_ci(
    current: list[tuple[str, float]],
    baseline: list[tuple[str, float]],
    *,
    block_days: int | None = None,
) -> dict[str, object]:
    """同交易日配对的差值序列 + moving-block bootstrap CI（预注册 §6）。

    先按交易日取差（任一侧缺失就丢弃该日并计数），再对**差值序列**做同一个块 bootstrap
    ——不得对两条序列各自求 CI 后看区间是否重叠，那不是配对检验。
    """
    from stock_analyzer.learning.scoring_eval import DEFAULT_BLOCK_TRADING_DAYS  # noqa: WPS433

    base_map = {str(day): float(value) for day, value in baseline}
    diffs: list[tuple[str, float]] = []
    for day, value in current:
        key = str(day)
        if key not in base_map:
            continue
        numeric = float(value) - base_map[key]
        if math.isfinite(numeric):
            diffs.append((key, numeric))
    resolved_block = (
        int(DEFAULT_BLOCK_TRADING_DAYS) if block_days is None else max(1, int(block_days))
    )
    ci = date_block_bootstrap_ci(diffs, block_days=resolved_block)
    mean = float(np.mean([v for _, v in diffs])) if diffs else float("nan")
    return {
        "mean": mean,
        "ci95": [float(ci["ci_low"]), float(ci["ci_high"])],
        "valid_days": int(ci["valid_days"]),
        "block_days": ci.get("block_days"),
        "n_blocks": ci.get("n_blocks"),
        "duplicate_days": ci.get("duplicate_days"),
        "unpaired_days": len(current) - len(diffs),
    }


def _merge_report(folds: list[FoldResult]) -> dict[str, object]:
    """C6 合并实验的汇总与判定（判据冻死在预注册 §5）。"""
    from stock_analyzer.backtest.variants import (  # noqa: WPS433
        MERGE_NOISE_FLOOR,
        MERGE_PRIMARY_WEIGHT,
        MERGE_WEIGHTS,
        merge_anchor_labels,
        merge_experiment_definition,
        merge_weight_label,
    )

    completed = [f for f in folds if f.status in {"completed", "completed_unlabeled"}]
    primary_label = merge_weight_label(MERGE_PRIMARY_WEIGHT)
    grid_labels = [merge_weight_label(w) for w in MERGE_WEIGHTS]
    anchors = merge_anchor_labels()
    series: dict[str, list[tuple[str, float]]] = {}
    for label in [*grid_labels, anchors["model"], anchors["reversal"]]:
        series[label] = [item for f in completed for item in f.merge_daily_ic.get(label, [])]
    rows_used = int(sum(f.merge_rows_used for f in completed))
    rows_excluded = int(sum(f.merge_rows_excluded for f in completed))
    skipped_days = int(sum(f.merge_skipped_days for f in completed))

    def _mean_ci(label: str) -> tuple[float, list[float]]:
        values = series.get(label, [])
        if not values:
            return float("nan"), [float("nan"), float("nan")]
        ci = date_block_bootstrap_ci(values)
        return float(np.mean([v for _, v in values])), [float(ci["ci_low"]), float(ci["ci_high"])]

    levels: dict[str, dict[str, object]] = {}
    for label in series:
        mean, ci = _mean_ci(label)
        # 键名与 paired_deltas 统一用 ci95：不统一会让 _ci_low 读不到而静默给 NaN，
        # 进而把一个本该 GO 的结果判成 INCONCLUSIVE（本函数第一版正是这么错的）。
        levels[label] = {"ic_mean": mean, "ci95": ci, "days": len(series[label])}
    deltas: dict[str, dict[str, object]] = {}
    for label in grid_labels:
        deltas[f"{label}-{anchors['model']}"] = _paired_delta_ci(
            series[label], series[anchors["model"]]
        )
        deltas[f"{label}-{anchors['reversal']}"] = _paired_delta_ci(
            series[label], series[anchors["reversal"]]
        )
    # 端点在**同一次运行内**的水平差（与 C3 的两次独立运行对照，差异应落在噪声地板内）
    anchor_delta = _paired_delta_ci(series[anchors["model"]], series[anchors["reversal"]])

    primary_vs_model = deltas.get(f"{primary_label}-{anchors['model']}", {})
    primary_vs_rev = deltas.get(f"{primary_label}-{anchors['reversal']}", {})
    primary_level = levels.get(primary_label, {})
    d_model = _as_float(primary_vs_model.get("mean"))
    d_model_low = _ci_low(primary_vs_model)
    d_rev_low = _ci_low(primary_vs_rev)
    primary_low = _ci_low(primary_level)
    d_model_high = _ci_high(primary_vs_model)
    grid_signs = {
        label: _sign(_as_float(deltas.get(f"{label}-{anchors['model']}", {}).get("mean")))
        for label in grid_labels
    }
    signs_consistent = len({s for s in grid_signs.values() if s != 0}) <= 1
    # 结构性完整门：已完成的 fold 里有任何一个没带合并数据，说明该 fold 是从旧/别口径
    # 的 checkpoint 恢复的（或漏算），此时日数会静默变少——必须 fail-closed，不能让
    # "少了一半日子的 CI" 冒充证据。
    folds_missing_merge = sum(1 for f in completed if not f.merge_daily_ic)
    invalid_reason = ""
    if not series.get(primary_label):
        invalid_reason = "merge_grid_not_computed"
    elif folds_missing_merge:
        invalid_reason = f"merge_partial:folds_without_merge_data={folds_missing_merge}"
    elif rows_excluded > 0:
        invalid_reason = (
            f"cross_section_shrunk:rows_excluded={rows_excluded}"
            "（合并横截面比主口径小，结论须带此差异读）"
        )
    if invalid_reason:
        verdict = "INVALID"
    elif _is_finite(d_model_high) and d_model_high < 0.0:
        verdict = "MERGE_NO_GO"
    elif (
        _is_finite(d_model)
        and d_model >= MERGE_NOISE_FLOOR
        and _is_finite(d_model_low)
        and d_model_low > 0.0
        and _is_finite(d_rev_low)
        and d_rev_low > 0.0
        and _is_finite(primary_low)
        and primary_low > 0.0
    ):
        verdict = "MERGE_GO_CANDIDATE" if signs_consistent else "MERGE_INCONCLUSIVE"
    else:
        verdict = "MERGE_INCONCLUSIVE"
    return {
        "definition": merge_experiment_definition(),
        "levels": levels,
        "paired_deltas": deltas,
        "anchor_delta_model_minus_reversal": anchor_delta,
        "primary_label": primary_label,
        "rows_used": rows_used,
        "rows_excluded": rows_excluded,
        "skipped_days": skipped_days,
        "grid_delta_signs": grid_signs,
        "grid_delta_signs_consistent": signs_consistent,
        "folds_without_merge_data": folds_missing_merge,
        "invalid_reason": invalid_reason,
        "verdict": verdict,
    }


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def _is_finite(value: float) -> bool:
    return math.isfinite(value)


def _sign(value: float) -> int:
    if not math.isfinite(value) or value == 0.0:
        return 0
    return 1 if value > 0.0 else -1


def _ci_low(payload: object) -> float:
    if not isinstance(payload, dict):
        return float("nan")
    bounds = payload.get("ci95")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        return _as_float(bounds[0])
    return float("nan")


def _ci_high(payload: object) -> float:
    if not isinstance(payload, dict):
        return float("nan")
    bounds = payload.get("ci95")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        return _as_float(bounds[1])
    return float("nan")


def _environment_fingerprint(training: object = None, labels: object = None) -> dict[str, object]:
    """训练口径指纹——不记录它，跨运行的口径漂移会完全静默。

    2026-09-15 实证（两次都踩到）：

    1. LightGBM 的 params 里**没有** ``num_threads``（见 ``models/adapters.py``），
       线程数完全由 ``OMP_NUM_THREADS`` 决定；
    2. 报告里的 ``dataset.label_basis`` 是**数据集**属性（来自 pit_meta），不是训练
       用的标签——训练标签来自 ``cfg.labels.basis``，会被 ``SA__LABELS__BASIS`` 覆盖。
       一次没带该环境变量的抛壳运行训的是 soup 标签，aggregate IC 因此是 0.0802/0.0825,
       而生产容器里（``SA__LABELS__BASIS=return_rank``）的历史三次是 0.0612/0.0624/0.0634。
       同一份数据、同一 fold、同一 IC 实现（reversal 端点 IC 逐位相同 0.05779939947337951）
       却差了 0.019——**属口径漂移而非噪声**。

    所以训练/标签/线程三项都必须随报告落盘，否则"同一个 harness 跑出来的数"可能根本
    不是同一个东西。
    """
    import os  # noqa: WPS433 - 仅在生成指纹时需要

    fingerprint: dict[str, object] = {
        key: os.environ.get(key, "")
        for key in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "SA__LABELS__BASIS",
            "SA__TRAINING__TEST_RATIO",
        )
    }
    fingerprint["cpu_count"] = os.cpu_count()
    for name in ("lightgbm", "xgboost", "numpy", "pandas"):
        try:
            module = __import__(name)
            fingerprint[name] = str(getattr(module, "__version__", ""))
        except Exception:  # noqa: BLE001 - 指纹缺失不应让运行失败
            fingerprint[name] = "unavailable"
    if training is not None:
        fingerprint["training_params"] = {
            key: getattr(training, key, None)
            for key in (
                "test_ratio",
                "validation_ratio",
                "calibration_ratio",
                "min_test_trade_dates",
                "min_test_split_window_days",
                "min_samples",
            )
        }
    if labels is not None:
        # 真正决定模型学什么的字段；与上面 dataset.label_basis 不是一回事。
        fingerprint["labels"] = {
            key: getattr(labels, key, None)
            for key in ("basis", "horizon_days", "positive_return_threshold")
        }
    return fingerprint


def cast_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])

def checkpoint_dir(out_dir: str | Path, variant: str) -> Path:
    """变体独立的 fold checkpoint 目录。

    同一目录被不同变体复用会让上一个变体的 fold 结果被当成当前变体的结果——
    配对比较里最致命的静默错误，所以目录名必须带变体。
    """
    return Path(out_dir) / f"checkpoints_{str(variant).strip().lower()}"


def _cost_report(folds: list[FoldResult], *, cost_bps: float | None) -> dict[str, object]:
    """把一个变体的逐日组合量汇总成成本后概览（C5 验收项）。

    成本口径与 ``learning.reversal_baseline`` 完全相同（单边 10 bps、换手 0.5·Σ|Δw|），
    使五变体与反转基线可比。失效月份如实列出、不做剔除。
    """
    from stock_analyzer.learning.reversal_baseline import (  # noqa: WPS433
        DEFAULT_COST_BPS,
        net_returns,
        summarize_baseline,
    )

    bps = float(DEFAULT_COST_BPS if cost_bps is None else cost_bps)
    gross = [item for f in folds for item in f.portfolio_gross]
    turns = [item for f in folds for item in f.portfolio_turnover]
    if not gross:
        summary = summarize_baseline([], cost_bps=bps)
        summary["gross_mean"] = float("nan")
        return summary
    turn_by_day = dict(turns)
    days = [day for day, _ in gross]
    gross_values = [value for _, value in gross]
    turnover_values = [float(turn_by_day.get(day, 0.0)) for day in days]
    net = net_returns(gross_values, turnover_values, cost_bps=bps)
    avg_turn = float(np.mean(turnover_values)) if turnover_values else float("nan")
    summary = summarize_baseline(
        list(zip(days, net, strict=True)), cost_bps=bps, avg_turnover=avg_turn
    )
    summary["gross_mean"] = float(np.mean(gross_values))
    summary["net_mean"] = float(np.mean(net))
    return summary


def aggregate_report(
    *,
    folds: list[FoldResult],
    dataset_meta_rows: int,
    train_window: int,
    test_window: int,
    step: int,
    embargo_days: int,
    variant: str = "blend",
    cost_bps: float | None = None,
    merge_grid: bool = False,
) -> dict[str, object]:
    completed = [f for f in folds if f.status in {"completed", "completed_unlabeled"}]
    daily_ic_all = [item for f in completed for item in f.daily_ic]
    daily_tb_all = [item for f in completed for item in f.daily_top_bottom]
    ci = date_block_bootstrap_ci(daily_ic_all)
    # C3 追加：残差 IC（对 ret_20d 横截面正交化后）——与水平 IC 同口径聚合，
    # 用于回答「模型在反转因子之外还剩多少信息」。
    residual_all = [item for f in completed for item in f.daily_residual_ic]
    r2_all = [item for f in completed for item in f.daily_reversal_r2]
    residual_ci = date_block_bootstrap_ci(residual_all)
    residual_mean = (
        float(np.mean([v for _, v in residual_all])) if residual_all else float("nan")
    )
    r2_mean = float(np.mean([v for _, v in r2_all])) if r2_all else float("nan")
    degenerate_days = int(sum(f.residual_ic_degenerate_days for f in completed))
    ic_mean = (
        float(np.mean([v for _, v in daily_ic_all])) if daily_ic_all else float("nan")
    )
    tb_mean = (
        float(np.mean([v for _, v in daily_tb_all])) if daily_tb_all else float("nan")
    )
    # 池化指标：fold 级即取即算（eval 行不跨 fold 驻留）——逐 fold 增量累积
    # score/return/label 数组后一次性计算。
    pooled_scores: list[np.ndarray] = []
    pooled_returns: list[np.ndarray] = []
    pooled_labels: list[np.ndarray] = []
    for f in completed:
        if f.eval_scores is None:
            continue
        pooled_scores.append(f.eval_scores)
        pooled_returns.append(f.eval_returns)
        pooled_labels.append((f.eval_returns > 0.0).astype(float))
    quantile_means: list[float] = []
    pooled_auc = float("nan")
    pooled_brier = float("nan")
    pooled_n = 0
    if pooled_scores:
        scores = np.concatenate(pooled_scores)
        returns = np.concatenate(pooled_returns)
        labels_binary = np.concatenate(pooled_labels)
        pooled_n = int(len(scores))
        quantiles = compute_quantile_returns(scores, returns, n_quantiles=5)
        quantile_means = [float(q) for q in quantiles["quantile_means"]]
        auc = compute_auc_brier(scores, labels_binary)
        pooled_auc = auc["auc"]
        pooled_brier = auc["brier"]
    # 月度单调性：逐月 top-bottom 均值方向。
    monthly: dict[str, list[float]] = {}
    for d, v in daily_tb_all:
        monthly.setdefault(d[:7], []).append(v)
    monthly_means = {m: float(np.mean(vals)) for m, vals in sorted(monthly.items())}
    months_positive = sum(1 for v in monthly_means.values() if v >= 0)
    total_months = len(monthly_means)

    monotonic_ok = (
        months_positive >= max(1, math.ceil(4 / 6 * total_months)) if total_months else False
    )
    ic_positive = not math.isnan(ic_mean) and ic_mean > 0
    tb_ok = not math.isnan(tb_mean) and tb_mean >= 0
    folds_ok = len(completed) >= 4
    lookahead_total = sum(f.lookahead_violations for f in folds)
    ci_low = float(ci["ci_low"])
    ci_high = float(ci["ci_high"])
    # C1：过程级判据必须真正进入 verdict。此前 ci_supports 只被"上报"不参与
    # 判定（文档写了"CI 是否跨 0"却未实现），lookahead 违规同样只上报——
    # 结果是「IC=+0.01 且 CI=[-0.032,+0.052] 且 lookahead=1」也能判 GO_CANDIDATE。
    # 现在：时间安全违规 → 直接 NO_GO（指标不可用）；CI 下界 > 0 是 GO 的
    # **必要条件**（不是"CI 不反对"）；CI 整体为负 → 有负向证据 → NO_GO。
    lookahead_ok = lookahead_total == 0
    ci_supports_positive = (not math.isnan(ci_low)) and ci_low > 0.0
    ci_excludes_zero = ci_supports_positive or ((not math.isnan(ci_high)) and ci_high < 0.0)
    ci_supports_negative = (not math.isnan(ci_high)) and ci_high < 0.0
    ci_supports = not ci_supports_negative  # 兼容旧字段语义：CI 不反证"负向"
    # 覆盖门（**结构性**判据，非人为阈值）：块数 < 2 时每轮重采样抽到同一个块，
    # CI 退化成零宽（假显著）——此时不论 IC 多正都不得判 GO。
    coverage_blocks = int(ci.get("n_blocks") or 0)
    coverage_ok = coverage_blocks >= 2
    if not folds_ok:
        verdict = "INSUFFICIENT_FOLDS"
    elif not lookahead_ok:
        verdict = "NO_GO"
    elif ci_supports_negative or (not ic_positive and ci_excludes_zero):
        verdict = "NO_GO"
    elif not coverage_ok or not ci_supports_positive:
        verdict = "INCONCLUSIVE"
    elif ic_positive and tb_ok and monotonic_ok:
        verdict = "GO_CANDIDATE"
    else:
        verdict = "NO_GO"

    return {
        "aggregate_ic_mean": ic_mean,
        "aggregate_ic_ci95": [ci_low, ci_high],
        "ci_valid_days": ci["valid_days"],
        "ci_block_days": ci.get("block_days"),
        "ci_method": ci.get("method"),
        "ci_duplicate_days": ci.get("duplicate_days"),
        "aggregate_top_minus_bottom": tb_mean,
        "pooled_auc": pooled_auc,
        "pooled_brier": pooled_brier,
        "pooled_n": pooled_n,
        "quantile_means": quantile_means,
        "monthly_top_bottom_mean": monthly_means,
        "months_top_bottom_positive": months_positive,
        "months_total": total_months,
        "folds_completed": len(completed),
        "folds_total": len(folds),
        "fold_gate": {"folds_ok": folds_ok, "min_required": 4},
        "coverage_gate": {
            "blocks": coverage_blocks,
            "valid_days": ci["valid_days"],
            "ok": coverage_ok,
            "rule": "moving-block 块数 >= 2（块数=1 时 CI 退化为零宽，不构成证据）",
        },
        "validation_scope": "process",  # 流程级；工件级前向验证见批次 D
        "verdict_rule": (
            "INSUFFICIENT_FOLDS(folds<4) → NO_GO(lookahead>0) → "
            "NO_GO(ci_high<0 或 ic<=0 且 CI 不跨 0) → "
            "INCONCLUSIVE(块数<2 或 ci_low<=0) → "
            "GO_CANDIDATE(ic>0 且 top>=bottom 且 月度>=4/6)"
        ),
        "verdict_inputs": {
            "ic_positive": ic_positive,
            "top_ge_bottom": tb_ok,
            "monthly_monotonic_4_of_6": monotonic_ok,
            "ci_supports_positive": ci_supports_positive,
            "ci_excludes_zero": ci_excludes_zero,
            "ci_supports_negative": ci_supports_negative,
            "ci_does_not_support_negative": ci_supports,
            "lookahead_gate_pass": lookahead_ok,
            "lookahead_violations_total": lookahead_total,
            "coverage_gate_pass": coverage_ok,
        },
        "verdict": verdict,
        "variant": variant,
        "cost_report": _cost_report(completed, cost_bps=cost_bps),
        "aggregate_residual_ic_mean": residual_mean,
        "aggregate_residual_ic_ci95": [residual_ci["ci_low"], residual_ci["ci_high"]],
        "residual_ic_days": len(residual_all),
        "residual_ic_degenerate_days": degenerate_days,
        "reversal_r2_mean": r2_mean,
        "reversal_r2_days": len(r2_all),
        "residual_ic_method": (
            "每日横截面：rank(score) 对 rank(ret_20d) 做含截距 OLS 取残差，"
            "再与 rank(fwd_return) 求相关；只扣横截面线性部分"
        ),
        # C6 合并实验：未开 --merge-grid 时该节为空并写明原因，不得当成"未通过"。
        "merge_experiment": (
            _merge_report(completed)
            if merge_grid
            else {"verdict": "NOT_RUN", "reason": "未开 --merge-grid（本次运行不算合并实验）"}
        ),
        "params": {
            "train_window": train_window,
            "test_window": test_window,
            "step": step,
            "embargo_days": embargo_days,
            "dataset_rows": dataset_meta_rows,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 cross-sectional walk-forward harness")
    parser.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--train-window", type=int, default=120)
    parser.add_argument("--test-window", type=int, default=20)
    parser.add_argument("--step", type=int, default=20)
    parser.add_argument("--k-list", default="5,10")
    # C3 配对比较的变体选择（预注册定义见 docs/learning_chain_c3_preregistration_20260915.md）
    parser.add_argument(
        "--variant",
        default="blend",
        choices=list(VARIANTS),
        help="评估变体：blend/raw_blend/ridge/stump/reversal（同一窗口/资格集/脚本下配对）",
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=None,
        help="单边成本（bps）；默认沿用 reversal_baseline 的 10bps，不另设口径",
    )
    # C6 合并实验（预注册见 docs/learning_chain_c6_merge_experiment_preregistration_20260915.md）：
    # 单次运行内同时给出三个权重与两个端点，配对差里不含跨运行训练噪声。
    parser.add_argument(
        "--merge-grid",
        action="store_true",
        help="开启 C6 合并实验的每日秩标准化网格（默认关；关了就不算做过合并实验）",
    )
    # 对照基线（方向一'验收要求）：旧 soup label 的 Phase 2 数字，写进
    # 报告作对照行。默认取 2026-09-06 NO-GO 结论；传 "none" 可省略。
    parser.add_argument(
        "--baseline-soup-ic",
        type=str,
        default="-0.024",
        help="旧 soup label 的 aggregate IC 对照值（含 CI 用 'ic:lo:hi' 三段）",
    )
    args = parser.parse_args()
    k_list = [int(k) for k in args.k_list.split(",") if k.strip()]
    variant = str(args.variant).strip().lower()

    from stock_analyzer.config import get_config

    cfg = get_config()
    embargo_days = int(cfg.labels.horizon_days) + int(cfg.evolution.execution_spec.settlement_lag)

    t0 = time.time()
    store = PitDatasetStore(args.dataset_dir)
    meta_path = Path(args.dataset_dir) / "pit_meta.json"
    dataset_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    label_basis = str(dataset_meta.get("label_policy_note", "")).split(" ", 1)[0]
    trading_dates = store.trading_dates()
    feature_columns = store.feature_columns
    print(
        f"[1] dataset rows={store.row_count():,} "
        f"dates={len(trading_dates)} features={len(feature_columns)} "
        f"label_basis={label_basis}",
        flush=True,
    )

    folds_plan = plan_folds(
        trading_dates=trading_dates,
        dataset_first_date=trading_dates[0],
        dataset_last_date=trading_dates[-1],
        train_window=args.train_window,
        test_window=args.test_window,
        step=args.step,
        embargo_days=embargo_days,
    )
    print(
        f"[2] folds planned: {len(folds_plan)} "
        f"(train={args.train_window}/test={args.test_window}/step={args.step}/embargo={embargo_days})",
        flush=True,
    )

    def _build_trainer() -> ModelTrainer:
        # 每 fold 新建 trainer：LightGBM/XGBoost 的 C 层分配器缓存不归 Python
        # gc 管，跨 fold 复用实例会累积 ~1.6GB（2026-09-06 实测）。
        return ModelTrainer(
            training=cfg.training,
            labels=cfg.labels,
            models=cfg.models,
            settlement_lag_days=int(cfg.evolution.execution_spec.settlement_lag),
            provider=None,
            market_relative_feature=cfg.market_relative_feature,
        )


    # fold checkpoint（方案 §5）：每 fold 完成即落盘，重跑跳过已完成
    # fold（含 OOM/中断后续跑）。目录 {out_dir}/checkpoints/。
    # checkpoint 按变体隔离（原因见 checkpoint_dir 的 docstring）。
    ckpt_dir = checkpoint_dir(args.out_dir, variant)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    def _fold_to_payload(f: FoldResult) -> dict[str, object]:
        return {
            "fold_id": f.fold_id,
            "train_window": [f.train_start, f.train_end],
            "eval_dates": f.eval_dates,
            "status": f.status,
            "invalid_reason": f.invalid_reason,
            "training_cutoff": f.training_cutoff,
            "label_mature_cutoff": f.label_mature_cutoff,
            "embargo_days": f.embargo_days,
            "lookahead_violations": f.lookahead_violations,
            "daily_ic": [[d, v] for d, v in f.daily_ic],
            "daily_top_bottom": [[d, v] for d, v in f.daily_top_bottom],
            "daily_residual_ic": [[d, v] for d, v in f.daily_residual_ic],
            "daily_reversal_r2": [[d, v] for d, v in f.daily_reversal_r2],
            "residual_ic_degenerate_days": f.residual_ic_degenerate_days,
            "merge_daily_ic": {
                label: [[d, v] for d, v in values] for label, values in f.merge_daily_ic.items()
            },
            "merge_rows_used": f.merge_rows_used,
            "merge_rows_excluded": f.merge_rows_excluded,
            "merge_skipped_days": f.merge_skipped_days,
            "pooled_auc": f.pooled_auc,
            "pooled_brier": f.pooled_brier,
            "pooled_n": f.pooled_n,
            "quantile_means": f.quantile_means,
            "top_minus_bottom": f.top_minus_bottom,
            "universe_stats": f.universe_stats,
            "portfolio_gross": [[d, v] for d, v in f.portfolio_gross],
            "portfolio_turnover": [[d, v] for d, v in f.portfolio_turnover],
        }

    loaded: dict[int, FoldResult] = {}
    for ckpt_file in sorted(ckpt_dir.glob("fold_*.json")):
        try:
            raw = json.loads(ckpt_file.read_text(encoding="utf-8"))
            fid = int(raw["fold_id"])
        except Exception:  # noqa: BLE001 - 损坏 checkpoint 忽略重跑
            continue
        loaded[fid] = FoldResult(
            fold_id=fid,
            train_start=str(raw["train_window"][0]),
            train_end=str(raw["train_window"][1]),
            eval_dates=[str(d) for d in raw.get("eval_dates", [])],
            status=str(raw.get("status", "")),
            invalid_reason=str(raw.get("invalid_reason", "")),
            training_cutoff=str(raw.get("training_cutoff", "")),
            label_mature_cutoff=str(raw.get("label_mature_cutoff", "")),
            embargo_days=int(raw.get("embargo_days", 0)),
            lookahead_violations=int(raw.get("lookahead_violations", 0)),
            daily_ic=[(str(d), float(v)) for d, v in raw.get("daily_ic", [])],
            daily_top_bottom=[(str(d), float(v)) for d, v in raw.get("daily_top_bottom", [])],
            daily_residual_ic=[(str(d), float(v)) for d, v in raw.get("daily_residual_ic", [])],
            daily_reversal_r2=[(str(d), float(v)) for d, v in raw.get("daily_reversal_r2", [])],
            residual_ic_degenerate_days=int(raw.get("residual_ic_degenerate_days", 0)),
            merge_daily_ic={
                str(label): [(str(d), float(v)) for d, v in values]
                for label, values in (raw.get("merge_daily_ic") or {}).items()
            },
            merge_rows_used=int(raw.get("merge_rows_used", 0)),
            merge_rows_excluded=int(raw.get("merge_rows_excluded", 0)),
            merge_skipped_days=int(raw.get("merge_skipped_days", 0)),
            pooled_auc=float(raw.get("pooled_auc", "nan") or "nan"),
            pooled_brier=float(raw.get("pooled_brier", "nan") or "nan"),
            pooled_n=int(raw.get("pooled_n", 0)),
            quantile_means=[float(q) for q in raw.get("quantile_means", [])],
            top_minus_bottom=float(raw.get("top_minus_bottom", "nan") or "nan"),
            universe_stats=dict(raw.get("universe_stats", {})),
        )

    folds: list[FoldResult] = []
    for plan in folds_plan:
        fid = int(plan["fold_id"])  # type: ignore[call-overload]
        if fid in loaded and loaded[fid].status in {"completed", "completed_unlabeled"}:
            folds.append(loaded[fid])
            print(f"[3] fold {fid} resumed from checkpoint", flush=True)
            continue
        started = time.time()
        result = run_fold(
            fold=plan,
            store=store,
            trading_dates=trading_dates,
            trainer=_build_trainer(),
            feature_columns=feature_columns,
            embargo_days=embargo_days,
            k_precision=k_list,
            variant=variant,
            merge_grid=bool(args.merge_grid),
        )
        folds.append(result)
        (ckpt_dir / f"fold_{fid:02d}.json").write_text(
            json.dumps(_fold_to_payload(result), ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(
            f"[3] fold {result.fold_id} {result.status} "
            f"train={result.train_start}..{result.train_end} "
            f"({time.time() - started:.0f}s) {result.invalid_reason} "
            f"rss={_rss_mib():.0f}MiB",
            flush=True,
        )
        gc.collect()

    report = aggregate_report(
        folds=folds,
        dataset_meta_rows=int(dataset_meta.get("rows", store.row_count())),
        train_window=args.train_window,
        test_window=args.test_window,
        step=args.step,
        embargo_days=embargo_days,
        variant=variant,
        cost_bps=args.cost_bps,
        merge_grid=bool(args.merge_grid),
    )
    # 验收硬门（方向一'任务书）：新 label 下模型分数 IC 的 moving-block
    # bootstrap 95% CI 下界 > 0，且 fold 内不得有 lookahead 违规。
    # 说明：verdict 现在同样把这两条当必要条件（C1 修正），本块是报告层的
    # 显式留痕（含块长/方法/基线可比性），以便审计时不必反推。
    baseline_payload = baseline_comparison(int(cast(int, report["folds_total"])))
    ci_values = list(report["aggregate_ic_ci95"])  # type: ignore[call-overload]
    ci_low = float(ci_values[0])
    ci_high = float(ci_values[1])
    report["hard_gate"] = {
        "rule": "aggregate_ic_ci95_low > 0 (date-block bootstrap 95%)",
        "ic_ci95_low": ci_low,
        "ic_ci95_high": ci_high,
        "pass": bool(ci_low > 0),
        "ci_method": report.get("ci_method"),
        "ci_block_days": report.get("ci_block_days"),
        "lookahead_violations_total": report["verdict_inputs"]["lookahead_violations_total"],  # type: ignore[index]
        "lookahead_gate_pass": report["verdict_inputs"]["lookahead_gate_pass"],  # type: ignore[index]
        "verdict": report["verdict"],
        "baseline_comparable": bool(baseline_payload["comparable"]),
        "variant": variant,
        "variant_definition": variant_definitions().get(variant, {}),
    }

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "validation_scope": VALIDATION_SCOPE_PROCESS,
        "environment": _environment_fingerprint(cfg.training, cfg.labels),
        "dataset": {
            "dir": args.dataset_dir,
            "label_basis": label_basis,
            "meta": dataset_meta,
        },
        # 对照基线行（验收硬门要求）：同一 harness 配置下旧 soup label 的
        # Phase 2 结论。fold 数按本次运行**生成**并判定可比性，写死的只是
        # 基线自身的历史值（见 BASELINE_SOUP_LABEL）。
        "baseline_soup_label": baseline_payload,
        "folds": [
            {
                "fold_id": f.fold_id,
                "train_window": [f.train_start, f.train_end],
                "eval_dates": f.eval_dates,
                "status": f.status,
                "invalid_reason": f.invalid_reason,
                "training_cutoff": f.training_cutoff,
                "label_mature_cutoff": f.label_mature_cutoff,
                "embargo_days": f.embargo_days,
                "lookahead_violations": f.lookahead_violations,
                "daily_ic": [[d, v] for d, v in f.daily_ic],
                "daily_residual_ic": [[d, v] for d, v in f.daily_residual_ic],
                "daily_reversal_r2": [[d, v] for d, v in f.daily_reversal_r2],
                "residual_ic_degenerate_days": f.residual_ic_degenerate_days,
                "merge_daily_ic": {
                    label: [[d, v] for d, v in values] for label, values in f.merge_daily_ic.items()
                },
                "merge_rows_used": f.merge_rows_used,
                "merge_rows_excluded": f.merge_rows_excluded,
                "merge_skipped_days": f.merge_skipped_days,
                "pooled_auc": f.pooled_auc,
                "pooled_brier": f.pooled_brier,
                "pooled_n": f.pooled_n,
                "quantile_means": f.quantile_means,
                "top_minus_bottom": f.top_minus_bottom,
                "universe_stats": f.universe_stats,
            }
            for f in folds
        ],
        "aggregate": report,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"phase2_walk_forward_{stamp}.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[4] json={json_path}", flush=True)
    print("ENVIRONMENT " + json.dumps(payload["environment"], ensure_ascii=False), flush=True)
    print("AGGREGATE " + json.dumps(report, ensure_ascii=False, default=str), flush=True)
    print(f"[done] {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
