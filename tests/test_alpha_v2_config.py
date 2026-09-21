"""Alpha V2 P0-00：Feature Flag 默认值、fail-closed 组合、Legacy 配置不受影响。

对应施工蓝图 §5 P0-00 与任务卡 §11 的 Test A–E：

- Test A 默认关闭：``alpha_v2.enabled == false``（含"YAML 缺块"与"整块缺失"两种路径）；
- Test B 默认 Shadow：``shadow_only == true`` 且 ``enforce_final_selection == false``；
- Test C Legacy 参数不变：阈值 / Cross Review / 漏斗目标 / 上限一字未改；
- Test D 配置序列化：load / model_dump / copy+update / hash 全部可用，
  且**密钥轮换不改变指纹、行为配置改变必变指纹**；
- Test E 非法配置 fail-closed：``enforce_final_selection=true`` 而 ``enabled=false``
  （或 ``shadow_only=true``）在加载期直接报错。

这些测试不碰任何 Legacy 业务代码，只读配置。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from pytest import MonkeyPatch

from stock_analyzer.config import (
    AlphaV2Config,
    StockAnalyzerConfig,
    load_config,
)
from stock_analyzer.config_identity import redacted_config_hash, redacted_config_payload

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG = _ROOT / "config" / "default.yaml"


def _clear_sa_env(monkeypatch: MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SA__"):
            monkeypatch.delenv(key, raising=False)


def _load_clean_default_config(monkeypatch: MonkeyPatch) -> StockAnalyzerConfig:
    """加载受跟踪默认配置：清空 SA__* 覆盖，保证与 `.env` 无关。"""
    _clear_sa_env(monkeypatch)
    return load_config(_DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Test A：默认关闭
# ---------------------------------------------------------------------------


def test_alpha_v2_disabled_by_default(monkeypatch: MonkeyPatch) -> None:
    config = _load_clean_default_config(monkeypatch)
    assert config.alpha_v2.enabled is False


def test_alpha_v2_block_missing_from_yaml_still_defaults_disabled(
    monkeypatch: MonkeyPatch,
) -> None:
    """旧 YAML（尚无 alpha_v2 块）也必须解析成关闭态，而不是"缺字段"报错。"""
    _clear_sa_env(monkeypatch)
    raw = yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    raw.pop("alpha_v2", None)
    config = StockAnalyzerConfig.model_validate(raw)
    assert config.alpha_v2.enabled is False
    assert config.alpha_v2.shadow_only is True
    assert config.alpha_v2.enforce_final_selection is False


def test_alpha_v2_dataclass_defaults_are_off() -> None:
    defaults = AlphaV2Config()
    assert (defaults.enabled, defaults.shadow_only, defaults.enforce_final_selection) == (
        False,
        True,
        False,
    )


# ---------------------------------------------------------------------------
# Test B：默认 Shadow
# ---------------------------------------------------------------------------


def test_alpha_v2_defaults_to_shadow_only(monkeypatch: MonkeyPatch) -> None:
    config = _load_clean_default_config(monkeypatch)
    assert config.alpha_v2.shadow_only is True
    assert config.alpha_v2.enforce_final_selection is False


def test_alpha_v2_default_static_fields(monkeypatch: MonkeyPatch) -> None:
    alpha_v2 = _load_clean_default_config(monkeypatch).alpha_v2
    assert alpha_v2.artifact_root == "artifacts/alpha_v2"
    assert alpha_v2.selection_contract == "night_alpha_v2_v1"
    assert alpha_v2.model_resolver_mode == "pit_research"
    assert alpha_v2.entry_mode == "next_session_open"
    assert alpha_v2.primary_horizon_days == 5
    assert alpha_v2.candidate_output_top_k == 5


# ---------------------------------------------------------------------------
# Test C：Legacy 参数保持不变
# ---------------------------------------------------------------------------


def test_alpha_v2_does_not_touch_legacy_thresholds(monkeypatch: MonkeyPatch) -> None:
    config = _load_clean_default_config(monkeypatch)
    week5 = config.week5
    # 禁止修改清单（蓝图 §18）：这里逐一钉死，任何"顺手调阈值"都会被测试拦下。
    assert week5.final_signal_min_threshold == 70.0
    assert week5.final_signal_cap == 5
    assert week5.allow_zero_signal is True
    assert week5.night_quality_target == 300
    assert week5.night_light_candidate_target == 100
    assert week5.night_deep_candidate_target == 50
    assert week5.light_candidate_target == 100
    assert week5.deep_candidate_target == 20
    assert week5.universe_quality_target_size == 100
    cross_review = config.models.cross_review
    assert cross_review.p_lgbm_min == 0.60
    assert cross_review.p_xgb_min == 0.55
    assert cross_review.p_meta_min == 0.54
    assert cross_review.max_diff == 0.18


def test_alpha_v2_values_do_not_alias_legacy_keys(monkeypatch: MonkeyPatch) -> None:
    """Alpha V2 的漏斗/上限字段与 Legacy 同名字段必须是两套（不许共享）。"""
    config = _load_clean_default_config(monkeypatch)
    assert config.alpha_v2.candidate_output_top_k == 5
    # Legacy cap 也是 5，但两者不是同一字段对象：改 V2 不许动 Legacy。
    patched = config.model_copy(
        update={"alpha_v2": config.alpha_v2.model_copy(update={"candidate_output_top_k": 3})}
    )
    assert patched.alpha_v2.candidate_output_top_k == 3
    assert patched.week5.final_signal_cap == 5


def test_alpha_v2_toggles_leave_legacy_surface_unchanged(monkeypatch: MonkeyPatch) -> None:
    """打开 V2 开关不会改动任何 Legacy 行为面取值。"""
    from stock_analyzer.alpha_v2.baseline import behavior_surface_snapshot

    config = _load_clean_default_config(monkeypatch)
    baseline = behavior_surface_snapshot(config)
    shadow = config.model_copy(
        update={"alpha_v2": config.alpha_v2.model_copy(update={"enabled": True})}
    )
    enforcing = config.model_copy(
        update={
            "alpha_v2": config.alpha_v2.model_copy(
                update={"enabled": True, "shadow_only": False, "enforce_final_selection": True}
            )
        }
    )
    assert behavior_surface_snapshot(shadow) == baseline
    assert behavior_surface_snapshot(enforcing) == baseline


# ---------------------------------------------------------------------------
# Test D：配置序列化 / 反序列化 / 拷贝 / 哈希
# ---------------------------------------------------------------------------


def test_alpha_v2_config_round_trip(monkeypatch: MonkeyPatch) -> None:
    config = _load_clean_default_config(monkeypatch)
    dumped = config.model_dump(mode="json")
    assert dumped["alpha_v2"] == {
        "enabled": False,
        "shadow_only": True,
        "enforce_final_selection": False,
        "artifact_root": "artifacts/alpha_v2",
        "selection_contract": "night_alpha_v2_v1",
        "model_resolver_mode": "pit_research",
        "entry_mode": "next_session_open",
        "primary_horizon_days": 5,
        "candidate_output_top_k": 5,
        # M4-L：生产漏斗工件根 + 影子日常循环窗口 + preflight 新鲜度上限。
        # 全部是"安全默认"：enabled 仍为 False ⇒ 不注册任何调度、不写任何工件。
        "production_funnel_root": "artifacts/runtime/production_funnel",
        "live_cycle_start_time": "22:00",
        "live_cycle_latest_time": "23:55",
        "live_cycle_interval_minutes": 5,
        "preflight_max_age_hours": 48.0,
    }
    # 反序列化只对 alpha_v2 子块断言：整份配置的 dump->validate 在本仓库
    # 本就不等价（limit_rule 用 alias "from" 建字段，裸 model_dump 输出
    # field name "from_date"，见 Deferred Findings），与本任务无关。
    restored = AlphaV2Config.model_validate(dumped["alpha_v2"])
    assert restored == config.alpha_v2


def test_alpha_v2_config_env_override_and_copy_update(monkeypatch: MonkeyPatch) -> None:
    _clear_sa_env(monkeypatch)
    monkeypatch.setenv("SA__ALPHA_V2__ENABLED", "true")
    config = load_config(_DEFAULT_CONFIG)
    assert config.alpha_v2.enabled is True
    assert config.alpha_v2.shadow_only is True  # Shadow 期默认语义不变
    copied = config.model_copy(update={"alpha_v2": AlphaV2Config()})
    assert copied.alpha_v2.enabled is False


def test_redacted_config_hash_is_stable_and_behavior_sensitive(
    monkeypatch: MonkeyPatch,
) -> None:
    config = _load_clean_default_config(monkeypatch)
    first = redacted_config_hash(config)
    assert first == redacted_config_hash(load_config(_DEFAULT_CONFIG))
    assert len(first) == 64

    # 行为配置变化 -> 指纹必变（否则基线对照会漏掉真实改动）
    behavior_changed = config.model_copy(
        update={"week5": config.week5.model_copy(update={"final_signal_min_threshold": 65.0})}
    )
    assert redacted_config_hash(behavior_changed) != first

    # 密钥轮换 -> 指纹不变（同一套行为配置在本地/NAS 必须得到同一指纹）
    secret_a = config.model_copy(
        update={
            "notifications": config.notifications.model_copy(
                update={"feishu_webhook": "https://example.invalid/hook-a"}
            )
        }
    )
    secret_b = config.model_copy(
        update={
            "notifications": config.notifications.model_copy(
                update={"feishu_webhook": "https://example.invalid/hook-b"}
            )
        }
    )
    assert redacted_config_hash(secret_a) == first
    assert redacted_config_hash(secret_b) == first


def test_redacted_config_payload_masks_secrets(monkeypatch: MonkeyPatch) -> None:
    config = _load_clean_default_config(monkeypatch)
    secret_config = config.model_copy(
        update={
            "notifications": config.notifications.model_copy(
                update={"telegram_bot_token": "123456:SUPER-SECRET-BOT-TOKEN"}
            )
        }
    )
    payload = redacted_config_payload(secret_config)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SUPER-SECRET-BOT-TOKEN" not in serialized
    assert payload["notifications"]["telegram_bot_token"] == "<redacted>"
    # 控制组：非凭据字段不受脱敏影响（脱敏是字段级替换，不是整块删除）
    assert payload["week5"]["final_signal_min_threshold"] == 70.0
    assert payload["app"]["mode"] == "simulation"


# ---------------------------------------------------------------------------
# Test E：非法配置 fail-closed
# ---------------------------------------------------------------------------


def test_alpha_v2_enforce_requires_enabled() -> None:
    with pytest.raises(ValidationError, match="requires enabled=true"):
        AlphaV2Config(enabled=False, enforce_final_selection=True)


def test_alpha_v2_enforce_requires_shadow_only_false() -> None:
    with pytest.raises(ValidationError, match="requires shadow_only=false"):
        AlphaV2Config(enabled=True, shadow_only=True, enforce_final_selection=True)


def test_alpha_v2_legal_switch_combinations_are_accepted() -> None:
    off = AlphaV2Config()
    shadow = AlphaV2Config(enabled=True, shadow_only=True, enforce_final_selection=False)
    enforcing = AlphaV2Config(enabled=True, shadow_only=False, enforce_final_selection=True)
    assert (off.enabled, off.enforce_final_selection) == (False, False)
    assert shadow.enforce_final_selection is False
    assert enforcing.enforce_final_selection is True


def test_alpha_v2_invalid_env_override_fails_closed(monkeypatch: MonkeyPatch) -> None:
    """通过 SA__ 环境变量拼出的非法组合同样在加载期失败（NAS 开关路径）。"""
    config = _load_clean_default_config(monkeypatch)
    monkeypatch.setenv("SA__ALPHA_V2__ENFORCE_FINAL_SELECTION", "true")
    with pytest.raises(ValidationError, match="requires enabled=true"):
        load_config(_DEFAULT_CONFIG)
    assert config.alpha_v2.enabled is False


def test_alpha_v2_invalid_enum_and_range_rejected() -> None:
    with pytest.raises(ValidationError, match="model_resolver_mode"):
        AlphaV2Config(model_resolver_mode="whatever")
    with pytest.raises(ValidationError, match="entry_mode"):
        AlphaV2Config(entry_mode="t_close")
    with pytest.raises(ValidationError, match="must be > 0"):
        AlphaV2Config(candidate_output_top_k=0)
    with pytest.raises(ValidationError, match="must not be empty"):
        AlphaV2Config(artifact_root="   ")
