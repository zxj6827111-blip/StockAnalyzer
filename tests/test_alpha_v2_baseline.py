"""Alpha V2 P0-00：基线清单、artifact 目录语义，以及"零行为变化"回归保护。

三组保护：

1. **Golden 回归**（``legacy_baseline_contract.json``）：受跟踪默认配置的 Legacy
   行为面必须与基线逐字段一致——任何顺手改阈值/门禁的动作都会在这里失败；
2. **架构守卫**：Legacy 源码不得消费 ``alpha_v2``（P0-00 阶段唯一的消费者是
   配置模型与本包），保证"V2 关闭 = 零行为变化"是结构性的；
3. **清单卫生**：基线清单只写白名单字段、不含任何凭据值，且写盘幂等。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from stock_analyzer.alpha_v2 import (
    ALPHA_V2_SUBDIRECTORIES,
    AlphaV2ArtifactLayout,
    behavior_surface_snapshot,
    build_baseline_manifest,
    write_baseline_manifest,
)
from stock_analyzer.config import AlphaV2Config, StockAnalyzerConfig, load_config
from stock_analyzer.config_identity import is_sensitive_config_name

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG = _ROOT / "config" / "default.yaml"
_CONTRACT_FIXTURE = _ROOT / "tests" / "fixtures" / "alpha_v2" / "legacy_baseline_contract.json"


def _clear_sa_env(monkeypatch: MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SA__"):
            monkeypatch.delenv(key, raising=False)


def _clean_default_config(monkeypatch: MonkeyPatch) -> StockAnalyzerConfig:
    _clear_sa_env(monkeypatch)
    return load_config(_DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Golden 回归：Legacy 行为面与基线一致
# ---------------------------------------------------------------------------


def test_legacy_behavior_surface_matches_frozen_contract(
    monkeypatch: MonkeyPatch,
) -> None:
    fixture = json.loads(_CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    config = _clean_default_config(monkeypatch)
    assert behavior_surface_snapshot(config) == fixture["behavior_surface"], (
        "Legacy 行为面与 P0-00 基线不一致：若是有意的业务参数变更，请连同 "
        f"{_CONTRACT_FIXTURE.name} 一起更新并在提交说明里给出理由。"
    )


def test_legacy_behavior_surface_unchanged_when_alpha_v2_enabled(
    monkeypatch: MonkeyPatch,
) -> None:
    """V2 关闭/Shadow/接管三种状态下，Legacy 行为面取值完全相同。"""
    fixture = json.loads(_CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    config = _clean_default_config(monkeypatch)
    for alpha_v2 in (
        AlphaV2Config(),
        AlphaV2Config(enabled=True, shadow_only=True),
        AlphaV2Config(enabled=True, shadow_only=False, enforce_final_selection=True),
    ):
        patched = config.model_copy(update={"alpha_v2": alpha_v2})
        assert behavior_surface_snapshot(patched) == fixture["behavior_surface"]


def test_legacy_source_tree_does_not_consume_alpha_v2_flag() -> None:
    """P0-00 架构守卫：Legacy 源码不得读取 alpha_v2 配置。

    允许清单只有"配置定义本身"与"Alpha V2 自己的包"。后续任务（P0-01+）新增
    V2 消费者时，必须同时更新这里——把"V2 会不会影响 Legacy"从口头承诺变成
    必须显式修改白名单的可审查动作。
    """
    src_root = _ROOT / "src" / "stock_analyzer"
    allowed = {
        src_root / "config.py",
        src_root / "config_identity.py",
    }
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if path in allowed or src_root / "alpha_v2" in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "alpha_v2" in text:
            offenders.append(str(path.relative_to(_ROOT)))
    assert offenders == [], f"Legacy 源码出现 alpha_v2 引用: {offenders}"


# ---------------------------------------------------------------------------
# 基线清单内容
# ---------------------------------------------------------------------------


def test_build_baseline_manifest_records_required_identity_fields(
    monkeypatch: MonkeyPatch,
) -> None:
    config = _clean_default_config(monkeypatch)
    manifest = build_baseline_manifest(
        config, project_root=_ROOT, generated_at="2026-09-17T00:00:00+00:00"
    )
    assert manifest["schema"] == "alpha_v2_baseline_manifest.v1"
    assert manifest["generated_at"] == "2026-09-17T00:00:00+00:00"
    assert len(str(manifest["config_hash"])) == 64

    alpha_v2 = manifest["alpha_v2"]
    assert isinstance(alpha_v2, dict)
    assert alpha_v2["enabled"] is False
    assert alpha_v2["shadow_only"] is True
    assert alpha_v2["enforce_final_selection"] is False
    assert alpha_v2["artifact_root"] == "artifacts/alpha_v2"

    legacy = manifest["legacy"]
    assert isinstance(legacy, dict)
    assert legacy["final_signal_min_threshold"] == 70.0
    assert legacy["final_signal_cap"] == 5
    assert legacy["allow_zero_signal"] is True
    assert legacy["night_quality_target"] == 300
    assert legacy["night_light_candidate_target"] == 100
    assert legacy["night_deep_candidate_target"] == 50
    assert legacy["cross_review"] == {
        "p_lgbm_min": 0.60,
        "p_xgb_min": 0.55,
        "p_meta_min": 0.54,
        "max_diff": 0.18,
        "dynamic_enabled": True,
    }

    training = manifest["training"]
    models = manifest["models"]
    runtime = manifest["runtime"]
    assert isinstance(training, dict) and training["artifact_path"] == "artifacts/model_v1.json"
    assert isinstance(models, dict) and models["inference_score_source"] == "raw"
    assert isinstance(runtime, dict)
    assert runtime["app_mode"] == "simulation"
    assert runtime["advisory_only"] is True
    assert runtime["theme_mode"] == "shadow"
    assert runtime["news_risk_mode"] == "shadow"

    # 代码身份：仓库内必须能取到真实 commit（容器/无 .git 环境下允许 unknown）
    commit = str(manifest["code_commit"])
    assert commit == "unknown" or len(commit) == 40
    assert isinstance(manifest["git_branch"], str) and manifest["git_branch"] != ""


def test_baseline_manifest_contains_no_credentials(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """注入假凭据后生成清单：清单里不得出现这些值，也不得出现敏感字段名。"""
    _clear_sa_env(monkeypatch)
    secrets = {
        "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN": "tushare-token-SHOULD-NOT-LEAK",
        "SA__NOTIFICATIONS__FEISHU_WEBHOOK": "https://open.feishu.cn/SHOULD-NOT-LEAK",
        "SA__NOTIFICATIONS__FEISHU_APP_SECRET": "app-secret-SHOULD-NOT-LEAK",
        "SA__SECURITY__API_TOKEN": "api-token-SHOULD-NOT-LEAK",
        "SA__COMMAND_CHANNEL__SECRET_KEY": "command-key-SHOULD-NOT-LEAK",
        "SA__EVOLUTION__LLM_API_KEY": "llm-api-key-SHOULD-NOT-LEAK",
        "SA__NOTIFICATIONS__TELEGRAM_CHAT_ID": "chat-SHOULD-NOT-LEAK",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    config = load_config(_DEFAULT_CONFIG)
    target = write_baseline_manifest(
        config, project_root=tmp_path, generated_at="2026-09-17T00:00:00+00:00"
    )
    text = target.read_text(encoding="utf-8")
    for value in secrets.values():
        assert value not in text
    manifest = json.loads(text)
    assert _collect_sensitive_paths(manifest) == []


def _collect_sensitive_paths(node: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_sensitive_config_name(str(key)):
                found.append(path)
            found.extend(_collect_sensitive_paths(value, path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_collect_sensitive_paths(item, f"{prefix}[{index}]"))
    return found


# ---------------------------------------------------------------------------
# Artifact 目录语义
# ---------------------------------------------------------------------------


def test_artifact_layout_resolves_and_creates_directories(tmp_path: Path) -> None:
    config = AlphaV2Config(artifact_root="artifacts/alpha_v2")
    layout = AlphaV2ArtifactLayout.from_config(config, project_root=tmp_path)
    assert layout.root == tmp_path / "artifacts" / "alpha_v2"
    assert set(layout.subdirectory_paths()) == set(ALPHA_V2_SUBDIRECTORIES)
    assert layout.audit == layout.root / "audit"

    layout.ensure()
    for name, path in layout.subdirectory_paths().items():
        assert path.is_dir(), f"{name} 未创建: {path}"

    # 幂等：重复 ensure 不报错、不产生额外内容
    layout.ensure()
    assert sorted(p.name for p in layout.root.iterdir()) == sorted(ALPHA_V2_SUBDIRECTORIES)


def test_artifact_layout_absolute_root_is_not_reanchored(tmp_path: Path) -> None:
    absolute_root = tmp_path / "absolute_alpha_v2"
    layout = AlphaV2ArtifactLayout.from_config(
        AlphaV2Config(artifact_root=str(absolute_root)), project_root=tmp_path / "ignored"
    )
    assert layout.root == absolute_root


def test_write_baseline_manifest_is_idempotent_and_scoped(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    config = _clean_default_config(monkeypatch)
    first = write_baseline_manifest(
        config, project_root=tmp_path, generated_at="2026-09-17T00:00:00+00:00"
    )
    assert first == tmp_path / "artifacts" / "alpha_v2" / "audit" / "baseline_manifest.json"
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["generated_at"] == "2026-09-17T00:00:00+00:00"

    second = write_baseline_manifest(
        config, project_root=tmp_path, generated_at="2026-09-17T01:00:00+00:00"
    )
    assert second == first
    rewritten = json.loads(second.read_text(encoding="utf-8"))
    assert rewritten["generated_at"] == "2026-09-17T01:00:00+00:00"

    # 只在 audit/ 下落一个文件：不写 decisions/outcomes/reports 业务产物
    written = [
        path.relative_to(tmp_path).as_posix()
        for path in sorted((tmp_path / "artifacts").rglob("*"))
        if path.is_file()
    ]
    assert written == ["artifacts/alpha_v2/audit/baseline_manifest.json"]


def test_baseline_manifest_config_hash_tracks_alpha_v2_switch(
    monkeypatch: MonkeyPatch,
) -> None:
    """V2 开关属于配置身份的一部分：打开后 config_hash 必变（可审计）。"""
    config = _clean_default_config(monkeypatch)
    off = build_baseline_manifest(config, project_root=_ROOT)
    on = build_baseline_manifest(
        config.model_copy(update={"alpha_v2": AlphaV2Config(enabled=True)}),
        project_root=_ROOT,
    )
    assert off["config_hash"] != on["config_hash"]
    assert off["legacy"] == on["legacy"]


def test_alpha_v2_package_not_imported_by_legacy_entrypoints() -> None:
    """Legacy 入口模块不得 import Alpha V2 包（结构性零行为变化）。"""
    offenders: list[str] = []
    for relative in (
        "src/stock_analyzer/pipeline.py",
        "src/stock_analyzer/runtime/service.py",
        "src/stock_analyzer/main.py",
    ):
        path = _ROOT / relative
        if not path.exists():
            continue
        if "alpha_v2" in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    assert offenders == []


@pytest.mark.parametrize(
    "name", ["feishu_webhook", "api_token", "tushare_token", "secret_key"]
)
def test_sensitive_name_detector_matches_known_credentials(name: str) -> None:
    assert is_sensitive_config_name(name) is True


@pytest.mark.parametrize(
    "name", ["final_signal_min_threshold", "llm_max_tokens", "app_mode", "advisory_only"]
)
def test_sensitive_name_detector_ignores_business_fields(name: str) -> None:
    assert is_sensitive_config_name(name) is False
