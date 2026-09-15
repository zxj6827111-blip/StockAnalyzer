"""Probability calibration tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.float64]


@dataclass(slots=True)
class _Block:
    x_right: float
    weight: float
    mean: float


class IsotonicCalibrator:
    """Piecewise-constant isotonic regression via PAV.

    区间契约（阶梯按**右端点**保存）：

    - ``x_right[i]`` 是第 i 个区间的右端点，拟合后严格递增；
    - 第 i 个区间为 ``(x_right[i-1], x_right[i]]``（左开右闭），
      第 0 个区间为 ``(-inf, x_right[0]]``；
    - ``predict`` 返回命中区间对应的 ``y_hat``；高于 ``x_right[-1]`` 的分数
      按最后一个区间外推（右端夹取），低于 ``x_right[0]`` 的分数落第 0 区间。
    - 同一分数只能映射到同一个值：拟合时相同分数会被合并成一个加权块，
      否则后一个同分块在该查询契约下永远不可达。
    """

    def __init__(self) -> None:
        self._x_right: FloatArray | None = None
        self._y_hat: FloatArray | None = None

    def fit(self, scores: FloatArray, labels: FloatArray) -> None:
        if scores.ndim != 1 or labels.ndim != 1:
            raise ValueError("scores and labels must be 1D arrays")
        if scores.shape[0] != labels.shape[0]:
            raise ValueError("scores and labels sizes must match")
        if scores.shape[0] == 0:
            raise ValueError("empty calibration data")

        order = np.argsort(scores, kind="stable")
        sorted_scores = scores[order]
        sorted_labels = labels[order]

        blocks: list[_Block] = []
        index = 0
        total = sorted_scores.shape[0]
        while index < total:
            # 相同分数先聚合成一个加权块：单调阶梯对同一输入只能给出一个输出，
            # 逐条入块会让后一个同分块在 predict 里永远命中不到。
            end = index + 1
            while end < total and sorted_scores[end] == sorted_scores[index]:
                end += 1
            weight = float(end - index)
            mean = float(np.mean(sorted_labels[index:end]))
            blocks.append(_Block(x_right=float(sorted_scores[index]), weight=weight, mean=mean))
            index = end
            while len(blocks) >= 2 and blocks[-2].mean > blocks[-1].mean:
                right = blocks.pop()
                left = blocks.pop()
                merged_weight = left.weight + right.weight
                merged_mean = (left.mean * left.weight + right.mean * right.weight) / merged_weight
                blocks.append(_Block(x_right=right.x_right, weight=merged_weight, mean=merged_mean))

        self._x_right = np.asarray([item.x_right for item in blocks], dtype=float)
        self._y_hat = np.asarray([item.mean for item in blocks], dtype=float)

    def predict(self, scores: FloatArray) -> FloatArray:
        if self._x_right is None or self._y_hat is None:
            raise RuntimeError("calibrator is not fitted")
        if self._x_right.shape[0] == 0:
            # 旧工件可能出现空校准表（bootstrap 占位）。空表无法给出任何映射，
            # 显式失败，避免用 -1 索引读到错误值。
            raise RuntimeError("calibrator has no intervals")
        # 阶梯以右端点保存，区间左开右闭，因此要取第一个 x_right >= score 的块，
        # 即 side="left"（side="right" 会在 score 恰好等于右端点时跳到下一区间）。
        positions = np.searchsorted(self._x_right, scores, side="left")
        clipped = np.clip(positions, 0, len(self._y_hat) - 1)
        return self._y_hat[clipped]

    def to_dict(self) -> dict[str, object]:
        if self._x_right is None or self._y_hat is None:
            raise RuntimeError("calibrator is not fitted")
        return {"x_right": self._x_right.tolist(), "y_hat": self._y_hat.tolist()}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> IsotonicCalibrator:
        calibrator = cls()
        raw_x = payload.get("x_right", [])
        raw_y = payload.get("y_hat", [])
        if not isinstance(raw_x, list) or not isinstance(raw_y, list):
            raise ValueError("invalid isotonic payload")
        if len(raw_x) != len(raw_y):
            raise ValueError("invalid isotonic payload: x_right/y_hat length mismatch")
        calibrator._x_right = np.asarray(raw_x, dtype=float)
        calibrator._y_hat = np.asarray(raw_y, dtype=float)
        return calibrator
