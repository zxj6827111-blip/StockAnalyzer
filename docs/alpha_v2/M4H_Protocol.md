# M4-H Research Protocol（冻结）

> **本协议在任何 M4-H 结果产生之前冻结。** 冻结之后：
> - 不得因结果不好而修改协议并覆盖旧结果；
> - 任何参数变化都必须换 `protocol_id` + 新 `experiment_id`，**旧结果必须保留**；
> - 落盘键 `(protocol_id, fold_id, decision_date, symbol)` 一旦写入不得改写，
>   重跑不一致即抛 `HistoricalOOSRestatementError`（见 §11）。
>
> 机器可读的同一份协议：`artifacts/alpha_v2/m4h/protocol/m4h_protocol_manifest.json`
> （含 `protocol_hash`）。

## 1. 身份

| 字段 | 值 |
|---|---|
| `protocol_id` | `m4h_exp_001` |
| `experiment_id` | `M4H_EXP_001` |
| `code_baseline_commit` | `33d0f7f97e79215ad8c65f972c0e56eeead10614`（M3 冻结基线） |
| 工作树 | `research/alpha-v2-m4h-historical-locked-oos`（独立 worktree，与 Runtime Identity Hardening 隔离） |
| `created_at` / `protocol_hash` | 见 manifest |

**关于 code 身份的诚实说明**：`code_commit` 记录 worktree HEAD（= 基线 commit）。
M4-H 新增的 runner/盘点脚本在工作树中**尚未提交**，因此单独记录**脚本内容指纹**
（`runner_fingerprint`，见 manifest 的 `code_identity`），它同时充当矩阵缓存的有效性锚点。

## 2. 窗口与数据

| 字段 | 值 | 说明 |
|---|---|---|
| `eval_start` | 2016-01-04 | 本地库最早交易日 |
| `eval_end` | 2025-05-30 | 污染窗口 2025-06-02 之前最后一个交易日，且位于 volume 单位断点之前 |
| `warmup_days` | 200 | 只为派生 `prev_close` 与滚动统计，不进日历 |
| `decision_step_days` | 4 | 每 4 个交易日取一个决策日 |
| 交易日数 | 2,284（日历） | 决策日 571 |
| `market_db` | `artifacts/warehouse/market.duckdb` | 只读打开，源库指纹见盘点 JSON |

决策日采样只降低**时间分辨率**、不改变横截面构成，因此不引入 symbol 选择偏差。

## 3. Fold Policy

复用 S19 `plan_folds`（`purged_walk_forward.py`），并做**最小泛化**：

| 字段 | 值 |
|---|---|
| `mode` | **expanding**（训练窗起点固定为首个决策日） |
| `min_train_days` | 504 个**交易日** |
| `test_window_days` | 60 个**交易日** |
| `step_days` | 60（相邻 fold 的测试块不重叠） |
| `calibration_days` | 20 个**决策日**（≈60 交易日） |
| `purge_days` | 15 交易日（= `execution_delay 1 + max_horizon 15 - 1`） |
| `embargo_days` | 15 交易日 |

**两处实现要点（必须记录，否则结论不可复现）**：

1. **fold 边界按交易日规划**。决策日是交易日历的采样序列；若直接对采样序列调用
   `plan_folds`，"504 个交易日"会被解读成"504 个决策日"（≈1512 交易日 ≈ 6 年），
   训练/测试边界整体错位（实测该错误会使 29 折退化成 4 折）。规划后把每个 test 窗内
   **实际存在的决策日**收进 `test_dates`。
2. **expanding 不修改 S19**。`run_fold` 以 `decision_date >= fold.train_start` 为训练下界，
   因此只把 `train_start` 改写为序列首日即可得到 expanding 窗口；
   purge / embargo / 成熟日上界（由 `train_label_mature_cutoff` 承载）等安全逻辑全部不变。

预期产出 **29 折**，测试窗覆盖 2018-02 ～ 2025-04，合计约 **435 个测试决策日**
（每个 test block = 60 交易日 = 15 个决策日）。

## 4. 防泄漏（Purge / Embargo）

- **第一道（日历口径）**：训练决策日上界 = `train_label_mature_cutoff`，使标签**成熟日**
  严格早于测试起点；train 与 test 之间另有 `embargo=15` 个交易日的空隙。
- **第二道（数据驱动，复用 S19 已修正逻辑）**：按**真实成熟日**列
  （`maturity_date_{max_horizon}d`）再剔一遍——日历口径假设"决策日 + purge 个交易日即成熟"，
  而实际成熟日按**标的自身 bar 序列**推进，停牌/停更标的会晚于日历口径。
  剔除行数如实计数（`maturity_purged_rows`）。
- **独立复核**：`overlap_leakage_check` 重新检查训练行的决策日与成熟日是否越过测试起点，
  违规计数进 `lookahead_violations`。**本协议要求 `lookahead_violations = 0`**，
  非 0 即判 `NO_GO_LEAKAGE`。

## 5. Calibration Policy（严格早于 Test）

顺序固定为：

```
TRAIN  →  CALIBRATION  →  PURGE / EMBARGO  →  TEST
```

- 校准窗从**训练窗尾部独立切出**（最近 20 个决策日），与训练窗**互斥**；
- **禁止**从 test block 抽取校准行（`calibration_in_test` 必须为 0）；
- 校准对象：方向头概率 `p_up_net_3d` / `p_up_net_5d` / `p_up_excess_3d` / `p_up_excess_5d`；
- 方法：isotonic（`multi_head.calibrate_direction`，其自带 train/calibration 重叠 fail-closed）；
- 无合法校准时如实写 `not_available`，**绝不伪概率**。

## 6. PIT Universe（复用 M1 S03）

- 每个历史决策日只用**该日已知**的股票池；
- 判定链路逐字复用 `stock_analyzer.data.asof_universe.resolve_asof_universe`
  （未来上市票因"无 ≤ as_of 的 bar"被硬排除；历史不足的票单列
  `insufficient_history_window_bars`；停牌票单列且不进覆盖率分母）；
- 统计部分用向量化索引（`PitUniverseIndex`）加速，**判定规则不变**；
  每次运行对拍 8 个决策日与 S03 原实现逐位一致，不一致即 fail-closed。
- `survivorship_coverage` 恒为 `incomplete_or_unknown`（库内无退市标的，见 §9）。

## 7. 执行契约（完全复用 M1/M2/M3）

| 项 | 值 |
|---|---|
| 决策时点 | T 日收盘后（15:30） |
| 入场 | **T+1 开盘**（`max_entry_sessions=1`，gap ≠ 1 记 `no_fill`，**不推迟**） |
| 执行价 | **raw**（`execution_price_mode=raw`；不得用复权价充当可成交价） |
| 成本 | 佣金 0.0003（最低 5 元）、印花税 0.0005（仅卖出）、过户费 0.00001，参考名义 100,000 → **往返费用率 0.00112**（实测 `round_trip_cost_rate()`：买 0.00031 + 卖 0.00081） |
| 滑点 | 0.0015 / 边（`static_slippage_ratio("trend")`，显式声明），**按价格生效**（买 ×1.0015、卖 ×0.9985），往返合计 ≈0.003 |
| 往返总摩擦 | 费用 0.00112 + 双边滑点 ≈0.003 → **≈0.00412** |
| 涨跌停 | 开盘价 ≥ 涨停价 → `limit_up_open`，不可买 |
| 停牌/缺口 | 下一根可观测 bar 不是次日 → `no_fill` |

禁止：T 日收盘价成交、用复权价当成交价、用未来价格判断可成交。

**费用率是全样本常数**（`compute_outcomes` 固定以 `trade_date=None` 调用，取配置默认
印花税率；研究链的限额规则未装载日期化费率表），而每层基准都是同一 `net_return_{h}d`
列的池内等权均值，因此 `excess = net − 基准` 里费用**精确抵消**。**不得**用
"超额收益 < 交易成本"论证经济性（该比较不成立）；经济性只能看**绝对净收益**。

## 8. Label 与 Feature Schema

- **Label**：`alpha_v2_label_v2.v1`；horizons `[3,5,10,15]`，primary=5，confirmation=3；
  训练标签 `alpha_target_5d` = 逐日横截面 `excess_return_5d` 的 rank 分位；
  评估指标列 `excess_return_5d`。
- **Feature Schema**：**M3 已验收的 120 列**（`feature_schema_hash` 见 manifest）。
  由 `feature_audit.safe_feature_columns` 从 `FeatureEngineer` 的 208 列裁出
  （asof 已证明 + 登记为 `in_base_v2`），实测与 `s14_validation.json` 的
  `audit.base_v2_feature_columns` **逐位一致**。
- **本阶段不治理近常数特征**：不得因为某年特征表现不好而临时删列/换填法/重调 feature
  （那会破坏 Locked OOS）。允许**报告** per-fold 的 feature coverage / constant rate /
  missingness drift。

## 9. Benchmark（三层冻结 + 简单基线）

| 层 | 口径 |
|---|---|
| `eligible_ew` | 决策集合自身等权（PIT 合格池） |
| `quality_pool_ew` | **主基准层**；研究侧代理规则（PIT 合格 + 当日 20 日平均成交额前 300），来源标 `research_proxy` |
| `style_matched` | 同板块 + 风格近邻（k=20，min_peers=5）对照，输出残差超额 |
| `simple_baseline` | **同日配对比较**（无训练、无超参、不消费标签），并给出 `compare_with_ml` 配对差 |

不得因为结果不好而更换 benchmark。

## 10. 指标与门

**核心指标**（至少）：Top1 / Top3 / Top5 × {3D, 5D, 10D, 15D} 的
命中率、平均净收益、中位净收益、平均/中位超额收益；Rank IC（mean/median/分布/置信区间，
含 moving-block bootstrap 与 Newey-West HAC）；分位单调性；top-bottom spread；
Winner Recall；MAE / MFE / tail loss / max adverse excursion；fill rate / no_fill rate。

- **Primary = 5D**；**Confirmation = 3D**；10D/15D 用于衰减研究。
- 概率校准指标：Brier score、ECE、可靠性分箱（无合法校准则 `not_available`）。

**分层**：按 calendar year 单独报告；按 fold 报告（含 `positive folds / total folds`）；
regime（bull/bear/sideways/high-vol/low-vol）**仅作为事后评估分层**，定义在看结果前冻结，
不得进入预测输入或事后改变模型。

**Historical Evidence Gates（H）**：

```
H20  >= 20 mature historical locked decision dates
H60  >= 60
H120 >= 120
H250 >= 250
```

**Live Gates（L）** 独立且不得由 H 推导：

```
L20 = L60 = L120 = L250 = AWAITING_DATA
```

`H250 reached` **不等于** `L250 reached`。

## 11. 不可变结果与工件结构

```
artifacts/alpha_v2/m4h/
  protocol/     协议清单（含 protocol_hash）
  folds/        fold_schedule.json + fold_<NNN>.json（逐折结果）
  predictions/  fold_<NNN>.json（逐折逐日逐票打分，键含 protocol_id + fold_id）
  metrics/      metrics_summary.json
  audit/        leakage_audit.json / run_manifest.json
  cache/        矩阵缓存（键含脚本指纹）
```

同键结果不得重写；不一致即 `HistoricalOOSRestatementError`。

## 12. 本阶段不授权

```
ALPHA_VERIFIED = FALSE
PRODUCTION_PROMOTION = LOCKED
```

即使 `H250 PASS`，也只说明"历史锁定验证表现支持/不支持 Alpha 假设"，
不自动 enable final selection、不 promote model、不 replace Legacy。
不得修改 Legacy 70 / Cross Review / 300-100-50 / Feature Set / Label V2 /
Benchmark Definition / 正式 serving。

## 13. 已知数据缺陷对证据等级的影响

M4-H 的证据强度受以下**实测**缺陷限制（详见 `M4H_Historical_Data_Inventory.md`）：

| 缺陷 | 影响 |
|---|---|
| 无退市标的（幸存者偏差） | **不可修复**；多年横截面结论只能表述为"在存活名单内" |
| 2025-09 起 volume 单位混合 | 主评估窗止于 2025-05-30 以规避 |
| 财务/背景列 99.93% 为同一快照日回填 | 非 PIT，不得声称当时可得 |
| `market_relative` 11 列 2024-03 前不可计算 | 按 fold 的特征覆盖漂移，须如实报告 |
| 涨跌停价仅 154 行有值、`suspended` 恒 False | 涨跌停与停牌由推导/缺 bar 承接 |
| 无 `pre_close`、无复权列 | 价格为 raw；除权日存在非交易性跳变 |

**证据分级**（`historical_evidence_inventory.json`）：

```
LOCKED_OOS_ELIGIBLE        2016-01-04 .. 2025-05-30   （contamination=UNVERIFIED_NOT_UNTOUCHED）
DEVELOPMENT_CONTAMINATED   2025-06-02 .. 2026-03-31
DATA_INCOMPLETE            2026-04-01 .. 2026-04-03
HISTORICAL_UNTOUCHED_HOLDOUT  —— 不存在（无法证明任何区间未被使用）
```
