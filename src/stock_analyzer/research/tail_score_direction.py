"""开发阶段 raw-score 高分端 / 低分端同池 Top-K 诊断。

此处比较的是未校准模型排序，不是可上线概率，也不修改模型的正向校准硬门。
每个交易日两端都从同一份可评分股票池选取，结果只用于开发假设检查。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from stock_analyzer.research.tail_ablation import holm_adjusted_p_values

DEFAULT_DRAWS = 20_000
DEFAULT_SEED = 20261010
DEFAULT_BLOCK_LENGTHS = (10, 5, 20)


def select_directional_top_k(
    rows: Sequence[Mapping[str, Any]],
    scores: Mapping[str, float],
    *,
    cap: int = 3,
) -> dict[str, list[str]]:
    """在同一候选池里按分数升序 / 降序选股；同分均按 symbol 升序。"""
    if cap < 0:
        raise ValueError("cap must be non-negative")
    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("scored pool contains a row without symbol")
        if symbol in seen:
            raise ValueError(f"scored pool contains duplicate symbol: {symbol}")
        seen.add(symbol)
        if symbol not in scores:
            raise ValueError(f"score missing for symbol: {symbol}")
        value = float(scores[symbol])
        if not math.isfinite(value):
            raise ValueError(f"score is not finite for symbol: {symbol}")
        symbols.append(symbol)
    high = sorted(symbols, key=lambda symbol: (-float(scores[symbol]), symbol))[:cap]
    low = sorted(symbols, key=lambda symbol: (float(scores[symbol]), symbol))[:cap]
    return {"high": high, "low": low}


def summarize_picks(
    pool: Sequence[Mapping[str, Any]],
    symbols: Sequence[str],
) -> dict[str, Any]:
    """对已选名单计算成熟成交结果；未成熟项保留在推荐数中但不补选。"""
    by_symbol = {str(row.get("symbol") or "").strip(): row for row in pool}
    if len(by_symbol) != len(pool):
        raise ValueError("pool symbols must be unique and non-empty")
    if any(symbol not in by_symbol for symbol in symbols):
        raise ValueError("selected symbol is outside the scored pool")
    selected = [by_symbol[symbol] for symbol in symbols]
    returns = [
        float(row["net_return"])
        for row in selected
        if row.get("trainable") and row.get("net_return") is not None
    ]
    if any(not math.isfinite(value) for value in returns):
        raise ValueError("matured net return is not finite")
    return {
        "symbols": list(symbols),
        "recommendations": len(selected),
        "fills": sum(1 for row in selected if row.get("filled")),
        "matured_fills": len(returns),
        "net_profits": sum(1 for value in returns if value > 0.0),
        "net_return_sum": float(sum(returns)),
        "net_returns": returns,
        "net_profit_rate": (
            sum(1 for value in returns if value > 0.0) / len(returns) if returns else None
        ),
        "mean_net_return": float(np.mean(returns)) if returns else None,
    }


def _resample_index_matrix(
    daily: Sequence[Mapping[str, Any]],
    *,
    block_length: int,
    draws: int,
    seed: int,
) -> tuple[np.ndarray | None, list[int]]:
    if block_length < 1 or draws < 1:
        raise ValueError("block_length and draws must be positive")
    ordered = sorted(
        daily,
        key=lambda row: (int(row["fold_id"]), int(row["session_index"])),
    )
    if any(row.get("fold_id") is None or row.get("session_index") is None for row in ordered):
        return None, []
    if len({(row["fold_id"], row["session_index"]) for row in ordered}) != len(ordered):
        raise ValueError("duplicate fold/session identity in directional observations")

    segments: list[list[int]] = []
    for index, row in enumerate(ordered):
        if not segments:
            segments.append([index])
            continue
        previous = ordered[segments[-1][-1]]
        if (
            row["fold_id"] == previous["fold_id"]
            and int(row["session_index"]) == int(previous["session_index"]) + 1
        ):
            segments[-1].append(index)
        else:
            segments.append([index])
    lengths = [len(segment) for segment in segments]
    if any(length < block_length for length in lengths):
        return None, lengths

    rng = np.random.Generator(np.random.PCG64(seed))
    pieces: list[np.ndarray] = []
    offsets: NDArray[np.int64] = np.arange(block_length, dtype=np.int64)
    for segment in segments:
        count = len(segment)
        blocks_per_draw = int(math.ceil(count / block_length))
        starts = rng.integers(
            0,
            count - block_length + 1,
            size=(draws, blocks_per_draw),
            endpoint=False,
        )
        local = (starts[:, :, None] + offsets[None, None, :]).reshape(draws, -1)[:, :count]
        pieces.append(np.asarray(segment, dtype=np.int64)[local])
    return np.concatenate(pieces, axis=1), lengths


def _aggregate_metric(
    daily: Sequence[Mapping[str, Any]],
    indices: np.ndarray,
    *,
    arm: str,
    metric: str,
) -> NDArray[np.float64]:
    field = "high" if arm == "high" else "low"
    if metric == "net_profit_rate":
        wins = np.asarray([row[field]["net_profits"] for row in daily], dtype=float)
        fills = np.asarray([row[field]["matured_fills"] for row in daily], dtype=float)
        numerator = wins[indices].sum(axis=1)
        denominator = fills[indices].sum(axis=1)
    elif metric == "mean_net_return":
        totals = np.asarray([row[field]["net_return_sum"] for row in daily], dtype=float)
        fills = np.asarray([row[field]["matured_fills"] for row in daily], dtype=float)
        numerator = totals[indices].sum(axis=1)
        denominator = fills[indices].sum(axis=1)
    else:
        raise ValueError(f"unsupported directional metric: {metric}")
    result = np.divide(
            numerator,
            denominator,
            out=np.full(numerator.shape, np.nan, dtype=float),
            where=denominator > 0,
    )
    return cast(NDArray[np.float64], result)


def bootstrap_direction_difference(
    daily: Sequence[Mapping[str, Any]],
    *,
    metric: str,
    block_length: int = 10,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """按折及连续交易日分块，估计 low-score 减 high-score 的差。"""
    if not daily:
        return {"status": "blocked", "blockers": ["no_test_sessions"]}
    if any(
        row[arm]["matured_fills"] < 0 or row[arm]["net_profits"] < 0
        for row in daily for arm in ("high", "low")
    ):
        raise ValueError("directional counts must be non-negative")
    sorted_daily = sorted(
        daily,
        key=lambda row: (int(row["fold_id"]), int(row["session_index"])),
    )
    indices, segment_lengths = _resample_index_matrix(
        sorted_daily,
        block_length=block_length,
        draws=draws,
        seed=seed,
    )
    if indices is None:
        return {
            "status": "blocked",
            "blockers": [f"contiguous_segment_shorter_than_block_{block_length}"],
            "segment_lengths": segment_lengths,
        }
    high_values = _aggregate_metric(sorted_daily, indices, arm="high", metric=metric)
    low_values = _aggregate_metric(sorted_daily, indices, arm="low", metric=metric)
    differences = low_values - high_values
    if not np.isfinite(differences).all():
        return {
            "status": "blocked",
            "blockers": ["bootstrap_replicate_has_no_matured_fills_in_an_arm"],
            "draws_requested": int(draws),
            "segment_lengths": segment_lengths,
        }
    ci_low, ci_high = np.quantile(differences, [0.025, 0.975], method="linear")
    point_high = _point_metric(sorted_daily, arm="high", metric=metric)
    point_low = _point_metric(sorted_daily, arm="low", metric=metric)
    p_value = (1 + int(np.sum(differences <= 0.0))) / (len(differences) + 1)
    return {
        "status": "completed",
        "metric": metric,
        "estimand": "pooled_matured_picks_low_score_minus_high_score",
        "effect_estimate": point_low - point_high,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "p_one_sided_low_gt_high": float(p_value),
        "draws": int(len(differences)),
        "seed": int(seed),
        "bit_generator": "PCG64",
        "block_length_sessions": int(block_length),
        "segment_lengths": segment_lengths,
    }


def _point_metric(
    daily: Sequence[Mapping[str, Any]],
    *,
    arm: str,
    metric: str,
) -> float:
    field = "high" if arm == "high" else "low"
    fills = sum(int(row[field]["matured_fills"]) for row in daily)
    if fills == 0:
        return float("nan")
    if metric == "net_profit_rate":
        return sum(int(row[field]["net_profits"]) for row in daily) / fills
    if metric == "mean_net_return":
        return sum(float(row[field]["net_return_sum"]) for row in daily) / fills
    raise ValueError(f"unsupported directional metric: {metric}")


def compare_score_directions(
    daily: Sequence[Mapping[str, Any]],
    *,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
    block_lengths: Sequence[int] = DEFAULT_BLOCK_LENGTHS,
) -> dict[str, Any]:
    """报告净盈利率和平均单笔净收益；主块长 10，5/20 日为敏感性。"""
    totals: dict[str, dict[str, Any]] = {}
    for arm in ("high", "low"):
        fills = sum(int(row[arm]["matured_fills"]) for row in daily)
        wins = sum(int(row[arm]["net_profits"]) for row in daily)
        net_return = sum(float(row[arm]["net_return_sum"]) for row in daily)
        totals[arm] = {
            "recommendations": sum(int(row[arm]["recommendations"]) for row in daily),
            "fills": sum(int(row[arm]["fills"]) for row in daily),
            "matured_fills": fills,
            "net_profits": wins,
            "net_profit_rate": wins / fills if fills else None,
            "mean_net_return": net_return / fills if fills else None,
        }
    metrics: dict[str, Any] = {}
    for metric in ("net_profit_rate", "mean_net_return"):
        block_reports = {
            str(length): bootstrap_direction_difference(
                daily,
                metric=metric,
                block_length=int(length),
                draws=draws,
                seed=seed,
            )
            for length in block_lengths
        }
        metrics[metric] = {
            "point_difference_low_minus_high": (
                (totals["low"][metric] - totals["high"][metric])
                if totals["low"][metric] is not None and totals["high"][metric] is not None
                else None
            ),
            "bootstrap": block_reports,
            "holm_p_value": None,
        }
        main = block_reports.get("10", {})
        if main.get("status") == "completed":
            metrics[metric]["raw_p_one_sided_low_gt_high"] = main[
                "p_one_sided_low_gt_high"
            ]
        else:
            metrics[metric]["raw_p_one_sided_low_gt_high"] = None
    p_values = {
        name: value["raw_p_one_sided_low_gt_high"]
        for name, value in metrics.items()
    }
    adjusted = holm_adjusted_p_values(p_values, family_size=2)
    for name in metrics:
        metrics[name]["holm_p_value"] = adjusted[name]
    return {
        "status": "completed" if daily else "blocked",
        "daily_sessions": len(daily),
        "high_score": totals["high"],
        "low_score": totals["low"],
        "metrics": metrics,
        "holm": {"method": "step_down", "family_size": 2},
        "release_eligible": False,
    }


__all__ = [
    "DEFAULT_BLOCK_LENGTHS",
    "DEFAULT_DRAWS",
    "DEFAULT_SEED",
    "bootstrap_direction_difference",
    "compare_score_directions",
    "select_directional_top_k",
    "summarize_picks",
]
