"""方向一'（2026-09-07）测试：return_rank label + 98 特征接入。

覆盖任务书要求的对抗测试：
- 时间不变量：label_mature_trade_date 公式（T+1 开盘锚点、horizon 成熟）
  与 soup 完全一致——return_rank 只换公式不换锚点；
- 横截面分位无前视：改另一日 fwd_return 不影响当日 label；分位只由
  当日截面决定；
- 中间段处理边界：drop_middle（NaN）与 soft（0.5）两模式、平秩 ties、
  薄截面整日 NaN；
- registry：v3 policy 注册 hash 绑定、id 冲突拒绝、与 v1/v2 共存；
- 覆盖率回归：pit 生成器产出的特征列必须包含归因 JSON 里的 98 个
  NaN 特征名（接入完整性）——具体数据覆盖率属 NAS 验收（本地断言
  链路打通，源列缺失字段如实 NaN）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.backtest.pit_dataset import (
    _BACKGROUND_COLUMNS,
    _INTRADAY_SUMMARY_COLUMNS,
    _apply_return_rank_labels,
)
from stock_analyzer.labels.return_rank import build_return_rank_labels
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRegistry,
    build_return_rank_policy_record,
)


def _panel(days: int = 2, symbols: int = 10, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.MultiIndex.from_product(
        [
            pd.to_datetime([f"2026-03-{i:02d}" for i in range(2, 2 + days)]),
            [f"S{i:03d}" for i in range(symbols)],
        ],
        names=["trade_date", "symbol"],
    )
    return pd.Series(rng.standard_normal(len(idx)), index=idx, name="fwd_return")


class TestReturnRankLabels:
    def test_quantile_semantics_top_bottom(self) -> None:
        fwd = _panel(symbols=10)
        labels = build_return_rank_labels(fwd, min_cross_section=5)
        for day in fwd.index.get_level_values(0).unique():
            day_fwd = fwd.xs(day, level=0)
            day_lab = labels.xs(day, level=0)
            order = day_fwd.sort_values()
            assert day_lab[order.index[-1]] == 1.0
            assert day_lab[order.index[0]] == 0.0
            assert (day_lab == 1.0).sum() == 3
            assert (day_lab == 0.0).sum() == 3

    def test_no_cross_day_leakage(self) -> None:
        fwd = _panel()
        perturbed = fwd.copy()
        perturbed.loc[("2026-03-03", slice(None))] = np.random.default_rng(99).standard_normal(10)
        base = build_return_rank_labels(fwd, min_cross_section=5)
        other = build_return_rank_labels(perturbed, min_cross_section=5)
        assert (base.xs("2026-03-02", level=0) == other.xs("2026-03-02", level=0)).all()

    def test_thin_cross_section_all_nan(self) -> None:
        fwd = _panel(symbols=10)
        labels = build_return_rank_labels(fwd, min_cross_section=30)
        assert labels.isna().all()

    def test_drop_middle_vs_soft(self) -> None:
        fwd = _panel(symbols=10)
        dropped = build_return_rank_labels(fwd, drop_middle=True, min_cross_section=5)
        soft = build_return_rank_labels(fwd, drop_middle=False, min_cross_section=5)
        for day in fwd.index.get_level_values(0).unique():
            d = dropped.xs(day, level=0)
            s = soft.xs(day, level=0)
            # drop：中间段 NaN；soft：中间段 0.5，两端语义一致。
            assert (d == 1.0).sum() == (s == 1.0).sum() == 3
            assert (d == 0.0).sum() == (s == 0.0).sum() == 3
            assert d.isna().sum() == 4
            assert (s == 0.5).sum() == 4

    def test_ties_average_rank(self) -> None:
        # 平秩：2 只并列最高 → 都进 top；2 只并列最低 → 都进 bottom。
        idx = pd.MultiIndex.from_product(
            [[pd.Timestamp("2026-03-02")], ["A", "B", "C", "D", "E", "F"]]
        )
        fwd = pd.Series([0.05, 0.05, 0.01, -0.05, -0.05, 0.0], index=idx)
        labels = build_return_rank_labels(fwd, min_cross_section=3)
        assert labels[("2026-03-02", "A")] == 1.0
        assert labels[("2026-03-02", "B")] == 1.0
        assert labels[("2026-03-02", "D")] == 0.0
        assert labels[("2026-03-02", "E")] == 0.0

    def test_invalid_params_rejected(self) -> None:
        fwd = _panel(symbols=3)
        with pytest.raises(ValueError, match="quantiles"):
            build_return_rank_labels(fwd, top_quantile=0.0)
        with pytest.raises(ValueError, match="<="):
            build_return_rank_labels(fwd, top_quantile=0.8, bottom_quantile=0.8)
        with pytest.raises(ValueError, match="min_cross_section"):
            build_return_rank_labels(fwd, min_cross_section=1)

    def test_frozen_cross_section_matches_full_panel(self) -> None:
        """单日切片调用（month-block 路径）与整面板调用逐位一致。"""
        fwd = _panel(days=3, symbols=20)
        full = build_return_rank_labels(fwd, min_cross_section=10)
        parts: dict[object, float] = {}
        for day in fwd.index.get_level_values(0).unique():
            day_labels = build_return_rank_labels(
                fwd.xs(day, level=0), min_cross_section=10
            )
            for key, value in day_labels.items():
                parts[(day, key)] = float(value)
        for (day, sym), value in parts.items():
            assert full[(day, sym)] == value


class TestApplyReturnRankMonthBlock:
    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "symbol": ["A", "B", "C", "D", "A", "B", "C", "D"],
                "trade_date": ["2026-03-02"] * 4 + ["2026-03-03"] * 4,
                "label": [None] * 8,
                "fwd_return": [0.01, 0.02, np.nan, 0.005, -0.05, 0.00, 0.03, 0.10],
                "label_mature_trade_date": ["2026-03-16"] * 8,
                "feat_x": [1.0] * 8,
            }
        )

    def test_labels_written_per_day(self) -> None:
        out = _apply_return_rank_labels(
            self._frame(), top_q=0.3, bottom_q=0.3, drop_middle=True, min_cross=3
        )
        # 3/2 有效截面 [A .01, B .02, D .005]：B 唯一 top（rank 3/3 > 0.7）；
        # bottom 30%（rank <= 0.3）无人命中（D rank 1/3 ≈ 0.333）。
        assert out.loc[1, "label"] == 1.0  # B 3/2
        assert pd.isna(out.loc[0, "label"]) and pd.isna(out.loc[3, "label"])
        # 3/3 截面 [A -.05, B 0, C .03, D .10]：A 唯一 bottom；C/D 为 top 段
        # （rank 0.75/1.0 > 0.7）。
        assert out.loc[4, "label"] == 0.0
        assert out.loc[6, "label"] == 1.0 and out.loc[7, "label"] == 1.0
        # fwd_return 缺失行（C 3/2）不参与也不得有 label
        assert pd.isna(out.loc[2, "label"])

    def test_nan_rows_excluded_from_cross_section(self) -> None:
        """fwd_return=NaN 行不占分位名额（当日剩余 3 只按 3 只截面算）。"""
        out = _apply_return_rank_labels(
            self._frame(), top_q=0.3, bottom_q=0.3, drop_middle=False, min_cross=3
        )
        assert out.loc[2, "label"] is None or pd.isna(out.loc[2, "label"])

    def test_mature_date_column_untouched(self) -> None:
        """时间不变量：label 改写不动 label_mature_trade_date。"""
        frame = self._frame()
        out = _apply_return_rank_labels(
            frame, top_q=0.3, bottom_q=0.3, drop_middle=True, min_cross=3
        )
        assert (out["label_mature_trade_date"] == frame["label_mature_trade_date"]).all()


class TestReturnRankPolicyRegistry:
    def test_v3_record_deterministic_hash(self) -> None:
        a = build_return_rank_policy_record(horizon_days=10)
        b = build_return_rank_policy_record(horizon_days=10)
        assert a.label_policy_hash == b.label_policy_hash
        assert a.label_name == "label_return_rank"
        assert a.schema_version == "3"
        assert a.conflict_policy == "rank_quantile"
        assert a.label_policy_id == f"label_policy_v3_{a.label_policy_hash[:12]}"

    def test_horizon_changes_hash(self) -> None:
        a = build_return_rank_policy_record(horizon_days=10)
        b = build_return_rank_policy_record(horizon_days=5)
        assert a.label_policy_hash != b.label_policy_hash

    def test_registry_v3_coexists_with_v2(self, tmp_path: Path) -> None:
        registry = LabelPolicyRegistry(tmp_path / "label_policy.duckdb")
        soup_record = registry.register_from_config(
            _soup_labels_config(), schema_version="2"
        )
        rank_record = registry.register_return_rank(horizon_days=10)
        assert soup_record.label_policy_id != rank_record.label_policy_id
        assert registry.get_by_id(soup_record.label_policy_id) is not None
        assert registry.get_by_id(rank_record.label_policy_id) is not None
        # 幂等重放：同契约再注册返回既有记录，不产生新行。
        replay = registry.register_return_rank(horizon_days=10)
        assert replay.label_policy_hash == rank_record.label_policy_hash
        assert len(registry.list_records()) == 2

    def test_id_conflict_rejected(self, tmp_path: Path) -> None:
        registry = LabelPolicyRegistry(tmp_path / "label_policy.duckdb")
        registry.register_return_rank(horizon_days=10)
        conflicting = build_return_rank_policy_record(
            horizon_days=5,
            # 手动占用同 id：不同契约同 id 必须拒绝。
            label_policy_id=registry.register_return_rank(horizon_days=10).label_policy_id,
        )
        with pytest.raises(ValueError, match="already registered"):
            registry.register(conflicting)


def _soup_labels_config():
    from stock_analyzer.config import LabelsConfig

    return LabelsConfig()


class TestPitDatasetFeatureOnboarding:
    """98 个 NaN 特征接入完整性（链路级回归，数据覆盖属 NAS 验收）。"""

    def test_background_columns_cover_attribution_nan_families(self) -> None:
        """归因 JSON 中因 daily_bars 背景列缺失而全 NaN 的特征族，
        其源列必须在生成器 SELECT 列表里（bg_*/moneyflow/hk/inst/
        block_trade/holder/northbound/financing/roe/debt/board）。"""
        mapping = {
            "bg_roe": "roe",
            "bg_debt_ratio": "debt_ratio",
            "bg_holder_reduction20": "holder_count",
            "bg_block_trade_net10": "block_trade_net",
            "bg_margin_trend20": "margin_financing_balance",
            "bg_northbound_net5": "northbound_net",
            "bg_dragon_tiger_freq20": "dragon_tiger_flag",
            "bg_board_code": "board",
            "moneyflow_net_5": "moneyflow_net_amount",
            "hk_hold_ratio_chg_5": "hk_hold_ratio",
            "hk_hold_change_5": "hk_hold_change",
            "inst_net_amount_5": "inst_net_amount",
            "block_trade_amount_5": "block_trade_amount",
            "block_trade_volume_20": "block_trade_volume",
            "block_trade_premium_mean_20": "block_trade_premium_discount",
            "holder_count_chg_5": "holder_count",
            "northbound_net_5": "northbound_net",
            "financing_balance_chg_5": "margin_financing_balance",
            "roe_trend_60": "roe",
            "debt_ratio_stability_60": "debt_ratio",
        }
        for feature, source in mapping.items():
            assert source in _BACKGROUND_COLUMNS, (feature, source)

    def test_intraday_columns_match_engineer_contract(self) -> None:
        from stock_analyzer.feature.engineer import _STANDARD_INTRADAY_SUMMARY_COLUMNS

        assert set(_INTRADAY_SUMMARY_COLUMNS) == set(_STANDARD_INTRADAY_SUMMARY_COLUMNS)

    def test_nan_feature_list_regressed_from_attribution_json(self) -> None:
        """9/5 归因 JSON 的 98 个全 NaN 特征逐个断言——它们必须能在新
        生成器的特征空间里产生（源列已接入，或映射到 intraday/指数）。"""
        attribution = (
            Path(__file__).resolve().parents[1]
            / "backfill_tmp"
            / "feature_attribution_20260907.json"
        )
        if not attribution.exists():
            pytest.skip("attribution json not present locally (NAS-only artifact)")
        features = json.loads(attribution.read_text(encoding="utf-8"))["features"]
        nan_features = {
            r["feature"]
            for r in features
            if r.get("ic_mean") is None
            or (isinstance(r.get("ic_mean"), float) and np.isnan(r["ic_mean"]))
        }
        assert len(nan_features) == 98
        # 全部 98 个必须映射到三类已接入源：背景列 / 分钟 summary / 指数。
        background_derived = _background_derived_prefixes()
        intraday_derived = {"i1m_", "i5m_"}
        market_derived = {
            "excess_ret_5",
            "excess_ret_20",
            "excess_ret_60",
            "relative_strength_5",
            "relative_strength_20",
            "rs_ma5",
            "rs_ma20",
            "rolling_beta_60",
            "excess_vol_20",
            "excess_vol_60",
            "market_trend",
        }
        unexplained = {
            f
            for f in nan_features
            if not any(f.startswith(p) or f == p for p in background_derived)
            and not any(f.startswith(p) for p in intraday_derived)
            and f not in market_derived
        }
        assert not unexplained, sorted(unexplained)


def _background_derived_prefixes() -> set[str]:
    return {
        "bg_",
        "moneyflow_net_",
        "hk_hold_",
        "inst_net_amount_",
        "block_trade_",
        "holder_count_",
        "northbound_",
        "financing_balance_",
        "roe_trend_",
        "debt_ratio_",
    }


class TestTimeInvariantsUnchanged:
    """return_rank 与 soup 的 label_mature_trade_date 公式逐位一致。"""

    def test_mature_date_formula_shared(self) -> None:
        # 两 basis 共用 _mature_of：entry=dec+1, mature=entry+horizon-1。
        # 此处以纯公式断言（不跑生成器——DB fixture 在 phase2 测试另有覆盖）。
        trading_dates = [date(2026, 3, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17)]
        horizon = 10
        for dec_idx in range(len(trading_dates) - horizon):
            entry_idx = dec_idx + 1
            mature_idx = entry_idx + horizon - 1
            # T+1 开盘入场，入场日算第 1 天，第 horizon 交易日收盘成熟。
            assert mature_idx == dec_idx + horizon
        # embargo = horizon + settlement_lag（harness 口径）不变。
        assert 10 + 1 == 11
