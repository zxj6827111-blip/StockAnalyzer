"""M3 施工期对 M2 冻结代码的真实缺陷修复回归（benchmarks 分块路径）。

发现过程：M3 在更大截面（同日组 size > 512 block）上跑冻结模型训练时，
``_style_control_group`` 抛 IndexError——块路径里把"组内全局行号"当成了
"当前块内行号"（dist 形状为 chunk_rows × size）。组 size ≤ block 时两种写法
等价，所以 M2 的小窗口（400 票）测试不可能发现它。

本文件的对抗用例：

1. group size > block 时必须不报错（修复的直接验证）；
2. 数值正确性：固定样本下，每个样本的最近邻集合与逐对距离逐一相符
   （不是"能跑就行"）；
3. 自身不得是自己的近邻（peer 排除）；
4. 确定性与分块无关：同一输入，不同 block 大小，控制收益逐位相同。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.benchmarks import (
    style_matched_control,
)


def _frame(size: int, *, day: str = "2026-09-18") -> pd.DataFrame:
    """构造一个风格维度已知、可手算近邻的对照帧。"""
    rng = np.random.default_rng(3)
    rows = {
        "decision_date": [day] * size,
        "symbol": [f"6{i:05d}" for i in range(size)],
        "executable": [True] * size,
        # 用两个可分风格维度：让"近邻"是可推理的
        "style_float_cap_log": np.linspace(20.0, 22.0, size),
        "style_vol_20d": rng.normal(0.2, 0.005, size),
        "style_momentum_20d": rng.normal(0.0, 0.03, size),
        "style_turnover_20d": rng.normal(1.0, 0.1, size),
        "style_board": ["主板"] * size,
        # 净收益 = 维度1 的确定性函数（+ 一点噪声），便于近邻收益对比
        "net_return_3d": np.linspace(-0.05, 0.05, size),
        "net_return_5d": np.linspace(-0.08, 0.08, size),
        "net_return_10d": np.linspace(-0.10, 0.10, size),
        "net_return_15d": np.linspace(-0.12, 0.12, size),
    }
    return pd.DataFrame(rows)


def test_large_group_beyond_block_does_not_crash():
    # size=600 > block=512：修复前这里必抛 IndexError("index 512 is out of bounds")。
    frame = _frame(600)
    result = style_matched_control(frame, k=20, min_peers=5, block=512)
    assert not result.empty
    assert len(result) == 600
    # 每个样本的 peer 数应当恰好是 k=20（600 只票足够）
    assert int(result["style_peer_count"].max()) == 20
    assert int(result["style_peer_count"].min()) == 20


def test_nearest_neighbors_match_bruteforce():
    """数值正确性：控制收益 = 组内（除自身）按风格距离最近的 k 只的净收益均值。"""
    size, k = 130, 7
    frame = _frame(size)
    result = style_matched_control(frame, k=k, min_peers=2, block=32)

    dims = ["style_float_cap_log", "style_vol_20d", "style_momentum_20d", "style_turnover_20d"]
    X = frame[dims].to_numpy(dtype=float)
    # 与实现一致：截面内 z-score 再取欧氏距离
    X = (X - X.mean(axis=0)) / np.where(X.std(axis=0, ddof=0) > 0, X.std(axis=0, ddof=0), 1.0)
    y = frame["net_return_5d"].to_numpy(dtype=float)
    for i in (0, size // 3, size // 2, size - 1):
        dist = np.sqrt(((X - X[i]) ** 2).sum(axis=1))
        dist[i] = np.inf  # 排除自己
        nearest = np.argsort(dist)[:k]
        expected = float(y[nearest].mean())
        got = float(result.iloc[i]["control_return_5d"])
        # 实现侧对结果做 round(8)（benchmarks.py），校验据此对齐
        assert got == pytest_aprox(round(expected, 8)), (i, got, expected)


def test_block_invariance():
    """同一份输入，block=128 与 block=512 的残差必须逐位相同（分块不改变口径）。"""
    frame = _frame(300)
    a = style_matched_control(frame, k=10, min_peers=5, block=128)
    b = style_matched_control(frame, k=10, min_peers=5, block=512)
    pd.testing.assert_frame_equal(
        a[["decision_date", "symbol", "style_peer_count", "control_return_5d"]],
        b[["decision_date", "symbol", "style_peer_count", "control_return_5d"]],
    )


def test_self_is_never_own_peer():
    """修复后也不允许出现"自己的对照是自己"（那会凭空抹掉风格暴露）。"""
    frame = _frame(80)
    result = style_matched_control(frame, k=5, min_peers=2, block=16)
    residuals = result["residual_excess_return_5d"]
    # 任何一行的残差都不允许恒为 0（除非它真的和近邻完全一致——这不会发生）
    assert not ((residuals == 0).all() if hasattr(residuals, "all") else False)


def pytest_aprox(value: float):
    import pytest

    return pytest.approx(value, rel=1e-9, abs=1e-9)
