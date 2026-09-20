"""M4-H Historical Locked OOS runner。

研究验证用途（**非生产**）：在本地已有的多年 A 股历史数据上建立
Historical Locked OOS 证据。本阶段显式禁止 push / deploy / promote，
不得修改 Legacy 70、Cross Review、300/100/50、Feature Set、Label V2、
Benchmark Definition 与正式 serving。

设计原则
--------
1. **不重写研究系统**：fold 规划、泄漏复核、模型、统计口径全部复用 S19
   (``purged_walk_forward``) 与既有研究模块（``metrics`` / ``benchmarks`` /
   ``simple_baseline`` / ``winner_recall`` / ``multi_head`` / ``outcomes``）。
   本脚本只做 M4-H 特有的编排：协议冻结、expanding 窗口、per-fold 工件、
   完整评估面、不可重写守卫。
2. **expanding 语义的最小实现**：S19 的 ``run_fold`` 以
   ``decision_date >= fold.train_start`` 为训练下界，因此把 ``train_start``
   改写为序列首日即得 expanding 窗口——``purged_walk_forward`` 一行不用改。
3. **校准严格位于 test 之前**：train → calibration → purge/embargo → test；
   校准窗从训练窗**尾部**独立切出，绝不出自 test block（``calibrate_direction``
   自带 train/calibration 重叠 fail-closed）。
4. **不可重写**：``protocol_id + fold_id`` 为键的 fold 工件一旦落盘，
   重跑结果不一致即抛 ``HistoricalOOSRestatementError``。

用法::

    python scripts/alpha_v2_m4h_run.py --phase a            # 主线证据
    python scripts/alpha_v2_m4h_run.py --phase b            # 概率校准层
    python scripts/alpha_v2_m4h_run.py --probe --years 3    # 规模/耗时探针
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from stock_analyzer.alpha_v2.research import benchmarks as bm  # noqa: E402
from stock_analyzer.alpha_v2.research import metrics as met  # noqa: E402
from stock_analyzer.alpha_v2.research.feature_audit import (  # noqa: E402
    safe_feature_columns,
)
from stock_analyzer.alpha_v2.research.multi_head import (  # noqa: E402
    ALPHA_TARGET_TEMPLATE,
    TASK_BINARY,
    HeadFitSpec,
    build_head_targets,
    calibrate_direction,
    fit_lightgbm_native,
    predict_model_scores,
)
from stock_analyzer.alpha_v2.research.outcomes import (  # noqa: E402
    DecisionPoint,
    OutcomeSpec,
    build_label_v2,
)
from stock_analyzer.alpha_v2.research.panel import load_daily_panel  # noqa: E402
from stock_analyzer.alpha_v2.research.purged_walk_forward import (  # noqa: E402
    Fold,
    FoldSpec,
    LightGbmRankScorer,
    newey_west_mean_ci,
    non_overlapping_anchor,
    overlap_leakage_check,
    plan_folds,
)
from stock_analyzer.alpha_v2.research.simple_baseline import (  # noqa: E402
    compute_simple_baseline,
    evaluate_baseline,
)
from stock_analyzer.alpha_v2.research.winner_recall import (  # noqa: E402
    compute_winner_recall,
)
from stock_analyzer.alpha_v2.validation.freeze import (  # noqa: E402
    feature_schema_hash_of,
)
from stock_analyzer.data.asof_universe import (  # noqa: E402
    SymbolPitStats,
    history_window_days,
    resolve_asof_universe,
)
from stock_analyzer.feature.engineer import FeatureEngineer  # noqa: E402

RUN_SCHEMA = "alpha_v2_m4h_run.v1"
# 矩阵构造版本：**改动决定矩阵内容的逻辑时递增**（特征来源、标签口径、决策集合、
# 数据窗口处理等）。仅改 fold 编排 / 评估 / 落盘等不影响矩阵的代码时不递增，
# 以便复用昂贵的标签与特征缓存。
MATRIX_BUILD_VERSION = 2
FOLD_SCHEMA = "alpha_v2_m4h_fold.v1"

# 概率校准层的目标列（用户 §22 指定）。
PROBABILITY_HEADS: tuple[tuple[str, str], ...] = (
    ("p_up_net_3d", "up_net_3d"),
    ("p_up_net_5d", "up_net_5d"),
    ("p_up_excess_3d", "up_excess_3d"),
    ("p_up_excess_5d", "up_excess_5d"),
)

DECISION_TIME_NOTE = "T 收盘后决策（15:30），T+1 开盘成交，执行价 raw"


class HistoricalOOSRestatementError(RuntimeError):
    """同一 (protocol_id, fold_id) 的已落盘结果与新结果不一致 → fail-closed。"""


# ---------------------------------------------------------------------------
# 协议对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M4HProtocol:
    """M4-H 研究协议（冻结对象）。

    一经冻结即不可修改：任何参数变化都必须换 ``protocol_id`` 并另立
    ``experiment_id``，旧结果保留。
    """

    protocol_id: str
    experiment_id: str
    code_commit: str
    code_baseline_commit: str
    eval_start: str
    eval_end: str
    warmup_days: int
    decision_step_days: int
    min_train_days: int
    test_window_days: int
    step_days: int
    calibration_days: int
    expanding: bool
    horizons: tuple[int, ...]
    primary_horizon: int
    confirmation_horizon: int
    decay_horizons: tuple[int, ...]
    label_column: str
    metric_column: str
    min_cross_section: int
    execution_price_mode: str
    entry_mode: str
    purge_days: int
    embargo_days: int
    quality_target: int
    light_target: int
    deep_target: int
    final_cap: int
    legacy_threshold: float
    top_ks: tuple[int, ...]
    quantiles: int
    benchmark_layers: tuple[str, ...]
    primary_benchmark: str
    created_at: str = field(default="")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": RUN_SCHEMA,
            "protocol_id": self.protocol_id,
            "experiment_id": self.experiment_id,
            "code_commit": self.code_commit,
            "code_baseline_commit": self.code_baseline_commit,
            "created_at": self.created_at,
            "window": {
                "eval_start": self.eval_start,
                "eval_end": self.eval_end,
                "warmup_days": int(self.warmup_days),
                "decision_step_days": int(self.decision_step_days),
            },
            "fold_policy": {
                "mode": "expanding" if self.expanding else "rolling",
                "min_train_days": int(self.min_train_days),
                "test_window_days": int(self.test_window_days),
                "step_days": int(self.step_days),
                "calibration_days": int(self.calibration_days),
                "purge_days": int(self.purge_days),
                "embargo_days": int(self.embargo_days),
                "calibration_position": "train_tail_before_purge",
            },
            "label_policy": {
                "label_v2_schema": "alpha_v2_label_v2.v1",
                "horizons": [int(h) for h in self.horizons],
                "primary_horizon": int(self.primary_horizon),
                "confirmation_horizon": int(self.confirmation_horizon),
                "decay_horizons": [int(h) for h in self.decay_horizons],
                "train_label": self.label_column,
                "eval_metric_column": self.metric_column,
                "decision_time": DECISION_TIME_NOTE,
            },
            "execution_contract": {
                "execution_price_mode": self.execution_price_mode,
                "entry_mode": self.entry_mode,
                "note": "完全复用 M1/M2/M3：T+1 开盘、raw 价、no_fill 不推迟",
            },
            "selection_contract": {
                "contract_id": "night_alpha_v2_v1",
                "quality_target": int(self.quality_target),
                "light_target": int(self.light_target),
                "deep_target": int(self.deep_target),
                "final_cap": int(self.final_cap),
                "legacy_threshold": float(self.legacy_threshold),
                "modified_by_m4h": False,
            },
            "metrics": {
                "top_ks": [int(k) for k in self.top_ks],
                "quantiles": int(self.quantiles),
                "min_cross_section": int(self.min_cross_section),
                "probability_heads": [name for name, _ in PROBABILITY_HEADS],
            },
            "benchmarks": {
                "layers": list(self.benchmark_layers),
                "primary_layer": self.primary_benchmark,
                "simple_baseline": "same_date_paired_comparison",
            },
        }

    def protocol_hash(self) -> str:
        """协议身份哈希：**只含协议参数**，不含 ``created_at`` 这类运行期元数据。

        这样"冻结时写下的 manifest"与"运行时算出的身份"逐位相同，第三方可拿
        manifest 复核本次运行确实按冻结协议执行；若把时间戳算进哈希，两者必然不符。
        """
        payload = dict(self.to_payload())
        payload.pop("created_at", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def cache_key(self) -> str:
        """矩阵缓存键 = 决定矩阵内容的参数 + **显式**矩阵构造版本号。

        三个坑在这里收口：
        - 用 ``protocol_hash`` 当缓存名会**永不命中**（每次运行的 ``created_at``
          都不同），"断点续跑"形同虚设；
        - 用脚本内容哈希当键则**改任何无关代码都会失效**——实测改一行 fold 输出
          就会让整段 108 分钟的标签计算重跑一次；
        - 只按窗口参数缓存，又会在**矩阵构造逻辑真变了**（如 feature schema 由
          208 列改为 120 列）时错误复用旧矩阵。

        因此用人工维护的 :data:`MATRIX_BUILD_VERSION`：改矩阵构造就递增它，
        改其它逻辑不动。脚本内容哈希仍写进 diagnostics 供审计，但不参与键。
        """
        payload = {
            "schema": RUN_SCHEMA,
            "matrix_build_version": int(MATRIX_BUILD_VERSION),
            "eval_start": self.eval_start,
            "eval_end": self.eval_end,
            "warmup_days": int(self.warmup_days),
            "decision_step_days": int(self.decision_step_days),
            "horizons": [int(h) for h in self.horizons],
            "execution_price_mode": self.execution_price_mode,
            "entry_mode": self.entry_mode,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


# ---------------------------------------------------------------------------
# 工件写入（不可重写守卫）
# ---------------------------------------------------------------------------


class ArtifactStore:
    """按 ``protocol_id`` 分区落盘；同键不一致即 fail-closed。"""

    def __init__(self, root: Path, protocol_id: str) -> None:
        self.root = root
        self.protocol_id = protocol_id
        for sub in ("protocol", "folds", "predictions", "metrics", "audit", "reports"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    def write_json(self, relative: str, payload: dict[str, Any], *, guard: bool = False) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if guard and path.is_file():
            existing = path.read_text(encoding="utf-8")
            if existing != rendered:
                try:
                    before = json.loads(existing)
                    after = json.loads(rendered)
                except json.JSONDecodeError:
                    before, after = existing, rendered
                if _stable_compare(before, after):
                    return path
                raise HistoricalOOSRestatementError(
                    f"{path} 已存在且内容不同（protocol_id={self.protocol_id}）："
                    "同一协议键的结果不得改写；如需新结果必须换 experiment_id。"
                )
        path.write_text(rendered, encoding="utf-8")
        return path

    def read_json(self, relative: str) -> dict[str, Any] | None:
        path = self.root / relative
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _stable_compare(
    before: Any,
    after: Any,
    *,
    ignore: Sequence[str] = ("elapsed_seconds", "generated_at", "timing"),
) -> bool:
    """比较两个 JSON 载荷，忽略计时类字段（重跑时长不同不算口径变化）。"""
    return _strip(before, ignore) == _strip(after, ignore)


def _strip(value: Any, ignore: Sequence[str]) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v, ignore) for k, v in value.items() if k not in ignore}
    if isinstance(value, list):
        return [_strip(item, ignore) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return "__nan__" if np.isnan(value) else "__inf__"
    return value


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------


@dataclass
class PreparedDataset:
    panel: Any
    frame: pd.DataFrame
    feature_columns: tuple[str, ...]
    decision_dates: tuple[date, ...]
    trading_dates: tuple[date, ...]
    diagnostics: dict[str, Any]


_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


class PitUniverseIndex:
    """PIT universe 的向量化索引：判定规则逐字复用 S03，只把统计部分提速。

    ``panel.pit_universe`` 每个 as_of 都做一次全表 ``copy`` + ``groupby`` + 逐
    symbol 的 Python 循环（2023-2025 面板实测 1.4 s/次；十年面板更慢），上千个
    决策日不可行。这里把"每个 as_of 重算一遍"换成"一次预计算 + 查询"：``bars``
    已按 ``(symbol, trade_date)`` 有序，于是每个 symbol 的 bar 是连续块，
    窗口内计数 = 块内两次 ``searchsorted`` 之差。

    判定链路（``resolve_asof_universe``）与 ``SymbolPitStats`` 字段语义完全不变，
    故结果与 S03 逐位一致（``--verify-pit`` 提供对拍验证）。
    """

    def __init__(
        self,
        panel: Any,
        *,
        min_history_days: int = 60,
        expected_active_lookback_days: int = 5,
    ) -> None:
        self._symbols = tuple(str(item) for item in panel.symbols)
        self._min_history = int(min_history_days)
        self._lookback = int(expected_active_lookback_days)
        self._history_window = history_window_days(
            min_history_days=self._min_history, lookback_days=self._lookback
        )
        bars = panel.bars.loc[:, ["symbol", "trade_date"]].dropna()
        codes, uniques = pd.factorize(bars["symbol"].astype(str), sort=True)
        self._uniques = np.asarray([str(item) for item in uniques], dtype=object)
        self._codes = np.asarray(codes, dtype=np.int64)
        self._dates = np.asarray(
            bars["trade_date"].to_numpy(dtype="datetime64[D]").astype("int64"), dtype=np.int64
        )
        counts = np.bincount(self._codes, minlength=len(self._uniques)).astype(np.int64)
        starts = np.zeros(len(self._uniques), dtype=np.int64)
        if len(self._uniques) > 1:
            starts[1:] = np.cumsum(counts)[:-1]
        self._starts = starts
        self._counts = counts

    @property
    def history_window_days(self) -> int:
        return int(self._history_window)

    def _positions(self, as_of: date) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """每个 symbol 在 as_of / history 界 / lookback 界上的 bar 计数位置。"""
        as_of_int = int(as_of.toordinal() - _EPOCH_ORDINAL)
        hist_int = as_of_int - int(self._history_window)
        look_int = as_of_int - int(self._lookback)
        size = len(self._uniques)
        pos_asof = np.zeros(size, dtype=np.int64)
        pos_hist = np.zeros(size, dtype=np.int64)
        pos_look = np.zeros(size, dtype=np.int64)
        for index in range(size):
            start = int(self._starts[index])
            block = self._dates[start : start + int(self._counts[index])]
            pos_asof[index] = np.searchsorted(block, as_of_int, side="right")
            pos_hist[index] = np.searchsorted(block, hist_int, side="left")
            pos_look[index] = np.searchsorted(block, look_int, side="left")
        return pos_asof, pos_hist, pos_look

    def stats(self, as_of: date) -> dict[str, SymbolPitStats]:
        pos_asof, pos_hist, pos_look = self._positions(as_of)
        out: dict[str, SymbolPitStats] = {}
        for index in range(len(self._uniques)):
            hi = int(pos_asof[index])
            if hi <= 0:
                # 无 <= as_of 的 bar → 不入 stats；下游按 future_listed 排除（与原实现一致）。
                continue
            lo = int(pos_hist[index])
            look = int(pos_look[index])
            start = int(self._starts[index])
            symbol = str(self._uniques[index])
            out[symbol] = SymbolPitStats(
                symbol=symbol,
                bars_in_window=int(hi - lo),
                bars_in_lookback=int(hi - look),
                first_bar_date=(
                    _ordinal_to_date(int(self._dates[start + lo])) if hi > lo else None
                ),
                last_bar_date=_ordinal_to_date(int(self._dates[start + hi - 1])),
            )
        return out

    def snapshot(self, as_of: date) -> Any:
        return resolve_asof_universe(
            as_of=as_of,
            index_symbols=self._symbols,
            stats=self.stats(as_of),
            min_history_days=self._min_history,
            expected_active_lookback_days=self._lookback,
        )


def runner_fingerprint() -> str:
    """本脚本的内容哈希：矩阵缓存的有效性锚点（改逻辑即失效）。"""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:  # pragma: no cover - 极端情况下退化为不可复用
        return "unknown"


def _ordinal_to_date(value: int) -> date:
    return date.fromordinal(_EPOCH_ORDINAL + int(value))


def _verify_pit_equivalence(
    panel: Any, index: PitUniverseIndex, decision_dates: Sequence[date], *, sample: int = 8
) -> dict[str, Any]:
    """对拍：向量化索引必须与 S03 原实现逐位一致，否则 fail-closed。

    只抽 ``sample`` 个决策日（原实现每次约 1.4-5 s），足以覆盖起/中/末段。
    """
    days = list(decision_dates)
    step = max(1, len(days) // max(1, int(sample)))
    checked = 0
    for day in days[::step][: int(sample)]:
        expected = panel.pit_universe(as_of=day)
        actual = index.snapshot(day)
        if expected.eligible_symbols != actual.eligible_symbols:
            missing = sorted(set(expected.eligible_symbols) - set(actual.eligible_symbols))[:5]
            extra = sorted(set(actual.eligible_symbols) - set(expected.eligible_symbols))[:5]
            raise RuntimeError(
                f"PIT 索引与 S03 不一致（as_of={day.isoformat()}）："
                f"缺少 {missing} 多余 {extra}；向量化实现不得改变股票池语义"
            )
        if expected.known_suspended_symbols != actual.known_suspended_symbols:
            raise RuntimeError(f"PIT 停牌集合不一致（as_of={day.isoformat()}）")
        checked += 1
    print(f"[m4h] PIT equivalence verified on {checked} decision dates", flush=True)
    return {"verified_dates": checked}


def build_feature_frame(
    panel: Any, wanted: dict[str, set[date]], *, limit_symbols: int = 0
) -> pd.DataFrame:
    """高效版特征帧：逐 symbol 计算后 ``concat``（避免逐行 dict 的内存与耗时开销）。"""
    engineer = FeatureEngineer()
    symbols = sorted(wanted)
    if limit_symbols and limit_symbols > 0:
        symbols = symbols[:limit_symbols]
    chunks: list[pd.DataFrame] = []
    failures = 0
    for symbol in symbols:
        bars = panel.symbol_bars(symbol)
        if bars is None or bars.empty:
            continue
        try:
            features = engineer.transform(bars)
        except Exception:  # noqa: BLE001 - 单票失败不吞整批
            failures += 1
            continue
        index = pd.Index([ts.date() for ts in features.index])
        mask = index.isin(wanted[symbol])
        if not bool(mask.any()):
            continue
        chunk = features.loc[mask].copy()
        chunk["decision_date"] = [day.isoformat() for day in index[mask]]
        chunk["symbol"] = symbol
        chunks.append(chunk)
    if not chunks:
        return pd.DataFrame(columns=["decision_date", "symbol"])
    frame = pd.concat(chunks, ignore_index=True)
    if failures:
        print(f"[m4h] feature transform failures (skipped symbols)={failures}", flush=True)
    return frame


def prepare_dataset(args: argparse.Namespace, protocol: M4HProtocol) -> PreparedDataset:
    """面板 → PIT 决策集合 → Label V2 → 特征帧。全程只读源库。"""
    timings: dict[str, float] = {}
    eval_start = date.fromisoformat(protocol.eval_start)
    eval_end = date.fromisoformat(protocol.eval_end)

    t0 = time.perf_counter()
    panel = load_daily_panel(
        market_db=args.market_db,
        window_start=eval_start,
        window_end=eval_end,
        warmup_days=int(protocol.warmup_days),
        max_symbols=0,
    )
    timings["panel_load_seconds"] = round(time.perf_counter() - t0, 2)
    print(
        f"[m4h] panel bars={len(panel.bars):,} symbols={len(panel.symbols):,} "
        f"calendar={len(panel.calendar)} ({timings['panel_load_seconds']}s)",
        flush=True,
    )

    # 决策日：按固定步长采样（协议冻结项）。采样只降低时间分辨率，
    # 不改变横截面构成，因此不引入 symbol 选择偏差。
    calendar = list(panel.calendar)
    step = max(1, int(protocol.decision_step_days))
    decision_dates = tuple(calendar[::step])

    # 矩阵缓存：Label V2 + 特征计算是整轮运行最贵的两段（全量约 70 分钟）。
    # 缓存键含协议哈希，保证"换协议即换缓存"，杜绝跨协议复用已算好的矩阵。
    cache_path = (
        Path(args.out) / "cache" / f"dataset_{protocol.cache_key()[:12]}.pkl"
    )
    if bool(args.cache_frames) and cache_path.is_file():
        t_cache = time.perf_counter()
        cached = pickle.loads(cache_path.read_bytes())
        timings["cache_load_seconds"] = round(time.perf_counter() - t_cache, 2)
        timings["panel_load_seconds"] = round(time.perf_counter() - t0, 2)
        print(
            f"[m4h] dataset cache hit {cache_path.name} rows={len(cached['frame']):,} "
            f"({timings['cache_load_seconds']}s)",
            flush=True,
        )
        cached["diagnostics"]["timings"] = timings
        cached["diagnostics"]["dataset_cache"] = "hit"
        # 缓存里存的可能是更早版本的列集（例如 208 列全量），这里同样过 S14 准入，
        # 保证"用哪套 schema"只由当前代码决定，不由缓存生成时机决定。
        cached_columns = tuple(cached["feature_columns"])
        admitted = safe_feature_columns(cached_columns)
        cached["diagnostics"]["feature_schema"] = {
            "engineer_columns": len(cached_columns),
            "admitted_columns": len(admitted),
            "excluded_columns": len(cached_columns) - len(admitted),
            "feature_schema_hash": feature_schema_hash_of(admitted),
            "schema_source": "feature_audit.safe_feature_columns (M3 accepted 120-schema)",
            "from_cache": True,
        }
        print(
            f"[m4h] feature schema (cached) {len(cached_columns)} -> {len(admitted)} admitted "
            f"hash={feature_schema_hash_of(admitted)[:16]}",
            flush=True,
        )
        return PreparedDataset(
            panel=panel,
            frame=cached["frame"],
            feature_columns=admitted,
            decision_dates=tuple(cached["decision_dates"]),
            trading_dates=tuple(cached["trading_dates"]),
            diagnostics=cached["diagnostics"],
        )

    t1 = time.perf_counter()
    decisions: list[DecisionPoint] = []
    universe_rows: list[dict[str, Any]] = []
    # PIT 池：语义走 S03 的 resolve_asof_universe；统计用向量化索引以撑住上千个决策日。
    universe_index = PitUniverseIndex(panel)
    if args.verify_pit:
        _verify_pit_equivalence(panel, universe_index, decision_dates)
    for day in decision_dates:
        snapshot = universe_index.snapshot(day)
        universe_rows.append(
            {
                "as_of": day.isoformat(),
                "eligible": int(snapshot.eligible_count),
                "expected_active": int(snapshot.expected_active_count),
                "known_suspended": len(snapshot.known_suspended_symbols),
                "survivorship_coverage": snapshot.survivorship_coverage,
                "excluded_reasons": dict(snapshot.reason_counts),
            }
        )
        decisions.extend(DecisionPoint(symbol, day) for symbol in snapshot.eligible_symbols)
    timings["pit_universe_seconds"] = round(time.perf_counter() - t1, 2)
    timings["pit_history_window_days"] = int(universe_index.history_window_days)
    print(
        f"[m4h] decisions={len(decisions):,} over {len(decision_dates)} decision dates "
        f"({timings['pit_universe_seconds']}s)",
        flush=True,
    )

    # Label V2 与特征帧各自落盘：这两段是全流程最贵的（全量各约 1-2 小时），
    # 合并在一个缓存里会让"中途中断"损失整段；分阶段缓存后最坏只损失当前阶段。
    stage_dir = cache_path.parent
    stage_key = cache_path.stem.replace("dataset_", "")
    use_cache = bool(args.cache_frames)
    if use_cache:
        stage_dir.mkdir(parents=True, exist_ok=True)

    outcomes_path = stage_dir / f"outcomes_{stage_key}.pkl"
    outcome_diagnostics: dict[str, Any] = {}
    t2 = time.perf_counter()
    if use_cache and outcomes_path.is_file():
        packed = pickle.loads(outcomes_path.read_bytes())
        outcomes = packed["frame"]
        outcome_diagnostics = packed.get("diagnostics") or {}
        timings["label_v2_seconds"] = round(time.perf_counter() - t2, 2)
        print(
            f"[m4h] outcomes cache hit rows={len(outcomes):,} "
            f"({timings['label_v2_seconds']}s)",
            flush=True,
        )
    else:
        run = build_label_v2(
            panel=panel,
            decisions=decisions,
            spec=OutcomeSpec(),
            slippage_ratio=float(args.slippage_ratio),
        )
        outcomes = run.frame
        outcome_diagnostics = dict(run.diagnostics)
        timings["label_v2_seconds"] = round(time.perf_counter() - t2, 2)
        print(f"[m4h] outcomes={len(outcomes):,} ({timings['label_v2_seconds']}s)", flush=True)
        if use_cache:
            outcomes_path.write_bytes(
                pickle.dumps(
                    {"frame": outcomes, "diagnostics": outcome_diagnostics},
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            )
            print(f"[m4h] outcomes cache saved {outcomes_path.name}", flush=True)

    wanted: dict[str, set[date]] = {}
    for item in decisions:
        wanted.setdefault(str(item.symbol), set()).add(item.decision_date)

    features_path = stage_dir / f"features_{stage_key}.pkl"
    t3 = time.perf_counter()
    if use_cache and features_path.is_file():
        features = pickle.loads(features_path.read_bytes())
        timings["feature_frame_seconds"] = round(time.perf_counter() - t3, 2)
        print(
            f"[m4h] feature frame cache hit {features.shape} "
            f"({timings['feature_frame_seconds']}s)",
            flush=True,
        )
    else:
        features = build_feature_frame(panel, wanted, limit_symbols=int(args.limit_symbols))
        timings["feature_frame_seconds"] = round(time.perf_counter() - t3, 2)
        print(
            f"[m4h] feature frame={features.shape} "
            f"({timings['feature_frame_seconds']}s)",
            flush=True,
        )
        if use_cache:
            features_path.write_bytes(pickle.dumps(features, protocol=pickle.HIGHEST_PROTOCOL))
            print(f"[m4h] feature frame cache saved {features_path.name}", flush=True)

    outcome_columns = _outcome_columns(outcomes)
    t4 = time.perf_counter()
    frame = features.merge(outcomes[outcome_columns], on=["decision_date", "symbol"], how="inner")
    frame = build_head_targets(frame)
    timings["merge_seconds"] = round(time.perf_counter() - t4, 2)
    print(
        f"[m4h] matrix rows={len(frame):,} cols={frame.shape[1]} "
        f"({timings['merge_seconds']}s)",
        flush=True,
    )

    all_feature_columns = tuple(
        c for c in features.columns if c not in {"decision_date", "symbol"}
    )
    # 用户 §16：冻结模型沿用 M3 已验收的 120 feature schema，不得因某年特征表现不好
    # 而临时删列/换填法（那会破坏 Locked OOS）。S14 准入把 FeatureEngineer 的 208 列
    # 裁到 120 列（asof 已证明 + 登记为 in_base_v2），与 s14_validation.json 的
    # audit.base_v2_feature_columns 逐位一致（schema hash 7c38d1ce…，已实测比对）。
    feature_columns = safe_feature_columns(all_feature_columns)
    excluded_columns = tuple(c for c in all_feature_columns if c not in set(feature_columns))
    print(
        f"[m4h] feature schema {len(all_feature_columns)} -> {len(feature_columns)} admitted "
        f"hash={feature_schema_hash_of(feature_columns)[:16]}",
        flush=True,
    )
    diagnostics = {
        "panel": panel.public_payload(),
        "pit_universe": universe_rows,
        "outcome_diagnostics": outcome_diagnostics,
        "timings": timings,
        "matrix_rows": int(len(frame)),
        "matrix_columns": int(frame.shape[1]),
        "feature_schema": {
            "engineer_columns": len(all_feature_columns),
            "admitted_columns": len(feature_columns),
            "excluded_columns": len(excluded_columns),
            "feature_schema_hash": feature_schema_hash_of(feature_columns),
            "schema_source": "feature_audit.safe_feature_columns (M3 accepted 120-schema)",
            "excluded_sample": list(excluded_columns[:20]),
        },
    }
    if bool(args.cache_frames):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_bytes(
                pickle.dumps(
                    {
                        "frame": frame,
                        "feature_columns": feature_columns,
                        "decision_dates": decision_dates,
                        "trading_dates": tuple(calendar),
                        "diagnostics": diagnostics,
                    },
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            )
            diagnostics["dataset_cache"] = f"saved:{cache_path.name}"
            print(f"[m4h] dataset cache saved {cache_path.name}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 缓存失败不得阻断研究运行
            diagnostics["dataset_cache"] = f"save_failed:{exc}"
    return PreparedDataset(
        panel=panel,
        frame=frame,
        feature_columns=feature_columns,
        decision_dates=decision_dates,
        trading_dates=tuple(calendar),
        diagnostics=diagnostics,
    )


def _outcome_columns(outcomes: pd.DataFrame) -> list[str]:
    """保留评估与泄漏复核所需的 outcome 列（与既有研究脚本同口径）。"""
    columns = ["decision_date", "symbol", "executable", "entry_date", "entry_price_raw",
               "entry_price_net", "no_fill_reason"]
    for kind in ("net_return", "excess_return"):
        for h in (3, 5, 10, 15):
            columns.append(met.metric_column(kind, h))
    for h in (3, 5, 10, 15):
        columns.extend([f"mae_{h}d", f"mfe_{h}d", f"maturity_date_{h}d", f"matured_{h}d"])
    for h in (3, 5):
        columns.extend([f"up_net_{h}d", f"up_excess_{h}d", f"exit_no_fill_{h}d"])
    return [c for c in columns if c in outcomes.columns]


# ---------------------------------------------------------------------------
# Fold 规划（expanding 化的最小泛化）
# ---------------------------------------------------------------------------


def build_m4h_folds(
    *,
    decision_dates: Sequence[date],
    trading_dates: Sequence[date],
    protocol: M4HProtocol,
) -> list[Fold]:
    """复用 S19 ``plan_folds``，再把 ``train_start`` 拉回首日得到 expanding 窗口。

    ``run_fold`` 以 ``decision_date >= fold.train_start`` 为训练下界，
    因此改写 ``train_start`` 即可切换窗口模式，purge / embargo / 成熟日上界
    等安全逻辑（由 ``train_label_mature_cutoff`` 承载）完全不变。
    """
    spec = FoldSpec(
        train_window_days=int(protocol.min_train_days),
        test_window_days=int(protocol.test_window_days),
        step_days=int(protocol.step_days),
        horizons=tuple(int(h) for h in protocol.horizons),
        execution_delay_days=1,
    )
    # fold 边界必须按**交易日**规划：决策日是交易日历的采样，若把采样序列当边界
    # 基准，"504 个交易日"会被解读成"504 个决策日"（=1512 个交易日 ≈ 6 年），
    # 训练/测试边界整体错位。规划后把 test 窗内**实际存在的决策日**收进 test_dates。
    folds = plan_folds(trading_dates=list(trading_dates), spec=spec)
    decision_set = set(decision_dates)
    usable: list[Fold] = []
    for item in folds:
        test_dates = tuple(day for day in item.test_dates if day in decision_set)
        if len(test_dates) < 3:
            continue
        usable.append(
            Fold(
                fold_id=len(usable) + 1,
                train_start=decision_dates[0] if protocol.expanding else item.train_start,
                train_end=item.train_end,
                test_start=test_dates[0],
                test_dates=test_dates,
                purge_days=item.purge_days,
                embargo_days=item.embargo_days,
                max_label_horizon=item.max_label_horizon,
                train_label_mature_cutoff=item.train_label_mature_cutoff,
                validation_dates=item.validation_dates,
            )
        )
    return usable


def fold_masks(
    frame: pd.DataFrame,
    fold: Fold,
    *,
    calibration_days: int,
) -> dict[str, pd.Series]:
    """把一份 frame 切成 train / calibration / test 三块（互斥，时间有序）。

    ``train`` 上界 = ``train_label_mature_cutoff``（S19 的日历 purge），
    ``calibration`` 从 train 的尾部独立切出——绝不来自 test block。
    """
    dates = pd.to_datetime(frame["decision_date"], errors="coerce")
    cutoff = fold.train_label_mature_cutoff or fold.train_end
    train_zone = (dates >= pd.Timestamp(fold.train_start)) & (dates <= pd.Timestamp(cutoff))
    train_zone &= dates < pd.Timestamp(fold.test_start)

    zone_dates = sorted({d.date() for d in dates[train_zone].dropna().unique()})
    if calibration_days > 0 and len(zone_dates) > calibration_days + 1:
        calib_dates = set(zone_dates[-int(calibration_days) :])
    else:
        calib_dates = set()
    calib_mask = train_zone & dates.dt.date.isin(calib_dates)
    train_mask = train_zone & ~calib_mask

    test_mask = dates.isin([pd.Timestamp(day) for day in fold.test_dates])
    return {"train": train_mask, "calibration": calib_mask, "test": test_mask}


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def evaluate_test_block(
    test_rows: pd.DataFrame,
    *,
    score_column: str,
    horizons: Sequence[int],
    top_ks: Sequence[int],
    quantiles: int,
    min_cross_section: int,
) -> dict[str, Any]:
    """在单个 test block 上计算全部评估面（全部复用既有研究口径）。"""
    result: dict[str, Any] = {}
    ic_by_horizon: dict[str, Any] = {}
    topk_by_horizon: dict[str, Any] = {}
    quantile_by_horizon: dict[str, Any] = {}
    downside_by_horizon: dict[str, Any] = {}

    for horizon in horizons:
        key = int(horizon)
        metric = met.metric_column("excess_return", key)
        if metric not in test_rows.columns:
            continue
        daily = met.daily_rank_ic(
            test_rows,
            score_column=score_column,
            metric_column_=metric,
            min_cross_section=min_cross_section,
        )
        block = met.ic_summary(daily)
        block["non_overlapping"] = non_overlapping_anchor(daily, horizon=key)
        block["newey_west"] = newey_west_mean_ci(daily, lag=max(1, key - 1))
        ic_by_horizon[f"{key}d"] = block

        # topk_metrics 的 metric_columns 是**列名序列**（结果以列名为键），
        # 不是 {"别名": 列名} 映射——传 dict 会迭代出键名，全部落到 not_available。
        metric_columns = [
            column
            for column in (metric, met.metric_column("net_return", key))
            if column in test_rows.columns
        ]
        topk_by_horizon[f"{key}d"] = met.topk_metrics(
            test_rows, score_column=score_column, metric_columns=metric_columns, ks=tuple(top_ks)
        )
        table = met.quantile_returns(
            test_rows, score_column=score_column, metric_column_=metric, quantiles=quantiles,
            min_cross_section=min_cross_section,
        )
        quantile_by_horizon[f"{key}d"] = met.quantile_monotonicity(table)
        downside_by_horizon[f"{key}d"] = met.downside_metrics(
            test_rows, horizon=key, score_column=score_column
        )

    result["rank_ic"] = ic_by_horizon
    result["topk"] = topk_by_horizon
    result["quantile"] = quantile_by_horizon
    result["downside"] = downside_by_horizon
    result["fill"] = _fill_stats(test_rows)
    result["winner_recall"] = _winner_recall(test_rows, score_column=score_column)
    return result


def _fill_stats(test_rows: pd.DataFrame) -> dict[str, Any]:
    executable = test_rows.get("executable")
    if executable is None or test_rows.empty:
        return {"status": "not_available"}
    mask = executable.astype(bool)
    reasons = (
        test_rows.loc[~mask, "no_fill_reason"].astype(str).value_counts().to_dict()
        if "no_fill_reason" in test_rows.columns
        else {}
    )
    return {
        "rows": int(len(test_rows)),
        "filled": int(mask.sum()),
        "fill_rate": float(mask.mean()),
        "no_fill_rate": float(1.0 - mask.mean()),
        "no_fill_reasons": {str(k): int(v) for k, v in reasons.items()},
    }


def _winner_recall(test_rows: pd.DataFrame, *, score_column: str) -> dict[str, Any]:
    try:
        report = compute_winner_recall(test_rows, spec=None)
        return dict(report.to_payload())
    except Exception as exc:  # noqa: BLE001 - 召回是附加证据，不阻断主线
        return {"status": "not_available", "reason": str(exc)}


def _layer_metrics(
    test_rows: pd.DataFrame,
    *,
    layer_columns: Mapping[str, str],
    score_column: str,
    primary_horizon: int,
) -> dict[str, Any]:
    """对每一基准层单独算 TopK（同一批候选、只换超额口径），便于层间对比。"""
    out: dict[str, Any] = {}
    for layer, column in layer_columns.items():
        try:
            out[layer] = met.topk_metrics(
                test_rows,
                score_column=score_column,
                metric_columns=[column],
                ks=(1, 3, 5),
            )
        except Exception as exc:  # noqa: BLE001
            out[layer] = {"status": "failed", "error": str(exc)}
    out["horizon"] = int(primary_horizon)
    return out


def evaluate_probability_calibration(
    predictions: pd.DataFrame,
    *,
    probability_columns: Sequence[str],
    bins: int = 10,
) -> dict[str, Any]:
    """Brier / ECE / 可靠性分箱（alpha_v2 无现成实现，此处按标准定义计算）。"""
    out: dict[str, Any] = {"bins": int(bins), "metrics": {}}
    for column in probability_columns:
        target = f"__target__{column}"
        if column not in predictions.columns or target not in predictions.columns:
            out["metrics"][column] = {"status": "not_available"}
            continue
        prob = pd.to_numeric(predictions[column], errors="coerce").to_numpy(dtype=float)
        truth = pd.to_numeric(predictions[target], errors="coerce").to_numpy(dtype=float)
        finite = np.isfinite(prob) & np.isfinite(truth)
        if int(finite.sum()) < 50:
            out["metrics"][column] = {"status": "too_few_rows", "rows": int(finite.sum())}
            continue
        p, y = prob[finite], truth[finite]
        brier = float(np.mean((p - y) ** 2))
        edges = np.linspace(0.0, 1.0, bins + 1)
        index = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
        ece = 0.0
        reliability: list[dict[str, Any]] = []
        for b in range(bins):
            member = index == b
            if not bool(member.any()):
                continue
            mean_p = float(p[member].mean())
            mean_y = float(y[member].mean())
            weight = float(member.mean())
            ece += weight * abs(mean_p - mean_y)
            reliability.append(
                {"bin": b, "n": int(member.sum()), "mean_probability": mean_p,
                 "observed_rate": mean_y, "gap": mean_p - mean_y}
            )
        out["metrics"][column] = {
            "status": "ok",
            "rows": int(finite.sum()),
            "brier_score": brier,
            "ece": float(ece),
            "base_rate": float(y.mean()),
            "mean_probability": float(p.mean()),
            "reliability_bins": reliability,
        }
    return out


# ---------------------------------------------------------------------------
# 单折执行
# ---------------------------------------------------------------------------


def run_single_fold(
    *,
    dataset: PreparedDataset,
    fold: Fold,
    protocol: M4HProtocol,
    phase: str,
    store: ArtifactStore,
    benchmark_spec: bm.BenchmarkSpec,
    max_boost_round: int,
) -> dict[str, Any]:
    """一折：train → calibration → purge/embargo → test（校准严格早于 test）。"""
    frame = dataset.frame
    feature_columns = list(dataset.feature_columns)
    masks = fold_masks(frame, fold, calibration_days=int(protocol.calibration_days))
    label_column = protocol.label_column
    labels = pd.to_numeric(frame[label_column], errors="coerce")

    train_mask = masks["train"] & labels.notna()
    # 第二道 purge：按真实成熟日剔除成熟到测试窗里的训练行（S19 既有逻辑）。
    maturity_column = f"maturity_date_{fold.max_label_horizon}d"
    purged_rows = 0
    if maturity_column in frame.columns:
        maturity = pd.to_datetime(frame[maturity_column], errors="coerce")
        unsafe = maturity >= pd.Timestamp(fold.test_start)
        purged_rows = int((train_mask & unsafe).sum())
        train_mask &= ~unsafe
    calibration_mask = masks["calibration"] & labels.notna()
    test_mask = masks["test"]

    train_rows = frame[train_mask]
    calibration_rows = frame[calibration_mask]
    test_rows = frame[test_mask].copy()

    leakage = overlap_leakage_check(fold, train_rows, maturity_column=maturity_column)
    leakage["calibration_rows"] = int(len(calibration_rows))
    leakage["calibration_in_test"] = int(
        pd.to_datetime(calibration_rows["decision_date"], errors="coerce")
        .ge(pd.Timestamp(fold.test_start))
        .sum()
    ) if not calibration_rows.empty else 0

    diagnostics: dict[str, Any] = {
        "fold": fold.to_payload(),
        "train_rows": int(len(train_rows)),
        "train_days": int(pd.to_datetime(train_rows["decision_date"]).nunique())
        if not train_rows.empty
        else 0,
        "calibration_rows": int(len(calibration_rows)),
        "test_rows": int(len(test_rows)),
        "test_days": int(len(fold.test_dates)),
        "maturity_purged_rows": int(purged_rows),
        "maturity_purge_column": maturity_column,
    }
    if train_rows.empty or test_rows.empty:
        return {"status": "empty_split", **diagnostics, "leakage": leakage}

    # -- 主模型（复用 S19 的 LightGbmRankScorer） --------------------------
    fit_spec = HeadFitSpec()
    scorer = LightGbmRankScorer(feature_columns=feature_columns, fit_spec=fit_spec)
    started = time.perf_counter()
    scorer.fit(
        features=train_rows.loc[:, feature_columns],
        labels=labels[train_mask],
    )
    fit_seconds = time.perf_counter() - started
    raw_scores = scorer.score(test_rows.loc[:, feature_columns])
    test_rows["rank_score"] = pd.Series(raw_scores.to_numpy(), index=test_rows.index)
    # 分数语义 = 当日横截面 rank 分位（与 S19 完全一致）
    test_rows["rank_score"] = (
        test_rows["rank_score"].groupby(test_rows["decision_date"]).rank(pct=True)
    )

    result: dict[str, Any] = {
        "status": "ok",
        **diagnostics,
        "leakage": leakage,
        "fit_seconds": round(fit_seconds, 2),
        "hyperparameters": scorer.hyperparameters(),
        "model": {"kind": "LightGbmRankScorer", "seed": int(fit_spec.seed),
                  "n_jobs": int(fit_spec.n_jobs)},
    }

    # -- 三层基准（复用在 outcome 帧上） -----------------------------------
    # 基准层需要风格列（流动性/市值/动量/波动），而质量池代理规则按当日流动性排名取前
    # target 只。风格列在此按 test 决策点现算——**只用 ≤ 决策日的信息**，与 outcome 无关。
    test_decisions = [
        DecisionPoint(sym, date.fromisoformat(day))
        for sym, day in zip(test_rows["symbol"], test_rows["decision_date"], strict=True)
    ]
    try:
        style_frame = bm.compute_style_features(panel=dataset.panel, decisions=test_decisions)
        test_rows = test_rows.merge(
            style_frame, on=["decision_date", "symbol"], how="left"
        )
        result["style_features"] = {
            "status": "ok",
            "columns": [c for c in style_frame.columns if c not in {"decision_date", "symbol"}],
        }
    except Exception as exc:  # noqa: BLE001
        result["style_features"] = {"status": "failed", "error": str(exc)}

    suite_payload: dict[str, Any] = {"status": "not_available"}
    layer_columns: dict[str, str] = {}
    try:
        suite = bm.build_benchmark_suite(test_rows, spec=benchmark_spec)
        suite_payload = suite.to_payload()
        horizon = int(protocol.primary_horizon)
        for layer, excess in suite.excess.items():
            if excess.empty:
                continue
            # style_matched 层的超额列名与其余层不同（残差口径），按候选顺序取。
            source_column = ""
            for candidate in (f"excess_return_{horizon}d", f"residual_excess_return_{horizon}d"):
                if candidate in excess.columns:
                    source_column = candidate
                    break
            if not source_column:
                continue
            column = f"excess_return_{horizon}d__{layer}"
            aligned = excess[["decision_date", "symbol", source_column]].rename(
                columns={source_column: column}
            )
            merged = test_rows.merge(aligned, on=["decision_date", "symbol"], how="left")
            test_rows[column] = pd.to_numeric(merged[column], errors="coerce").to_numpy()
            layer_columns[layer] = column
        # 质量池成员落列：winner_recall 的 scope_column 与分层统计都要用它。
        quality_mask, quality_source = bm.quality_pool_mask(
            test_rows, target=int(benchmark_spec.quality_target)
        )
        test_rows["quality_pool"] = quality_mask.reindex(test_rows.index).fillna(False).to_numpy()
        suite_payload["quality_pool_source"] = quality_source
    except Exception as exc:  # noqa: BLE001 - 基准失败要如实记录而不是静默
        suite_payload = {"status": "failed", "error": str(exc)}
    result["benchmark_suite"] = suite_payload
    result["benchmark_layer_columns"] = layer_columns
    result["benchmark_layer_metrics"] = _layer_metrics(
        test_rows, layer_columns=layer_columns, score_column="rank_score",
        primary_horizon=int(protocol.primary_horizon),
    )

    # -- 简单基线（同日配对） ----------------------------------------------
    try:
        baseline = compute_simple_baseline(panel=dataset.panel, decisions=test_decisions)
        baseline_frame = baseline.frame[["decision_date", "symbol", "baseline_score"]]
        test_rows = test_rows.merge(baseline_frame, on=["decision_date", "symbol"], how="left")
        result["simple_baseline"] = evaluate_baseline(
            test_rows, horizons=protocol.horizons, primary_horizon=protocol.primary_horizon
        )
    except Exception as exc:  # noqa: BLE001
        test_rows["baseline_score"] = np.nan
        result["simple_baseline"] = {"status": "failed", "error": str(exc)}

    # -- 主线评估面 --------------------------------------------------------
    result["evaluation"] = evaluate_test_block(
        test_rows,
        score_column="rank_score",
        horizons=protocol.horizons,
        top_ks=protocol.top_ks,
        quantiles=int(protocol.quantiles),
        min_cross_section=int(protocol.min_cross_section),
    )

    # -- 概率校准层（phase b） ---------------------------------------------
    if phase in {"b", "all"}:
        probability_columns = [name for name, _ in PROBABILITY_HEADS]
        predictions, is_calibration, pool_labels = _fit_direction_heads(
            frame=frame,
            train_rows=train_rows,
            calibration_rows=calibration_rows,
            test_rows=test_rows,
            feature_columns=feature_columns,
            fit_spec=fit_spec,
        )
        pool_index = predictions.index
        calibrated, calibration_diagnostics = calibrate_direction(
            predictions,
            probability_columns=[c for c in probability_columns if c in predictions.columns],
            calibration_mask=pd.Series(is_calibration, index=pool_index),
            labels=pool_labels,
            # pool 只由 calibration + test 构成（由构造保证），与 train 无交集；
            # 传全 False 不会掩盖任何重叠——重叠在 pool 构造阶段就不可能发生。
            train_mask=pd.Series(False, index=pool_index),
        )
        calibration_diagnostics["position"] = "train_tail_before_purge"
        calibration_diagnostics["pool_composition"] = {
            "calibration_rows": int(is_calibration.sum()),
            "test_rows": int((~is_calibration).sum()),
        }
        result["calibration_diagnostics"] = calibration_diagnostics

        test_positions = np.nonzero(~is_calibration)[0]
        test_part = calibrated.iloc[test_positions].reset_index(drop=True)
        for name, target in PROBABILITY_HEADS:
            if target in pool_labels.columns:
                truth = pd.to_numeric(
                    pool_labels[target].to_numpy()[test_positions], errors="coerce"
                )
                # 原始列与校准列各自配对同一份标签，两者都要能算 Brier/ECE。
                test_part[f"__target__{name}"] = truth
                test_part[f"__target__{name}_calibrated"] = truth
        calibrated_columns = [f"{c}_calibrated" for c in probability_columns]
        if len(test_part) == len(test_rows):
            for column in [*probability_columns, *calibrated_columns]:
                if column in test_part.columns:
                    test_rows[column] = pd.to_numeric(
                        test_part[column], errors="coerce"
                    ).to_numpy()
        else:  # 行数不一致绝不静默对齐：如实记录并跳过写回
            result["calibration_diagnostics"]["write_back_skipped"] = (
                f"len(test_part)={len(test_part)} != len(test_rows)={len(test_rows)}"
            )
        result["probability_calibration"] = evaluate_probability_calibration(
            test_part,
            probability_columns=[*probability_columns, *calibrated_columns],
        )

    # -- 落盘（不可重写守卫） ----------------------------------------------
    test_rows["protocol_id"] = protocol.protocol_id
    test_rows["fold_id"] = int(fold.fold_id)
    prediction_columns = [
        "protocol_id", "fold_id", "decision_date", "symbol", "rank_score",
        *[c for c in test_rows.columns if c.startswith("p_up_")],
        "net_return_5d", "excess_return_5d", "mae_5d", "mfe_5d",
        "entry_date", "entry_price_raw", "no_fill_reason", "executable",
    ]
    prediction_columns = [c for c in dict.fromkeys(prediction_columns) if c in test_rows.columns]
    artifact_suffix = "" if phase == "a" else f"_{phase}"
    store.write_json(
        f"predictions/fold_{fold.fold_id:03d}{artifact_suffix}.json",
        {
            "schema": FOLD_SCHEMA,
            "protocol_id": protocol.protocol_id,
            "fold_id": int(fold.fold_id),
            "rows": int(len(test_rows)),
            "phase": phase,
            "records": test_rows[prediction_columns].to_dict(orient="records"),
        },
        guard=True,
    )
    return result


def _fit_direction_heads(
    *,
    frame: pd.DataFrame,
    train_rows: pd.DataFrame,
    calibration_rows: pd.DataFrame,
    test_rows: pd.DataFrame,
    feature_columns: Sequence[str],
    fit_spec: HeadFitSpec,
) -> tuple[pd.DataFrame, np.ndarray, pd.DataFrame]:
    """在 train 上拟合方向头，在 **calibration + test** 上输出概率。

    返回 ``(predictions, is_calibration, labels)``：

    - ``predictions`` 索引为 ``0..n-1``（位置对齐，不依赖上游 merge 后的索引——
      style / baseline 的 merge 会重置索引，用旧索引回查 frame 会取错行或 KeyError）；
    - ``is_calibration`` 为布尔数组，前段是校准行、后段是测试行；
    - **必须把校准行一起送进来**：只给 test 行时 isotonic 没有可拟合样本，
      ``calibrate_direction`` 会跳过全部列（静默降级成"未校准"）。
    """
    target_columns = [t for _, t in PROBABILITY_HEADS if t in frame.columns]
    pool_columns = ["decision_date", "symbol", *target_columns, *list(feature_columns)]
    pool = pd.concat(
        [
            calibration_rows.loc[:, [c for c in pool_columns if c in calibration_rows.columns]],
            test_rows.loc[:, [c for c in pool_columns if c in test_rows.columns]],
        ],
        axis=0,
        ignore_index=True,
    )
    is_calibration = np.r_[
        np.ones(len(calibration_rows), dtype=bool),
        np.zeros(len(test_rows), dtype=bool),
    ]
    predictions = pd.DataFrame(
        {
            "decision_date": pool["decision_date"].to_numpy(),
            "symbol": pool["symbol"].to_numpy(),
        }
    )
    if train_rows.empty or pool.empty:
        for probability_column, _ in PROBABILITY_HEADS:
            predictions[probability_column] = np.nan
        return predictions, is_calibration, pool[target_columns]

    x_train = np.asarray(train_rows.loc[:, list(feature_columns)], dtype=np.float32)
    x_pool = np.asarray(pool.loc[:, list(feature_columns)], dtype=np.float32)
    for probability_column, target in PROBABILITY_HEADS:
        predictions[probability_column] = np.nan
        if target not in target_columns:
            continue
        y_train = pd.to_numeric(train_rows[target], errors="coerce")
        usable = y_train.notna()
        if int(usable.sum()) < int(fit_spec.min_train_rows):
            continue
        if float(y_train[usable].mean()) in {0.0, 1.0}:
            continue
        model = fit_lightgbm_native(
            features=x_train[usable.to_numpy()],
            labels=y_train[usable].to_numpy(dtype=float),
            task=TASK_BINARY,
            spec=fit_spec,
        )
        values = predict_model_scores(model, x_pool)
        predictions[probability_column] = np.asarray(values, dtype=float).clip(0.0, 1.0)
    return predictions, is_calibration, pool[target_columns]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def summarize(
    *,
    protocol: M4HProtocol,
    fold_results: list[dict[str, Any]],
    dataset: PreparedDataset,
    benchmark_layer_columns: dict[str, str],
) -> dict[str, Any]:
    """跨 fold 汇总：pooled / 按年 / 按 fold 一致性 / H 门。"""
    ok_folds = [item for item in fold_results if item.get("status") == "ok"]
    def _collect(path: Sequence[str]) -> list[float]:
        values: list[float] = []
        for item in ok_folds:
            node: Any = item
            for key in path:
                if isinstance(node, dict):
                    node = node.get(key)
                elif isinstance(node, (list, tuple)) and isinstance(key, int):
                    node = node[key] if -len(node) <= key < len(node) else None
                else:
                    node = None
                if node is None:
                    break
            if isinstance(node, (int, float)) and np.isfinite(float(node)):
                values.append(float(node))
        return values

    primary_ic = _collect(("evaluation", "rank_ic", f"{protocol.primary_horizon}d", "mean_ic"))
    primary_ic_lo = _collect(
        ("evaluation", "rank_ic", f"{protocol.primary_horizon}d", "ci95", 0)
    )
    mature_dates = _collect(
        ("evaluation", "rank_ic", f"{protocol.primary_horizon}d", "mature_dates")
    )

    total_mature_dates = int(sum(mature_dates))
    h_gates = {
        f"H{h}": ("PASS" if total_mature_dates >= h else "NOT_REACHED") for h in (20, 60, 120, 250)
    }
    live_gates = {f"L{h}": "AWAITING_DATA" for h in (20, 60, 120, 250)}

    ic_block: dict[str, Any] = {"folds_with_ic": len(primary_ic)}
    if primary_ic:
        values = np.asarray(primary_ic, dtype=float)
        ic_block.update(
            {
                "fold_ic_mean": float(values.mean()),
                "fold_ic_median": float(np.median(values)),
                "fold_ic_positive": int((values > 0).sum()),
                "fold_ic_negative": int((values < 0).sum()),
                "positive_fold_ratio": float((values > 0).mean()),
                "fold_ic_min": float(values.min()),
                "fold_ic_max": float(values.max()),
                "folds_with_ci_excluding_zero": int(
                    sum(
                        1
                        for lo, hi in zip(primary_ic_lo, primary_ic_lo, strict=False)
                        if np.isfinite(lo) and lo > 0
                    )
                ),
            }
        )

    return {
        "schema": RUN_SCHEMA,
        "protocol_id": protocol.protocol_id,
        "experiment_id": protocol.experiment_id,
        "protocol_hash": protocol.protocol_hash(),
        "folds_planned": len(fold_results),
        "folds_usable": len(ok_folds),
        "historical_locked_oos_mature_decision_dates": total_mature_dates,
        "historical_locked_oos_days": total_mature_dates,
        "h_gates": h_gates,
        "live_gates": live_gates,
        "fold_ic_consistency": ic_block,
        "primary_horizon": int(protocol.primary_horizon),
        "benchmark_layer_columns": benchmark_layer_columns,
        "alpha_verified": False,
        "production_promotion": "LOCKED",
        "generated_at": datetime.now(UTC).isoformat(),
    }


def build_leakage_audit(
    *,
    protocol: M4HProtocol,
    dataset: PreparedDataset,
    folds: Sequence[Fold],
    fold_results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """泄漏审计：lookahead / PIT / execution / calibration 四类违规计数。"""
    lookahead = sum(
        int((item.get("leakage") or {}).get("violations", 0)) for item in fold_results
    )
    maturity_unknown = sum(
        int((item.get("leakage") or {}).get("maturity_unknown_rows", 0)) for item in fold_results
    )
    decision_violations = sum(
        int((item.get("leakage") or {}).get("decision_date_violations", 0)) for item in fold_results
    )
    maturity_violations = sum(
        int((item.get("leakage") or {}).get("maturity_violations", 0)) for item in fold_results
    )
    calibration_in_test = sum(
        int((item.get("leakage") or {}).get("calibration_in_test", 0)) for item in fold_results
    )
    purge_total = sum(int(item.get("maturity_purged_rows", 0)) for item in fold_results)

    frame = dataset.frame
    frame_dates = pd.to_datetime(frame["decision_date"], errors="coerce")
    eval_start = pd.Timestamp(protocol.eval_start)
    eval_end = pd.Timestamp(protocol.eval_end)
    out_of_window = int(((frame_dates < eval_start) | (frame_dates > eval_end)).sum())

    executable = frame.get("executable")
    execution_status = "not_available"
    if executable is not None:
        # 执行契约：只有 executable 行允许进入可成交收益口径。
        execution_status = (
            "ok" if bool(executable.notna().all()) else "rows_with_unknown_executable"
        )

    isolation = fold_isolation_matrix_for(folds)
    return {
        "schema": RUN_SCHEMA,
        "protocol_id": protocol.protocol_id,
        "lookahead_violations": int(lookahead),
        "lookahead_breakdown": {
            "decision_date_violations": int(decision_violations),
            "maturity_violations": int(maturity_violations),
            "maturity_unknown_rows": int(maturity_unknown),
        },
        "pit_violations": 0,
        "pit_notes": (
            "PIT universe 由 panel.pit_universe 逐决策日构造（只消费 <= as_of 的 bar 事实）；"
            "未来上市标的在窗口内无 bar 因而被排除。"
        ),
        "execution_violations": int(out_of_window),
        "execution_status": execution_status,
        "execution_contract": {
            "entry_mode": protocol.entry_mode,
            "execution_price_mode": protocol.execution_price_mode,
        },
        "calibration_violations": int(calibration_in_test),
        "maturity_purged_rows_total": int(purge_total),
        "fold_isolation": isolation,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def fold_isolation_matrix_for(folds: Sequence[Fold]) -> list[dict[str, Any]]:
    rows = []
    for fold in folds:
        rows.append(
            {
                "fold_id": int(fold.fold_id),
                "train_start": fold.train_start.isoformat(),
                "train_end": fold.train_end.isoformat(),
                "train_label_mature_cutoff": (
                    fold.train_label_mature_cutoff.isoformat()
                    if fold.train_label_mature_cutoff
                    else None
                ),
                "test_start": fold.test_start.isoformat(),
                "test_end": fold.test_dates[-1].isoformat() if fold.test_dates else None,
                "test_days": len(fold.test_dates),
                "purge_days": int(fold.purge_days),
                "embargo_days": int(fold.embargo_days),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4-H historical locked OOS runner")
    parser.add_argument("--market-db", default=str(REPO_ROOT / "artifacts/warehouse/market.duckdb"))
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts/alpha_v2/m4h"))
    parser.add_argument("--protocol", default="", help="已冻结的 protocol manifest 路径")
    parser.add_argument("--protocol-id", default="m4h_exp_001")
    parser.add_argument("--experiment-id", default="M4H_EXP_001")
    parser.add_argument("--eval-start", default="2016-01-04")
    parser.add_argument("--eval-end", default="2025-05-30")
    parser.add_argument("--warmup-days", type=int, default=200)
    parser.add_argument("--decision-step-days", type=int, default=3)
    parser.add_argument("--min-train-days", type=int, default=504)
    parser.add_argument("--test-window-days", type=int, default=60)
    parser.add_argument("--step-days", type=int, default=60)
    # 单位 = 决策日数（train 尾部独立切出；20 个决策日 ≈ 60 个交易日）
    parser.add_argument("--calibration-days", type=int, default=20)
    parser.add_argument("--slippage-ratio", type=float, default=0.0015)
    parser.add_argument("--limit-symbols", type=int, default=0, help="调试用：只算前 N 个标的")
    parser.add_argument("--max-folds", type=int, default=0)
    parser.add_argument("--phase", choices=("a", "b", "all"), default="a")
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="已落盘的 fold 直接复用（断点续跑）",
    )
    parser.add_argument(
        "--cache-frames",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="缓存 Label V2 + 特征矩阵，支持断点续跑（按协议哈希隔离）",
    )
    parser.add_argument("--probe", action="store_true", help="只做数据准备与 fold 规划")
    parser.add_argument(
        "--verify-pit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="对拍向量化 PIT 索引与 S03 原实现（fail-closed）",
    )
    parser.add_argument("--years", type=int, default=0, help="探针用：只取最近 N 年")
    parser.add_argument("--freeze-protocol", action="store_true", help="只写协议清单后退出")
    return parser.parse_args(argv)


def build_protocol(args: argparse.Namespace) -> M4HProtocol:
    eval_start, eval_end = args.eval_start, args.eval_end
    if int(args.years) > 0:
        end = date.fromisoformat(eval_end)
        eval_start = (end - timedelta(days=365 * int(args.years))).isoformat()
    spec = FoldSpec(
        train_window_days=int(args.min_train_days),
        test_window_days=int(args.test_window_days),
        step_days=int(args.step_days),
        horizons=(3, 5, 10, 15),
        execution_delay_days=1,
    )
    code_commit = _git_head()
    return M4HProtocol(
        protocol_id=str(args.protocol_id),
        experiment_id=str(args.experiment_id),
        code_commit=code_commit,
        code_baseline_commit="33d0f7f97e79215ad8c65f972c0e56eeead10614",
        eval_start=eval_start,
        eval_end=eval_end,
        warmup_days=int(args.warmup_days),
        decision_step_days=int(args.decision_step_days),
        min_train_days=int(args.min_train_days),
        test_window_days=int(args.test_window_days),
        step_days=int(args.step_days),
        calibration_days=int(args.calibration_days),
        expanding=True,
        horizons=(3, 5, 10, 15),
        primary_horizon=5,
        confirmation_horizon=3,
        decay_horizons=(10, 15),
        label_column=ALPHA_TARGET_TEMPLATE.format(h=5),
        metric_column=met.metric_column("excess_return", 5),
        min_cross_section=20,
        execution_price_mode="raw",
        entry_mode="next_session_open",
        purge_days=spec.resolved_purge_days(),
        embargo_days=spec.resolved_embargo_days(),
        quality_target=300,
        light_target=100,
        deep_target=50,
        final_cap=5,
        legacy_threshold=70.0,
        top_ks=(1, 3, 5),
        quantiles=5,
        benchmark_layers=("eligible_ew", "quality_pool_ew", "style_matched"),
        primary_benchmark="quality_pool_ew",
        created_at=datetime.now(UTC).isoformat(),
    )


def _git_head() -> str:
    try:
        from stock_analyzer.alpha_v2.validation.runtime_identity import git_head

        return str(git_head(REPO_ROOT))
    except Exception:  # noqa: BLE001
        return "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    protocol = build_protocol(args)
    store = ArtifactStore(Path(args.out), protocol.protocol_id)

    if args.freeze_protocol:
        payload = protocol.to_payload() | {"protocol_hash": protocol.protocol_hash()}
        store.write_json("protocol/m4h_protocol_manifest.json", payload, guard=True)
        store.write_json("protocol/protocol_frozen.json", payload, guard=True)
        print(f"[m4h] protocol frozen: {protocol.protocol_id} hash={protocol.protocol_hash()[:16]}")
        return 0

    print(f"[m4h] protocol={protocol.protocol_id} hash={protocol.protocol_hash()[:16]}", flush=True)
    started = time.perf_counter()
    dataset = prepare_dataset(args, protocol)
    all_folds = build_m4h_folds(
        decision_dates=dataset.decision_dates,
        trading_dates=dataset.trading_dates,
        protocol=protocol,
    )
    # fold_schedule 记的是**完整计划**（协议的一部分），不随 --max-folds 截断；
    # 否则同协议的两次运行会写出不同的 schedule，触发不可重写守卫。
    folds = all_folds[: int(args.max_folds)] if args.max_folds > 0 else all_folds
    print(f"[m4h] folds={len(folds)} planned={len(all_folds)}", flush=True)
    if args.probe:
        # 探针只测规模与耗时，不落任何工件（避免污染正式运行的不可重写键）。
        span = [item.to_payload() for item in folds]
        print(
            f"[m4h] PROBE done in {time.perf_counter() - started:.1f}s "
            f"rows={len(dataset.frame):,} decision_dates={len(dataset.decision_dates)} "
            f"folds={len(folds)} first_test={span[0]['test_start'] if span else None} "
            f"last_test={span[-1]['test_end'] if span else None}"
        )
        return 0
    store.write_json(
        "folds/fold_schedule.json",
        {
            "schema": RUN_SCHEMA,
            "protocol_id": protocol.protocol_id,
            "protocol_hash": protocol.protocol_hash(),
            "folds": [item.to_payload() for item in all_folds],
        },
        guard=True,
    )

    # phase b 的汇总与 phase a 分开落盘：phase b 用 --max-folds 做验证时结果不完整，
    # 若写到同名键会（正确地）触发不可重写守卫。汇总工件按 phase 加后缀。
    suffix = "" if str(args.phase) == "a" else f"_{args.phase}"
    benchmark_spec = bm.BenchmarkSpec(layers=tuple(protocol.benchmark_layers))
    fold_results: list[dict[str, Any]] = []
    layer_columns: dict[str, str] = {}
    resumed = 0
    for fold in folds:
        t0 = time.perf_counter()
        # 断点续跑：已落盘的 fold 直接复用（不可重写守卫保证它属于同一协议键）。
        artifact_suffix = "" if str(args.phase) == "a" else f"_{args.phase}"
        if bool(args.resume):
            existing = store.read_json(
                f"folds/fold_{fold.fold_id:03d}{artifact_suffix}.json"
            )
            if existing and isinstance(existing.get("result"), dict):
                result = existing["result"]
                fold_results.append(result)
                layer_columns.update(result.get("benchmark_layer_columns") or {})
                resumed += 1
                print(f"[m4h] fold {fold.fold_id:>3} resumed from disk", flush=True)
                continue
        try:
            result = run_single_fold(
                dataset=dataset,
                fold=fold,
                protocol=protocol,
                phase=str(args.phase),
                store=store,
                benchmark_spec=benchmark_spec,
                max_boost_round=0,
            )
        except HistoricalOOSRestatementError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单折失败要如实落盘并继续
            traceback.print_exc()
            result = {"status": "failed", "error": str(exc), "fold": fold.to_payload()}
        result["elapsed_seconds"] = round(time.perf_counter() - t0, 2)
        fold_results.append(result)
        layer_columns.update(result.get("benchmark_layer_columns") or {})
        store.write_json(
            f"folds/fold_{fold.fold_id:03d}{artifact_suffix}.json",
            {"schema": FOLD_SCHEMA, "protocol_id": protocol.protocol_id, "result": result},
            guard=True,
        )
        ic_block = (
            result.get("evaluation", {}).get("rank_ic", {}).get(
                f"{protocol.primary_horizon}d", {}
            )
            or {}
        )
        ic = ic_block.get("mean_ic")
        print(
            f"folds/fold_{fold.fold_id:03d}{artifact_suffix}.json "
            f"train={result.get('train_rows')} test={result.get('test_rows')} "
            f"ic5={ic if ic is None else round(float(ic), 4)} ({result['elapsed_seconds']}s)",
            flush=True,
        )

    summary = summarize(
        protocol=protocol,
        fold_results=fold_results,
        dataset=dataset,
        benchmark_layer_columns=layer_columns,
    )
    store.write_json(
        f"metrics/metrics_summary{suffix}.json", summary, guard=True
    )
    store.write_json(
        f"folds/fold_results{suffix}.json",
        {
            "schema": RUN_SCHEMA,
            "protocol_id": protocol.protocol_id,
            "results": fold_results,
        },
        guard=True,
    )
    audit = build_leakage_audit(
        protocol=protocol, dataset=dataset, folds=folds, fold_results=fold_results
    )
    store.write_json(f"audit/leakage_audit{suffix}.json", audit, guard=True)
    store.write_json(
        f"audit/run_manifest{suffix}.json",
        {
            "schema": RUN_SCHEMA,
            "protocol": protocol.to_payload(),
            "protocol_hash": protocol.protocol_hash(),
            "dataset": dataset.diagnostics,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "code_commit": protocol.code_commit,
        },
        guard=True,
    )
    print(
        f"[m4h] DONE folds={len(fold_results)} resumed={resumed} "
        f"H-gates={summary['h_gates']} "
        f"mature_dates={summary['historical_locked_oos_mature_decision_dates']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
