# Quality Gate Playbook

## Goal

Use layered gates to keep new work clean even while the repository still has historical backlog in full `ruff`, full `mypy`, and full `pytest`.

## Stages

1. `quality-clean-scope`
   Run curated `ruff` and `mypy` checks on the scopes that are already maintainable and should stay clean.

2. `quality-smoke`
   Run fast, business-critical regression tests for release-sensitive modules.

3. `quality-integration`
   Run broader cross-module regressions for the currently hardened service flows.

4. `quality-slow-report`
   Run the historically expensive suites with `--durations=20` and write a profiling log to `artifacts/quality/pytest_slow_report.log`.

## Commands

```powershell
python scripts/run_quality_gate.py --stage clean-scope --fail-on-error
python scripts/run_quality_gate.py --stage smoke --fail-on-error
python scripts/run_quality_gate.py --stage integration --fail-on-error
python scripts/run_quality_gate.py --stage slow-report --fail-on-error
python scripts/run_quality_gate.py --stage all --fail-on-error
```

Equivalent `make` targets are available:

```powershell
make quality-clean-scope
make quality-smoke
make quality-integration
make quality-slow-report
make quality-gate
```

## Current Strategy

- Treat `clean-scope`, `smoke`, and `integration` as blocking gates.
- Treat `slow-report` as a profiling and governance tool first.
- Keep full-repository `ruff` and `mypy` backlog visible, but do not block releases on historical debt outside the curated clean scope.
