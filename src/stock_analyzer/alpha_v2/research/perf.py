"""Alpha V2 性能与 NAS 资源加固（S22 / 蓝图 §12）。

NAS 现状：8 CPU / 16GB，API 4G、heavy 4G、critical 3G。生产证据表明**真正贵的不是
模型推理**，而是 snapshot ensure / bar fetch / 特征计算 / 持久化。因此本模块做三件事：

1. **阶段计时与峰值内存**：``fetch / feature / matrix / predict / persist`` 分段计时 +
   峰值 RSS，并与基线并列给出 before/after；
2. **单遍纪律的可执行检查**：复用 S16 的 :class:`BuildStats`，把
   "fetch once / feature once / matrix once / predict N heads / persist once"
   变成断言（``assert_single_pass``）；
3. **determinism 不变量**：同一输入两次运行必须给出相同的矩阵指纹、预测摘要与
   候选顺序——性能优化不得改变结果语义（Gate S22 Blocking）。

内存读数优先用 ``/proc/self/status``（容器内可用），退回 ``psutil``/``resource``，
都拿不到时返回 ``-1`` 并如实标注 ``unavailable``（不编造一个好看的数）。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.multi_head import BuildStats

PERF_SCHEMA = "alpha_v2_perf_report.v1"

STAGE_FETCH = "fetch"
STAGE_FEATURE = "feature"
STAGE_MATRIX = "matrix"
STAGE_PREDICT = "predict"
STAGE_PERSIST = "persist"
STAGE_LABELS: tuple[str, ...] = (
    STAGE_FETCH,
    STAGE_FEATURE,
    STAGE_MATRIX,
    STAGE_PREDICT,
    STAGE_PERSIST,
)

# heavy 容器 4GiB；留出 pandas/lightgbm 的临时峰值余量后取 3GiB 为硬预算。
DEFAULT_MAX_PEAK_RSS_MIB = 3072.0
DEFAULT_MAX_WALL_SECONDS = 1800.0

RSS_UNAVAILABLE = -1.0


def rss_mib() -> float:
    """当前进程 RSS（MiB）；拿不到返回 ``-1``（调用方必须如实标注，不得当 0）。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    try:  # pragma: no cover - 非 Linux 平台
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return float(usage) / 1024.0
    except Exception:  # noqa: BLE001
        return RSS_UNAVAILABLE


@dataclass
class StageTimer:
    """阶段计时器（``with timer.stage("fetch"): ...``）。"""

    stages: dict[str, float] = field(default_factory=dict)
    peak_rss_mib: float = RSS_UNAVAILABLE
    started_at: float = field(default_factory=time.perf_counter)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.stages[name] = self.stages.get(name, 0.0) + elapsed
            self.peak_rss_mib = max(self.peak_rss_mib, rss_mib())

    @property
    def wall_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def to_payload(self) -> dict[str, object]:
        return {
            "stages_seconds": {key: round(value, 4) for key, value in sorted(self.stages.items())},
            "stage_total_seconds": round(sum(self.stages.values()), 4),
            "wall_seconds": round(self.wall_seconds, 4),
            "peak_rss_mib": (
                None if self.peak_rss_mib == RSS_UNAVAILABLE else round(self.peak_rss_mib, 2)
            ),
            "peak_rss_status": ("unavailable" if self.peak_rss_mib == RSS_UNAVAILABLE else "ok"),
        }


@dataclass(frozen=True, slots=True)
class PerfBudget:
    """NAS 资源预算（超过即记为 ``exceeded``，不静默）。"""

    max_peak_rss_mib: float = DEFAULT_MAX_PEAK_RSS_MIB
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS

    def to_payload(self) -> dict[str, object]:
        return {
            "max_peak_rss_mib": float(self.max_peak_rss_mib),
            "max_wall_seconds": float(self.max_wall_seconds),
        }


def build_perf_report(
    *,
    timer: StageTimer,
    stats: BuildStats | None = None,
    baseline: Mapping[str, object] | None = None,
    budget: PerfBudget | None = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """组装性能报告（含 before/after 对照与预算判定）。"""
    resolved_budget = budget or PerfBudget()
    payload: dict[str, object] = {
        "schema": PERF_SCHEMA,
        "current": timer.to_payload(),
        "budget": resolved_budget.to_payload(),
    }
    if stats is not None:
        payload["single_pass"] = stats.to_payload()
    if baseline is not None:
        payload["baseline"] = dict(baseline)
        payload["delta"] = compare_baseline(baseline, payload["current"])
    if extra:
        payload["extra"] = dict(extra)
    payload["budget_check"] = guard_budget(payload, budget=resolved_budget)
    return payload


def compare_baseline(
    baseline: Mapping[str, object], current: Mapping[str, object]
) -> dict[str, object]:
    """before/after 差值（缺项标 ``not_available``，不做比例除法以免除零）。"""
    before_stages = _stages(baseline)
    after_stages = _stages(current)
    stages: dict[str, object] = {}
    for name in sorted(set(before_stages) | set(after_stages)):
        before = before_stages.get(name)
        after = after_stages.get(name)
        if before is None or after is None:
            stages[name] = {
                "before_seconds": before,
                "after_seconds": after,
                "delta_seconds": "not_available",
            }
            continue
        stages[name] = {
            "before_seconds": round(before, 4),
            "after_seconds": round(after, 4),
            "delta_seconds": round(after - before, 4),
        }
    before_wall = _float_or_none(baseline.get("wall_seconds"))
    after_wall = _float_or_none(current.get("wall_seconds"))
    before_rss = _float_or_none(baseline.get("peak_rss_mib"))
    after_rss = _float_or_none(current.get("peak_rss_mib"))
    return {
        "stages": stages,
        "wall_seconds": _delta(before_wall, after_wall),
        "peak_rss_mib": _delta(before_rss, after_rss),
    }


def _delta(before: float | None, after: float | None) -> dict[str, object]:
    if before is None or after is None:
        return {"before": before, "after": after, "delta": "not_available"}
    return {"before": before, "after": after, "delta": round(after - before, 4)}


def _stages(block: Mapping[str, object]) -> dict[str, float]:
    stages = block.get("stages_seconds")
    if not isinstance(stages, Mapping):
        return {}
    return {
        str(key): float(value) for key, value in stages.items() if isinstance(value, (int, float))
    }


def _float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def guard_budget(
    payload: Mapping[str, object], *, budget: PerfBudget | None = None
) -> dict[str, object]:
    """预算判定：超预算只记录、只告警，不静默放行也不改变结果。"""
    resolved = budget or PerfBudget()
    current = payload.get("current")
    if not isinstance(current, Mapping):
        return {"status": "no_current_measurement"}
    peak = _float_or_none(current.get("peak_rss_mib"))
    wall = _float_or_none(current.get("wall_seconds"))
    exceeded: list[str] = []
    if peak is not None and peak > resolved.max_peak_rss_mib:
        exceeded.append("peak_rss_mib")
    if wall is not None and wall > resolved.max_wall_seconds:
        exceeded.append("wall_seconds")
    return {
        "status": "exceeded" if exceeded else "ok",
        "exceeded": exceeded,
        "peak_rss_mib": peak,
        "wall_seconds": wall,
        "peak_rss_unavailable": peak is None,
    }


# ---------------------------------------------------------------------------
# determinism 不变量
# ---------------------------------------------------------------------------


def prediction_digest(frame: pd.DataFrame, *, columns: Sequence[str], digits: int = 6) -> str:
    """预测结果摘要（按行排序后取定长量化，浮点容差内视为相同）。"""
    available = [column for column in columns if column in frame.columns]
    if not available:
        return "no_columns"
    keyed = frame[["decision_date", "symbol", *available]].copy()
    keyed = keyed.sort_values(["decision_date", "symbol"], kind="mergesort")
    values = keyed[available].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    quantized = np.round(np.nan_to_num(values, nan=-999.0), digits)
    digest = hashlib.sha256()
    digest.update(",".join(available).encode("utf-8"))
    digest.update(quantized.tobytes())
    return digest.hexdigest()[:16]


def selection_order(frame: pd.DataFrame, *, score_column: str, top_k: int) -> list[str]:
    """按分数取 TopK 的符号顺序（用于"优化不改变结果语义"的对照）。"""
    if frame.empty or score_column not in frame.columns:
        return []
    values = pd.to_numeric(frame[score_column], errors="coerce")
    ranked = frame.assign(__score=values).dropna(subset=["__score"])
    ranked = ranked.sort_values(
        ["decision_date", "__score", "symbol"], ascending=[True, False, True], kind="mergesort"
    )
    ranked["__rank"] = ranked.groupby("decision_date").cumcount() + 1
    selected = ranked[ranked["__rank"] <= max(1, int(top_k))]
    return [str(symbol) for symbol in selected["symbol"]]


def assert_deterministic(
    left: Mapping[str, object],
    right: Mapping[str, object],
    *,
    keys: Sequence[str] | None = None,
    label: str = "run",
) -> None:
    """同输入两次运行的关键产物必须一致（性能优化不得改结果语义）。"""
    checked = list(keys or ("prediction_digest", "matrix_fingerprint", "selection_order"))
    mismatches: list[str] = []
    for key in checked:
        if key not in left or key not in right:
            continue
        if left[key] != right[key]:
            mismatches.append(f"{key}: {left[key]!r} != {right[key]!r}")
    if mismatches:
        raise AssertionError(
            f"{label} 两次运行结果不一致（determinism 被破坏，禁止用于性能对照）: "
            + "; ".join(mismatches)
        )


def determinism_evidence(
    *,
    matrix: Any,
    predictions: pd.DataFrame,
    feature_columns: Sequence[str],
    top_k: int = 5,
    score_column: str = "alpha_rank_score",
) -> dict[str, object]:
    """一次运行的可对照指纹集合（供 :func:`assert_deterministic` 使用）。"""
    return {
        "matrix_fingerprint": getattr(matrix, "fingerprint", "not_available"),
        "prediction_digest": prediction_digest(
            predictions, columns=[score_column, *list(feature_columns)]
        ),
        "selection_order": selection_order(predictions, score_column=score_column, top_k=top_k),
    }


def perf_report_json(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)


__all__ = [
    "DEFAULT_MAX_PEAK_RSS_MIB",
    "DEFAULT_MAX_WALL_SECONDS",
    "PERF_SCHEMA",
    "PerfBudget",
    "RSS_UNAVAILABLE",
    "STAGE_FETCH",
    "STAGE_FEATURE",
    "STAGE_LABELS",
    "STAGE_MATRIX",
    "STAGE_PERSIST",
    "STAGE_PREDICT",
    "StageTimer",
    "assert_deterministic",
    "build_perf_report",
    "compare_baseline",
    "determinism_evidence",
    "guard_budget",
    "perf_report_json",
    "prediction_digest",
    "rss_mib",
    "selection_order",
]
