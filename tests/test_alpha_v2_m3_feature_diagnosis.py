"""M3 特征诊断测试（DF-M2-003 五分类）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.validation.feature_diagnosis import (
    CLASS_DATA_MISSINGNESS,
    CLASS_FILL_ZERO_ARTIFACT,
    CLASS_REAL_CONSTANT,
    CLASS_UNKNOWN,
    CLASS_UPSTREAM_NOT_POPULATED,
    diagnose_features,
)


def _frame() -> pd.DataFrame:
    n = 1000
    return pd.DataFrame(
        {
            "decision_date": ["2026-09-01"] * n,
            # 近全空：数据真的缺席
            "holder_count_chg_5": [np.nan] * 990 + [0.1] * 10,
            # 常数：真实产生的常量（有值且非 0、非缺失）——价格量组、非 0 众数
            "float_market_cap": [5.0e9] * n,
            # 0 值伪影：众数是 0、占比极高且组策略是 fill_zero_after_shift
            "bg_block_trade_net10": [0.0] * 998 + [1.0, 2.0],
            # 正常特征
            "ret_5d": np.linspace(-0.1, 0.1, n),
        }
    )


def test_classification_buckets():
    frame = _frame()
    # 让探针说：shareholder_count 上游源列不存在（holder_count 不在 daily_bars）
    probe = {"shareholder_count": "source_columns_absent:holder_count"}
    report = diagnose_features(frame, upstream_probe=probe)
    by_col = {row.column: row for row in report.rows}

    # 证据驱动：源列不在 => 即便统计上也几乎全空，也要归因于上游
    assert by_col["holder_count_chg_5"].classification == CLASS_UPSTREAM_NOT_POPULATED
    assert by_col["holder_count_chg_5"].upstream_evidence.startswith("source_columns_absent")

    assert by_col["float_market_cap"].classification == CLASS_REAL_CONSTANT
    assert by_col["bg_block_trade_net10"].classification == CLASS_FILL_ZERO_ARTIFACT
    assert by_col["ret_5d"].classification == CLASS_UNKNOWN  # 正常特征不归任何受害类

    counts = report.classification_counts()
    assert counts[CLASS_UPSTREAM_NOT_POPULATED] == 1
    assert counts[CLASS_REAL_CONSTANT] == 1
    assert counts[CLASS_FILL_ZERO_ARTIFACT] == 1


def test_stats_fields_present():
    report = diagnose_features(_frame())
    for row in report.rows:
        payload = row.to_payload()
        for key in (
            "coverage",
            "missing_ratio",
            "unique_count",
            "std",
            "zero_ratio",
            "mode_value",
            "mode_ratio",
            "asof_safe",
            "missing_policy",
            "upstream_source",
        ):
            assert key in payload, f"{row.column} 缺 {key}"
    # 有具体数值：float_market_cap 的 std 必须是 0
    fmc = next(row for row in report.rows if row.column == "float_market_cap")
    assert fmc.std == 0.0
    assert fmc.unique_count == 1


def test_diagnose_subset_of_columns():
    frame = _frame()
    report = diagnose_features(frame, columns=["ret_5d"])
    assert len(report.rows) == 1
    assert report.rows[0].column == "ret_5d"


def test_group_driven_evidence_when_no_probe():
    # 不探库时(默认)：所有近全空列按统计形态归类（不伪称探过上游）
    frame = _frame()
    report = diagnose_features(frame)  # 无 upstream_probe
    row = next(r for r in report.rows if r.column == "holder_count_chg_5")
    # 没有上游证据：近全空（NaN）就属于 DATA_MISSINGNESS
    assert row.classification == CLASS_DATA_MISSINGNESS
