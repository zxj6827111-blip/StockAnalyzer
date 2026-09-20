"""Alpha V2 Benchmark 体系（S12 / 原 P1-02）。

**为什么不能只跟全市场比**：一个系统只要"比全市场好"，仍然可能只是因为它选的
股票本来就都在 Quality300 里（选池效应），而不是排序带来了增量。所以每条预测
必须同时给出三层基准：

======================  ==============================================
``eligible_ew``         PIT 合格股票池等权 —— 系统整体是否创造收益
``quality_pool_ew``     **最关键**：相对"已经进入质量池的股票"是否有增量
``style_matched``       同板块 + 同风格近邻对照 —— 扣掉风格暴露后的残差超额
``baseline``            简单因子基准（S15 提供成员/分数，本层复用同一机制）
======================  ==============================================

三条纪律：

1. **基准必须与候选同口径**：同一天、同一入场（T+1 开盘）、同一退出（第 h 个
   持有日收盘）、同样只统计"可成交且已成熟"的样本。基准用"假设能成交"的收益
   会把自己抬高，从而把 Alpha 抹平；
2. **风格维度只用 ≤ 决策日的数据**：20 日动量/波动/成交额都由决策日往前取，
   分位桶在**当日横截面内**计算——这是"同一天的可比性"，不是用未来信息分层；
3. **来源必须自述**：质量池既可能是生产选股链路的真实输出，也可能是研究侧的
   代理池。二者不能混为一谈，故每次运行都写 ``quality_pool_source``。
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.outcomes import (
    HORIZONS,
    NOT_AVAILABLE,
    attach_excess_returns,
    benchmark_series_from_outcomes,
)
from stock_analyzer.alpha_v2.research.panel import DailyPanel

BENCHMARK_ELIGIBLE = "eligible_ew"
BENCHMARK_QUALITY_POOL = "quality_pool_ew"
BENCHMARK_STYLE_MATCHED = "style_matched"
BENCHMARK_BASELINE = "simple_baseline"

DEFAULT_LAYERS: tuple[str, ...] = (
    BENCHMARK_ELIGIBLE,
    BENCHMARK_QUALITY_POOL,
    BENCHMARK_STYLE_MATCHED,
)
# 主基准：蓝图 §P1-02 明确"Quality Pool EW 最重要"。
PRIMARY_LAYER = BENCHMARK_QUALITY_POOL

# 研究侧质量池代理规则（PIT 安全、可复现、显式登记）。
QUALITY_POOL_RULESET_PROXY = "alpha_v2_quality_v1"
QUALITY_POOL_SOURCE_PRODUCTION = "production_selection_engine"
QUALITY_POOL_SOURCE_PROXY = f"research_proxy:{QUALITY_POOL_RULESET_PROXY}"

# 风格维度（蓝图 §P1-02 要求至少覆盖：行业/流通市值/波动/动量/成交额）。
STYLE_DIM_BOARD = "style_board"
STYLE_DIM_FLOAT_CAP = "style_float_cap_log"
STYLE_DIM_VOL = "style_vol_20d"
STYLE_DIM_MOMENTUM = "style_momentum_20d"
STYLE_DIM_TURNOVER = "style_turnover_20d"

DEFAULT_STYLE_DIMS: tuple[str, ...] = (
    STYLE_DIM_FLOAT_CAP,
    STYLE_DIM_VOL,
    STYLE_DIM_MOMENTUM,
    STYLE_DIM_TURNOVER,
)
STYLE_BOARD_DIM = STYLE_DIM_BOARD

STYLE_LOOKBACK = 20
DEFAULT_STYLE_K = 20
STYLE_MIN_PEERS = 5


@dataclass(frozen=True, slots=True)
class BenchmarkSpec:
    """一次基准套件的口径（全部进审计工件）。"""

    layers: tuple[str, ...] = DEFAULT_LAYERS
    primary_layer: str = PRIMARY_LAYER
    quality_target: int = 300
    quality_ruleset: str = QUALITY_POOL_RULESET_PROXY
    style_dims: tuple[str, ...] = DEFAULT_STYLE_DIMS
    style_k: int = DEFAULT_STYLE_K
    style_min_peers: int = STYLE_MIN_PEERS
    horizons: tuple[int, ...] = HORIZONS

    def to_payload(self) -> dict[str, object]:
        return {
            "layers": list(self.layers),
            "primary_layer": self.primary_layer,
            "quality_target": int(self.quality_target),
            "quality_ruleset": self.quality_ruleset,
            "style_dims": list(self.style_dims),
            "style_board_dim": STYLE_BOARD_DIM,
            "style_k": int(self.style_k),
            "style_min_peers": int(self.style_min_peers),
            "style_matching": "same_board_knn_on_standardized_dims",
            "horizons": [int(h) for h in self.horizons],
            "matched_on_same_entry_exit_window": True,
        }


@dataclass
class BenchmarkSuite:
    """三层基准的成员、收益序列与超额列。"""

    spec: BenchmarkSpec
    series: dict[str, pd.DataFrame]
    excess: dict[str, pd.DataFrame]
    report: dict[str, object] = field(default_factory=dict)

    @property
    def primary(self) -> str:
        """主基准层：配置指定层缺席时退回**第一个可用层**（并保持可审计）。

        例如只想看 style layer 的调用方传了 ``layers=("style_matched",)``，
        此时不能因为 ``primary_layer`` 默认是 quality_pool 就报 not_available——
        那会把"层没被要求"伪装成"层算不出来"。
        """
        if self.spec.primary_layer in self.excess:
            return self.spec.primary_layer
        for name in self.spec.layers:
            if name in self.excess:
                return name
        return self.spec.primary_layer

    def primary_excess(self) -> pd.DataFrame:
        return self.excess.get(self.primary, pd.DataFrame())

    def to_payload(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_payload(),
            "layers": {
                name: {
                    "rows": int(len(frame)),
                    "median_pool_size": (
                        float(frame["pool_size"].median()) if not frame.empty else 0.0
                    ),
                }
                for name, frame in self.series.items()
            },
            "report": dict(self.report),
        }


# ---------------------------------------------------------------------------
# 风格特征（PIT 安全：只用 ≤ 决策日的数据）
# ---------------------------------------------------------------------------


def compute_style_features(
    *,
    panel: DailyPanel,
    decisions: Sequence[Any],
    lookback: int = STYLE_LOOKBACK,
) -> pd.DataFrame:
    """逐 (symbol, decision_date) 算风格维度（同一输入 → 同一结果）。

    ``decisions`` 元素需有 ``symbol`` / ``decision_date`` 两个属性（S11 的
    :class:`~stock_analyzer.alpha_v2.research.outcomes.DecisionPoint` 即可）。
    """
    grouped: dict[str, list[Any]] = {}
    for item in decisions:
        grouped.setdefault(str(item.symbol), []).append(item)

    rows: list[dict[str, object]] = []
    window = max(2, int(lookback))
    for symbol in sorted(grouped):
        frame = panel.symbol_bars(symbol)
        if frame is None or frame.empty:
            for item in grouped[symbol]:
                rows.append(
                    {
                        "decision_date": item.decision_date.isoformat(),
                        "symbol": symbol,
                        STYLE_DIM_BOARD: "unknown",
                        STYLE_DIM_FLOAT_CAP: math.nan,
                        STYLE_DIM_VOL: math.nan,
                        STYLE_DIM_MOMENTUM: math.nan,
                        STYLE_DIM_TURNOVER: math.nan,
                    }
                )
            continue
        dates = [ts.date() for ts in frame.index]
        positions = {day: index for index, day in enumerate(dates)}
        closes = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
        turnovers = pd.to_numeric(frame["turnover"], errors="coerce").to_numpy(dtype=float)
        caps = pd.to_numeric(frame["float_market_cap"], errors="coerce").to_numpy(dtype=float)
        for item in grouped[symbol]:
            position = positions.get(item.decision_date)
            if position is None:
                continue
            start = max(0, position - window + 1)
            window_closes = closes[start : position + 1]
            window_turnovers = turnovers[start : position + 1]
            returns = np.diff(window_closes) / window_closes[:-1]
            momentum = (
                float(window_closes[-1] / window_closes[0] - 1.0)
                if len(window_closes) > 1 and window_closes[0] > 0
                else math.nan
            )
            volatility = (
                float(np.nanstd(returns, ddof=0))
                if returns.size > 1 and np.isfinite(returns).sum() > 1
                else math.nan
            )
            turnover_mean = (
                float(np.nanmean(window_turnovers))
                if np.isfinite(window_turnovers).any()
                else math.nan
            )
            cap = caps[position] if position < len(caps) else math.nan
            rows.append(
                {
                    "decision_date": item.decision_date.isoformat(),
                    "symbol": symbol,
                    STYLE_DIM_BOARD: str(frame.iloc[position].get("board") or "unknown"),
                    STYLE_DIM_FLOAT_CAP: (
                        float(math.log10(cap)) if _finite(cap) and cap > 0 else math.nan
                    ),
                    STYLE_DIM_VOL: volatility,
                    STYLE_DIM_MOMENTUM: momentum,
                    STYLE_DIM_TURNOVER: turnover_mean,
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(
            columns=["decision_date", "symbol", STYLE_DIM_BOARD, *DEFAULT_STYLE_DIMS]
        )
    return frame


def _finite(value: object) -> bool:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed)


# ---------------------------------------------------------------------------
# 池子成员
# ---------------------------------------------------------------------------


def eligible_pool_mask(frame: pd.DataFrame) -> pd.Series:
    """Eligible Universe EW 的成员：决策集合本身（已是 PIT 合格池）。"""
    return pd.Series(True, index=frame.index)


def quality_pool_mask(
    frame: pd.DataFrame,
    *,
    target: int = 300,
    membership: Mapping[str, Sequence[str]] | pd.Series | None = None,
) -> tuple[pd.Series, str]:
    """质量池成员掩码 + 来源标签。

    ``membership`` 给了就用**生产选股链路的真实成员**（``{decision_date: [symbol...]}``
    或 ``{symbol: bool}`` 形式），并标 ``production_selection_engine``；没给则用
    研究侧代理规则 :data:`QUALITY_POOL_RULESET_PROXY`：
    PIT 合格 + 当日 20 日平均成交额排名前 ``target`` —— 显式登记、可复现、
    与生产链路**不是同一件事**，因此来源必须写进报告。
    """
    if membership is not None:
        mask = _membership_mask(frame, membership)
        return mask, QUALITY_POOL_SOURCE_PRODUCTION
    if frame.empty:
        return pd.Series(dtype=bool), QUALITY_POOL_SOURCE_PROXY
    if "style_turnover_20d" in frame.columns:
        key = "style_turnover_20d"
    elif "turnover_20d" in frame.columns:
        key = "turnover_20d"
    else:  # pragma: no cover - 防御：没有流动性列就无法构造代理池
        raise ValueError(
            "quality_pool_mask 需要流动性列（style_turnover_20d / turnover_20d）或显式 "
            "membership；不允许用一个没有登记规则的池子冒充质量池"
        )
    liquidity = pd.to_numeric(frame[key], errors="coerce")
    ranks = liquidity.groupby(frame["decision_date"]).rank(ascending=False, method="first")
    mask = ranks <= max(1, int(target))
    return mask.fillna(False), QUALITY_POOL_SOURCE_PROXY


def _membership_mask(
    frame: pd.DataFrame, membership: Mapping[str, Sequence[str]] | pd.Series
) -> pd.Series:
    if isinstance(membership, pd.Series):
        aligned = membership.reindex(frame["symbol"].to_numpy())
        return aligned.fillna(False).astype(bool).set_axis(frame.index)
    allowed = {str(key): {str(item) for item in value} for key, value in membership.items()}
    if "decision_date" in frame.columns and any("-" in key for key in allowed):
        return pd.Series(
            [
                str(symbol) in allowed.get(str(day), set())
                for symbol, day in zip(frame["symbol"], frame["decision_date"], strict=True)
            ],
            index=frame.index,
        )
    flat = {item for values in allowed.values() for item in values}
    return frame["symbol"].astype(str).isin(flat)


# ---------------------------------------------------------------------------
# 风格匹配对照（同板块 kNN）
# ---------------------------------------------------------------------------


def style_matched_control(
    frame: pd.DataFrame,
    *,
    dims: Sequence[str] = DEFAULT_STYLE_DIMS,
    horizons: Sequence[int] = HORIZONS,
    k: int = DEFAULT_STYLE_K,
    min_peers: int = STYLE_MIN_PEERS,
    block: int = 512,
) -> pd.DataFrame:
    """同板块内按风格近邻取对照，输出 ``control_return_{h}d`` 与 ``residual_excess_return_{h}d``。

    实现：同 ``decision_date`` + 同板块内把风格维度做 z-score，按欧氏距离取最近
    ``k`` 个**其他**样本，对照收益 = 这些近邻的等权净收益均值。同行不足
    ``min_peers`` 时该行标记 ``style_fallback=True``，由调用方回退到质量池基准。
    """
    columns = [
        "decision_date",
        "symbol",
        "style_peer_count",
        "style_distance_mean",
        "style_fallback",
    ]
    for horizon in horizons:
        key = int(horizon)
        columns.extend([f"control_return_{key}d", f"residual_excess_return_{key}d"])
    if frame.empty:
        return pd.DataFrame(columns=columns)

    usable = frame[frame["executable"].astype(bool)].copy()
    if usable.empty:
        return pd.DataFrame(columns=columns)

    dim_cols = [column for column in dims if column in usable.columns]
    board_col = STYLE_BOARD_DIM if STYLE_BOARD_DIM in usable.columns else None
    usable["__board"] = usable[board_col].astype(str) if board_col else "all"

    pieces: list[pd.DataFrame] = []
    key_cols = ["decision_date", "__board"]
    for _, group in usable.groupby(key_cols, sort=False):
        pieces.append(
            _style_control_group(
                group, dim_cols=dim_cols, horizons=horizons, k=k, min_peers=min_peers, block=block
            )
        )
    matched = pd.concat(pieces, ignore_index=True) if pieces else usable.iloc[0:0].copy()

    result = frame[["decision_date", "symbol"]].copy()
    merged = result.merge(
        matched[
            ["decision_date", "symbol", "style_peer_count", "style_distance_mean", "style_fallback"]
            + [f"control_return_{int(h)}d" for h in horizons]
        ],
        on=["decision_date", "symbol"],
        how="left",
    )
    merged["style_peer_count"] = merged["style_peer_count"].fillna(0).astype(int)
    merged["style_fallback"] = merged["style_fallback"].fillna(True).astype(bool)
    for horizon in horizons:
        key = int(horizon)
        net = pd.to_numeric(frame[f"net_return_{key}d"], errors="coerce").to_numpy()
        control = pd.to_numeric(merged[f"control_return_{key}d"], errors="coerce").to_numpy()
        valid = np.isfinite(net) & np.isfinite(control)
        residual = np.where(valid, net - control, np.nan)
        merged[f"control_return_{key}d"] = [
            NOT_AVAILABLE if not ok else round(float(value), 8)
            for ok, value in zip(valid, control, strict=True)
        ]
        merged[f"residual_excess_return_{key}d"] = [
            NOT_AVAILABLE if not ok else round(float(value), 8)
            for ok, value in zip(valid, residual, strict=True)
        ]
    return merged


def _style_control_group(
    group: pd.DataFrame,
    *,
    dim_cols: Sequence[str],
    horizons: Sequence[int],
    k: int,
    min_peers: int,
    block: int,
) -> pd.DataFrame:
    size = len(group)
    if size < 2:
        # 组内不足两只：没有任何"别的票"可以做对照 —— 全部标 fallback，
        # 绝不拿自己当自己的 peer（那会把残差恒等于 0，凭空造出"无风格暴露"）。
        result = group[["decision_date", "symbol"]].copy()
        result["style_peer_count"] = 0
        result["style_distance_mean"] = float("nan")
        result["style_fallback"] = True
        for horizon in horizons:
            result[f"control_return_{int(horizon)}d"] = float("nan")
        return result
    width = min(max(1, int(k)), size - 1)
    returns = np.column_stack(
        [
            pd.to_numeric(group[f"net_return_{int(h)}d"], errors="coerce").to_numpy(dtype=float)
            for h in horizons
        ]
    )
    if dim_cols:
        matrix = np.column_stack(
            [
                pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=float)
                for column in dim_cols
            ]
        )
    else:
        matrix = np.zeros((size, 0))
    matrix = _standardize(matrix)
    peers = np.full((size, max(1, int(k))), -1, dtype=int)
    distances = np.full((size, max(1, int(k))), np.nan, dtype=float)
    for start in range(0, size, max(1, int(block))):
        stop = min(size, start + max(1, int(block)))
        chunk = matrix[start:stop]
        if matrix.shape[1] == 0:
            dist = np.zeros((chunk.shape[0], size), dtype=float)
        else:
            dist = np.sqrt(
                np.maximum(
                    0.0,
                    (chunk**2).sum(axis=1)[:, None]
                    + (matrix**2).sum(axis=1)[None, :]
                    - 2.0 * chunk @ matrix.T,
                )
            )
        for offset in range(chunk.shape[0]):
            row_index = start + offset
            # 行维是"当前 chunk 内偏移"，列维才是组内全局序号——自配对必须摘出去，
            # 否则 size > block 时 row_index 越界（dist 形状是 chunk_rows × size）。
            dist[offset, row_index] = np.inf
        order = np.argsort(dist, axis=1)[:, :width]
        peers[start:stop, :width] = order
        distances[start:stop, :width] = np.take_along_axis(dist, order, axis=1)
    control = np.full((size, len(horizons)), np.nan, dtype=float)
    counts = np.zeros(size, dtype=int)
    mean_distance = np.full(size, np.nan, dtype=float)
    for offset in range(size):
        selected = peers[offset][peers[offset] >= 0]
        if selected.size == 0:
            continue
        peer_returns = returns[selected]
        counts[offset] = int(selected.size)
        if np.isfinite(peer_returns).any():
            # 某些 horizon 的近邻收益可能全缺（未成熟）→ 该列保持 NaN，
            # 由调用方按"不足 peer"回退；此处只需静默 numpy 的空切片告警。
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                control[offset] = np.nanmean(peer_returns, axis=0)
        finite_distance = distances[offset][np.isfinite(distances[offset])]
        if finite_distance.size:
            mean_distance[offset] = float(finite_distance.mean())
    result = group[["decision_date", "symbol"]].copy()
    result["style_peer_count"] = counts
    result["style_distance_mean"] = mean_distance
    result["style_fallback"] = counts < max(1, int(min_peers))
    for index, horizon in enumerate(horizons):
        result[f"control_return_{int(horizon)}d"] = control[:, index]
    return result


def _standardize(matrix: np.ndarray) -> np.ndarray:
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        return matrix
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        means = np.nanmean(matrix, axis=0)
        stds = np.nanstd(matrix, axis=0)
    stds = np.where((stds > 0) & np.isfinite(stds), stds, 1.0)
    standardized = (matrix - means) / stds
    return np.nan_to_num(standardized, nan=0.0, posinf=0.0, neginf=0.0)


# ---------------------------------------------------------------------------
# 套件组装
# ---------------------------------------------------------------------------


def build_benchmark_suite(
    frame: pd.DataFrame,
    *,
    spec: BenchmarkSpec | None = None,
    quality_membership: Mapping[str, Sequence[str]] | pd.Series | None = None,
    baseline_mask: pd.Series | None = None,
) -> BenchmarkSuite:
    """在 outcome 帧上组装三层（含可选 baseline 层）基准与超额列。"""
    resolved = spec or BenchmarkSpec()
    series: dict[str, pd.DataFrame] = {}
    excess: dict[str, pd.DataFrame] = {}
    report: dict[str, object] = {"quality_pool_source": None, "layers": {}}

    quality_msk, quality_source = quality_pool_mask(
        frame,
        target=resolved.quality_target,
        membership=quality_membership,
    )
    report["quality_pool_source"] = quality_source
    report["quality_pool_size_median"] = (
        float(quality_msk.groupby(frame["decision_date"]).sum().median())
        if not frame.empty
        else 0.0
    )

    for layer in resolved.layers:
        if layer == BENCHMARK_ELIGIBLE:
            mask = eligible_pool_mask(frame)
        elif layer == BENCHMARK_QUALITY_POOL:
            mask = quality_msk
        elif layer == BENCHMARK_STYLE_MATCHED:
            matched = style_matched_control(
                frame,
                dims=resolved.style_dims,
                horizons=resolved.horizons,
                k=resolved.style_k,
                min_peers=resolved.style_min_peers,
            )
            excess[layer] = matched
            report["layers"][layer] = {
                "definition": "same_board_knn_standardized_style_dims",
                "fallback_rows": int(matched["style_fallback"].sum()) if not matched.empty else 0,
                "median_peers": (
                    float(matched["style_peer_count"].median()) if not matched.empty else 0.0
                ),
                "dimensions_used": list(resolved.style_dims),
            }
            continue
        elif layer == BENCHMARK_BASELINE:
            if baseline_mask is None:
                continue
            mask = baseline_mask.reindex(frame.index).fillna(False).astype(bool)
        else:
            raise ValueError(f"unsupported benchmark layer: {layer}")
        bench = benchmark_series_from_outcomes(
            frame, horizons=resolved.horizons, pool_mask=mask, name=layer
        )
        series[layer] = bench
        attached = attach_excess_returns(
            frame[
                ["decision_date", "symbol", *[f"net_return_{int(h)}d" for h in resolved.horizons]]
            ],
            benchmark=bench,
            horizons=resolved.horizons,
            short_horizons=(),
            name=layer,
        )
        excess[layer] = attached
        report["layers"][layer] = {
            "definition": _layer_definition(layer),
            "median_pool_size": float(bench["pool_size"].median()) if not bench.empty else 0.0,
            "date_horizon_rows": int(len(bench)),
        }

    return BenchmarkSuite(spec=resolved, series=series, excess=excess, report=report)


def _layer_definition(layer: str) -> str:
    if layer == BENCHMARK_ELIGIBLE:
        return "equal_weight_mean_of_pit_eligible_pool"
    if layer == BENCHMARK_QUALITY_POOL:
        return "equal_weight_mean_of_quality_pool"
    if layer == BENCHMARK_BASELINE:
        return "equal_weight_mean_of_simple_baseline_pool"
    return "unknown"


def merge_primary_excess(
    frame: pd.DataFrame,
    suite: BenchmarkSuite,
    *,
    horizons: Sequence[int] | None = None,
    short_horizons: Sequence[int] = (3, 5),
) -> pd.DataFrame:
    """把主基准层的超额列写回 outcome 帧（下游只用一套规范列名）。"""
    resolved_horizons = tuple(horizons or suite.spec.horizons)
    layer = suite.primary
    source = suite.excess.get(layer)
    merged = frame.copy()
    if source is None or source.empty:
        for horizon in resolved_horizons:
            key = int(horizon)
            merged[f"benchmark_return_{key}d"] = NOT_AVAILABLE
            merged[f"excess_return_{key}d"] = NOT_AVAILABLE
            if key in short_horizons:
                merged[f"up_excess_{key}d"] = NOT_AVAILABLE
        merged["benchmark_name"] = NOT_AVAILABLE
        return merged
    columns = ["decision_date", "symbol"]
    rename: dict[str, str] = {}
    if layer == BENCHMARK_STYLE_MATCHED:
        for horizon in resolved_horizons:
            key = int(horizon)
            columns.append(f"control_return_{key}d")
            rename[f"control_return_{key}d"] = f"benchmark_return_{key}d"
            columns.append(f"residual_excess_return_{key}d")
            rename[f"residual_excess_return_{key}d"] = f"excess_return_{key}d"
        columns.extend(["style_peer_count", "style_fallback"])
    else:
        for horizon in resolved_horizons:
            key = int(horizon)
            columns.append(f"benchmark_return_{key}d")
            columns.append(f"excess_return_{key}d")
    selected = source[columns].rename(columns=rename)
    # 先清掉同名旧列再合并：否则 pandas 会给两边加 _x/_y 后缀，随后按原名取列会 KeyError
    # （2026-09-18 实测：build_label_v2 已经写过 benchmark_return_*d/excess_return_*d）。
    drop_prefixes = ("excess_return_", "benchmark_return_", "up_excess_")
    merged = merged.drop(
        columns=[
            column
            for column in merged.columns
            if any(str(column).startswith(prefix) for prefix in drop_prefixes)
        ],
        errors="ignore",
    )
    merged = merged.merge(selected, on=["decision_date", "symbol"], how="left")
    for horizon in resolved_horizons:
        key = int(horizon)
        # merge 未命中的行（基准池当天空）：明确 not_available，不用 NaN 冒充
        merged[f"excess_return_{key}d"] = merged[f"excess_return_{key}d"].where(
            merged[f"excess_return_{key}d"].notna(), NOT_AVAILABLE
        )
        merged[f"benchmark_return_{key}d"] = merged[f"benchmark_return_{key}d"].where(
            merged[f"benchmark_return_{key}d"].notna(), NOT_AVAILABLE
        )
        if key in short_horizons:
            merged[f"up_excess_{key}d"] = [
                NOT_AVAILABLE if value == NOT_AVAILABLE else bool(float(value) > 0)
                for value in merged[f"excess_return_{key}d"]
            ]
    merged["benchmark_name"] = layer
    return merged


__all__ = [
    "BENCHMARK_BASELINE",
    "BENCHMARK_ELIGIBLE",
    "BENCHMARK_QUALITY_POOL",
    "BENCHMARK_STYLE_MATCHED",
    "BenchmarkSpec",
    "BenchmarkSuite",
    "DEFAULT_LAYERS",
    "DEFAULT_STYLE_DIMS",
    "PRIMARY_LAYER",
    "QUALITY_POOL_RULESET_PROXY",
    "QUALITY_POOL_SOURCE_PRODUCTION",
    "QUALITY_POOL_SOURCE_PROXY",
    "STYLE_BOARD_DIM",
    "STYLE_DIM_FLOAT_CAP",
    "STYLE_DIM_MOMENTUM",
    "STYLE_DIM_TURNOVER",
    "STYLE_DIM_VOL",
    "build_benchmark_suite",
    "compute_style_features",
    "eligible_pool_mask",
    "merge_primary_excess",
    "quality_pool_mask",
    "style_matched_control",
]
