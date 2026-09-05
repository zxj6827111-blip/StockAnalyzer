"""Phase 1 时间安全的候选打分评估（方案 §3）。

职责边界：
- 只做打分层评估（IC/Spearman、AUC/Brier、分位收益、Precision@K、单调性、
  universe 覆盖率），不要求 final_count>0，不做组合 NAV，不做生产放行判断。
- 时间安全规则（方案 §3 硬口径）：

      max(train_sample.label_mature_trade_date) + embargo_trading_days < evaluation_trade_date

  以交易日历计（禁用自然日相加），earliest_valid_eval_date 取成熟截止后的
  第 embargo 个交易日。

- ``lookahead_bias`` 为计算值：评估日必须晚于 earliest_valid_eval_date 且
  被评分快照的 decision_time 与评估日一致，否则判 invalid。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime

import numpy as np


def _parse_dt(raw: str | datetime | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class ManifestEligibility:
    """单个 manifest 的时间安全资格判定结果。"""

    dataset_manifest_id: str
    item_count: int
    training_cutoff: datetime | None
    max_label_mature_time: datetime | None
    embargo_trading_days: int
    earliest_valid_eval_date: date | None
    missing_outcome_items: int = 0
    invalid_reason: str = ""

    @property
    def is_eligible(self) -> bool:
        return not self.invalid_reason and self.earliest_valid_eval_date is not None


def resolve_manifest_eligibility(
    *,
    dataset_manifest_id: str,
    item_decision_times: list[str],
    item_label_mature_times: list[str | None],
    missing_outcome_items: int,
    embargo_trading_days: int,
    trading_dates: list[date],
) -> ManifestEligibility:
    """按方案 §3 口径判定 manifest 的最早可评估交易日。

    - trading_dates 必须升序去重（交易日历，来自 market.duckdb daily_bars）；
    - 成熟截止取 manifest 内 outcome 的 max(label_mature_time)；
    - 缺 outcome 的 manifest 直接判 invalid（manifest 完整性缺陷，如实记录
      而不是静默剔除样本）。
    """

    base = ManifestEligibility(
        dataset_manifest_id=dataset_manifest_id,
        item_count=len(item_decision_times),
        training_cutoff=max(
            (t for t in (_parse_dt(v) for v in item_decision_times) if t is not None),
            default=None,
        ),
        max_label_mature_time=max(
            (t for t in (_parse_dt(v) for v in item_label_mature_times) if t is not None),
            default=None,
        ),
        embargo_trading_days=max(0, int(embargo_trading_days)),
        earliest_valid_eval_date=None,
        missing_outcome_items=int(missing_outcome_items),
    )
    if base.item_count == 0:
        return replace(base, invalid_reason="empty_manifest")
    if base.missing_outcome_items > 0:
        return replace(base, invalid_reason=f"outcome_missing_items:{base.missing_outcome_items}")
    if base.max_label_mature_time is None or base.training_cutoff is None:
        return replace(base, invalid_reason="no_mature_time")
    mature_date = base.max_label_mature_time.date()
    # 成熟截止之后的第一个交易日起算 embargo 个交易日。
    idx_after = bisect_right(trading_dates, mature_date)
    earliest_idx = idx_after + base.embargo_trading_days
    if earliest_idx >= len(trading_dates):
        return replace(base, invalid_reason="no_trading_day_after_embargo")
    return replace(base, earliest_valid_eval_date=trading_dates[earliest_idx])


# ---------------------------------------------------------------- 打分层指标


def _rankdata(values: np.ndarray) -> np.ndarray:
    """平均秩（ties 取平均），用于 Spearman。"""

    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.shape[0], dtype=float)
    sorted_values = values[order]
    i = 0
    n = values.shape[0]
    while i < n:
        j = i
        while j + 1 < n and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0
        ranks[order[i : j + 1]] = average_rank
        i = j + 1
    return ranks


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if x.shape[0] < 2:
        return float("nan")
    xc = x - x.mean()
    yc = y - y.mean()
    denom = math.sqrt(float((xc * xc).sum()) * float((yc * yc).sum()))
    if denom <= 0.0:
        return float("nan")
    return float((xc * yc).sum() / denom)


def compute_rank_ic(scores: np.ndarray, returns: np.ndarray) -> dict[str, float]:
    """Spearman（秩 IC）与 Pearson IC；样本 <2 或零方差返回 NaN。"""

    scores = np.asarray(scores, dtype=float)
    returns = np.asarray(returns, dtype=float)
    mask = ~(np.isnan(scores) | np.isnan(returns))
    scores, returns = scores[mask], returns[mask]
    if scores.shape[0] < 2:
        return {"ic_spearman": float("nan"), "ic_pearson": float("nan"), "n": int(scores.shape[0])}
    return {
        "ic_spearman": _pearson(_rankdata(scores), _rankdata(returns)),
        "ic_pearson": _pearson(scores, returns),
        "n": int(scores.shape[0]),
    }


def compute_quantile_returns(
    scores: np.ndarray,
    returns: np.ndarray,
    *,
    n_quantiles: int = 5,
) -> dict[str, object]:
    """按分数分位的收益统计（横截面当日）：各分位均值与 top-bottom 价差。"""

    scores = np.asarray(scores, dtype=float)
    returns = np.asarray(returns, dtype=float)
    mask = ~(np.isnan(scores) | np.isnan(returns))
    scores, returns = scores[mask], returns[mask]
    n = scores.shape[0]
    if n < n_quantiles:
        return {
            "n": int(n),
            "quantile_means": [],
            "top_minus_bottom": float("nan"),
            "monotonic_top_ge_bottom": False,
        }
    order = np.argsort(scores, kind="mergesort")
    sorted_returns = returns[order]
    buckets = np.array_split(sorted_returns, n_quantiles)
    quantile_means = [float(np.mean(bucket)) if bucket.size else float("nan") for bucket in buckets]
    top = quantile_means[-1]
    bottom = quantile_means[0]
    spread = (
        top - bottom
        if not (math.isnan(top) or math.isnan(bottom))
        else float("nan")
    )
    return {
        "n": int(n),
        "quantile_means": quantile_means,
        "top_minus_bottom": spread,
        "monotonic_top_ge_bottom": bool(
            not math.isnan(spread) and spread >= 0.0
        ),
    }


def compute_auc_brier(probabilities: np.ndarray, labels: np.ndarray) -> dict[str, float]:
    """AUC（秩实现，无需 scipy）与 Brier；正/负类缺失时 AUC 返回 NaN。"""

    probabilities = np.asarray(probabilities, dtype=float)
    labels = np.asarray(labels, dtype=float)
    mask = ~(np.isnan(probabilities) | np.isnan(labels))
    probabilities, labels = probabilities[mask], labels[mask]
    n = labels.shape[0]
    positives = int(np.count_nonzero(labels == 1.0))
    negatives = n - positives
    if positives == 0 or negatives == 0:
        return {"auc": float("nan"), "brier": float("nan"), "n": int(n)}
    ranks = _rankdata(probabilities)
    rank_sum_pos = float(ranks[labels == 1.0].sum())
    auc = (rank_sum_pos - positives * (positives + 1) / 2.0) / (positives * negatives)
    brier = float(np.mean((probabilities - labels) ** 2))
    return {"auc": float(auc), "brier": brier, "n": int(n)}


def compute_precision_at_k(
    scores: np.ndarray,
    labels: np.ndarray,
    *,
    k: int = 5,
) -> dict[str, float]:
    """Top-K 命中率：分数最高的 K 个样本中正标签占比（K 超过样本数取全体）。"""

    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=float)
    mask = ~(np.isnan(scores) | np.isnan(labels))
    scores, labels = scores[mask], labels[mask]
    n = labels.shape[0]
    if n == 0:
        return {"precision_at_k": float("nan"), "k": 0, "n": 0}
    effective_k = min(max(1, int(k)), n)
    order = np.argsort(scores, kind="mergesort")[::-1][:effective_k]
    hits = float(np.count_nonzero(labels[order] == 1.0))
    return {
        "precision_at_k": hits / effective_k,
        "k": int(effective_k),
        "n": int(n),
    }


def date_block_bootstrap_ci(
    daily_values: list[tuple[date, float]],
    *,
    n_boot: int = 1000,
    seed: int = 20260905,
    confidence: float = 0.95,
) -> dict[str, float]:
    """以交易日为 block 的 bootstrap 置信区间（方案 Phase 2 口径，Phase 1 先行输出）。

    输入为逐日指标（如日 IC）；按日重采样后取均值分布的分位数。
    有效日 < 2 时返回全 NaN。
    """

    values = [float(v) for _, v in daily_values if not math.isnan(float(v))]
    if len(values) < 2:
        return {"ci_low": float("nan"), "ci_high": float("nan"), "valid_days": len(values)}
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    means = np.empty(int(n_boot), dtype=float)
    for i in range(int(n_boot)):
        sample = arr[rng.integers(0, arr.shape[0], arr.shape[0])]
        means[i] = sample.mean()
    alpha = (1.0 - confidence) / 2.0
    return {
        "ci_low": float(np.quantile(means, alpha)),
        "ci_high": float(np.quantile(means, 1.0 - alpha)),
        "valid_days": len(values),
    }


def check_lookahead(
    *,
    eligibility: ManifestEligibility,
    eval_date: date,
    snapshot_decision_dates: list[date],
) -> dict[str, object]:
    """lookahead_bias 计算值：资格门 + 快照 decision_time 与评估日一致。"""

    reasons: list[str] = []
    if eligibility.earliest_valid_eval_date is None or not eligibility.is_eligible:
        reasons.append("model_not_eligible")
    elif eligibility.earliest_valid_eval_date > eval_date:
        reasons.append(
            f"eval_before_earliest_valid:{eligibility.earliest_valid_eval_date.isoformat()}"
        )
    for decision_date in snapshot_decision_dates:
        if decision_date > eval_date:
            reasons.append(f"snapshot_after_eval:{decision_date.isoformat()}")
            break
    return {
        "lookahead_bias": bool(reasons),
        "lookahead_reasons": reasons,
        "training_cutoff": (
            eligibility.training_cutoff.isoformat() if eligibility.training_cutoff else ""
        ),
        "max_label_mature_time": (
            eligibility.max_label_mature_time.isoformat()
            if eligibility.max_label_mature_time
            else ""
        ),
        "embargo_trading_days": eligibility.embargo_trading_days,
        "earliest_valid_eval_date": (
            eligibility.earliest_valid_eval_date.isoformat()
            if eligibility.earliest_valid_eval_date
            else ""
        ),
    }
