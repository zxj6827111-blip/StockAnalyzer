# V1.3 Deployment Guide

## Prerequisites

- Python environment can install project dependencies from `pyproject.toml`.
- Native model backends must be available: `lightgbm`, `xgboost`.
- Runtime artifact directory must be writable: `artifacts/acceptance/`.

## Install

```powershell
pip install -e .
python -c "import lightgbm, xgboost; print('native_backends_ok')"
```

## Generate Acceptance Artifacts

```powershell
python -m stock_analyzer.cli acceptance-bundle --symbol 600000 --lookback-days 320
```

Outputs:

- `artifacts/acceptance/baseline_report.json`
- `artifacts/acceptance/checkpoint_phase_a.json`
- `artifacts/acceptance/checkpoint_phase_b.json`
- `artifacts/acceptance/checkpoint_phase_c.json`
- `artifacts/acceptance/m9_failure_retention_report.json`
- `artifacts/acceptance/portfolio_execution_report.json`
- `artifacts/acceptance/v13_acceptance_report.json`

## Run Release Gate

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_acceptance_release_gate.ps1 -LookbackDays 320
```

Or run the last gate step directly after smoke tests:

```powershell
python -m stock_analyzer.cli acceptance-release-gate --closed-loop-smoke-passed --closed-loop-smoke-detail "pytest tests/test_service_closed_loop_flow.py" --fail-on-blocked
```

## Pass Criteria

- `baseline_type == native_baseline`
- `v13_acceptance_report.status == pass`
- `not_tested_count == 0`
- closed-loop smoke test passed

## NAS Support Bundle

If the runtime is deployed on NAS and later needs diagnosis or upgrade planning, export a support bundle first:

```powershell
python scripts/export_support_bundle.py
```

Default output:

- `artifacts/support/nas_support_bundle.json`

This bundle captures runtime HTTP snapshots, runtime state, tracked deployment files, container status, recent logs, and Redis realtime key activity. See `docs/nas_support_bundle.md` for the full workflow.
