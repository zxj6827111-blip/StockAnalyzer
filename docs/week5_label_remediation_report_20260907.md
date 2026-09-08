# Week5 方向一' 实施报告：label 改收益排序语义 + NaN 特征接入 + 18-fold 重验（2026-09-07）

> 分支 `feat/week5-label-remediation-0907`。上游定案见
> `docs/week5_feature_attribution_20260907.md` §6（双口径诊断：label/评估口径错位）。
> 本文记录实施内容、验证数据与验收硬门判定，供复核会话按任务书「验收清单」逐项检查。

## 1. 结论（先读）

**验收硬门 PASS**：return_rank label 下模型分数 IC = **+0.0658**，
date-block bootstrap 95% CI = **[+0.0532, +0.0791]**，CI 下界 > 0
（任务书硬门：下界 > 0，不只是均值转正）。

对照行（同一 harness 配置 train=120/test=20/step=20/embargo=11，
同一窗口 2024-09-02 ~ 2026-08-28、同一 5571 只源 universe）：

| label | aggregate IC | 95% CI | 判定 |
|---|---|---|---|
| 旧 soup TP/SL（2026-09-06 Phase 2） | **-0.024** | [-0.043, -0.005] | NO-GO |
| **新 return_rank（本报告）** | **+0.0658** | **[+0.0532, +0.0791]** | **硬门 PASS** |

18/18 fold completed、lookahead violations = 0、每 fold 20 个评估日
（fold 18 为 2 日——窗口尾部截断，与旧 run 同形态）、评估日并集 352 日
（与归因扫描完全同口径）。

verdict 仍为 `INCONCLUSIVE`：月度单调性判据（≥4/6 月 top-bottom 为正）
为 11/18 不达标——**但这是 Phase 3 组合层判据，不是本任务验收硬门**
（任务书硬门只要求 IC CI 下界 > 0）。月度明细：2026-02/04/05/06/07 为负
（2026-04~06 正是归因报告 §3 指认的动量 regime 窗口，return_rank 也没能
完全逃逸该时段），其余 11 个月为正。

## 2. 任务 1：return_rank label（新 policy，旧 policy 零改动）

- **`src/stock_analyzer/labels/return_rank.py`（新增）**：
  `build_return_rank_labels` —— 以 fwd_return（T+1 开盘入场、horizon 期末
  收盘，与 IC 评估同公式）为基础的**同一交易日横截面分位**：top 30% → 1、
  bottom 30% → 0、中间段 `drop_middle=True` 记 NaN（选择理由：横截面
  ~5000 只下中间 40% 是最大的沉默多数，标 0.5 只会稀释梯度并让 isotonic
  校准器在 0.5 处堆积；剔除后 top/bottom 各 ~50 万行样本量充足）。
  平秩用平均秩（与 Spearman IC 口径一致）；截面 <30 只整日 NaN。
- **时间锚点零改动**：`label_mature_trade_date` 公式与 soup 完全同源
  （`_mature_of`：entry=dec+1，mature=entry+horizon-1）；T+1 开盘入场、
  horizon=10、embargo=11 全部沿用——只换 label 公式。
- **无前视论证**：分位只依赖当日截面的 fwd_return；fwd_return 本身是未来
  窗口数据，但它只进训练目标，训练/评估隔离由既有 maturity purge + embargo
  保证（与 soup 标签使用未来 high/low 同级别的前视需求）。实测
  lookahead_violations = 0。
- **registry 版本化共存**：`build_return_rank_policy_record`（schema v3，
  `label_policy_v3_ebe9dfacae62`，conflict_policy=rank_quantile 占位）+
  `LabelPolicyRegistry.register_return_rank`；TP/SL 字段以 0 占位登记
  （v3 不消费路径信息）。v1/v2 soup policy 不动，`LabelsConfig.basis`
  默认 `"soup"`（default.yaml 同步）——生产 night_scan/asof 回测行为
  零影响（label policy_id/hash 绑定链未触碰）。
- **PIT 数据集**：`generate_pit_dataset(label_basis="return_rank")` 时
  分片阶段只落 fwd_return，月度合并阶段（整月完整截面就绪后）逐日计算
  分位回写 label 列——分片内逐票无法算横截面分位，必须等全截面。

## 3. 任务 2：98 个 NaN 特征接入 PIT 快照链

**根因定性（先查清再改）**：不是"生成器没读列"，是 **v1 生成器的
`_fetch_symbol_bars` 只 SELECT 12 个价格/状态列**——FeatureEngineer 消费
的背景/资金/财务列（daily_bars 里 DDL 全在、9/5 回填后有值）、分钟
summary、基准指数从未进入 bars 帧；缺列经 `_optional_numeric` → NaN →
末端 `fillna(0)` 变成常量 0 列 → 归因扫描记 NaN。

改造（`pit_dataset.py` 重写）：
1. `_fetch_symbol_bars` 一次性拉齐 16 个背景/资金/财务列
   （holder_count、北向、融资、大宗 7 列、moneyflow、hk 2 列、inst、
   roe/debt_ratio、board、background_data_complete）——显式列清单
   fail-closed，不 SELECT *；
2. `_fetch_intraday_panel`：intraday_summary_1m/5m 窗口级批量拉取
   （symbol 分批 IN 1000/批，非逐票 SQL），注入 `engineer.transform`;
3. `_fetch_market_index`：index_daily 基准指数（000300 优先），
   excess_ret/rs_ma/rolling_beta 等 11 个市场相对特征族激活。
   **顺带修了一个真 bug**：index_daily 的 index_code 带交易所后缀
   （`000300.SH`），裸代码 `IN ('000300',...)` 永远查空——改为前缀
   剥离匹配（NAS 实测 472 行 000300 可读出）。

PIT 纪律：财务列（roe/debt_ratio）读行内已按公告日 as-of 物化的值
（9/5 `enrich_daily_financial_pit` 修复产物），不重算 as-of join；
历史无数据的字段如实保留 NaN，不假填充。

**副产物（真缺陷修复，仓库代码）**：`intraday_summary_1m/5m` 表与
`_INTRADAY_COLUMNS` 写入端只落 12 列，`summarize_minute_bars` 产出的
后 8 列（above_vwap_ratio、price_efficiency、tail_volatility_ratio 等）
从未进表——已补齐（表 DDL 迁移 `ADD COLUMN IF NOT EXISTS` + 列清单
扩展），历史 8 列回填依赖 QQ 链路幂等重跑（属独立改造，未越界实施）。

## 4. 新数据集与特征覆盖（诚实口径）

`pit_dataset_rank`（新目录，旧的 4.6G `pit_dataset_ext` 保留未动）：
rows=2,405,272 / symbols=5,191 / trade_dates=482（与旧数据集同量级，
差异可解释：同源同窗同过滤）；positive_rate=0.5016（top/bottom 平衡）；
matured_rows=1,406,073（drop_middle 剔除中间 40%）；生成 2976s，
峰值 RSS 827MiB（3GiB 限额安全）。

**98 特征覆盖率（`nan_feature_coverage.json`，指标口径经修正——
nonnull_rate 被 FeatureEngineer 末端 fillna(0) 完全掩蔽，改用
nonzero_rate + n_distinct 分档）**：

| 档 | 数量 | 含义与代表 |
|---|---|---|
| real_data（nonzero>1% 且 distinct>10） | **65** | bg_roe（99.9%）、bg_block_trade_net10（53%）、financing_balance_chg_5（77%）、northbound_net_20（26%）、i1m_session_return（18%）、excess_ret_5/rolling_beta_60（98%+） |
| partial（有真值但覆盖薄） | 11 | block_trade_amount 族（0.1-0.2%，源 2026-04 后断供）、moneyflow（110 个值，专表仅 2 票）、inst_net_amount（52 值）、bg_board_code/market_trend（真实但低基数） |
| constant_fill（仍无数据，如实保留） | 22 | hk_hold 族（hk_hold 表 0 行）、i1m/i5m 的 8 个新列 ×2 间隔（写入端缺陷历史数据无列）、dragon_tiger_freq/premium（源列全空或极薄） |

即：**98 个特征里 65 个从全 NaN 变为真实数据，11 个获得部分覆盖，
22 个如实保持无数据**（其中 16 个在写入端缺陷修复 + QQ 回填落地后会
自然转正；hk/inst/moneyflow 族受上游断供约束，属数据链路独立问题）。

## 5. 任务 3：18-fold 重训重验（NAS 实测）

- 执行：scheduler-critical 容器，凌晨 01:30-01:35（避开 21:30-23:00
  cron 窗口），185s 完成 18 fold，RSS 峰值 < 800MiB。
- 逐 fold IC：18 个 fold 中 16 个为正（最强 fold 7 +0.174、fold 12
  +0.159、fold 18 +0.167）；fold 6 -0.010、fold 14 -0.110（2026-04~
  06 动量 regime 段）为负。
- 池化指标：AUC 0.527（vs soup label 的 0.586——注意口径：分位 label
  与 IC 天然一致，AUC 基线意义已变化，不再可比）、Brier 0.252、
  分位收益 top 1.47% vs bottom 1.00%（q1 反常 1.0% 高于 q2 0.36%，
  但 top-bottom 价差方向正确）。

## 6. 若未达标的排查清单（未触发，留档）

硬门已 PASS，此清单未启用。留档供后续 regime/终门层分析复用：
1. 月度单调性 11/18（2026-02/04/05/06/07 负）——动量 regime 段的
   return_rank 失效是否与归因报告 §3 的 2026-04~06 窗口重合（是）；
2. fold 14（-0.110）深查：训练窗尾部是否恰为动量正 IC 月；
3. top 分位 q1 1.0% 与 q2 0.36% 的非单调——极端收益尾部效应；
4. 22 个 constant_fill 特征在 QQ 回填落地后的重训增量。

## 7. 工程纪律执行记录

- 内存：全链路实测峰值 827MiB（生成）/< 800MiB（训练），3GiB 限额内；
- 执行窗口：生成 23:38-00:30（cron 窗口结束 + 8 分钟后启动）、
  重训 01:30-01:35；
- 容器同步：宿主 git checkout → SFTP → docker cp 三跳（镜像未重建，
  正式部署走 `nas_deploy_update.sh`，属合并后动作）；
- 测试：本地新对抗测试 18 项 + phase2/phase0/labels/registry/config
  回归 54 项 + warehouse/intraday 26 项全绿；ruff 全过；mypy 与 main
  基线对比**内容级 new=0**（行号级剩余 4 条为既有 `_row_to_record`
  错误的位移）；
- 凭据：全程走 `~/.kiro/nas_credentials.json`（git 忽略域），不入
  任何文件/命令行历史；backfill_tmp 草稿目录未入库。

## 8. 复核会话对照（任务书验收清单）

- [x] label_policy_registry 新 policy（v3）注册 + hash 绑定 + 对抗测试
      （时间不变量/横截面无前视/中间段边界，`tests/test_week5_label_remediation.py`）
- [x] 98 特征覆盖率显著 >0：65 real + 11 partial（逐特征 JSON：
      `phase2_label_remediation/nan_feature_coverage.json`）；
      22 个 constant 保留并逐项说明（hk/inst 源空、intraday 新 8 列
      写入端缺陷历史无数据、premium/dragon 源列空）
- [x] pit_meta.json 同量级（2,405,272 / 5,191 / 482 vs 旧版完全一致；
      positive_rate 0.50 vs 0.31 可解释：分位 label 天然平衡）
- [x] 18-fold 报告：IC +0.0658 CI [0.0532, 0.0791]、对照行
      soup -0.024 [-0.043, -0.005]、lookahead violations = 0
- [x] 硬门判定行：`hard_gate.pass = true`（IC CI 下界 0.0532 > 0）
- [x] 生产链路零影响：`LabelsConfig.basis` 默认 soup；旧 policy/registry
      零改动；新数据集/报告全部独立目录
- [x] 测试套件全绿 + ruff/mypy 基线无新增（见 §7）

## 9. 产物清单（NAS `/app/artifacts/phase2_label_remediation/`）

- `pit_dataset_rank/`（3.6G，pit_meta.json + 24 月度块 + shards）
- `nan_feature_coverage.json`（98 特征三档覆盖明细）
- `walkforward/phase2_walk_forward_20260907T172653Z.json`（18 fold
  完整报告，含 baseline_soup_label 对照行与 hard_gate 判定行）
- `walkforward/checkpoints/fold_01-18.json`（逐 fold checkpoint）
- 日志：`phase2_label_remediation_generate.log`、`wf_rank.log`
