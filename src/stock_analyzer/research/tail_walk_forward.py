"""尾盘链路的滚动前推验证：把计划 §4 的"按交易日滚动训练、校准和测试"跑成一条命令。

这里唯一新增的是**编排**：折边界、训练、校准、排序、判定全部复用线上那几套权威实现
（``train_tail_net_profit_model`` / ``rank_final_recommendations`` /
``evaluate_selection_quality``），因为"同一份输入在线上与历史路径必须给出一致判定"
不允许验证器自带一套排序逻辑。

三件事是刻意的：

- 样本不足时返回 ``blocked`` 并给出缺口，**不产任何命中率数字**；
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
    MIN_TEST_FOLDS,
    DateSplit,
    DayOutcome,
    TailModelSpec,
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
    warmup = (
        min_train_sessions
        + calibration_sessions
        + 2 * embargo
    )
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
        splits.append(DateSplit(
            train_dates=tuple(ordered[:train_end]),
            calibration_dates=tuple(ordered[calibration_start:calibration_end]),
            test_dates=tuple(ordered[test_start:test_end]),
            embargo_sessions=embargo,
        ))
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


def _outcome(
    day: date,
    arm: str,
    selected: Sequence[str],
    pool: Sequence[Mapping[str, Any]],
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
    )


def _select_by_field(
    pool: Sequence[Mapping[str, Any]], field: str, *, cap: int
) -> list[str]:
    """匹配基线：同一合格池、同一名额上限，只把排序键换成旧的合成分。

    基线**不吃 0.60 阈值**——那个阈值是新概率的准入规则，套到旧分数上就变成两回事了。
    """
    tradeable = [row for row in pool if row.get("filled")]
    ranked = sorted(
        tradeable,
        key=lambda row: (-float(row.get(field) or 0.0), str(row.get("symbol"))),
    )
    return [str(row.get("symbol")) for row in ranked[:max(0, int(cap))]]


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
    date_field: str,
    label_field: str,
    folds: int,
    contract: TrendStrategyContract,
    calibration_sessions: int,
    min_train_sessions: int,
) -> list[str]:
    """样本够不够跑滚动验证——不够就直说，别用"跑通了"掩盖空结果。"""
    dates = _observed_days(rows, date_field=date_field)
    labelled = [row for row in rows if row.get(label_field) in (0, 1, 0.0, 1.0)]
    blockers: list[str] = []
    if not labelled:
        blockers.append(
            "no_labelled_samples: p_net_profit_5d_tail 的真实成熟标签还没有生产者，"
            "尾盘策略验证记为阻塞（不得用开盘回测代替）"
        )
        return blockers
    try:
        build_rolling_splits(
            dates, folds=folds, contract=contract,
            calibration_sessions=calibration_sessions,
            min_train_sessions=min_train_sessions,
        )
    except ValueError as exc:
        blockers.append(f"insufficient_trade_dates: {exc}")
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
    spec: TailModelSpec | None = None,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    date_field: str = "decision_date",
    label_field: str = "label",
    probability_field: str = NET_PROFIT_PROBABILITY_FIELD,
    baseline_rank_field: str = "composite_score",
    folds: int = MIN_TEST_FOLDS,
    calibration_sessions: int = DEFAULT_CALIBRATION_SESSIONS,
    min_train_sessions: int = DEFAULT_MIN_TRAIN_SESSIONS,
) -> dict[str, Any]:
    """滚动训练 → 校准 → 测试，并把每日结果交给唯一的质量判定门。"""
    blockers = sufficiency_blockers(
        rows, date_field=date_field, label_field=label_field, folds=folds,
        contract=contract, calibration_sessions=calibration_sessions,
        min_train_sessions=min_train_sessions,
    )
    if blockers:
        return {
            "status": STATUS_BLOCKED,
            "blockers": blockers,
            "metrics_computed": False,
            "note": "样本不足时不产命中率数字；继续采集带时刻的分钟行情后再跑",
        }

    all_dates = _observed_days(rows, date_field=date_field)
    by_date: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        day = _day(row.get(date_field))
        if day is not None:
            by_date.setdefault(day, []).append(dict(row))

    splits = build_rolling_splits(
        all_dates, folds=folds, contract=contract,
        calibration_sessions=calibration_sessions,
        min_train_sessions=min_train_sessions,
    )
    outcomes: list[DayOutcome] = []
    fold_reports: list[dict[str, Any]] = []
    blocked_folds: list[dict[str, Any]] = []

    for index, split in enumerate(splits):
        artifact = train_tail_net_profit_model(
            rows=rows,
            feature_names=feature_names,
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
            if not pool:
                continue
            probabilities = score_rows(artifact, pool, feature_names=feature_names)
            candidates = [
                _candidate_row(row, probability, probability_field)
                for row, probability in zip(pool, probabilities, strict=True)
            ]
            ranked = rank_final_recommendations(
                trade_date=day,
                rows=candidates,
                model_identity=identity,
                contract=contract,
                probability_field=probability_field,
            )
            outcomes.append(_outcome(
                day, ARM_TREATMENT, list(ranked.symbols), pool
            ))
            outcomes.append(_outcome(
                day, ARM_BASELINE,
                _select_by_field(
                    pool, baseline_rank_field,
                    cap=contract.max_final_recommendations,
                ),
                pool,
            ))
            fold_days.append(str(day))

        fold_reports.append({
            "fold": index + 1,
            "split": split.as_dict(),
            "artifact_digest": artifact["artifact_digest"],
            "calibration_auc": artifact["calibration_auc"],
            "test_metrics": artifact["metrics"]["overall"],
            "capture_modes_in_test": artifact["metrics"]["reported_separately"],
            "evaluated_days": fold_days,
        })

    if blocked_folds:
        return {
            "status": STATUS_BLOCKED,
            "blockers": [item["reason"] for item in blocked_folds],
            "blocked_folds": blocked_folds,
            "metrics_computed": False,
        }

    quality = evaluate_selection_quality(
        outcomes=outcomes,
        candidate_days=len(all_dates),
        baseline_arm=ARM_BASELINE,
        treatment_arm=ARM_TREATMENT,
    )
    treatment_matured = sum(
        item.matured_fills for item in outcomes if item.arm == ARM_TREATMENT
    )
    test_dates = {
        day for split in splits for day in split.test_dates
    }
    sample_basis = _sample_basis(rows, test_dates, date_field=date_field)
    return {
        "status": STATUS_COMPLETED,
        "metrics_computed": True,
        "folds": fold_reports,
        "fold_count": len(fold_reports),
        "arm_totals": _arm_totals(outcomes),
        "contract_digest": contract.digest(),
        "contract_version": contract.contract_version,
        "arms": {"treatment": ARM_TREATMENT, "baseline": ARM_BASELINE,
                 "baseline_rank_field": baseline_rank_field},
        "sample_basis": sample_basis,
        "quality": quality,
        "shadow_readiness": shadow_readiness(
            observed_trade_days=len({
                _day(row.get(date_field)) for row in rows
                if str(row.get("capture_mode")) == CAPTURE_OBSERVED
            } & test_dates),
            matured_simulated_fills=treatment_matured,
        ),
        "caveats": [
            "阈值 0.60 是初始选股规则，不代表已证明命中率达到 60%",
            "observed 与 replayed 样本分开计数，不互借样本量",
        ],
    }


def _arm_totals(outcomes: Sequence[DayOutcome]) -> dict[str, dict[str, int]]:
    """逐臂合计。分母口径要能被外部核对，否则"净盈利率"只是一个无法追问的数字。"""
    totals: dict[str, dict[str, int]] = {}
    for item in outcomes:
        bucket = totals.setdefault(item.arm, {
            "days": 0, "recommendations": 0, "fills": 0,
            "matured_fills": 0, "net_profits": 0,
        })
        bucket["days"] += 1
        bucket["recommendations"] += item.recommendations
        bucket["fills"] += item.fills
        bucket["matured_fills"] += item.matured_fills
        bucket["net_profits"] += item.net_profits
    return totals


def _sample_basis(
    rows: Sequence[Mapping[str, Any]],
    test_dates: set[Any],
    *,
    date_field: str,
) -> dict[str, Any]:
    counted = [row for row in rows if _day(row.get(date_field)) in test_dates]
    modes = [CAPTURE_OBSERVED, CAPTURE_REPLAYED]
    per_mode = {
        mode: sum(1 for row in counted if str(row.get("capture_mode")) == mode)
        for mode in modes
    }
    other = len(counted) - sum(per_mode.values())
    return {
        "test_rows": len(counted),
        "observed_rows": per_mode[CAPTURE_OBSERVED],
        "replayed_rows": per_mode[CAPTURE_REPLAYED],
        "unmarked_rows": other,
        "rule": "replayed 样本可以支撑训练，但不得冒充 observed 样本量",
    }
