"""Alpha V2 基线身份清单（蓝图 §5 P0-00）。

产出 ``artifacts/alpha_v2/audit/baseline_manifest.json``：记录"改造开始前的
Legacy 行为面 + Alpha V2 开关状态 + 代码/配置身份"，供后续所有 P0/P1 任务做
before/after 对照。

三条硬约束：

1. **不写任何凭据**：清单内容取自显式白名单字段（业务阈值、目标、模式），
   不含 token/webhook/password；配置指纹走
   :func:`stock_analyzer.config_identity.redacted_config_hash`（先脱敏再哈希）。
2. **不在 Legacy 路径里自动触发**：只由显式调用（脚本 / 测试）产生，因此
   ``alpha_v2.enabled=false`` 时不会有隐式副作用——本函数恰恰是"关闭状态下
   也要能生成基线"的工具。
3. **不改变任何业务值**：本模块只读配置，不写回、不迁移、不修参数。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import AlphaV2ArtifactLayout, write_json_atomic
from stock_analyzer.build_identity import generated_at_utc, get_build_manifest
from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.config_identity import redacted_config_hash

BASELINE_MANIFEST_SCHEMA = "alpha_v2_baseline_manifest.v1"
BASELINE_MANIFEST_FILENAME = "baseline_manifest.json"

# Legacy 行为面白名单：这些值构成"Alpha V2 关闭时不该被碰到"的基线。
# 新增字段只加到这里，不要让 manifest 变成整份配置的拷贝（那才会带来
# 凭据泄漏与噪音）。风险门（breadth/overextension/board_risk）一并冻结，
# 对应 P0-00 验收项"不改变生产风险门"。


def behavior_surface_snapshot(config: StockAnalyzerConfig) -> dict[str, object]:
    """Legacy 行为面快照（业务阈值 / 目标 / 模式 / 模型与训练口径）。"""
    week5 = config.week5
    cross_review = config.models.cross_review
    overextension = config.overextension
    board_risk = config.board_risk
    return {
        "legacy": {
            "week5_enabled": week5.enabled,
            "final_signal_min_threshold": week5.final_signal_min_threshold,
            "final_signal_cap": week5.final_signal_cap,
            "allow_zero_signal": week5.allow_zero_signal,
            "night_quality_target": week5.night_quality_target,
            "night_light_candidate_target": week5.night_light_candidate_target,
            "night_deep_candidate_target": week5.night_deep_candidate_target,
            "light_candidate_target": week5.light_candidate_target,
            "deep_candidate_target": week5.deep_candidate_target,
            "universe_quality_target_size": week5.universe_quality_target_size,
            "cross_review": {
                "p_lgbm_min": cross_review.p_lgbm_min,
                "p_xgb_min": cross_review.p_xgb_min,
                "p_meta_min": cross_review.p_meta_min,
                "max_diff": cross_review.max_diff,
                "dynamic_enabled": cross_review.dynamic_enabled,
            },
            "risk_gates": {
                "market_breadth_enabled": week5.market_breadth_enabled,
                "market_breadth_disable_if_below": week5.market_breadth_disable_if_below,
                "overextension_bias_reject_min": overextension.bias_reject_min,
                "overextension_atr_distance_reject": overextension.atr_distance_reject,
                "board_risk_consecutive_limit_up_reject": (
                    board_risk.consecutive_limit_up_reject
                ),
            },
        },
        "training": {
            "enabled": config.training.enabled,
            "artifact_path": config.training.artifact_path,
            "model_archive_dir": config.training.model_archive_dir,
            "embargo_days": config.training.embargo_days,
        },
        "models": {
            "inference_score_source": config.models.inference_score_source,
            "calibration": config.models.calibration,
        },
        "runtime": {
            "app_mode": config.app.mode,
            "advisory_only": config.app.advisory_only,
            "theme_mode": config.theme.mode,
            "news_risk_mode": config.evolution.news_risk_mode,
            "auto_promotion_enabled": config.auto_promotion.enabled,
            "auto_load_predictor": config.auto_promotion.auto_load_predictor,
        },
    }


def resolve_code_identity(project_root: str | Path | None = None) -> dict[str, object]:
    """代码身份：优先复用构建清单（容器内无 .git 也可用），退回 git 命令。"""
    build_manifest = get_build_manifest()
    commit = str(build_manifest.get("commit", "")).strip()
    source = str(build_manifest.get("source", "")).strip()
    if commit in {"", "unknown"}:
        git_commit = _git(["rev-parse", "HEAD"], project_root)
        if git_commit:
            commit = git_commit
            source = "git"
    return {
        "code_commit": commit or "unknown",
        "code_commit_source": source or "unknown",
        "code_dirty": build_manifest.get("dirty", "unknown"),
        "git_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], project_root) or "unknown",
    }


def build_baseline_manifest(
    config: StockAnalyzerConfig,
    *,
    project_root: str | Path | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    """构造基线清单（纯计算，不落盘；落盘见 :func:`write_baseline_manifest`）。"""
    layout = AlphaV2ArtifactLayout.from_config(config.alpha_v2, project_root=project_root)
    manifest: dict[str, object] = {
        "schema": BASELINE_MANIFEST_SCHEMA,
        "generated_at": generated_at or generated_at_utc(),
        "config_hash": redacted_config_hash(config),
        # 指纹口径必须写清楚：本清单记的是**生成环境的有效配置**（含 SA__ 环境覆盖
        # 与 local override），与"受跟踪默认值"的指纹不是同一个值——跨环境比对时
        # 不区分口径会把环境差异误读成配置漂移。
        "config_hash_scope": "effective_config_with_env_overrides",
        "alpha_v2": {
            "enabled": config.alpha_v2.enabled,
            "shadow_only": config.alpha_v2.shadow_only,
            "enforce_final_selection": config.alpha_v2.enforce_final_selection,
            "artifact_root": layout.configured_root,
            "artifact_root_resolved": str(layout.root),
            "selection_contract": config.alpha_v2.selection_contract,
            "model_resolver_mode": config.alpha_v2.model_resolver_mode,
            "entry_mode": config.alpha_v2.entry_mode,
            "primary_horizon_days": config.alpha_v2.primary_horizon_days,
            "candidate_output_top_k": config.alpha_v2.candidate_output_top_k,
        },
    }
    manifest.update(resolve_code_identity(project_root))
    manifest.update(behavior_surface_snapshot(config))
    return manifest


def write_baseline_manifest(
    config: StockAnalyzerConfig,
    *,
    project_root: str | Path | None = None,
    output_path: str | Path | None = None,
    generated_at: str | None = None,
) -> Path:
    """幂等生成基线清单并返回落盘路径。"""
    layout = AlphaV2ArtifactLayout.from_config(config.alpha_v2, project_root=project_root)
    layout.ensure()
    target = (
        Path(output_path)
        if output_path is not None
        else layout.audit / BASELINE_MANIFEST_FILENAME
    )
    manifest = build_baseline_manifest(
        config, project_root=project_root, generated_at=generated_at
    )
    return write_json_atomic(target, manifest)


def _git(args: list[str], project_root: str | Path | None) -> str:
    """尽力而为地取 git 信息：任何失败（无 git / 非仓库 / 超时）都返回空串。"""
    cwd = Path(project_root) if project_root is not None else Path.cwd()
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    value = completed.stdout.strip()
    # detached HEAD 下 --abbrev-ref 会返回字面量 "HEAD"，不是分支名。
    return "" if value in {"", "HEAD"} else value
