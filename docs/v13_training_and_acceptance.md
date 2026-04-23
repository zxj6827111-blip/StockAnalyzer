# V1.3 Training And Acceptance

## Baseline

```powershell
python -m stock_analyzer.cli baseline-report --symbol 600000 --lookback-days 320
```

Expected:

- baseline artifact uses `lightgbm` and `xgboost`
- output path defaults to `artifacts/acceptance/baseline_report.json`

## Phase Checkpoints

```powershell
python -m stock_analyzer.cli phase-checkpoint --phase A
python -m stock_analyzer.cli phase-checkpoint --phase B
python -m stock_analyzer.cli phase-checkpoint --phase C
```

## V1.3 Acceptance

```powershell
python -m stock_analyzer.cli v13-acceptance
```

Key sections:

- `11.1_mainline_credibility`
- `11.2_data_utilization`
- `11.3_shortlist_quality`
- `11.4_runtime_quality`
- `11.5_portfolio_execution`

## Full Bundle

```powershell
python -m stock_analyzer.cli acceptance-bundle --symbol 600000 --lookback-days 320 --run-week5-scan
```

Use `--run-week5-scan` when you need a fresh Week5 evidence set instead of the last cached report.
