"""`measure_marketwide_gate_coverage.py` 的口径测试（计划 §2 第一个诊断问题的度量工具）。

钉三件事：
1. 计数恒等式 `inputs == advanced + Σ rejected` 必须逐日成立，否则全市场读数不可用；
2. 归因只记**第一条**命中的 HARD 规则（顺序来自契约），一只票同时踩两条门不能进两个桶；
3. "过了所有硬门但不在研究清单里"的数量要真的算出来 —— 这就是清单遮蔽的合格候选，
   它决定 §2 的"前置筛选是否过早淘汰"能不能回答。
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "measure_marketwide_gate_coverage",
    REPO_ROOT / "scripts" / "measure_marketwide_gate_coverage.py",
)
gate = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gate)

DAY = date(2026, 3, 2)


def _rows(symbols: list[str], *, st: tuple[str, ...] = (),
          thin: tuple[str, ...] = (), young: tuple[str, ...] = (),
          stale: tuple[str, ...] = ()) -> pd.DataFrame:
    """每只票造 130 根日线（够 120 根历史），再按参数破坏对应条件。"""
    days = [DAY - timedelta(days=129 - i) for i in range(130)]
    rows = []
    for symbol in symbols:
        for day in days:
            rows.append({
                "symbol": symbol, "date": day,
                "turnover": 1e8 if symbol in thin else 5e9,
                "float_market_cap": 5e10,
                "is_st": 1 if symbol in st else 0,
                "is_delisting_risk": 0, "suspended": 0,
            })
    frame = pd.DataFrame(rows)
    frame["prev_bar_date"] = frame.groupby("symbol")["date"].shift(1)
    frame["bar_ordinal"] = frame.groupby("symbol").cumcount() + 1
    # 历史不足：把这只票的序号压到 120 以下（脚本按序号判 PIT 资格）。
    frame.loc[frame["symbol"].isin(young), "bar_ordinal"] = 50
    # 行情陈旧：把上一根 bar 推到 40 天前。
    for symbol in stale:
        mask = (frame["symbol"] == symbol) & (frame["date"] == DAY)
        frame.loc[mask, "prev_bar_date"] = DAY - timedelta(days=40)
    return frame


def test_marketwide_funnel_keeps_the_stage_count_identity() -> None:
    frame = _rows(["600000", "000001", "300750", "688001", "920001"])
    funnel = gate.day_funnel(frame, DAY, min_turnover=1e9, min_float_cap=1e10)
    assert funnel["inputs"] == 5
    assert funnel["identity_ok"] == 1
    assert funnel["advanced"] + sum(funnel["rejected"].values()) == funnel["inputs"]
    # 北交所代码不在 A 股前缀口径里：这一条要能被 board_eligibility 抓到
    assert funnel["rejected"]["board_eligibility"] == 1


def test_first_matching_rule_wins_and_order_comes_from_the_contract() -> None:
    """同一只票又 ST 又流动性不足时只记一条：归因顺序是契约里的那条，不是书写顺序。"""
    frame = _rows(["600000", "000001", "300750"], st=("300750",), thin=("300750",))
    funnel = gate.day_funnel(frame, DAY, min_turnover=1e9, min_float_cap=1e10)
    assert funnel["rejected"]["is_st"] == 1
    assert funnel["rejected"]["min_avg_turnover_20"] == 0
    assert funnel["identity_ok"] == 1
    # 陈旧与历史不足各自归到自己的规则名下，不互相吞
    frame2 = _rows(["600000", "000001", "688981"], stale=("688981",), young=("000001",))
    funnel2 = gate.day_funnel(frame2, DAY, min_turnover=1e9, min_float_cap=1e10)
    assert funnel2["rejected"]["stale_market_data"] == 1
    assert funnel2["rejected"]["insufficient_history_at_asof"] == 1
    assert funnel2["identity_ok"] == 1


def test_eligible_names_outside_the_research_list_are_counted() -> None:
    """清单遮蔽量的定义：过了所有硬门、但符号不在清单里。"""
    frame = _rows(["600000", "000001", "300750"])
    funnels = [gate.day_funnel(frame, DAY, min_turnover=1e9, min_float_cap=1e10)]
    summary = gate.summarize(funnels, pool={"600000"})
    assert summary["marketwide_symbol_days_advanced"] == 3
    assert summary["eligible_symbol_days_missing_from_research_list"] == 2
    assert summary["distinct_symbols_eligible_but_missing_from_research_list"] == 2
    assert summary["counting_broken_days"] == 0
