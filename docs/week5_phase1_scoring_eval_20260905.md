# Week5 Phase 1 时间安全打分评估（2026-06-24 ~ 2026-08-28）

> 生成：2026-09-05T09:28:57.714621+00:00；embargo=11 交易日（horizon+settlement）

- **evaluation_validity**: valid
- **model_id**: multi
- **models_used**: {'phase1_eval_dataset_manifest_v1_3dddbc7034ef': 1, 'phase1_eval_dataset_manifest_v1_af1a30ff4887': 34}
- **eval_days_total**: 48
- **eval_days_valid**: 35
- **eval_days_invalid**: 13
- **embargo_trading_days**: 11
- **pooled_auc**: 0.6237691930662318
- **pooled_brier**: 0.2763434858145941
- **pooled_n_labeled**: 1820
- **pooled_mean_realized_return**: -0.021384314285714225
- **daily_ic_mean**: 0.23919360126975073
- **daily_ic_ci95**: [0.1588887167897392, 0.31559648932846696]
- **mean_quantile_returns**: [-0.0457608528232513, -0.057800550457346014, -0.01143072807307094, 0.010661313980785297, 0.017858622433166247]
- **note**: label=realized_return sign（诊断口径）；IC/分位收益用 realized_return 连续值

## 逐日记录

| eval_date | model | n_scored | n_labeled | ic_spearman | top-bottom | coverage | lookahead |
|---|---|---|---|---|---|---|---|
| 2026-06-24 | phase1_eval_dataset_manifest_v1_ | 158 | 106 | 0.1628 | 0.0326 | 0.0306 | N |
| 2026-06-25 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-06-26 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-06-29 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-06-30 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-07-01 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-07-02 | phase1_eval_dataset_manifest_v1_ | 120 | 71 | 0.2440 | 0.0909 | 0.0232 | N |
| 2026-07-03 | phase1_eval_dataset_manifest_v1_ | 140 | 80 | 0.3571 | 0.0612 | 0.0271 | N |
| 2026-07-06 | phase1_eval_dataset_manifest_v1_ | 159 | 100 | 0.3579 | 0.0746 | 0.0308 | N |
| 2026-07-07 | phase1_eval_dataset_manifest_v1_ | 159 | 102 | 0.4039 | 0.1554 | 0.0308 | N |
| 2026-07-08 | phase1_eval_dataset_manifest_v1_ | 159 | 101 | 0.3046 | 0.0709 | 0.0308 | N |
| 2026-07-09 | phase1_eval_dataset_manifest_v1_ | 159 | 102 | 0.3515 | 0.1123 | 0.0308 | N |
| 2026-07-10 | phase1_eval_dataset_manifest_v1_ | 159 | 104 | 0.3650 | 0.1120 | 0.0308 | N |
| 2026-07-13 | phase1_eval_dataset_manifest_v1_ | 159 | 97 | 0.5307 | 0.2045 | 0.0308 | N |
| 2026-07-14 | phase1_eval_dataset_manifest_v1_ | 159 | 96 | 0.4064 | 0.0633 | 0.0308 | N |
| 2026-07-15 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | 0.4061 | 0.1286 | 0.0308 | N |
| 2026-07-16 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | 0.3860 | 0.1155 | 0.0308 | N |
| 2026-07-17 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | 0.2221 | 0.0359 | 0.0308 | N |
| 2026-07-20 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | 0.2398 | 0.0537 | 0.0308 | N |
| 2026-07-21 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | 0.0943 | 0.0308 | 0.0308 | N |
| 2026-07-22 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | 0.0409 | 0.0071 | 0.0308 | N |
| 2026-07-23 | phase1_eval_dataset_manifest_v1_ | 120 | 53 | -0.0756 | -0.0099 | 0.0232 | N |
| 2026-07-24 | phase1_eval_dataset_manifest_v1_ | 159 | 91 | -0.0726 | -0.0190 | 0.0308 | N |
| 2026-07-27 | phase1_eval_dataset_manifest_v1_ | 36 | 31 | 0.1712 | 0.0268 | 0.0070 | N |
| 2026-07-28 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-07-29 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-07-30 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-07-31 | phase1_eval_dataset_manifest_v1_ | 166 | 140 | -0.1122 | -0.0747 | 0.0302 | N |
| 2026-08-03 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-08-04 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-08-05 | phase1_eval_dataset_manifest_v1_ | 61 | 0 | NaN | NaN | 0.0110 | N |
| 2026-08-06 | phase1_eval_dataset_manifest_v1_ | 59 | 0 | NaN | NaN | 0.0107 | N |
| 2026-08-07 | phase1_eval_dataset_manifest_v1_ | 120 | 0 | NaN | NaN | 0.0217 | N |
| 2026-08-10 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-08-11 | phase1_eval_dataset_manifest_v1_ | 107 | 0 | NaN | NaN | 0.0193 | N |
| 2026-08-12 | phase1_eval_dataset_manifest_v1_ | 87 | 0 | NaN | NaN | 0.0157 | N |
| 2026-08-13 | phase1_eval_dataset_manifest_v1_ | 33 | 0 | NaN | NaN | 0.0060 | N |
| 2026-08-14 | phase1_eval_dataset_manifest_v1_ | 49 | 0 | NaN | NaN | 0.0088 | N |
| 2026-08-17 | phase1_eval_dataset_manifest_v1_ | 32 | 0 | NaN | NaN | 0.0058 | N |
| 2026-08-18 | phase1_eval_dataset_manifest_v1_ | 56 | 0 | NaN | NaN | 0.0101 | N |
| 2026-08-19 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-08-20 | phase1_eval_dataset_manifest_v1_ | 37 | 0 | NaN | NaN | 0.0067 | N |
| 2026-08-21 | phase1_eval_dataset_manifest_v1_ | None | None | None | None | None | N |
| 2026-08-24 | phase1_eval_dataset_manifest_v1_ | 59 | 0 | NaN | NaN | 0.0106 | N |
| 2026-08-25 | phase1_eval_dataset_manifest_v1_ | 40 | 0 | NaN | NaN | 0.0072 | N |
| 2026-08-26 | phase1_eval_dataset_manifest_v1_ | 39 | 0 | NaN | NaN | 0.0070 | N |
| 2026-08-27 | phase1_eval_dataset_manifest_v1_ | 69 | 0 | NaN | NaN | 0.0124 | N |
| 2026-08-28 | phase1_eval_dataset_manifest_v1_ | 7 | 0 | NaN | NaN | 0.0013 | N |

