# M2 Implementation Report（S11–S23，Alpha V2.0 Alpha Research & Shadow）

> 批次：`M2 = S11–S23`
> 分支：`feat/alpha-v2-m1-0917`（无 upstream，**未 push**）
> 起始 HEAD：`43e91cc266027bca3fa7f5b78a73929d741b03e9`（= M1 最终验收基线）
> 本报告落盘目的：供 Codex 第二批独立验收追溯（施工证据 + 争议点定位）
> 生成日期：2026-09-18

---

## 1. Batch Status

```text
Batch = M2
Status = DONE（工程实现完成，等待 Codex 整批验收）
Stages Completed = S11 S12 S13 S14 S15 S16 S17 S18 S19 S20 S21 S22 S23
Stopped At = （无）
Engineering Verdict = PASS（自评）
Research Evidence Status = 60D AVAILABLE（本地开发窗口）/ 120D·250D AWAITING_DATA
Production Promotion = LOCKED
Codex Acceptance = PASS（工程实现层；外部独立验收结论，用户转达，2026-09-18 转录，见 §32）
```

## 2. Starting Repository State

```text
pwd            = E:/Software Development/StockAnalyzer
branch         = feat/alpha-v2-m1-0917
HEAD           = 43e91cc266027bca3fa7f5b78a73929d741b03e9（与预期一致）
git status     = 仅 1 个用户未跟踪文件 docs/system_issues_for_review_20260917.md（全程未动）
M1 基线测试    = 3202 passed / 2 skipped / 0 failed
```

M1 已通过 Codex 第三轮验收（PASS），本轮**未回退、未重做、未修改**任何 M1 已验收成果。

## 3. Stage Matrix

| Stage | 状态 | 定向测试 | 审计工件 | 关键交付 |
|---|---|---|---|---|
| S11 | DONE | 34 passed | `s11_validation.json` | Label V2：3/5/10/15D 可执行净收益 + 超额 + MAE/MFE + 方向 + 路径标签 |
| S12 | DONE | 17 passed | `s12_validation.json` | Eligible EW / Quality Pool EW / Style-Matched（同板块 kNN）+ residual |
| S13 | DONE | 14 passed | `s13_validation.json` | Winner Recall：Recall@Light/Deep/Final + 20D/60D 滚动 |
| S14 | DONE | 21 passed | `s14_validation.json` | 特征组可用性登记表 + Base V2 准入断言 + 常数/零填充检测 |
| S15 | DONE | 34 passed | `s15_validation.json` | 6 个可解释因子 + 等权 baseline + 与 ML 的公平对照函数 |
| S16 | DONE | 25 passed | `s16_validation.json` | 共享矩阵 + 四 Head + 单遍计数 + OOS isotonic 校准 |
| S17 | DONE | 19 passed | `s17_validation.json` | lgbm/xgb 分歧观测量 + 证据块（未升级为门） |
| S18 | DONE | 22 passed | `s18_validation.json` | Shadow 策略：v2_top1/3/5，无阈值旋钮、不强制选满 |
| S19 | DONE | 24 passed | `s19_validation.json` | Purged WF：purge+embargo、date-block/HAC/anchor、独立泄漏复核 |
| S20 | DONE | 23 passed | `s20_validation.json` | 双轨报告 + 决策日志/outcome 成熟接线（DF-S10-001/002 能力侧） |
| S21 | DONE | 23 passed | `s21_validation.json` + `.md` | 八块健康报告 + Review Trigger（只触发人工复核）+ 文案语义检查 |
| S22 | DONE | 18 passed | `s22_validation.json` | 阶段计时/峰值 RSS/预算/determinism 证据 |
| S23 | DONE | 19 passed | `s23_validation.json` | 同日配对增量实验框架 + News/Intraday 前置阻断 |

定向测试合计 **293** 例（0 failed）。

## 4. Files Changed

**新增（受跟踪路径）**

```text
src/stock_analyzer/alpha_v2/research/          18 个模块（见 PROGRESS §13.2）
scripts/alpha_v2_research_run.py               M2 全链路跑批（生成 S11–S23 审计工件）
tests/_alpha_v2_research_helpers.py            测试共用夹具
tests/test_alpha_v2_s11_outcomes.py            ... s23（13 个阶段测试文件）
docs/alpha_v2/M2_Implementation_Report.md      本文件
```

**修改（纯增量，不改既有行为）**

```text
src/stock_analyzer/backtest/matcher.py         + apply_price_tick 公开透传（研究侧与执行侧共用同一 tick 规则）
src/stock_analyzer/alpha_v2/decision_log.py    + read_decision_rows / read_outcome_rows（报告与复核读取）
docs/alpha_v2/PROGRESS.md                      + §13 M2 批次记录（追加，未覆盖历史）
```

**运行时产物（`.gitignore` 内，属 `artifacts/*`）**

```text
artifacts/alpha_v2/audit/s11_validation.json .. s23_validation.json
artifacts/alpha_v2/audit/s21_health_report.md
artifacts/alpha_v2/audit/m2_summary.json
```

**未改动**：`src/stock_analyzer/pipeline.py`、`runtime/service.py`、`main.py`、
`nightly_report_service.py`、`week5_notification_service.py`、`config/default.yaml`、
`config.py`、`src/stock_analyzer/models/**`、`src/stock_analyzer/learning/**`。

## 5. Business Objective Implementation

业务目标：**T 日收盘后，只用当时已知信息，从 T+1 可实际买入的股票中，找未来 3–5 个
交易日更可能上涨、预期净收益为正、预期超额更高且下行可接受的少量股票**（允许 0 只）。

| 业务问题 | 实现位置 | 落地方式 |
|---|---|---|
| 未来 3D/5D 正收益概率 | S16 Head C `p_up_net_{3,5}d` / `p_up_excess_{3,5}d` | LightGBM 二分类；**未校准前标 `calibration=none` 且展示层写「方向分（未校准）」**；OOS isotonic 校准后才允许叫「正收益概率」 |
| 未来 3D/5D 预期净收益 | S16 Head B `expected_net_return_{3,5,10,15}d` | 直接回归 S11 的可执行净收益 |
| 未来 3D/5D 预期超额 | S16 Head B `expected_excess_return_{3,5,10,15}d`；S12 三层基准 | 超额一律相对**显式基准**（Eligible EW / Quality Pool EW / Style-Matched），报告写 `benchmark_name` |
| 当日 Alpha Rank | S16 Head A `alpha_rank_score` | 学 5D 超额收益的**当日横截面 rank 分位**（不是绝对收益），输出再取截面分位 |
| 未来几天回撤风险 | S16 Head D `expected_mae_{3,5}d` / `p_mae_le_5pct_{3,5}d` | MAE 回归 + `P(MAE_5d <= -5%)` 分类 |
| T+1 是否真实可成交 | S11 `executable` / `no_fill_reason` / `entry_delay_sessions`；S18 取候选时「可成交优先」 | 复用 M1 S02 的 `simulate_entry`（一字涨停/停牌/无有效价 → no_fill） |
| Data Health / Market Regime | S21 八块报告 + S18 `gates` 标注 | 评估并**标注**，Shadow 阶段 `enforced=False` |

**3D = 短期确认**（`up_net_3d` / `up_excess_3d` / `expected_*_3d` 全部产出），
**5D = 主业务 Horizon**（`primary_horizon=5`，主 Rank 目标与 report primary 都是 5D），
10D/15D 保留用于信号衰减（S21 给出 3/5/10/15 四档 IC）。

## 6. Label / Outcome Contract

```text
schema            = alpha_v2_label_v2.v1
entry_mode        = next_session_open（T+1 开盘；不可成交即 no_fill）
price_basis       = raw（执行价必须是原始价；面板口径先认证后使用）
cost_model        = round_trip_rate_from_matcher_config（佣金 + 过户费 + 卖出印花税，参考名义 10 万）
horizons          = 3 / 5 / 10 / 15（交易日；入场日 = 第 1 个持有日）
path_label        = tp8_before_sl5_10d（legacy soup 口径，冲突策略复用同一实现）
```

产出字段（每条决策 × 每个 horizon）：

```text
net_return_{h}d, excess_return_{h}d, mae_{h}d, mfe_{h}d
up_net_{3,5}d, up_excess_{3,5}d            （3D/5D 方向，业务核心）
tp8_before_sl5_10d, tp8_conflict_10d
matured_{h}d, maturity_date_{h}d, exit_no_fill_{h}d
executable, no_fill_reason, entry_date, entry_price_raw, entry_price_net,
entry_delay_sessions, round_trip_cost_rate, price_mode, price_mode_certified,
execution_uncertain, corporate_action_suspected
```

护栏：

- **未成交不计收益**：`executable=False` 时全部收益/MAE/MFE 写 `not_available`（不是 0）；
- **不得从 T 收盘算收益**：入场价恒取 T+1 开盘（有测试断言决策日收盘价不影响入场价）；
- **不可成交不进基准**：`benchmark_series_from_outcomes` 只统计可成交且已成熟样本；
- **除权处理**：数据源给权威 `pre_close` 时按 0.5% 阈值标 `corporate_action_suspected`；
  拿不到就写 `None + pre_close_source_unavailable`（不知道就写不知道）；
- Legacy `soup` label 保留未动，`tp8_before_sl5_10d` 与 `build_soup_labels` 逐例对齐
  （三种冲突策略参数化测试）。

## 7. Benchmark System

```text
eligible_ew         PIT 合格股票池等权（决策集合本身）
quality_pool_ew     **主基准**：质量池等权（来源自述，见下）
style_matched       同板块 + 风格近邻（kNN, k=20）对照 → residual_excess_return
simple_baseline     S15 因子组合成员（可选层，同一机制）
```

风格维度（蓝图 §P1-02 的五维全覆盖）：

```text
style_board（板块，硬过滤）
style_float_cap_log（流通市值对数）
style_vol_20d（20 日已实现波动）
style_momentum_20d（20 日动量）
style_turnover_20d（20 日平均成交额）
```

**PIT 纪律**：五维全部由 ≤ 决策日的 bar 计算（有对抗测试：追加决策日之后一根暴涨 bar，
特征逐字段不变）；标准化在**当日截面内**完成；对照集排除自身（`style_peer_count=0` 时
标 `style_fallback=True`，绝不拿自己当自己的对照）。

**来源自述（重要）**：

```text
quality_pool_source = production_selection_engine        # 生产成员注入时
                    | research_proxy:alpha_v2_quality_v1 # 研究侧代理（PIT 合格 + 20D 成交额 top-N）
```

本轮本地证据用的是**研究代理**，已在报告里显式标注（DF-M2-001），**没有**冒充生产
Quality300 成员。

## 8. Winner Recall

```text
赢家定义 = Quality 池内按**未来真实可执行超额收益**（默认 5D）排序的 Top quantile（默认 10%）
分组单位 = decision_date（逐日独立）
输出     = Recall@quality / Recall@light / Recall@deep / Recall@final + 20D/60D 滚动
```

**自证循环防护**：排序列必须匹配 `^(excess_return|net_return|mae|mfe)_\d+d$`，
传 `score` / `v2_rank_score` / `predicted_alpha` 直接抛错（有 3 条对抗测试）。

本地读数（400 票 / 197 成熟日）：

```text
recall_quality = 1.000（定义域内自然为 1）
recall_light   = 0.427（60D 0.464）
recall_deep    = 0.220（60D 0.252）
recall_final   = 0.029（60D 0.030）
winner_mean − pool_mean = +0.124
```

即：**研究代理漏斗把约 97% 的未来赢家挡在了最后一档之前**；这是下一步漏斗改造的量化入口。
（口径说明：此处四级池是研究侧按当日流动性名次构造的代理，不是生产 300/100/50 的真实成员。）

## 9. Feature Leakage Audit

登记表 16 个组，每组给出 `source / available_at_rule / asof_safe / asof_evidence /
missing_policy / price_series_mode / in_base_v2`。

`asof_safe` 三态与结论：

```text
proven      price_volume_technical, market_relative, calendar, market_state   → 允许进 Base V2
unverified  financial_pit, intraday_summary, shareholder_count, northbound,
            margin_financing, block_trade, dragon_tiger_inst, moneyflow,
            hk_hold, learning_protocol_derived, news_theme_derived,
            background_completeness_meta                                     → 禁止进 Base V2
```

三层 fail-closed：

1. **未登记列**（不属于任何组）→ 不进 Base V2（`unregistered` 计数进报告）；
2. **outcome/label 黑名单**（`net_return_*` / `excess_return_*` / `mae_*` / `fwd_return` /
   `label*` / `entry_*` / `maturity_date_*` …）→ **优先于分组匹配**判为禁止，
   防止"前缀撞车"（`excess_ret_5` 是行情特征、`excess_return_5d` 是答案）；
3. **构建矩阵时断言**：`build_shared_feature_matrix` 对全部特征列跑
   `assert_safe_feature_columns`，含未证明列直接抛错（不是过滤后继续）。

其他机械检查：`missing_<group>` 标记列（缺失 ≠ 真实 0）；常数/零填充检测
（众数占比 ≥ 0.995 或空值率 ≥ 0.98 → `constant_suspects`）；
`financial_as_of > trade_date` 违规计数（列存在时）。

本地读数：`Base V2 安全列 120 / 排除 88 / 未登记 0 / 常数疑似 101`。
**101 列近乎常数或近全空**与 Phase 2 归因的「98 特征全 NaN」同源，已记入 DF-M2-003。

## 10. Simple Baseline

```text
trend_pullback      trend_ma_gap_20(+), ma_slope_20(+), reversal_5d(+)
relative_strength   rs_excess_20d(+) —— 截面去均值动量（不依赖指数数据口径）
liquidity           liquidity_turnover_20d(+)
volatility_quality  vol_quality_20d(+) = −20D 波动
fundamental_quality **blocked**：输入属 financial_pit（asof_safe=unverified），
                    按蓝图「Fundamental Quality（仅 PIT-safe）」不予启用
```

方向**预先登记在代码常量**（附理由），报告同时给出实测 IC 与 `direction_consistent`
——事后翻方向必留痕。

公平性：baseline 与 ML 共用 S11 outcome / S12 benchmark / S19 同一 fold 划分 /
S21 同一评价块；`compare_with_ml` 同时看 IC 与 TopK 超额，
`mature_dates < 60` 时结论只能写「awaiting_sample」；
`require_baseline_companion` 强制 ML 评测块必须带 baseline 同屏结果。

本地读数：baseline 5D IC −0.022（CI[−0.060, +0.026] 跨 0）→ 本窗口简单因子无效；
ML 5D IC +0.052、TopK 超额亦略高 → `ml_beats_baseline`（197 日，非 clean OOS）。

## 11. Multi-Head Architecture

```text
fetch once → feature once → matrix once → predict N heads → persist once
```

**可机械验证**：`BuildStats` 计数 + `assert_single_pass()`；每个 Head 的输出携带 **矩阵指纹**，
`_assert_shared_matrix` 在指纹不一致时直接抛错（"共享矩阵"是断言不是约定）。
本地跑批实测：`matrix_build_calls=1, prediction_calls=1`。

| Head | 输出 | output_kind | 语义约束 |
|---|---|---|---|
| A Alpha Rank | `alpha_rank_score` | `rank_score` | 学 5D 超额收益的截面 rank；输出再取截面分位 |
| B Expected Return | `expected_{net,excess}_return_{h}d` | `expected_return` | 直接回归可执行净/超额收益 |
| C Direction | `p_up_{net,excess}_{3,5}d` | `probability` | **唯一**可称「正收益概率」的 Head，且需 OOS isotonic 校准 |
| D Risk | `expected_mae_{h}d` / `p_mae_le_5pct_{h}d` | `risk_score` | 禁止回流成 Alpha（标签列在 S14 黑名单内） |

校准纪律：`calibrate_direction` 要求校准窗与训练窗**互斥**（重叠直接抛错），
校准行必须被推理过（否则退化成静默跳过），产出 `*_calibrated` 列并写
`method=isotonic_oos`。未校准的 Direction 在 `head_display_semantics` 里
`may_call_probability=False`、展示词为「方向分（未校准）」。

实现注记：LightGBM 走**原生 `lgb.train`**（`lightgbm.sklearn` 需要 scikit-learn，
本环境未安装），依赖面更小、确定性更易保证（`deterministic=True` + `force_row_wise`）；
同 seed 两次运行预测逐位相同（有测试）。

## 12. Cross Review V2

- **Legacy 完全不动**：`legacy_cross_review_policy` 只读并声明 `modified_by_alpha_v2=False`；
  有结构测试断言 V2 配置块里**不存在**任何 Legacy 阈值字段；
- 观测量：`lgbm_rank_pct / xgb_rank_pct / rank_disagreement(=|Δrank_pct|) /
  prob_disagreement(=|Δprob|)`（两个模型共用同一份共享矩阵）；
- 证据块：按分歧分位分组看未来真实可执行超额（逐日 + date-block CI），
  并对"高分歧桶 vs 低分歧桶"做**同日配对**（`paired_extremes`）；
- 策略升级判据：`mature_dates >= 60` **且** 高分歧桶显著更差 → 才允许标
  `hard_gate_candidate`，且**仍不自动生效**（需人工批准）。

本地读数：高−低 = −0.00065（方向符合"分歧大有轻微负作用"），但仅 36 成熟日 →
`policy=observation_only`、`evidence_sufficient=False`。

## 13. Final Policy V2 Shadow

```text
Candidate = Deep50（缺候选阶段标记时 fail-closed，不把全表当候选）
排序      = alpha_rank_score（缺 rank 的行保留但排最后，不静默丢弃）
取前 K    = v2_top1 / v2_top3 / v2_top5（**可成交优先**，不可成交的上榜行如实计数）
```

- **没有阈值旋钮**：`FinalPolicySpec` 无任何 `*threshold*` 字段（结构测试 + `assert_no_threshold_knobs`）；
- **不强制选满**：候选不足时 `v2_top5` 少于 5 只，且 `zero_signal_is_valid=True`；
- **不编造值**：缺失 Head 字段写 `not_available`（NaN 也映射为 `not_available`）；
- **门只标注不启用**：`gates.enforced=False`，`data_health / market_regime / risk_level`
  的 `would_block` 只做记录；
- 每天落 `reports/shadow_policy_YYYYMMDD.json`（原子写）。

## 14. Walk-Forward / OOS

```text
method            = time（随机切分入口 refuse_random_split 直接抛错）
train/test/step   = 120 / 20 / 20 交易日
max_label_horizon = max(horizons) = 15
purge_days        = execution_delay + max_horizon − 1 = 15
embargo_days      = max(max_horizon, 配置值) = 15（配置调小无效，被抬高）
```

- 训练决策日上界 = 使**成熟日**严格早于 test_start 的最大决策日（公式写进代码注释）；
- **第二道数据驱动 purge**：按真实 `maturity_date_{H}d` 再剔一遍（见 §21 缺陷 4），
  剔除行数 `maturity_purged_rows` 计入报告，`purge_adequacy` 标 `calendar_purge_insufficient`；
- **独立泄漏复核**（`overlap_leakage_check`）：分别检查决策日越界、成熟日越界、
  成熟日未知行数，三者都计入 `lookahead_violations`；
- 统计口径三件套：date-block moving-block bootstrap（复用 `learning.scoring_eval`）+
  Newey-West HAC（lag = H−1）+ non-overlapping anchor（stride = H）；
- 报告逐折写出 `train/validation/test 区间 / purge / embargo / max horizon / 泄漏违规数`。

本地读数：3 折全部可用、`lookahead_violations=0`、`maturity_purged_rows=20`、
pooled IC **+0.016**、CI **[−0.018, +0.057]**（跨 0）→ **INCONCLUSIVE**、
60 clean OOS 日 → `initial_direction_review`。

## 15. Shadow Dual Run

```text
Legacy → 正式结果/通知（只读提取：funnel.final_selection.final_signals / rejected）
V2     → artifacts/alpha_v2/reports/dual_run_YYYYMMDD.json
```

报告自述五条未触碰声明（`legacy_modified=False` 等）+ `shadow_flags` 读出的
`enforce_final_selection=False`（为 true 时 `assert_legacy_untouched` 直接抛错）。
`comparison` 给出：`legacy_final / v2_top1/3/5 / overlap_with_legacy /
v2_top5_previously_rejected_by_legacy / legacy_reject_reasons_summary / v2_only / legacy_only`。

**台账接线（DF-S10-001/002 能力侧闭合）**：

- `build_shadow_decision_rows` 用 S16 Head 输出填 `v2_rank_score / v2_expected_return /
  v2_direction_score / v2_risk_score`（已校准方向分优先）；缺字段仍写 `not_available`；
- `mature_shadow_outcomes` 复用 M1 `compute_outcomes`，**信号当天写 0 行**（有测试）,
  成熟后才落 `outcomes/YYYY/MM/`；未成熟只报 `pending_horizons`；
- 生产调度接线**未做**（需部署授权），列 DF-S10-001/002 剩余项。

## 16. Daily Alpha Health Report

八块齐备（缺数据写 `not_available`，不省略）：`identity / data_health / funnel /
winner_recall / score_distribution / alpha_quality / execution / drift_governance`。

- `alpha_quality` = S15/S16/S19 同一评价块（3/5/10/15 IC + 20D/60D 滚动 + TopK + 分位单调性 + 下行）；
- **Review Trigger**：`20D IC < 0 AND 60D IC <= 0 AND 60D Top5 超额 <= 0` →
  `action=human_review_only`；`assert_no_auto_action` 禁止任何自动动作；
  样本 < 20 成熟日 → `status=insufficient_sample`（不误报）；
- **文案语义检查（DF-S09-002）**：`audit_display_text` / `audit_report_text_blocks`
  检查「上涨概率 / 胜率 / 命中概率」等词是否被用在非校准输出上；
- 产出 `health_report_YYYYMMDD.json` + `.md`。

本地读数：八块 `ok`（funnel/data_health 为 `observed`）；Trigger **未触发**
（`ic_20d=-0.018 < 0` 但 `ic_60d=+0.0094 > 0`，规则要求两条同时成立）。

## 17. NAS Performance

本地实测（400 票 / 202 交易日 / 80,800 决策 / 208 特征）：

```text
fetch    4.79s      feature  94.21s
matrix   0.22s      predict  49.22s
persist  2.42s      wall     552.32s
stage_total 150.86s（其余为 S13–S21 分析/报告时间）
peak_rss_mib = null（Windows 无 /proc → peak_rss_status=unavailable，如实标注）
budget_check = ok（未超；内存项因不可读未参与判定）
single_pass  = {matrix_build_calls: 1, prediction_calls: 1}
determinism  = {matrix_fingerprint: 095d2279dfb82d89, prediction_digest: 666ac7eacbc3d6ae, selection_order: [...]}
```

**不变量**：`assert_deterministic` 要求同输入两次运行的矩阵指纹 / 预测摘要 /
候选顺序完全一致（3 条测试覆盖：一致通过、选择变化报错、指纹变化报错）。
`guard_budget` 超预算只记录 `exceeded`，不改变结果也不静默放行。

## 18. Theme / News / Intraday Status

```text
theme    readiness=ok（板块映射走 tushare 板块；仅框架，enabled=False）
         本地实验：control vs experiment 两臂选择规模相同；
         affected_symbol_dates=0 → gate_pass=False → verdict=awaiting_sample
news     readiness=blocked(news_pit_path_incomplete)：新闻发布时间未证明 <= 决策时点
intraday readiness=blocked(intraday_coverage_unverified / freshness_unverified / pit_unverified)
         另有 independent 守卫：zero_fill_mixing=true 时直接阻断
```

**"出票更多"不是证据**：两臂选择规模恒相同（`arms_size_equal` 有测试），
成功判据唯一 —— `same_day_paired_excess`（同日配对超额 + date-block CI）。
样本门：≥60 交易日、≥30 独立受影响日、≥200 受影响 symbol-day。

## 19. M1 Deferred Findings Status

| ID | M2 状态 | 说明 |
|---|---|---|
| DF-S07-001（execution qfq vs 生产 raw） | **AWAITING_NAS_VERIFICATION** | 本地用实测探针认证 `market_duckdb` 面板为 raw（超限比例 6.8e-05）；受跟踪配置仍是 qfq，生产 vendor 链路口径需 NAS 核验。未假装闭合 |
| DF-S07-002（corporate action 治理） | PARTIAL | S11 增加 `corporate_action_suspected` 检测（需权威 `pre_close`）；本地无权威前收 → 全量 `unavailable`，如实标注 |
| DF-S06-001（strict replay 证据稀缺） | **AWAITING_NAS_VERIFICATION** | M2 未涉及 strict_production_replay 链路；`strict_production_replay != pit_research` 的表述在 S19 文档与报告中保持 |
| DF-S06-002（asof 非 week5 路径闸门未接线） | OPEN | M2 未改动该路径（本批不涉及），列入 DFT-M2 转 M3 |
| DF-S08-002（live breadth fail-open 接线） | **AWAITING_PRODUCTION_OBSERVATION** | 需生产观察，未自行部署 |
| DF-S09-002（文案层语义替换） | **CLOSED（能力侧）** | S21 `audit_display_text` + `audit_report_text_blocks` + 报告 Markdown 全部过检查；UI/飞书正式文案仍待部署期接入 |
| DF-S10-001 / DF-S10-002 | **PARTIAL** | S20 已实现台账写入与 outcome 成熟接线（含"信号当天 0 行"测试）；**生产调度接线未做** |
| DF-S03-001 / DF-S03-002（历史 universe 数据侧不足） | PARTIAL | S13/S19 报告显式携带 `survivorship_coverage`（沿用 M1 语义，默认 `incomplete_or_unknown`）；S11 面板报告 `listed_age_basis`，未把它包装成"全市场无偏历史" |
| DF-S00-001（config dump/validate 往返） | OPEN | M2 未触（与本批无关） |
| DF-S05-002（历史坏记录治理动作） | **AWAITING_NAS_VERIFICATION** | 需 NAS 跑同一 CLI 才有权威分类 |

## 20. Tests Executed

```bash
# 各阶段定向（13 个文件，合计 293 例）
python -m pytest tests/test_alpha_v2_s11_outcomes.py ... tests/test_alpha_v2_s23_experiments.py -q

# M2 全量定向
python -m pytest tests/test_alpha_v2_s11_outcomes.py tests/test_alpha_v2_s12_benchmarks.py \
  tests/test_alpha_v2_s13_winner_recall.py tests/test_alpha_v2_s14_feature_audit.py \
  tests/test_alpha_v2_s15_simple_baseline.py tests/test_alpha_v2_s16_multi_head.py \
  tests/test_alpha_v2_s17_cross_review_v2.py tests/test_alpha_v2_s18_decision_policy.py \
  tests/test_alpha_v2_s19_walk_forward.py tests/test_alpha_v2_s20_shadow_dual_run.py \
  tests/test_alpha_v2_s21_health_report.py tests/test_alpha_v2_s22_perf.py \
  tests/test_alpha_v2_s23_experiments.py -q

# M1 回归（本轮唯一被修改的 M1 模块 + M1 契约门）
python -m pytest tests/test_decision_log_s10.py -q                                # 14 passed
python -m pytest tests/test_alpha_v2_baseline.py tests/test_alpha_v2_config.py -q  # S00 golden/架构守卫
python -m pytest tests/test_entry_simulation.py tests/test_asof_universe.py \
  tests/test_selection_contract.py tests/test_holding_curve.py -q   # M1 S02/S03/S04/S07 契约
# 上述 M1 定向集合计 105 passed（本机实测）

# 批次级全量回归
python -m pytest -n 4 --dist loadfile

# lint
python -m ruff check src/stock_analyzer/alpha_v2/ scripts/alpha_v2_research_run.py tests/test_alpha_v2_s*.py tests/_alpha_v2_research_helpers.py

# 审计工件生成（真实本地数据）
python scripts/alpha_v2_research_run.py --window-start 2025-06-02 --window-end 2026-03-31 \
  --warmup-days 200 --max-symbols 400 --out artifacts/alpha_v2/audit
```

## 21. Test Results

```text
M2 定向合计        = 293 passed / 0 failed
批次级全量回归     = 3496 passed / 2 skipped / 0 failed（641.27s，-n 4 --dist loadfile）
M1 基线对照        = 3202 passed / 2 skipped / 0 failed（M1 报告 §7 记录）
                     M2 施工提示词记录的 M1 基线为 3204 passed——两者相差 2 例，本轮未复核
                     该差异来源（两者均 0 failed）；如需精确净增数请以 Codex 复核为准
ruff（M2 全部文件）= All checks passed
```

## 22. Negative / Adversarial Tests

每个阶段至少一条对抗用例，清单：

| 阶段 | 对抗/负向用例 |
|---|---|
| S11 | 一字涨停不可成交；ST 5% 上限；创业板 20%（按 10% 会被误拒）；次日无 bar **不得顺延成交**；停牌退出标 `exit_no_fill`；`symbol` 不在面板；决策日非交易日；未认证价格口径 → 主样本 0；复权序列探针必须**拒绝**认证 |
| S12 | 不可成交样本不得进基准（并断言"若假设能成交会抬高基准"）；风格对照排除自身；peer 不足标 fallback；质量池无流动性列时抛错，不得用无规则池子冒充 |
| S13 | 用预测分数定义赢家 → 抛错（自证循环）；缺级列 → `not_available` 而非"全留下"；池子过小跳过 |
| S14 | 未登记列/收益列/高风险组列进 Base V2 → 抛错；常数零填充检测；`financial_as_of > trade_date` 违规检出 |
| S15 | ML 未带 baseline 同屏 → 抛错；baseline 未被打败 → verdict=`stop_adding_complexity`；缺失因子按可用因子归一，不按 0 计 |
| S16 | 未证明特征列/收益列进矩阵 → 抛错；Head 指纹不一致 → 抛错；校准窗与训练窗重叠 → 抛错；未校准方向分不得称概率 |
| S17 | `random`/`kfold` 切分 → 抛错；分歧证据不足仍 `observation_only`；`assert_no_hard_gate` 拒绝未知策略 |
| S18 | 阈值字段守卫；缺候选阶段标记 → 不得把全表当候选；`not_available` 不编造；门只标注不启用 |
| S19 | 随机切分拒绝；未来成熟日检测；`NO_GO_LEAKAGE` 路径覆盖；负关系检测（指标反号不得判 GO）；训练不混入未 purge 样本 |
| S20 | 篡改"未触碰声明" → 抛错；`enforce_final_selection=true` → 抛错；Legacy 报告缺失 → `final_selection_not_found` 而非编造；信号当天 outcome 必须 0 行 |
| S21 | 自动动作 payload → 抛错；rank score 写"上涨概率" → 误标检出；样本不足不误触发 |
| S22 | determinism 被破坏（选择/指纹变化）→ 抛错；超预算显式 `exceeded`；缺失阶段标 `not_available` |
| S23 | News/Intraday 未证明前置 → `blocked` 且不给数字；两臂规模必须相等；`success_criterion` 非同日配对 → 抛错 |

## 23. Legacy Regression

```text
final_signal_min_threshold = 70            未改（S00 golden 契约仍通过）
Cross Review 四阈值                         未改（S17 只读声明 + 结构测试）
night 300/100/50、final cap 5              未改
风险门（breadth / overextension / board）   未改
serving model / challenger                 未切换、未 promote
飞书正式通知                               未改、未接 V2
生产部署 / git push / 容器重启 / .env       均未执行
alpha_v2 三个开关                           enabled=false / shadow_only=true / enforce_final_selection=false
```

额外结构守卫：S00 的 `test_alpha_v2_flag_consumers_are_explicitly_declared` 与
`test_production_entrypoints_never_consume_alpha_v2_flag` 仍通过——M2 新增代码全部位于
`src/stock_analyzer/alpha_v2/**`（V2 边界内），**没有**新增生产入口对 V2 flag 的消费。

## 24. PIT / Leakage Review

```text
1. 面板只装载 <= window_end 的行（SQL 谓词写死）；日历只覆盖 window 内
2. 特征：FeatureEngineer 全滚动/shift，无未来窗口；风格特征有"追加未来 bar 不变"测试
3. 标签：入场 T+1 开盘、出场按标的自身 bar 序列；未成熟一律 not_available
4. 基准：只用可成交且已成熟的行；同日截面内标准化
5. 股票池：S03 PIT 语义（无 <= as_of 的 bar 即 future_listed 硬排除）
6. OOS：purge+embargo 双口径（日历 + 真实成熟日）+ 独立复核；随机切分被拒
7. 未成交不计收益、不进基准、不进 winner 定义
8. Base V2 特征准入断言：未证明 PIT 的列无法进入矩阵
```

**本轮修复的真实泄漏**：S19 首轮跑批报 `NO_GO_LEAKAGE`（3 折共 20 行训练样本的
成熟日晚于测试起点）。根因：日历口径 purge 与"按标的自身 bar 序列推进的真实成熟日"
不一致（停牌/停更票越界）。修复后 `lookahead_violations=0` 且 `maturity_purged_rows=20`
如实计入报告（`purge_adequacy=calendar_purge_insufficient`），
独立复核逻辑保持不变（仍能发现未 purge 的实现）。

## 25. Security Review

```text
- 新增代码不读取/不写入任何凭据；不 import 通知、下单、券商、任务调度模块
- artifact 写入只落 artifacts/alpha_v2/**（.gitignore 内）；原子写（临时文件 + os.replace）
- 唯一外发面：无（跑批脚本不联网、不发通知）
- 结构测试：S17/S20 模块源码中不得出现 notification / feishu / order / broker 等关键字
- 运行日志（tmp_m2_run.log / tmp_m2_full_tests.log）不含任何密钥，已在批次收尾删除
```

## 26. Audit Artifacts

```text
artifacts/alpha_v2/audit/s11_validation.json    面板 + outcome 口径 + no_fill/主样本账
artifacts/alpha_v2/audit/s12_validation.json    三层基准 + 来源自述 + 风格层诊断
artifacts/alpha_v2/audit/s13_validation.json    winner recall 摘要与逐日明细行数
artifacts/alpha_v2/audit/s14_validation.json    Base V2 列清单 + 分组覆盖 + 机械检查 + 常数疑似
artifacts/alpha_v2/audit/s15_validation.json    因子定义/覆盖 + baseline 评价 + 逐因子实测 IC
artifacts/alpha_v2/audit/s16_validation.json    矩阵身份 + 四 Head 语义/校准 + 评价 + vs baseline
artifacts/alpha_v2/audit/s17_validation.json    Legacy 只读声明 + 分歧观测 + 证据块
artifacts/alpha_v2/audit/s18_validation.json    Shadow 策略全文（含 gates 标注）
artifacts/alpha_v2/audit/s19_validation.json    逐折隔离矩阵 + 泄漏复核 + pooled 判定
artifacts/alpha_v2/audit/s20_validation.json    双轨对照 + 未触碰声明 + 回滚开关
artifacts/alpha_v2/audit/s21_validation.json    八块报告 + Review Trigger
artifacts/alpha_v2/audit/s21_health_report.md   人读版八块
artifacts/alpha_v2/audit/s22_validation.json    阶段计时 + 单遍计数 + determinism 指纹 + 预算判定
artifacts/alpha_v2/audit/s23_validation.json    三类信号就绪度 + theme 配对实验
artifacts/alpha_v2/audit/m2_summary.json        批次总表（含 13 阶段状态与代码/配置身份）
```

## 27. PROGRESS.md

已追加 `§13 M2 批次`（批次状态、Stage Matrix、文件清单、行为变化、5 条实修缺陷、
研究样本门与读数、Deferred 表、回滚），**未覆盖** M1 历史记录。

## 28. Research Evidence Status

```text
20D  = AVAILABLE（197 成熟决策日；本地开发窗口）
60D  = AVAILABLE（197 > 60，仅"第一轮方向判断"）
120D = AWAITING_DATA（S19 clean OOS 仅 60 日；S16/S21 的 197 日非 clean OOS 口径）
250D = AWAITING_DATA
```

结论口径（必须原样保留）：

```text
S19 clean OOS 判定 = INCONCLUSIVE（pooled IC +0.016，CI 跨 0）
S16/S21 的 197 日读数 = 开发窗口，已被反复用于选择，不是 clean OOS
=> 不得宣称模型有效、不得进入 Advisory、不得调阈值、不得 promote
```

## 29. Production Promotion

```text
Production Promotion = LOCKED
```

未执行：`git push` / 部署 / 容器重启 / `.env` 修改 / serving model 切换 / model promote /
飞书文案改动 / `alpha_v2.enforce_final_selection=true`。

## 30. Deferred Findings

见 PROGRESS §13.6（DF-M2-001..005 与本轮仍未闭合的 M1 项）。
其中最需要用户拍板的一条：

```text
DF-M2-003：101/208 特征列在本窗口近乎常数或近全空（背景/资金/分钟组）。
这些组已被 S14 排除出 Base V2（正确），但上游回填治理决定"能否把这些维度
重新纳入 Alpha 研究"——属数据侧投入决策，不在本批范围。
```

## 31. Rollback

```text
整批回滚（全部为新增文件，删除即完全复原）：
  src/stock_analyzer/alpha_v2/research/
  scripts/alpha_v2_research_run.py
  tests/_alpha_v2_research_helpers.py
  tests/test_alpha_v2_s11..s23_*.py
  docs/alpha_v2/M2_Implementation_Report.md
  artifacts/alpha_v2/audit/s1[1-9]_*,s2[0-3]_*,m2_summary.json

增量改动回滚（纯新增函数，不影响 M1/Legacy）：
  git checkout -- src/stock_analyzer/backtest/matcher.py
  git checkout -- src/stock_analyzer/alpha_v2/decision_log.py
  git checkout -- docs/alpha_v2/PROGRESS.md   # 仅撤销 §13 追加（会丢失本批记录）

无数据迁移、无不可逆操作、无生产侧状态被改写。
```

## 32. Codex Acceptance

**PASS（工程实现层，2026-09-18）** —— 结论由**外部独立验收**给出并经用户转达，
本节只做转录（ZCode 不自行判定 PASS）。

```text
Batch Verdict         = PASS
Engineering Verdict   = PASS
Blocking Findings     = 无
Research Gate         = 60D AVAILABLE / 120D·250D AWAITING_DATA（与蓝图一致）
Production Promotion  = LOCKED
```

验收方独立完成的证据（转述）：三层核验 —— ①读代码核实不变量（S11 未成交恒
`not_available`、S13 自证循环防护、S14 黑名单优先、S16 指纹断言、S19 双口径 purge 等）；
②重跑测试（M2 定向 293 / M1 回归 105 / 批次级全量 3496 passed·0 failed，
外加 10 条独立反例探针全过）；③自编对抗探针逐门抽查。
其余核查：两处改动确认为纯增量、凭据扫描零命中、15 件审计工件与报告一致、
表述边界未被包装成 PASS。

非阻塞观察（4 项）与待决事项（commit 授权、DF-M2-003 数据侧治理、生产动作）
已转录到 `docs/alpha_v2/PROGRESS.md` §14。

### 原始建议复核清单（交付验收方参考，已完成）

按优先级：

1. **S19 的两道 purge 是否真的消除泄漏**：看 `s19_validation.json` 的
   `leakage.maturity_violations=0` 与 `diagnostics.maturity_purged_rows`，
   以及 `purge_adequacy`；并检查独立复核是否仍能发现"没 purge 的实现"
   （`test_walk_forward_verdict_no_go_when_leakage_survives`）。
2. **S14 的 Base V2 准入是否真的 fail-closed**：试图把 `bg_roe`/`i1m_*`/
   `excess_return_5d` 放进特征矩阵，应直接抛错而不是过滤后继续。
3. **S11 是否真的"未成交不计收益、不用 T 收盘成交"**：
   `test_entry_price_is_never_decision_day_close`、`test_no_fill_rows_have_no_returns`。
4. **S16 的 Direction 语义**：未校准不得称概率（`head_display_semantics`），
   校准必须 OOS 且与训练窗互斥。
5. **S18 是否真的没有阈值/不强制选满/不接管**：结构测试 + `enforce_final_selection=False`。
6. **S12 的 `quality_pool_source` 是否被如实标注为研究代理**（不得冒充生产成员）。
7. **S13 的赢家定义是否只用 outcome**（预测分数必须抛错）。
8. **S22 的 determinism 证据**：同输入两次运行的指纹/顺序一致性。
9. **研究结论表述边界**：`INCONCLUSIVE` / `AWAITING_DATA` 不得被读成 PASS。

## 33. Next Step

M2 工程实现完成，**停止**。等待 Codex 第二批独立验收（`CURRENT_BATCH = M2`）。
在获得明确 PASS 与用户授权之前：

```text
不部署、不切模型、不开启正式 V2、不调 Legacy 阈值、不 promote。
```
