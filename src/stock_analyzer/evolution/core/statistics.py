"""Statistical tools for evolution validation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

_TRADING_DAYS_PER_YEAR = 252.0
_DEFAULT_FDR_ALPHA = 0.10


@dataclass(frozen=True, slots=True)
class BootstrapTestResult:
    """Result of a block bootstrap mean test."""

    observed_mean: float
    p_value: float
    confidence_interval: tuple[float, float]
    block_size: int
    n_resamples: int
    n_samples: int


@dataclass(frozen=True, slots=True)
class FDRCorrectionResult:
    """Result of Benjamini-Hochberg correction."""

    adjusted_p_values: list[float]
    rejected: list[bool]
    alpha: float = _DEFAULT_FDR_ALPHA


def bootstrap_test(
    data: Sequence[float],
    n_resamples: int = 1000,
    block_size: int | None = None,
) -> BootstrapTestResult:
    """Run a one-sided block bootstrap test for mean(data) > 0.

    The p-value is estimated under the null hypothesis by centering the sample
    around zero and re-sampling with a circular moving-block bootstrap.

    Args:
        data: One-dimensional return or excess-return series.
        n_resamples: Number of bootstrap resamples.
        block_size: Optional fixed block size. If omitted, adaptive block size
            is used: ``max(5, sqrt(n_samples))`` with a safety cap at
            ``n_samples``.

    Returns:
        A :class:`BootstrapTestResult` containing p-value and confidence interval.

    Raises:
        ValueError: If input data or arguments are invalid.
    """
    if n_resamples <= 0:
        raise ValueError("n_resamples must be > 0")

    sample = _to_valid_sample(data)
    n_samples = int(sample.size)
    chosen_block_size = _resolve_block_size(n_samples=n_samples, block_size=block_size)

    rng = np.random.default_rng()
    observed_mean = float(np.mean(sample))
    bootstrap_means = _block_bootstrap_means(
        sample=sample,
        n_resamples=n_resamples,
        block_size=chosen_block_size,
        rng=rng,
    )

    centered = sample - observed_mean
    null_means = _block_bootstrap_means(
        sample=centered,
        n_resamples=n_resamples,
        block_size=chosen_block_size,
        rng=rng,
    )
    tail_count = int(np.sum(null_means >= observed_mean))
    p_value = float((tail_count + 1) / (n_resamples + 1))

    alpha = 0.05
    ci_low, ci_high = np.quantile(bootstrap_means, [alpha / 2.0, 1.0 - alpha / 2.0])

    return BootstrapTestResult(
        observed_mean=observed_mean,
        p_value=p_value,
        confidence_interval=(float(ci_low), float(ci_high)),
        block_size=chosen_block_size,
        n_resamples=n_resamples,
        n_samples=n_samples,
    )


def fdr_correct(p_values: Sequence[float], method: str = "bh") -> FDRCorrectionResult:
    """Apply module-local FDR correction.

    Args:
        p_values: Raw p-values from hypotheses within one module.
        method: Correction method. Only ``"bh"`` is supported.

    Returns:
        Adjusted p-values and rejection flags at alpha=0.10.

    Raises:
        ValueError: If inputs are invalid.
    """
    normalized_method = method.strip().lower()
    if normalized_method != "bh":
        raise ValueError("only Benjamini-Hochberg ('bh') is supported")

    raw = list(p_values)
    if not raw:
        return FDRCorrectionResult(adjusted_p_values=[], rejected=[])
    if any((value < 0.0 or value > 1.0) for value in raw):
        raise ValueError("p_values must be in [0, 1]")

    arr = np.asarray(raw, dtype=float)
    m = int(arr.size)
    order = np.argsort(arr)
    sorted_p = arr[order]

    adjusted_sorted: NDArray[np.float64] = np.empty(m, dtype=float)
    running_min = 1.0
    for idx in range(m - 1, -1, -1):
        rank = idx + 1
        scaled = float(sorted_p[idx] * m / rank)
        running_min = min(running_min, scaled)
        adjusted_sorted[idx] = min(1.0, running_min)

    adjusted: NDArray[np.float64] = np.empty(m, dtype=float)
    adjusted[order] = adjusted_sorted
    adjusted_values = [float(value) for value in adjusted]
    rejected = [value <= _DEFAULT_FDR_ALPHA for value in adjusted_values]

    return FDRCorrectionResult(
        adjusted_p_values=adjusted_values,
        rejected=rejected,
        alpha=_DEFAULT_FDR_ALPHA,
    )


def information_ratio(
    challenger_after_cost: Sequence[float],
    champion_after_cost: Sequence[float],
) -> float:
    """Compute annualized Information Ratio for challenger vs champion.

    Formula:
    ``IR = mean(excess) / max(std(excess), 1e-6) * sqrt(252)``, where
    ``excess = challenger_after_cost - champion_after_cost``.

    Args:
        challenger_after_cost: Challenger return series.
        champion_after_cost: Champion return series.

    Returns:
        Annualized information ratio.

    Raises:
        ValueError: If sequences are empty or lengths differ.
    """
    challenger = _to_valid_sample(challenger_after_cost)
    champion = _to_valid_sample(champion_after_cost)
    if challenger.size != champion.size:
        raise ValueError("challenger_after_cost and champion_after_cost must have equal length")

    excess = challenger - champion
    excess_mean = float(np.mean(excess))
    excess_std = float(np.std(excess))
    return excess_mean / max(excess_std, 1e-6) * math.sqrt(_TRADING_DAYS_PER_YEAR)


def _resolve_block_size(n_samples: int, block_size: int | None) -> int:
    if n_samples <= 0:
        raise ValueError("n_samples must be > 0")
    if block_size is not None:
        if block_size <= 0:
            raise ValueError("block_size must be > 0")
        return min(n_samples, block_size)
    adaptive = max(5, int(math.sqrt(n_samples)))
    return min(n_samples, adaptive)


def _to_valid_sample(data: Sequence[float]) -> NDArray[np.float64]:
    sample = np.asarray(list(data), dtype=float)
    if sample.ndim != 1:
        raise ValueError("data must be one-dimensional")
    if sample.size == 0:
        raise ValueError("data must not be empty")
    if not np.isfinite(sample).all():
        raise ValueError("data must contain only finite numbers")
    return sample


def _block_bootstrap_means(
    sample: NDArray[np.float64],
    n_resamples: int,
    block_size: int,
    rng: np.random.Generator,
) -> NDArray[np.float64]:
    n_samples = int(sample.size)
    means: NDArray[np.float64] = np.empty(n_resamples, dtype=float)
    block_offsets: NDArray[np.int_] = np.arange(block_size, dtype=int)

    for index in range(n_resamples):
        collected: list[int] = []
        while len(collected) < n_samples:
            start = int(rng.integers(0, n_samples))
            block: list[int] = ((start + block_offsets) % n_samples).tolist()
            collected.extend(block)
        resample_idx = np.asarray(collected[:n_samples], dtype=int)
        means[index] = float(np.mean(sample[resample_idx]))
    return means
