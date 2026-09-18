"""S14 特征可用性 / 穿越审计：Base V2 准入闸门的阶段验收测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _alpha_v2_research_helpers import DAYS, panel, walk  # noqa: E402

from stock_analyzer.alpha_v2.research.feature_audit import (
    ASOF_SAFE_PROVEN,
    DAILY_ONLY_SAFE_GROUPS,
    FEATURE_GROUPS,
    GROUP_BACKGROUND_META,
    GROUP_FINANCIAL,
    GROUP_INTRAADAY,
    GROUP_MARGIN,
    GROUP_MONEYFLOW,
    GROUP_PRICE_VOLUME,
    GROUP_UNREGISTERED,
    assert_safe_feature_columns,
    audit_feature_columns,
    audit_summary,
    classify_feature_columns,
    feature_group_missing_flags,
    group_columns,
    mechanical_checks,
    safe_feature_columns,
)

# ---------------------------------------------------------------------------
# 登记表元数据完整性
# ---------------------------------------------------------------------------


def test_every_group_declares_required_metadata() -> None:
    for spec in FEATURE_GROUPS:
        assert spec.source.strip(), spec.group_id
        assert spec.available_at_rule.strip(), spec.group_id
        assert spec.asof_evidence.strip(), spec.group_id
        assert spec.asof_safe in {"proven", "refuted", "unverified"}, spec.group_id
        assert spec.missing_policy.strip(), spec.group_id
        assert spec.price_series_mode.strip(), spec.group_id


def test_base_v2_groups_are_exactly_the_proven_ones() -> None:
    for spec in FEATURE_GROUPS:
        if spec.in_base_v2:
            assert spec.asof_safe == ASOF_SAFE_PROVEN, (
                f"{spec.group_id} 声明进 Base V2 但 asof_safe={spec.asof_safe}"
            )
    proven = {spec.group_id for spec in FEATURE_GROUPS if spec.asof_safe == ASOF_SAFE_PROVEN}
    assert set(DAILY_ONLY_SAFE_GROUPS) == proven


def test_high_risk_groups_are_excluded_by_default() -> None:
    excluded = {spec.group_id for spec in FEATURE_GROUPS if not spec.in_base_v2}
    for group in (
        GROUP_FINANCIAL,
        GROUP_INTRAADAY,
        GROUP_MARGIN,
        GROUP_MONEYFLOW,
        GROUP_BACKGROUND_META,
    ):
        assert group in excluded


# ---------------------------------------------------------------------------
# 列级分类与准入
# ---------------------------------------------------------------------------


def test_classify_routes_known_columns() -> None:
    assignment, unregistered = classify_feature_columns(
        ["ma20", "rsi14", "i1m_session_return", "bg_roe", "rolling_beta_60", "mystery_feature"]
    )
    assert assignment["ma20"] == GROUP_PRICE_VOLUME
    assert assignment["i1m_session_return"] == GROUP_INTRAADAY
    assert assignment["bg_roe"] == GROUP_FINANCIAL
    assert assignment["mystery_feature"] == GROUP_UNREGISTERED
    assert unregistered == ["mystery_feature"]


def test_safe_feature_columns_only_returns_proven_groups() -> None:
    columns = ["ma20", "rsi14", "i1m_session_return", "bg_roe", "financing_balance_chg_5"]
    safe = safe_feature_columns(columns)
    assert set(safe) == {"ma20", "rsi14"}


def test_assert_safe_rejects_unregistered_column() -> None:
    with pytest.raises(ValueError, match="fail-closed"):
        assert_safe_feature_columns(["ma20", "brand_new_signal_42"])


def test_assert_safe_rejects_outcome_column_as_feature() -> None:
    """收益列/未来列绝不允许被当成特征喂进 Base V2。"""
    for column in ("excess_return_5d", "net_return_3d", "fwd_return", "label"):
        with pytest.raises(ValueError, match="fail-closed"):
            assert_safe_feature_columns(["ma20", column])


def test_assert_safe_rejects_unverified_high_risk_groups() -> None:
    for column in ("bg_roe", "i1m_close_position", "northbound_net_5", "holder_count_chg_20"):
        with pytest.raises(ValueError, match="fail-closed"):
            assert_safe_feature_columns([column])


def test_group_columns_matches_only_actual_columns() -> None:
    columns = ["ma5", "ma20", "bg_roe", "rsi14"]
    assert set(group_columns(columns, group_id=GROUP_PRICE_VOLUME)) == {"ma5", "ma20", "rsi14"}
    assert set(group_columns(columns, group_id=GROUP_FINANCIAL)) == {"bg_roe"}
    assert group_columns(columns, group_id=GROUP_MONEYFLOW) == ()


# ---------------------------------------------------------------------------
# 缺失 vs 0
# ---------------------------------------------------------------------------


def test_missing_group_flags_distinguish_absent_group_from_zero() -> None:
    frame = pd.DataFrame(
        {
            "ma20": [1.0, np.nan, 3.0],
            "bg_roe": [np.nan, np.nan, np.nan],
            "moneyflow_net_5": [0.0, 0.0, 0.0],
        }
    )
    flags = feature_group_missing_flags(frame)
    # 背景全缺 → missing
    assert bool(flags[f"missing_{GROUP_FINANCIAL}"].all())
    # 资金流全为 0 但有值 → 不算 missing（缺失与真实 0 必须区分）
    assert not bool(flags[f"missing_{GROUP_MONEYFLOW}"].any())
    # 价格特征第 2 行缺失、第 1/3 行存在
    assert list(flags[f"missing_{GROUP_PRICE_VOLUME}"]) == [False, True, False]


def test_missing_flag_true_when_group_column_absent_from_frame() -> None:
    flags = feature_group_missing_flags(pd.DataFrame({"ma20": [1.0]}))
    assert bool(flags[f"missing_{GROUP_INTRAADAY}"].all())


def test_constant_zero_fill_is_flagged_as_suspect() -> None:
    frame = pd.DataFrame(
        {
            "ma20": list(np.linspace(1.0, 2.0, 100)),
            "bg_roe": [0.0] * 100,  # fillna(0) 伪装成"真实为 0"
            "bg_debt_ratio": [np.nan] * 100,
        }
    )
    report = audit_feature_columns(frame.columns, frame=frame)
    assert "bg_roe" in report.constant_suspects
    assert "bg_debt_ratio" in report.constant_suspects
    assert "ma20" not in report.constant_suspects


# ---------------------------------------------------------------------------
# 机械检查
# ---------------------------------------------------------------------------


def _mechanical_panel(*, asof_offset_days: int) -> pd.DataFrame:
    rows = []
    for day in DAYS[:4]:
        rows.append(
            {
                "symbol": "600000",
                "trade_date": pd.Timestamp(day),
                "financial_as_of": pd.Timestamp(day) + pd.Timedelta(days=asof_offset_days),
                "financial_report_date": pd.Timestamp(day) - pd.Timedelta(days=30),
                "price_series_mode": "raw",
            }
        )
    return pd.DataFrame(rows)


def test_mechanical_checks_pass_for_clean_asof() -> None:
    result = mechanical_checks(_mechanical_panel(asof_offset_days=-5))
    assert result["financial_asof_le_trade_date"]["status"] == "ok"
    assert result["financial_asof_le_trade_date"]["violations"] == 0
    assert result["financial_report_date_le_trade_date"]["status"] == "ok"


def test_mechanical_checks_detect_future_asof_violation() -> None:
    result = mechanical_checks(_mechanical_panel(asof_offset_days=+5))
    assert result["financial_asof_le_trade_date"]["status"] == "violation"
    assert result["financial_asof_le_trade_date"]["violations"] == 4


def test_mechanical_checks_report_absent_columns() -> None:
    result = mechanical_checks(pd.DataFrame({"trade_date": [pd.Timestamp(DAYS[0])]}))
    assert result["financial_asof_le_trade_date"] == "column_absent"
    assert result["price_series_mode_declared"] == "column_absent"


def test_mechanical_checks_on_empty_panel() -> None:
    assert mechanical_checks(pd.DataFrame())["status"] == "no_panel"


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def test_audit_summary_marks_unregistered_as_fail_closed() -> None:
    clean = audit_feature_columns(["ma20", "rsi14"])
    assert audit_summary(clean)["verdict"] == "base_v2_uses_proven_groups_only"
    dirty = audit_feature_columns(["ma20", "brand_new_signal_42"])
    assert audit_summary(dirty)["verdict"] == "unregistered_columns_present_fail_closed"
    payload = dirty.to_payload()
    assert payload["policy"] == "unproven_feature_must_not_enter_base_v2"
    assert "brand_new_signal_42" in payload["unregistered_columns"]
    assert payload["excluded_columns_count"] >= 1


def test_no_group_declares_proven_without_evidence_of_checked_rule() -> None:
    """proven 组的依据必须提到可检查的对象（列/规则/实现），不能是空话。"""
    keywords = ("列", "rolling", "shift", "收盘", "行", "派生", "index", "date")
    for spec in FEATURE_GROUPS:
        if spec.asof_safe != ASOF_SAFE_PROVEN:
            continue
        assert any(keyword in spec.asof_evidence for keyword in keywords), spec.group_id


# ---------------------------------------------------------------------------
# 与真实 FeatureEngineer 对齐（新增列必须登记）
# ---------------------------------------------------------------------------


def test_real_feature_engineer_columns_are_fully_registered() -> None:
    from stock_analyzer.feature.engineer import FeatureEngineer

    bars = walk("600000", [10.0 + index * 0.1 for index in range(17)])
    frame = pd.DataFrame(bars).set_index("trade_date")
    features = FeatureEngineer().transform(frame)
    assignment, unregistered = classify_feature_columns(list(features.columns))
    assert unregistered == [], (
        "FeatureEngineer 产出了未登记特征列：必须先在 FEATURE_GROUPS 登记并给出 "
        f"PIT 证据，否则它们不能进 Base V2。未登记={unregistered}"
    )
    safe = safe_feature_columns(list(features.columns))
    assert len(safe) > 50
    assert all(assignment[column] in DAILY_ONLY_SAFE_GROUPS for column in safe)


def test_audit_coverage_reports_per_group_non_null_ratio() -> None:
    panel_frame = pd.DataFrame(
        {
            "ma20": [1.0, 2.0, np.nan, 4.0],
            "bg_roe": [np.nan, np.nan, np.nan, 0.1],
        }
    )
    report = audit_feature_columns(panel_frame.columns, frame=panel_frame)
    assert report.coverage[GROUP_PRICE_VOLUME]["mean_non_null_ratio"] == pytest.approx(0.75)
    assert report.coverage[GROUP_FINANCIAL]["mean_non_null_ratio"] == pytest.approx(0.25)
    assert report.missing_ratio["bg_roe"] == pytest.approx(0.75)


def test_panel_helper_missing_group_flag_is_not_filled_with_zero() -> None:
    built = panel(walk("600000", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]))
    frame = pd.DataFrame({"ma20": [1.0] * len(built.bars)})
    flags = feature_group_missing_flags(frame)
    assert f"missing_{GROUP_FINANCIAL}" in flags.columns
    assert bool(flags[f"missing_{GROUP_FINANCIAL}"].all())
