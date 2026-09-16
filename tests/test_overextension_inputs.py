"""过热闸输入：**必须喂真值**，否则闸门退化为"无条件否决"。

2026-09-16 事故：生产把只有 OHLC 列的 bar 喂给 `evaluate_overextension`，而
`bias_ma5`/`atr_distance` 依赖 `ma5`/`atr14` 两列——缺失时 evaluator 取占位常量
（`DEFAULT_MA5_FALLBACK=1.0`、`DEFAULT_ATR14_FALLBACK=0.03`），算出

    bias_ma5     = |close / 1.0 - 1| = close - 1
    atr_distance = (close - 1) / 0.03 = 33.333 × bias_ma5

于是**任何股价 > 1.15 元的票**都同时越过 `bias_reject_min=0.15` 与
`atr_distance_reject=3.0`。实测 12 轮夜扫 600 条候选 level 全是 reject、
`atr/bias` 恒为 33.333，夜扫因此长期 0 信号。

本文件钉两件事：①算 ma5/atr14 的公式口径；②生产路径必须用算出来的真值。
"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_analyzer.config import OverextensionConfig
from stock_analyzer.risk.overextension import (
    evaluate_overextension,
    overextension_inputs_from_ohlc,
    overextension_row_from_bars,
)

# 6 根 bar：末根收盘 12，MA5 = (11+11+11+12+12)/5 = 11.4，TR 恒为 2
_NORMAL = [
    (10.0, 11.0, 9.0, 10.0),
    (10.0, 12.0, 10.0, 11.0),
    (11.0, 12.0, 10.0, 11.0),
    (11.0, 12.0, 10.0, 11.0),
    (11.0, 13.0, 11.0, 12.0),
    (12.0, 13.0, 11.0, 12.0),
]


def _bars_frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["open", "high", "low", "close"],
        index=pd.RangeIndex(start=1, stop=len(rows) + 1),
    )


# --- 公式口径（唯一一份定义，gate_metrics 与生产路径共用）----------------------


def test_inputs_follow_documented_formula() -> None:
    inputs = overextension_inputs_from_ohlc([list(r) for r in _NORMAL])
    assert inputs is not None
    assert inputs.ma5 == pytest.approx(11.4)
    assert inputs.atr14 == pytest.approx(2.0)
    assert inputs.close == pytest.approx(12.0)
    assert inputs.bias_ma5 == pytest.approx(abs(12.0 / 11.4 - 1.0))
    assert inputs.atr_distance == pytest.approx(abs(12.0 - 11.4) / 2.0)
    assert inputs.ret5 == pytest.approx(12.0 / 11.0 - 1.0)
    assert inputs.gap_pct == pytest.approx((12.0 - 12.0) / 12.0)


def test_inputs_are_scale_invariant() -> None:
    """bias_ma5 / atr_distance 对价格均匀缩放不变 —— 故不要求与 evaluator 的其它
    调用方共用复权口径（qfq/raw 只差一个常因子）。"""
    base = overextension_inputs_from_ohlc([list(r) for r in _NORMAL])
    scaled = overextension_inputs_from_ohlc([[v * 3.7 for v in r] for r in _NORMAL])
    assert base is not None and scaled is not None
    assert scaled.bias_ma5 == pytest.approx(base.bias_ma5)
    assert scaled.atr_distance == pytest.approx(base.atr_distance)


def test_inputs_none_when_history_too_short() -> None:
    """历史不足必须返回 None —— **绝不返回占位值**（占位值参与阈值比较就会算出
    荒谬结论，正是本次事故的形态）。调用方据此走 fail-open 的既有默认。"""
    assert overextension_inputs_from_ohlc([list(r) for r in _NORMAL[:4]]) is None
    assert overextension_inputs_from_ohlc([]) is None


# --- 生产路径：喂真值 vs 只有 OHLC 列 -----------------------------------------


def test_bare_bar_row_reproduces_the_production_bug() -> None:
    """**回归证据**：只喂 OHLC 列的末根 bar（旧生产路径），一只正常票也被判 reject。

    断言 bias ≈ close-1、atr/bias ≈ 33.333，把事故形态写进测试，防止有人再
    "顺手"把 row 换回 _latest_bar_dict。
    """
    config = OverextensionConfig()
    bare = {"open": 12.0, "high": 13.0, "low": 11.0, "close": 12.0}
    decision = evaluate_overextension(row=bare, config=config)
    assert decision.level == "reject"
    assert decision.reject_new_buy is True
    metrics = decision.metrics
    assert metrics["bias_ma5"] == pytest.approx(12.0 - 1.0)  # close - 1
    assert metrics["atr_distance"] / metrics["bias_ma5"] == pytest.approx(33.333, abs=0.01)


def test_row_from_bars_clears_the_false_reject() -> None:
    """修好后：同一只正常票不再被判"过热"（bias 5.3%、atr 0.3，远低于 reject 档）。"""
    config = OverextensionConfig()
    row = overextension_row_from_bars(_bars_frame(_NORMAL))
    assert row["ma5"] == pytest.approx(11.4)
    assert row["atr14"] == pytest.approx(2.0)
    decision = evaluate_overextension(row=row, config=config)
    assert decision.level == "none"
    assert decision.reject_new_buy is False


def test_row_from_bars_still_rejects_a_genuinely_extended_stock() -> None:
    """反向护栏：修好输入不等于关掉闸门——真的过热（单日 +22%）仍必须 reject。"""
    config = OverextensionConfig()
    hot = [*_NORMAL[:-1], (12.0, 14.7, 12.0, 14.6)]
    row = overextension_row_from_bars(_bars_frame(hot))
    decision = evaluate_overextension(row=row, config=config)
    assert decision.level == "reject"
    assert decision.reject_new_buy is True


def test_row_from_bars_leaves_inputs_absent_when_history_short() -> None:
    """历史不足时不注入任何占位键，让 evaluator 的"缺失"路径接手（而非假值比阈值）。"""
    row = overextension_row_from_bars(_bars_frame(_NORMAL[:3]))
    assert "ma5" not in row
    assert "atr14" not in row


def test_row_from_bars_keeps_base_row_when_no_ohlc_columns() -> None:
    """没有 OHLC 列时原样返回 base_row（不注入任何键）——生产就是这么用的：
    `overextension_row_from_bars(bars, base_row=_latest_bar_dict(bars))`。"""
    frame = pd.DataFrame({"symbol": ["000001"], "score": [61.2]})
    base = {"symbol": "000001", "score": 61.2}
    row = overextension_row_from_bars(frame, base_row=base)
    assert row == base
    assert "ma5" not in row and "atr14" not in row


# --- 生产的实际入口（不是只有 helper）-----------------------------------------


def test_production_entry_points_clear_the_false_reject() -> None:
    """走**生产中真正被调用的两个函数**（`_overextension_row` →
    `_overextension_decision_dict`），确认正常票不再被判过热。

    只测 helper 不够：事故的形态恰恰是"helper 是对的，但生产没喂对 row"。
    """
    from stock_analyzer.runtime.services.week5_service import (
        _overextension_decision_dict,
        _overextension_row,
    )

    config = OverextensionConfig()
    decision = _overextension_decision_dict(
        row=_overextension_row(_bars_frame(_NORMAL)), config=config
    )
    assert decision["level"] == "none"
    assert decision["reject_new_buy"] is False
    # evaluator 的 metrics 保留 6 位小数
    assert decision["metrics"]["bias_ma5"] == pytest.approx(abs(12.0 / 11.4 - 1.0), abs=1e-6)

    # 旧的裸 bar（只有 OHLC 列）在同一入口下必然 reject —— 对照
    bare = _overextension_decision_dict(
        row={"open": 12.0, "high": 13.0, "low": 11.0, "close": 12.0}, config=config
    )
    assert bare["level"] == "reject"
    assert bare["reject_new_buy"] is True
