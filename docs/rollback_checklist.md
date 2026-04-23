# Rollback Checklist

## Trigger Conditions

1. Confirm the current release gate report is failing, or an explicit rollback decision has been approved.
2. Identify the last accepted native artifact path from acceptance history.
3. Verify the target artifact still reports `lightgbm` and `xgboost` backends.

## Snapshot Validation

1. Create a fresh release snapshot before any production cutover:
   `python scripts/run_release_snapshot.py create`
2. Record the returned `snapshot_dir` and `manifest_path`.
3. Validate the restore path without mutating files:
   `python scripts/run_release_snapshot.py restore --snapshot-dir <snapshot_dir> --dry-run`
4. Confirm the command returns `ok: true` and that `missing` is empty.

## Rollback Execution

1. Repoint runtime to the rollback artifact and reload predictor.
2. Execute the restore:
   `python scripts/run_release_snapshot.py restore --snapshot-dir <snapshot_dir>`
3. Keep the generated `restore_backup_*` directory until post-release observation is complete.

## Post-Restore Verification

1. Re-run `python -m stock_analyzer.cli v13-acceptance`.
2. Re-run `python -m stock_analyzer.cli acceptance-release-gate --closed-loop-smoke-passed`.
3. Confirm `artifacts/acceptance/release_gate_report.json` status is `pass`.
4. Record rollback reason, operator, artifact path, snapshot directory, backup directory, and timestamp in the deployment log.

## Validated Local Drill

Validated on `2026-03-14` with the following local commands and results:

1. `python scripts/run_release_snapshot.py create`
   Result: `ok: true`
   Snapshot: `artifacts/release/snapshots/20260314_165753`
2. `python scripts/run_release_snapshot.py restore --snapshot-dir artifacts/release/snapshots/20260314_165753 --dry-run`
   Result: `ok: true`, `missing: []`
3. `python scripts/run_week8_hardening_release_rehearsal.py`
   Report: `artifacts/evolution/rehearsal/week8_hardening_release_rehearsal_20260314_165934.json`
   Key result: confirm path and rollback path both accepted
