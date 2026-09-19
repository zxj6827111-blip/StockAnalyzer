"""Alpha V2 outcome 成熟任务（M3 §8）：把冻结的 Shadow 预测变成真实成熟结果。

调用时机：每个交易日收盘数据入库之后跑一次（生产调度接线属部署授权范围）。

口径来源（全部复用已验收实现，不重写）：

- 入场/退出/净收益/MAE/MFE/成熟标记：S11 :func:`compute_outcomes`
  （T+1 真实可成交开盘 + raw 价 + 滑点/费用 + 一字涨停/停牌 no_fill）；
- 基准超额：M3 §12 冻结的四层基准——``eligible_ew`` / ``quality_pool_ew``
  （快照成员）/ ``style_matched``（S12 kNN 对照）逐列落盘；Simple Baseline
  在 KPI 层做同日配对（它产生 TopK，不是 EW 序列）；
- 成熟门：行情只装载到 ``evaluation_date`` 为止（面板里没有未来的 bar，
  这是"不预写未来"的物理保证），且 horizon 还需跨过市场日历上限。

改写规则（M3 §7 的另一半）：

- 已成熟的 horizon 值**重算必须逐字节一致**，否则抛
  :class:`OutcomeRestatementError`——行情被回改/口径被改都会在这里现形；
- 未成熟的 pending horizon 只在成熟那一刻一次性写入；
- 信号当天调用（``evaluation_date == signal_date``）写 0 行（有测试）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.benchmarks import (
    BENCHMARK_ELIGIBLE,
    BENCHMARK_QUALITY_POOL,
    compute_style_features,
    style_matched_control,
)
from stock_analyzer.alpha_v2.research.metrics import NOT_AVAILABLE
from stock_analyzer.alpha_v2.research.outcomes import (
    DecisionPoint,
    OutcomeSpec,
    attach_excess_returns,
    benchmark_series_from_outcomes,
    compute_outcomes,
)
from stock_analyzer.alpha_v2.research.panel import DailyPanel
from stock_analyzer.alpha_v2.validation.epoch import (
    ROW_IDENTITY_KEYS,
    EpochRecord,
    epoch_identity_matches,
    epoch_subdirs,
    require_epoch_identity_match,
    update_epoch_days,
)
from stock_analyzer.alpha_v2.validation.freeze import FROZEN_BENCHMARK_LAYERS
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    list_shadow_dates,
    read_shadow_rows,
)

OUTCOME_ROW_SCHEMA = "alpha_v2_shadow_outcome.v1"
OUTCOME_FILENAME_PREFIX = "outcome"

# 快照里"真实生产质量池 / 研究代理池"的来源标记（与 S12 的词表对齐）。
QUALITY_POOL_SOURCE_PRODUCTION = "production_selection_engine"
QUALITY_POOL_SOURCE_PROXY = "research_proxy:alpha_v2_quality_v1"

# 成熟后逐日per列写入风格对照时识别"成熟但未算超额"的哨兵。
_ELIGIBLE_SUFFIX = "__eligible_ew"


class OutcomeMaturationError(RuntimeError):
    """成熟任务结构性失败（epoch 缺失、面板缺口径等）。"""


class OutcomeRestatementError(RuntimeError):
    """已成熟 horizon 的值被重算成不同结果（行情回改/口径漂移的信号）。"""


class ShadowSnapshotIntegrityError(RuntimeError):
    """outcome 文件与影子快照不一致（出现了影子圈外的 symbol 等）时对账拒绝。"""


def outcome_path(root: str | Path, epoch_id: str, signal_date: date) -> Path:
    base = epoch_subdirs(root, epoch_id)["outcomes"]
    return base / f"{signal_date.year:04d}" / f"{signal_date.month:02d}" / (
        f"{OUTCOME_FILENAME_PREFIX}_{signal_date.strftime('%Y%m%d')}.jsonl"
    )


def mature_epoch_outcomes(
    *,
    root: str | Path,
    epoch: EpochRecord,
    panel: DailyPanel,
    evaluation_date: date,
    matcher: Any,
    slippage_ratio: float,
    price_mode: str,
    price_mode_certified: bool,
    spec: OutcomeSpec | None = None,
    runtime_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """对 epoch 内所有快照日做一**幂等**成熟扫描；返回本次运行的审计摘要。

    先过三道门才允许动手（F2/F3 修复）：

    1. epoch `require_open_epoch`（注册表为准）——closed epoch 不写不更；
    2. 磁盘冻结清单仍锚定 epoch（``freeze_manifest_hash`` 逐位一致）；
    3. ``runtime_identity``（如提供）与 epoch 冻结身份按所给键严格核对。
    """
    record = require_epoch_identity_match(
        root=root,
        epoch_id=epoch.epoch_id,
        identity=runtime_identity,
        keys=(tuple(runtime_identity) if runtime_identity is not None else None),
        require_manifest_anchor=True,
    )
    resolved_spec = spec or OutcomeSpec()
    # 物理截断：只用 evaluation_date 当天及以前的 bar——"还差几天才成熟的 horizon
    # 不提前写"靠的不该是函数内的日期判断，而是"未来的数据根本不进场"。
    clip = _clip_panel(panel, evaluation_date)
    calendar = [day for day in clip.calendar if day <= evaluation_date]
    calendar_index = {day: index for index, day in enumerate(calendar)}
    horizons = tuple(int(h) for h in resolved_spec.horizons)

    summary: dict[str, object] = {
        "schema": OUTCOME_ROW_SCHEMA,
        "validation_epoch_id": epoch.epoch_id,
        "evaluation_date": evaluation_date.isoformat(),
        "run_at": datetime.now().astimezone().isoformat(),
        "price_mode": str(price_mode),
        "price_mode_certified": bool(price_mode_certified),
        "horizons": list(horizons),
        "benchmarks": {
            "layers": [BENCHMARK_ELIGIBLE, BENCHMARK_QUALITY_POOL, "style_matched"],
            "frozen_layers": list(FROZEN_BENCHMARK_LAYERS),
            "note": (
                "eligible_ew / quality_pool_ew / style_matched 逐列落盘；"
                "simple_baseline 在 KPI 层同日配对（它产生 TopK 不是 EW 序列）"
            ),
        },
        "days": [],
        "rows_written": 0,
        "rows_skipped_pending": 0,
        "dates_without_snapshot": 0,
        "restatement_guard": "matured_horizon_values_must_match_exactly",
    }

    for signal_date in list_shadow_dates(root, epoch.epoch_id):
        shadow_rows = read_shadow_rows(root, epoch.epoch_id, signal_date)
        if not shadow_rows:
            continue
        # 影子行的身份就是这天的证据出处——不得与 epoch 冻结身份漂移半步。
        for row in shadow_rows:
            violations = epoch_identity_matches(record, row, keys=ROW_IDENTITY_KEYS)
            if violations:
                raise OutcomeRestatementError(
                    f"{signal_date} 快照行身份与 epoch 冻结身份不符"
                    f"（{row.get('symbol', '?')}）: {'; '.join(violations)}"
                )
        pending = _pending_horizons(
            root, epoch.epoch_id, signal_date, horizons=horizons
        )
        fully_before = [
            h
            for h in horizons
            if _horizon_maturity_reached(
                signal_date, h, calendar=calendar, calendar_index=calendar_index
            )
        ]
        if not pending or not fully_before:
            summary["rows_skipped_pending"] = int(summary["rows_skipped_pending"]) + len(
                shadow_rows
            )
            continue

        decisions = [
            DecisionPoint(symbol=str(row.get("symbol", "")), decision_date=signal_date)
            for row in shadow_rows
            if str(row.get("symbol", "") or "").strip()
        ]
        if not decisions:
            continue

        quality_members = {
            str(row.get("symbol"))
            for row in shadow_rows
            if _truthy(row.get("in_quality_pool"))
        }
        quality_source = _quality_pool_source(shadow_rows)

        outcome_run = compute_outcomes(
            panel=clip,
            decisions=decisions,
            spec=resolved_spec,
            matcher=matcher,
            slippage_ratio=float(slippage_ratio),
            price_mode=price_mode,
            price_mode_certified=price_mode_certified,
            source_meta={
                "validation_epoch_id": epoch.epoch_id,
                "signal_date": signal_date.isoformat(),
                "quality_pool_source": quality_source,
            },
        )
        frame = outcome_run.frame
        if frame.empty:
            continue

        # 基准 1：eligible EW（决策集合本身的等权，即快照全体）。
        eligible_series = benchmark_series_from_outcomes(
            frame, horizons=list(horizons), pool_mask=None, name=BENCHMARK_ELIGIBLE
        )
        frame = _attach_named_benchmark(
            frame,
            series=eligible_series,
            name=BENCHMARK_ELIGIBLE,
            horizons=horizons,
        )
        # 基准 2：quality300 EW（主基准，写进规范列 excess_return_{h}d）。
        quality_mask = (
            frame["symbol"].astype(str).isin(quality_members) if quality_members else None
        )
        quality_series = benchmark_series_from_outcomes(
            frame,
            horizons=list(horizons),
            pool_mask=quality_mask,
            name=BENCHMARK_QUALITY_POOL,
        )
        frame = attach_excess_returns(
            frame,
            benchmark=quality_series,
            horizons=list(horizons),
            short_horizons=resolved_spec.short_horizons,
            name=BENCHMARK_QUALITY_POOL,
        )
        frame["benchmark_name"] = BENCHMARK_QUALITY_POOL
        frame["quality_pool_source"] = quality_source
        # 基准 3：style_matched（同板块 kNN 对照，残差 = 净收益 - 对照）。
        try:
            styles = compute_style_features(panel=clip, decisions=decisions)
            enriched = frame.merge(styles, on=["decision_date", "symbol"], how="left")
            style = style_matched_control(enriched, horizons=list(horizons))
            if not style.empty:
                suffix_cols = [
                    "decision_date",
                    "symbol",
                    "style_peer_count",
                    "style_fallback",
                ] + [f"control_return_{int(h)}d" for h in horizons]
                suffix_cols += [f"residual_excess_return_{int(h)}d" for h in horizons]
                keep = [c for c in suffix_cols if c in style.columns]
                frame = frame.drop(
                    columns=[
                        c
                        for c in keep
                        if c in frame.columns and c not in ("decision_date", "symbol")
                    ],
                    errors="ignore",
                ).merge(style[keep], on=["decision_date", "symbol"], how="left")
        except Exception as exc:  # noqa: BLE001 - 风格层失败不吞主口径，如实标 not_available
            frame["style_layer_error"] = f"{type(exc).__name__}: {exc}"

        # 行级 epoch/冻结身份
        frame["validation_epoch_id"] = epoch.epoch_id
        frame["signal_date"] = signal_date.isoformat()

        written = _merge_and_write_outcomes(
            root=root,
            epoch_id=epoch.epoch_id,
            signal_date=signal_date,
            frame=frame,
            horizons=horizons,
            shadow_symbols={str(row.get("symbol", "")) for row in shadow_rows},
        )
        summary["rows_written"] = int(summary["rows_written"]) + written["rows"]
        summary["days"].append(  # type: ignore[attr-defined]
            {
                "signal_date": signal_date.isoformat(),
                "matured_horizons_before": pending["matured"],
                "rows_written": written["rows"],
                "rows_total": written["total"],
                "no_fill": outcome_run.diagnostics.get("no_fill_by_reason", {}),
            }
        )

    # 回写 epoch 样本账（成熟日期数、总行数）。
    days = _epoch_days_summary(root, epoch.epoch_id, horizons=horizons)
    update_epoch_days(root=root, epoch_id=epoch.epoch_id, days=days)
    summary["epoch_days"] = days
    if not summary["days"]:
        summary["status"] = "nothing_matured_yet"
    else:
        summary["status"] = "ok"
    return summary


def _clip_panel(panel: DailyPanel, evaluation_date: date) -> DailyPanel:
    """把面板裁到 ``evaluation_date``（含）为止。

    不裁会出现的真实泄漏：S11 按"标的自身 bar 数"判成熟——若面板里装进了
    未来的 bar，5D/10D outcome 会在仅过去 3 天时就被当成熟写出。
    """
    bars = panel.bars
    if not bars.empty and "trade_date" in bars.columns:
        dates = pd.to_datetime(bars["trade_date"]).dt.date
        clipped = bars.loc[dates <= evaluation_date].copy()
    else:
        clipped = bars
    return DailyPanel(
        bars=clipped,
        calendar=tuple(day for day in panel.calendar if day <= evaluation_date),
        symbols=panel.symbols,
        source=panel.source,
        window_start=panel.window_start,
        window_end=min(panel.window_end, evaluation_date),
        warmup_days=panel.warmup_days,
    )


# ---------------------------------------------------------------------------
# 成熟门与合并
# ---------------------------------------------------------------------------


def _horizon_maturity_reached(
    signal_date: date, horizon: int, *, calendar: Sequence[date], calendar_index: Mapping[date, int]
) -> bool:
    """市场日历层面：signal + horizon 个交易日是否已到（含）。"""
    position = calendar_index.get(signal_date)
    if position is None:
        return False
    return position + int(horizon) <= len(calendar) - 1


def _pending_horizons(
    root: str | Path, epoch_id: str, signal_date: date, *, horizons: Sequence[int]
) -> dict[str, list[int]] | None:
    """已存 outcome 里还没成熟的 horizon（文件不存在 = 全部 pending）。"""
    path = outcome_path(root, epoch_id, signal_date)
    if not path.exists():
        return {"matured": [], "pending": [int(h) for h in horizons]}
    matured_any: dict[int, bool] = {int(h): False for h in horizons}
    for row in _read_jsonl(path):
        for key in horizons:
            if row.get(f"matured_{int(key)}d") is True:
                matured_any[int(key)] = True
    pending = [key for key, done in matured_any.items() if not done]
    return {"matured": [key for key, done in matured_any.items() if done], "pending": pending}


def _attach_named_benchmark(
    frame: pd.DataFrame,
    *,
    series: pd.DataFrame,
    name: str,
    horizons: Sequence[int],
) -> pd.DataFrame:
    """给 frame 增加 ``benchmark_return_{h}d__{name}`` / ``excess_return_{h}d__{name}``。"""
    result = frame.copy()
    suffix = f"__{name}"
    if series.empty:
        for horizon in horizons:
            key = int(horizon)
            result[f"benchmark_return_{key}d{suffix}"] = NOT_AVAILABLE
            result[f"excess_return_{key}d{suffix}"] = NOT_AVAILABLE
        return result
    lookup = series.copy()
    lookup["decision_date"] = lookup["decision_date"].astype(str)
    wide = lookup.pivot_table(
        index="decision_date", columns="horizon", values="benchmark_return", aggfunc="first"
    )
    keys = result["decision_date"].astype(str)
    for horizon in horizons:
        key = int(horizon)
        column = wide[key] if key in wide.columns else None
        if column is None:
            result[f"benchmark_return_{key}d{suffix}"] = NOT_AVAILABLE
            result[f"excess_return_{key}d{suffix}"] = NOT_AVAILABLE
            continue
        base = pd.Series(column.reindex(keys).to_numpy(), index=result.index)
        present = keys.isin(set(wide.index))
        base = base.where(present)
        net = pd.to_numeric(result.get(f"net_return_{key}d"), errors="coerce")
        excess = (net - base).where(base.notna() & net.notna())
        result[f"benchmark_return_{key}d{suffix}"] = [
            NOT_AVAILABLE if pd.isna(value) else round(float(value), 8) for value in base
        ]
        result[f"excess_return_{key}d{suffix}"] = [
            NOT_AVAILABLE if pd.isna(value) else round(float(value), 8) for value in excess
        ]
    return result


def _merge_and_write_outcomes(
    *,
    root: str | Path,
    epoch_id: str,
    signal_date: date,
    frame: pd.DataFrame,
    horizons: Sequence[int],
    shadow_symbols: set[str] | None = None,
) -> dict[str, int]:
    """成熟合并：旧行已成熟值必须逐字节一致；新成熟 horizon 一次性补齐。

    `shadow_symbols`：当日快照的 symbol 集合。磁盘上已有的 outcome 行若落在
    这个集合之外（手填/篡改/快照被收回），直接拒写——outcome 只能来自影子。
    """
    path = outcome_path(root, epoch_id, signal_date)
    existing = _read_jsonl(path)
    if shadow_symbols is not None:
        existing_symbols = {str(row.get("symbol", "")) for row in existing}
        stray = sorted(symbol for symbol in existing_symbols - shadow_symbols if symbol)
        if stray:
            raise ShadowSnapshotIntegrityError(
                f"{signal_date} 的 outcome 文件含影子外的 symbol: {stray[:10]}"
                "（可能是手填或快照链路被篡改）。这条证据链断开，必须由 close→"
                "新 epoch 处理。"
            )
    existing_by_symbol = {str(row.get("symbol", "")): row for row in existing}
    merged: dict[str, dict[str, object]] = {
        key: dict(value) for key, value in existing_by_symbol.items()
    }

    records = frame.to_dict(orient="records")
    for record in records:
        symbol = str(record.get("symbol", "") or "")
        if not symbol:
            continue
        normalized = _json_safe_record(record, horizons=horizons)
        # 行内标记 schema / epoch
        normalized.setdefault("schema", OUTCOME_ROW_SCHEMA)
        prior = merged.get(symbol)
        if prior is None:
            merged[symbol] = normalized
            continue
        _assert_no_restatement(
            prior, normalized, horizons=horizons, symbol=symbol, signal_date=signal_date
        )
        # pending → matured 的列补齐；其余以先写为准。
        for key in horizons:
            matured_key = f"matured_{int(key)}d"
            if prior.get(matured_key) is True:
                continue
            if normalized.get(matured_key) is True:
                for field, value in normalized.items():
                    if field.endswith(f"_{int(key)}d") or field == matured_key:
                        prior[field] = value
        prior["signal_date"] = signal_date.isoformat()
        prior["validation_epoch_id"] = epoch_id

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(path, [merged[key] for key in sorted(merged)])
    return {"rows": len(records), "total": len(merged)}


def _assert_no_restatement(
    prior: Mapping[str, object],
    incoming: Mapping[str, object],
    *,
    horizons: Sequence[int],
    symbol: str,
    signal_date: date,
) -> None:
    diffs: list[str] = []
    for horizon in horizons:
        key = int(horizon)
        if prior.get(f"matured_{key}d") is not True:
            continue
        for field in (
            f"maturity_date_{key}d",
            f"net_return_{key}d",
            f"mae_{key}d",
            f"mfe_{key}d",
            f"excess_return_{key}d",
            f"benchmark_return_{key}d",
            f"exit_no_fill_{key}d",
        ):
            before = _scalar(prior.get(field))
            after = _scalar(incoming.get(field))
            if before != after:
                diffs.append(f"{field}:{before!r}=>{after!r}")
    for field in (
        "executable",
        "entry_date",
        "entry_price_raw",
        "entry_price_net",
        "no_fill_reason",
    ):
        before = _scalar(prior.get(field))
        after = _scalar(incoming.get(field))
        if before != after and before is not None:
            diffs.append(f"{field}:{before!r}=>{after!r}")
    if diffs:
        raise OutcomeRestatementError(
            f"{signal_date} {symbol}: 已成熟 outcome 与重算不一致（{len(diffs)} 处）: "
            + "; ".join(diffs[:12])
            + "。请检查行情数据是否被回改或口径是否漂移（这应触发 epoch 复核，而非静默改写）。"
        )


def _scalar(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return NOT_AVAILABLE
        return round(value, 10)
    return value


def _json_safe_record(
    record: Mapping[str, object], *, horizons: Sequence[int]
) -> dict[str, object]:
    """把 DataFrame 行记录转成 JSON 安全 dict（numpy 标量显式降级，NaN/Inf → not_available）。"""
    safe: dict[str, object] = {}
    for key, value in record.items():
        safe[key] = _json_scalar(value)
    return safe


def _json_scalar(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        parsed = float(value)
        return parsed if np.isfinite(parsed) else NOT_AVAILABLE
    if isinstance(value, float):
        finite = value == value and value not in (float("inf"), float("-inf"))
        return value if finite else NOT_AVAILABLE
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        try:
            return value.isoformat()  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return str(value)
    return value


def _epoch_days_summary(
    root: str | Path, epoch_id: str, *, horizons: Sequence[int]
) -> dict[str, object]:
    days: dict[str, object] = {}
    dates = list_shadow_dates(root, epoch_id)
    days["signal_dates_total"] = len(dates)
    for horizon in horizons:
        key = int(horizon)
        mature_days = 0
        for signal_date in dates:
            rows = _read_jsonl(outcome_path(root, epoch_id, signal_date))
            if any(row.get(f"matured_{key}d") is True for row in rows):
                mature_days += 1
        days[f"mature_dates_{key}d"] = mature_days
    return days


def _quality_pool_source(shadow_rows: Sequence[Mapping[str, object]]) -> str:
    for row in shadow_rows:
        value = row.get("quality_pool_source")
        if isinstance(value, str) and value.strip():
            return value.strip()
    # 快照行没写来源就保守标代理（生产接线时由 capture 侧显式给 production 值）。
    return QUALITY_POOL_SOURCE_PROXY


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    content = "\n".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) for row in rows)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content + ("\n" if content else ""), encoding="utf-8")
    temp.replace(path)


__all__ = [
    "OUTCOME_FILENAME_PREFIX",
    "OUTCOME_ROW_SCHEMA",
    "OutcomeMaturationError",
    "OutcomeRestatementError",
    "QUALITY_POOL_SOURCE_PRODUCTION",
    "QUALITY_POOL_SOURCE_PROXY",
    "ShadowSnapshotIntegrityError",
    "mature_epoch_outcomes",
    "outcome_path",
]
