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
import os
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
    TrendContractError,
    TrendStrategyContract,
    evaluate_tail_entry,
    hard_gate_confirmation,
    rank_final_recommendations,
)
from stock_analyzer.labels.tail_net_profit import verify_tail_label_policy
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    KIND_PREDICTIVE,
    archive_final_recommendations,
    build_funnel_trace,
    record_stage,
    write_trace,
)
from stock_analyzer.research.night_scan_funnel_trace import (
    NIGHT_TRACE_SUFFIX,
    build_night_scan_funnel_trace,
)
from stock_analyzer.research.shadow_evidence import (
    CAPTURE_MODE_OBSERVED,
    shadow_readiness,
    summarize_shadow_evidence,
    trace_paths,
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

    def record_night_scan(
        self,
        report: Mapping[str, Any],
        *,
        trade_date: date | datetime | str,
        model_identity: ModelIdentity | Mapping[str, Any] | None = None,
        features_used: Sequence[str] = (),
    ) -> dict[str, Any]:
        """夜扫一跑完就把 Quality300/Light100/Deep50 落成与尾盘半段同构的留档。

        只写证据，不参与任何选股判定；但**写失败要回显在返回值里**，不能静默——
        "哪一层没有留档"本身就是改进计划 §2 要交付的根因事实。
        夜扫与尾盘是两个时刻，两份留档分文件（``_night`` 后缀），互不覆盖。
        """
        try:
            trace = build_night_scan_funnel_trace(
                report=report,
                trade_date=trade_date,
                contract=self._contract,
                model_identity=model_identity,
                features_used=features_used,
            )
        except Exception as exc:  # noqa: BLE001 - 证据留档不得炸掉夜扫
            return {"emitted": False, "reason": f"night_trace_failed:{type(exc).__name__}"}
        if trace is None:
            return {"emitted": False, "reason": "night_scan_report_has_no_funnel_members"}
        path = write_trace(trace, self._report_dir, suffix=NIGHT_TRACE_SUFFIX,
                           contract=self._contract)
        return {
            "emitted": True,
            "path": str(path),
            "trade_date": trace.trade_date.isoformat(),
            "layers": [item.stage for item in trace.stages],
        }

    def shadow_readiness_summary(self) -> dict[str, Any]:
        """把发布清单 R12 的门槛输入从留档目录里数出来（只读，不参与本轮任何判定）。

        只算 ``observed_snapshot`` 口径：事后重算的样本与系统当时的真实打分差一个量级，
        混进同一个分子分母得到的门槛读数没有意义。目录里一份留档都没有时不抛错，
        但必须写明"还没有留档"——否则 0 会被读成"这 60 天都合格"。
        """
        base: dict[str, Any] = {
            "capture_mode": CAPTURE_MODE_OBSERVED,
            "trace_dir": str(self._report_dir),
        }
        paths = trace_paths(self._report_dir)
        if not paths:
            return {
                **base,
                "trace_files_seen": 0,
                "observed_trade_days": 0,
                "matured_simulated_fills": 0,
                "note": "no_funnel_traces_written_yet",
                "readiness": shadow_readiness(observed_trade_days=0,
                                             matured_simulated_fills=0),
            }
        try:
            return summarize_shadow_evidence(paths, capture_mode=CAPTURE_MODE_OBSERVED)
        except (OSError, ValueError) as exc:
            # 数不出来不等于达标，也等于不达标：把原因写清楚，门槛仍按 0 计。
            return {
                **base,
                "trace_files_seen": len(paths),
                "observed_trade_days": 0,
                "matured_simulated_fills": 0,
                "note": f"shadow_evidence_unreadable:{type(exc).__name__}",
                "readiness": shadow_readiness(observed_trade_days=0,
                                             matured_simulated_fills=0),
            }

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
        trade_date: date | str | None = None,
    ) -> dict[str, Any]:
        """跑一次尾盘确认。

        两种模式共用同一套判定，唯一差别是有没有行情陈旧度门：

        - **线上**：传 ``timestamp``（当前时钟），只评估已经到点的确认槽。
        - **历史重算**：``timestamp=None`` + 显式 ``trade_date``，不做陈旧度门。

        ``trade_date`` 不接受 ``date.today()`` 兜底：历史重算把留档盖成"今天"
        会让漏斗层的时间语义整体失真，宁可抛错。
        """
        contract = self._contract
        day = _resolve_trading_day(timestamp=timestamp, trade_date=trade_date)
        as_of = timestamp or _latest_bar_time(minute_bars)
        rows = [dict(row) for row in watch_pool if str(row.get("symbol", "")).strip()]
        bar_map = dict(minute_bars or {})
        prob_map = dict(probabilities or {})

        # 专属清单只读一次：打分与身份绑定必须看同一份文件，不能各读各的。
        tail_manifest, tail_failures = _read_tail_serving_manifest(self._service, contract)
        probability_source = "caller_supplied" if prob_map else "none"
        scoring_failures: tuple[str, ...] = ()
        if not prob_map and rows:
            prob_map, probability_source, scoring_failures = self._score_watch_pool(
                rows, tail_manifest
            )

        identity, identity_error, recording_failures = self._resolve_model_identity(
            prob_map, tail_manifest=tail_manifest, tail_failures=tail_failures
        )
        recording_failures = tuple(dict.fromkeys(list(recording_failures) + list(
            scoring_failures)))
        rejections: dict[str, list[str]] = {}
        decisions: dict[str, TailEntryDecision] = {}

        if identity_error:
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
                    confirmation=hard_gate_confirmation,
                    contract=contract,
                    quote_as_of=timestamp,
                    model_probabilities={NET_PROFIT_PROBABILITY_FIELD: prob_map[symbol]}
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
                "data_as_of": _fill_time_iso(decisions, symbol),
            }
            for symbol in sorted({str(row.get("symbol")) for row in rows})
        ]
        # 资金只够 k 只就把已选出的结果截到 k。**不能改契约再排**：改后的契约摘要会
        # 让模型身份校验失败，那会把"钱不够"错报成"模型不可信"。
        result = rank_final_recommendations(
            trade_date=day,
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
                    "filled": decision.filled,
                    "fill_time": (
                        decision.fill_time.isoformat() if decision.fill_time else None
                    ),
                    "quantity": decision.quantity,
                    "entry_amount": decision.entry_amount,
                    "buy_cost": decision.buy_cost,
                }
                for symbol, decision in decisions.items()
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
                timestamp=as_of, identity=identity, contract=contract,
            ),
            _stage(
                stage="tail_confirmation",
                kind=KIND_HARD_GATE,
                inputs=attempted,
                advanced=filled,
                reason_map=by_symbol_reason,
                timestamp=as_of, identity=identity, contract=contract,
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
                timestamp=as_of, identity=identity, contract=contract,
            ),
        ]
        trace = build_funnel_trace(
            trade_date=day,
            stages=stages,
            final_recommendations=archive_rows,
            rejected_final=rejected_final,
            blocking_reason=(budget_block or identity_error or result.blocking_reason),
            contract=contract,
        )
        report: dict[str, Any] = {
            "ok": True,
            "mode": "shadow",
            "trade_date": day.isoformat(),
            "data_as_of": as_of.isoformat(),
            "contract_version": contract.contract_version,
            "contract_digest": contract.digest(),
            "probability_field": NET_PROFIT_PROBABILITY_FIELD,
            "strategy": contract.strategy,
            "reference_notional": float(contract.reference_notional),
            "entry_window": [contract.entry_window_start, contract.entry_window_end],
            "min_net_profit_probability": float(contract.min_net_profit_probability),
            "holding_days": int(contract.holding_days),
            "take_profit_pct": float(contract.take_profit_pct),
            "stop_loss_pct": float(contract.stop_loss_pct),
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
                "probability_source": probability_source,
                # §3.1"记录失败必须可见"：绑定不上哪一项，就点名哪一项，不留空当默认值。
                "recording_failures": list(recording_failures),
                "training_manifest_id": str(getattr(identity, "training_manifest_id", "") or ""),
                # 正向确认，不只是"没报错"：registry 里查得到这个 id，且逐字段等于
                # 本契约推导出的那条标签口径。
                "label_policy_verified": bool(identity is not None) and not any(
                    str(item).startswith("label_policy_") for item in recording_failures
                ),
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

    def _score_watch_pool(
        self, rows: list[dict[str, Any]], tail_manifest: Mapping[str, Any]
    ) -> tuple[dict[str, float], str, tuple[str, ...]]:
        """没人生成概率时，由**已核验的 challenger 工件**自己算。

        这里不设兜底：清单缺失 / 工件加载不过 / 某只缺特征，都返回点名原因，
        让这一轮按 §3.4"数据不足、模型无效 ⇒ 输出 0 只"落地，而不是补一个
        看起来像概率的数（缺特征填零会被当成真实信息拿去排序）。
        """
        if not tail_manifest:
            return {}, "none", ("challenger_artifact_not_bound",)
        artifact_path = _manifest_field(
            tail_manifest, "artifact_path", "authoritative_artifact")
        if not artifact_path:
            return {}, "none", ("challenger_artifact_path_missing",)
        try:
            from stock_analyzer.models.tail_model_artifact import (
                TailArtifactError,
                load_tail_model_predictor,
            )

            resolved = getattr(self._service, "_resolve_evolution_path",
                               lambda value: value)(artifact_path)
            predictor = load_tail_model_predictor(Path(resolved), contract=self._contract)
        except TailArtifactError as exc:
            return {}, "none", (f"challenger_artifact_unusable:{exc}",)
        except Exception as exc:  # noqa: BLE001 - 加载不过就是模型无效，点名即可
            return {}, "none", (f"challenger_artifact_load_failed:{type(exc).__name__}",)

        scored: dict[str, float] = {}
        failures: list[str] = []
        for row in rows:
            symbol = str(row.get("symbol", "")).strip()
            if not symbol:
                continue
            try:
                scored[symbol] = predictor.probability(dict(row.get("features") or {}))
            except TailArtifactError as exc:
                failures.append(f"probability_scoring_failed:{symbol}:{exc}")
        return scored, "challenger_artifact", tuple(failures)

    def _resolve_model_identity(
        self,
        probabilities: Mapping[str, float],
        *,
        tail_manifest: Mapping[str, Any] | None = None,
        tail_failures: tuple[str, ...] = (),
    ) -> tuple[ModelIdentity | None, str, tuple[str, ...]]:
        """在服模型必须**就是**本契约的净盈利模型，否则身份按不可验证处理。

        返回 ``(identity, 阻塞原因, 记录失败清单)``。第三项是 §3.1 的"记录失败必须
        可见"：绑不上训练 manifest / 读不到运行 commit 时，留档里要能看出**为什么**空着。
        """
        if not probabilities:
            return None, "no_tail_probability_available", ()
        if tail_manifest is None:
            tail_manifest, tail_failures = _read_tail_serving_manifest(
                self._service, self._contract)
        if tail_manifest and tail_failures:
            # 专属清单存在但对不上 = 模型无效，直接 0 只；退回旧在服清单等于
            # 静默换一个模型（§3.3 明令禁止）。
            return None, "tail_serving_manifest_unverified", tuple(tail_failures)
        if tail_manifest:
            manifest = tail_manifest
            failures: list[str] = []
        else:
            # 没有尾盘专属清单时才回退到旧的在服清单，并把"回退了"这件事留名：
            # 回退路径上的身份缺 commit 是可预期的，但排查时必须能看出用的是哪份文件。
            manifest, manifest_failures = _read_serving_manifest(self._service)
            failures = list(manifest_failures)
            if not manifest:
                return None, "serving_manifest_missing", tuple(failures)
            failures.append("tail_serving_manifest_absent")
        label_policy_id = _manifest_field(manifest, "label_policy_id")
        if not label_policy_id.startswith("label_policy_v4_"):
            return None, "serving_model_is_not_tail_label_policy", tuple(failures)
        # 清单里写着一个 id 不等于这个 id 存在。registry 才是标签契约的落库处，
        # 所以拿它逐字段核对一次：查不到、或者查到的是另一套 TP/SL/持有期/价格口径，
        # 都记成可见的失败原因，而不是让留档里留一个无法解释的字符串。
        _registered_policy, policy_failures = verify_tail_label_policy(
            getattr(self._service, "_label_policy_registry", None),
            label_policy_id=label_policy_id, contract=self._contract,
        )
        failures.extend(policy_failures)
        training_commit = _manifest_field(
            manifest, "code_commit", "commit", "training_commit", "training_code_commit")
        if not training_commit:
            failures.append("training_commit_absent_from_serving_manifest")
        training_manifest_id = _manifest_field(
            manifest, "dataset_manifest_id", "manifest_id", "training_manifest_id")
        if not training_manifest_id:
            failures.append("training_manifest_id_absent_from_serving_manifest")
        runtime_commit, runtime_failures = _runtime_commit(self._service)
        feature_compute_version, version_failures = _feature_compute_version()
        failures.extend(runtime_failures)
        failures.extend(version_failures)
        identity = ModelIdentity(
            model_id=(_manifest_field(manifest, "model_id")
                      or _manifest_field(manifest, "artifact_path", "authoritative_artifact")),
            artifact_content_hash=_manifest_field(
                manifest, "artifact_content_hash", "authoritative_content_hash"),
            training_commit=training_commit,
            runtime_commit=runtime_commit,
            feature_compute_version=feature_compute_version,
            label_policy_id=label_policy_id,
            contract_digest=self._contract.digest(),
            training_manifest_id=training_manifest_id,
        )
        return identity, identity.validate(self._contract), tuple(dict.fromkeys(failures))

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
                "contract_digest", "strategy", "entry_window", "min_net_profit_probability"):
        if report.get(key) in (None, "", []):
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
                f"持有至多 {int(report.get('holding_days') or 0)} 个交易日净收益>0 的概率"
            ),
            "strategy": str(report["strategy"]),
            "reference_notional_cny": float(report["reference_notional"]),
            "contract_version": str(report["contract_version"]),
            "contract_digest": str(report["contract_digest"]),
            "entry_window": [str(value) for value in report["entry_window"]],
            "min_net_profit_probability": float(report["min_net_profit_probability"]),
            "holding_days": int(report.get("holding_days") or 0),
            "max_recommendations": report.get("max_recommendations_effective"),
            "take_profit_pct": report.get("take_profit_pct"),
            "stop_loss_pct": report.get("stop_loss_pct"),
            "data_as_of": str(
                report.get("data_as_of")
                or (rows[0].get("data_as_of") if rows else "")
                or report["trade_date"]
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
            # §3.1"记录失败必须可见"：绑定不上训练 manifest / 读不到运行 commit，
            # 也要在页面上说清楚，而不是只留一个空字段让读的人自己猜。
        } | {
            f"identity_recording_failed:{item}"
            for item in ((report.get("model_identity") or {}).get("recording_failures") or [])
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


def _resolve_trading_day(
    *, timestamp: datetime | None, trade_date: date | str | None
) -> date:
    """交易日只从 ``timestamp`` 或显式 ``trade_date`` 推导，**不接受 ``date.today()`` 兜底**。

    历史重算若把留档盖成"今天"，漏斗每一层的时间语义会整体失真，§4 要求的
    "相同输入下线上与历史路径判定一致"也就无从验证；宁可抛错。
    """
    if isinstance(timestamp, datetime):
        return timestamp.date()
    if isinstance(trade_date, datetime):
        return trade_date.date()
    if isinstance(trade_date, date):
        return trade_date
    if trade_date:
        return date.fromisoformat(str(trade_date)[:10])
    raise TrendContractError(
        "尾盘确认需要 timestamp 或 trade_date 之一；历史重算不接受 date.today() 兜底"
    )


def _latest_bar_time(
    minute_bars: Mapping[str, Sequence[tuple[datetime, Mapping[str, Any]]]] | None,
) -> datetime:
    """历史模式的数据时间：所有标的里最大的一根 bar 时刻。"""
    latest: datetime | None = None
    for bars in (minute_bars or {}).values():
        for bar_time, _ in bars:
            if isinstance(bar_time, datetime) and (latest is None or bar_time > latest):
                latest = bar_time
    return latest or datetime.min


def _fill_time_iso(
    decisions: Mapping[str, TailEntryDecision], symbol: str
) -> str:
    """成交时刻的 ISO 串；没下单或没成交就是空串，不猜一个时间。"""
    decision = decisions.get(symbol)
    if decision is None or decision.fill_time is None:
        return ""
    return decision.fill_time.isoformat()


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
        # 留档里的特征计算版本必须就是** gates 掉这次决策的那个身份**里的版本，
        # 而不是再从模块常量读一遍（两处读值可以不一致，身份却只有一份）。
        feature_compute_version=int(getattr(identity, "feature_compute_version", 0) or 0)
        if identity is not None else _feature_compute_version()[0],
        label_policy_id=getattr(identity, "label_policy_id", "") if identity else "",
    )


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


def _read_serving_manifest(service: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
    """返回 ``(manifest, 记录失败原因)``。

    "读不到" 不能只留下一个空 dict：没配路径、读盘炸了、内容空是三件不同的事，
    计划 §3.1 要求记录失败必须可见，否则排查时只能看到 ``serving_manifest_missing``。
    """
    reader = getattr(service, "_read_serving_manifest", None)
    if callable(reader):
        try:
            payload = dict(reader() or {})
        except Exception as exc:  # noqa: BLE001 - 读不到就是身份不明，交给上层 fail-closed
            return {}, (f"serving_manifest_reader_raised:{type(exc).__name__}",)
    else:
        try:
            from stock_analyzer.models.serving_manifest import read_serving_manifest

            config = getattr(service, "_config", None)
            path = getattr(getattr(config, "training", None), "serving_manifest_path", None)
            if not path:
                return {}, ("serving_manifest_path_not_configured",)
            resolved = getattr(service, "_resolve_evolution_path", lambda value: value)(path)
            payload = dict(read_serving_manifest(Path(resolved)) or {})
        except Exception as exc:  # noqa: BLE001
            return {}, (f"serving_manifest_read_failed:{type(exc).__name__}",)
    if not payload:
        return {}, ("serving_manifest_empty",)
    return payload, ()


_MANIFEST_SECTIONS = ("serving", "authority", "registry", "identity", "label", "contract")


def _read_tail_serving_manifest(
    service: Any, contract: TrendStrategyContract
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """读**尾盘专属**在服清单：存在就必须在服；不存在才允许走旧在服清单。

    存在但对不上时把原因原样交回去（不吞、不退），因为"换一份清单就能跑"正是
    §3.3 禁止的静默替代模型。
    """
    path = _tail_serving_manifest_path(service)
    if not path:
        return {}, ()
    try:
        from stock_analyzer.models.tail_serving_manifest import (
            read_tail_serving_manifest,
            verify_tail_serving_manifest,
        )

        resolved = getattr(service, "_resolve_evolution_path", lambda value: value)(path)
        payload = dict(read_tail_serving_manifest(Path(resolved)) or {})
    except Exception as exc:  # noqa: BLE001 - 读不到就是身份不明，交给上层 fail-closed
        return {}, (f"tail_serving_manifest_read_failed:{type(exc).__name__}",)
    if not payload:
        return {}, ()
    _, failures = verify_tail_serving_manifest(payload, contract=contract)
    return payload, tuple(failures)


def _tail_serving_manifest_path(service: Any) -> str:
    """路径优先级：注入 > 配置声明 > 环境变量 > 研究区默认值。

    故意**不**放进 ``TrendStrategyConfig``：那是契约字段表
    （``contract_from_config`` 直接把整个块喂给 ``TrendStrategyContract``），
    而"清单文件在哪"是部署事实，不是策略语义。
    """
    override = getattr(service, "_trend_tail_serving_manifest_path", None)
    if callable(override):
        return str(override() or "").strip()
    if isinstance(override, str):
        return override.strip()
    config = getattr(service, "_config", None)
    trend = getattr(getattr(config, "trend_strategy", None), "tail_serving_manifest_path", "")
    if str(trend or "").strip():
        return str(trend).strip()
    env_value = str(os.environ.get("SA_TAIL_SERVING_MANIFEST_PATH") or "").strip()
    if env_value:
        return env_value
    from stock_analyzer.models.tail_serving_manifest import (
        DEFAULT_TAIL_SERVING_MANIFEST_PATH,
    )

    return DEFAULT_TAIL_SERVING_MANIFEST_PATH


def _manifest_field(manifest: Mapping[str, Any], *names: str) -> str:
    """从 ``model_serving_manifest.v1`` 的**真实分层**里取字段。

    ``build_serving_manifest`` 把 ``label_policy_id`` 放在 ``serving``、``model_id`` 放在
    ``registry``、权威哈希放在 ``authority``，顶层只有 ``schema``/``generated_at``/``source``。
    只按顶层读会永远读空 —— 那样 §3.1 的"绑定实际加载的模型"就只在扁平 fixture 里成立。
    顶层仍作回退，兼容自定义/早期清单。
    """
    for name in names:
        for section in _MANIFEST_SECTIONS:
            block = manifest.get(section)
            if isinstance(block, Mapping) and str(block.get(name, "") or "").strip():
                return str(block[name]).strip()
        if str(manifest.get(name, "") or "").strip():
            return str(manifest[name]).strip()
    return ""


def _runtime_commit(service: Any) -> tuple[str, tuple[str, ...]]:
    getter = getattr(service, "_runtime_code_commit", None)
    if callable(getter):
        value = str(getter() or "")
    else:
        value = str(getattr(getattr(service, "_state", None), "code_commit", "") or "")
    return value, () if value else ("runtime_code_commit_unavailable",)


def _feature_compute_version() -> tuple[int, tuple[str, ...]]:
    try:
        from stock_analyzer.feature.engineer import FEATURE_COMPUTE_VERSION

        value = int(FEATURE_COMPUTE_VERSION)
    except Exception as exc:  # noqa: BLE001
        return 0, (f"feature_compute_version_unreadable:{type(exc).__name__}",)
    return value, () if value > 0 else ("feature_compute_version_not_positive",)


__all__ = [
    "REPORT_DIR_DEFAULT",
    "TrendTailShadowService",
    "page_view",
    "tail_shadow_history",
    "tail_shadow_page",
]
