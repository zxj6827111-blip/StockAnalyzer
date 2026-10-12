"""从漏斗留档算出影子验证门槛的两个数：观察到的完整交易日、已成熟的模拟成交。

发布清单 R12 要 `shadow_readiness(observed_trade_days≥60, matured_simulated_fills≥100)`，
但此前**没有任何生产者**算这两个数——门槛写着，输入靠人脑估。本模块只做一件事：
把 `funnel_trace_*.json` 数清楚，并且**把不该进分母的东西点名排除**，
而不是给一个看起来完整的数字。

三条不能糊弄的口径：

1. **observed 与 replayed 不得合并**。本项目已实测同一批信号在"系统当时真实打分"
   与"事后重算"下前向结果差一个量级，所以 ``capture_mode`` 必须由调用方显式声明，
   且只能取两个值之一；一个目录混两种口径时数出来的 readiness 没有意义。
2. **不可信的留档不进任何计数**：读侧 ``verify_trace()`` 不过、或时间解释
   （``record_time_semantics``）判为 ``evidence_eligible=false`` 的文件一律排除，
   并把文件名留在输出里——排除本身是可查的事实，不是静默丢数据。
3. **被阻断的那一轮不算完整交易日**：``blocking_reason`` 非空的那天没有产生
   可比的观察，计入天数会让门槛提前"达标"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    TrendStrategyContract,
)
from stock_analyzer.models.tail_net_profit_trainer import shadow_readiness
from stock_analyzer.research.funnel_trace import read_trace, verify_trace

CAPTURE_MODE_OBSERVED = "observed_snapshot"
CAPTURE_MODE_REPLAYED = "replayed_recompute"
ALLOWED_CAPTURE_MODES = (CAPTURE_MODE_OBSERVED, CAPTURE_MODE_REPLAYED)

TRACE_GLOB = "funnel_trace_*.json"
#: 夜扫半段那一份不是尾盘确认的结果，不能当成"又一个观察日"。
NIGHT_SUFFIX = "_night"


def trace_paths(directory: Path | str, *, include_night_half: bool = False) -> list[Path]:
    paths = sorted(Path(directory).glob(TRACE_GLOB))
    if include_night_half:
        return paths
    return [path for path in paths if not path.name.endswith(f"{NIGHT_SUFFIX}.json")]


def _counted_rows(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in (trace.get("final_recommendations") or ())
            if isinstance(row, Mapping)]


def summarize_shadow_evidence(
    directory: Path | str | Iterable[Path],
    *,
    capture_mode: str,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> dict[str, Any]:
    """数留档 → 交给 ``shadow_readiness``；排除项与理由一并返回，不藏。"""
    if capture_mode not in ALLOWED_CAPTURE_MODES:
        raise ValueError(
            f"capture_mode must be one of {ALLOWED_CAPTURE_MODES}; got {capture_mode!r}. "
            "observed 与 replayed 合并计数会得到一个没有意义的门槛读数。"
        )
    paths = ([Path(item) for item in directory] if isinstance(directory, Iterable)
             and not isinstance(directory, (str, Path)) else trace_paths(directory))

    untrusted: list[str] = []
    time_ineligible: list[str] = []
    blocked_days: list[str] = []
    trade_days: set[str] = set()
    days_with_recommendation: set[str] = set()
    rows = 0
    matured = 0
    pending = 0
    counted_files = 0

    for path in paths:
        payload = read_trace(path)
        failures = verify_trace(payload, contract=contract)
        if failures:
            untrusted.append(f"{path.name}:{';'.join(failures)[:120]}")
            continue
        interpretation = payload.get("time_interpretation") or {}
        if not interpretation.get("evidence_eligible"):
            # 修复前的留档：原始值一个字节都不改，但没有可信的时刻就不配进未来真值的门槛。
            time_ineligible.append(path.name)
            continue
        counted_files += 1
        day = str(payload.get("trade_date", "") or "")
        if str(payload.get("blocking_reason", "") or "").strip():
            blocked_days.append(day or path.name)
            continue
        trade_days.add(day)
        rows_here = _counted_rows(payload)
        if rows_here:
            days_with_recommendation.add(day)
        for row in rows_here:
            rows += 1
            fill = row.get("fill")
            if isinstance(fill, Mapping) and fill.get("realized"):
                matured += 1
            elif isinstance(fill, Mapping) and fill:
                pending += 1

    observed_trade_days = len({day for day in trade_days if day})
    readiness = shadow_readiness(
        observed_trade_days=observed_trade_days,
        matured_simulated_fills=matured,
    )
    return {
        "capture_mode": capture_mode,
        "contract_digest": contract.digest(),
        "trace_files_seen": len(list(paths)),
        "trace_files_counted": counted_files,
        "excluded_untrusted": untrusted,
        "excluded_time_ineligible": time_ineligible,
        "blocked_days": sorted(blocked_days),
        "observed_trade_days": observed_trade_days,
        "days_with_recommendation": len(days_with_recommendation),
        "recommendation_coverage": (
            len(days_with_recommendation) / observed_trade_days
            if observed_trade_days else 0.0
        ),
        "recommendation_rows": rows,
        "matured_simulated_fills": matured,
        "pending_fills": pending,
        "readiness": readiness,
    }


__all__ = [
    "ALLOWED_CAPTURE_MODES",
    "CAPTURE_MODE_OBSERVED",
    "CAPTURE_MODE_REPLAYED",
    "NIGHT_SUFFIX",
    "TRACE_GLOB",
    "shadow_readiness",
    "summarize_shadow_evidence",
    "trace_paths",
]
