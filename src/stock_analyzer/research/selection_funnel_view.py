"""把夜扫半段与尾盘半段的留档拼成**整条选股漏斗**，并如实说明哪些问题现在答不了。

改进计划 §2 要的是一条可追溯链：**全市场 → 硬性资格检查 → Quality300 → Light100 →
Deep50 → 夜间观察池 → 尾盘确认 → 最终推荐 → 成交与退出**。现实是两半分别落在两种留档里：

- 前三层（Quality300/Light100/Deep50）在 ``alpha_v2/validation/production_funnel`` 的
  快照里，只有**成员与计数**，没有逐只拒绝原因；
- 后四层在 ``research/funnel_trace`` 的尾盘留档里，原因、特征、身份、数据时间齐全；
- ``universe`` 与 ``hard_eligibility`` **两侧都没有生产者**。

本模块不新造数据，只做对齐与判定：缺的层标 ``recorded=false``，从计数推的落差不管
成员是否真的是子集都不硬推（``subset_hold=false`` 时不落数），并把 §2 的每个诊断问题
翻译成"凭现有证据能不能回答、缺哪一份留档"。这份判定本身就是要交付的根因事实。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from stock_analyzer.contracts.trend_strategy import FUNNEL_LAYERS

NIGHT_HALF_LAYERS = ("quality_300", "light_100", "deep_50")
TAIL_HALF_LAYERS = tuple(
    stage for stage in FUNNEL_LAYERS
    if stage not in NIGHT_HALF_LAYERS and stage != "universe"
)
UNRECORDED_LAYERS = ("universe", "hard_eligibility")

_MEMBERS_KEY = {
    "quality_300": "quality_members",
    "light_100": "light_members",
    "deep_50": "deep_members",
}
_PREVIOUS_LAYER = {
    "quality_300": "",
    "light_100": "quality_300",
    "deep_50": "light_100",
}

#: §2 列出的诊断问题 → 需要哪一层记录才答得了。判定全部由 ``_answerable`` 算出来，
#: 这张表只负责把问题说成人话。
DIAGNOSIS_QUESTIONS = {
    "screening_too_early": (
        "前置筛选是否过早淘汰了适合短期上涨的股票",
        ("universe", "hard_eligibility"),
    ),
    "duplicated_information": (
        "旧模型、综合分、等级与交叉复核是否反复使用相同信息造成错误排序",
        ("quality_300", "light_100", "deep_50"),
    ),
    "overheated_or_illiquid": (
        "是否偏向已经大幅上涨、波动过高或成交困难的股票",
        ("deep_50", "night_watch_pool"),
    ),
    "data_or_degradation_impact": (
        "数据缺失、旧缓存、特征滞后、模型降级分别影响多少股票",
        ("night_watch_pool", "tail_confirmation"),
    ),
    "where_quality_is_lost": (
        "最终推荐变差来自候选池、预测、排序还是交易规则",
        ("quality_300", "light_100", "deep_50", "night_watch_pool",
         "tail_confirmation", "final_recommendation"),
    ),
}


def _symbols(members: Any) -> list[str]:
    out: list[str] = []
    for row in members or ():
        if isinstance(row, Mapping):
            symbol = str(row.get("symbol", "")).strip()
        else:
            symbol = str(row).strip()
        if symbol:
            out.append(symbol)
    return out


def _night_layer_view(night: Mapping[str, Any], layer: str,
                      previous: Mapping[str, Any]) -> dict[str, Any]:
    members = _symbols(night.get(_MEMBERS_KEY[layer]))
    prev_members = list(previous.get("members", ()))
    subset_hold = bool(prev_members) and set(members).issubset(set(prev_members))
    return {
        "layer": layer,
        "source": "night_scan_funnel_snapshot",
        "recorded": True,
        "reasons_available": False,
        "advanced": len(members),
        "advanced_symbols": members,
        "members": members,
        "inputs": (len(prev_members) if subset_hold else None),
        "dropped": (len(prev_members) - len(members) if subset_hold else None),
        "rejected_by_reason": {},
        "subset_hold": subset_hold if prev_members else None,
        "features_used": [],
        "model_identity": {},
        "data_as_of": str(night.get("signal_date", "") or night.get("trade_date", "")),
        "gaps": ([] if subset_hold or not prev_members else
                 ["membership_is_not_a_subset_of_previous_layer"]),
    }


def _tail_layer_view(stage: Mapping[str, Any]) -> dict[str, Any]:
    rejected_symbols = {
        str(reason): list(symbols)
        for reason, symbols in (stage.get("rejected_symbols") or {}).items()
    }
    inputs = int(stage.get("inputs", 0) or 0)
    advanced = int(stage.get("advanced", 0) or 0)
    return {
        "layer": str(stage.get("stage", "")),
        "source": "funnel_trace",
        "recorded": True,
        # "没有原因"有两种：这层没淘汰任何股票（不是缺口），或淘汰了却没记原因（是缺口）。
        "reasons_available": bool(rejected_symbols) or inputs == advanced,
        "inputs": inputs,
        "advanced": advanced,
        "advanced_symbols": list(stage.get("advanced_symbols") or ()),
        "rejected_by_reason": rejected_symbols,
        "features_used": list(stage.get("features_used") or ()),
        "raw_predictions": dict(stage.get("raw_predictions") or {}),
        "calibrated_probabilities": dict(stage.get("calibrated_probabilities") or {}),
        "model_identity": dict(stage.get("model_identity") or {}),
        "data_as_of": str(stage.get("data_as_of", "") or ""),
        "drop_rate": float(stage.get("drop_rate", 0.0) or 0.0),
    }


def _missing_layer_view(layer: str) -> dict[str, Any]:
    return {
        "layer": layer,
        "source": "none",
        "recorded": False,
        "reasons_available": False,
        "advanced_symbols": [],
        "rejected_by_reason": {},
        "features_used": [],
        "gaps": ["no_producer_records_this_layer"],
    }


def _answerable(layers: Mapping[str, Mapping[str, Any]],
                layers_needed: Iterable[str]) -> bool:
    for layer in layers_needed:
        view = layers.get(layer)
        if view is None or not view.get("recorded"):
            return False
    return True


def build_selection_funnel_view(
    *,
    night_funnel: Mapping[str, Any] | None,
    tail_traces: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """合并两半留档；任何一层缺记录都显式写出来，不折算成 0。"""
    stages_by_name: dict[str, Mapping[str, Any]] = {}
    blocking_reasons: list[str] = []
    trade_dates: list[str] = []
    for trace in tail_traces or ():
        for stage in trace.get("stages") or ():
            if isinstance(stage, Mapping) and stage.get("stage"):
                stages_by_name.setdefault(str(stage["stage"]), stage)
        if trace.get("blocking_reason"):
            blocking_reasons.append(str(trace["blocking_reason"]))
        if trace.get("trade_date"):
            trade_dates.append(str(trace["trade_date"]))

    night = dict(night_funnel or {})
    layers: dict[str, dict[str, Any]] = {}
    previous: Mapping[str, Any] = {}
    for layer in NIGHT_HALF_LAYERS:
        if not night:
            layers[layer] = {**_missing_layer_view(layer),
                              "gaps": ["night_scan_funnel_snapshot_absent"]}
            continue
        view = _night_layer_view(night, layer, previous)
        layers[layer] = view
        previous = view

    for layer in FUNNEL_LAYERS:
        if layer in layers:
            continue
        stage = stages_by_name.get(layer)
        layers[layer] = _tail_layer_view(stage) if stage else _missing_layer_view(layer)

    ordered = [layers[layer] for layer in FUNNEL_LAYERS]
    questions = {
        key: {
            "question": question,
            "answerable_with_current_records": _answerable(layers, needed),
            "requires_layers": list(needed),
        }
        for key, (question, needed) in DIAGNOSIS_QUESTIONS.items()
    }
    unrecorded = [view["layer"] for view in ordered if not view["recorded"]]
    no_reasons = [view["layer"] for view in ordered
                  if view["recorded"] and not view["reasons_available"]]
    return {
        "trade_dates": sorted(set(trade_dates)),
        "night_scan_trade_date": str(night.get("trade_date", "") or ""),
        "layers": ordered,
        "coverage": {
            "layers_total": len(FUNNEL_LAYERS),
            "layers_recorded": len(FUNNEL_LAYERS) - len(unrecorded),
            "layers_without_reasons": no_reasons,
            "layers_unrecorded": unrecorded,
            "final_recommendation_count": len(
                layers.get("final_recommendation", {}).get("advanced_symbols", [])
            ),
        },
        "blocking_reasons": sorted(set(blocking_reasons)),
        "diagnosis": questions,
        "consistent": not any(
            gap for view in ordered for gap in view.get("gaps", ())
            if gap == "membership_is_not_a_subset_of_previous_layer"
        ),
    }


__all__ = [
    "DIAGNOSIS_QUESTIONS",
    "NIGHT_HALF_LAYERS",
    "TAIL_HALF_LAYERS",
    "UNRECORDED_LAYERS",
    "build_selection_funnel_view",
]
