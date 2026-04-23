# Pre Release Checklist

1. Run `python scripts/run_release_preflight.py --fail-on-not-ready`.
2. Run `python scripts/run_release_smoke.py --fail-on-failure`.
3. Run `python scripts/run_staging_rehearsal.py --fail-on-blocked`.
4. Run `python scripts/run_release_snapshot.py create` and record the generated snapshot directory.
5. Run `python scripts/run_release_snapshot.py restore --snapshot-dir <snapshot_dir> --dry-run` and confirm `ok: true`.
6. Run `pytest -q -p no:cacheprovider tests/test_service_closed_loop_flow.py`.
7. Run `python -m stock_analyzer.cli acceptance-bundle --symbol 600000 --lookback-days 320`.
8. Check `artifacts/acceptance/v13_acceptance_report.json`.
9. Run `python -m stock_analyzer.cli acceptance-release-gate --closed-loop-smoke-passed --closed-loop-smoke-detail "pytest tests/test_service_closed_loop_flow.py" --fail-on-blocked`.
10. Confirm `artifacts/acceptance/release_gate_report.json` status is `pass`.
