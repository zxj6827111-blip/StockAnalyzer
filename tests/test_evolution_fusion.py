from __future__ import annotations

import pytest

from stock_analyzer.evolution.core.fusion import ScoreFusionEngine


def test_fusion_cache_key_binds_active_champion_id() -> None:
    engine = ScoreFusionEngine()
    key_a = engine.build_cache_key({"M1": 70.0}, active_champion_id="champ_a")
    key_b = engine.build_cache_key({"M1": 70.0}, active_champion_id="champ_b")
    assert key_a != key_b


def test_fusion_uses_cache_on_same_key() -> None:
    engine = ScoreFusionEngine()
    first = engine.fuse({"M1": 60.0, "M3": 80.0}, active_champion_id="champ")
    second = engine.fuse({"M1": 60.0, "M3": 80.0}, active_champion_id="champ")
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.fused_score == pytest.approx(first.fused_score)


def test_fusion_respects_weights() -> None:
    engine = ScoreFusionEngine(default_weights={"M1": 2.0, "M3": 1.0})
    result = engine.fuse({"M1": 90.0, "M3": 60.0}, active_champion_id="champ")
    assert result.fused_score == pytest.approx((90.0 * 2.0 + 60.0) / 3.0)


def test_invalidate_champion_clears_related_cache_entries() -> None:
    engine = ScoreFusionEngine()
    engine.fuse({"M1": 70.0}, active_champion_id="champ")
    removed = engine.invalidate_champion("champ")
    assert removed == 1


def test_fusion_caps_bonus_uplift_for_m3_m7() -> None:
    engine = ScoreFusionEngine(
        default_weights={"M3": 1.0, "M7": 1.0},
        bonus_modules=("M3", "M7"),
        bonus_neutral_score=50.0,
        bonus_cap=10.0,
        enable_veto=False,
    )
    result = engine.fuse({"M3": 100.0, "M7": 100.0}, active_champion_id="champ")
    assert result.base_score == pytest.approx(100.0)
    assert result.bonus_raw == pytest.approx(50.0)
    assert result.bonus_capped == pytest.approx(10.0)
    assert result.fused_score == pytest.approx(60.0)
    assert "bonus_cap" in result.applied_rules


def test_fusion_applies_veto_when_confidence_is_high() -> None:
    engine = ScoreFusionEngine(
        enable_bonus_cap=False,
        enable_veto=True,
        veto_modules=("M1",),
        veto_score_threshold=60.0,
        veto_score_cap=62.0,
        veto_confidence_gate=0.75,
    )
    result = engine.fuse(
        {"M1": 50.0, "M3": 90.0},
        active_champion_id="champ",
        veto_confidence=0.80,
    )
    assert result.base_score == pytest.approx(70.0)
    assert result.fused_score == pytest.approx(62.0)
    assert result.veto_triggered is True
    assert result.veto_module == "M1"
    assert "veto:M1" in result.applied_rules
