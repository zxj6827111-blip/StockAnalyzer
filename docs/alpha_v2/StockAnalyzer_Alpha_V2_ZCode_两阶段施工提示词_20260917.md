# StockAnalyzer Alpha V2.0 — ZCode 两阶段施工提示词

> 版本：2026-09-17  
> 用途：交给 ZCode 执行 Alpha V2.0 大批次改造。  
> 上位规格：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`  
> 细化施工卡：`StockAnalyzer_Alpha_V2_ZCode_阶段施工提示词_20260917.md`  
> 配套验收：`StockAnalyzer_Alpha_V2_Codex_两阶段验收提示词_20260917.md`

---

# 0. 唯一需要人工修改的变量

每次开工只修改：

```text
CURRENT_BATCH = M1
```

或：

```text
CURRENT_BATCH = M2
```

对应关系：

```text
M1 = S00 ～ S10
M2 = S11 ～ S23
```

不要一次同时执行 M1 和 M2。

---

# 1. 当前任务

```text
CURRENT_BATCH = M1
```

你现在作为 StockAnalyzer 项目的高级 Python 架构师、量化研究基础设施工程师和代码施工负责人，执行 `CURRENT_BATCH` 对应的大批次改造。

本任务不是讨论或重新设计，而是实际代码施工。

---

# 2. 开工前必须完整读取

请先读取：

```text
docs/alpha_v2/StockAnalyzer_Alpha_V2_完整改造方案_20260917.md
docs/alpha_v2/StockAnalyzer_Alpha_V2_ZCode_阶段施工提示词_20260917.md
docs/alpha_v2/StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md
docs/alpha_v2/PROGRESS.md
```

如果 `PROGRESS.md` 不存在：

- M1 从 S00 创建；
- M2 若仍不存在则立即 BLOCKED，不得继续。

优先级：

```text
完整改造方案
    >
阶段施工提示词
    >
本两阶段控制提示词
```

如果代码现实与蓝图不一致：

1. 不静默改变目标；
2. 记录差异；
3. 优先复用现有架构；
4. 采用最小兼容实现；
5. 在最终报告中列入 `Deviation From Blueprint`。

---

# 3. 批次定义

## M1 — Correctness Foundation

如果：

```text
CURRENT_BATCH = M1
```

则按顺序执行：

```text
S00  Alpha V2 Feature Flag / No-op Baseline
S01  Model Identity Truth
S02  T+1 Entry Simulation
S03  Point-in-Time Historical Universe
S04  SelectionContract 300/100/50
S05  Registry / Archive Governance
S06  HistoricalModelResolver
S07  Feature Price / Execution Price Split
S08  Data Health / Market Breadth Split
S09  Model / Label Semantic Guard
S10  Decision Log + Outcome Maturation
```

目标：

> 把研究、回测、模型身份、历史股票池、执行价格和数据健康地基做正确。

M1 完成以前禁止进入 Alpha 模型优化。

---

## M2 — Alpha Research & Shadow

如果：

```text
CURRENT_BATCH = M2
```

则按顺序执行：

```text
S11  Label V2
S12  Benchmark System
S13  Winner Recall
S14  Feature Availability / Leakage Audit
S15  Simple Factor Baseline
S16  Shared Feature Matrix + Multi-Head
S17  Cross Review V2
S18  Final Decision Policy V2 Shadow
S19  Purged Walk-Forward / OOS
S20  Legacy vs V2 Shadow Dual Run
S21  Daily Alpha Health Report
S22  NAS Performance Hardening
S23  Theme / News / Intraday Incremental Experiment Framework
```

目标：

> 在 M1 的可信基础设施上建立真正可验证的 Alpha Ranking、Clean OOS、Shadow 双轨和后续增量研究体系。

---

# 4. M2 前置硬门

如果：

```text
CURRENT_BATCH = M2
```

施工前必须检查：

```text
M1 Codex Acceptance = PASS
```

如果没有明确 PASS：

```text
Status = BLOCKED
reason = M1 acceptance gate not passed
```

然后停止。

不得以“看起来已经完成”代替 Codex PASS。

---

# 5. Git Preflight

每批开始前执行：

```bash
pwd
git branch --show-current
git rev-parse HEAD
git status --short
git log -5 --oneline
```

原始评审基线：

```text
branch = fix/asof-breadth-gate-coverage-0917
commit = 7e9e33bdb9d03506cff5dfff29c78b1c95019541
```

但当前施工必须基于最新已验收代码，不得 reset 回原始基线。

如果工作区有修改：

- 不 reset；
- 不 checkout 覆盖；
- 不擅自 stash；
- 不删除；
- 先识别属于用户、前阶段还是当前批次。

---

# 6. 批次内部必须保留 SXX 检查点

虽然一次执行整个 M1 或 M2，但**不能把所有步骤揉成一个不可审计的大改动**。

必须严格：

```text
执行 Sxx
→ 运行 Sxx 对应测试
→ 生成 Sxx validation artifact
→ 更新 PROGRESS
→ 自检
→ 通过后才进入下一 Sxx
```

建议审计目录：

```text
artifacts/alpha_v2/audit/
```

至少形成：

```text
s00_validation.json
s01_validation.json
...
```

M1 应形成 S00～S10 的阶段证据。

M2 应形成 S11～S23 的阶段证据。

---

# 7. Fail-Fast 规则

任一 SXX 出现以下情况：

```text
Blocking test failure
PIT violation
future fallback
T close 假成交
Legacy 行为被意外改变
关键 contract 无法满足
关键测试无法建立
```

则：

```text
立即停止 CURRENT_BATCH
```

不得为了“一次做完”继续后续 SXX。

最终状态：

```text
PARTIAL 或 BLOCKED
```

并指出：

```text
STOPPED_AT = SXX
```

---

# 8. 每个 SXX 的具体要求从哪里读取

不要凭本提示词概括施工。

对每一个 SXX：

在：

```text
StockAnalyzer_Alpha_V2_ZCode_阶段施工提示词_20260917.md
```

中读取对应完整章节并执行。

例如 M1：

```text
先读 S00 完整章节
完成并自检
再读 S01 完整章节
...
```

不得仅依据阶段标题自行发挥。

---

# 9. 全局禁止事项

在 M1 完整 PASS、并产生 Clean OOS 证据以前，严禁：

```text
降低 final_signal_min_threshold = 70
降低 p_lgbm_min
降低 p_xgb_min
降低 p_meta_min
放宽 max_diff
为增加出票修改 breadth threshold
为增加出票取消 overextension
扩大 final cap 强行出票
修改现有 LGBM/XGB/meta 权重
直接 promote challenger
直接切 serving model
对 AUC 0.331 模型做 1-p 上线
将 Theme/News 正式并入 Alpha
```

同时禁止：

```text
git push
生产部署
重启 NAS / docker
修改生产 .env
修改生产密钥
修改生产数据库正式记录
模型 promotion
```

除非用户另行明确授权。

---

# 10. Legacy 保护

M1 和 M2 默认都必须保护：

```text
Legacy 正式夜扫
Legacy Final Selection
Legacy 70 分
Legacy Cross Review
正式飞书推荐
生产 serving model
正式调度
```

V2 默认：

```text
Shadow
```

在 M2 的 S20/S21 中可以建立生产 Shadow 代码能力，但未经用户部署授权：

```text
只能完成代码与本地验收
不能自行部署生产
```

---

# 11. M1 特别验收不变量

M1 结束前必须同时满足：

```text
1. Alpha V2 有独立 feature flags
2. 实际模型身份可追
3. historical replay 不会 future-model fallback
4. T 日盘后信号不再 T close 假成交
5. historical universe 不含 future-listed symbol
6. execution 使用 raw price
7. production/historical night contract 可统一为 300/100/50
8. Data Health 与 Breadth 分开
9. 模型输出语义明确
10. Decision/Outcome 可以审计和成熟
11. Legacy 默认行为未被 V2 接管
12. 所有关键改动可回滚
```

M1 的目标不是提高模型收益。

---

# 12. M2 特别施工原则

M2 必须建立：

```text
多 horizon executable outcome
Eligible / Quality300 / Style-Matched benchmarks
Winner Recall
PIT-safe feature set
Simple factor baseline
Shared Feature Matrix
Rank / Return / Direction / Risk heads
Cross Review V2 shadow observation
Final Decision Policy V2 shadow
Purged Walk-Forward
Legacy vs V2 dual-run code path
Daily Alpha Health Report
NAS 性能优化
Theme/News/Intraday 增量实验框架
```

但必须区分：

```text
工程实现完成
!=
研究证据已经成熟
```

以下证据不能凭空制造：

```text
20 mature decision dates
60 mature decision dates
120 clean OOS dates
~250 OOS dates
```

因此 M2 可以合法出现：

```text
implementation = DONE
research_gate = AWAITING_DATA
```

这不是失败。

---

# 13. M2 中需要时间积累的模块

对 S20～S23：

如果需要真实生产/未来交易日观察：

完成代码、测试、artifact contract 和报告能力后标记：

```text
AWAITING_PRODUCTION_OBSERVATION
```

或：

```text
AWAITING_MATURE_OUTCOMES
```

不得伪造：

```text
60D PASS
120D PASS
```

---

# 14. 测试纪律

每个 SXX 完成时：

1. 跑新增测试；
2. 跑受影响模块测试；
3. 至少一个 negative/failure case；
4. 必要的 Legacy regression；
5. 记录真实测试命令与结果。

批次结束时再跑：

```text
批次级集成测试
关键 Legacy regression
Alpha V2 contract/replay 测试
```

如果完整测试套件太大，可运行最小充分集，但必须说明未覆盖范围。

不得：

```text
删除失败测试
放宽断言掩盖问题
未运行却写 PASS
```

---

# 15. PROGRESS.md 管理

每完成一个 SXX：

追加该阶段记录。

至少：

```text
Stage
Status
Date
Starting HEAD
Working tree / commit
Files Changed
Tests
Audit Artifacts
Rollback
Deferred Findings
Codex Acceptance = PENDING
```

批次结束再追加：

```text
Batch = M1 / M2
Batch Status
Stages Completed
Stopped At
Batch Test Summary
Codex Batch Acceptance = PENDING
```

ZCode 不能自己把 Codex Acceptance 写成 PASS。

---

# 16. 审计工件

M1 建议至少：

```text
artifacts/alpha_v2/audit/m1_summary.json
```

M2 建议至少：

```text
artifacts/alpha_v2/audit/m2_summary.json
```

批次摘要至少：

```text
batch
starting_head
ending_head_or_worktree
stages_requested
stages_completed
stopped_at
tests_run
test_summary
legacy_behavior_status
audit_artifacts
deferred_findings
research_gates
generated_at
```

禁止写 secrets。

---

# 17. Commit 策略

允许两种方式：

## 推荐方式 A：阶段性本地 commits

```text
S00 commit
S01 commit
...
```

好处：

- 容易回滚；
- Codex 容易逐阶段 diff；
- 出错易定位。

## 方式 B：一个批次 commit

如果不希望很多 commit：

```text
M1 one commit
M2 one commit
```

但必须保留 SXX validation artifacts 和清晰 diff。

未经授权：

```text
不得 git push
```

---

# 18. 批次最终报告

完成或停止后必须输出：

```text
# CURRENT_BATCH Implementation Report

## 1. Batch Status
DONE / PARTIAL / BLOCKED

## 2. Batch Scope
M1 = S00-S10
或
M2 = S11-S23

## 3. Preflight
- branch
- starting HEAD
- initial git status
- prerequisite gate

## 4. Stage Matrix

| Stage | Status | Tests | Audit Artifact | Notes |
|---|---|---|---|---|
| S00 | DONE/PARTIAL/BLOCKED | ... | ... | ... |

## 5. Files Changed
按模块归类。

## 6. Behavior Changes
说明 V2 新行为以及 Legacy 不变量。

## 7. Contract / Config Changes

## 8. Tests Executed
列出真实命令。

## 9. Test Summary
XX passed
XX failed
XX skipped

## 10. Negative Tests

## 11. Audit Artifacts

## 12. Legacy Regression Result

## 13. PIT / Leakage Result

## 14. Execution Semantics Result

## 15. Security Check

## 16. Resource / Performance Result
M1 可 N/A；
M2 S22 应提供实测。

## 17. Research Gate Status
尤其 M2 区分：
- implementation
- awaiting data
- 20D/60D/120D/250D

## 18. Deviation From Blueprint

## 19. Deferred Findings

## 20. Git Diff Summary

## 21. Rollback Plan

## 22. Codex Acceptance
PENDING

## 23. Next Batch
LOCKED — waiting for Codex PASS
```

---

# 19. 当前执行命令

现在读取：

```text
CURRENT_BATCH
```

如果是：

```text
M1
```

执行 S00～S10。

如果是：

```text
M2
```

先验证 M1 Codex PASS，再执行 S11～S23。

再次强调：

> 这是“一次交互完成一个大批次”，不是取消内部 SXX 质量门。

任何阶段遇到 Blocking Failure，立即停止当前批次，不得带病施工。
