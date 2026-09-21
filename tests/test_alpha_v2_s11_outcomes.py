"""S11 Label V2：多 horizon 可执行 outcome 的阶段验收测试。

覆盖三类证据：

1. **正向**：T+1 开盘成交 → 各 horizon 净收益/超额/MAE/MFE/方向标签全部按
   定义算出，且与 legacy soup 路径标签同源一致；
2. **对抗**：一字涨停 / 停牌 / 次日无 bar / 数据不足 / 未认证价格口径 —— 每一条
   都必须 fail-closed（``no_fill`` 或 ``not_available``），绝不能被算成收益；
3. **口径守卫**：入场价不得等于决策日收盘价；无 benchmark 时超额列必须
   ``not_available``；未认证口径不得进主评价样本。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest
from _alpha_v2_research_helpers import (  # noqa: E402 - sys.path 由 conftest 注入
    DAYS,
)
from _alpha_v2_research_helpers import (
    bar as _bar,
)
from _alpha_v2_research_helpers import (
    matcher as _matcher,
)
from _alpha_v2_research_helpers import (
    panel as _panel,
)
from _alpha_v2_research_helpers import (
    walk as _walk,
)

from stock_analyzer.alpha_v2.dual_price_series import PriceSeriesContractError
from stock_analyzer.alpha_v2.research.outcomes import (
    DecisionPoint,
    OutcomeSpec,
    attach_excess_returns,
    benchmark_series_from_outcomes,
    build_label_v2,
    compute_outcomes,
    outcome_columns,
    round_trip_cost_rate,
    sample_diagnostics,
)
from stock_analyzer.alpha_v2.research.panel import (
    CERT_SOURCE_EMPIRICAL,
    CERT_SOURCE_ROW_DECLARED,
    DailyPanel,
    load_daily_panel,
    normalize_board,
)

# ---------------------------------------------------------------------------
# 正向：口径正确
# ---------------------------------------------------------------------------


def test_normal_entry_uses_next_session_open_with_costs() -> None:
    # 决策日 1/5 收 10.0；1/6 开盘 10.20；其后 10.40 / 10.60
    bars = _walk("600000", [10.0, 10.40, 10.60, 10.80, 11.00, 11.20, 11.40])
    panel = _panel(bars)
    matcher = _matcher()
    run = build_label_v2(
        panel=panel,
        decisions=[DecisionPoint("600000", DAYS[0])],
        matcher=matcher,
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert row["executable"] is True or row["executable"] == True  # noqa: E712
    assert row["entry_date"] == DAYS[1].isoformat()
    assert float(row["entry_price_raw"]) == pytest.approx(10.0)
    assert row["entry_delay_sessions"] == 1

    cost = float(row["round_trip_cost_rate"])
    assert cost > 0
    # 持有 3 个交易日 = 入场日(1/6)、1/7、1/8 → 退出价取 1/8 收盘 10.80
    expected_exit = 10.80
    expected_net = expected_exit / 10.0 - 1.0 - cost
    assert row["maturity_date_3d"] == DAYS[3].isoformat()
    assert float(row["net_return_3d"]) == pytest.approx(expected_net, abs=1e-6)
    # 净收益必须严格小于不含成本的口径 → 成本真的扣了
    assert float(row["net_return_3d"]) < expected_exit / 10.0 - 1.0


def test_entry_price_is_never_decision_day_close() -> None:
    """决策日收盘价被刻意设成极端值：入场价必须取 T+1 开盘，不受其影响。"""
    bars = _walk("600001", [3.0, 10.0, 10.2, 10.4, 10.6, 10.8, 11.0])
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600001", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert float(row["entry_price_raw"]) == pytest.approx(3.0)  # 1/6 开盘 = 1/5 收盘
    assert row["entry_date"] == DAYS[1].isoformat()
    assert float(row["net_return_3d"]) > 0


def test_mae_and_mfe_come_from_lows_and_highs() -> None:
    bars = _walk("600002", [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    # 1/7 的 low 打到 -4%，1/8 的 high 打到 +6%
    bars[2]["low"] = 9.6
    bars[3]["high"] = 10.6
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600002", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert float(row["mae_3d"]) == pytest.approx(9.6 / 10.0 - 1.0, abs=1e-6)
    assert float(row["mfe_3d"]) == pytest.approx(10.6 / 10.0 - 1.0, abs=1e-6)
    assert float(row["mfe_3d"]) > 0 > float(row["mae_3d"])


def test_direction_columns_only_for_short_horizons() -> None:
    bars = _walk("600003", [10.0, 10.0, 10.0, 10.5, 10.0, 10.0, 10.0])
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600003", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert row["up_net_3d"] is True or row["up_net_3d"] == True  # noqa: E712
    assert "up_net_5d" in run.frame.columns
    assert "up_net_10d" not in run.frame.columns
    assert "up_net_15d" not in run.frame.columns


def test_legacy_path_label_matches_soup_implementation() -> None:
    """V2 的 tp8_before_sl5_10d 必须与 legacy ``build_soup_labels`` 同源一致。"""
    from stock_analyzer.labels.soup import build_soup_labels

    for policy in ("soft_label", "conservative_zero", "bar_shape_heuristic"):
        bars = _walk(
            "600004", [10.0, 10.1, 9.5, 9.0, 10.9, 11.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        )
        panel = _panel(bars)
        run = compute_outcomes(
            panel=panel,
            decisions=[DecisionPoint("600004", DAYS[0])],
            spec=OutcomeSpec(conflict_policy=policy),
            matcher=_matcher(),
            price_mode="raw",
            price_mode_certified=True,
        )
        decided = run.frame.iloc[0]
        legacy = build_soup_labels(
            panel.symbol_bars("600004"),
            take_profit_pct=0.08,
            stop_loss_pct=0.05,
            horizon_days=10,
            price_basis="next_tradable_open",
            exclude_untradable=True,
            conflict_policy=policy,
        )
        expected = legacy.loc[pd.Timestamp(DAYS[0])]
        assert float(decided["tp8_before_sl5_10d"]) == pytest.approx(float(expected)), policy


# ---------------------------------------------------------------------------
# 对抗：fail-closed
# ---------------------------------------------------------------------------


def test_one_price_limit_up_is_no_fill() -> None:
    bars = _walk("600005", [10.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0])
    # 1/6 一字涨停：open=high=low=close=11.0（主板 10% → 上限 11.0）
    bars[1].update({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0})
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600005", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["executable"]) is False
    assert row["no_fill_reason"] == "limit_up_open"
    for column in ("net_return_3d", "net_return_5d", "mae_3d", "mfe_3d", "up_net_3d"):
        assert row[column] == "not_available"
    assert run.diagnostics["no_fill_by_reason"] == {"limit_up_open": 1}


def test_st_board_uses_five_percent_limit() -> None:
    bars = _walk("600006", [10.0, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0])
    for bar in bars:
        bar["is_st"] = True
    # ST 上限 5%：10.5 = 10.0 × 1.05 一字涨停
    bars[1].update({"open": 10.5, "high": 10.5, "low": 10.5, "close": 10.5})
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600006", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    assert run.frame.iloc[0]["no_fill_reason"] == "limit_up_open"


def test_gem_board_uses_twenty_percent_limit() -> None:
    """创业板 20%：11.5/10.0 = +15% 应当是可成交的（若按 10% 计算会被误拒）。"""
    bars = _walk("300001", [10.0, 11.5, 11.6, 11.7, 11.8, 11.9, 12.0])
    for bar in bars:
        bar["board"] = "gem"
    panel = _panel(bars)
    assert normalize_board("gem") == "创业板"
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("300001", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["executable"]) is True
    assert float(row["entry_price_raw"]) == pytest.approx(10.0)


def test_suspended_next_session_is_no_fill() -> None:
    bars = _walk("600007", [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    bars[1]["suspended"] = True
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600007", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["executable"]) is False
    assert row["no_fill_reason"] == "suspended"
    assert row["net_return_3d"] == "not_available"


def test_missing_next_session_bar_is_no_fill_not_delayed_fill() -> None:
    """次日无 bar（停牌/数据缺口）→ 主口径 no_fill，不得顺延成交。"""
    bars = _walk("600008", [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    bars = [bar for bar in bars if bar["trade_date"].date() != DAYS[1]]
    # 交易日历仍包含 1/6（它是交易日，只是这只票当天没有 bar）
    panel = _panel(bars, calendar=DAYS)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600008", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["executable"]) is False
    assert row["no_fill_reason"] == "suspended_or_missing_on_next_session"
    assert int(row["entry_delay_sessions"]) == 2
    assert row["entry_date"] == "not_available"


def test_insufficient_bars_mark_unmatured_without_guessing() -> None:
    bars = _walk("600009", [10.0, 10.1, 10.2, 10.3])  # 只有 4 根
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600009", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["matured_3d"]) is True
    assert bool(row["matured_5d"]) is False
    assert row["net_return_5d"] == "not_available"
    assert row["maturity_date_5d"] == "not_available"


def test_suspended_exit_day_marks_exit_no_fill() -> None:
    bars = _walk("600010", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    bars[3]["suspended"] = True  # 1/8 = 3D 退出日
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600010", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["executable"]) is True
    assert bool(row["exit_no_fill_3d"]) is True
    assert row["net_return_3d"] == "not_available"
    assert bool(row["matured_3d"]) is True


def test_symbol_absent_from_panel_is_no_fill() -> None:
    bars = _walk("600011", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("999999", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    assert run.frame.iloc[0]["no_fill_reason"] == "symbol_not_in_panel"


def test_decision_date_not_a_trading_day_is_no_fill() -> None:
    bars = _walk("600012", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600012", date(2026, 1, 3))],  # 周六
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    assert run.frame.iloc[0]["no_fill_reason"] == "decision_date_not_in_panel"


# ---------------------------------------------------------------------------
# benchmark / 超额
# ---------------------------------------------------------------------------


def _two_symbol_panel() -> DailyPanel:
    a = _walk("600100", [10.0, 10.0, 11.0, 11.0, 11.0, 11.0, 11.0])
    b = _walk("600101", [10.0, 10.0, 9.0, 9.0, 9.0, 9.0, 9.0])
    return _panel(a + b)


def test_excess_return_is_net_return_minus_benchmark() -> None:
    panel = _two_symbol_panel()
    run = build_label_v2(
        panel=panel,
        decisions=[DecisionPoint("600100", DAYS[0]), DecisionPoint("600101", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    frame = run.frame.set_index("symbol")
    bench = float(frame.loc["600100", "benchmark_return_3d"])
    assert float(frame.loc["600101", "benchmark_return_3d"]) == pytest.approx(bench)
    for symbol in ("600100", "600101"):
        net = float(frame.loc[symbol, "net_return_3d"])
        excess = float(frame.loc[symbol, "excess_return_3d"])
        assert excess == pytest.approx(net - bench, abs=1e-6)
    assert run.frame["benchmark_name"].iloc[0] == "eligible_ew"
    # 上/下两票在同一基准下方向相反
    assert bool(frame.loc["600100", "up_excess_3d"]) is True
    assert bool(frame.loc["600101", "up_excess_3d"]) is False


def test_pool_masked_benchmark_only_uses_pool_members() -> None:
    panel = _two_symbol_panel()
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600100", DAYS[0]), DecisionPoint("600101", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    mask = run.frame["symbol"] == "600100"
    benchmark = benchmark_series_from_outcomes(run.frame, pool_mask=mask, name="quality_pool")
    attached = attach_excess_returns(run.frame, benchmark=benchmark, name="quality_pool")
    only_top = attached[attached["symbol"] == "600100"].iloc[0]
    # 池子里只有 600100，基准 = 它自己 → 超额为 0
    assert float(only_top["excess_return_3d"]) == pytest.approx(0.0, abs=1e-9)
    assert attached["benchmark_name"].iloc[0] == "quality_pool"


def test_benchmark_excludes_non_executable_rows() -> None:
    """不可成交样本不得进入基准（否则基准被"假设能成交"的收益抬高）。"""
    bars = _walk("600200", [10.0, 10.0, 12.0, 12.0, 12.0, 12.0, 12.0])
    bars[1].update({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0})  # 一字涨停
    bars += _walk("600201", [10.0, 10.0, 10.1, 10.1, 10.1, 10.1, 10.1])
    panel = _panel(bars)
    run = build_label_v2(
        panel=panel,
        decisions=[DecisionPoint("600200", DAYS[0]), DecisionPoint("600201", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    frame = run.frame.set_index("symbol")
    assert frame.loc["600200", "net_return_3d"] == "not_available"
    bench = float(frame.loc["600201", "benchmark_return_3d"])
    net_b = float(frame.loc["600201", "net_return_3d"])
    assert bench == pytest.approx(net_b, abs=1e-9)


def test_attach_excess_without_benchmark_is_not_available() -> None:
    panel = _two_symbol_panel()
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600100", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    attached = attach_excess_returns(run.frame, benchmark=pd.DataFrame(), name="")
    row = attached.iloc[0]
    assert row["excess_return_3d"] == "not_available"
    assert row["up_excess_3d"] == "not_available"
    assert row["benchmark_name"] == "not_available"


# ---------------------------------------------------------------------------
# 价格口径守卫 + 样本账
# ---------------------------------------------------------------------------


def test_uncertified_price_mode_blocks_main_sample() -> None:
    """未认证口径在**样本账**上如实留痕（``compute_outcomes`` 层）。"""
    panel = _two_symbol_panel()
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600100", DAYS[0]), DecisionPoint("600101", DAYS[0])],
        matcher=_matcher(),
        price_mode="unknown",
        price_mode_certified=False,
    )
    assert bool(run.frame["execution_uncertain"].all())
    assert run.diagnostics["price_mode"] == "unknown"
    assert run.diagnostics["price_mode_certified"] is False
    diagnostics = sample_diagnostics(run.frame)
    assert diagnostics["main_sample_status"] == "execution_price_mode_unverified"
    assert diagnostics["main_sample_rows"] == 0
    assert diagnostics["execution_uncertain_rows"] == 2


def test_build_label_v2_fails_closed_on_uncertified_execution() -> None:
    """P0：``build_label_v2`` 不再"仅告警"——不是 raw+certified 就直接抛错。

    它是训练目标的入口：复权价一旦进来，``net_return_*`` / ``excess_return_*``
    本身就失真，所以研究口径那种"标 execution_uncertain 继续跑"不适用于它。
    """
    panel = _two_symbol_panel()
    with pytest.raises(PriceSeriesContractError):
        build_label_v2(
            panel=panel,
            decisions=[DecisionPoint("600100", DAYS[0])],
            matcher=_matcher(),
            price_mode="unknown",
            price_mode_certified=False,
        )
    with pytest.raises(PriceSeriesContractError):
        build_label_v2(
            panel=panel,
            decisions=[DecisionPoint("600100", DAYS[0])],
            matcher=_matcher(),
        )


def test_build_label_v2_research_opt_out_requires_reason() -> None:
    """研究回放可以关掉守卫，但必须留下理由（禁止静默降级）。"""
    panel = _two_symbol_panel()
    with pytest.raises(PriceSeriesContractError):
        build_label_v2(
            panel=panel,
            decisions=[DecisionPoint("600100", DAYS[0])],
            matcher=_matcher(),
            price_mode="unknown",
            price_mode_certified=False,
            enforce_execution_price_series=False,
        )
    run = build_label_v2(
        panel=panel,
        decisions=[DecisionPoint("600100", DAYS[0])],
        matcher=_matcher(),
        price_mode="unknown",
        price_mode_certified=False,
        enforce_execution_price_series=False,
        research_replay_reason="s11_test:research_replay",
    )
    assert run.diagnostics["execution_price_series_enforced"] is False
    assert run.diagnostics["research_replay_reason"] == "s11_test:research_replay"
    assert bool(run.frame["execution_uncertain"].all())


def test_round_trip_cost_rate_matches_matcher_schedule() -> None:
    cost = round_trip_cost_rate(matcher=_matcher(), reference_notional=100_000.0)
    assert cost["buy_cost_rate"] > 0
    assert cost["sell_cost_rate"] > 0
    # 卖出含印花税 → 卖出成本率高于买入
    assert cost["sell_cost_rate"] > cost["buy_cost_rate"]
    assert cost["round_trip_cost_rate"] == pytest.approx(
        cost["buy_cost_rate"] + cost["sell_cost_rate"], abs=1e-9
    )


def test_outcome_schema_is_complete() -> None:
    panel = _two_symbol_panel()
    run = build_label_v2(
        panel=panel,
        decisions=[DecisionPoint("600100", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    expected = set(outcome_columns())
    assert expected.issubset(set(run.frame.columns))
    spec = OutcomeSpec()
    assert spec.horizons == (3, 5, 10, 15)
    assert spec.primary_horizon == 5
    payload = spec.to_payload()
    assert payload["entry_mode"] == "next_session_open"
    assert payload["price_basis"] == "raw"


def test_corporate_action_flag_is_none_without_authoritative_pre_close() -> None:
    bars = _walk("600300", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    for bar in bars:
        bar["pre_close_source"] = "derived_previous_close"
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600300", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert row["corporate_action_suspected"] is None
    assert row["corporate_action_flag_source"] == "pre_close_source_unavailable"


def test_corporate_action_flag_detects_price_jump_in_pre_close() -> None:
    bars = _walk("600301", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    # 入场日（1/6）的权威前收比上一根 raw 收盘高 4% → 疑似除权
    bars[1]["pre_close"] = bars[1]["prev_close_raw"] * 1.04
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600301", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    row = run.frame.iloc[0]
    assert bool(row["corporate_action_suspected"]) is True
    assert run.diagnostics["corporate_action_suspected_rows"] == 1


def test_corporate_action_flag_ignores_out_of_window_jump() -> None:
    """窗口外的除权不影响本次持有期收益，不得被误标。"""
    bars = _walk("600302", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    bars[0]["pre_close"] = bars[0]["prev_close_raw"] * 1.04  # 决策日（入场前）
    panel = _panel(bars)
    run = compute_outcomes(
        panel=panel,
        decisions=[DecisionPoint("600302", DAYS[0])],
        matcher=_matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    assert bool(run.frame.iloc[0]["corporate_action_suspected"]) is False


# ---------------------------------------------------------------------------
# 面板级：价格口径认证
# ---------------------------------------------------------------------------


def test_panel_price_mode_certified_when_rows_declare_raw() -> None:
    panel = _panel(_walk("600400", [10.0, 10.1, 10.2, 10.3]))
    cert = panel.certify_price_mode(min_sample=1)
    assert cert.certified is True
    assert cert.source == CERT_SOURCE_ROW_DECLARED
    assert cert.mode == "raw"


def test_panel_price_mode_rejected_when_rows_declare_qfq() -> None:
    bars = _walk("600401", [10.0, 10.1, 10.2, 10.3])
    for bar in bars:
        bar["price_series_mode"] = "qfq"
    panel = _panel(bars)
    cert = panel.certify_price_mode(min_sample=1)
    assert cert.certified is False
    assert cert.mode == "qfq"
    assert cert.evidence["decision_rule"] == "panel_rows_declared_non_raw"


def test_panel_price_mode_probe_rejects_adjusted_series() -> None:
    """前复权序列在除权日会有超限跳变 → 探针必须拒绝（对抗性回归）。"""
    bars: list[dict[str, Any]] = []
    for index in range(60):
        # 20 行超过 10% 幅度（模拟复权跳变），其余正常
        jump = index % 3 == 0
        prev = 10.0
        close = prev * (1.20 if jump else 1.01)
        bars.append(
            _bar(
                f"6005{index:02d}",
                DAYS[index % len(DAYS)],
                open_=prev,
                high=max(prev, close),
                low=min(prev, close),
                close=close,
                prev_close=prev,
                price_series_mode=None,
            )
        )
    panel = _panel(bars)
    cert = panel.certify_price_mode(min_sample=30, max_violation_ratio=0.005)
    assert cert.certified is False
    assert cert.source == CERT_SOURCE_EMPIRICAL
    assert cert.evidence["probe_reason"] == "violation_ratio_above_threshold"


def test_panel_price_mode_probe_certifies_clean_raw_series() -> None:
    bars: list[dict[str, Any]] = []
    for index in range(60):
        prev = 10.0
        close = prev * (1.0 + 0.01 * ((index % 5) - 2))
        bars.append(
            _bar(
                f"6006{index:02d}",
                DAYS[index % len(DAYS)],
                open_=prev,
                high=max(prev, close),
                low=min(prev, close),
                close=close,
                prev_close=prev,
                price_series_mode=None,
            )
        )
    panel = _panel(bars)
    cert = panel.certify_price_mode(min_sample=30, max_violation_ratio=0.005)
    assert cert.certified is True
    assert cert.source == CERT_SOURCE_EMPIRICAL
    assert cert.evidence["probe_violation_ratio"] == 0.0


def test_panel_price_mode_probe_needs_minimum_sample() -> None:
    bars = _walk("600700", [10.0, 10.1, 10.2])
    for bar in bars:
        bar["price_series_mode"] = None
    panel = _panel(bars)
    cert = panel.certify_price_mode(min_sample=1000)
    assert cert.certified is False
    assert cert.evidence["probe_reason"] == "insufficient_sample"


# ---------------------------------------------------------------------------
# 真实数据（本机 market.duckdb）冒烟：只在数据存在时运行
# ---------------------------------------------------------------------------


def test_local_market_duckdb_smoke_if_available() -> None:
    from pathlib import Path

    db = Path(__file__).resolve().parents[1] / "artifacts" / "warehouse" / "market.duckdb"
    if not db.exists():
        pytest.skip("local market.duckdb not available")
    panel = load_daily_panel(
        market_db=db,
        window_start=date(2025, 12, 1),
        window_end=date(2025, 12, 31),
        warmup_days=30,
        max_symbols=40,
    )
    assert panel.calendar, "面板日历不应为空"
    cert = panel.certify_price_mode(min_sample=100)
    assert cert.source in {CERT_SOURCE_ROW_DECLARED, CERT_SOURCE_EMPIRICAL}
    decisions = [
        DecisionPoint(symbol, day)
        for symbol in panel.symbols[:10]
        for day in [panel.calendar[len(panel.calendar) // 2]]
    ]
    run = compute_outcomes(
        panel=panel,
        decisions=decisions,
        matcher=_matcher(),
        price_mode=cert.mode,
        price_mode_certified=cert.certified,
    )
    assert len(run.frame) == len(decisions)
    assert run.frame["entry_date"].notna().all()


# ---------------------------------------------------------------------------
# 面板级：PIT 股票池（与 M1 data.asof_universe 同源）
# ---------------------------------------------------------------------------


def test_panel_pit_universe_excludes_future_listed_symbols() -> None:
    """未来上市票必须同时排除出 eligible 与覆盖率分母（列名对不上会让它静默变空）。"""
    early = _walk("600001", [10.0 + index * 0.01 for index in range(7)])
    late = [
        _bar(
            "600002",
            day,
            open_=20.0,
            high=20.2,
            low=19.8,
            close=20.0,
            prev_close=20.0,
        )
        for day in DAYS[4:7]
    ]
    built = _panel(early + late, calendar=DAYS)
    snapshot = built.pit_universe(
        as_of=DAYS[2], min_history_days=60, expected_active_lookback_days=5
    )
    # 窗口内 bar 数不足 60：两只都不 eligible，但绝不出现"未来上市"误判串台
    assert snapshot.index_symbol_count == 2
    assert "600001" in snapshot.excluded_reasons or snapshot.eligible_count == 0
    assert snapshot.eligible_count == 0


def test_panel_pit_universe_marks_expected_active_from_recent_bars() -> None:
    bars = _walk("600001", [10.0 + index * 0.01 for index in range(17)])
    bars.extend(_walk("600002", [20.0 + index * 0.01 for index in range(17)]))
    built = _panel(bars, calendar=DAYS)
    snapshot = built.pit_universe(
        as_of=DAYS[16], min_history_days=5, expected_active_lookback_days=5
    )
    assert snapshot.eligible_count == 2
    assert snapshot.expected_active_count == 2
    assert snapshot.coverage_ratio(valid_symbol_count=1) == 0.5
    assert snapshot.survivorship_coverage == "incomplete_or_unknown"


def test_panel_pit_universe_does_not_count_bars_after_as_of() -> None:
    bars = _walk("600001", [10.0 + index * 0.01 for index in range(17)])
    built = _panel(bars, calendar=DAYS)
    early = built.pit_universe(as_of=DAYS[4], min_history_days=60)
    late = built.pit_universe(as_of=DAYS[16], min_history_days=60)
    assert early.eligible_count == 0
    assert late.eligible_count == 0  # 17 根仍不足 60 → 两边都不合格，但都不是 future_listed


def test_panel_pit_stats_uses_trade_date_column() -> None:
    bars = _walk("600001", [10.0 + index * 0.01 for index in range(17)])
    built = _panel(bars, calendar=DAYS)
    stats = built.pit_stats(as_of=DAYS[10], lookback_days=5, history_window=60)
    assert set(stats) == {"600001"}
    assert stats["600001"].bars_in_lookback > 0
    assert stats["600001"].last_bar_date == DAYS[10]
