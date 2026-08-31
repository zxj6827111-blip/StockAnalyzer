# Week5 回测与生产链路整改方案（最终修订版 · 2026-08-31）

> 本版本在 GPT5.6 修订版基础上，合入主代理对仓库/NAS 的逐项核实结果（见 §0 审计快照）。
> 2026-08-31 二次修订（Phase -1 定稿）：修正分支状态表述（6fda5ba 仅为 patch-equivalent、9f3c81 已在 main）；补充 7/30 半日数据 degraded/排除规则、最早有效日期与最小有效 fold 门禁、IC/分位收益/月度单调性的 date-block bootstrap 置信区间要求、原始样本/distinct symbol-date/有效资金槽位口径区分、Phase 1.5 诊断边界与 Phase 0 排期说明。
> 原则：**先验证、后放行**；模型身份、数据时点与交易评估口径修复前，生产链路保持 `fail-closed`，不放宽风控门。
> 前置报告：`docs/week5_backtest_report_20260831.md`（8/3–8/21 十五交易日回测验收）。

## 0. Phase 0 审计快照（2026-08-31 实测，本方案的事实基线）

| 审计项 | 实测结果 | 对方案的影响 |
|---|---|---|
| 分支盘点 | `feat/learning-protocol-remediation-0823` 相对 main：落后 54 commit、独有 1 commit 6fda5ba（weekend learning 门控 + predictor reload 延迟）。实测 `git merge-base --is-ancestor 6fda5ba main` = NO——**6fda5ba 仅为 patch-equivalent（`git cherry` 标 `-`、触碰文件 diff=0），不是 main 祖先，分支整体未合并：不得直接归档/删除，也不整体合并**；旧分支有效修复在 Phase 0 §3.1 逐项对照移植。v3.1 补救项 9f3c81（Commit A+B：e44 事件驱动 slot NAV 模拟器、C3 报告层去重、`unique_test_trade_dates≥20` 晋级硬门、baseline_bootstrap）**已在 main**（`merge-base --is-ancestor` = YES），不再标记为待实施；其余 v3.1 偏离项以 Phase 0 逐项审计结论为准 | Phase 0 分支审计改为"逐项对照移植"模式，不做分支归并；e44/报告去重已实施部分只做一致性验证 |
| Model Registry（NAS 实态） | ① 全部记录 `artifact_content_hash=''`（一致率 0%）；② 无 `role=champion` 记录，bootstrap 记录 `lifecycle_state=blocked`；③ 多个 model_id 指向同一 `/app/artifacts/model_v1.json`（身份与文件脱钩实锤）；④ runtime 经 `auto_load_predictor` 直接加载文件、绕过 registry（回测 `model_id=''` 的根因）；⑤ `dataset_manifest_id` 已填充（好基础） | registry 修复是 Phase 0 主体工作，非纯验收 |
| 训练样本 Universe | 快照库 90,103 条 / **1,557 只**（2026-04-05 起，非记忆中的 220 只）；但**单个训练 manifest 仅覆盖 152–496 只 / 5k–12k 样本**；评估 universe 为全市场 5,203→质量选 100。outcome 成熟率 94%（84,790 matured / 5,194 pending） | universe 错配确认存在但形态更新：不是"220 vs 全市场"，而是"每 fold 150–500 只历史候选 vs 每日全市场 100 强"。Phase 0 仍必须做"扩样本 or 缩评估"正式决策 |
| 数据覆盖 | **日线无 7/18–7/31 空洞**（此前结论证伪）：market.duckdb 日线 7/18–7/31 完整（约 5,169 行/日；7/30 半日 2,354 行；7/31 由同步脚本补齐）。真实空洞在**分钟线**：7/20–7/30 仅 36 票/日、7/31–8/3 为零、8/4 起正常。delta 日线 max=8/28=最后完整交易日（8/29–30 为周末），**数据完全新鲜，updater 无缺口** | walk-forward 的 3–6 月窗口日线特征干净；7/20–8/3 的 fold 仅 intraday 因子 degraded；"8/29 缺口"撤销 |

## 1. 目标与问题陈述

修复四类问题（全部已验证成立）：

1. **回测训练时间穿越**：8/16 训练模型被用于 8/3–8/14 选股；`lookahead_bias` 在 `asof_backtest_service.py` 硬编码为 `true`；历史 runner 无按日期选模型逻辑。
2. **漏斗统计与实际处理不一致**：`funnel.deep_count=100` 实为 light 池数（`deep_stage.selected_count=20`）；`universe_count=100` 实为质量目标数（真实 5479→5203→100）；终门 `input_count=100` 接收 light 池而非 deep 输出。
3. **终门配置致 15 交易日零成交**：过热门（bias_reject_min=0.15 绝对阈值）与动量选池逻辑结构性死锁；`final_signal_min_threshold=70` 高于候选分数主体分布（58–70）。
4. **收益评估为简化毛收益**：逐笔求和、无成本、无仓位/重叠约束，只能作诊断，不能作放行依据。

## 2. Phase 0：训练基础、Universe 和分支状态硬门禁

任何一项未通过不得进入重训。

### 2.1 标签链路审计

- 核对 `label_anchor_time`、entry price、horizon、settlement lag、`label_mature_time`（`time_semantics.py` 不变量已存在，以审计+跑通为主）。
- 修复 outcome 标签锚点及 TP/SL 冲突标签（`build_soup_labels` 已有 `conflict_policy/conflict_soft_label_value` 参数，重点是策略审计与 backfill）。
- 受影响历史 outcome 重新 backfill；新旧标签生成差异报告（变更行数、正负比例、冲突比例、成熟时间分布）。
- 每个 label 记录 `label_policy_id/hash`、anchor/mature time、`horizon_days`、`settlement_lag_days`、`conflict_policy`、`source_data_cutoff`；旧新标签不得混用。
- v3.1 补救项 9f3c81（Commit A+B）已在 main：e44 复利口径已改为事件驱动 slot NAV 模拟器、报告层已纵深防御去重。本项不再重复实施，仅审计其对 outcome/NAV 语义的一致性（legacy_* 逐笔口径仅保留作对照，不入正式结论）。

### 2.2 Model Registry 修复与审计

- 训练生成唯一不可变 `model_id`；禁止覆盖旧 artifact alias；旧 artifact 迁移为 content-addressed bundle 保留回滚。
- 回填并强制校验 `artifact_content_hash`：registry record ↔ artifact 文件 ↔ manifest ↔ feature schema 四层一致。
- `active_champion_id` 必须指向存在且 hash 校验通过的 artifact；**废除 `auto_load_predictor` 绕过 registry 的直载路径**（或改为"直载前必须通过 registry 校验"）。
- 清理空 `model_id`、`artifact_overwritten`、registry 指向旧文件三类脏状态。
- 本地、容器、NAS 三层分别核对 registry record / artifact 文件 / content hash / active champion / `/health` 运行版本；NAS 以 runtime 实态为准，不以分支名推断。

### 2.3 训练样本 Universe 覆盖审计与决策

每个训练/评估窗口记录：

```text
training_universe_size / evaluation_universe_size
training_symbol_date_count / evaluation_symbol_date_count
symbol_overlap_ratio / trade_date_coverage_ratio
universe_source / universe_snapshot_hash / selection_ruleset_id
```

- **默认推荐扩展训练样本 universe** 至模型实际评分阶段的候选分布（全市场质量筛选后的候选集，而非机械的 5479 全量）。
- 若模型目标为全市场横截面排序，训练必须覆盖全市场有效样本。
- 临时缩小评估 universe 到历史候选池只作诊断，报告必须标注 watchlist scope，不得外推为全市场生产结论。
- **Phase 0 必须完成"扩样本 or 缩评估"的正式选择，否则 Phase 2 不得启动。**

### 2.4 数据覆盖审计

- 日线：3–8 月窗口已确认完整（§0）；7/30 半日数据（2,354 行）**处理规则：优先尝试补齐；无法补齐时，依赖完整日数据的相关特征标记 `degraded`，依赖该完整日的 fold 整体排除，禁止静默填补**。
- 分钟线：7/20–8/3 空洞（36 票/日→零）——相关 fold 的 intraday 因子标记 `degraded`，禁止静默填补或混入主结论。
- 新鲜度门禁：以"最后完整交易日 + updater freshness"为准（8/29 为周六，不构成缺口）。
- 交易日历校验、symbol-date coverage、特征覆盖率、标签成熟率全量出表。

### Phase 0 验收

- 标签时间不变量违规 = 0；受影响 outcome 已重建或明确排除；
- active champion、artifact、registry、hash 100% 一致；`model_id` 非空率 100%；未处理 `artifact_overwritten` = 0；
- Universe 口径选择已落文档；数据缺口已剔除/标注/修复；
- NAS runtime 能读取有效 champion；
- **未通过时禁止进入任何正式 walk-forward。**

## 3. Phase 1：时间安全的模型选择与候选打分评估

模型可用规则（交易日口径，禁用自然日相加）：

```text
max(train_sample.label_mature_trade_date) + embargo_trading_days < evaluation_trade_date
```

- 优先用实际 `label_mature_time`；缺失时按 `label_anchor_time + horizon + settlement_lag` 推算。
- 现有 PR#19 embargo/purge 机制为强制路径；训练/校准/测试三集合均按 label maturity 分组 purge。
- `walk_forward.py` 现状（单标的、`decision_time=now()` 全局成熟过滤、train/test 切片间无 embargo gap）不满足要求；Phase 1 交付 per-fold 成熟期 purge 与边界 gap（该文件保留为单标的诊断工具）。
- 无合格模型 → 输出 `blocked/invalid`，禁止 fallback 冒充有效模型。
- 回测结果记录 `evaluation_validity`、`invalid_reason`、`model_id`、`training_cutoff`、`embargo_days`；`lookahead_bias` 改为计算值，发现未来模型/数据/特征即标无效。

仅评估打分层：IC/Spearman、AUC/Brier/校准、分数分位收益、Precision@K、top/bottom 单调性、相对基准、universe 覆盖率、模型身份与时间完整性。
不要求：`final_count>0`、组合 NAV、正式成交、生产放行。
`fallback_top5` 仅作诊断样本，不入正式收益统计，不解释为生产信号。

## 4. Phase 1.5：终门诊断实验（提前，仅诊断环境）

前置：Phase 0 + 最小时间安全检查通过。先计算最早可评估日：

```text
earliest_valid_eval_date = max(train_sample.label_mature_trade_date) + embargo 之后的第一个交易日
```

- 若可用窗口 < 10 个交易日，不单独形成结论，并入 Phase 2 首个有效 fold；不得用"7 月底截止模型"强行覆盖 8 月上旬。
- 实验矩阵：bias `0.15/0.20/0.25` × ATR 距离 `3/4` × score floor `60/65/70` × top-K `3/5/10`。
- 必须输出：`light_count`、`deep_input_count`、`deep_scored_count`、`rerank_count`、`final_count`、首个拒绝阶段统计、全部命中原因统计（并说明是否互斥）、breadth block / 数据无效 / 模型无效标记。
- 生产配置不变、不计正式收益、不触发自动晋级。
- **该矩阵仅为终门死锁的诊断工具：探索性多重比较结果，不从中挑选"最佳参数"进入生产，不计入正式收益，不触发自动晋级；生产参数调整必须走 Phase 2/3 的预注册评估流程。**

## 5. Phase 2：新建横截面 Walk-Forward Harness

**不复用 `walk_forward.py` 作为主 harness**（它是单标的 trend 引擎）。新增横截面 harness，按交易日：

```text
Universe snapshot -> feature snapshot -> label maturity purge -> model training
-> cross-sectional scoring -> candidate ranking metrics -> optional gate diagnostics
```

必须支持：每日 universe snapshot、每日训练/评估时间边界、maturity+embargo purge、多股票横截面排名、训练/评估 universe 统计、fold checkpoint、fold 失败重跑、score-level 与 execution-level 结果分离、不可变产物、不覆盖历史模型/报告。

- 窗口暂定 2026-03 ~ 2026-08-28，受 §2.4 审计约束；7/20–8/3 分钟空洞相关 fold 剔除或标 `degraded`；7/30 半日数据按 §2.4 规则处理。
- **启动前必须先计算：最早可训练日期（按真实数据覆盖与标签成熟时间）、最早合格评估日期（label maturity + embargo 之后的第一个交易日）、完整 fold 数、degraded fold 数。**
- **最小有效 fold 门禁：至少 4 个完整、无关键数据污染的有效 fold；不足 4 个时只能输出诊断报告，或经书面决策扩展至更早历史窗口（须重审特征质量与 universe 一致性），不得降低门槛进入 Phase 3。**
- fold 数由单 fold benchmark 实测决定（默认 `train_window=120/test_window=20/step=20`，不预设 24 次）。
- 放行判据以打分层为主：aggregate IC > 0；top 分位 ≥ bottom 分位；≥4/6 自然月排序不反向；**IC、分位收益、月度单调性均须给出 95% 置信区间（date-block bootstrap，以交易日为 block 单位）；若指标方向正确但置信区间跨过 0，结论为 `INCONCLUSIVE`，不得进入 Phase 3**；训练/评估 universe 分布已对齐；lookahead 违规 = 0；相对基准无显著劣化。
- 无 final signal 不判 Phase 2 失败（终门可能仍关闭），只说明候选层有效性未传递到终门层。

## 6. Phase 3：组合级执行回测与终门选择

仅当 Phase 2 候选排序通过后进行。使用 `ExecutionMatcher` + `HoldingCurve`，纳入成本/滑点/涨跌停/停牌/T+1/止盈止损/持仓重叠/资金占用，输出组合 NAV、净收益、最大回撤、换手率；诊断毛收益与执行净收益分开报告；终门参数实验仅在 shadow/backtest 环境。

成熟样本三级标准（**三个口径必须分开统计与报告：原始交易样本数（含同票/同日重复入选）、distinct symbol-date 数（去重后的独立观测）、有效资金槽位数（受 T+1、持仓重叠、资金占用与组合容量约束后实际可建立的独立头寸数）；统计功效判定以 distinct 口径为准，原始样本数不得单独作为生产放行依据**）：

- < 60：只输出工程/诊断结论；
- 60–99：可输出初步策略判断，标"统计功效不足"，不得生产放行；
- ≥ 100：允许较强盈利性判断，但仍须通过有效独立样本与月度稳定性检查。

若 2026-03~08 达不到 60 个成熟样本，再评估扩展至 2025-09（须重审特征质量、universe 一致性、历史完整性，不因"数据存在"直接纳入）。

正式放行条件：扣成本后不弱于全市场基准；≥4/6 自然月相对基准不为负；最大回撤 ≤ 基准+5pp；成本 +50% 压力测试方向不反转；非 breadth-block 日不再长期 `final_count=0`；信号可全程通过执行模拟且可追溯；**不允许用降阈值掩盖模型排序无效**。

## 7. Phase 4：NAS 部署与 20 交易日 Paper/Shadow

部署前：本地代码/测试/回测/产物校验完成；独立回测输出目录与数据库快照；不与生产 updater/night_scan 共用写锁；核对 repo HEAD、容器 build commit、`/health`、active champion、artifact hash。

部署后：连续 20 个交易日 paper/shadow，不真实下单；每日记录模型身份、数据新鲜度、universe、候选、终门拒绝、模拟成交、净收益与风险指标；身份丢失/数据时点错误/hash 不一致/执行异常 → 立即阻断 promotion；20 日后结合 OOS 与执行报告决定是否进入真实交易。

## 8. 算力与排期

| 阶段 | 排期 |
|---|---|
| Phase 0 | 1 个周末只读审计；**backfill、registry 修复与 NAS 三层核验需独立运行窗口，不保证与审计同一周末完成**，按实际运行窗口排期 |
| Phase 1 + 1.5 | 1 个周末 |
| Phase 2 | 2 个周末 |
| Phase 3 | 1 个周末 |
| Phase 4 | 4 个自然周 paper/shadow |

总计约 5–6 周开发+计算，加 4 周 paper，**约 2–2.5 个月**得到可信结论。

正式批量运行前完成单 fold benchmark：重训耗时、单交易日横截面回测耗时、内存峰值、DuckDB 锁占用、失败恢复耗时。
资源约束：api 容器 4GiB 上限，**3.2GiB soft cap / 3.6GiB hard stop**；重训与全市场回测避开 21:30–23:00 sync/night_scan cron；同一时间仅一个重型横截面任务。

## 9. 风险假设（非验收标准）

- 现有打分模型族在无泄漏 OOS 下通不过 Phase 2 打分层门槛是**大概率风险**（8 月泄漏样本内已呈负相关；训练 universe 150–500 只与评估分布错配；8/23 定性的 AUC 0.33 疑点未闭环）。此判断仅作排期与预期管理，最终结论以无泄漏数据为准。
- 若通不过：整改产出为可评审的 NO-GO 证据 + 转向决策（扩训练 universe 重训 vs 换信号族，如把已验证的动量延续显式建模），而非在失效模型上继续调参。

## 10. 交付顺序

1. 本文档（已完成）。
2. Phase 0：标签、registry、universe 决策、数据覆盖四类硬门禁。
3. Phase 1/1.5：时间安全模型选择 + 打分层评估 + 终门诊断矩阵。
4. Phase 2：横截面 harness + 6 个月 walk-forward。
5. Phase 3：组合级执行回测与终门选择。
6. Phase 4：NAS 部署 + 20 交易日 paper/shadow。
7. 全部通过前，生产保持 NO-GO，不放宽过热门与分数门槛。
