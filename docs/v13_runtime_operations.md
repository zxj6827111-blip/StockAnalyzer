# V1.3 Runtime Operations

## Runtime Status Fields

Check `provider_status()` or `artifacts/acceptance/v13_acceptance_report.json`.

Important fields:

- `predictor_mode`
- `reason`
- `degraded_reason_at`
- `status_timestamp`
- `lgbm_backend`
- `xgb_backend`

## Degraded Mode

Controlled degradation is acceptable only when:

- reason is visible
- timestamp is visible
- backend visibility is preserved

If degraded mode is active without a timestamp or reason, treat it as a blocking runtime defect.

## Portfolio Shadow And Execution Artifacts

- `artifacts/acceptance/portfolio_execution_report.json`
  - `staged_take_profit.average_return_delta`
  - `hrp_shadow.baseline_max_drawdown`
  - `hrp_shadow.shadow_max_drawdown`

## Rollback And Evolution

Check latest evolution outputs before rollback decisions:

- `latest_evolution_report()`
- `latest_evolution_release_gate()`
- `artifacts/acceptance/release_gate_report.json`

Rollback should prefer the last accepted native artifact and should not bypass the acceptance release gate.
Before deployment, create a snapshot with `python scripts/run_release_snapshot.py create`.
Before production restore, validate the manifest with `python scripts/run_release_snapshot.py restore --snapshot-dir <snapshot_dir> --dry-run`.
