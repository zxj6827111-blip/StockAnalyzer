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

判定（§5 放行判据，INCONCLUSIVE 语义内置）：≥4 完整 fold、≥4/6 月、
IC>0、top≥bottom、CI 是否跨 0。产出 JSON+MD 报告（时间戳命名不覆盖）。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from stock_analyzer.learning.scoring_eval import (
    compute_auc_brier,
    compute_quantile_returns,
    compute_rank_ic,
    date_block_bootstrap_ci,
)
from stock_analyzer.models.trainer import ModelTrainer

DEFAULT_DATASET_DIR = "/app/artifacts/phase2/pit_dataset"
DEFAULT_OUT_DIR = "/app/artifacts/phase2"


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
        max_rows_per_day=800,
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

    aligned = train[feature_columns + ["label"]].copy()
    aligned.index = pd.to_datetime(train["trade_date"])
    aligned["label"] = aligned["label"].astype(float)
    try:
        print(
            f"    [fold {fold_id}] training aligned={len(aligned):,} "
            f"rss={_rss_mib():.0f}MiB",
            flush=True,
        )
        trained = trainer.train_on_feature_label(
            features=aligned[feature_columns], labels=aligned["label"]
        )
        print(
            f"    [fold {fold_id}] trained rss={_rss_mib():.0f}MiB",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - fold 级失败可重跑
        result.status = "failed"
        result.invalid_reason = f"{type(exc).__name__}: {exc}"
        return result

    from stock_analyzer.models.predictor import SignalPredictor

    predictor = SignalPredictor.from_artifact(trained.artifact)

    eval_parts: list[pd.DataFrame] = []
    for day in test_dates:
        day_frame = store.fetch_eval_rows(on=day)
        if day_frame.empty:
            continue
        scores = predictor.predict_rows(day_frame[feature_columns])["meta"]
        day_frame["score"] = scores
        eval_parts.append(day_frame)
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
    result.pooled_auc = auc["auc"]
    result.pooled_brier = auc["brier"]
    result.quantile_means = [float(q) for q in quantiles["quantile_means"]]
    result.top_minus_bottom = float(quantiles["top_minus_bottom"])
    result.status = "completed"
    return result


def cast_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def aggregate_report(
    *,
    folds: list[FoldResult],
    dataset_meta_rows: int,
    train_window: int,
    test_window: int,
    step: int,
    embargo_days: int,
) -> dict[str, object]:
    completed = [f for f in folds if f.status in {"completed", "completed_unlabeled"}]
    daily_ic_all = [item for f in completed for item in f.daily_ic]
    daily_tb_all = [item for f in completed for item in f.daily_top_bottom]
    ci = date_block_bootstrap_ci(daily_ic_all)
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
    ci_supports = not (ci["ci_high"] < 0)  # CI 不支持"负向"即视为不反证
    verdict = (
        "GO_CANDIDATE"
        if (folds_ok and ic_positive and tb_ok and monotonic_ok)
        else "INCONCLUSIVE"
    )
    if not folds_ok:
        verdict = "INSUFFICIENT_FOLDS"

    return {
        "aggregate_ic_mean": ic_mean,
        "aggregate_ic_ci95": [ci["ci_low"], ci["ci_high"]],
        "ci_valid_days": ci["valid_days"],
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
        "verdict_inputs": {
            "ic_positive": ic_positive,
            "top_ge_bottom": tb_ok,
            "monthly_monotonic_4_of_6": monotonic_ok,
            "ci_does_not_support_negative": ci_supports,
            "lookahead_violations_total": sum(f.lookahead_violations for f in folds),
        },
        "verdict": verdict,
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
    args = parser.parse_args()
    k_list = [int(k) for k in args.k_list.split(",") if k.strip()]

    from stock_analyzer.config import get_config

    cfg = get_config()
    embargo_days = int(cfg.labels.horizon_days) + int(cfg.evolution.execution_spec.settlement_lag)

    t0 = time.time()
    store = PitDatasetStore(args.dataset_dir)
    meta_path = Path(args.dataset_dir) / "pit_meta.json"
    dataset_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    trading_dates = store.trading_dates()
    feature_columns = store.feature_columns
    print(
        f"[1] dataset rows={store.row_count():,} "
        f"dates={len(trading_dates)} features={len(feature_columns)}",
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

    trainer = ModelTrainer(
        training=cfg.training,
        labels=cfg.labels,
        models=cfg.models,
        settlement_lag_days=int(cfg.evolution.execution_spec.settlement_lag),
        provider=None,
        market_relative_feature=cfg.market_relative_feature,
    )

    folds: list[FoldResult] = []
    for plan in folds_plan:
        started = time.time()
        result = run_fold(
            fold=plan,
            store=store,
            trading_dates=trading_dates,
            trainer=trainer,
            feature_columns=feature_columns,
            embargo_days=embargo_days,
            k_precision=k_list,
        )
        folds.append(result)
        print(
            f"[3] fold {result.fold_id} {result.status} "
            f"train={result.train_start}..{result.train_end} "
            f"({time.time() - started:.0f}s) {result.invalid_reason}",
            flush=True,
        )

    report = aggregate_report(
        folds=folds,
        dataset_meta_rows=int(dataset_meta.get("rows", store.row_count())),
        train_window=args.train_window,
        test_window=args.test_window,
        step=args.step,
        embargo_days=embargo_days,
    )
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
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
    print("AGGREGATE " + json.dumps(report, ensure_ascii=False, default=str), flush=True)
    print(f"[done] {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
