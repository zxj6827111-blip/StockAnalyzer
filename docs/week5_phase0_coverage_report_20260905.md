# Week5 Phase 0 B12 数据覆盖报告（2026-09-05 NAS 实库）

> 窗口口径：方案 §2.4 Phase 2 评估窗口 `2026-03-01 ~ 2026-08-28`（交易日历另附 8/29~9/4 新鲜度核验）。
> 数据源：`/app/artifacts/warehouse/market.duckdb`（daily_bars）、`/app/artifacts/training/learning_protocol.duckdb`（signal_snapshots / outcome_records）。
> 执行环境：api 容器内只读连接，memory_limit=2GB / threads=2。

## 1. 交易日历校验

- 窗口内出现数据的自然日数：125
- 每日行数分布：min=2354, p25=5167, median=5172, p75=5181, max=5546
- 每日 distinct symbol 分布：min=2354, median=5172, max=5546
- 交易日数（工作日有数据）：125
- 节假日/缺失工作日（工作日 0 行）：5 天 → 2026-04-06, 2026-05-01, 2026-05-04, 2026-05-05, 2026-06-19
- 严重低覆盖日（行数 < median 25%）：0 天 → 无
- 低覆盖日（行数 < median 60%）：1 天 → 2026-07-30(2354行)
- 周末异常有数据：0 → 无

### 1.1 新鲜度核验（窗口之后）
- 2026-08-31（周一）：5543 行 / 5543 只
- 2026-09-01（周二）：5542 行 / 5542 只
- 2026-09-02（周三）：5542 行 / 5542 只
- 2026-09-03（周四）：5543 行 / 5543 只
- 2026-09-04（周五）：5541 行 / 5541 只
- 全库范围：2016-01-04 ~ 2026-09-04，10,441,524 行 / 5,571 只

## 2. symbol-date coverage

- 窗口内出现 symbol 数：5,568
- 每 symbol 交易日数分布（窗口交易日 125 天）：min=5, p25=124, median=124, p75=125, max=125
- 交易天数 < 50 的 symbol：384 只
- 交易天数 < 90% 交易日的 symbol：401 只
- 总 symbol-date 行数：651,225（日均 5,210 行）

### 2.1 价格字段完整性（窗口内）
- 总行数 651,225；NULL 行数 open=0, high=0, low=0, close=0

### 2.2 训练样本 universe vs 市场 universe 重叠
- signal_snapshots distinct symbol：1,577
- market 窗口 distinct symbol：5,568
- 重叠：1,576（训练样本覆盖市场窗口 28.3%）
- 仅存在于训练样本（市场窗口无日线）：1

## 3. feature coverage（signal_snapshots.feature_vector_json）

快照总量（按 schema_version）：
- 1: 90,316
- 合计：90,316
- feature_vector_json 非法 JSON 行数：0

### 3.1 特征键覆盖（union of json_keys，逐键统计，按 schema_version）
| schema_version | feature_key | present | presence% | non_null% |
|---|---|---|---|---|
| 1 | adl_norm | 90,316 | 100.0 | 100.0 |
| 1 | adl_slope5 | 90,316 | 100.0 | 100.0 |
| 1 | amplitude_rank20 | 90,316 | 100.0 | 100.0 |
| 1 | atr14 | 90,316 | 100.0 | 100.0 |
| 1 | atr20 | 90,316 | 100.0 | 100.0 |
| 1 | atr5 | 90,316 | 100.0 | 100.0 |
| 1 | atr_ratio | 90,316 | 100.0 | 100.0 |
| 1 | background_completion_score | 90,316 | 100.0 | 100.0 |
| 1 | bg_block_trade_net10 | 90,316 | 100.0 | 100.0 |
| 1 | bg_board_code | 90,316 | 100.0 | 100.0 |
| 1 | bg_debt_ratio | 90,316 | 100.0 | 100.0 |
| 1 | bg_debt_ratio_rank60 | 90,316 | 100.0 | 100.0 |
| 1 | bg_dragon_tiger_freq20 | 90,316 | 100.0 | 100.0 |
| 1 | bg_financial_quality | 90,316 | 100.0 | 100.0 |
| 1 | bg_holder_reduction20 | 90,316 | 100.0 | 100.0 |
| 1 | bg_is_delisting_risk | 90,316 | 100.0 | 100.0 |
| 1 | bg_is_st | 90,316 | 100.0 | 100.0 |
| 1 | bg_margin_trend20 | 90,316 | 100.0 | 100.0 |
| 1 | bg_northbound_net5 | 90,316 | 100.0 | 100.0 |
| 1 | bg_roe | 90,316 | 100.0 | 100.0 |
| 1 | bg_roe_rank60 | 90,316 | 100.0 | 100.0 |
| 1 | block_trade_amount_20 | 90,316 | 100.0 | 3.3 |
| 1 | block_trade_amount_5 | 90,316 | 100.0 | 3.3 |
| 1 | block_trade_direction_10 | 90,316 | 100.0 | 100.0 |
| 1 | block_trade_event_freq_20 | 90,316 | 100.0 | 3.3 |
| 1 | block_trade_frequency_20 | 90,316 | 100.0 | 100.0 |
| 1 | block_trade_net_20 | 90,316 | 100.0 | 100.0 |
| 1 | block_trade_net_5 | 90,316 | 100.0 | 100.0 |
| 1 | block_trade_premium_mean_20 | 90,316 | 100.0 | 3.3 |
| 1 | block_trade_turnover_ratio_20 | 90,316 | 100.0 | 3.3 |
| 1 | block_trade_volume_20 | 90,316 | 100.0 | 3.3 |
| 1 | body_pct | 90,316 | 100.0 | 100.0 |
| 1 | boll_mid20 | 90,316 | 100.0 | 100.0 |
| 1 | boll_pos20 | 90,316 | 100.0 | 100.0 |
| 1 | boll_width20 | 90,316 | 100.0 | 100.0 |
| 1 | cci14 | 90,316 | 100.0 | 100.0 |
| 1 | close_rank20 | 90,316 | 100.0 | 100.0 |
| 1 | close_t1 | 90,316 | 100.0 | 100.0 |
| 1 | debt_ratio_stability_60 | 90,316 | 100.0 | 100.0 |
| 1 | distance_high20 | 90,316 | 100.0 | 100.0 |
| 1 | distance_low20 | 90,316 | 100.0 | 100.0 |
| 1 | downside_vol20 | 90,316 | 100.0 | 100.0 |
| 1 | drawdown_20 | 90,316 | 100.0 | 100.0 |
| 1 | ema10 | 90,316 | 100.0 | 100.0 |
| 1 | ema13 | 90,316 | 100.0 | 100.0 |
| 1 | ema20 | 90,316 | 100.0 | 100.0 |
| 1 | ema3 | 90,316 | 100.0 | 100.0 |
| 1 | ema30 | 90,316 | 100.0 | 100.0 |
| 1 | ema5 | 90,316 | 100.0 | 100.0 |
| 1 | ema60 | 90,316 | 100.0 | 100.0 |
| 1 | ema8 | 90,316 | 100.0 | 100.0 |
| 1 | ema_gap_12_26 | 90,316 | 100.0 | 100.0 |
| 1 | excess_ret_20 | 90,316 | 100.0 | 3.4 |
| 1 | excess_ret_5 | 90,316 | 100.0 | 3.4 |
| 1 | excess_ret_60 | 90,316 | 100.0 | 3.4 |
| 1 | excess_vol_20 | 90,316 | 100.0 | 3.4 |
| 1 | excess_vol_60 | 90,316 | 100.0 | 3.4 |
| 1 | financing_balance_chg_20 | 90,316 | 100.0 | 100.0 |
| 1 | financing_balance_chg_5 | 90,316 | 100.0 | 100.0 |
| 1 | financing_balance_chg_60 | 90,316 | 100.0 | 100.0 |
| 1 | financing_balance_trend_5v20 | 90,316 | 100.0 | 100.0 |
| 1 | financing_balance_zscore_60 | 90,316 | 100.0 | 100.0 |
| 1 | hk_hold_change_20 | 90,316 | 100.0 | 3.3 |
| 1 | hk_hold_change_5 | 90,316 | 100.0 | 3.3 |
| 1 | hk_hold_ratio_chg_20 | 90,316 | 100.0 | 3.3 |
| 1 | hk_hold_ratio_chg_5 | 90,316 | 100.0 | 3.3 |
| 1 | hl_range_pct | 90,316 | 100.0 | 100.0 |
| 1 | holder_count_chg_20 | 90,316 | 100.0 | 100.0 |
| 1 | holder_count_chg_5 | 90,316 | 100.0 | 100.0 |
| 1 | holder_count_chg_60 | 90,316 | 100.0 | 100.0 |
| 1 | holder_count_decrease_streak | 90,316 | 100.0 | 100.0 |
| 1 | holder_count_zscore_60 | 90,316 | 100.0 | 100.0 |
| 1 | i1m_above_vwap_ratio | 90,316 | 100.0 | 60.7 |
| 1 | i1m_am_pm_diff | 90,316 | 100.0 | 89.0 |
| 1 | i1m_am_pm_reversal_strength | 90,316 | 100.0 | 60.7 |
| 1 | i1m_am_return | 90,316 | 100.0 | 89.0 |
| 1 | i1m_close_position | 90,316 | 100.0 | 89.0 |
| 1 | i1m_close_vwap_stability | 90,316 | 100.0 | 60.7 |
| 1 | i1m_intraday_pullback_ratio | 90,316 | 100.0 | 60.7 |
| 1 | i1m_last30_return | 90,316 | 100.0 | 89.0 |
| 1 | i1m_last30_volume_share | 90,316 | 100.0 | 89.0 |
| 1 | i1m_minute_count | 90,316 | 100.0 | 89.0 |
| 1 | i1m_morning30_volume_share | 90,316 | 100.0 | 60.7 |
| 1 | i1m_pm_return | 90,316 | 100.0 | 89.0 |
| 1 | i1m_positive_bar_ratio | 90,316 | 100.0 | 89.0 |
| 1 | i1m_price_efficiency | 90,316 | 100.0 | 60.7 |
| 1 | i1m_realized_vol | 90,316 | 100.0 | 89.0 |
| 1 | i1m_session_range_pct | 90,316 | 100.0 | 89.0 |
| 1 | i1m_session_return | 90,316 | 100.0 | 89.0 |
| 1 | i1m_tail30_volume_share | 90,316 | 100.0 | 60.7 |
| 1 | i1m_tail_volatility_ratio | 90,316 | 100.0 | 60.7 |
| 1 | i1m_vwap_gap | 90,316 | 100.0 | 89.0 |
| 1 | i5m_above_vwap_ratio | 90,316 | 100.0 | 61.4 |
| 1 | i5m_am_pm_diff | 90,316 | 100.0 | 88.7 |
| 1 | i5m_am_pm_reversal_strength | 90,316 | 100.0 | 61.4 |
| 1 | i5m_am_return | 90,316 | 100.0 | 88.7 |
| 1 | i5m_close_position | 90,316 | 100.0 | 88.7 |
| 1 | i5m_close_vwap_stability | 90,316 | 100.0 | 61.4 |
| 1 | i5m_intraday_pullback_ratio | 90,316 | 100.0 | 61.4 |
| 1 | i5m_last30_return | 90,316 | 100.0 | 88.7 |
| 1 | i5m_last30_volume_share | 90,316 | 100.0 | 88.7 |
| 1 | i5m_minute_count | 90,316 | 100.0 | 88.7 |
| 1 | i5m_morning30_volume_share | 90,316 | 100.0 | 61.4 |
| 1 | i5m_pm_return | 90,316 | 100.0 | 88.7 |
| 1 | i5m_positive_bar_ratio | 90,316 | 100.0 | 88.7 |
| 1 | i5m_price_efficiency | 90,316 | 100.0 | 61.4 |
| 1 | i5m_realized_vol | 90,316 | 100.0 | 88.7 |
| 1 | i5m_session_range_pct | 90,316 | 100.0 | 88.7 |
| 1 | i5m_session_return | 90,316 | 100.0 | 88.7 |
| 1 | i5m_tail30_volume_share | 90,316 | 100.0 | 61.4 |
| 1 | i5m_tail_volatility_ratio | 90,316 | 100.0 | 61.4 |
| 1 | i5m_vwap_gap | 90,316 | 100.0 | 88.7 |
| 1 | inst_net_amount_20 | 90,316 | 100.0 | 3.3 |
| 1 | inst_net_amount_5 | 90,316 | 100.0 | 3.3 |
| 1 | intraday_ret | 90,316 | 100.0 | 100.0 |
| 1 | log_ret_1d | 90,316 | 100.0 | 100.0 |
| 1 | log_ret_5d | 90,316 | 100.0 | 100.0 |
| 1 | lower_shadow_pct | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_applied | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_bucket_medium | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_bucket_mild | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_bucket_severe | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_penalty | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_reason_count | 90,316 | 100.0 | 100.0 |
| 1 | lp_m1_negative_case_similarity | 90,316 | 100.0 | 100.0 |
| 1 | lp_m3_gate_pass_ratio | 90,316 | 100.0 | 100.0 |
| 1 | lp_m3_match_score | 90,316 | 100.0 | 100.0 |
| 1 | lp_m7_effectiveness_score | 90,316 | 100.0 | 100.0 |
| 1 | lp_m7_mean_confidence | 90,316 | 100.0 | 100.0 |
| 1 | lp_m7_mean_sentiment | 90,316 | 100.0 | 100.0 |
| 1 | lp_m7_news_count | 90,316 | 100.0 | 100.0 |
| 1 | lp_m7_source_reliability | 90,316 | 100.0 | 100.0 |
| 1 | ma10 | 90,316 | 100.0 | 100.0 |
| 1 | ma13 | 90,316 | 100.0 | 100.0 |
| 1 | ma20 | 90,316 | 100.0 | 100.0 |
| 1 | ma3 | 90,316 | 100.0 | 100.0 |
| 1 | ma30 | 90,316 | 100.0 | 100.0 |
| 1 | ma5 | 90,316 | 100.0 | 100.0 |
| 1 | ma60 | 90,316 | 100.0 | 100.0 |
| 1 | ma8 | 90,316 | 100.0 | 100.0 |
| 1 | ma_gap_10_30 | 90,316 | 100.0 | 100.0 |
| 1 | ma_gap_20_60 | 90,316 | 100.0 | 100.0 |
| 1 | ma_gap_5_20 | 90,316 | 100.0 | 100.0 |
| 1 | macd_hist | 90,316 | 100.0 | 100.0 |
| 1 | macd_line | 90,316 | 100.0 | 100.0 |
| 1 | macd_signal | 90,316 | 100.0 | 100.0 |
| 1 | market_trend | 90,316 | 100.0 | 3.4 |
| 1 | mfi14 | 90,316 | 100.0 | 100.0 |
| 1 | moneyflow_net_20 | 90,316 | 100.0 | 3.3 |
| 1 | moneyflow_net_5 | 90,316 | 100.0 | 3.3 |
| 1 | moneyflow_net_zscore_60 | 90,316 | 100.0 | 3.3 |
| 1 | month_cos | 90,316 | 100.0 | 100.0 |
| 1 | month_sin | 90,316 | 100.0 | 100.0 |
| 1 | northbound_momentum_5v20 | 90,316 | 100.0 | 100.0 |
| 1 | northbound_net_10 | 90,316 | 100.0 | 100.0 |
| 1 | northbound_net_20 | 90,316 | 100.0 | 100.0 |
| 1 | northbound_net_5 | 90,316 | 100.0 | 100.0 |
| 1 | northbound_net_60 | 90,316 | 100.0 | 100.0 |
| 1 | northbound_net_zscore_60 | 90,316 | 100.0 | 100.0 |
| 1 | obv_norm | 90,316 | 100.0 | 100.0 |
| 1 | obv_slope5 | 90,316 | 100.0 | 100.0 |
| 1 | overnight_gap | 90,316 | 100.0 | 100.0 |
| 1 | price_volume_corr20 | 90,316 | 100.0 | 100.0 |
| 1 | pvt_norm | 90,316 | 100.0 | 100.0 |
| 1 | realized_kurt20 | 90,316 | 100.0 | 100.0 |
| 1 | realized_skew20 | 90,316 | 100.0 | 100.0 |
| 1 | relative_strength_20 | 90,316 | 100.0 | 3.4 |
| 1 | relative_strength_5 | 90,316 | 100.0 | 3.4 |
| 1 | ret_10d | 90,316 | 100.0 | 100.0 |
| 1 | ret_1d | 90,316 | 100.0 | 100.0 |
| 1 | ret_20d | 90,316 | 100.0 | 100.0 |
| 1 | ret_2d | 90,316 | 100.0 | 100.0 |
| 1 | ret_3d | 90,316 | 100.0 | 100.0 |
| 1 | ret_5d | 90,316 | 100.0 | 100.0 |
| 1 | ret_60d | 90,316 | 100.0 | 100.0 |
| 1 | roe_trend_60 | 90,316 | 100.0 | 100.0 |
| 1 | rolling_beta_60 | 90,316 | 100.0 | 3.4 |
| 1 | rs_ma20 | 90,316 | 100.0 | 3.4 |
| 1 | rs_ma5 | 90,316 | 100.0 | 3.4 |
| 1 | rsi14 | 90,316 | 100.0 | 100.0 |
| 1 | rsi24 | 90,316 | 100.0 | 100.0 |
| 1 | rsi6 | 90,316 | 100.0 | 100.0 |
| 1 | stoch_d | 90,316 | 100.0 | 100.0 |
| 1 | stoch_j | 90,316 | 100.0 | 100.0 |
| 1 | stoch_k | 90,316 | 100.0 | 100.0 |
| 1 | trend_slope_20 | 90,316 | 100.0 | 100.0 |
| 1 | trend_slope_60 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_rank20 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_rate | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_10 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_13 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_20 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_3 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_30 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_5 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_60 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_ratio_8 | 90,316 | 100.0 | 100.0 |
| 1 | turnover_zscore20 | 90,316 | 100.0 | 100.0 |
| 1 | upper_shadow_pct | 90,316 | 100.0 | 100.0 |
| 1 | upside_vol20 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_10 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_13 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_20 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_3 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_30 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_5 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_60 | 90,316 | 100.0 | 100.0 |
| 1 | volatility_8 | 90,316 | 100.0 | 100.0 |
| 1 | volume_rank20 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_10 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_13 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_20 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_3 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_30 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_5 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_60 | 90,316 | 100.0 | 100.0 |
| 1 | volume_ratio_8 | 90,316 | 100.0 | 100.0 |
| 1 | volume_zscore20 | 90,316 | 100.0 | 100.0 |
| 1 | vwap_gap5 | 90,316 | 100.0 | 100.0 |
| 1 | weekday_cos | 90,316 | 100.0 | 100.0 |
| 1 | weekday_sin | 90,316 | 100.0 | 100.0 |
| 1 | williams_r14 | 90,316 | 100.0 | 100.0 |

### 3.2 presence < 95% 的键（前 20，共 0）

- 无（所有键 presence ≥ 95%）

## 4. label maturity coverage（outcome_records）

- outcome 总量：90,287
- label_matured: 84,788（93.9%）
- pending: 5,389（6.0%）
- reconciled: 110（0.1%）

### 4.1 matured 按月分布（label_mature_time）
- 2026-04: 25,742
- 2026-05: 15,685
- 2026-06: 20,154
- 2026-07: 18,598
- 2026-08: 4,609

### 4.2 pending 按月分布（label_anchor_time，缺失时回退快照 decision_time）
- 2026-04: 29
- 2026-05: 107
- 2026-06: 734
- 2026-07: 1,679
- 2026-08: 2,645
- 2026-09: 195

### 4.3 B4 回填字段覆盖
- label_anchor_time：非空 84,897 / 90,287（94.0%）
- source_data_cutoff：非空 84,897 / 90,287（94.0%）
- conflict_flag：非空 84,897 / 90,287（94.0%）
- conflict_flag=False: 68,112
- conflict_flag=True: 16,785

### 4.4 outcome ↔ snapshot 链接覆盖与标签 universe
- JOIN 命中：90,287 / 90,287（100.0%）
- 有标签样本的 distinct symbol（训练 universe）：1,577
- 其中含 matured 样本的 symbol：1,378


