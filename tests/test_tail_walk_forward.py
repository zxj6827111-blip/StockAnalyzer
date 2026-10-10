"""尾盘链路滚动前推验证的验收测试（改进计划 §4 选股质量验收）。

钉的是编排纪律，不是"能不能凑出一个好看的数字"：折边界必须带 embargo、标签必须
先成熟、身份不通过就整体验证不成立、未成交样本绝不进净盈利率分母、样本不足只能
得到 blocked。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
)
from stock_analyzer.models.tail_net_profit_trainer import (
    KIND_LOGISTIC,
    DateSplit,
    TailModelSpec,
    TailTrainingError,
    train_tail_net_profit_model,
)
from stock_analyzer.research.tail_walk_forward import (
    ARM_BASELINE,
    ARM_TREATMENT,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    build_rolling_splits,
    run_walk_forward,
    score_rows,
    sufficiency_blockers,
)

CONTRACT = DEFAULT_TREND_CONTRACT
# 特征列名必须都登记在 trend 特征契约的四组可复现行情信息里，否则训练入口会拒绝
# （见 assert_training_features）。ret_5 带可学信号，close_position 是常数噪声列。
FEATURES = ["ret_5", "close_position"]
START = date(2024, 1, 2)


def _trade_days(count: int) -> list[date]:
    days, cursor = [], START
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _rows(*, days: int = 120, labelled: bool = True) -> list[dict]:
    """每天 6 只：3 只高信号盈利、2 只低信号亏损、1 只未成交（无标签）。

    ``composite_score`` 与真实结果**反相关**，模拟"旧合成分排序"这个匹配基线。
    """
    out: list[dict] = []
    for day in _trade_days(days):
        for index in range(6):
            unfilled = index == 5
            positive = index < 3
            strength = 0.9 if positive else 0.1
            label = None if not labelled else (None if unfilled else (1 if positive else 0))
            out.append({
                "symbol": f"{600000 + index}.SH",
                "decision_date": day,
                "entry_date": day + timedelta(days=1),
                "ret_5": strength,
                "close_position": 0.5,
                "composite_score": 1.0 - strength,
                "label": label,
                "filled": not unfilled,
                "trainable": not unfilled,
                "net_return": (0.06 if positive else -0.05) if not unfilled else None,
                "reason": "" if not unfilled else "tail_confirmation_failed",
                "capture_mode": "replayed_recompute",
                "risk_state": "",
                "fill_time": f"{day.isoformat()}T14:31:00" if not unfilled else "",
            })
    return out


def _identity_kwargs() -> dict:
    return {
        "model_id": "trend-tail-lgbm",
        "training_commit": "cafe123",
        "runtime_commit": "cafe123",
        "feature_compute_version": 1,
        "label_policy_id": "label_policy_v4_07335bbe3d3e",
    }


def _run(rows: list[dict], **overrides) -> dict:
    kwargs = {
        "rows": rows,
        "feature_names": FEATURES,
        "spec": TailModelSpec(kind=KIND_LOGISTIC, min_train_samples=150,
                              min_calibration_samples=30, min_test_samples=30),
        "folds": 4,
        "min_train_sessions": 40,
        "calibration_sessions": 12,
        "contract": CONTRACT,
        **_identity_kwargs(),
    }
    kwargs.update(overrides)
    return run_walk_forward(**kwargs)


# ---------------------------------------------------------------------------
# 折边界
# ---------------------------------------------------------------------------


def test_rolling_splits_are_disjoint_embargoed_and_expand_forward() -> None:
    days = _trade_days(120)
    splits = build_rolling_splits(
        days, folds=4, contract=CONTRACT,
        calibration_sessions=12, min_train_sessions=40,
    )
    assert len(splits) == 4
    embargo = CONTRACT.holding_days

    tested = [day for split in splits for day in split.test_dates]
    assert len(tested) == len(set(tested)), "测试折之间不能重叠"
    assert tested == sorted(tested)
    assert tested[-1] == days[-1], "最后一折必须吃满尾部，零头不能被丢掉"

    for split in splits:
        train_gap = _gap(days, split.train_dates[-1], split.calibration_dates[0])
        calib_gap = _gap(days, split.calibration_dates[-1], split.test_dates[0])
        assert train_gap >= embargo and calib_gap >= embargo


def test_rolling_splits_grow_the_training_window() -> None:
    days = _trade_days(120)
    splits = build_rolling_splits(days, folds=4, contract=CONTRACT)
    sizes = [len(split.train_dates) for split in splits]
    assert sizes == sorted(sizes) and len(set(sizes)) > 1


def test_rolling_splits_refuse_to_invent_a_fold_out_of_thin_data() -> None:
    with pytest.raises(ValueError, match="cannot fill"):
        build_rolling_splits(_trade_days(20), folds=4, contract=CONTRACT)


def _gap(days: list[date], left: date, right: date) -> int:
    return len([day for day in days if left < day < right])


# ---------------------------------------------------------------------------
# 注入的折必须由训练器自己核对 embargo（标签成熟纪律不能靠调用方自觉）
# ---------------------------------------------------------------------------


def test_injected_split_without_embargo_is_rejected() -> None:
    days = _trade_days(120)
    rows = _rows()
    tight = DateSplit(
        train_dates=tuple(days[:40]),
        calibration_dates=tuple(days[40:52]),
        test_dates=tuple(days[52:72]),
        embargo_sessions=CONTRACT.holding_days,
    )
    with pytest.raises(TailTrainingError, match="train→calibration"):
        train_tail_net_profit_model(
            rows=rows, feature_names=FEATURES, label_field="label",
            date_field="decision_date", contract=CONTRACT,
            model_id="m", training_commit="cafe123", feature_compute_version=1,
            label_policy_id="label_policy_v4_x", split=tight,
        )


def test_injected_split_with_a_real_embargo_trains() -> None:
    splits = build_rolling_splits(_trade_days(120), folds=4, contract=CONTRACT)
    artifact = train_tail_net_profit_model(
        rows=_rows(), feature_names=FEATURES, label_field="label",
        date_field="decision_date", contract=CONTRACT, spec=TailModelSpec(
            kind=KIND_LOGISTIC, min_train_samples=150,
            min_calibration_samples=30, min_test_samples=30,
        ),
        model_id="m", training_commit="cafe123", feature_compute_version=1,
        label_policy_id="label_policy_v4_x", split=splits[0],
    )
    assert artifact["split"]["test_dates"] == len(splits[0].test_dates)
    assert artifact["metrics"]["overall"]["test_auc"] is not None


# ---------------------------------------------------------------------------
# 编排：跑通 / 阻塞 / 身份失败
# ---------------------------------------------------------------------------


def test_walk_forward_produces_four_folds_and_a_matched_baseline() -> None:
    report = _run(_rows())
    assert report["status"] == STATUS_COMPLETED
    assert report["fold_count"] == 4
    assert report["arms"] == {
        "treatment": ARM_TREATMENT, "baseline": ARM_BASELINE,
        "baseline_rank_field": "composite_score",
    }
    quality = report["quality"]
    assert quality["test_folds"] >= 1
    assert quality["paired_days"] > 0
    # 高信号样本是全部可学的：新排序必须真的比反着排的旧合成分选得好。
    assert (
        quality["treatment"]["net_profit_rate"]
        > quality["baseline"]["net_profit_rate"]
    )
    assert all(item["artifact_digest"] for item in report["folds"])


def test_walk_forward_separates_observed_and_replayed_sample_counts() -> None:
    rows = _rows()
    for index, row in enumerate(rows):
        if index % 3 == 0:
            row["capture_mode"] = "observed_snapshot"
    basis = _run(rows)["sample_basis"]
    assert basis["observed_rows"] and basis["replayed_rows"]
    assert basis["test_rows"] == (
        basis["observed_rows"] + basis["replayed_rows"] + basis["unmarked_rows"]
    )
    assert "不得冒充" in basis["rule"]


def test_uncertain_selected_rows_are_neither_profit_nor_loss_samples() -> None:
    """买入过但交易状态未知（trainable=False）的样本：占推荐名额，不进净盈利率分母。"""
    rows = _rows()
    for row in rows:
        if row["symbol"] == "600000.SH":
            row["trainable"] = False
            row["net_return"] = None
            row["label"] = None
            row["reason"] = "unknown_trade_status"
    totals = _run(rows)["arm_totals"][ARM_TREATMENT]
    assert totals["recommendations"] > totals["matured_fills"] > 0
    assert totals["net_profits"] <= totals["matured_fills"]
    assert totals["fills"] == totals["recommendations"]


def test_identity_mismatch_blocks_the_whole_validation_instead_of_scoring_anything() -> None:
    report = _run(_rows(), runtime_commit="beef999")
    assert report["status"] == STATUS_BLOCKED
    assert report["metrics_computed"] is False
    assert "training_runtime_commit_mismatch" in report["blockers"]
    assert "quality" not in report


def test_missing_runtime_commit_is_blocked_not_assumed() -> None:
    report = _run(_rows(), runtime_commit="")
    assert report["status"] == STATUS_BLOCKED
    assert "runtime_commit_unknown" in report["blockers"]


def test_no_labels_yields_blocked_without_any_hit_rate_number() -> None:
    report = _run(_rows(labelled=False))
    assert report["status"] == STATUS_BLOCKED
    assert report["metrics_computed"] is False
    assert "no_labelled_samples" in report["blockers"][0]
    assert "开盘回测" in report["blockers"][0]
    assert "quality" not in report
    assert "note" in report


def test_thin_history_is_reported_as_insufficient_not_run_through() -> None:
    report = _run(_rows(days=30))
    assert report["status"] == STATUS_BLOCKED
    assert any("insufficient_trade_dates" in item for item in report["blockers"])


def test_sufficiency_blockers_is_a_standalone_gate() -> None:
    rows = _rows(labelled=False)
    blockers = sufficiency_blockers(
        rows, date_field="decision_date", label_field="label", folds=4,
        contract=CONTRACT, calibration_sessions=12, min_train_sessions=40,
    )
    assert blockers and blockers[0].startswith("no_labelled_samples")


# ---------------------------------------------------------------------------
# 打分路径必须和训练用的是同一个校准器
# ---------------------------------------------------------------------------


def test_score_rows_requires_a_calibrator_and_uses_it() -> None:
    with pytest.raises(ValueError, match="calibrator"):
        score_rows({"model": object()}, [{"ret_5": 1.0}], feature_names=FEATURES)

    splits = build_rolling_splits(_trade_days(120), folds=4, contract=CONTRACT)
    artifact = train_tail_net_profit_model(
        rows=_rows(), feature_names=FEATURES, label_field="label",
        date_field="decision_date", contract=CONTRACT, spec=TailModelSpec(
            kind=KIND_LOGISTIC, min_train_samples=150,
            min_calibration_samples=30, min_test_samples=30,
        ),
        model_id="m", training_commit="cafe123", feature_compute_version=1,
        label_policy_id="label_policy_v4_x", split=splits[0],
    )
    probabilities = score_rows(
        artifact,
        [
            {"symbol": "600000.SH", "ret_5": 0.9, "close_position": 0.5},
            {"symbol": "600001.SH", "ret_5": 0.1, "close_position": 0.5},
        ],
        feature_names=FEATURES,
    )
    assert probabilities[0] > probabilities[1]
    assert all(0.0 <= value <= 1.0 for value in probabilities)


def test_probability_field_is_the_new_one_not_the_old_composite() -> None:
    assert NET_PROFIT_PROBABILITY_FIELD == "p_net_profit_5d_tail"
    report = _run(_rows())
    assert report["arms"]["treatment"] == NET_PROFIT_PROBABILITY_FIELD


def test_walk_forward_refuses_features_it_cannot_reproduce() -> None:
    """§3.2 的门在编排层同样成立：一折都不许用不可复现的信息列去训。"""
    with pytest.raises(TailTrainingError, match="不可复现的信息源"):
        _run(_rows(), feature_names=["ret_5", "news_sentiment"])


# ---------------------------------------------------------------------------
# CLI：退出码必须是真实退出码
# ---------------------------------------------------------------------------

_CLI = Path(__file__).resolve().parents[1] / "scripts" / "validate_tail_selection_quality.py"


def _write_samples(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "samples.jsonl"
    path.write_text(
        "\n".join(json.dumps({
            **row,
            "decision_date": row["decision_date"].isoformat(),
            "entry_date": row["entry_date"].isoformat(),
        }) for row in rows),
        encoding="utf-8",
    )
    return path


def _cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )


def test_cli_returns_blocked_exit_when_samples_have_no_labels(tmp_path) -> None:
    samples = _write_samples(tmp_path, _rows(labelled=False))
    result = _cli([
        "--samples", str(samples), "--features", "ret_5,close_position",
        "--model-id", "m", "--training-commit", "cafe123", "--runtime-commit", "cafe123",
        "--feature-compute-version", "1", "--label-policy-id", "label_policy_v4_x",
        "--out", str(tmp_path / "report.json"),
    ])
    assert result.returncode == 3, result.stdout + result.stderr
    assert "blocked" in result.stdout
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["metrics_computed"] is False


def test_cli_fails_visibly_on_an_unreadable_sample_file(tmp_path) -> None:
    result = _cli([
        "--samples", str(tmp_path / "missing.jsonl"), "--features", "ret_5",
        "--model-id", "m", "--training-commit", "cafe123", "--runtime-commit", "cafe123",
        "--feature-compute-version", "1", "--label-policy-id", "label_policy_v4_x",
        "--out", str(tmp_path / "report.json"),
    ])
    assert result.returncode == 5, result.stdout + result.stderr
    assert "样本不可用" in result.stderr


def test_cli_runs_the_full_walk_forward_and_writes_a_report(tmp_path) -> None:
    samples = _write_samples(tmp_path, _rows())
    out = tmp_path / "report.json"
    result = _cli([
        "--samples", str(samples), "--features", "ret_5,close_position",
        "--model-id", "m", "--training-commit", "cafe123", "--runtime-commit", "cafe123",
        "--feature-compute-version", "1", "--label-policy-id", "label_policy_v4_x",
        "--folds", "4", "--out", str(out),
    ])
    assert result.returncode in (0, 4), result.stdout + result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == STATUS_COMPLETED
    assert payload["fold_count"] == 4
    assert "0.60 是初始选股规则" in " ".join(payload["caveats"])


def test_cli_refuses_illegal_features_before_reading_samples(tmp_path) -> None:
    """特征清单不合法时不必去解析样本文件：退出码要能区分"这条链根本不该训"。"""
    result = _cli([
        "--samples", str(tmp_path / "missing.jsonl"), "--features", "news_sentiment",
        "--model-id", "m", "--training-commit", "cafe123", "--runtime-commit", "cafe123",
        "--feature-compute-version", "1", "--label-policy-id", "label_policy_v4_x",
        "--out", str(tmp_path / "report.json"),
    ])
    assert result.returncode == 5, result.stdout + result.stderr
    assert "特征不符合 trend 契约" in result.stderr
    assert not (tmp_path / "report.json").exists()
