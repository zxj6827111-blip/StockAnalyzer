"""`replay_tail_candidate_pool.py --minute-db` 的日内两列口径（计划 §3.2 量价/波动两组）。

钉三件事：
1. 尾盘 30 分钟的定义是 ``bar_time >= 14:30``，且 ``bar_time`` 是 **bar_end** 语义；
2. 算不出来就是**没有这一行**，不是填 0 —— 填 0 会被训练当成"尾盘真的没量"这个事实；
3. 分钟库拿不到时返回空帧，调用方保持整列缺失（宁可不训，也不假装有值）。
"""

from __future__ import annotations

import importlib.util
import math
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "replay_tail_candidate_pool", REPO_ROOT / "scripts" / "replay_tail_candidate_pool.py"
)
replay = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(replay)

DAY = date(2026, 3, 2)


def _seed(db: Path, rows: list[tuple[str, str, float, float]]) -> None:
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE minute_bars_1min (
            symbol VARCHAR, trade_date DATE, bar_time TIMESTAMP,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, amount DOUBLE
        )
        """
    )
    con.executemany(
        "INSERT INTO minute_bars_1min VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (symbol, DAY, f"2026-03-02 {hhmm}:00", close, close, close, close, volume, 0.0)
            for symbol, hhmm, close, volume in rows
        ],
    )
    con.close()


@pytest.fixture()
def minute_db(tmp_path: Path) -> Path:
    path = tmp_path / "tail_minute_bars.duckdb"
    _seed(
        path,
        [
            # 尾盘三根：14:30 +10%、14:31 +1/11、14:32 持平；量能占全天 400/500
            ("600000.SH", "10:00", 10.0, 100.0),
            ("600000.SH", "14:30", 11.0, 100.0),
            ("600000.SH", "14:31", 12.0, 300.0),
            ("600000.SH", "14:32", 12.0, 0.0),
            # 只有一根早盘 bar：尾盘确实没量，但波动比无从计算
            ("000001.SZ", "10:00", 9.0, 50.0),
        ],
    )
    return path


def test_intraday_columns_are_computed_from_completed_minute_bars(minute_db: Path) -> None:
    frame = replay.load_intraday_features(minute_db, DAY, DAY)
    assert set(frame["symbol"]) == {"600000", "000001"}

    hot = frame[frame["symbol"] == "600000"].iloc[0]
    assert hot["date"] == DAY
    assert math.isclose(float(hot["last30_volume_share"]), 0.8, rel_tol=1e-9)
    # 尾盘那三根正好是全部有收益的 bar，所以波动比就是 1.0
    assert math.isclose(float(hot["tail_volatility_ratio"]), 1.0, rel_tol=1e-9)


def test_missing_tail_window_is_absent_rather_than_zero_filled(minute_db: Path) -> None:
    frame = replay.load_intraday_features(minute_db, DAY, DAY)

    cold = frame[frame["symbol"] == "000001"].iloc[0]
    assert float(cold["last30_volume_share"]) == 0.0  # 真的没有尾盘量
    assert pd.isna(cold["tail_volatility_ratio"])  # 无从计算，不能编一个数

    # 当天完全没有 bar 的代码：整行不出现（缺 bar ≠ 停牌，也 ≠ 0 成交量）
    assert "688036" not in set(frame["symbol"])


def test_unavailable_minute_db_yields_empty_frame_not_nan_columns(tmp_path: Path) -> None:
    frame = replay.load_intraday_features(tmp_path / "does_not_exist.duckdb", DAY, DAY)
    assert frame.empty


# --- sidecar 事实：留档词汇表必须闭合 --------------------------------------------


def _fact(**overrides):
    kwargs = {
        "decision_date": DAY, "warehouse": "market_copy.duckdb",
        "considered": ["600000", "000001", "300750"], "advanced": ["600000", "000001"],
        "rejected": {"min_avg_turnover_20": ["300750"], "is_st": ["300750"]},
        "pit_excluded": [], "coverage": "incomplete_or_unknown", "delisting_verified": False,
    }
    kwargs.update(overrides)
    return replay._universe_fact(**kwargs)


def test_universe_fact_uses_only_declared_rule_names() -> None:
    """写进 archive 的淘汰原因必须是契约登记的规则，否则 §2 的原因分布与线上不是一套语言。"""
    from stock_analyzer.feature.trend_candidate_contract import is_declared_rule

    fact = _fact()
    assert all(is_declared_rule(reason) for reason in fact["excluded_reasons"].values())
    # 一只票被两条硬门同时淘汰时只记一条：StageTrace 的计数恒等式不允许一只票进两个桶。
    # 归因顺序来自契约的 HARD_GATE_ATTRIBUTION_ORDER（is_st 排在流动性下限之前），
    # 不是 rejected 这个 dict 的插入顺序。
    assert fact["excluded_reasons"] == {"300750": "is_st"}
    assert fact["known_suspended_symbols"] == []
    assert fact["delisting_coverage_verified"] is False


def test_undeclared_gate_name_is_refused_not_recorded() -> None:
    """契约不认的名字按 predictive 处理：悄悄落进硬门留档会让消融实验漏掉这条规则。"""
    with pytest.raises(SystemExit) as caught:
        _fact(rejected={"composite_score_floor": ["300750"]})
    assert "composite_score_floor" in str(caught.value)


def test_attribution_follows_contract_order_not_dict_insertion() -> None:
    """``rejected`` 的键序换了，归因结果不许跟着换。

    原因分布是 §2 消融实验的输入：它若取决于代码里 dict 的书写顺序，
    换一行就会悄悄改每条规则的淘汰计数（``min_float_market_cap`` 命中 8,527 次
    却只被归因 132 次，就是这条顺序的效果）。
    """
    low_first = _fact(rejected={
        "min_float_market_cap": ["300750"], "min_avg_turnover_20": ["300750"]})
    turn_first = _fact(rejected={
        "min_avg_turnover_20": ["300750"], "min_float_market_cap": ["300750"]})
    assert low_first["excluded_reasons"] == {"300750": "min_avg_turnover_20"}
    assert turn_first["excluded_reasons"] == low_first["excluded_reasons"]


def test_symbols_file_facts_records_the_universe_input_provenance(tmp_path: Path) -> None:
    """`universe` 层的输入是这份符号清单，不是全市场 —— 必须能复现它是哪一份。

    仓库里没有产生这份清单的代码，所以"全市场→硬性资格检查"这层无法追溯；
    至少要记下路径、字节摘要与数量，否则整条链的输入不可复现。
    """
    import hashlib

    file = tmp_path / "pool.txt"
    file.write_text("# 注释行\n600000\n000001\n300750\n", encoding="utf-8")
    facts = replay.symbols_file_facts(file)
    assert facts["symbols_count"] == 3
    assert facts["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert facts["producer"] == "unknown_not_recorded_in_repo"
    # 摘要必须随内容变：只记路径等于什么都没记（清单会被就地改）。
    file.write_text("600000\n", encoding="utf-8")
    assert replay.symbols_file_facts(file)["sha256"] != facts["sha256"]


def test_every_name_the_replay_can_emit_is_a_declared_hard_rule() -> None:
    """重放真正会写进留档的名字（``daily_gates`` 的键 + PIT 原因）必须全在 HARD 词表里。

    ``insufficient_history_at_asof`` 当初只在脚本里 invented、没登记进 ``_RULE_KIND``，
    于是 §2 的原因分布里混进一条消融实验与线上都不认识的规则。
    """
    from stock_analyzer.feature.trend_candidate_contract import HARD, classify_rule

    rows = pd.DataFrame({
        "symbol": ["600000"], "date": [pd.Timestamp(DAY)], "prev_bar_date": [pd.Timestamp(DAY)],
        "is_st": [1], "is_delisting_risk": [1], "suspended": [1],
        "avg_turnover_20": [0.0], "float_market_cap": [0.0],
        "ret_20_raw": [1.0], "range_position_60": [1.0], "atr14_pct": [1.0],
    })
    emitted = set(replay.daily_gates(rows, min_turnover=1.0, min_float_cap=1.0))
    emitted.add(replay.PIT_REJECT_REASON)
    assert len(emitted) >= 8
    not_hard = sorted(name for name in emitted if classify_rule(name) != HARD)
    assert not_hard == []
