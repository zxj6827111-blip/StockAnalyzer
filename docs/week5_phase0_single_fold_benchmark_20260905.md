# Week5 Phase 0 单 fold benchmark 报告（2026-09-05 NAS 实测）

> 对应方案 §8：「正式批量运行前完成单 fold benchmark：重训耗时、单交易日横截面回测耗时、内存峰值、DuckDB 锁占用、失败恢复耗时」。
> 执行环境：NAS api 容器（4GiB 上限 / 3.2GiB soft / 3.6GiB hard stop），周六 14:00–16:30 非生产窗口。
> fold 口径：一个 dataset manifest = 一个历史训练 fold（训练样本窗口约 4–5 周）。

## 1. 单交易日横截面回测（week5_daily as-of 2026-08-28）

| 项 | 实测 |
|---|---|
| 调用方式 | 生产 API `POST /backtest/asof-scan`（后台任务，生产进程内执行） |
| 任务时长 | 提交 2026-09-05T07:20:55Z → 完成 07:28:41Z = **466s（7.8 分钟）** |
| 内存（任务尾段） | 容器 cgroup memory.current = **2,647 MiB**（基线 ~450 MiB → 任务增量约 2.2 GiB） |
| 内存（容器峰值水位） | cgroup memory.peak = **4,096 MiB = 打满 4GiB 上限**（当日水位含上午 B4 backfill 与本次任务贡献，无法完全归属） |
| 漏斗 | provider_index 5,482 → as_of_valid 5,211 → selected 100 → light 100 → deep 100 → **final 0**（终门关闭，与 8 月回测验收结论一致） |
| 锁冲突 | 0（15:20–15:28 非生产窗口，无 night_scan/updater 并发写） |

**容量结论**：单日全市场横截面回测在 api 容器内运行时，容器内存逼近 4GiB 上限。Phase 2 批量 walk-forward（连续多日）**必须在独立容器/独立内存限额下执行**，且避开 21:30–23:00 生产 cron；否则有 OOM kill 或与 night_scan 抢锁风险——§8 资源约束经实测确认必要。

## 2. 单 fold 重训（train_on_dataset_manifest）

按 `included_snapshot_count` 降序遍历 manifest，逐个尝试训练，首个成功者为计时样本：

| manifest | 快照数 | 结果 | 耗时 |
|---|---|---|---|
| dataset_manifest_v1_c782b2a32a7e | 34,084 | ❌ `training split produced empty train/calibration/test set` | 4.2s |
| dataset_manifest_v1_776f6d399d2b | 34,084 | ❌ 同上 | 3.7s |
| dataset_manifest_v1_50ec7236be71 | 15,977 | ❌ `outcome missing for manifest item: snap_20260720100046_3ee24563` | 5.4s |
| **dataset_manifest_v1_af1a30ff4887** | **12,324** | ✅ | **train 6.77s** |

成功者详情（窗口 2026-04-20 ~ 2026-05-25）：

| 项 | 实测 |
|---|---|
| 训练耗时 | **6.77s**（lightgbm + xgboost 双后端，12,324 样本 × 222 特征） |
| 进程内存峰值 | ru_maxrss **1,591 MiB**（含前面 3 次失败尝试的累积；单次训练峰值更低） |
| 配置加载 / manifest 扫描 | 0.06s / 0.06s |
| split | train 9,860 / calibration 1,232 / test 1,232（manifest 预分） |
| 指标 | test AUC 0.562、Brier 0.178、Precision@K 0.268、正样本率 6.1% |
| **重复逻辑行** | **11,369 / 12,324 = 92.2%**（distinct 逻辑样本仅 955；unique trade dates 20） |
| 标签 | hard 3,131 正 / 9,193 负；soft_label_count = 0（v1 口径 manifest） |

**三个失败模式是 Phase 2 fold 规划的硬证据**：
1. 两个最大 manifest 是 **train-only**（split 只有 train，无 calibration/test）——不能单独作为 fold 使用；
2. 8/16 的 manifest **引用了无 outcome 的快照**（manifest 完整性缺陷）；
3. 成功 manifest 的 92% 行为重复逻辑行——历史候选池快照的重复录入问题（此前 e44 口径已定性），Phase 2 的 universe 快照生成必须带逻辑键去重。

## 3. DuckDB 锁占用（间接实测）

- 训练侧：每 manifest 的 store 读段（manifest items + snapshots + outcomes）3.7–5.4s 内完成，多次短连接 open/close；训练计算段完全不持锁。**单 fold 训练对协议库的写锁占用 < 6s 且分片**。
- 回测侧：生产进程内运行，未单独测量持锁区间；15:20–15:28 非生产窗口全程 0 锁冲突。
- 对照（B4 wrapper 实测）：白天块平均 5 次锁冲突/块，夜间 0 次；锁冲突集中在 night_scan 21:45–22:15 与 updater 19:45 窗口。

## 4. 失败恢复耗时（B4 实测口径）

- 块失败检测 → 60s 固定退避 → 同 offset 重跑（wrapper v4，`rc≠0 → sleep 60`）；
- 恢复总耗时 = 检测 + 60s + 重跑时长；9/1–9/2 白天多次触发（锁冲突），夜间零触发；
- 暂停窗口（19:30–20:45、21:15–23:15）外的长块用 timeout 截断续跑，状态经 offset 幂等续传。

## 5. 结论与 Phase 2 排期含义

1. **单 fold 重训 7s、单日横截面 7.8 分钟**——按 plan 默认 `train_window=120/test_window=20/step=20`（约 4–6 fold）估算，全量 walk-forward 纯算力 1–2 小时量级，算力不是瓶颈；**manifest 质量（train-only/缺 outcome/92% 重复）才是 fold 数量的实际约束**。
2. **内存是硬约束**：单日横截面已把容器顶到 4GiB；Phase 2 必须独立执行容器 + 内存限额 + 单任务并发，并预注册 fold 检查点以支持失败重跑。
3. 失败恢复机制已实战验证（B4 全程 43 次重试后 9/9 块成功），Phase 2 harness 沿用「分块 + 幂等 offset + 暂停窗口 + 锁退避」模式即可。
