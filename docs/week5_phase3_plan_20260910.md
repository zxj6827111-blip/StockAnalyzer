# Week5 Phase 3 最终实施方案（含 radar 部署合并窗口与 M12 主线协同）

日期：2026-09-10
前置：9/10 三轮 Tailscale 外网核查完结判定——整改主线（Phase -1→2 + 方向一'）全部闭环，
18-fold IC +0.0658 硬门 PASS（main d1f1a7c 已部署）。生产证据：lr1-lr5 全 success、cf=0；
evolution_offhours 9/9 23:06 首跑 success；night_scan 9/9 21:45 success；heavy 4G oom_kill=0。
唯一功能缺口：radar 全天 unavailable → realtime_snapshot_stale → 可交易信号保持关闭。

## 1. 结论性事实核验（本地代码确认，2026-09-10）

| 事实 | 本地核验结果 |
|---|---|
| 5915e47 / cee8b0c / dcbbe96 | ✅ 已在本地 main（`git log` 可见），生产镜像仍是 9/8 d1f1a7c 未带 |
| `labels.basis`（config.py:1056） | 默认 `"soup"`；return_rank 参数已就位（1058-1061） |
| return_rank 生产消费方 | **无**——`build_return_rank_labels`/`register_return_rank` 仅测试引用，生产训练/backfill 无 basis 分支（关键开发点，见 §4.1） |
| score_floor | `week5.final_signal_min_threshold: float = 70.0`（config.py:515），消费于 selection_engine.py:1780 |
| promotion 正样本率门槛 | `m5_positive_ratio_low=0.30 / high=0.70`（config.py:1343-1344），orchestrator.py:1294 消费 |
| weekend 重训窗口 | `week5_weekend_learning_time="12:00"`（config.py:1011 附近） |
| 原 Phase 3 方案 | docs/week5_backtest_remediation_plan_20260831.md §6（组合级执行回测：ExecutionMatcher+HoldingCurve，≥60 成熟样本门槛） |
| M12 工作区 | 8 修改 + 6 新文件，含已暂存 tests/test_week5_automation.py（需确认是否误暂存） |
| 调度组 | theme_daily_sync 落 heavy（4G，与 offhours 同组，错峰 16:45 vs 23:00） |

## 2. 总体策略

按依赖顺序分四条子线推进，与 M12 共用一次部署窗口：

```
部署（一次重建，带 main 5915e47..HEAD + M12）
  ├─ ① return_rank 切生产训练（现在可启动，硬门已过）── 周末重训窗口
  │     └─ ② 终门校准（依赖 ① 的新分数分布，重定 final_signal_min_threshold）
  ├─ ③ regime 识别（独立并行，代码工作）
  └─ ④ 执行层建模（独立并行，原 Phase 3 本体，最大块）
```

预期管理（来自核查结论，须写入验收口径）：IC +0.0658 的同源警告仍在——证明的是
口径对齐而非预测力翻倍；重训后的真实提升以 ② 校准评估为准，不拿 harness IC 直接外推。

## 3. 第一步：radar 修复 + M12 合并的部署窗口（本周）

### 3.1 合并清单
一次重建窗口把三者都带上，不为 radar 单独起部署：
1. **main 已有未部署**：5915e47（快照新鲜度排除无行情行——修 radar stale 模式）、
   cee8b0c（market_snapshot_timeout_sec 10→30）、dcbbe96（scheduler-heavy 3G→4G）；
2. **M12 主题层**（工作区：config.py/default.yaml/pipeline.py/service.py/
   week5_service/week5_automation_service/api/theme.py/theme 包 + 测试）——
   部署前先提交/合并；
3. **本周运维观察**：night_scan 21:45、offhours 23:00 自然窗口（旧镜像上跑也安全，
   不阻塞部署）。

### 3.2 部署后验证点（radar）
- radar 五个 slot：stale 模式应消失（5915e47 生效）；
- unavailable 模式（efinance+akshare ConnectionError，5-7s）**可能仍在**——上游/NAS
  DNS 间歇故障同源，非代码可修；若持续，方向是查 NAS DNS/网络而非改代码；
- radar 恢复是 actionable 门打开的前提（realtime_snapshot_stale 依赖 radar 链）。

### 3.3 M12 部署动作
- `SA__THEME__ENABLED=true`（shadow 观察期开始计时；不设则 M12 全链路休眠零影响）；
- 部署后手动 `POST /api/theme/sync` 一次做真实接口 spike（cls 期货/板块成分三接口
  从未打真实网络）；
- 后续每交易日 16:45 自动跑（heavy 组，与 offhours 23:00 错峰）。

## 4. ②③④ 子线方案

### 4.1 子线 ①：return_rank 切生产训练（现在可启动）

**关键发现**：`labels.basis` 目前无生产消费方——这不是纯配置切换，需要 1 个开发点 +
1 个配置切换 + 1 个重训窗口 + 1 个 gate 复核。

**开发点（唯一代码改动）**：在训练/backfill 的 label 构造入口按 `labels.basis` 分支：
- `basis == "soup"`（现状）：走 `build_label_policy_record` + soup label（当前行为，零变化）；
- `basis == "return_rank"`：走 `register_return_rank` + `build_return_rank_labels`
  （schema v3 契约，TOP30%→1 / BOTTOM30%→0 / 中间剔除，T+1 开盘锚点与 embargo 不变）。
位置候选：`learning/backfill.py` label 构造处 + `runtime/services/training_service.py`
训练入口（实施时以 grep `label_policy_registry` 调用点为准，两个入口都要接）。

**配置切换**（default.yaml + NAS .env）：
```yaml
labels:
  basis: return_rank          # 由 soup 切换
  return_rank_top_quantile: 0.3
  return_rank_bottom_quantile: 0.3
  return_rank_drop_middle: true
  return_rank_min_cross_section: 30
```
切换时机：**代码合入后、周末重训窗口前**；工作日保持 soup 不影响 night_scan。

**周末重训窗口**：`week5_weekend_learning_time=12:00`，切换 basis 后首个周末触发；
重训后走两阶段发布（champion/challenger），人工确认 registry hash 与 manifest。

**promotion gate 正样本率语义复核（关键）**：B4 实测正样本率 8.9%→28.0%（新 label
下分布变化），`m5_positive_ratio_low/high=0.30/0.70` 的语义是按 soup TP/SL 标定
的——新 label 下：
- 复核 0.30/0.70 是否仍适用（return_rank 理论上限约 30% 正样本率因 drop_middle，
  与 soup 的 8.9%→28.0% 动态范围不同）；
- 若需调整，改配置并在文档记录新标定依据，不得无记录调整。

### 4.2 子线 ②：终门校准（依赖 ①）

- 前提：① 重训完成，新模型分数分布已知；
- 动作：重定 `week5.final_signal_min_threshold`（当前 70.0）。Phase 1.5 已定位：
  候选分数中位 40-54 vs 门槛 60-70 脱节，最终门 60→70 时 final 273→90；
- 方法：在 shadow/backtest 环境做阈值扫描（照原方案 §6：终门参数实验仅在
  shadow/backtest 环境），选终门判据 = 扣成本后收益不反转 + 非 breadth-block 日
  不再长期 final_count=0；
- **硬约束**：不允许用降阈值掩盖模型排序无效（原方案正式放行条件原文）。

### 4.3 子线 ③：regime 识别（独立，可并行）

- 动机（核查结论）：月度单调性 11/18，2026-04~06 动量段 return_rank 失效；
- 代码工作：在 week6/engines.py 基础上识别动量/反转 regime 切换；
- 产出：regime 状态进 week5 扫描上下文（reuse market_radar/offhours 既有机制），
  供 ② 终门按 regime 差异化或供 ④ 执行层条件化；
- 验收：历史月度单调性重算，动量段失效可解释（regime 标记与该段对齐）。

### 4.4 子线 ④：执行层建模（独立，最大块，原 Phase 3 本体）

- 内容：照 docs/week5_backtest_remediation_plan_20260831.md §6——
  ExecutionMatcher + HoldingCurve，纳入成本/滑点/涨跌停/停牌/T+1/止盈止损/持仓
  重叠/资金占用，输出组合 NAV/净收益/最大回撤/换手率；毛收益与执行净收益分开报告；
- 样本口径：原始交易样本数 / distinct symbol-date / 有效资金槽位 三口径分开统计，
  功效判定以 distinct 为准；<60 只出诊断，60-99 初步判断标注功效不足，≥100 允许
  较强盈利性判断；
- 正式放行：扣成本不弱于全市场基准、≥4/6 自然月相对基准不为负、最大回撤 ≤ 基准
  +5pp、成本 +50% 压力测试方向不反转。

## 5. 排期

| 事项 | 窗口 | 前置 |
|---|---|---|
| 部署：5915e47+cee8b0c+dcbbe96+M12 一次重建 | 本周（用户定） | M12 提交合并 |
| 部署后 radar slot 观察 + M12 真实接口 spike | 部署次日 | 部署 |
| 子线① 开发点（basis 分支）+ 配置切换 | 本周 | 用户拍板 |
| 子线① 周末重训窗口 | 切换后首个周末 12:00 | 代码合入 |
| 子线① promotion gate 语义复核 | 重训后 | 重训完成 |
| 子线② 终门校准（阈值扫描） | 依赖①（重训后 1-2 天） | ①完成 |
| 子线③ regime 识别 | 可并行排期 | 无 |
| 子线④ 执行层建模 | 独立排期（最大块） | 无 |

## 6. 风险与回滚

1. **basis 切换开发点**：训练/backfill 双入口都要接分支，漏一处会导致 label 口径
   不一致（训练用 return_rank、backfill 用 soup）→ 实施时对两入口写一致性测试；
   回滚 = 配置切回 soup + 重训（champion 回退到切换前 artifact，registry 身份与
   文件脱钩已知风险，回滚时以 registry 为准）。
2. **radar unavailable 模式**：部署后可能仍在（上游 DNS/连接重置），不因 radar
   未恢复而阻塞 ①②③④ 的代码与回测工作——radar 只影响 live actionable 门，
   不影响训练/校准/建模。
3. **M12 与主线冲突面**：已由结构隔离（权重 0.0 + as_of 中性化 + enabled 默认关），
   部署后 18-fold 口径不受 M12 影响；但 M12 与子线① 共用 weekend/offhours 窗口时
   注意 heavy 组并发（theme 16:45 短任务 vs offhours 23:00 长任务，错峰无冲突）。
4. **learning_governance CI flaky**：既有问题，提交 CI 红时先 rerun 排除。

## 7. 验收清单（全部完成才算 Phase 3 ① 闭环）

- [ ] 代码：basis 分支双入口接入，soup 路径回归测试全绿（零行为变化）
- [ ] 配置：default.yaml + NAS .env basis=return_rank
- [ ] 重训：周末窗口完成，champion 新 artifact，registry hash 一致
- [ ] gate 复核：m5_positive_ratio 语义结论 + 新标定记录
- [ ] ② 终门校准：final_signal_min_threshold 新值 + shadow/backtest 阈值扫描记录
- [ ] 18-fold 硬门（IC +0.0658）不回退（校准前后各跑一次对比）
- [ ] lr1/lr2 生产零回归（部署后自然窗口确认）
