"""trend 尾盘影子链路：把契约接进运行时，但**不接管**旧推荐输出。

改进计划 §3.4 要求夜间观察池在次日尾盘复核后产出最多 3 只最终推荐；§4/§5 同时要求
生产切换在证据达标后单独发布、保留旧路径回滚。所以本服务是**影子**的：它算自己的结果、
写自己的留档，不改 ``actionable_signals``，旧路径完全不受影响。

它也是 fail-closed 的示范——在满足下列任一条件时输出 **0 只**并写明原因，绝不补名额、
绝不改用开盘价顶替：

- 在服模型身份无法验证（缺 hash / commit 不一致 / 不是本契约的 label policy）；
- 观察池当日没有带时刻的分钟行情（本项目当前的真实状态）；
- 行情快照陈旧、停牌、涨停锁死、风险不允许；
- 没有一只达到准入阈值。

这正是 §4 要的"未来影子验证"证据来源：逐日留档的候选、最终推荐与成交状态。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    ModelIdentity,
    RankedCandidate,
    TailEntryDecision,
    TrendStrategyContract,
    evaluate_tail_entry,
    rank_final_recommendations,
)
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    KIND_PREDICTIVE,
    archive_final_recommendations,
    build_funnel_trace,
    record_stage,
    write_trace,
)

REPORT_DIR_DEFAULT = "artifacts/runtime/trend_tail_shadow"


class TrendTailShadowService:
    """观察池 → 尾盘确认 → 最终推荐 的影子执行与留档。"""

    def __init__(
        self,
        service: Any,
        *,
        contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
        report_dir: str | Path | None = None,
    ) -> None:
        self._service = service
        self._contract = contract
        raw_dir = str(report_dir or REPORT_DIR_DEFAULT)
        resolver = getattr(service, "_resolve_evolution_path", None)
        self._report_dir = Path(resolver(raw_dir)) if callable(resolver) else Path(raw_dir)

    def run(
        self,
        *,
        timestamp: datetime | None,
        watch_pool: Sequence[Mapping[str, Any]],
        minute_bars: Mapping[str, Sequence[tuple[datetime, Mapping[str, Any]]]] | None = None,
        probabilities: Mapping[str, float] | None = None,
        capital_budget: float | None = None,
        cost_estimator: Any | None = None,
        price_ticker: Any | None = None,
        slippage_ratio: float = 0.0,
        write_artifacts: bool = True,
    ) -> dict[str, Any]:
        """跑一次尾盘确认。``timestamp`` 为 None 时按历史模式（不做陈旧度门）。"""
        contract = self._contract
        day = timestamp.date() if isinstance(timestamp, datetime) else None
        rows = [dict(row) for row in watch_pool if str(row.get("symbol", "")).strip()]
        bar_map = dict(minute_bars or {})
        prob_map = dict(probabilities or {})

        identity, identity_error = self._resolve_model_identity(prob_map)
        rejections: dict[str, list[str]] = {}
        decisions: dict[str, TailEntryDecision] = {}

        if day is None:
            rejections.setdefault("no_trade_date", []).extend(
                sorted(str(row["symbol"]) for row in rows)
            )
        elif identity_error:
            # 身份不可验证：整轮直接 0 只。逐只确认也不做，避免用没核实的模型分数下单。
            rejections.setdefault(identity_error, []).extend(
                sorted(str(row.get("symbol")) for row in rows)
            )
        else:
            for row in rows:
                symbol = str(row.get("symbol", "")).strip()
                bars = list(bar_map.get(symbol, ()))
                if not bars:
                    rejections.setdefault("minute_bars_unavailable", []).append(symbol)
                    continue
                decisions[symbol] = evaluate_tail_entry(
                    symbol=symbol,
                    trading_day=day,
                    minute_bars=bars,
                    confirmation=_hard_gate_confirmation,
                    contract=contract,
                    quote_as_of=timestamp,
                    model_probabilities={NET_PROFIT_PROBABILITY_FIELD: prob_map.get(symbol)}
                    if symbol in prob_map
                    else None,
                    overnight_features={
                        key: value
                        for key, value in row.items()
                        if key not in {"symbol", "features"}
                    },
                    slippage_ratio=slippage_ratio,
                    cost_estimator=cost_estimator,
                    price_ticker=price_ticker,
                )
            for symbol, decision in decisions.items():
                if decision.confirmed and decision.no_fill_reason:
                    rejections.setdefault(decision.no_fill_reason, []).append(symbol)
                elif not decision.confirmed:
                    rejections.setdefault(
                        decision.reason or "tail_confirmation_failed", []
                    ).append(symbol)

        filled_symbols = sorted(
            symbol for symbol, decision in decisions.items() if decision.filled
        )
        budget = contract.reference_notional * contract.max_final_recommendations
        if capital_budget is not None:
            budget = float(capital_budget)
        affordable = max(0, int(budget // contract.reference_notional))
        budget_block = "capital_budget_exhausted" if affordable == 0 else ""

        candidate_rows = [
            {
                "symbol": symbol,
                NET_PROFIT_PROBABILITY_FIELD: prob_map.get(symbol),
                "tradeable": symbol in filled_symbols,
                "not_tradeable_reason": (
                    decisions[symbol].no_fill_reason
                    if symbol in decisions and not decisions[symbol].filled
                    else "not_confirmed"
                ),
                "risk_state": str(
                    _row_for(rows, symbol).get("risk_state", "") or ""
                ),
                "data_as_of": (
                    decisions[symbol].fill_time.isoformat()
                    if symbol in decisions and decisions[symbol].fill_time
                    else ""
                ),
            }
            for symbol in sorted({str(row.get("symbol")) for row in rows})
        ]
        # 资金只够 k 只就把已选出的结果截到 k。**不能改契约再排**：改后的契约摘要会
        # 让模型身份校验失败，那会把"钱不够"错报成"模型不可信"。
        result = rank_final_recommendations(
            trade_date=day or date.today(),
            rows=[] if budget_block else candidate_rows,
            model_identity=identity,
            contract=contract,
            probability_field=NET_PROFIT_PROBABILITY_FIELD,
        )
        if not budget_block:
            result = _apply_cap(result, affordable)
        archive_rows, rejected_final = archive_final_recommendations(
            result=result,
            feature_snapshots={
                str(row.get("symbol")): dict(row.get("features") or {})
                for row in rows
            },
            model_identity=identity,
            contract=contract,
            fills={
                symbol: {
                    "filled": decisions[symbol].filled,
                    "fill_time": decisions[symbol].fill_time.isoformat()
                    if decisions[symbol].fill_time
                    else None,
                    "quantity": decisions[symbol].quantity,
                    "entry_amount": decisions[symbol].entry_amount,
                    "buy_cost": decisions[symbol].buy_cost,
                }
                for symbol in decisions
            },
            probability_field=NET_PROFIT_PROBABILITY_FIELD,
        )

        all_symbols = sorted({str(row.get("symbol")) for row in rows})
        attempted = sorted(decisions)
        filled = filled_symbols
        by_symbol_reason = _reason_map(rows, decisions, rejections)

        stages = [
            _stage(
                stage="night_watch_pool",
                kind=KIND_PREDICTIVE,
                inputs=all_symbols,
                advanced=attempted,
                reason_map=by_symbol_reason,
                features_used=sorted(
                    {key for row in rows for key in (row.get("features") or {})}
                ),
                timestamp=timestamp, identity=identity, contract=contract,
            ),
            _stage(
                stage="tail_confirmation",
                kind=KIND_HARD_GATE,
                inputs=attempted,
                advanced=filled,
                reason_map=by_symbol_reason,
                timestamp=timestamp, identity=identity, contract=contract,
            ),
            _stage(
                stage="final_recommendation",
                kind=KIND_PREDICTIVE,
                inputs=filled,
                advanced=list(result.symbols),
                reason_map={
                    symbol: (item.reason or "not_selected")
                    for item, symbol in ((item, item.symbol)
                                          for item in result.rejected)
                },
                timestamp=timestamp, identity=identity, contract=contract,
            ),
        ]
        trace = build_funnel_trace(
            trade_date=day or date.today(),
            stages=stages,
            final_recommendations=archive_rows,
            rejected_final=rejected_final,
            blocking_reason=(budget_block or identity_error or result.blocking_reason),
            contract=contract,
        )
        report: dict[str, Any] = {
            "ok": True,
            "mode": "shadow",
            "trade_date": (day or date.today()).isoformat(),
            "contract_version": contract.contract_version,
            "contract_digest": contract.digest(),
            "probability_field": NET_PROFIT_PROBABILITY_FIELD,
            "reference_notional": float(contract.reference_notional),
            "watch_pool_size": len(rows),
            "confirmed": sum(1 for d in decisions.values() if d.confirmed),
            "filled": len(filled_symbols),
            "final_recommendations": [row.as_dict() for row in archive_rows],
            "final_symbols": list(result.symbols),
            "rejected_reasons": {key: sorted(set(value)) for key, value in rejections.items()},
            "final_rejections": _count_by_symbol(rejected_final),
            "blocking_reason": (
                budget_block or identity_error or result.blocking_reason
            ),
            "capital_budget": float(budget),
            "model_identity": {
                "recorded": bool(identity is not None and not identity_error),
                "error": identity_error or result.blocking_reason,
            },
            "counts": dict(result.counts),
            "max_recommendations_effective": int(
                min(affordable, contract.max_final_recommendations)
            ),
            "trace_digest": trace.digest(),
        }
        if write_artifacts:
            report["artifact_paths"] = self._write(trace, report)
        return report

    def _resolve_model_identity(
        self, probabilities: Mapping[str, float]
    ) -> tuple[ModelIdentity | None, str]:
        """在服模型必须**就是**本契约的净盈利模型，否则身份按不可验证处理。"""
        if not probabilities:
            return None, "no_tail_probability_available"
        manifest = _read_serving_manifest(self._service)
        if not manifest:
            return None, "serving_manifest_missing"
        label_policy_id = str(manifest.get("label_policy_id", "") or "")
        if not label_policy_id.startswith("label_policy_v4_"):
            return None, "serving_model_is_not_tail_label_policy"
        identity = ModelIdentity(
            model_id=str(manifest.get("model_id", "") or manifest.get("artifact_path", "") or ""),
            artifact_content_hash=str(
                manifest.get("artifact_content_hash", "")
                or manifest.get("authoritative_content_hash", "")
                or ""
            ),
            training_commit=str(
                manifest.get("code_commit", "") or manifest.get("commit", "") or ""
            ),
            runtime_commit=str(_runtime_commit(self._service)),
            feature_compute_version=_feature_compute_version(),
            label_policy_id=label_policy_id,
            contract_digest=self._contract.digest(),
        )
        return identity, identity.validate(self._contract)

    def _write(self, trace: Any, report: Mapping[str, Any]) -> dict[str, str]:
        self._report_dir.mkdir(parents=True, exist_ok=True)
        trace_path = str(write_trace(trace, self._report_dir))
        report_path = self._report_dir / f"tail_shadow_report_{trace.trade_date}.json"
        report_path.write_text(
            json.dumps(dict(report), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return {"funnel_trace": trace_path, "shadow_report": str(report_path)}


def _count_by_symbol(rejected: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in rejected:
        grouped.setdefault(str(item.get("reason") or "not_selected"), []).append(
            str(item.get("symbol"))
        )
    return {key: sorted(values) for key, values in sorted(grouped.items())}


def page_view(report: Mapping[str, Any]) -> dict[str, Any]:
    """页面视图：候选、最终推荐、成交状态分列，并标明概率口径 / 参考金额 / 数据日期。

    计划 §3.4 要求"注明概率所对应的策略、参考金额及数据日期"——这些是响应体里的
    必填元信息，缺失即报错，而不是让页面自己猜。
    """
    for key in ("trade_date", "probability_field", "reference_notional", "contract_version",
                "contract_digest"):
        if report.get(key) in (None, ""):
            raise ValueError(f"tail shadow report is missing required field {key!r}")
    rows = [dict(item) for item in report.get("final_recommendations") or []]
    rejections = dict(report.get("final_rejections") or {})
    rejected_reasons = dict(report.get("rejected_reasons") or {})
    candidates = sorted({
        *[str(row.get("symbol")) for row in rows],
        *[symbol for values in rejections.values() for symbol in values],
        *[symbol for values in rejected_reasons.values() for symbol in values],
    })
    return {
        "status": "ok" if report.get("ok", True) else "error",
        "mode": str(report.get("mode", "shadow")),
        "meta": {
            "trade_date": str(report["trade_date"]),
            "probability_field": str(report["probability_field"]),
            "probability_meaning": (
                "按该契约成交并扣除佣金/最低佣金/过户费/印花税/滑点后，"
                "持有至多 5 个交易日净收益>0 的概率"
            ),
            "strategy": str(report.get("strategy", "trend")),
            "reference_notional_cny": float(report["reference_notional"]),
            "contract_version": str(report["contract_version"]),
            "contract_digest": str(report["contract_digest"]),
            "entry_window": list(report.get("tail_entry_window") or ["14:30", "14:50"]),
            "min_net_profit_probability": report.get("min_net_profit_probability"),
            "data_as_of": str(
                (rows[0].get("data_as_of") if rows else report.get("trade_date")) or ""
            ),
        },
        "candidates": candidates,
        "final_recommendations": rows,
        "fills": {
            str(row.get("symbol")): dict(row.get("fill") or {})
            for row in rows
        },
        "rejection_reasons": {
            "final_ranking": rejections,
            "pre_confirmation": rejected_reasons,
        },
        "blocking_reason": str(report.get("blocking_reason") or ""),
        "caveats": sorted({
            caveat
            for row in rows
            for caveat in (row.get("caveats") or [])
        }),
    }


def _report_dir_for(service: Any) -> Path:
    raw = str(getattr(
        getattr(getattr(service, "_config", None), "week5", None),
        "tail_shadow_report_dir", REPORT_DIR_DEFAULT,
    ) or REPORT_DIR_DEFAULT)
    resolver = getattr(service, "_resolve_evolution_path", None)
    return Path(resolver(raw)) if callable(resolver) else Path(raw)


def tail_shadow_page(service: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    """读某日（默认最新）的尾盘影子留档并转成页面视图。"""
    directory = _report_dir_for(service)
    pattern = (f"tail_shadow_report_{trade_date}.json" if trade_date
               else "tail_shadow_report_*.json")
    files = sorted(directory.glob(pattern))
    if not files:
        return {"status": "no_report", "meta": {"report_dir": str(directory)},
                "candidates": [], "final_recommendations": [], "fills": {},
                "rejection_reasons": {}, "blocking_reason": "no_tail_shadow_report"}
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    return page_view(payload)


def tail_shadow_history(service: Any, *, limit: int = 20) -> dict[str, Any]:
    directory = _report_dir_for(service)
    files = sorted(directory.glob("tail_shadow_report_*.json"))[-max(1, int(limit)):]
    days = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        view = page_view(payload)
        days.append({
            "trade_date": view["meta"]["trade_date"],
            "final_symbols": [row.get("symbol") for row in view["final_recommendations"]],
            "candidate_count": len(view["candidates"]),
            "blocking_reason": view["blocking_reason"],
        })
    return {"days": days, "count": len(days)}


def _reason_map(
    rows: Sequence[Mapping[str, Any]],
    decisions: Mapping[str, TailEntryDecision],
    rejections: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """符号 → 它**最早**被挡下的原因，供逐层留档分组。"""
    reasons: dict[str, str] = {}
    for reason, symbols in rejections.items():
        for symbol in symbols:
            reasons.setdefault(str(symbol), str(reason))
    for symbol, decision in decisions.items():
        if decision.filled:
            continue
        reasons[symbol] = decision.no_fill_reason or decision.reason or "not_filled"
    return reasons


def _stage(
    *,
    stage: str,
    kind: str,
    inputs: Sequence[str],
    advanced: Sequence[str],
    reason_map: Mapping[str, str],
    timestamp: datetime | None,
    identity: ModelIdentity | None,
    contract: TrendStrategyContract,
    features_used: Sequence[str] = (),
) -> Any:
    """一层留档：只统计本层的输入，被挡住的原因按符号归组。"""
    input_set = {str(symbol) for symbol in inputs}
    kept = {str(symbol) for symbol in advanced}
    rejected: dict[str, list[str]] = {}
    for symbol in sorted(input_set - kept):
        rejected.setdefault(str(reason_map.get(symbol, "dropped")), []).append(symbol)
    return record_stage(
        stage=stage,
        kind=kind,
        input_symbols=sorted(input_set),
        advanced_symbols=tuple(sorted(kept & input_set)),
        rejected=rejected,
        features_used=features_used,
        data_as_of=(timestamp or datetime.min).isoformat(),
        contract=contract,
        model_identity=identity,
        feature_compute_version=_feature_compute_version(),
        label_policy_id=getattr(identity, "label_policy_id", "") if identity else "",
    )


def _hard_gate_confirmation(context: Any) -> tuple[bool, str]:
    """确认谓词的默认实现：**只用硬门**。

    新路径不让旧综合分/等级/分歧试探决定资格（计划 §3.4）；模型分只参与最终排序。
    有确认可用的最新价即通过硬门复核，其余判定留给 ``rank_final_recommendations``。
    """
    if context.latest_price_raw is None:
        return False, "no_completed_minute_bar"
    return True, ""


def _apply_cap(result: Any, affordable: int) -> Any:
    """把名额收紧到资金允许的只数，多出来的记成 capital_budget_cap 而不是丢掉。"""
    if affordable >= len(result.selected):
        return result
    kept = result.selected[:max(0, affordable)]
    dropped = tuple(
        RankedCandidate(
            symbol=item.symbol, probability=item.probability,
            reason="capital_budget_cap", accepted=False, details=item.details,
        )
        for item in result.selected[max(0, affordable):]
    )
    counts = dict(result.counts)
    counts["selected"] = len(kept)
    counts["rejected"] = len(result.rejected) + len(dropped)
    return replace(result, selected=kept, rejected=result.rejected + dropped,
                   counts=counts)


def _row_for(rows: Sequence[Mapping[str, Any]], symbol: str) -> Mapping[str, Any]:
    for row in rows:
        if str(row.get("symbol")) == symbol:
            return row
    return {}


def _trim(
    rejections: Mapping[str, Sequence[str]], *, exclude: tuple[str, ...]
) -> dict[str, list[str]]:
    return {
        str(reason): sorted(set(symbols))
        for reason, symbols in rejections.items()
        if str(reason) not in exclude and symbols
    }


def _read_serving_manifest(service: Any) -> dict[str, Any]:
    reader = getattr(service, "_read_serving_manifest", None)
    if callable(reader):
        try:
            return dict(reader() or {})
        except Exception:  # noqa: BLE001 - 读不到就是身份不明，交给上层 fail-closed
            return {}
    try:
        from stock_analyzer.models.serving_manifest import read_serving_manifest

        config = getattr(service, "_config", None)
        path = getattr(getattr(config, "training", None), "serving_manifest_path", None)
        if not path:
            return {}
        resolved = getattr(service, "_resolve_evolution_path", lambda value: value)(path)
        return dict(read_serving_manifest(Path(resolved)) or {})
    except Exception:  # noqa: BLE001
        return {}


def _runtime_commit(service: Any) -> str:
    getter = getattr(service, "_runtime_code_commit", None)
    if callable(getter):
        return str(getter() or "")
    state = getattr(service, "_state", None)
    return str(getattr(state, "code_commit", "") or "")


def _feature_compute_version() -> int:
    try:
        from stock_analyzer.feature.engineer import FEATURE_COMPUTE_VERSION

        return int(FEATURE_COMPUTE_VERSION)
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "REPORT_DIR_DEFAULT",
    "TrendTailShadowService",
    "page_view",
    "tail_shadow_history",
    "tail_shadow_page",
]
