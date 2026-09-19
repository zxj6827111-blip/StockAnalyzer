"""M3 冻结 Shadow 模型工件测试（训练确定 / 持久化 / 哈希校验 / fail-closed）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
from stock_analyzer.alpha_v2.validation.frozen_model import (
    FrozenModelError,
    fit_frozen_model,
    frozen_model_identity_payload,
    load_frozen_model,
    persist_frozen_model,
    predict_frozen_model_matrix,
)

SAFE_FEATURES = ["ret_1d", "ret_5d", "ma5", "ma20", "volume_ratio_5", "turnover_zscore20"]


def _matrix(rows_train: int = 160, rows_cal: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    total = rows_train + rows_cal
    days = np.repeat(np.arange(total // 20), 20)[:total]
    days = np.asarray([f"2026-07-{int(d) + 1:02d}" for d in days])
    frame = pd.DataFrame(
        {
            "decision_date": days,
            "symbol": [f"6000{index % 20:02d}" for index in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in SAFE_FEATURES},
        }
    )
    # 用特征+噪声造真实可学的目标
    base = 0.3 * frame["ret_1d"] + 0.2 * frame["ma5"] - 0.1 * frame["volume_ratio_5"]
    for horizon in (3, 5, 10, 15):
        frame[f"net_return_{horizon}d"] = base * (horizon / 5.0) + rng.normal(0.0, 0.01, total)
        frame[f"excess_return_{horizon}d"] = frame[f"net_return_{horizon}d"] - 0.001
        frame[f"mae_{horizon}d"] = -np.abs(frame[f"net_return_{horizon}d"]) * 0.6
        frame[f"up_net_{horizon}d"] = (frame[f"net_return_{horizon}d"] > 0).astype(float)
        frame[f"up_excess_{horizon}d"] = (frame[f"excess_return_{horizon}d"] > 0).astype(float)
        frame[f"mae_le_5pct_{horizon}d"] = (frame[f"mae_{horizon}d"] <= -0.05).astype(float)
    frame["alpha_target_5d"] = frame["excess_return_5d"].groupby(frame["decision_date"]).rank(
        pct=True
    )
    frame["is_train"] = False
    frame["is_calibration"] = False
    frame.loc[: rows_train - 1, "is_train"] = True
    frame.loc[rows_train : rows_train + rows_cal - 1, "is_calibration"] = True
    return frame


def _spec() -> HeadFitSpec:
    return HeadFitSpec(min_train_rows=20, min_class_balance=0.05)


def test_fit_persist_load_roundtrip(tmp_path):
    frame = _matrix()
    model = fit_frozen_model(frame=frame, model_id="epoch_test", spec=_spec(),
                             created_at="2026-09-18T22:00:00+08:00")
    assert set(model.boosters)  # 有训练成功的目标
    assert model.feature_columns == tuple(sorted(SAFE_FEATURES))
    out_dir = persist_frozen_model(model, tmp_path)
    loaded = load_frozen_model(out_dir)
    assert loaded.model_id == model.model_id
    assert loaded.feature_columns == model.feature_columns
    # 同一份工件同一输入：两遍预测完全一致（确定性）
    sample = frame.drop(columns=["is_train", "is_calibration"]).head(25)
    first = predict_frozen_model_matrix(loaded, sample)
    loaded2 = load_frozen_model(out_dir)
    second = predict_frozen_model_matrix(loaded2, sample)
    pd.testing.assert_frame_equal(first, second)


def test_artifact_hash_verification_refuses_mismatch(tmp_path):
    frame = _matrix()
    model = fit_frozen_model(frame=frame, model_id="epoch_test", spec=_spec(),
                             created_at="2026-09-18T22:00:00+08:00")
    out_dir = persist_frozen_model(model, tmp_path)
    payload = frozen_model_identity_payload(out_dir)
    assert payload["status"] == "frozen"
    with pytest.raises(FrozenModelError):
        load_frozen_model(out_dir, expected_artifact_hash="0" * 64)
    # 篡改 booster 内容 → 文件哈希校验拒绝
    some_booster = next(Path(out_dir).glob("booster__*.txt"))
    some_booster.write_text("corrupted", encoding="utf-8")
    with pytest.raises(FrozenModelError, match="哈希不符"):
        load_frozen_model(out_dir)


def test_train_calibration_overlap_is_refused(tmp_path):
    frame = _matrix()
    frame["is_train"] = True
    frame["is_calibration"] = True  # 与训练集完全重叠
    with pytest.raises(FrozenModelError, match="重叠"):
        fit_frozen_model(frame=frame, model_id="bad_overlap", spec=_spec())


def test_predict_refuses_missing_feature_column(tmp_path):
    frame = _matrix()
    model = fit_frozen_model(frame=frame, model_id="epoch_test", spec=_spec(),
                             created_at="2026-09-18T22:00:00+08:00")
    sample = frame.drop(columns=["is_train", "is_calibration", "ret_1d"]).head(10)
    with pytest.raises(FrozenModelError, match="缺冻结特征列"):
        predict_frozen_model_matrix(model, sample)


def test_uncalibrated_direction_makes_no_probability_claim(tmp_path):
    # 校准窗给了 0 行 → 无校准器；输出里不得出现 *_calibrated，semantic 为 none
    frame = _matrix()
    frame["is_calibration"] = False
    model = fit_frozen_model(frame=frame, model_id="epoch_test", spec=_spec(),
                             created_at="2026-09-18T22:00:00+08:00")
    assert model.calibrators == {}
    out = predict_frozen_model_matrix(model, frame.head(10))
    assert "p_up_net_5d_calibrated" not in out.columns
    assert set(out["direction_calibration"].unique()) == {"none"}


def test_calibration_window_produces_oos_calibrator(tmp_path):
    frame = _matrix()
    model = fit_frozen_model(frame=frame, model_id="epoch_test", spec=_spec(),
                             created_at="2026-09-18T22:00:00+08:00")
    assert "p_up_net_5d" in model.calibrators  # 二分类目标上有 OOS 校准器
    out = predict_frozen_model_matrix(model, frame.head(10))
    assert "p_up_net_5d_calibrated" in out.columns
    assert set(out["direction_calibration"].unique()) == {"isotonic_oos"}


def test_unsafe_feature_column_rejected_at_fit():
    frame = _matrix()
    frame["embezzled_secret"] = frame["ret_1d"]  # 未登记列名
    with pytest.raises(ValueError, match="fail-closed"):
        fit_frozen_model(frame=frame, model_id="bad_cols", spec=_spec())


def test_two_fits_are_deterministic():
    frame = _matrix()
    a = fit_frozen_model(frame=frame, model_id="m", spec=_spec(),
                         created_at="2026-09-18T22:00:00+08:00")
    b = fit_frozen_model(frame=frame, model_id="m", spec=_spec(),
                         created_at="2026-09-18T22:00:00+08:00")
    sample = frame.head(15)
    pa = predict_frozen_model_matrix(a, sample)
    pb = predict_frozen_model_matrix(b, sample)
    pd.testing.assert_frame_equal(pa, pb)
