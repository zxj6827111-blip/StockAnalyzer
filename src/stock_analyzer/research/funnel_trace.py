"""选股漏斗分层留档：把"在哪一层损失了选股质量"变成可查的事实。

改进计划 §2 要求给完整漏斗增加可追溯记录，每层记下输入、晋级、拒绝原因、使用的
特征、原始预测、校准概率、实际模型身份与数据时间；并要求"最终推荐单独留档，
关联其特征快照"——此前只有 ``universe_quality_snapshot.json`` 一个候选快照，
它代表不了尾盘确认之后的最终推荐。

两条硬约束：

1. **计数不许自相矛盾**：``inputs == advanced + sum(rejected)``。留档说谎比不留档
   更糟，所以构造即校验、不成立就 raise。
2. **区分硬门与预测性规则**（``kind``）：§2 要逐层移除/替换**预测性规则**做对照，
   而生产安全、交易资格与数据完整性**硬门始终保留**。没有这个标记，消融实验会
   误删 fail-closed 的门。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    FUNNEL_LAYERS,
    FinalRecommendationResult,
    ModelIdentity,
    TrendStrategyContract,
)

KIND_HARD_GATE = "hard_gate"
KIND_PREDICTIVE = "predictive"
_KINDS = frozenset({KIND_HARD_GATE, KIND_PREDICTIVE})

#: 必须留档但允许"记录失败"可见地缺失的字段。缺失不阻断留档，阻断的是"静默缺失"。
MISSING_FEATURE_SNAPSHOT = "feature_snapshot_missing"


class FunnelTraceError(ValueError):
    """留档自身不自洽（计数不符、未知层名、缺时间戳）。"""


@dataclass
class StageTrace:
    """漏斗某一层的完整判定记录。"""

    stage: str
    kind: str
    inputs: int
    advanced: int
    rejected: dict[str, int]
    advanced_symbols: tuple[str, ...]
    rejected_symbols: dict[str, tuple[str, ...]]
    features_used: tuple[str, ...]
    raw_predictions: dict[str, float]
    calibrated_probabilities: dict[str, float]
    model_identity: dict[str, Any]
    data_as_of: str
    feature_compute_version: int
    contract_digest: str
    label_policy_id: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.stage not in FUNNEL_LAYERS:
            raise FunnelTraceError(f"unknown funnel stage {self.stage!r}")
        if self.kind not in _KINDS:
            raise FunnelTraceError(
                f"stage {self.stage} kind must be one of {sorted(_KINDS)}"
            )
        dropped = sum(int(value) for value in self.rejected.values())
        if self.inputs != self.advanced + dropped:
            raise FunnelTraceError(
                f"stage {self.stage} counts do not add up: inputs={self.inputs} != "
                f"advanced={self.advanced} + rejected={dropped}"
            )
        if self.advanced != len(self.advanced_symbols):
            raise FunnelTraceError(
                f"stage {self.stage} advanced count {self.advanced} != "
                f"symbol list size {len(self.advanced_symbols)}"
            )
        for reason, symbols in self.rejected_symbols.items():
            if int(self.rejected.get(reason, -1)) != len(symbols):
                raise FunnelTraceError(
                    f"stage {self.stage} reason {reason!r} count does not match its "
                    f"symbol list ({self.rejected.get(reason)} vs {len(symbols)})"
                )
        if not str(self.data_as_of).strip():
            raise FunnelTraceError(f"stage {self.stage} must record data_as_of")

    @property
    def drop_rate(self) -> float:
        if self.inputs <= 0:
            return 0.0
        return 1.0 - (self.advanced / self.inputs)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["advanced_symbols"] = list(self.advanced_symbols)
        payload["rejected_symbols"] = {
            reason: list(symbols) for reason, symbols in self.rejected_symbols.items()
        }
        payload["features_used"] = list(self.features_used)
        payload["drop_rate"] = self.drop_rate
        return payload


@dataclass
class FinalRecommendationRow:
    """一条最终推荐留档：把"推荐了什么"与"凭什么推荐、成交得没成"绑在一起。"""

    symbol: str
    rank: int
    probability: float | None
    reference_notional: float
    strategy: str
    contract_version: str
    contract_digest: str
    probability_field: str
    data_as_of: str
    model_identity: dict[str, Any]
    feature_snapshot: dict[str, Any]
    fill: dict[str, Any]
    caveats: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["caveats"] = list(self.caveats)
        return payload


@dataclass
class FunnelTrace:
    """一个交易日一条完整漏斗留档。"""

    trade_date: date
    strategy: str
    stages: tuple[StageTrace, ...]
    final_recommendations: tuple[FinalRecommendationRow, ...]
    rejected_final: tuple[dict[str, Any], ...] = ()
    blocking_reason: str = ""
    contract_digest: str = DEFAULT_TREND_CONTRACT.digest()

    def stage(self, name: str) -> StageTrace | None:
        for item in self.stages:
            if item.stage == name:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "strategy": self.strategy,
            "stages": [item.as_dict() for item in self.stages],
            "final_recommendations": [item.as_dict() for item in self.final_recommendations],
            "rejected_final": [dict(item) for item in self.rejected_final],
            "blocking_reason": self.blocking_reason,
            "contract_digest": self.contract_digest,
            "digest": self.digest(),
        }

    def digest(self) -> str:
        payload = {
            "trade_date": self.trade_date.isoformat(),
            "strategy": self.strategy,
            "stages": [item.as_dict() for item in self.stages],
            "final": [row.symbol for row in self.final_recommendations],
            "contract_digest": self.contract_digest,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def record_stage(
    *,
    stage: str,
    kind: str,
    input_symbols: Sequence[str],
    advanced_symbols: Sequence[str],
    rejected: Mapping[str, Sequence[str]] | None = None,
    features_used: Sequence[str] = (),
    raw_predictions: Mapping[str, float] | None = None,
    calibrated_probabilities: Mapping[str, float] | None = None,
    model_identity: ModelIdentity | Mapping[str, Any] | None = None,
    data_as_of: str,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    label_policy_id: str = "",
    feature_compute_version: int = 0,
    notes: str = "",
) -> StageTrace:
    """把一层的输入/晋级/拒绝落成自洽记录；计数不自洽直接 raise。"""
    rejected_map = {
        str(reason): tuple(str(symbol) for symbol in symbols)
        for reason, symbols in (rejected or {}).items()
    }
    advanced_tuple = tuple(str(symbol) for symbol in advanced_symbols)
    return StageTrace(
        stage=str(stage),
        kind=str(kind),
        inputs=len(input_symbols),
        advanced=len(advanced_tuple),
        rejected={reason: len(symbols) for reason, symbols in rejected_map.items()},
        rejected_symbols=rejected_map,
        advanced_symbols=advanced_tuple,
        features_used=tuple(str(name) for name in features_used),
        raw_predictions={str(k): float(v) for k, v in (raw_predictions or {}).items()},
        calibrated_probabilities={
            str(k): float(v) for k, v in (calibrated_probabilities or {}).items()
        },
        model_identity=_identity_payload(model_identity),
        data_as_of=str(data_as_of),
        feature_compute_version=int(feature_compute_version),
        contract_digest=contract.digest(),
        label_policy_id=str(label_policy_id),
        notes=notes,
    )


def _identity_payload(model_identity: ModelIdentity | Mapping[str, Any] | None) -> dict[str, Any]:
    if model_identity is None:
        return {"identity_recorded": False, "reason": "model_identity_not_supplied"}
    if isinstance(model_identity, ModelIdentity):
        payload = asdict(model_identity)
    else:
        payload = dict(model_identity)
    payload["identity_recorded"] = True
    return payload


def archive_final_recommendations(
    *,
    result: FinalRecommendationResult,
    feature_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
    model_identity: ModelIdentity | Mapping[str, Any] | None,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    fills: Mapping[str, Mapping[str, Any]] | None = None,
    probability_field: str = "p_net_profit_5d_tail",
) -> tuple[tuple[FinalRecommendationRow, ...], tuple[dict[str, Any], ...]]:
    """把最终推荐单独留档并关联特征快照；无快照/无成交都写成 caveat，不静默省略。"""
    snapshots = dict(feature_snapshots or {})
    identity = _identity_payload(model_identity)
    fill_map = dict(fills or {})
    rows: list[FinalRecommendationRow] = []
    for rank, candidate in enumerate(result.selected, start=1):
        snapshot = snapshots.get(candidate.symbol)
        caveats: list[str] = []
        # 空 dict 与缺失等价：没有特征的"快照"代表不了这条推荐凭什么被选出。
        if not snapshot:
            caveats.append(MISSING_FEATURE_SNAPSHOT)
        fill = fill_map.get(candidate.symbol)
        if fill is None:
            caveats.append("fill_status_missing")
        rows.append(FinalRecommendationRow(
            symbol=candidate.symbol,
            rank=rank,
            probability=candidate.probability,
            reference_notional=float(contract.reference_notional),
            strategy=contract.strategy,
            contract_version=contract.contract_version,
            contract_digest=contract.digest(),
            probability_field=probability_field,
            data_as_of=str(candidate.details.get("data_as_of") or result.trade_date),
            model_identity=identity,
            feature_snapshot=dict(snapshot or {}),
            fill=dict(fill or {}),
            caveats=tuple(caveats),
        ))
    rejected = [
        {"symbol": item.symbol, "reason": item.reason, "probability": item.probability,
         "stage": "final_recommendation"}
        for item in result.rejected
    ]
    return tuple(rows), tuple(rejected)


def build_funnel_trace(
    *,
    trade_date: date | str,
    stages: Sequence[StageTrace],
    final_recommendations: Sequence[FinalRecommendationRow] = (),
    rejected_final: Sequence[Mapping[str, Any]] = (),
    blocking_reason: str = "",
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> FunnelTrace:
    day = _to_date(trade_date)
    ordered: list[StageTrace] = []
    seen: set[str] = set()
    for stage in FUNNEL_LAYERS:
        for item in stages:
            if item.stage == stage and stage not in seen:
                ordered.append(item)
                seen.add(stage)
    unknown = [item.stage for item in stages if item.stage not in seen]
    if unknown:
        raise FunnelTraceError(
            f"stages not part of the declared funnel: {unknown}; extend FUNNEL_LAYERS "
            "explicitly instead of leaving them out of the ordering"
        )
    return FunnelTrace(
        trade_date=day,
        strategy=contract.strategy,
        stages=tuple(ordered),
        final_recommendations=tuple(final_recommendations),
        rejected_final=tuple(dict(item) for item in rejected_final),
        blocking_reason=blocking_reason,
        contract_digest=contract.digest(),
    )


def _to_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def write_trace(
    trace: FunnelTrace,
    directory: Path | str,
    *,
    suffix: str = "",
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> Path:
    """落一条漏斗留档。

    ``suffix`` 给"同一个交易日、不同时间成熟"的层用（例如成交与退出要等 5 个交易日
    才知道结果）：它必须落到**另一个文件**，不能回头覆盖入场那天已经写好的留档。

    ``written_at`` 必须是带时区的时刻并写明用的是哪个时区（计划 §3.1"修复新记录的时区"）。
    裸 ``datetime.now()`` 会跟着宿主机的 UTC 偏移变，NAS 上跑出来的留档和本地对不上，
    而留档正是影子验证唯一的证据来源。
    """
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{suffix}" if str(suffix or "").strip() else ""
    path = out_dir / f"funnel_trace_{trace.trade_date.isoformat()}{tag}.json"
    written_at = datetime.now(ZoneInfo(contract.timezone))
    payload = dict(
        trace.as_dict(),
        written_at=written_at.isoformat(timespec="seconds"),
        written_at_timezone=contract.timezone,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return path


def read_trace(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def diagnose_funnel(traces: Iterable[FunnelTrace]) -> dict[str, Any]:
    """跨日聚合每层的淘汰率与拒绝原因，定位质量损失发生在哪一层。

    只报告"哪一层淘汰了多少、为什么"，不推断因果——因果需要 ``compare_traces``
    的对照组（逐层移除预测性规则）才能支撑。
    """
    items = list(traces)
    per_stage: dict[str, dict[str, Any]] = {}
    journeys: dict[str, dict[str, Any]] = {}
    for trace in items:
        for stage in trace.stages:
            bucket = per_stage.setdefault(stage.stage, {
                "kind": stage.kind,
                "days": 0,
                "inputs": 0,
                "advanced": 0,
                "rejected_by_reason": {},
                "model_identity_missing_days": 0,
            })
            bucket["days"] += 1
            bucket["inputs"] += stage.inputs
            bucket["advanced"] += stage.advanced
            for reason, count in stage.rejected.items():
                bucket["rejected_by_reason"][reason] = (
                    bucket["rejected_by_reason"].get(reason, 0) + count
                )
            if not stage.model_identity.get("identity_recorded"):
                bucket["model_identity_missing_days"] += 1
        for stage in trace.stages:
            for reason, symbols in stage.rejected_symbols.items():
                for symbol in symbols:
                    journeys.setdefault(symbol, {}).setdefault("dropped_at", stage.stage)
                    journeys[symbol].setdefault("drop_reasons", []).append(reason)
        for row in trace.final_recommendations:
            journeys.setdefault(row.symbol, {})["recommended"] = True
            journeys[row.symbol]["fill"] = bool(row.fill)

    for bucket in per_stage.values():
        inputs = bucket["inputs"]
        bucket["drop_rate"] = (1.0 - bucket["advanced"] / inputs) if inputs else 0.0
        bucket["top_reject_reasons"] = dict(sorted(
            bucket["rejected_by_reason"].items(), key=lambda kv: (-kv[1], kv[0])
        )[:8])
    return {
        "days": len(items),
        "per_stage": per_stage,
        "stage_order": [stage for stage in FUNNEL_LAYERS if stage in per_stage],
        "predictive_stages": sorted(
            name for name, bucket in per_stage.items()
            if bucket["kind"] == KIND_PREDICTIVE
        ),
        "hard_gate_stages": sorted(
            name for name, bucket in per_stage.items()
            if bucket["kind"] == KIND_HARD_GATE
        ),
        "distinct_symbols_seen": len(journeys),
        "symbols_recommended": sum(
            1 for meta in journeys.values() if meta.get("recommended")
        ),
        "blocking_days": [str(trace.trade_date) for trace in items if trace.blocking_reason],
    }


def compare_traces(
    *,
    baseline: Sequence[FunnelTrace],
    variant: Sequence[FunnelTrace],
    metric: str = "final_fill_net_profit_rate",
) -> dict[str, Any]:
    """对照两组留档（例如"移除某个预测性加分层"）在同一批交易日上的结果差异。

    要求两边的交易日集合完全一致——否则是"换了样本"而不是"换了规则"。
    """
    base_days = {trace.trade_date for trace in baseline}
    variant_days = {trace.trade_date for trace in variant}
    if base_days != variant_days:
        only_base = sorted(str(day) for day in base_days - variant_days)
        only_variant = sorted(str(day) for day in variant_days - base_days)
        raise FunnelTraceError(
            f"对照要求交易日集合一致；仅基线有 {only_base[:5]}，仅对照有 "
            f"{only_variant[:5]}"
        )
    base_rows = [row for trace in baseline for row in trace.final_recommendations]
    variant_rows = [row for trace in variant for row in trace.final_recommendations]
    base_stats = _variant_stats(base_rows)
    variant_stats = _variant_stats(variant_rows)
    delta: dict[str, Any] = {}
    for key, new_value in variant_stats.items():
        old_value = base_stats[key]
        if isinstance(new_value, (int, float)) and isinstance(old_value, (int, float)):
            delta[key] = float(new_value) - float(old_value)
    return {
        "metric": metric,
        "days": len(base_days),
        "baseline": base_stats,
        "variant": variant_stats,
        "delta": delta,
    }


def _variant_stats(rows: Sequence[FinalRecommendationRow]) -> dict[str, Any]:
    filled = [row for row in rows if row.fill]
    realized = [row for row in filled if row.fill.get("realized")]
    profits = [row for row in realized if row.fill.get("net_profit")]
    return {
        "recommendations": len(rows),
        "filled": len(filled),
        "realized": len(realized),
        "net_profit_rate": (len(profits) / len(realized)) if realized else None,
        "mean_net_return": (
            sum(float(row.fill["net_return"]) for row in realized) / len(realized)
            if realized else None
        ),
    }


__all__ = [
    "FunnelTrace",
    "FunnelTraceError",
    "FinalRecommendationRow",
    "KIND_HARD_GATE",
    "KIND_PREDICTIVE",
    "MISSING_FEATURE_SNAPSHOT",
    "StageTrace",
    "archive_final_recommendations",
    "build_funnel_trace",
    "compare_traces",
    "diagnose_funnel",
    "read_trace",
    "record_stage",
    "write_trace",
]
