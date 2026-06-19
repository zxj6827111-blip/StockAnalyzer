# P1 Shadow Calibration NAS Handoff

## Purpose

Validate branch `codex/p1-shadow-calibration-data-quality` on NAS in
`advisory_only` mode. This handoff is research-only. Do not place real orders,
do not enable auto promotion, and do not relax production risk controls.

Expected local branch and commit:

- Branch: `codex/p1-shadow-calibration-data-quality`
- Commit: latest `origin/codex/p1-shadow-calibration-data-quality` after
  `git fetch origin`
- Expected files include `docker-compose.advisory.yml`,
  `scripts/p1_nas_rebuild_and_collect.sh`,
  `scripts/p1_capture_nas_environment.py`,
  `scripts/p1_accept_nas_advisory_collection.py`,
  `scripts/p1_audit_goal_completion.py`,
  `scripts/p1_nas_shadow_validation.py`,
  `scripts/p1_run_nas_advisory_collection.py`, and the P1 research changes from
  `272e4eb`
- Current expected tip includes the NAS advisory compose override and the NAS
  rebuild-and-collection wrapper, or a newer commit on the same branch

## Required Safety Gates

Before running any pipeline or research command, verify:

- Git checkout is the real repo: `/vol1/docker/StockAnalyzer_repo`
- `HEAD` equals the latest fetched
  `origin/codex/p1-shadow-calibration-data-quality`
- Runtime mode is `advisory_only`
- `auto_promotion.enabled=false`
- Core production guardrails are unchanged
- No production config is edited
- No live order execution is enabled

If any safety check fails, stop and write a failed validation report.

Preferred NAS command:

```bash
cd /vol1/docker/StockAnalyzer_repo
bash scripts/p1_nas_rebuild_and_collect.sh
```

The wrapper fetches the latest branch, checks out
`origin/codex/p1-shadow-calibration-data-quality`, rebuilds `api` and
`scheduler` with the advisory override, waits for `/health` to prove
`advisory_only=true` and `training_enabled=false`, then starts the P1 collection.
If either health value is unsafe, the wrapper stops before any collection run.
After collection, it writes an environment snapshot, an acceptance report, and
a final goal-completion audit from the generated collection artifacts.

Optional overrides:

```bash
API_BASE=http://127.0.0.1:18001 \
OUTPUT_DIR=artifacts/research/p1_advisory_collection_$(date +%Y%m%dT%H%M%S%z) \
RUNS=6 \
INTERVAL_SEC=1800 \
bash scripts/p1_nas_rebuild_and_collect.sh
```

Manual fallback: when rebuilding containers for this research branch, include
the advisory override so Docker does not fall back to `config/default.yaml`
defaults (`advisory_only=false`, `training.enabled=true`):

```bash
docker compose -f docker-compose.yml -f docker-compose.advisory.yml up -d --build api scheduler
```

If the NAS uses legacy Compose, run:

```bash
docker-compose -f docker-compose.yml -f docker-compose.advisory.yml up -d --build api scheduler
```

After rebuild, `/health` must report:

- `runtime.advisory_only=true`
- `runtime.training_enabled=false`

Do not run the collection if either value is wrong.

## Required Runtime Probe

Run one controlled advisory-only pipeline probe using the same container/service
entrypoint used for the previous P0 NAS advisory validation.

After the probe, verify:

- Latest `pipeline_run.execution_mode` is `advisory_only`
- Latest `portfolio_update.executions` exists and is empty
- Latest `portfolio_update.status` is advisory-only / skipped advisory-only
- `execution_attempts` is empty or absent for real execution
- `advisory_attempts` is present
- `/signals/latest` uses `latest_signals` from the controlled `pipeline_run`
- `/signals/latest` is not `empty` and not only `week5_latest_candidates`

## Required Research Artifacts

Generate P0/P1 research artifacts into:

`artifacts/research/p1_shadow_calibration_nas_<timestamp>/`

Required files:

- `nas_validation_report.md`
- `nas_validation_report.json`
- `analysis/final_report_v3.json`
- `analysis/p4_feature_family_ablation_v1.json`
- `analysis/p5_position/position_framework_analysis.json`
- `analysis/p0_shadow_experiment_plan_v1.json`
- `commands/pipeline_advisory.json`
- `commands/signals_latest_after.json`
- `commands/signal_quality_after.json`
- `commands/config_safety_snapshot.json`

After the controlled probe creates the artifact directory, run:

```bash
python scripts/p1_nas_shadow_validation.py --probe-dir <artifact_dir>
```

This command writes the final `nas_validation_report.md` and
`nas_validation_report.json` from the P1 checks.

## Continuous Advisory Collection

Use the collection runner for repeated advisory-only evidence gathering. First
write a dry plan; this must not call the API or trigger a pipeline run:

```bash
python scripts/p1_run_nas_advisory_collection.py \
  --output-dir artifacts/research/p1_advisory_collection_test \
  --runs 2
```

Then run repeated advisory-only probes. From inside the API container, use
`http://127.0.0.1:8000`; from the NAS host, use `http://127.0.0.1:18001`.
The runner also checks `/health` before starting any run and writes
`status=safety_check_failed` if the runtime is unsafe.

```bash
python scripts/p1_run_nas_advisory_collection.py \
  --api-base http://127.0.0.1:8000 \
  --output-dir artifacts/research/p1_advisory_collection_$(date +%Y%m%dT%H%M%S%z) \
  --symbols 600000,000001 \
  --runs 6 \
  --interval-sec 1800 \
  --confirm-run
```

The collection runner writes per-run reports under `run_001`, `run_002`, etc.,
and writes:

- `p1_nas_environment.json`
- `p1_advisory_collection_report.md`
- `p1_advisory_collection_report.json`
- `p1_advisory_collection_acceptance.md`
- `p1_advisory_collection_acceptance.json`
- `p1_goal_completion_audit.md`
- `p1_goal_completion_audit.json`

The collection report must show:

- `production_change_allowed=false`
- `failed_runs=0` for evidence used as pass
- no `safety_failure` block for evidence used as pass
- `financial_raw_fields_observed_runs > 0`
- `roe_present_rows > 0`
- `debt_ratio_present_rows > 0`
- `financial_source_present_rows > 0`
- `financial_report_date_present_rows > 0`
- `max_candidate_variant_count > 0`
- `max_mature_return_samples`
- No recommendation to change production thresholds before 100 mature samples

The acceptance report must show:

- `status=pass`
- `collection_status=pass`
- `minimum_completed_runs` passed
- `no_safety_failure` passed
- `financial_raw_fields_observed` passed
- `mature_samples_not_enough_for_production_threshold_change` passed

The goal-completion audit must show:

- `status=complete`
- `nas_environment_safe_and_current` passed
- `collection_report_passed` passed
- `acceptance_report_passed` passed
- `continuous_advisory_runs_completed` passed
- `no_real_trading_or_promotion` passed
- `latest_signals_from_controlled_pipeline` passed
- `p1_shadow_and_financial_evidence_present` passed
- `profitability_threshold_change_not_justified` passed

## P1 Report Checks

In `analysis/final_report_v3.json`, verify:

- `production_change_allowed=false`
- `p1_probability_scale_shadow_grid` exists
- `p1_probability_scale_shadow_grid.production_change_allowed=false`
- `p1_probability_scale_shadow_grid.grid.xgb_min` includes values near
  `0.18, 0.20, 0.23, 0.25, 0.27`
- `p1_probability_scale_shadow_grid.grid.meta_min` includes values near
  `0.20, 0.22, 0.24, 0.26, 0.27`
- `p1_probability_scale_shadow_grid.grid.score_min` includes values near
  `18, 20, 22, 24, 25`
- `candidate_variant_count`, `max_pass_count`, and
  `top_candidate_generating_variants` are present
- `guardrails.do_not_relax_production_cross_review=true`
- `outcome_linkage.can_claim_profitability=false` unless at least 100 mature
  return samples are linked

In `analysis/p4_feature_family_ablation_v1.json`, verify:

- `financial_data_quality.raw_field_coverage` exists
- It reports `roe_present_rows`, `debt_ratio_present_rows`,
  `both_gate_fields_present_rows`, `financial_source_present_rows`,
  `financial_report_date_present_rows`, and `financial_missing_fields_present_rows`
- `same_period_confirmed` is `unknown` unless same-period evidence is explicit
- `same_source_confirmed` is `unknown` unless same-source evidence is explicit
- `financial_data_complete` is treated as `gate_required_fields_present_only`,
  not as proof of a full same-period financial statement

In `analysis/p5_position/position_framework_analysis.json`, verify:

- `reentry_cooldown_shadow` exists
- `reentry_cooldown_shadow.production_change_allowed=false`
- Focus symbols include `000159`, `001258`, and `600956`
- Variants include stop-loss re-entry cooldown and trailing take-profit shadow
- Guardrails include `do_not_write_week6_controls=true`
- No `week6_controls`, `position_multiplier`, stop-loss config, or take-profit
  config is changed by this report

In `analysis/p0_shadow_experiment_plan_v1.json`, verify:

- `status=research_only`
- `production_change_allowed=false`
- `threshold_assessment.p1_probability_scale_shadow_grid` exists
- `feature_family_plan.financial_raw_field_coverage` exists
- `position_plan.reentry_cooldown_shadow` exists
- Recommended experiments still require shadow/advisory validation before any
  production threshold change

## Final NAS Report Must State

The final `nas_validation_report.md` must explicitly answer:

- Was any real order placed?
- Was `auto_promotion` enabled?
- Were production risk controls relaxed?
- Did `/signals/latest` use the controlled latest pipeline signals?
- Did the P1 probability-scale shadow grid generate candidates?
- Are mature return samples sufficient to rank by profitability?
- Can the current evidence justify production threshold changes?
- Can financial penalties distinguish true low ROE from missing/default/stale
  source evidence?
- What did the focus-symbol re-entry/stop-loss/trailing shadow show for
  `000159`, `001258`, and `600956`?

If mature return samples are fewer than 50, the report must recommend continuing
`advisory_only` collection. If mature return samples are fewer than 100, the
report must not recommend production threshold changes.
