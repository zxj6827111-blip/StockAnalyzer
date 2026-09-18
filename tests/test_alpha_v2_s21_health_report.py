"""S21 Daily Alpha Health Report 阶段验收测试（八块 + 文案语义检查）。"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.health_report import (
    BLOCK_NAMES,
    FORBIDDEN_AUTO_ACTIONS,
    REPORT_SCHEMA,
    REVIEW_ACTION,
    HealthReportSpec,
    assert_no_auto_action,
    audit_display_text,
    audit_report_text_blocks,
    build_health_report,
    render_markdown,
    review_trigger,
    write_health_report,
    write_health_report_markdown,
)


def _frame(
    *, days: int = 80, per_day: int = 40, signal: float = 1.0, seed: int = 5150
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for day_index in range(days):
        day = f"2026-{(day_index // 28) + 1:02d}-{(day_index % 28) + 1:02d}"
        excess = signal * rng.normal(0.0, 0.02, per_day)
        scores = excess + rng.normal(0.0, 0.01, per_day)
        for index in range(per_day):
            rows.append(
                {
                    "decision_date": day,
                    "symbol": f"{600000 + index:06d}",
                    "executable": index % 25 != 0,
                    "no_fill_reason": "" if index % 25 != 0 else "limit_up_open",
                    "entry_delay_sessions": 1 if index % 25 != 0 else "not_available",
                    "matured_3d": True,
                    "matured_5d": True,
                    "matured_10d": True,
                    "matured_15d": True,
                    "excess_return_3d": float(excess[index] * 0.6),
                    "excess_return_5d": float(excess[index]),
                    "excess_return_10d": float(excess[index] * 1.2),
                    "excess_return_15d": float(excess[index] * 1.4),
                    "net_return_3d": float(excess[index] * 0.6 + 0.001),
                    "net_return_5d": float(excess[index] + 0.001),
                    "net_return_10d": float(excess[index] * 1.2 + 0.001),
                    "net_return_15d": float(excess[index] * 1.4 + 0.001),
                    "mae_5d": float(-abs(excess[index]) - 0.01),
                    "alpha_rank_score": float(pd.Series(scores).rank(pct=True).iloc[index]),
                    "expected_excess_return_5d": float(excess[index]),
                    "p_up_net_5d": 0.5 + 0.05 * np.sign(excess[index]),
                    "expected_mae_5d": float(-abs(excess[index])),
                    "baseline_score": float(pd.Series(excess).rank(pct=True).iloc[index]),
                    "legacy_score": 50.0 + float(excess[index]) * 100,
                    "quality_pool": index < 30,
                    "light_pool": index < 15,
                    "deep_pool": index < 8,
                    "final_pool": index < 2,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 八块结构
# ---------------------------------------------------------------------------


def test_report_contains_all_eight_blocks() -> None:
    report = build_health_report(
        frame=_frame(),
        identity={"code_commit": "abc", "config_hash": "def"},
        data_health={"level": "healthy"},
        funnel={"eligible": 5000, "quality": 300, "light": 100, "deep": 50},
        report_date="2026-03-20",
    )
    payload = report.to_payload()
    assert payload["schema"] == REPORT_SCHEMA
    for block in BLOCK_NAMES:
        assert block in payload, block
    assert set(payload["block_status"]) == set(BLOCK_NAMES)
    assert payload["block_status"]["alpha_quality"] == "ok"
    assert payload["block_status"]["data_health"] == "ok"


def test_report_without_inputs_marks_blocks_not_available() -> None:
    payload = build_health_report().to_payload()
    assert payload["identity"]["status"] == "not_available"
    assert payload["winner_recall"]["status"] == "not_available"
    assert payload["alpha_quality"]["status"] == "not_available"
    assert payload["review_trigger"]["status"] == "not_available"


def test_alpha_quality_reports_rank_ic_for_all_horizons() -> None:
    payload = build_health_report(frame=_frame()).to_payload()
    quality = payload["alpha_quality"]
    assert set(quality["rank_ic"]) == {"3d", "5d", "10d", "15d"}
    assert quality["rank_ic"]["5d"]["mean_ic"] > 0
    assert quality["rank_ic"]["5d"]["ic_20d"] is not None
    assert quality["quantiles"]["monotonicity"]["status"] == "ok"


def test_score_distribution_reports_available_columns_only() -> None:
    payload = build_health_report(frame=_frame()).to_payload()
    block = payload["score_distribution"]
    assert block["status"] == "ok"
    assert block["columns"]["alpha_rank_score"]["count"] > 0
    assert block["columns"]["p_up_net_5d"]["median"] == pytest.approx(0.5, abs=0.2)


def test_execution_block_counts_no_fill_reasons() -> None:
    payload = build_health_report(frame=_frame()).to_payload()
    block = payload["execution"]
    assert block["status"] == "ok"
    assert 0.0 < block["fill_rate"] < 1.0
    assert block["no_fill_reasons"]["limit_up_open"] > 0
    assert block["entry_delay_sessions"]["max"] == 1


def test_winner_recall_block_reports_research_gate() -> None:
    payload = build_health_report(frame=_frame(days=80)).to_payload()
    block = payload["winner_recall"]
    assert block["status"] == "ok"
    assert block["research_gate"] in {
        "insufficient",
        "failure_alert_only",
        "initial_direction_review",
    }
    # 至少给出 light/deep 两级的滚动召回（每日值落在 daily 明细，摘要给均值与滚动）
    assert "recall_light_mean" in block and "recall_deep_mean" in block
    assert block["recall_light_20d"] is not None or block["recall_light_20d"] == "not_available"


def test_drift_block_reports_prediction_drift() -> None:
    payload = build_health_report(frame=_frame(days=30)).to_payload()
    drift = payload["drift_governance"]
    assert drift["status"] == "ok"
    assert "prediction_drift" in drift
    assert drift["prediction_drift"]["days"] == 30


def test_explicit_execution_and_drift_override_computed_blocks() -> None:
    payload = build_health_report(
        frame=_frame(),
        execution={"status": "ok", "fill_rate": 0.99},
        drift={"status": "ok", "model_age_days": 33},
    ).to_payload()
    assert payload["execution"]["fill_rate"] == 0.99
    assert payload["drift_governance"]["model_age_days"] == 33


# ---------------------------------------------------------------------------
# Review Trigger
# ---------------------------------------------------------------------------


def test_review_trigger_is_insufficient_without_sample() -> None:
    payload = build_health_report(frame=_frame(days=5)).to_payload()
    trigger = payload["review_trigger"]
    assert trigger["status"] == "insufficient_sample"
    assert trigger["triggered"] is False
    assert trigger["action"] == REVIEW_ACTION


def test_review_trigger_fires_on_persistent_negative_quality() -> None:
    quality = {
        "status": "ok",
        "mature_dates": 90,
        "rank_ic": {"5d": {"ic_20d": -0.02, "ic_60d": -0.03}},
        "topk": {"top5": {"excess_return_5d": -0.004}},
    }
    trigger = review_trigger({"alpha_quality": quality})
    assert trigger["triggered"] is True
    assert trigger["action"] == REVIEW_ACTION
    assert "ic_20d_below_zero" in trigger["reasons"]
    for forbidden in FORBIDDEN_AUTO_ACTIONS:
        assert forbidden not in trigger["action"]


def test_review_trigger_holds_when_only_one_condition_met() -> None:
    quality = {
        "status": "ok",
        "mature_dates": 90,
        "rank_ic": {"5d": {"ic_20d": -0.02, "ic_60d": 0.01}},
        "topk": {"top5": {"excess_return_5d": -0.004}},
    }
    assert review_trigger({"alpha_quality": quality})["triggered"] is False


def test_review_trigger_on_real_report_never_auto_acts() -> None:
    payload = build_health_report(frame=_frame()).to_payload()
    assert_no_auto_action(payload)
    assert payload["spec"]["review_action"] == REVIEW_ACTION
    assert set(payload["review_trigger"]["forbidden_actions"]) == set(FORBIDDEN_AUTO_ACTIONS)


def test_assert_no_auto_action_rejects_auto_threshold_change() -> None:
    payload = {"review_trigger": {"action": "auto_lower_threshold"}}
    with pytest.raises(AssertionError, match="human_review_only"):
        assert_no_auto_action(payload)


# ---------------------------------------------------------------------------
# 文案语义检查（DF-S09-002）
# ---------------------------------------------------------------------------


def test_rank_score_must_not_be_labelled_as_probability() -> None:
    check = audit_display_text("600000 上涨概率 0.62", column="alpha_rank_score")
    assert check["verdict"] == "mislabeled_as_probability"
    assert "上涨概率" in check["violations"]


def test_uncalibrated_direction_must_not_be_labelled_as_probability() -> None:
    check = audit_display_text(
        "方向分 0.58（上涨概率）",
        column="p_up_net_5d",
        calibrated=False,
    )
    assert check["verdict"] == "mislabeled_as_probability"


def test_calibrated_direction_may_use_probability_wording() -> None:
    check = audit_display_text("正收益概率 0.58", column="p_up_net_5d_calibrated", calibrated=True)
    assert check["verdict"] == "ok"
    assert check["may_call_probability"] is True


def test_probability_wording_allowed_when_semantics_permits() -> None:
    check = audit_display_text(
        "正收益概率 0.58",
        column="p_up_net_5d",
        semantics={"may_call_probability": True},
    )
    assert check["verdict"] == "ok"


def test_report_text_blocks_audit_flags_violations() -> None:
    clean = audit_report_text_blocks({"title": "Alpha V2 健康报告", "note": "rank score 0.9"})
    assert clean["verdict"] == "ok"
    dirty = audit_report_text_blocks({"title": "Top5 上涨概率", "note": "ok"})
    assert dirty["verdict"] == "mislabeled_as_probability"
    assert dirty["violations"][0]["column"] == "title"


# ---------------------------------------------------------------------------
# 渲染与落盘
# ---------------------------------------------------------------------------


def test_render_markdown_covers_all_sections() -> None:
    payload = build_health_report(
        frame=_frame(),
        identity={"code_commit": "abc123"},
        data_health={"level": "healthy", "coverage": 0.99},
        funnel={"eligible": 5000, "deep": 50},
        report_date="2026-03-20",
    ).to_payload()
    markdown = render_markdown(payload)
    for index, title in enumerate(
        (
            "Identity",
            "Data Health",
            "Funnel",
            "Winner Recall",
            "Score Distribution",
            "Alpha Quality",
            "Execution",
            "Drift / Governance",
        ),
        start=1,
    ):
        assert f"## {index}. {title}" in markdown
    assert "## Review Trigger" in markdown
    assert "abc123" in markdown


def test_write_health_report_json_and_markdown(tmp_path) -> None:
    payload = build_health_report(frame=_frame(), report_date="2026-03-20").to_payload()
    json_path = write_health_report(root=tmp_path, payload=payload)
    markdown_path = write_health_report_markdown(root=tmp_path, payload=payload)
    assert json_path.name == "health_report_20260320.json"
    assert markdown_path.name == "health_report_20260320.md"
    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["schema"] == REPORT_SCHEMA
    assert "Alpha V2 健康报告" in markdown_path.read_text(encoding="utf-8")


def test_write_health_report_rejects_auto_action_payload(tmp_path) -> None:
    payload = build_health_report(frame=_frame()).to_payload()
    payload["review_trigger"]["action"] = "auto_retrain_and_promote"
    with pytest.raises(AssertionError):
        write_health_report(root=tmp_path, payload=payload)


def test_spec_payload_documents_trigger_rule() -> None:
    payload = HealthReportSpec().to_payload()
    assert "ic_20d" in payload["review_trigger_rule"]
    assert payload["review_action"] == REVIEW_ACTION
    assert payload["blocks"] == list(BLOCK_NAMES)


def test_report_is_deterministic_for_same_input() -> None:
    frame = _frame()
    first = build_health_report(frame=frame, report_date="2026-03-20").to_payload()
    second = build_health_report(frame=frame, report_date="2026-03-20").to_payload()
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )
