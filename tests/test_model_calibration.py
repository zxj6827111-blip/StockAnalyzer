"""IsotonicCalibrator 阶梯契约回归测试（A2）。

被测契约（阶梯按右端点保存）：

- ``x_right`` 严格递增，第 i 个区间为 ``(x_right[i-1], x_right[i]]``；
- 命中右端点时取**当前**区间，不得跳到下一区间（这是 9/13 定位到的实现缺陷：
  ``searchsorted(..., side="right")`` 让梯度整体错位一格）；
- 相同分数合并为一个加权块，否则同分块不可达且同一输入会得到两个不同输出；
- 空载荷容忍加载（旧工件占位），但预测必须显式失败而不是用 -1 索引读到错值。

本文件只验证实现契约，**不**主张修复能挽救 9/13 的退化工件：xgb 校准器
拟合后只剩一个区间，塌缩另有原因（校准窗上底模反向）。
"""

from __future__ import annotations

import numpy as np
import pytest

from stock_analyzer.models.calibration import IsotonicCalibrator


def _fit(scores: list[float], labels: list[float]) -> IsotonicCalibrator:
    calibrator = IsotonicCalibrator()
    calibrator.fit(np.asarray(scores, dtype=float), np.asarray(labels, dtype=float))
    return calibrator


def test_predict_at_left_edge_uses_first_interval() -> None:
    """F16 反例：拟合点 0.1 必须返回 0，不得被送进下一区间。"""
    calibrator = _fit([0.1, 0.9], [0.0, 1.0])

    assert calibrator.predict(np.asarray([0.1, 0.5, 0.9], dtype=float)).tolist() == [
        0.0,
        1.0,
        1.0,
    ]


def test_predict_at_each_right_endpoint_stays_in_its_interval() -> None:
    """逐右端点断言：``predict(x_right[i])`` 取第 i 块，不得前移一块。"""
    calibrator = _fit([0.1, 0.2, 0.4, 0.6, 0.8, 0.9], [0.0, 0.0, 0.0, 1.0, 1.0, 1.0])
    payload = calibrator.to_dict()
    right_endpoints = payload["x_right"]
    values = payload["y_hat"]

    # 逐块独立验证：右端点命中自身区间（side="right" 会让每个端点前移一块）。
    for index, endpoint in enumerate(right_endpoints):
        got = float(calibrator.predict(np.asarray([endpoint], dtype=float))[0])
        assert got == pytest.approx(float(values[index])), (
            f"右端点 {endpoint} 未命中区间 {index}，实际 {got}"
        )


def test_interval_contract_is_left_open_right_closed() -> None:
    """区间为左开右闭：一端之隔的分数必须落在相邻两块。"""
    calibrator = _fit([0.25, 0.75], [0.2, 0.9])

    got = calibrator.predict(np.asarray([0.249999, 0.25, 0.250001, 0.749999, 0.75], dtype=float))
    assert got.tolist() == pytest.approx([0.2, 0.2, 0.9, 0.9, 0.9])


def test_tied_scores_are_pooled_into_one_weighted_block() -> None:
    """重复分数必须聚合成一个加权块，同一输入只有一个输出。"""
    calibrator = _fit([0.5, 0.5], [0.0, 1.0])

    payload = calibrator.to_dict()
    assert payload["x_right"] == [0.5]
    assert payload["y_hat"] == pytest.approx([0.5])
    assert calibrator.predict(np.asarray([0.5], dtype=float)).tolist() == pytest.approx([0.5])

    # 加权的方向性：三个同分样本 [1, 0, 0] 的块均值是 1/3，不是 0 或 1。
    weighted = _fit([0.5, 0.5, 0.5], [1.0, 0.0, 0.0])
    assert weighted.predict(np.asarray([0.5], dtype=float)).tolist() == pytest.approx([1.0 / 3.0])


def test_ties_do_not_break_strict_monotonicity_of_grid() -> None:
    # 同分样本先合并再走 PAV：0.3 的块被 0.5 拉平后与 0.1 块相邻，
    # x_right 必须保持严格递增（否则区间契约下后一块不可达）。
    calibrator = _fit([0.3, 0.1, 0.1, 0.9, 0.5], [1.0, 0.0, 0.0, 1.0, 0.0])

    payload = calibrator.to_dict()
    assert payload["x_right"] == [0.1, 0.5, 0.9]
    assert payload["y_hat"] == pytest.approx([0.0, 0.5, 1.0])
    # 每个 grid 区间内的分数返回同一个值（0.3 已并入 (0.1, 0.5]）。
    assert calibrator.predict(np.asarray([0.11, 0.3, 0.5], dtype=float)).tolist() == pytest.approx(
        [0.5, 0.5, 0.5]
    )


def test_predict_clamps_outside_the_fitted_range() -> None:
    """越界：低于首端点落第 0 块，高于末端点按末块外推。"""
    calibrator = _fit([0.2, 0.8], [0.1, 0.9])

    assert calibrator.predict(np.asarray([-3.0], dtype=float)).tolist() == pytest.approx([0.1])
    assert calibrator.predict(np.asarray([9.0], dtype=float)).tolist() == pytest.approx([0.9])
    assert calibrator.predict(np.asarray([-1e12, 1e12], dtype=float)).tolist() == pytest.approx(
        [0.1, 0.9]
    )


def test_dict_round_trip_preserves_predictions_and_payload() -> None:
    scores = np.asarray([0.05, 0.2, 0.2, 0.45, 0.7, 0.95], dtype=float)
    labels = np.asarray([0.0, 0.0, 1.0, 1.0, 0.0, 1.0], dtype=float)
    calibrator = _fit(scores.tolist(), labels.tolist())
    payload = calibrator.to_dict()

    restored = IsotonicCalibrator.from_dict(payload)

    assert restored.to_dict() == payload
    probe = np.asarray([-1.0, 0.05, 0.2, 0.33, 0.7, 0.95, 4.0], dtype=float)
    assert restored.predict(probe).tolist() == calibrator.predict(probe).tolist()


def test_fitted_ladder_is_monotone() -> None:
    rng = np.random.default_rng(20260913)
    for _ in range(25):
        size = int(rng.integers(2, 60))
        scores = np.round(rng.normal(size=size), 1)
        labels = (rng.random(size) > 0.5).astype(float)
        calibrator = _fit(scores.tolist(), labels.tolist())

        payload = calibrator.to_dict()
        right = np.asarray(payload["x_right"], dtype=float)
        values = np.asarray(payload["y_hat"], dtype=float)
        assert np.all(np.diff(right) > 0.0)
        assert np.all(np.diff(values) >= 0.0)


def test_predict_requires_fitted_calibrator() -> None:
    with pytest.raises(RuntimeError, match="not fitted"):
        IsotonicCalibrator().predict(np.asarray([0.5], dtype=float))


def test_empty_payload_loads_but_predict_fails_closed() -> None:
    """旧工件存在空校准表占位：加载容忍，预测必须显式失败。"""
    restored = IsotonicCalibrator.from_dict({"x_right": [], "y_hat": []})

    with pytest.raises(RuntimeError, match="no intervals"):
        restored.predict(np.asarray([0.5], dtype=float))


def test_from_dict_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        IsotonicCalibrator.from_dict({"x_right": [0.1, 0.9], "y_hat": [0.0]})


def test_from_dict_rejects_non_list_payload() -> None:
    with pytest.raises(ValueError, match="invalid isotonic payload"):
        IsotonicCalibrator.from_dict({"x_right": "0.1,0.9", "y_hat": "0,1"})


def test_fit_rejects_invalid_inputs() -> None:
    calibrator = IsotonicCalibrator()
    with pytest.raises(ValueError, match="empty calibration data"):
        calibrator.fit(np.asarray([], dtype=float), np.asarray([], dtype=float))
    with pytest.raises(ValueError, match="sizes must match"):
        calibrator.fit(np.asarray([0.1], dtype=float), np.asarray([0.0, 1.0], dtype=float))
    with pytest.raises(ValueError, match="must be 1D"):
        calibrator.fit(np.asarray([[0.1]], dtype=float), np.asarray([[0.0]], dtype=float))
