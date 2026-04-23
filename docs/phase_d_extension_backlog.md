# Phase D Research Extensions

Phase D has moved from a pure backlog into a delivered research-sidecar layer.
These capabilities remain `research_sidecar` only and do not replace the runtime
main path by default, but they are now callable through service, API, and CLI.

## Completed Research Sidecars

- `alphalens_sidecar`
- `shap_sidecar`
- `catboost_shadow`
- `finbert_sidecar`
- `qlib_bridge`
- `tabnet_ft_transformer`
- `tft_sidecar`
- `finrl_sidecar`
- `heavy_ts_shadow`

## Status And Registry Outputs

Generate the original Phase D status report:

```powershell
python -m stock_analyzer.cli phase-d-status
```

Generate the delivered D6 research registry:

```powershell
python -m stock_analyzer.cli phase-d6-registry
```

Default artifacts:

- `artifacts/acceptance/phase_d_status_report.json`
- `artifacts/acceptance/phase_d6_research_registry.json`

## CLI Entry Points

```powershell
python -m stock_analyzer.cli phase-d-alphalens
python -m stock_analyzer.cli phase-d-shap
python -m stock_analyzer.cli phase-d-catboost-shadow
python -m stock_analyzer.cli phase-d-finbert --records "[{\"symbol\":\"600000.SH\",\"headline\":\"Positive outlook\"}]"
python -m stock_analyzer.cli phase-d-qlib-bridge
python -m stock_analyzer.cli phase-d-tabular-deep
python -m stock_analyzer.cli phase-d-tft
python -m stock_analyzer.cli phase-d-finrl
python -m stock_analyzer.cli phase-d-heavy-ts
```

## API Entry Points

- `POST /research/alphalens/report`
- `POST /research/shap/report`
- `POST /research/catboost-shadow/report`
- `POST /research/finbert/report`
- `POST /research/qlib-bridge/report`
- `POST /research/tabular-deep/report`
- `POST /research/tft/report`
- `POST /research/finrl/report`
- `POST /research/heavy-ts/report`
- `GET /research/d6/registry`

## Purpose

- Keep advanced experiments outside the runtime takeover path.
- Provide a uniform export surface for validation and audit.
- Preserve a clean handoff path for future backend replacement.
