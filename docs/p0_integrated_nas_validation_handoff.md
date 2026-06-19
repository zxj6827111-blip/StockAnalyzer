# P0 Integrated NAS Validation Handoff

## Current Branch

- Local repo: `E:\Software Development\StockAnalyzer`
- Final branch to validate on NAS: `codex/p0-pipeline-diagnostics-integrated`
- Minimum required commit included in this branch:
  `78a4831 docs(research): add NAS advisory validation handoff`
- Key code commit included in this branch:
  `4ad342f feat(research): link runtime portfolio outcomes`

## Local Evidence

Local audit output:

- `artifacts/research/p0_integrated_local_replay_20260619T144842+0800/p0_goal_completion_audit.md`
- `artifacts/research/p0_integrated_local_replay_20260619T144842+0800/analysis/p0_analysis_inputs_manifest.json`
- `artifacts/research/p0_integrated_local_replay_20260619T144842+0800/analysis/p0_shadow_experiment_plan_v1.json`

The local audit status is `needs_work` only because the NAS advisory runtime
probe has not been run from this local machine.

Local checks already passed:

- `research_inputs_complete`: `final_report_v3.json`,
  `p4_feature_family_ablation_v1.json`, and
  `p5_position/position_framework_analysis.json` are present.
- `cross_review_shadow_grid_covered`: the requested `3 x 3 x 3 x 4` threshold
  grid is present with 108 variants.
- `financial_data_quality_split_available`: financial penalties are split into
  confirmed, inferred, and ambiguous evidence.
- `position_framework_available`: position sizing, stop/take-profit, re-entry,
  and focus-symbol tracking are present.

Still required:

- `nas_advisory_probe_passed`: must be proven on NAS by a controlled
  advisory-only pipeline run.

## NAS Codex Prompt

Use this prompt in the NAS Codex session.

```text
请在 NAS 的 /vol1/docker/StockAnalyzer_repo 中验证分支 codex/p0-pipeline-diagnostics-integrated。
目标不是只检查代码是否更新，而是验证新代码在真实运行态是否真的生效。

硬性要求：
- 只允许 advisory_only 或明确 dry-run。
- 不真实下单。
- 不开启 auto_promotion。
- 不放松生产风控。
- 允许写入新的 runtime/audit 记录。

第一步，切换分支并确认 HEAD：

cd /vol1/docker/StockAnalyzer_repo
git fetch origin
git checkout -B codex/p0-pipeline-diagnostics-integrated origin/codex/p0-pipeline-diagnostics-integrated
git log --oneline -5

如果 HEAD 比下面两个提交更新也可以，但历史中必须包含：
78a4831 docs(research): add NAS advisory validation handoff

4ad342f feat(research): link runtime portfolio outcomes

第二步，运行受控 advisory-only probe。
如果服务端口不是 8000，请先替换 --api-base。

export PYTHONPATH=$(pwd)/src
python scripts/p0_run_nas_advisory_probe.py \
  --api-base http://127.0.0.1:8000 \
  --symbols 600000,000001,000159,001258,600956 \
  --strategy trend \
  --current-equity 1.0 \
  --runtime-state artifacts/runtime/runtime_state.json \
  --model-artifact artifacts/model_v1.json \
  --config config/default.yaml \
  --output-dir artifacts/research/p0_integrated_nas_probe_$(date +%Y%m%dT%H%M%S%z) \
  --confirm-run

第三步，检查 probe 输出目录中的文件：

- nas_advisory_validation_report.md
- nas_advisory_validation_report.json
- commands/*.json
- analysis/final_report_v3.json
- analysis/p4_feature_family_ablation_v1.json
- analysis/p5_position/position_framework_analysis.json
- p0_goal_completion_audit.md

第四步，逐项验证：

- /signals/latest 优先从真实 latest_signals 返回，而不是只从 week5 fallback 返回。
- runtime_state.latest_signals 已真正落盘。
- 最新 pipeline_run 是 advisory_only 或明确 dry-run。
- 最新 portfolio_update.executions 为空，或明确没有真实成交。
- 有 execution_attempts 或 advisory_attempts 字段。
- production_change_allowed=false。
- 没有开启 auto_promotion。
- 没有放松风控阈值。
- p0_goal_completion_audit.md 中 nas_advisory_probe_passed 为 PASS。

第五步，输出 Markdown 验证报告，至少包含：

- 当前分支与 HEAD。
- 是否真实下单，必须为否。
- runtime_state.latest_signals 是否存在。
- /signals/latest 的 storage/source 证据。
- 最新 pipeline_run 的 execution_mode、executions、execution_attempts/advisory_attempts。
- P0 research inputs 是否 complete。
- cross-review、model threshold、score、financial、liquidity、risk 当前最新卡点。
- outcome_linkage 是否有足够收益样本支持调参。
- 下一步是否可以进入 shadow 参数实验。
```
