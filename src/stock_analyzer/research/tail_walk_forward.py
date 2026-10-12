"""尾盘链路的滚动前推验证：把计划 §4 的"按交易日滚动训练、校准和测试"跑成一条命令。

这里唯一新增的是**编排**：折边界、训练、校准、排序、判定全部复用线上那几套权威实现
（``train_tail_net_profit_model`` / ``rank_final_recommendations`` /
``evaluate_selection_quality``），因为"同一份输入在线上与历史路径必须给出一致判定"
不允许验证器自带一套排序逻辑。

三件事是刻意的：

- 质量验证样本不足时返回 ``blocked``；只有显式开发探索可以在欠功效数据上测模型，
  且最终质量门仍判为 ``sample_insufficient``；
- 未成交/未成熟样本参与候选池与成交率，但绝不进净盈利率分子分母；
- observed 与 replayed 样本分开计数，绝不互借样本量。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    ModelIdentity,
    TrendStrategyContract,
    rank_final_recommendations,
)
from stock_analyzer.labels.tail_net_profit import CAPTURE_OBSERVED, CAPTURE_REPLAYED
from stock_analyzer.models.tail_net_profit_trainer import (
    MIN_CONFIRMATORY_SESSIONS_PER_FOLD,
    MIN_CONFIRMATORY_TEST_SESSIONS,
    MIN_TEST_FOLDS,
    DateSplit,
    DayOutcome,
    TailModelSpec,
    TailTrainingError,
    evaluate_selection_quality,
    shadow_readiness,
    train_tail_net_profit_model,
)

ARM_TREATMENT = "p_net_profit_5d_tail"
ARM_BASELINE = "legacy_order_same_pool"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"

#: 段间隔需要观察到的交易日数；训练段最少天数。两者都要"数得满"才继续。
#: 行数下限是训练器那边的权威口径（``TailModelSpec``），这里只保证天数窗口不像话。
DEFAULT_CALIBRATION_SESSIONS = 12
DEFAULT_MIN_TRAIN_SESSIONS = 45


def _day(value: Any) -> date | None:
    """折分组只认 ``date``（或能明确到某一天的 ``datetime``）。

    其余类型（ISO 字符串、None）一律当成"没有日期"：宁可整轮 blocked，也不把
    不同时区语义的时间戳混进同一根时间轴上排序。
    """
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _observed_days(rows: Sequence[Mapping[str, Any]], *, date_field: str) -> list[date]:
    return sorted(day for day in (_day(row.get(date_field)) for row in rows) if day)


def build_rolling_splits(
    trade_dates: Sequence[date],
    *,
    folds: int = MIN_TEST_FOLDS,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    calibration_sessions: int = DEFAULT_CALIBRATION_SESSIONS,
    min_train_sessions: int = DEFAULT_MIN_TRAIN_SESSIONS,
) -> list[DateSplit]:
    """把时间轴切成 ``folds`` 个互不重叠的测试块，每块前配一段被 embargo 隔开的校准段。

    训练窗随折右移而扩大（walk-forward），所以第 i 折的模型永远只看得到第 i 块之前的
    数据；段间空出 ``holding_days`` 个交易日，保证进入后段的标签已经真实成熟。
    """
    ordered = sorted({day for day in trade_dates if day is not None})
    embargo = max(1, int(contract.holding_days))
    if folds < 1:
        raise ValueError("folds must be >= 1")
    if calibration_sessions < 1 or min_train_sessions < 1:
        raise ValueError("calibration_sessions and min_train_sessions must be >= 1")
    warmup = min_train_sessions + calibration_sessions + 2 * embargo
    testable = len(ordered) - warmup
    if testable < folds:
        raise ValueError(
            f"{len(ordered)} trade dates cannot fill {folds} folds: need at least "
            f"{warmup + folds} (train>={min_train_sessions}, calib>={calibration_sessions}, "
            f"embargo={embargo} between every segment)"
        )
    block = int(math.floor(testable / folds))
    splits: list[DateSplit] = []
    for index in range(folds):
        test_start = warmup + index * block
        # 最后一折吃满剩余日期，避免尾部零头被静默丢掉。
        test_end = len(ordered) if index == folds - 1 else test_start + block
        calibration_start = test_start - embargo - calibration_sessions
        calibration_end = test_start - embargo
        # 训练段还要再往左空出一个 embargo：否则"训练最后一天"的持仓期会跨进校准段。
        train_end = calibration_start - embargo
        if train_end < min_train_sessions:
            raise ValueError(f"fold {index + 1} has no room for a training window")
        splits.append(
            DateSplit(
                train_dates=tuple(ordered[:train_end]),
                calibration_dates=tuple(ordered[calibration_start:calibration_end]),
                test_dates=tuple(ordered[test_start:test_end]),
                embargo_sessions=embargo,
            )
        )
    return splits


def score_rows(
    artifact: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    feature_names: Sequence[str],
) -> list[float]:
    """用训练工件打分：原始预测 → **同一段 isotonic** 校准，与训练时完全同一条路。"""
    model = artifact.get("model")
    calibrator = artifact.get("calibrator")
    if model is None or calibrator is None:
        raise ValueError("trained artifact carries no model/calibrator to score with")
    predict = getattr(model, "predict_proba", None)
    if not callable(predict):
        predict = getattr(model, "predict", None)
    if not callable(predict):
        raise ValueError("trained model exposes neither predict_proba nor predict")
    matrix = np.asarray([[float(row[name]) for name in feature_names] for row in rows])
    raw = [float(value) for value in predict(matrix)]
    return [float(value) for value in calibrator.predict(np.asarray(raw))]


def select_training_features_by_correlation(
    rows: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    *,
    train_dates: Sequence[date],
    date_field: str = "entry_date",
    label_field: str = "label",
    threshold: float = 0.95,
    minimum_pair_rows: int = 30,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Deduplicate only with labeled rows in the current training window.

    Completeness is ranked first; ties preserve the frozen input feature order. Correlation
    pairs with fewer than ``minimum_pair_rows`` complete observations are left untouched.
    Calibration and test rows never participate in this choice.
    """
    names = tuple(str(name) for name in feature_names)
    if not 0.0 < float(threshold) <= 1.0:
        raise ValueError("correlation threshold must be in (0, 1]")
    if int(minimum_pair_rows) < 2:
        raise ValueError("minimum_pair_rows must be >= 2")
    train_set = set(train_dates)
    training_rows = [
        row
        for row in rows
        if _day(row.get(date_field)) in train_set and row.get(label_field) in (0, 1, 0.0, 1.0)
    ]
    if not training_rows:
        raise ValueError("correlation deduplication has no labeled training rows")

    values: dict[str, np.ndarray] = {}
    completeness: dict[str, float] = {}
    for name in names:
        column: list[float] = []
        present = 0
        for row in training_rows:
            try:
                number = float(row.get(name))
            except (TypeError, ValueError):
                number = float("nan")
            if not np.isfinite(number):
                number = float("nan")
            else:
                present += 1
            column.append(number)
        values[name] = np.asarray(column, dtype=float)
        completeness[name] = present / len(training_rows)

    input_order = {name: index for index, name in enumerate(names)}
    ranked = sorted(names, key=lambda name: (-completeness[name], input_order[name]))
    kept_ranked: list[str] = []
    dropped: list[dict[str, Any]] = []
    for candidate in ranked:
        duplicate_of: str | None = None
        for kept in kept_ranked:
            pair_mask = np.isfinite(values[candidate]) & np.isfinite(values[kept])
            pair_count = int(pair_mask.sum())
            if pair_count < int(minimum_pair_rows):
                continue
            left = values[candidate][pair_mask]
            right = values[kept][pair_mask]
            if np.std(left) == 0.0 or np.std(right) == 0.0:
                continue
            rho = float(np.corrcoef(left, right)[0, 1])
            if np.isfinite(rho) and abs(rho) >= float(threshold):
                duplicate_of = kept
                dropped.append(
                    {
                        "dropped": candidate,
                        "kept": kept,
                        "correlation": rho,
                        "pair_rows": pair_count,
                        "dropped_completeness": completeness[candidate],
                        "kept_completeness": completeness[kept],
                    }
                )
                break
        if duplicate_of is None:
            kept_ranked.append(candidate)
    kept_set = set(kept_ranked)
    selected = tuple(name for name in names if name in kept_set)
    return selected, {
        "applied": True,
        "threshold_abs_correlation": float(threshold),
        "minimum_pair_rows": int(minimum_pair_rows),
        "training_labeled_rows": len(training_rows),
        "training_dates": len(train_set),
        "input_features": list(names),
        "feature_completeness": completeness,
        "kept_features": list(selected),
        "dropped_correlated_pairs": dropped,
        "scope": "current_fold_train_only",
    }


def _candidate_row(
    row: Mapping[str, Any], probability: float, probability_field: str
) -> dict[str, Any]:
    filled = bool(row.get("filled"))
    return {
        "symbol": str(row.get("symbol")),
        probability_field: probability,
        "tradeable": filled,
        "not_tradeable_reason": ("" if filled else str(row.get("reason") or "not_filled")),
        "risk_state": str(row.get("risk_state") or ""),
        "data_as_of": str(row.get("fill_time") or ""),
    }


def _has_complete_features(row: Mapping[str, Any], feature_names: Sequence[str]) -> bool:
    """Missing model inputs fail closed for that candidate; never cast None or fill zero."""
    try:
        return all(math.isfinite(float(row.get(name))) for name in feature_names)
    except (TypeError, ValueError):
        return False


def _outcome(
    day: date,
    arm: str,
    selected: Sequence[str],
    pool: Sequence[Mapping[str, Any]],
    *,
    fold_id: int,
    session_index: int,
    model_unscorable_rows: int = 0,
) -> DayOutcome:
    """按入选符号汇总当日结果；成交率用整池，净盈利率只用已实现样本。"""
    by_symbol = {str(row.get("symbol")): row for row in pool}
    chosen = [by_symbol[symbol] for symbol in selected if symbol in by_symbol]
    realized = [
        float(row["net_return"])
        for row in chosen
        if row.get("trainable") and row.get("net_return") is not None
    ]
    return DayOutcome(
        trade_date=day,
        arm=arm,
        recommendations=len(chosen),
        fills=sum(1 for row in chosen if row.get("filled")),
        matured_fills=len(realized),
        net_profits=sum(1 for value in realized if value > 0.0),
        net_returns=tuple(realized),
        fold_id=fold_id,
        session_index=session_index,
        model_unscorable_rows=int(model_unscorable_rows),
    )


def _select_by_field(pool: Sequence[Mapping[str, Any]], field: str, *, cap: int) -> list[str]:
    """匹配基线：同一合格池、同一名额上限，只把排序键换成旧的合成分。

    基线**不吃 0.60 阈值**——那个阈值是新概率的准入规则，套到旧分数上就变成两回事了。
    """
    tradeable = [row for row in pool if row.get("filled")]
    ranked = sorted(
        tradeable,
        key=lambda row: (-float(row.get(field) or 0.0), str(row.get("symbol"))),
    )
    return [str(row.get("symbol")) for row in ranked[: max(0, int(cap))]]


def _identity_for(artifact: Mapping[str, Any], *, runtime_commit: str) -> ModelIdentity:
    return ModelIdentity(
        model_id=str(artifact.get("model_id") or ""),
        artifact_content_hash=str(artifact.get("artifact_digest") or ""),
        training_commit=str(artifact.get("training_commit") or ""),
        runtime_commit=str(runtime_commit or ""),
        feature_compute_version=int(artifact.get("feature_compute_version") or 0),
        label_policy_id=str(artifact.get("label_policy_id") or ""),
        contract_digest=str(artifact.get("contract_digest") or ""),
    )


def sufficiency_blockers(
    rows: Sequence[Mapping[str, Any]],
    *,
    trade_dates: Sequence[date] | None = None,
    date_field: str,
    label_field: str,
    folds: int,
    contract: TrendStrategyContract,
    calibration_sessions: int,
    min_train_sessions: int,
    decision_calendar: Sequence[date] | None = None,
    allow_underpowered_development: bool = False,
) -> list[str]:
    """验证门；只允许显式的 replayed 开发探索绕过确认样本量下限。"""
    if not trade_dates:
        return [
            "authoritative_trade_calendar_missing: provide the complete exchange session calendar; "
            "sample dates cannot stand in for missing or no-recommendation sessions"
        ]
    dates = sorted(set(trade_dates))
    calendar_set = set(dates)
    decisions = sorted(set(decision_calendar or dates))
    outside_decision_calendar = sorted(set(decisions) - calendar_set)
    if outside_decision_calendar:
        return [
            "decision_sessions_outside_authoritative_trading_calendar:"
            f"{outside_decision_calendar[:5]}"
        ]
    row_decision_dates = set(_observed_days(rows, date_field=date_field))
    row_entry_dates = {
        day for row in rows if (day := _day(row.get("entry_date"))) is not None
    }
    outside_calendar = sorted((row_decision_dates | row_entry_dates) - calendar_set)
    if outside_calendar:
        return [f"sample_dates_outside_authoritative_calendar:{outside_calendar[:5]}"]
    outside_decisions = sorted(row_decision_dates - set(decisions))
    if outside_decisions:
        return [f"sample_decision_dates_without_coverage:{outside_decisions[:5]}"]
    if allow_underpowered_development and any(
        str(row.get("capture_mode") or "") != CAPTURE_REPLAYED for row in rows
    ):
        return ["underpowered_development_requires_replayed_samples_only"]
    labelled = [row for row in rows if row.get(label_field) in (0, 1, 0.0, 1.0)]
    blockers: list[str] = []
    if not labelled:
        blockers.append(
            "no_labelled_samples: p_net_profit_5d_tail 的真实成熟标签还没有生产者，"
            "尾盘策略验证记为阻塞（不得用开盘回测代替）"
        )
        return blockers
    try:
        splits = build_rolling_splits(
            decisions,
            folds=folds,
            contract=contract,
            calibration_sessions=calibration_sessions,
            min_train_sessions=min_train_sessions,
        )
    except ValueError as exc:
        blockers.append(f"insufficient_trade_dates: {exc}")
    else:
        test_sizes = [len(split.test_dates) for split in splits]
        test_total = sum(test_sizes)
        if not allow_underpowered_development and test_total < MIN_CONFIRMATORY_TEST_SESSIONS:
            blockers.append(
                f"complete_test_sessions={test_total} < {MIN_CONFIRMATORY_TEST_SESSIONS}"
            )
        if not allow_underpowered_development:
            for index, count in enumerate(test_sizes, 1):
                if count < MIN_CONFIRMATORY_SESSIONS_PER_FOLD:
                    blockers.append(
                        f"test_fold_{index}_sessions={count} "
                        f"< {MIN_CONFIRMATORY_SESSIONS_PER_FOLD}"
                    )
    return blockers


def run_walk_forward(
    *,
    rows: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    model_id: str,
    training_commit: str,
    runtime_commit: str,
    feature_compute_version: int,
    label_policy_id: str,
    trading_calendar: Sequence[date] | None = None,
    decision_calendar: Sequence[date] | None = None,
    spec: TailModelSpec | None = None,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    date_field: str = "decision_date",
    label_field: str = "label",
    probability_field: str = NET_PROFIT_PROBABILITY_FIELD,
    baseline_rank_field: str = "composite_score",
    folds: int = MIN_TEST_FOLDS,
    calibration_sessions: int = DEFAULT_CALIBRATION_SESSIONS,
    min_train_sessions: int = DEFAULT_MIN_TRAIN_SESSIONS,
    deduplicate_correlated_features: bool = False,
    correlation_threshold: float = 0.95,
    correlation_minimum_pair_rows: int = 30,
    booster_trainer: Any | None = None,
    verify_artifact_roundtrip: bool = False,
    allow_underpowered_development: bool = False,
) -> dict[str, Any]:
    """滚动训练 → 校准 → 测试，并把每日结果交给唯一的质量判定门。"""
    blockers = sufficiency_blockers(
        rows,
        trade_dates=trading_calendar,
        date_field=date_field,
        label_field=label_field,
        folds=folds,
        contract=contract,
        calibration_sessions=calibration_sessions,
        min_train_sessions=min_train_sessions,
        decision_calendar=decision_calendar,
        allow_underpowered_development=allow_underpowered_development,
    )
    if blockers:
        return {
            "status": STATUS_BLOCKED,
            "blockers": blockers,
            "metrics_computed": False,
            "note": "样本不足时不产命中率数字；继续采集带时刻的分钟行情后再跑",
        }

    if not trading_calendar:
        return {
            "status": STATUS_BLOCKED,
            "blockers": blockers,
            "metrics_computed": False,
            "note": "完整交易日历缺失时不推定空推荐日，也不压缩时间轴",
        }
    all_open_dates = sorted(set(trading_calendar))
    all_dates = sorted(set(decision_calendar or all_open_dates))
    row_dates = set(_observed_days(rows, date_field=date_field))
    outside_calendar = sorted(row_dates - set(all_dates))
    if outside_calendar:
        raise ValueError(
            f"sample dates fall outside the authoritative trading calendar: {outside_calendar[:5]}"
        )
    session_indices = {day: index for index, day in enumerate(all_dates)}
    by_date: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        day = _day(row.get(date_field))
        if day is not None:
            by_date.setdefault(day, []).append(dict(row))

    splits = build_rolling_splits(
        all_dates,
        folds=folds,
        contract=contract,
        calibration_sessions=calibration_sessions,
        min_train_sessions=min_train_sessions,
    )
    outcomes: list[DayOutcome] = []
    fold_reports: list[dict[str, Any]] = []
    blocked_folds: list[dict[str, Any]] = []

    for index, split in enumerate(splits):
        fold_feature_names = tuple(feature_names)
        feature_selection: dict[str, Any] = {
            "applied": False,
            "scope": "disabled",
            "input_features": list(fold_feature_names),
            "kept_features": list(fold_feature_names),
            "dropped_correlated_pairs": [],
        }
        if deduplicate_correlated_features:
            fold_feature_names, feature_selection = select_training_features_by_correlation(
                rows,
                feature_names,
                train_dates=split.train_dates,
                date_field=date_field,
                label_field=label_field,
                threshold=correlation_threshold,
                minimum_pair_rows=correlation_minimum_pair_rows,
            )
        artifact = train_tail_net_profit_model(
            rows=rows,
            feature_names=fold_feature_names,
            spec=spec,
            label_field=label_field,
            date_field=date_field,
            probability_field=probability_field,
            contract=contract,
            model_id=f"{model_id}#fold{index + 1}",
            training_commit=training_commit,
            feature_compute_version=feature_compute_version,
            label_policy_id=label_policy_id,
            split=split,
            booster_trainer=booster_trainer,
        )
        identity = _identity_for(artifact, runtime_commit=runtime_commit)
        identity_error = identity.validate(contract)
        if identity_error:
            # 身份不通过就停在这一折：宁可整体验证不成立，也不用没核实的分数选过。
            blocked_folds.append({"fold": index + 1, "reason": identity_error})
            continue

        fold_days: list[str] = []
        for day in split.test_dates:
            pool = by_date.get(day, [])
            scoreable_pool = [
                row for row in pool if _has_complete_features(row, fold_feature_names)
            ]
            model_unscorable_rows = len(pool) - len(scoreable_pool)
            if scoreable_pool:
                probabilities = score_rows(
                    artifact,
                    scoreable_pool,
                    feature_names=artifact["feature_names"],
                )
                candidates = [
                    _candidate_row(row, probability, probability_field)
                    for row, probability in zip(scoreable_pool, probabilities, strict=True)
                ]
                ranked = rank_final_recommendations(
                    trade_date=day,
                    rows=candidates,
                    model_identity=identity,
                    contract=contract,
                    probability_field=probability_field,
                )
            else:
                # No scoreable model candidates means the model fails closed for this day.
                ranked = None
            baseline_symbols = _select_by_field(
                pool,
                baseline_rank_field,
                cap=contract.max_final_recommendations,
            ) if pool else []
            outcomes.append(
                _outcome(
                    day,
                    ARM_TREATMENT,
                    list(ranked.symbols) if ranked else [],
                    pool,
                    fold_id=index + 1,
                    session_index=session_indices[day],
                    model_unscorable_rows=model_unscorable_rows,
                )
            )
            outcomes.append(
                _outcome(
                    day,
                    ARM_BASELINE,
                    baseline_symbols,
                    pool,
                    fold_id=index + 1,
                    session_index=session_indices[day],
                )
            )
            fold_days.append(str(day))

        roundtrip: dict[str, Any] = {"verified": False, "status": "not_requested"}
        if verify_artifact_roundtrip:
            from stock_analyzer.models.tail_model_artifact import (
                load_tail_model_predictor,
                serialize_tail_artifact,
            )

            serialized = serialize_tail_artifact(artifact, contract=contract)
            predictor = load_tail_model_predictor(serialized, contract=contract)
            test_date_set = set(split.test_dates)
            artifact_feature_names = tuple(artifact["feature_names"])

            check_rows = [
                row
                for row in rows
                if _day(row.get(date_field)) in test_date_set
                and _has_complete_features(row, artifact_feature_names)
            ]
            expected = score_rows(
                artifact,
                check_rows,
                feature_names=artifact["feature_names"],
            )
            actual = [predictor.probability(row) for row in check_rows]
            max_abs = max(
                (abs(left - right) for left, right in zip(expected, actual, strict=True)),
                default=0.0,
            )
            if not np.allclose(expected, actual, rtol=1e-10, atol=1e-10):
                raise TailTrainingError(
                    f"serialized model round-trip prediction drift in fold {index + 1}: "
                    f"max_abs={max_abs}"
                )
            roundtrip = {
                "verified": True,
                "status": "passed",
                "rows_compared": len(check_rows),
                "max_abs_probability_delta": float(max_abs),
                "serialized_artifact_digest": serialized["artifact_digest"],
            }
        fold_reports.append(
            {
                "fold": index + 1,
                "split": split.as_dict(),
                "artifact_digest": artifact["artifact_digest"],
                "feature_selection": feature_selection,
                "artifact_roundtrip": roundtrip,
                "calibration_auc": artifact["calibration_auc"],
                "test_metrics": artifact["metrics"]["overall"],
                "selection_metrics": _fold_selection_metrics(outcomes, index + 1),
                "capture_modes_in_test": artifact["metrics"]["reported_separately"],
                "evaluated_days": fold_days,
            }
        )

    if blocked_folds:
        return {
            "status": STATUS_BLOCKED,
            "blockers": [item["reason"] for item in blocked_folds],
            "blocked_folds": blocked_folds,
            "metrics_computed": False,
        }

    quality = evaluate_selection_quality(
        outcomes=outcomes,
        candidate_days=sum(len(split.test_dates) for split in splits),
        baseline_arm=ARM_BASELINE,
        treatment_arm=ARM_TREATMENT,
    )
    treatment_matured = sum(item.matured_fills for item in outcomes if item.arm == ARM_TREATMENT)
    test_dates = {day for split in splits for day in split.test_dates}
    sample_basis = _sample_basis(rows, test_dates, date_field=date_field)
    return {
        "status": STATUS_COMPLETED,
        "metrics_computed": True,
        "evidence_mode": (
            "underpowered_development_exploration"
            if allow_underpowered_development else "quality_validation"
        ),
        "folds": fold_reports,
        "fold_count": len(fold_reports),
        "arm_totals": _arm_totals(outcomes),
        "contract_digest": contract.digest(),
        "contract_version": contract.contract_version,
        "arms": {
            "treatment": ARM_TREATMENT,
            "baseline": ARM_BASELINE,
            "baseline_rank_field": baseline_rank_field,
        },
        "sample_basis": sample_basis,
        "trading_calendar": {
            "source": "explicit_exchange_session_calendar",
            "sessions": len(all_open_dates),
            "decision_sessions": len(all_dates),
            "test_sessions": sum(len(split.test_dates) for split in splits),
            "test_session_start": min(
                (day for split in splits for day in split.test_dates), default=None
            ),
            "test_session_end": max(
                (day for split in splits for day in split.test_dates), default=None
            ),
            "missing_candidate_sessions_are_explicit_zero_recommendation_days": True,
        },
        "quality": quality,
        "shadow_readiness": shadow_readiness(
            observed_trade_days=len(
                {
                    _day(row.get(date_field))
                    for row in rows
                    if str(row.get("capture_mode")) == CAPTURE_OBSERVED
                }
                & test_dates
            ),
            matured_simulated_fills=treatment_matured,
        ),
        "caveats": [
            "阈值 0.60 是初始选股规则，不代表已证明命中率达到 60%",
            "observed 与 replayed 样本分开计数，不互借样本量",
            *(
                [
                    "development-only: small replayed samples may produce exploratory "
                    "metrics, but fixed 120-session/30-per-fold/300-fill gates still "
                    "decide quality.passed"
                ]
                if allow_underpowered_development else []
            ),
        ],
    }


def _arm_totals(outcomes: Sequence[DayOutcome]) -> dict[str, dict[str, int]]:
    """逐臂合计。分母口径要能被外部核对，否则"净盈利率"只是一个无法追问的数字。"""
    totals: dict[str, dict[str, int]] = {}
    for item in outcomes:
        bucket = totals.setdefault(
            item.arm,
            {
                "days": 0,
                "recommendations": 0,
                "fills": 0,
                "matured_fills": 0,
                "net_profits": 0,
                "model_unscorable_rows": 0,
            },
        )
        bucket["days"] += 1
        bucket["recommendations"] += item.recommendations
        bucket["fills"] += item.fills
        bucket["matured_fills"] += item.matured_fills
        bucket["net_profits"] += item.net_profits
        bucket["model_unscorable_rows"] += item.model_unscorable_rows
    return totals


def _fold_selection_metrics(
    outcomes: Sequence[DayOutcome],
    fold_id: int,
) -> dict[str, Any]:
    """OOS net-return summary for the frozen candidate-selection tie-break rule."""
    report: dict[str, Any] = {}
    for arm in (ARM_TREATMENT, ARM_BASELINE):
        selected = [item for item in outcomes if item.fold_id == fold_id and item.arm == arm]
        returns = [value for item in selected for value in item.net_returns]
        matured = sum(item.matured_fills for item in selected)
        profits = sum(item.net_profits for item in selected)
        report[arm] = {
            "matured_fills": matured,
            "net_profit_rate": profits / matured if matured else None,
            "mean_net_return": float(np.mean(returns)) if returns else None,
            "tail_loss_p05": float(np.percentile(returns, 5)) if returns else None,
            "recommendations": sum(item.recommendations for item in selected),
            "fills": sum(item.fills for item in selected),
        }
    return report


def _sample_basis(
    rows: Sequence[Mapping[str, Any]],
    test_dates: set[Any],
    *,
    date_field: str,
) -> dict[str, Any]:
    counted = [row for row in rows if _day(row.get(date_field)) in test_dates]
    modes = [CAPTURE_OBSERVED, CAPTURE_REPLAYED]
    per_mode = {
        mode: sum(1 for row in counted if str(row.get("capture_mode")) == mode) for mode in modes
    }
    other = len(counted) - sum(per_mode.values())
    return {
        "test_rows": len(counted),
        "observed_rows": per_mode[CAPTURE_OBSERVED],
        "replayed_rows": per_mode[CAPTURE_REPLAYED],
        "unmarked_rows": other,
        "rule": "replayed 样本可以支撑训练，但不得冒充 observed 样本量",
    }
