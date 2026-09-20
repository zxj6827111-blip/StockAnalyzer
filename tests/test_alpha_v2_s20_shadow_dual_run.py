"""S20 Legacy vs V2 Shadow 双轨阶段验收测试。

守住：V2 不接管 Legacy、回滚只需一个开关、台账真的被接上（DF-S10-001/002）。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
from _alpha_v2_research_helpers import DAYS, bar, matcher, panel, walk  # noqa: E402

from stock_analyzer.alpha_v2.decision_log import read_decision_rows
from stock_analyzer.alpha_v2.research.decision_policy import (
    FinalPolicySpec,
    build_shadow_selection,
)
from stock_analyzer.alpha_v2.research.shadow_dual_run import (
    DUAL_RUN_SCHEMA,
    UNTOUCHED_DECLARATIONS,
    assert_legacy_untouched,
    build_dual_run,
    build_shadow_decision_rows,
    dual_run_dataframe,
    extract_legacy_final,
    mature_shadow_outcomes,
    persist_shadow_decision_log,
    shadow_flags,
    write_dual_run_report,
)
from stock_analyzer.config import AlphaV2Config, StockAnalyzerConfig, load_config

DECISION_DAY = DAYS[30]


def _legacy_report(*, selected: int = 1, rejected: int = 3) -> dict[str, object]:
    return {
        "funnel": {
            "final_selection": {
                "applied": True,
                "mode": "final_selection",
                "min_threshold": 70.0,
                "final_signal_cap": 5,
                "allow_zero_signal": True,
                "selected_count": selected,
                "rejected_count": rejected,
                "final_signals": [
                    {"symbol": f"60000{index}", "score": 78.5, "action": "buy"}
                    for index in range(selected)
                ],
                "rejected": [
                    {
                        "symbol": f"6001{index:02d}",
                        "score": 55.0,
                        "reject_reasons": ["below_min_threshold"],
                    }
                    for index in range(rejected)
                ]
                + [{"symbol": "600200", "score": 61.0, "reject_reasons": ["cross_review_failed"]}],
            }
        }
    }


def _policy_frame(*, symbols: list[str] | None = None) -> pd.DataFrame:
    resolved = symbols or [f"60000{index}" for index in range(6)]
    rows = []
    for index, symbol in enumerate(resolved):
        rows.append(
            {
                "decision_date": DECISION_DAY.isoformat(),
                "symbol": symbol,
                "deep_pool": True,
                "executable": True,
                "alpha_rank_score": 1.0 - index * 0.1,
                "expected_excess_return_5d": 0.02 - index * 0.002,
                "p_up_net_5d": 0.6 - index * 0.01,
                "expected_mae_5d": -0.02,
                "legacy_score": 78.5 - index,
            }
        )
    return pd.DataFrame(rows)


def _default_config() -> StockAnalyzerConfig:
    # 用受跟踪默认配置（与 S00 golden 契约同源），避免手搓残缺配置
    return load_config("config/default.yaml")


# ---------------------------------------------------------------------------
# Legacy 只读提取
# ---------------------------------------------------------------------------


def test_extract_legacy_final_reads_nested_funnel_selection() -> None:
    extracted = extract_legacy_final(_legacy_report())
    assert extracted["status"] == "ok"
    assert extracted["selected_count"] == 1
    assert extracted["min_threshold"] == 70.0
    assert extracted["allow_zero_signal"] is True
    assert extracted["selected"][0]["symbol"] == "600000"
    reasons = {reason for row in extracted["rejected"] for reason in row["reject_reasons"]}
    assert "below_min_threshold" in reasons


def test_extract_legacy_final_handles_missing_selection() -> None:
    extracted = extract_legacy_final({})
    assert extracted["status"] == "final_selection_not_found"
    assert extracted["selected"] == []


def test_extract_legacy_final_does_not_mutate_report() -> None:
    report = _legacy_report()
    before = json.dumps(report, sort_keys=True, default=str)
    extract_legacy_final(report)
    assert json.dumps(report, sort_keys=True, default=str) == before


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


def test_shadow_flags_default_to_not_enforced() -> None:
    payload = shadow_flags(_default_config())
    assert payload["enforce_final_selection"] is False
    assert payload["rollback_switch"] == "alpha_v2.enforce_final_selection"
    assert "violation" not in payload


def test_shadow_flags_report_violation_when_enforced() -> None:
    config = _default_config().model_copy(
        update={
            "alpha_v2": AlphaV2Config(enabled=True, shadow_only=False, enforce_final_selection=True)
        }
    )
    payload = shadow_flags(config)
    assert payload["enforce_final_selection"] is True
    assert "violation" in payload


# ---------------------------------------------------------------------------
# 双轨对照
# ---------------------------------------------------------------------------


def _dual_run(*, v2_symbols: list[str] | None = None) -> tuple[dict[str, object], pd.DataFrame]:
    frame = _policy_frame(symbols=v2_symbols)
    policy = build_shadow_selection(frame, spec=FinalPolicySpec(require_stage_column=True))
    result = build_dual_run(
        legacy_report=_legacy_report(),
        policy=policy,
        config=_default_config(),
        decision_date=DECISION_DAY.isoformat(),
        data_health={"level": "healthy"},
        market_regime={"level": "normal"},
    )
    return result.to_payload(), frame


def test_dual_run_payload_shape() -> None:
    payload, _ = _dual_run()
    assert payload["schema"] == DUAL_RUN_SCHEMA
    assert payload["decision_date"] == DECISION_DAY.isoformat()
    comparison = payload["comparison"]
    assert comparison["legacy_final"] == ["600000"]
    assert len(comparison["v2_top5"]) == 5
    assert "overlap_with_legacy" in comparison
    assert payload["data_health"]["level"] == "healthy"
    assert payload["market_regime"]["level"] == "normal"


def test_dual_run_declares_legacy_untouched() -> None:
    payload, _ = _dual_run()
    for key, expected in UNTOUCHED_DECLARATIONS.items():
        assert payload[key] == expected
    assert_legacy_untouched(payload)


def test_assert_legacy_untouched_rejects_false_declaration() -> None:
    payload, _ = _dual_run()
    tampered = dict(payload)
    tampered["legacy_modified"] = True
    with pytest.raises(AssertionError, match="未触碰声明"):
        assert_legacy_untouched(tampered)


def test_assert_legacy_untouched_rejects_enforced_flag() -> None:
    payload, _ = _dual_run()
    tampered = dict(payload)
    tampered["flags"] = {"enforce_final_selection": True}
    with pytest.raises(AssertionError, match="不允许为 true"):
        assert_legacy_untouched(tampered)


def test_dual_run_reports_v2_only_and_legacy_only() -> None:
    payload, _ = _dual_run(v2_symbols=["600100", "600101", "600102", "600103", "600104"])
    comparison = payload["comparison"]
    assert comparison["overlap_with_legacy"] == []
    assert "600100" in comparison["v2_only"]
    assert comparison["legacy_only"] == ["600000"]


def test_dual_run_surfaces_legacy_reject_reason_summary() -> None:
    payload, _ = _dual_run()
    summary = payload["comparison"]["legacy_reject_reasons_summary"]
    assert summary["below_min_threshold"] == 3
    assert summary["cross_review_failed"] == 1


def test_dual_run_marks_previously_rejected_names() -> None:
    payload, _ = _dual_run(v2_symbols=["600100", "600101", "600200", "600104", "600105"])
    assert "600200" in payload["comparison"]["v2_top5_previously_rejected_by_legacy"]


def test_write_dual_run_report_is_atomic(tmp_path) -> None:
    payload, _ = _dual_run()
    target = write_dual_run_report(root=tmp_path, payload=payload)
    assert target.name.startswith("dual_run_")
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["schema"] == DUAL_RUN_SCHEMA
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []


def test_dual_run_dataframe_pairs_stages() -> None:
    payload, _ = _dual_run()
    frame = dual_run_dataframe(payload)
    assert set(frame["stage"]) == {"legacy_final", "v2_top1", "v2_top3", "v2_top5"}
    assert len(frame[frame["stage"] == "v2_top5"]) == 5


# ---------------------------------------------------------------------------
# 台账接线（DF-S10-001 / DF-S10-002）
# ---------------------------------------------------------------------------


def test_shadow_decision_rows_fill_v2_fields_from_heads() -> None:
    rows = build_shadow_decision_rows(
        signal_date=DECISION_DAY,
        candidates=[
            {
                "symbol": "600000",
                "rank": 1,
                "alpha_rank_score": 0.97,
                "expected_excess_return_5d": 0.021,
                "p_up_net_5d": 0.58,
                "expected_mae_5d": -0.018,
            }
        ],
    )
    payload = rows[0].to_payload()
    assert payload["v2_rank_score"] == 0.97
    assert payload["v2_expected_return"] == 0.021
    assert payload["v2_direction_score"] == 0.58
    assert payload["v2_risk_score"] == -0.018


def test_shadow_decision_rows_keep_not_available_when_head_missing() -> None:
    rows = build_shadow_decision_rows(
        signal_date=DECISION_DAY, candidates=[{"symbol": "600000", "rank": 1}]
    )
    payload = rows[0].to_payload()
    for field in ("v2_rank_score", "v2_expected_return", "v2_direction_score", "v2_risk_score"):
        assert payload[field] == "not_available"


def test_calibrated_direction_takes_precedence_in_decision_log() -> None:
    rows = build_shadow_decision_rows(
        signal_date=DECISION_DAY,
        candidates=[{"symbol": "600000", "p_up_net_5d": 0.58, "p_up_net_5d_calibrated": 0.61}],
    )
    assert rows[0].to_payload()["v2_direction_score"] == 0.61


def test_persist_shadow_decision_log_writes_jsonl(tmp_path) -> None:
    rows = build_shadow_decision_rows(
        signal_date=DECISION_DAY,
        candidates=[{"symbol": "600000", "alpha_rank_score": 0.9}],
    )
    written = persist_shadow_decision_log(
        root=tmp_path / "alpha_v2",
        signal_date=DECISION_DAY,
        rows=rows,
        manifest={"run": "shadow", "decision_date": DECISION_DAY.isoformat()},
    )
    assert written["decisions"].endswith("decision_20260216.jsonl")
    assert written["manifest"].endswith("run_20260216.json")
    loaded = read_decision_rows(written["decisions"])
    assert loaded[0]["symbol"] == "600000"
    assert loaded[0]["v2_rank_score"] == 0.9


def test_mature_shadow_outcomes_writes_nothing_on_signal_day(tmp_path) -> None:
    built = panel(walk("600000", [10.0 + index * 0.05 for index in range(40)]))
    payload = mature_shadow_outcomes(
        root=tmp_path,
        signal_date=DECISION_DAY,
        evaluation_date=DECISION_DAY,  # 信号当天
        decision_rows=[{"symbol": "600000"}],
        bars_by_symbol={"600000": built.symbol_bars("600000")},
    )
    assert payload["matured_horizons"] == []
    assert payload["written"] is None
    assert payload["reason"] == "no_matured_horizons_on_evaluation_date"


def test_mature_shadow_outcomes_writes_after_maturity(tmp_path) -> None:
    built = panel(walk("600000", [10.0 + index * 0.05 for index in range(40)]))
    payload = mature_shadow_outcomes(
        root=tmp_path,
        signal_date=DECISION_DAY,
        evaluation_date=DAYS[33],
        decision_rows=[{"symbol": "600000"}],
        bars_by_symbol={"600000": built.symbol_bars("600000")},
        # 成熟日按**交易日**推进；不给日历会退回自然日近似（口径必须显式）
        trading_days=DAYS,
    )
    assert 3 in payload["matured_horizons"]
    assert payload["written"] is not None
    assert (tmp_path / "2026" / "02").is_dir()


def test_mature_shadow_outcomes_uses_run_config_matcher(tmp_path) -> None:
    """DF-S10-002：outcome 成本口径必须能接运行配置而不是裸默认。"""
    built = panel(walk("600000", [10.0 + index * 0.05 for index in range(40)]))
    payload = mature_shadow_outcomes(
        root=tmp_path,
        signal_date=DECISION_DAY,
        evaluation_date=DECISION_DAY,
        decision_rows=[{"symbol": "600000"}],
        bars_by_symbol={"600000": built.symbol_bars("600000")},
        trading_days=DAYS,
        config=_default_config(),
    )
    assert payload["rows"] == 0
    assert payload["written"] is None


def test_dual_run_with_no_legacy_selection_is_not_fabricated() -> None:
    policy = build_shadow_selection(_policy_frame())
    payload = build_dual_run(legacy_report={}, policy=policy).to_payload()
    assert payload["legacy"]["status"] == "final_selection_not_found"
    assert payload["comparison"]["legacy_final"] == []
    assert payload["comparison"]["overlap_with_legacy"] == []


def test_shadow_module_does_not_import_notification_or_broker() -> None:
    import pathlib

    source = pathlib.Path("src/stock_analyzer/alpha_v2/research/shadow_dual_run.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("notification_service", "feishu", "sim_broker", "place_order"):
        assert forbidden not in source, forbidden
    assert date is not None and matcher is not None and bar is not None
