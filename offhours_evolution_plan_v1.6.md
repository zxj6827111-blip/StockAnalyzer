# 盘后智能进化系统 — 独立实施方案 v1.6（终版）

> 版本：2026-03-01 | 定位：A股量化分析系统 v7 配套智能子系统
> 核心目标：**在绝对安全可控的前提下，利用盘后70+小时/周的空闲算力，实现策略的自学习与反脆弱迭代。**
>
> *v1.6 变更说明：经八大 AI 六轮审核后定稿。第六轮 Grok/Gemini/智谱清言确认无缺陷通过；Codex/GPT-5.2/GLM5/Kimi/DeepSeek 提出 14 条边角修正已全部整合。累计修正 100+ 条。*

---

## 术语表

| 缩写 | 全称 | 说明 |
|------|------|------|
| M1-M11 | Module 1-11 | 进化系统 11 个功能模块 |
| OOS | Out-of-Sample | 样本外验证 |
| IR | Information Ratio | 信息比率（年化） |
| PCV | Purged Cross-Validation | 清除式交叉验证 |
| FDR | False Discovery Rate | 错误发现率 |
| BH | Benjamini-Hochberg | FDR 控制方法 |
| HMM | Hidden Markov Model | 隐马尔可夫模型 |
| DAG | Directed Acyclic Graph | 有向无环图 |
| MFE/MAE | Max Favorable/Adverse Excursion | 最大有利/不利偏离 |

---

## 0. 设计理念

| 原则 | 说明 |
|------|------|
| 🎯 **净收益优先** | 顶层目标 `NetReturn_AfterCost / Calmar / 尾部风险`；Precision@K 为辅助 |
| 🔒 **交易决策类变更只建议不执行** | 评分/选股/仓位/止损/市场状态参数 → 人工审批 |
| 🔓 **监控类参数可受控自动微调** | 仅限 C 级白名单 |
| 🧱 **与主系统解耦** | 独立进程 |
| ⏱️ **As-Of 绝对纪律** | `available_at` + 踏空 T-1 |
| ⚖️ **回测引擎强同源** | 红线：强制 v7 `BacktestMatcher` |
| 🚧 **变更流水线** | `OOS → 影子A/B → 分级授权 → Tracking Error 回滚 → 状态恢复` |
| 🧠 **云端融合算力** | 脱敏调用；故障降级本地 |

---

## 1. 核心底座与治理流水线

### 1.1 Score Fusion / 冲突仲裁引擎

| 规则 | 校准 |
|------|------|
| **一票否决**：M6/M1 → 降级 | 置信度 ≥ 0.75；误杀率 ≤ 8% |
| **加分封顶**：M3+M7 日上限 15 分 | 月度校准 |
| **物理意义过滤**：M8 LLM + 噪音注入 | — |
| **否决观察队列**：日 ≤ 20，留 20 日 | 周度误杀率反哺 |

### 1.2 Change Proposal 自动门禁

#### 1.2.1 提案包可复现制品

```yaml
proposal_artifact:
  proposal_id: "prop_20260301_001_sha256:xyz"
  data_snapshot_id: "parquet_v20260301_sha256:abc123"
  code_commit_id: "git:def456"
  random_seed: { optuna: 42, lgbm: 123, sampling: 789 }
  eval_protocol_id: "v7.2_cost_model_20260301"
  llm_prompt_version: "classify_v3.1"
  payload_uri: "suggestions/M2/hmm_params/prop_20260301_001.json"
  payload_sha256: "e3b0c442..."
  payload_diff_summary: "state_2→3 transition_matrix 调整"

  user_facing_summary:
    pnl_diff: "+1.2% 年化"
    risk_diff: "回撤 -0.5%"
    ir_score: 0.45
    turnover_change: "+12%"
    avg_trades_per_day: 3.2
    key_reason: "针对震荡市优化止损宽度"
    summary_window: { oos_days: 60, shadow_days: 14 }
    baseline: "Champion_same_window_after_cost"
```

**批准时校验**：系统自动比对 `proposal.code_commit_id` 与当前运行版本，不一致时警告"系统已更新，建议重新生成提案"。

#### 1.2.2 验证流程

1. **OOS 验证**：
   - 门槛：年化 IR > 0.3
   - 公式：`IR = mean(excess) / max(std(excess), 1e-6) × √252`
   - `excess = Challenger_after_cost − Champion_after_cost`
   - 最大回撤不变差

2. **影子前向测试**：
   - ≥ 14 交易日，≥ 30 笔成交，覆盖 ≥ 2 种市场状态（每种 ≥ 3 天）
   - Block Bootstrap p < 0.10（FDR 模块内独立 BH）
   - **流量门禁**：每周 ≤ 3 个 Challenger
   - **应急通道**：`emergency_fix=true` 不占名额，**仅适用于 A/B 级变更**（C 级本身自动生效，无需应急通道）
   - **超时释放**：`max_shadow_days: 30` → expired + 释放名额

3. **分级授权**：

| 级别 | 范围 | 审批 |
|------|------|------|
| **A级** | 止损线、仓位、新因子、标签、**HMM 参数** | 人工。周六 10:00 集中推送 |
| **A级兜底** | eval_protocol / BacktestMatcher / embargo / fdr / bootstrap | 强制 A 级 |
| **B级** | 黑名单、失败模式库 | 48h 一键批准，否则过期 |
| **C级白名单** | `alert_threshold_*` / `observation_queue_*` / `dashboard_display_*` / `log_verbosity` | 自动生效 |

#### 1.2.3 回滚机制

```yaml
rollback_policy:
  observation_window: 10
  min_trades: 15
  low_freq_extension: "trades < min_trades 且 days ≥ window → 延长至 max 30 天"

  hard_circuit_breaker:   # 自动回滚
    max_drawdown_delta: 0.03
    tail_loss_trigger: true

  performance_degradation:
    formula: "Z = mean(diff) / max(std(diff), std_floor)"
    std_floor: "max(0.001, 0.1 × Shadow_Period_Champion_Daily_Vol)"
    soft_warning: "Z < -1.5 持续 ≥ 3 天 → 预警通知"
    hard_trigger: "Z < -2.0 持续 ≥ 5 天 → 人工确认回滚"
    pending_confirmation_ttl: 3   # 天内未确认 → 自动回滚
    notification_format: "累计跑输：-X%（约 ¥Y）"

  post_rollback_actions:
    - "purge challenger 任务"
    - "失效 Score Fusion 缓存"
    - "compliance_log → rolled_back / timeout_no_ack"

  on_champion_invalidated:   # 影子期 Champion 失效
    action: "state=expired(champion_invalidated); 释放名额; purge challenger"
```

### 1.3 统计检验（分层+模块内独立 FDR）

```
候选集 → [初筛] 胜率>55% 且 E[r]>0 → [终审] Block Bootstrap + FDR (BH)
⚠️ FDR 模块内独立，禁跨模块合并 p-value
```

---

## 2. 核心模块

### M1：错误案例与踏空双向学习（P0）
* As-Of T-1 红线 + 毒药过滤 + 分档（3-5%/5-10%/>10%）+ shared/ 输出供 M8

### M2：市场状态自适应（P0）
* HMM：4 状态（趋势上/下/震荡/极端），输入：ATR + 板块分化度 + 成交额 Z-Score
* 四档置信度（>0.7/0.5-0.7/0.4-0.5/<0.4）
* 冷却期：**状态置信度连续 2 天 > 0.7 方可切换**（非物理时间锁，避免低置信度震荡切换）
* Optuna 替代网格

### M3：K 线形态（P1）
* FAISS 流式 memmap 构建（batch 50000，周末），安全删除（rename→延迟 24h）

### M4：资金流向（P1） · M5：标签优化（P2） · M6：对手盘（P1）

### M7：新闻舆情（P2）
* **向量聚类去重**：BGE-m3 聚类，默认 cosine > 0.85 合并为同一事件
* 指数退避 + Pydantic + 成本熔断（日 ≤ 15 元）

### M8：因子挖掘（P2）
* 六道门禁：PCV → Deflated Sharpe+FDR → LLM → 噪音注入 → 随机游走 → 注册中心
* 周末拉取 shared/missed_signals/

### M9：数据质量（P1）
* 缺失/**volume=0** → 冻结。degraded 禁入训练。blackout_day 整日禁入。

### M10：模型健康（P1） · M11：影子盘（P1，含三红线+归因报告）

---

## 3. DAG 调度与合规

### 3.1 时段管控

```yaml
hard_stop_windows:
  windows: ["08:45-09:35", "14:55-15:05"]
  action: "SIGTERM → 30s grace → SIGKILL。checkpoint 写入 manifest"
  macos_note: "launchd.plist 须 ExitTimeOut=60"

soft_yield_windows:
  windows: ["09:35-11:35", "13:00-14:55"]
  action: "taskpolicy -b 或 cpulimit ≤ 10%（需预装：brew install cpulimit）"

transition_windows:
  midday: { window: "11:35-13:00", action: "cpulimit ≤ 40%，禁高 I/O 任务" }
  post_close: { window: "15:05-15:30", action: "cpulimit ≤ 50%，低优先级可恢复" }

full_power_windows:
  windows: ["15:30-次日 08:45", "周六 00:00-周一 08:45"]
```

### 3.2 资源配额

```yaml
resource_limits:
  global: { cpu: 80%, mem: 70%, disk: 50GB }
  priority: [P0: M1/M2, P1: M9/M10/M3/M4/M11/M6, P2: M7/M8/M5]
  disk_sentinel: { watermark: 75%, targets: ["影子盘日志","FAISS快照","suggestions/>7天"], cold: "3月转存" }
  cloud: { daily: 15元, fallback: 本地, timeout: 3s }
  env_check: "启动时校验 cpulimit/duckdb/faiss 等依赖，缺失则报错退出"
```

### 3.3 DAG

```
M9 ──data_ok──┬──→ M4 ──┬──→ M1 ──→ Score Fusion → Proposal
 [ROOT]        │          ├──→ M6
 retry 3×120s  │          └──→ M3 ──→ Score Fusion
 成功重置计数   ├──→ M10
 全失败→熔断    └──→ M2 ──→ M5
               恢复后自动触发下游

M7 ──→ Score Fusion | M8(周末) | M11(空仓期)
```

Circuit Breaker：全局（M9 3 次失败）+ 模块级（3 天）+ blackout_day
告警含 `Action_Required` + `Diagnosis_Hint`

### 3.4 恢复
* 时间窗口防火墙（恢复脚本第一步检查时间）
* `run_manifest.json` 含 checkpoint 时间戳

### 3.5 合规日志

```yaml
compliance_log:
  fields: [trace_id, 生成时间, 输入数据哈希, 置信水平, LLM_prompt_version,
           代码commit, proposal_id, state, active_champion_id, symbol]
  state_enum: [generated, validated, shadowing, approved, promoted,
               rolled_back, expired, invalidated, retry_pending]
  retention: "3年（~1TB，按月分区）"
```

---

## 4. 验收指标

| # | 中文名 | 门槛 | 窗口 | 样本 |
|---|--------|------|------|------|
| 1 | 年化信息比 | IR > 0.3 | 20 日 | ≥ 30 笔 |
| 2 | 风险收益比 | Calmar ≥ 2.0 | 60 日 | ≥ 60 笔 |
| 3 | 最大回撤 | ≤ 15% | 120 日 | — |
| 4 | 信号拒单率 | ≤ 12%（>12% 主动告警） | 20 日 | ≥ 50 |
| 5 | 换手率 | 周均 ≤ 40% | 4 周 | — |
| 6 | 开盘可执行率 | ≥ 85% | 20 日 | ≥ 50 |
| 7 | 提案通过率 | 监控用（周 < 20% 可能门槛过严） | 4 周 | — |

---

## 5. 分期交付

**第一批**：As-Of + BacktestMatcher API + M9(retry+blackout+volume=0) + M10 + M4 + M11(红线+归因) + Change Proposal(含版本校验) + 分级授权 + 回滚(TTL+软预警+状态恢复) + 合规日志 + Score Fusion + 统计工具库 + 恢复脚本(时间防火墙) + 环境依赖检查

**第二批**：M1(双向+As-Of+毒药+分档+shared/) + M2(4状态HMM+Optuna+冷却) + M3(FAISS流式+安全删除)

**第三批**：M6 + M8(六道门禁+shared/) + M7(聚类去重+预算熔断) + M5

---

## 附录：v1.7+ 候选

| # | 候选项 |
|---|--------|
| 1 | std_floor 系数敏感性回测（0.05/0.1/0.15） |
| 2 | IR 按策略频率分层 |
| 3 | 失败模式反身性监控 |
| 4 | 因子左偏检测 |
| 5 | MLflow 实验跟踪 |
| 6 | HMM 跨市场因子 |
| 7 | 成本参数敏感性 |
| 8 | 供应商标签漂移 |
| 9 | 策略切换换手率门禁 |
| 10 | 断电 RTO 验收 |
| 11 | Deflated Sharpe 伪代码 |
| 12 | 审批界面原型 |
