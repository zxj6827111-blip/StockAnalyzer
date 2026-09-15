"""推理分数口径（models.inference_score_source）的取值与回退契约。

背景（`docs/learning_chain_c3_variant_results_20260915.md` §6.1）：C3 配对比较实测
`raw_blend` 相对校准后 `meta` 的 ΔIC=+0.0098、CI [+0.0013,+0.0188] 不含 0，且校准后
分数在 13/342 个交易日塌成常数。据此把打分口径默认切到校准前分数，校准降级为诊断量。

本文件钉死四件事：
1. 口径解析 fail-closed（写错口径不得静默退回 calibrated）；
2. 分量取键在两种口径下分别取校准前/校准后的对应键；
3. raw 族不可用（旧接口 predictor / 降级模式）时**显式回退** calibrated 键，
   既不 KeyError 也不静默冒充 raw；
4. 默认配置就是 raw（防止有人顺手把默认改回 calibrated 而无留痕）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.config import load_config
from stock_analyzer.models.predictor import (
    SCORE_SOURCES,
    resolve_score_source,
    score_source_keys,
    scoring_components,
)

_TWO_FAMILIES = {
    "lgbm": 0.11,
    "xgb": 0.22,
    "meta": 0.165,
    "raw_lgbm": 0.31,
    "raw_xgb": 0.42,
    "raw_blend": 0.365,
}


def test_default_config_uses_raw_scores() -> None:
    """默认口径必须是 raw：改回 calibrated 等于撤回一条 CI 显著的实测结论。"""
    assert load_config().models.inference_score_source == "raw"
    assert "raw" in SCORE_SOURCES


def test_resolve_score_source_fail_closed() -> None:
    assert resolve_score_source("raw") == "raw"
    assert resolve_score_source("  CALIBRATED ") == "calibrated"
    # None 落到默认 raw（未配置时按默认口径，而不是退回修前行为）
    assert resolve_score_source(None) == "raw"
    with pytest.raises(ValueError):
        resolve_score_source("rawblend")


def test_scoring_components_pick_matching_family() -> None:
    assert scoring_components(_TWO_FAMILIES, source="calibrated") == {
        "lgbm": 0.11,
        "xgb": 0.22,
        "meta": 0.165,
    }
    # raw 口径下 meta 取 raw_blend（校准前的加权混合），不是校准后 meta
    assert scoring_components(_TWO_FAMILIES, source="raw") == {
        "lgbm": 0.31,
        "xgb": 0.42,
        "meta": 0.365,
    }
    assert score_source_keys("raw")["meta"] == "raw_blend"


def test_scoring_components_missing_raw_keys_fail_closed() -> None:
    """只有校准三键时按 raw 取分必须报错，不得静默拿校准分数冒充 raw。"""
    with pytest.raises(ValueError):
        scoring_components({"lgbm": 0.1, "xgb": 0.2, "meta": 0.15}, source="raw")


def _pipeline_with_predictor(tmp_path: Path, predictor: object):
    from stock_analyzer.pipeline import AnalyzerPipeline

    config = load_config()
    pipeline = AnalyzerPipeline(
        config,
        provider=_StubProvider(),
        news_provider=_NoNews(),
        sample_store=None,
    )
    pipeline._predictor = predictor  # noqa: SLF001 - 测试直接注入假 predictor
    return pipeline


class _NoNews:
    def score(self, **_: object) -> object:
        raise AssertionError("测试不应读取新闻")


class _StubProvider:
    """最小 provider：本测试只走特征与打分，不取真实行情。"""

    def fetch_bars(self, **_: object) -> pd.DataFrame:
        raise AssertionError("测试不应取行情")


class _LegacyPredictor:
    """旧接口：只有 predict_row（校准后三键），没有 raw 族。"""

    def predict_row(self, feature_row: pd.Series) -> dict[str, float]:
        _ = feature_row
        return {"lgbm": 0.7, "xgb": 0.6, "meta": 0.65}

    def mode_details(self) -> dict[str, object]:
        return {"degraded_model_mode": False}


class _TwoFamilyPredictor:
    """新接口：一次给出两族分数（raw_lgbm=0.9 明显高于校准后 0.2）。"""

    def predict_row(self, feature_row: pd.Series) -> dict[str, float]:
        return self.predict_row_with_raw(feature_row)

    def predict_row_with_raw(self, feature_row: pd.Series) -> dict[str, float]:
        _ = feature_row
        return {
            "lgbm": 0.2,
            "xgb": 0.2,
            "meta": 0.2,
            "raw_lgbm": 0.9,
            "raw_xgb": 0.9,
            "raw_blend": 0.9,
        }

    def mode_details(self) -> dict[str, object]:
        return {"degraded_model_mode": False}


def test_families_fall_back_to_calibrated_for_legacy_predictor(tmp_path: Path) -> None:
    """旧接口 predictor：两族同值且标记 raw_unavailable，绝不 KeyError。"""
    pipeline = _pipeline_with_predictor(tmp_path, _LegacyPredictor())
    families, raw_unavailable = pipeline._infer_probability_families(  # noqa: SLF001
        pd.Series({"f0": 1.0})
    )
    assert raw_unavailable is True
    assert families["raw"] == families["calibrated"]
    # 关键：回退后按 calibrated 键取分能取到值（否则打分路径会 KeyError）
    assert scoring_components(families["raw"], source="calibrated")["meta"] == 0.65


def test_families_expose_true_raw_keys_for_new_predictor(tmp_path: Path) -> None:
    pipeline = _pipeline_with_predictor(tmp_path, _TwoFamilyPredictor())
    families, raw_unavailable = pipeline._infer_probability_families(  # noqa: SLF001
        pd.Series({"f0": 1.0})
    )
    assert raw_unavailable is False
    assert scoring_components(families["raw"], source="raw") == {
        "lgbm": 0.9,
        "xgb": 0.9,
        "meta": 0.9,
    }
    assert scoring_components(families["calibrated"], source="calibrated")["meta"] == 0.2
