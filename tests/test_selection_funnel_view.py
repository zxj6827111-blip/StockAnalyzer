"""整条选股漏斗视图的验收（改进计划 §2：九层都要能追溯，缺的要说出缺在哪）。

关键约束是**不许把"没留档"折算成"这层没淘汰股票"**：夜扫半段只有成员与计数，
``universe`` / ``hard_eligibility`` 干脆没有生产者，视图必须让这些事实以
``recorded=false`` 的形式留在报告里，并据此判定 §2 的哪个诊断问题现在答不了。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    FUNNEL_LAYERS,
)
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    KIND_PREDICTIVE,
    ModelIdentity,
    build_funnel_trace,
    read_trace,
    record_stage,
    write_trace,
)
from stock_analyzer.research.selection_funnel_view import build_selection_funnel_view

CONTRACT = DEFAULT_TREND_CONTRACT
DAY = date(2026, 10, 9)


def _identity() -> ModelIdentity:
    return ModelIdentity(
        model_id="trend-tail-lgbm-1", artifact_content_hash="sha256:deadbeef",
        training_commit="eb691ed", runtime_commit="eb691ed", feature_compute_version=5,
        label_policy_id="label_policy_v4_abc", contract_digest=CONTRACT.digest(),
    )


def _stage(name: str, kind: str, *, inputs: int, kept: list[str],
           rejected: dict[str, list[str]] | None = None,
           features: tuple[str, ...] = ("avg_turnover_20",)) -> dict:
    return record_stage(
        stage=name, kind=kind,
        input_symbols=[f"S{i}" for i in range(inputs)],
        advanced_symbols=kept, rejected=rejected or {},
        data_as_of="2026-10-09T14:50:00", contract=CONTRACT,
        model_identity=_identity(), feature_compute_version=5,
        label_policy_id="label_policy_v4_abc", features_used=list(features),
    )


def _tail_traces(tmp_path: Path) -> list[dict]:
    entry = build_funnel_trace(
        trade_date=DAY,
        stages=[
            _stage("night_watch_pool", KIND_PREDICTIVE, inputs=3,
                   kept=["A", "B", "C"]),
            _stage("tail_confirmation", KIND_HARD_GATE, inputs=3, kept=["A", "B"],
                   rejected={"minute_bars_unavailable": ["C"]}),
            _stage("final_recommendation", KIND_PREDICTIVE, inputs=2, kept=["A"],
                   rejected={"below_threshold": ["B"]}),
        ],
        final_recommendations=[], rejected_final=[], contract=CONTRACT,
    )
    path = write_trace(entry, tmp_path, contract=CONTRACT)
    exit_trace = build_funnel_trace(
        trade_date=DAY,
        stages=[_stage("execution_exit", KIND_HARD_GATE, inputs=1, kept=["A"])],
        final_recommendations=[], rejected_final=[], contract=CONTRACT,
    )
    write_trace(exit_trace, tmp_path, suffix="execution_exit", contract=CONTRACT)
    return [read_trace(path)]


def _night(members: dict[str, list[str]]) -> dict:
    return {
        "trade_date": "2026-10-09", "signal_date": "2026-10-09",
        "quality_members": [{"symbol": s} for s in members["quality"]],
        "light_members": [{"symbol": s} for s in members["light"]],
        "deep_members": [{"symbol": s} for s in members["deep"]],
    }


def test_missing_layers_are_reported_missing_not_zero_dropped(tmp_path: Path) -> None:
    view = build_selection_funnel_view(
        night_funnel=None, tail_traces=_tail_traces(tmp_path))
    unrecorded = view["coverage"]["layers_unrecorded"]
    assert {"universe", "hard_eligibility", "quality_300", "deep_50"} <= set(unrecorded)
    layers = {row["layer"]: row for row in view["layers"]}
    # "没留档"的层不许出现 inputs=0：那会被读成"这层没淘汰任何股票"。
    assert layers["universe"].get("inputs") is None
    assert layers["universe"]["gaps"] == ["no_producer_records_this_layer"]
    assert layers["night_watch_pool"]["reasons_available"] is True
    assert layers["final_recommendation"]["rejected_by_reason"] == {"below_threshold": ["B"]}
    diagnosis = view["diagnosis"]
    assert diagnosis["screening_too_early"]["answerable_with_current_records"] is False
    assert diagnosis["data_or_degradation_impact"]["answerable_with_current_records"] is True


def test_night_half_join_only_derives_drops_when_membership_is_a_subset(tmp_path: Path) -> None:
    good = _night({"quality": ["A", "B", "C"], "light": ["A", "B"], "deep": ["A"]})
    view = build_selection_funnel_view(night_funnel=good, tail_traces=_tail_traces(tmp_path))
    layers = {row["layer"]: row for row in view["layers"]}
    assert layers["quality_300"]["inputs"] is None          # 上一层没留档，不硬推
    assert layers["light_100"]["inputs"] == 3
    assert layers["light_100"]["dropped"] == 1
    assert view["consistent"] is True

    bad = _night({"quality": ["A", "B"], "light": ["A", "Z"], "deep": ["A"]})
    view = build_selection_funnel_view(night_funnel=bad, tail_traces=_tail_traces(tmp_path))
    layers = {row["layer"]: row for row in view["layers"]}
    assert layers["light_100"]["inputs"] is None            # 不是子集就不落数
    assert "membership_is_not_a_subset_of_previous_layer" in layers["light_100"]["gaps"]
    assert view["consistent"] is False
    # 有成员但没逐只拒绝原因：这三层要出现在"答不了原因分布"的清单里。
    assert view["coverage"]["layers_without_reasons"] == ["quality_300", "light_100", "deep_50"]


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "audit_selection_funnel_cli",
        Path(__file__).resolve().parents[1] / "scripts" / "audit_selection_funnel.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_reports_the_gap_and_writes_the_view(tmp_path: Path) -> None:
    cli = _load_cli()
    traces_dir = tmp_path / "shadow"
    traces_dir.mkdir()
    _tail_traces(traces_dir)
    out = tmp_path / "funnel_view.json"
    rc = cli.main(["--tail-dir", str(traces_dir), "--out", str(out), "--quiet"])
    assert rc == 3                      # universe/hard_eligibility 至今没有生产者
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["coverage"]["layers_total"] == len(FUNNEL_LAYERS)
    assert "execution_exit" in [row["layer"] for row in payload["layers"]]

    assert cli.main(["--tail-dir", str(tmp_path / "nowhere"), "--quiet"]) == 5
    assert cli.main(["--tail-dir", str(out), "--quiet"]) == 5      # 目录里没有留档
