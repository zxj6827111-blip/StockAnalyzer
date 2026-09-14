"""C1 实现验证：用 NAS 上 return_rank 18-fold 报告的逐日 IC 序列，
复现**审核方独立计算**的圆形连续块 bootstrap CI。

参考值来自整改方案 v2 的 C1 保留结论：342 个有效日、IC 均值 +0.065810，
块长 1/5/10/20 日的 95% CI 分别为 [+0.053,+0.079] / [+0.041,+0.091] /
[+0.036,+0.096] / [+0.034,+0.097]。两侧实现（审核方 / 本仓库）互相独立，
数值吻合即证明本实现的块口径正确；块长 1 的结果还必须落在原报告用旧逐日
独立重采样得到的 CI 上（同一口径的退化情形）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_analyzer.learning.scoring_eval import (
    DEFAULT_BLOCK_TRADING_DAYS,
    date_block_bootstrap_ci,
)

FIXTURE = Path(__file__).parent / "fixtures" / "return_rank_18fold_daily_ic.json"


def _load() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _daily_ic(payload: dict[str, object]) -> list[tuple[object, float]]:
    folds = payload["folds"]
    assert isinstance(folds, list)
    return [(day, value) for fold in folds for day, value in fold["daily_ic"]]


class TestReturnRankBlockBootstrapReference:
    def test_fixture_provenance_and_shape(self) -> None:
        payload = _load()
        folds = payload["folds"]
        assert isinstance(folds, list) and len(folds) == 18
        daily = _daily_ic(payload)
        assert len(daily) == 342
        assert sum(1 for fold in folds if fold["status"] == "completed") == 18
        assert sum(int(fold["lookahead_violations"]) for fold in folds) == 0
        mean = sum(value for _, value in daily) / len(daily)
        assert mean == pytest.approx(float(payload["aggregate_ic_mean"]), abs=1e-9)  # type: ignore[arg-type]

    def test_default_block_length_is_prespecified(self) -> None:
        assert DEFAULT_BLOCK_TRADING_DAYS == 5

    @pytest.mark.parametrize(
        ("block_days", "expected"),
        [
            (1, (0.053, 0.079)),
            (5, (0.041, 0.091)),
            (10, (0.036, 0.096)),
            (20, (0.034, 0.097)),
        ],
    )
    def test_ci_matches_reviewer_independent_computation(
        self, block_days: int, expected: tuple[float, float]
    ) -> None:
        payload = _load()
        daily = _daily_ic(payload)
        ci = date_block_bootstrap_ci(daily, n_boot=2000, seed=20260905, block_days=block_days)
        assert ci["valid_days"] == 342
        assert ci["method"] == "moving_block"
        assert ci["block_days"] == block_days
        # 两个独立实现的数值一致（容差 0.003 ≈ 分位数估计的蒙特卡洛误差）
        assert float(ci["ci_low"]) == pytest.approx(expected[0], abs=0.003)
        assert float(ci["ci_high"]) == pytest.approx(expected[1], abs=0.003)
        # 保留结论的可复现部分：四个块长下 CI 全为正 → 支持继续研究
        assert float(ci["ci_low"]) > 0.0

    def test_block_one_reproduces_legacy_iid_ci(self) -> None:
        payload = _load()
        legacy = payload["report_ci95_legacy_iid"]
        ci = date_block_bootstrap_ci(_daily_ic(payload), n_boot=4000, seed=20260905, block_days=1)
        assert isinstance(legacy, list)
        assert float(ci["ci_low"]) == pytest.approx(float(legacy[0]), abs=0.002)
        assert float(ci["ci_high"]) == pytest.approx(float(legacy[1]), abs=0.002)

    def test_ci_widens_with_block_length(self) -> None:
        # 日间自相关存在时，块长 1→5→10 的 CI 必须递增变宽（这正是修正的动机）。
        daily = _daily_ic(_load())
        widths = []
        for block in (1, 5, 10):
            ci = date_block_bootstrap_ci(daily, n_boot=2000, seed=20260905, block_days=block)
            widths.append(float(ci["ci_high"]) - float(ci["ci_low"]))
        assert widths[0] < widths[1] < widths[2], widths
