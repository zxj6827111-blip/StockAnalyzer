"""Alpha V2 决策日志与 outcome 成熟（S10 / 原 P0-10）。

**目标**（阶段施工提示词 S10）：

- 每天盘后保存**当时真实**的 prediction snapshot（决策时刻可见的一切）；
- outcome 只在 T+3/T+5/T+10/T+15 **成熟后**独立追加，信号当天绝不写未来数据；
- 目录：``decisions/YYYY/MM/``、``outcomes/YYYY/MM/``、``manifests/``；
- 不新增数据库：JSONL + 现有 DuckDB 能力。

**Done When**：任选一天能完整回答"当时 universe / 当时模型 / 当时候选排序 /
当时预测 / 后续真实成熟 outcome"。

两条硬约束：

1. **不得编造**：V2 多 Head 尚未实现时，``v2_rank_score`` 等字段写
   ``"not_available"``，绝不用 legacy 分数冒充；
2. **不得预写未来**：outcome 只在成熟日之后写入，且必须用 S02 的 T+1 真实可成交
   入场 + raw 价格（S07）计算；未成熟的 horizon 只记录"尚未成熟"。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from stock_analyzer.alpha_v2.artifacts import AlphaV2ArtifactLayout, write_json_atomic
from stock_analyzer.config import AlphaV2Config

NOT_AVAILABLE = "not_available"
DEFAULT_HORIZONS: tuple[int, ...] = (3, 5, 10, 15)

DECISION_FILENAME_PREFIX = "decision"
OUTCOME_FILENAME_PREFIX = "outcome"


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """一条决策记录（决策时刻的完整快照；缺失字段一律 not_available）。"""

    signal_date: str
    symbol: str
    # 以下字段承载"决策时刻可见的值或 not_available 标记"，故类型为 object
    # （JSON 原样落盘；缺失时是标记串而不是 None，便于查询区分"缺失"与"空值"）。
    eligible: object
    quality_rank: object
    light_rank: object
    deep_rank: object
    legacy_score: object
    legacy_reject_reasons: object
    v2_rank_score: object
    v2_expected_return: object
    v2_direction_score: object
    v2_risk_score: object
    model_identity: dict[str, object]
    feature_schema: dict[str, object]
    label_policy: dict[str, object]
    data_snapshot: dict[str, object]
    selection_contract: dict[str, object]
    recorded_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "signal_date": self.signal_date,
            "symbol": self.symbol,
            "eligible": self.eligible,
            "quality_rank": self.quality_rank,
            "light_rank": self.light_rank,
            "deep_rank": self.deep_rank,
            "legacy_score": self.legacy_score,
            "legacy_reject_reasons": self.legacy_reject_reasons,
            "v2_rank_score": self.v2_rank_score,
            "v2_expected_return": self.v2_expected_return,
            "v2_direction_score": self.v2_direction_score,
            "v2_risk_score": self.v2_risk_score,
            "model_identity": dict(self.model_identity),
            "feature_schema": dict(self.feature_schema),
            "label_policy": dict(self.label_policy),
            "data_snapshot": dict(self.data_snapshot),
            "selection_contract": dict(self.selection_contract),
            "recorded_at": self.recorded_at,
        }


def decision_path(root: str | Path, *, signal_date: date) -> Path:
    """``decisions/YYYY/MM/decision_YYYYMMDD.jsonl``。"""
    base = Path(root)
    return base / f"{signal_date.year:04d}" / f"{signal_date.month:02d}" / (
        f"{DECISION_FILENAME_PREFIX}_{signal_date.strftime('%Y%m%d')}.jsonl"
    )


def outcome_path(root: str | Path, *, signal_date: date) -> Path:
    """``outcomes/YYYY/MM/outcome_YYYYMMDD.jsonl``（按**信号日**归档）。"""
    base = Path(root)
    return base / f"{signal_date.year:04d}" / f"{signal_date.month:02d}" / (
        f"{OUTCOME_FILENAME_PREFIX}_{signal_date.strftime('%Y%m%d')}.jsonl"
    )


def build_decision_rows(
    *,
    signal_date: date,
    candidates: Iterable[Mapping[str, object]],
    model_identity: Mapping[str, object] | None = None,
    feature_schema: Mapping[str, object] | None = None,
    label_policy: Mapping[str, object] | None = None,
    data_snapshot: Mapping[str, object] | None = None,
    selection_contract: Mapping[str, object] | None = None,
    recorded_at: str | None = None,
) -> list[DecisionRow]:
    """从**决策时刻可见**的候选与身份信息构造决策行。

    ``candidates`` 每项至少含 ``symbol``；可选 ``legacy_score`` / ``legacy_reject_reasons``
    / ``eligible`` 与各 rank。V2 多 Head 字段本阶段恒为 ``not_available``
    （阶段提示词：尚未实现时允许 null / not_available，**不得编造**）。
    """
    stamp = recorded_at or datetime.now().astimezone().isoformat()
    rows: list[DecisionRow] = []
    for index, candidate in enumerate(candidates, start=1):
        symbol = str(candidate.get("symbol", "") or "").strip()
        if not symbol:
            continue
        rows.append(
            DecisionRow(
                signal_date=signal_date.isoformat(),
                symbol=symbol,
                eligible=_first_present(candidate, ("eligible",), default=NOT_AVAILABLE),
                quality_rank=_first_present(candidate, ("quality_rank",), default=NOT_AVAILABLE),
                light_rank=_first_present(candidate, ("light_rank",), default=NOT_AVAILABLE),
                deep_rank=_first_present(
                    candidate,
                    ("deep_rank",),
                    default=index if candidate.get("deep") else NOT_AVAILABLE,
                ),
                legacy_score=_first_present(
                    candidate, ("legacy_score", "score"), default=NOT_AVAILABLE
                ),
                legacy_reject_reasons=_first_present(
                    candidate, ("legacy_reject_reasons",), default=NOT_AVAILABLE
                ),
                # V2 多 Head（S16）尚未实现：恒 not_available，绝不用其它分数顶替
                v2_rank_score=_first_present(
                    candidate, ("v2_rank_score",), default=NOT_AVAILABLE
                ),
                v2_expected_return=_first_present(
                    candidate, ("v2_expected_return",), default=NOT_AVAILABLE
                ),
                v2_direction_score=_first_present(
                    candidate, ("v2_direction_score",), default=NOT_AVAILABLE
                ),
                v2_risk_score=_first_present(
                    candidate, ("v2_risk_score",), default=NOT_AVAILABLE
                ),
                model_identity=dict(model_identity or {}),
                feature_schema=dict(feature_schema or {}),
                label_policy=dict(label_policy or {}),
                data_snapshot=dict(data_snapshot or {}),
                selection_contract=dict(selection_contract or {}),
                recorded_at=stamp,
            )
        )
    return rows


def write_decision_rows(
    *,
    root: str | Path,
    signal_date: date,
    rows: Sequence[DecisionRow],
) -> Path:
    """按 (signal_date, symbol) 幂等写入决策 JSONL（同日重复写不产生重复行）。"""
    path = decision_path(root, signal_date=signal_date)
    existing = _read_jsonl(path)
    merged: dict[str, dict[str, object]] = {}
    for item in existing + [row.to_payload() for row in rows]:
        key = f"{item.get('signal_date', '')}|{item.get('symbol', '')}"
        merged[key] = item
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(path, [merged[key] for key in sorted(merged)])
    return path


@dataclass(frozen=True, slots=True)
class OutcomeMaturity:
    """一次 outcome 成熟计算的结果（含未成熟 horizon 的如实标注）。"""

    signal_date: date
    evaluation_date: date
    matured_horizons: tuple[int, ...]
    pending_horizons: tuple[int, ...]
    rows: tuple[dict[str, object], ...]

    @property
    def any_matured(self) -> bool:
        return bool(self.matured_horizons)


def compute_outcomes(
    *,
    decision_rows: Sequence[Mapping[str, object]],
    bars_by_symbol: Mapping[str, Any],
    signal_date: date,
    evaluation_date: date,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    trading_days: Sequence[date] | None = None,
) -> OutcomeMaturity:
    """按 S02 的 T+1 可成交入场 + raw 价格计算已成熟 horizon 的 outcome。

    **只在成熟日之后写**：``evaluation_date`` 之前的 horizon 一律进 ``pending``；
    信号当天调用时（``evaluation_date == signal_date``）不会有任何 outcome 行——
    这正是"禁止 signal 当天提前写未来数据"的可执行形式。

    入场不可成交（停牌/一字涨停等）时 outcome 行标 ``executable=False`` +
    ``no_fill_reason``，收益字段保留 ``not_available``（不假设理想成交）。
    """
    from stock_analyzer.backtest.matcher import ExecutionMatcher
    from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig

    matcher = ExecutionMatcher(BacktestMatcherConfig(), limit_rule=LimitRuleConfig())
    calendar = list(trading_days) if trading_days else None

    matured: list[int] = []
    pending: list[int] = []
    rows: list[dict[str, object]] = []
    for horizon in sorted({int(item) for item in horizons if int(item) > 0}):
        target_date = _maturity_date(
            signal_date=signal_date, horizon=horizon, trading_days=calendar
        )
        if target_date is None or evaluation_date < target_date:
            pending.append(horizon)
            continue
        matured.append(horizon)
        for decision in decision_rows:
            symbol = str(decision.get("symbol", "") or "").strip()
            if not symbol:
                continue
            bars = bars_by_symbol.get(symbol)
            rows.append(
                _outcome_row(
                    decision=decision,
                    bars=bars,
                    signal_date=signal_date,
                    horizon=horizon,
                    target_date=target_date,
                    matcher=matcher,
                )
            )
    return OutcomeMaturity(
        signal_date=signal_date,
        evaluation_date=evaluation_date,
        matured_horizons=tuple(matured),
        pending_horizons=tuple(pending),
        rows=tuple(rows),
    )


def write_outcomes(
    *,
    root: str | Path,
    maturity: OutcomeMaturity,
) -> Path:
    """把已成熟 outcome 写入 ``outcomes/YYYY/MM/``（按 (signal_date,symbol,horizon) 幂等）。"""
    path = outcome_path(root, signal_date=maturity.signal_date)
    existing = _read_jsonl(path)
    merged: dict[str, dict[str, object]] = {}
    for item in existing + list(maturity.rows):
        key = "|".join(
            str(item.get(field, "")) for field in ("signal_date", "symbol", "horizon")
        )
        merged[key] = item
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(path, [merged[key] for key in sorted(merged)])
    return path


def write_manifest(
    *,
    root: str | Path,
    signal_date: date,
    payload: Mapping[str, object],
) -> Path:
    """``manifests/run_YYYYMMDD.json``：这次决策运行的身份与来源（含未成熟标注）。"""
    path = (
        Path(root)
        / f"{signal_date.year:04d}"
        / f"{signal_date.month:02d}"
        / f"run_{signal_date.strftime('%Y%m%d')}.json"
    )
    return write_json_atomic(path, dict(payload))


def resolve_decision_root(
    config: object, *, project_root: str | Path | None = None
) -> Path:
    """决策/outcome 根的解析（复用 ``alpha_v2.artifact_root``）。"""
    alpha_v2_config = getattr(config, "alpha_v2", None)
    layout = AlphaV2ArtifactLayout.from_config(
        alpha_v2_config if isinstance(alpha_v2_config, AlphaV2Config) else None,
        project_root=project_root,
    )
    layout.ensure()
    return layout.root


def _outcome_row(
    *,
    decision: Mapping[str, object],
    bars: object,
    signal_date: date,
    horizon: int,
    target_date: date,
    matcher: Any,
) -> dict[str, object]:
    row: dict[str, object] = {
        "signal_date": signal_date.isoformat(),
        "symbol": str(decision.get("symbol", "")),
        "horizon": horizon,
        "maturity_date": target_date.isoformat(),
        "entry_mode": "next_session_open",
        "price_basis": "raw",
    }
    if bars is None or not hasattr(bars, "empty"):
        row.update(
            {
                "executable": False,
                "no_fill_reason": "bars_unavailable",
                "net_return_pct": NOT_AVAILABLE,
            }
        )
        return row
    frame = bars  # pandas.DataFrame（由调用方保证）
    if bool(frame.empty):
        row.update(
            {
                "executable": False,
                "no_fill_reason": "empty_bars",
                "net_return_pct": NOT_AVAILABLE,
            }
        )
        return row
    entry_window = _bars_after(frame, signal_date=signal_date, count=1)
    entry = matcher.simulate_entry(
        signal_date=datetime.combine(signal_date, datetime.min.time()),
        future_bars=entry_window,
    )
    if not entry.executed or entry.entry_date is None:
        row.update(
            {
                "executable": False,
                "no_fill_reason": entry.no_fill_reason,
                "net_return_pct": NOT_AVAILABLE,
            }
        )
        return row
    exit_bars = _bars_after(frame, signal_date=signal_date, count=horizon + 1)
    if len(exit_bars) <= horizon:
        row.update({"executable": True, "no_fill_reason": "", "net_return_pct": NOT_AVAILABLE})
        row["outcome_pending_reason"] = "insufficient_bars_after_entry"
        return row
    exit_close = _close_of(exit_bars[horizon][1])
    entry_price = float(entry.net_entry_price)
    net_return = (exit_close - entry_price) / entry_price if entry_price > 0 else 0.0
    row.update(
        {
            "executable": True,
            "no_fill_reason": "",
            "entry_date": entry.entry_date.date().isoformat(),
            "entry_price_raw": float(entry.entry_price_raw),
            "entry_price_net": entry_price,
            "exit_price_raw": exit_close,
            "net_return_pct": round(net_return, 6),
        }
    )
    return row


def _bars_after(
    frame: Any, *, signal_date: date, count: int
) -> list[tuple[datetime, dict[str, object]]]:
    """信号日**之后**的前 count 根 bar（T+1 起；与 S02 入场契约一致）。"""
    result: list[tuple[datetime, dict[str, object]]] = []
    for index_value, row in frame.iterrows():
        stamp = index_value if isinstance(index_value, datetime) else _to_datetime(index_value)
        if stamp is None or stamp.date() <= signal_date:
            continue
        result.append((stamp, {str(key): value for key, value in row.items()}))
        if len(result) >= count:
            break
    return result


def _close_of(bar: Mapping[str, object]) -> float:
    value = bar.get("close")
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return 0.0


def _to_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _maturity_date(
    *, signal_date: date, horizon: int, trading_days: Sequence[date] | None
) -> date | None:
    """成熟日：给了交易日历用日历；否则按自然日近似（≈ horizon×1.5 自然日）。"""
    if trading_days:
        future = [item for item in trading_days if item > signal_date]
        if len(future) < horizon:
            return None
        return future[horizon - 1]
    return signal_date + timedelta(days=int(round(horizon * 1.5)))


def _first_present(
    payload: Mapping[str, object], keys: Sequence[str], *, default: object
) -> object:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return default


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
    "DEFAULT_HORIZONS",
    "NOT_AVAILABLE",
    "DecisionRow",
    "OutcomeMaturity",
    "build_decision_rows",
    "compute_outcomes",
    "decision_path",
    "outcome_path",
    "resolve_decision_root",
    "write_decision_rows",
    "write_manifest",
    "write_outcomes",
]
