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
