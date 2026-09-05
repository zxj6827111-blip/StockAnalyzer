# Week5 Phase 0 再验收报告（NAS 窗口完成后 · 2026-09-05）

> 前序：`docs/week5_phase0_audit_20260831.md`（8/31 只读审计，B1–B13 阻塞项清单与本地代码修复）。
> 本报告：NAS 运行窗口（B4/B8/B12/B13 + 单 fold benchmark）执行记录与 Phase 0 硬门再验收。
> 配套证据：`docs/week5_phase0_coverage_report_20260905.md`（B12 四报告）、`docs/week5_phase0_single_fold_benchmark_20260905.md`（§8 算力实测）。

## 1. NAS 运行窗口执行记录

### 1.1 B4 受影响 outcome backfill（9/1–9/5，已完成）

- 分块续跑 9/9 块全部 `rc=0`，末块 offset=80,000 于 2026-09-05 11:30 完成（wrapper 空转已停止）。
- 全库核验：outcome 总量 90,287（label_matured 84,788 / pending 5,389 / reconciled 110）；`label_anchor_time`/`source_data_cutoff`/`conflict_flag` 回填 84,897 行（94.0%，其余为 pending 行，设计上不计算 MFE/MAE）；conflict_flag=True 16,785 / False 68,112；outcome↔snapshot JOIN 命中 100%。
- **新旧标签差异报告（migration 主口径，末块 4,898 条）**：`changed_label_rows=935（19.1%）`；正样本率 old 8.9% → new 28.0%；冲突行数 935 不变；missing_mature_time_rows=0。旧 policy `label_policy_v1_f9b0a22b3ba9`（close 口径）→ 新 policy `label_policy_v2_90e420e4d8f0`（T+1 开盘口径，B1 修复）。
- 锁韧性实战：白天块平均 5 次锁冲突/块、夜间 0 次；wrapper v4 暂停窗口 + 60s 退避 + offset 幂等续传全程有效（43 次重试后全绿）。

### 1.2 B8 registry 脏数据隔离（2026-09-05，已完成）

- 全表备份 `model_registry_backup_20260905`（17 行全量保留）。
- 17/17 条记录全部 `artifact_content_hash=''`，置 **`lifecycle_state='revoked'`（终态，无出边）** + `blocked_reason` 记录隔离原因（`quarantine:empty_content_hash`；14 条另加 `quarantine:artifact_uri_points_to_manifest_json`；2 条另加 `quarantine:legacy_alias_pointer`；1 条保留 `orig:artifact_overwritten`）+ `revoked_at` 时间戳。
- 隔离后核验：champion 0 条、active_champion 为空、无 approved 记录——fail-closed 状态不变。
- 兼容性核验（代码路径）：`_validated_predictor_reload` 的 hash 匹配与 `active_champion()` 均不看 lifecycle_state；无 champion 时 unregistered artifact 走 `predictor_reload_unregistered_pre_champion` 审计放行窗口，night_scan 存量 predictor 加载不受撤销影响。

### 1.3 B12 数据覆盖四报告（2026-09-05，已完成）

详见 `docs/week5_phase0_coverage_report_20260905.md`，要点：

- **交易日历**：窗口（2026-03-01~08-28）125 个交易日；工作日缺失仅 5 个法定节假日（4/6、5/1、5/4、5/5、6/19）；低覆盖日仅 7/30（2,354 行，median 45.5%，已知半日）；周末 0 异常；8/31–9/4 每日 5,541–5,543 行完整——**数据新鲜到 9/4（最后完整交易日）**。
- **symbol-date**：窗口 5,568 只 / 651,225 行；每 symbol 交易天数 median 124/125；OHLC 全无 NULL；384 只 <50 天（新股）；训练样本 universe 1,577 只覆盖市场窗口 28.3%——**universe 错配量化实锤（B10 决策依据）**。
- **feature**：signal_snapshots 90,316 条全部 schema v1；222 个特征键全量出表，**100% presence / 100% non-null**；非法 JSON 0 行。
- **label maturity**：matured 84,788（93.9%），按月 25,742/15,685/20,154/18,598/4,609（4–8 月）；pending 5,389 集中在 7–8 月（新样本未成熟，符合预期）；1,577 只有标签、1,378 只有 matured 样本。

### 1.4 B13 updater 与 readiness 核验（2026-09-05，已完成）

- **updater 非 no-op**：readiness（schema v2，`source=batch_update`，发布于 9/4 20:38）显示 daily/delta/index 三链 `latest_trade_date=2026-09-04`、coverage_ratio=1.0、expected=actual=5,541。8/31 审计时 delta max=8/28 的结论**已被每日推进刷新**——日线 delta 链正常，8/31 的"断供"仅限分钟线链路。
- **readiness 发布机制**：每日 19:45 stale 失效（`.stale-*` 文件 9/1–9/4 齐全）→ 20:38 重新发布；night_scan 21:45 消费。
- **champion 三层核验**：17 条记录全 challenger / 全空 hash / 无 champion（与 8/31 一致），B8 隔离后 registry 已无活跃记录；runtime 走 pre-champion 兼容窗口（审计放行）。
- **生产观察项（既有问题，非 Phase 0 引入）**：night_scan 记录级 `data_gate=blocked`（`data_quality_blocked:0.000`、`financial_trust_level: missing`、provider 空）自 **8/26** 即存在；该门不阻断终门（9/3 在 gate=blocked 下仍产出 2 只 buy）。财务/背景数据层在 scan 快照中未填充是生产数据链缺陷，**Phase 4 shadow 前必须解决**，但不在 Phase 0 门禁范围内。

### 1.5 单 fold benchmark（2026-09-05，已完成）

详见 `docs/week5_phase0_single_fold_benchmark_20260905.md`：单日横截面 7.8 分钟（容器内存逼近 4GiB 上限）、单 fold 重训 6.77s（进程峰值 1.6GiB）、锁占用 <6s/fold、失败恢复 60s 退避实战验证。附带发现：3/4 个最大 manifest 不可训练（2 train-only、1 缺 outcome）、成功 manifest 92.2% 重复逻辑行——Phase 2 fold 规划的关键输入。

## 2. Phase 0 硬门对照（再验收）

| 硬门 | 状态 | 证据 |
|---|---|---|
| 标签时间不变量违规 = 0 | ✅ | time_semantics 四不变量 + manifest embargo（代码，115 测试）；实库：matured 行 anchor/cutoff 回填 100%、missing_mature_time_rows=0（§1.1） |
| 受影响标签已重建或明确排除 | ✅ | B4 全量完成 9/9 块；迁移差异 19.1% 行改变、正样本率 8.9%→28.0%；94.0% 行完成重建，其余为设计上不回填的 pending 行（§1.1） |
| active champion/registry/artifact/hash 完全一致 | ✅（真空一致） | 无 champion；17 条旧记录全部隔离为 revoked 终态；B6 强制新记录非空 hash（§1.2） |
| 新评估产物 model_id 非空 | ⏳（Phase 1 落地项） | 审计定稿口径：随 Phase 1 字段标准化落地；Phase 0 不产生评估产物 |
| 未处理 artifact_overwritten = 0 | ✅ | 原 1 条 artifact_overwritten 记录已 revoke 隔离（blocked_reason 保留原由）；无其他 overwritten 记录（§1.2） |
| Universe 口径已确定 | ✅ | B10 决策：扩训练样本（§7.1 决策 + B12 的 28.3% 覆盖量化） |
| 数据缺口已修复/排除/标记 | 🔶（规则定稿，标记落地随 harness） | 日线无洞（B12 §1）；7/30 半日已定位；分钟线 7/20–8/3 空洞已定位；`degraded_reasons` 字段随 Phase 1/2 harness 落地（B11 原口径） |
| NAS runtime 读取并实际使用有效 champion | ✅（N/A 状态明确） | 无有效 champion 可读（registry 已清空隔离）；pre-champion 兼容窗口审计放行且留审计事件；重训后经 `_validated_predictor_reload` 三层校验（B7）。生产 fail-closed 维持 |
| 单 fold benchmark 已完成 | ✅ | §1.5 |

## 3. 再验收结论

1. **Phase 0 硬门整体通过**（8/31 的 10 项阻塞 B1–B10 全部关闭：代码侧 7 项修复已落地测试，NAS 窗口 4 项 B4/B8/B12/B13 已实库核验；§8 单 fold benchmark 完成）。两项 ⏳/🔶 为审计时已定稿的 Phase 1 落地项（model_id 非空字段、degraded_reasons 字段），不构成 Phase 0 阻塞。
2. **重训与 walk-forward 禁令解除条件已满足**——Phase 1（时间安全的模型选择与候选打分评估）可以启动；生产 champion 引导（`bootstrap_baseline_champion`）仍需在 Phase 1/2 评估通过后按预注册流程执行。
3. **生产维持 fail-closed**：registry 无 champion、无放宽任何终门参数、无真实交易。
4. **Phase 1 启动前必须吸收的两个新事实**：
   - manifest 质量缺陷（train-only / 缺 outcome / 92% 重复逻辑行）→ Phase 2 fold 规划的前置数据工程项；
   - 单日横截面回测在 4GiB 容器内逼近内存上限 → Phase 2 独立执行环境。
5. **Phase 4 前必须解决的生产数据链问题**（既有，非本整改引入）：night_scan 财务/背景数据层缺失导致 scan 级 data_gate 长期 blocked。

## 4. 偏差签认（对 8/31 审计的补充）

| 项 | 说明 | 状态 |
|---|---|---|
| B8 隔离机制选择 | 审计原文"标记隔离"，实现为 `lifecycle_state='revoked'` 终态 + blocked_reason 原因标注 + 整表备份，未新增 quarantine 字段 | 签认：REVOKED 无出边、代码读取路径均不依赖其状态（§1.2 兼容性核验），满足"隔离且不删历史" |
| B13 "updater no-op" 表述 | 8/31 预期核验"no-op 状态"，实测 updater **非 no-op**（三链推进至 9/4） | 签认：以实库为准，8/31 的 delta 断供判断仅适用于分钟线链路 |
