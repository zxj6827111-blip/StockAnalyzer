# Week5 Phase 1 时间安全打分评估报告（2026-09-05 NAS 首轮）

> 方案依据：`docs/week5_backtest_remediation_plan_20260831.md` §3（时间安全的模型选择与候选打分评估）。
> 执行：`scripts/week5_phase1_scoring_eval.py`（dab3813 模块 + 17cb0a7 去重修复），NAS 实库，2026-09-05 17:13/17:28 两轮。
> 明细工件：`docs/week5_phase1_scoring_eval_20260905.md/.json`（去重后正式轮）；容器 `/app/artifacts/phase1/`（含全部轮次，时间戳命名不覆盖）。

## 1. 评估设置

| 项 | 值 |
|---|---|
| 评估窗口 | 2026-06-24 ~ 2026-08-28（48 个交易日） |
| 时间安全规则 | `max(label_mature) + 11 交易日 < eval_date`（horizon 10 + settlement 1，交易日历口径） |
| manifest 资格 | 25 个中 22 个合格；3 个因缺 outcome 判 invalid（50ec7236be71/a419320c8c08/f729d692fcc0） |
| 按日选型 | 每个交易日选「资格满足且训练截止最新」的 manifest；实际使用 3dddbc7034ef（1 天）+ af1a30ff4887（34 天） |
| 训练 | `train_on_dataset_manifest`（manifest 声明的 schema/policy），单次 ~6.5s，按 manifest 缓存 |
| 打分 | `SignalPredictor.predict_rows`（meta 概率），横截面按 symbol 去重（每 symbol 取当日末次快照） |
| 标签 | `realized_return` 符号（诊断口径，TP/SL policy 标签留 Phase 2）；IC/分位收益用连续 realized_return |

## 2. 首轮结果（去重后正式轮）

| 指标 | 值 | 解读 |
|---|---|---|
| 有效评估日 | 35/48（13 日无快照捕获） | 快照库捕获是周期性而非逐日 |
| **日 IC（Spearman）均值** | **+0.239**，CI95 **[+0.159, +0.316]** | **置信区间不跨 0**；35 日中 28 日为正 |
| 池化 AUC / Brier | **0.624** / 0.276（n=1,820 symbol-days） | 方向性判别力存在 |
| 五分位收益（10 日 horizon） | Q1 -4.6% / Q2 -5.8% / Q3 -1.1% / Q4 +1.1% / **Q5 +1.8%** | top-bottom 价差 +6.4%；Q4>Q3>Q5 之外 Q1≈Q2 底部非严格单调 |
| 池化均值收益 | -2.1% | 评估窗口候选池整体下行，模型排序仍分离出正收益组 |
| lookahead_bias（计算值） | 全部 False | 资格门 + 快照 decision_time 一致性均通过 |

**去重前后对比（同一评估器）**：去重前（每 symbol ~7.5 次日内重复捕获，1,100-1,200 行/日）日 IC 均值 0.082、CI [-0.018, +0.176] 跨 0、池化 AUC 0.559；去重后（~159 只/日）IC 0.239、CI 不跨 0、AUC 0.624。**日内重复捕获把 IC 拉低约 2/3**——重复行让高捕获频次 symbol 过权重。此发现对 Phase 2 的 universe 快照生成是硬约束：必须按逻辑键（symbol × 决策日）去重。

## 3. 结论与边界

1. **方向性证据为正**：在时间安全约束满足（模型训练数据成熟截止 + embargo 早于全部评估日）的前提下，模型对候选池横截面有正的排序能力（IC CI 不跨 0、AUC 0.62、top 分位收益显著高于 bottom）。8 月回测「分数-收益负相关」的结论在去重后的干净口径下**不再成立**（此前负相关的可能来源：重复行加权 + 未去重的池化口径）。
2. **按方案 §5 放行标准衡量仍为 INCONCLUSIVE→需 Phase 2 正式复验**，理由：
   - 评估 universe 是**历史候选池快照**（日覆盖 0.1%-23%），不是全市场 PIT 质量筛选（B10 决策的扩样本尚未实施）；
   - 标签是诊断口径（realized_return 符号），非 TP/SL policy 标签；
   - 有效样本 1,820 symbol-days、35 个有效日，date-block bootstrap CI 已算但月度单调性检验未做（Q1≈Q2 底部非单调）；
   - 2 个 manifest 承担全部评估（非逐 fold 滚动重训）， Phase 2 的 `train_window=120/step=20` 滚动才是正式口径。
3. **数据工程前置项（Phase 2 启动前）**：25 个 manifest 中 4 类不可用——3 个缺 outcome、4+ 个 train-only split、92% 重复逻辑行（af1a30ff4887 实测）；Phase 2 harness 必须自带逻辑键去重与 manifest 完整性门（缺 outcome 即拒绝，与 Phase 1 资格判定一致）。

## 4. Phase 1.5 / Phase 2 输入

- Phase 1.5 终门诊断矩阵可直接复用本评估的按日选型 + 打分输出（在诊断环境跑 bias×ATR×score floor×top-K 网格，不计正式收益）。
- Phase 2 harness 需要补的三件事：全市场 PIT universe 快照生成（B10 决策落地）、per-fold 滚动训练（walk_forward 逻辑键去重）、policy 标签口径 + 月度单调性 + bootstrap CI 全套放行统计。
