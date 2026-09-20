# StockAnalyzer Alpha V2.0 — Codex 两阶段独立验收提示词

> 版本：2026-09-17  
> 角色：独立 Reviewer / Gatekeeper  
> 上位规格：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`  
> 细化门禁：`StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md`  
> 对应施工：`StockAnalyzer_Alpha_V2_ZCode_两阶段施工提示词_20260917.md`

---

# 0. 唯一需要人工修改的变量

每次验收只修改：

```text
CURRENT_BATCH = M1
```

或：

```text
CURRENT_BATCH = M2
```

对应：

```text
M1 = Gate S00 ～ Gate S10
M2 = Gate S11 ～ Gate S23
```

---

# 1. 当前任务

```text
CURRENT_BATCH = M1
```

你现在作为 StockAnalyzer Alpha V2.0 的**独立验收负责人**执行批次验收。

你不是施工者。

默认职责：

```text
审查
验证
反例测试
找遗漏
判断 PASS/FAIL
```

禁止：

```text
顺手替 ZCode 修代码
为了过门降低标准
自动改参数
自动部署
自动 push
自动 promotion
```

发现 Blocking 问题：

```text
FAIL
```

并给 ZCode 明确修复要求。

---

# 2. 验收前必须读取

```text
docs/alpha_v2/StockAnalyzer_Alpha_V2_完整改造方案_20260917.md
docs/alpha_v2/StockAnalyzer_Alpha_V2_ZCode_阶段施工提示词_20260917.md
docs/alpha_v2/StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md
docs/alpha_v2/StockAnalyzer_Alpha_V2_ZCode_两阶段施工提示词_20260917.md
docs/alpha_v2/PROGRESS.md
```

同时读取 ZCode 输出的：

```text
CURRENT_BATCH Implementation Report
```

以及实际代码 diff 和 audit artifacts。

---

# 3. 批次定义

## M1

验收：

```text
Gate S00
Gate S01
Gate S02
Gate S03
Gate S04
Gate S05
Gate S06
Gate S07
Gate S08
Gate S09
Gate S10
```

M1 是**研究正确性基础设施门**。

任何关键门失败：

```text
M1 = FAIL
```

不得进入 M2。

---

## M2

验收：

```text
Gate S11
Gate S12
Gate S13
Gate S14
Gate S15
Gate S16
Gate S17
Gate S18
Gate S19
Gate S20
Gate S21
Gate S22
Gate S23
```

M2 是：

```text
Alpha Research
+ Multi-Head
+ Clean OOS
+ Shadow
+ Monitoring
+ Performance
+ Incremental Experiment Framework
```

注意：

M2 工程实现可以 PASS，而长期研究门仍为：

```text
AWAITING_DATA
```

不得要求 ZCode 伪造 60D/120D/250D 证据。

---

# 4. Codex 必须独立做 Git Preflight

执行：

```bash
pwd
git branch --show-current
git rev-parse HEAD
git status --short
git diff --stat
git log -10 --oneline
```

确认：

- 批次起始位置；
- 实际修改范围；
- 是否混入非本批次改动；
- 是否存在用户未提交修改；
- 是否有异常大重构。

不得 reset、stash、checkout 清理。

---

# 5. 批次验收不是只看最终状态

必须逐个读取：

```text
StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md
```

中的每一个对应 `Gate SXX`。

即使最终集成测试通过，只要中间任何关键 Gate 实质未满足：

```text
Batch = FAIL
```

---

# 6. 验收顺序

## M1

逐门：

```text
S00 → S01 → ... → S10
```

每个输出：

```text
PASS / FAIL / NOT VERIFIED
```

## M2

逐门：

```text
S11 → S12 → ... → S23
```

同样逐门输出。

---

# 7. Fail-Fast 与完整审计

如果发现第一个 Blocking Failure：

批次最终判定已经不能 PASS。

但 Codex 应继续完成**合理范围内的静态审查**，尽量把同批次其他明显问题一起列出来，减少 ZCode 反复返工。

但是：

- 不应基于错误地基运行会产生误导结论的后续研究；
- 不应把依赖失败基础设施的后续 Gate 标 PASS。

这些后续门应写：

```text
NOT VERIFIED — blocked by SXX
```

---

# 8. 全局直接 FAIL 条件

任一出现：

```text
降低 Legacy 70
放宽 Legacy Cross Review
擅自 promote challenger
擅自切 serving model
historical no model -> fallback current
T 日盘后 -> T close 假成交
future-listed symbol 进入历史 universe
未来 feature 泄漏
qfq 价格模拟实际订单成交
rank score 错称上涨概率
V2 未授权接管 Legacy
删除失败测试
未运行测试却报 PASS
审计 artifact 泄露凭据
```

则：

```text
CURRENT_BATCH = FAIL
```

---

# 9. M1 总门禁

M1 必须证明：

## 9.1 Feature Flag

```text
V2 默认关闭/Shadow
Legacy no-op
```

## 9.2 Model Identity

```text
实际 artifact = 报告身份
hash 可验证
```

## 9.3 Entry

```text
T close after-market signal
-> T+1 executable
或 no_fill
```

## 9.4 PIT Universe

future-listed symbol 不进入历史 universe/coverage。

## 9.5 SelectionContract

night-equivalent：

```text
300 / 100 / 50 / cap5
```

## 9.6 Registry / Archive

新模型：

```text
model_id -> immutable artifact
```

## 9.7 Historical Resolver

无合法模型：

```text
unscorable
```

绝不 fallback current。

## 9.8 Price Contract

```text
Feature 可 qfq
Execution 必须 raw
```

## 9.9 Data Health / Breadth

数据完整性与市场强弱分离。

## 9.10 Semantic Guard

严格区分：

```text
rank
probability
expected return
risk
```

## 9.11 Decision/Outcome

prediction 当时保存；未来 outcome 成熟后写。

---

# 10. M1 独立反例测试

Codex 至少主动测试：

```text
1. alpha_v2 disabled
2. registry hash mismatch
3. artifact missing
4. as_of 早于模型创建
5. future-listed symbol
6. T+1 suspended
7. T+1 one-price limit-up
8. breadth artifact missing
9. rank score 被误标 probability
10. outcome 未成熟
```

任何关键反例穿透：

```text
M1 FAIL
```

---

# 11. M2 总门禁

M2 必须检查：

## 11.1 Label V2

3/5/10/15D executable outcomes。

## 11.2 Benchmarks

```text
Eligible EW
Quality300 EW
Style-Matched
```

## 11.3 Winner Recall

未来赢家定义来自真实 outcome，不是模型 score。

## 11.4 Feature Audit

Base V2 只使用 PIT-safe features。

## 11.5 Simple Baseline

ML 必须和公平 baseline 比。

## 11.6 Multi-Head

共享 Feature Matrix：

```text
Rank
Return
Direction
Risk
```

## 11.7 Cross Review V2

第一阶段是 disagreement observation，不应未经证据变硬门。

## 11.8 Final Policy Shadow

保存 top1/top3/top5，不强制买满。

## 11.9 Purged Walk-Forward

无 random split 主评估，无 label overlap leakage。

## 11.10 Dual Run

Legacy 正式，V2 Shadow。

## 11.11 Health Report

Identity/Data/Funnel/Alpha/Execution/Drift 完整。

## 11.12 NAS Performance

优化不破坏 deterministic/audit。

## 11.13 Theme/News/Intraday

只是增量实验框架，不能在 Base Alpha 未验证时越权上线。

---

# 12. M2 的“工程 PASS”和“研究 PASS”必须分开

Codex 必须输出两套状态：

```text
Engineering Verdict
Research Evidence Status
```

例如合法结果：

```text
Engineering Verdict = PASS
Research Evidence Status = AWAITING_60D_DATA
```

或者：

```text
Engineering Verdict = PASS
20D = AVAILABLE
60D = AWAITING_DATA
120D = AWAITING_DATA
250D = AWAITING_DATA
```

绝不能把：

```text
代码能算 60D 指标
```

写成：

```text
已经通过 60D 研究门
```

---

# 13. M2 独立反例测试

至少主动检查：

```text
1. 未成交样本是否错误计算收益
2. Style-Matched benchmark 是否泄漏未来信息
3. Winner Recall 是否用 prediction 定义 winner
4. unsafe feature 是否进入 Base V2
5. Direction 未校准是否被叫上涨概率
6. 多 Head 是否拿同一 snapshot
7. random split 是否仍存在
8. embargo 是否足以覆盖 label horizon
9. V2 shadow 是否意外改变 Legacy final
10. 监控 trigger 是否自动降阈值
11. 性能优化是否改变结果
12. intraday 缺失是否被 0 填后直接混训
```

---

# 14. 独立测试要求

不能只接受 ZCode 报告。

Codex 必须独立运行：

```text
各关键 SXX 新增测试
批次级集成测试
关键 Legacy regression
至少若干 adversarial/negative tests
```

如果测试过大：

- 选择最小充分集合；
- 解释未覆盖范围。

没有实际运行：

```text
NOT VERIFIED
```

不能算 PASS。

---

# 15. Diff Review

必须实际看代码。

重点检查：

```text
fallback
except Exception
None handling
setdefault
config defaults
timezones
hash source
artifact resolution
path semantics
raw/qfq
future data
silent fail-open
report 与真实执行是否同源
```

特别防止：

```text
报告字段修正确了
但真实执行逻辑没改
```

---

# 16. Legacy Regression

除非上位规格明确授权，必须确认：

```text
70 分不变
Cross Review 不变
Legacy Final 不变
正式通知不变
serving model 不变
scheduler 不变
```

M2 Shadow 也不能接管 Legacy。

---

# 17. Security Review

检查 diff + tests + artifacts + reports：

```text
token
secret
password
webhook
api_key
authorization
bearer
```

任何凭据泄漏：

```text
FAIL
```

---

# 18. Rollback Review

检查：

- 能否只回滚 M1/M2；
- 最好还能回滚单个 SXX；
- 是否删除 Legacy；
- 是否做不可逆数据迁移；
- 是否覆盖历史 artifact；
- config 是否兼容。

如果一个大批次只能“全系统推倒重来”才能回滚：

列 Blocking Finding。

---

# 19. 批次 Gate Matrix

最终必须逐项输出：

```text
M1:
S00 PASS/FAIL/NOT VERIFIED
...
S10 PASS/FAIL/NOT VERIFIED
```

或：

```text
M2:
S11 ...
...
S23 ...
```

每项必须有证据摘要。

---

# 20. M1 判定

M1 只有全部关键门 PASS 才可：

```text
M1 Verdict = PASS
Permission To Proceed To M2 = YES
```

只要 S01/S02/S03/S06/S07/S08/S09/S10 等正确性门有关键失败：

```text
M1 FAIL
M2 LOCKED
```

---

# 21. M2 判定

M2 应区分：

```text
Engineering Verdict
Research Gate
Production Promotion Gate
```

可能结果：

```text
Engineering Verdict = PASS
Research Gate = AWAITING_DATA
Production Promotion = LOCKED
```

这是正常状态。

只有未来积累满足上位方案的 60D/120D/250D 标准，才能进一步解锁。

---

# 22. 最终输出格式

```text
# CURRENT_BATCH Acceptance Report

## 1. Batch Verdict
PASS / CONDITIONAL PASS / FAIL

## 2. Engineering Verdict
PASS / FAIL

## 3. Permission To Proceed
M1:
- TO M2 = YES / NO

M2:
- TO PRODUCTION PROMOTION = NO / REVIEW_REQUIRED

## 4. Repository State
- branch
- HEAD
- status
- diff stat

## 5. Batch Scope

## 6. Stage Gate Matrix

| Stage | Verdict | Evidence | Blocking? |
|---|---|---|---|
| S00 | PASS | ... | No |

## 7. Blocking Findings

### B1
Problem:
Evidence:
Risk:
Required Fix:

如无：
NONE

## 8. Non-Blocking Findings

## 9. Diff Review

## 10. Independent Tests Executed

## 11. Test Results
XX passed
XX failed
XX skipped
XX not verified

## 12. Adversarial Tests

## 13. Legacy Regression

## 14. PIT / Leakage Review

## 15. Execution Semantics Review

## 16. Model / Label Semantics Review

## 17. Artifact Review

## 18. Security Review

## 19. Rollback Review

## 20. PROGRESS.md Review

## 21. Research Evidence Status

M1:
N/A

M2:
20D = AVAILABLE / AWAITING_DATA
60D = ...
120D = ...
250D = ...

## 22. Production Promotion Status
LOCKED / REVIEW_REQUIRED

## 23. Required Fixes Before Recheck

## 24. Deferred Recommendations

## 25. Final Statement

CURRENT_BATCH = PASS / FAIL

NEXT BATCH = UNLOCKED / LOCKED
```

---

# 23. 当前验收命令

根据：

```text
CURRENT_BATCH
```

如果：

```text
M1
```

验收 Gate S00～S10。

如果：

```text
M2
```

验收 Gate S11～S23，并明确区分“代码实现完成”和“研究样本已成熟”。

再次强调：

> Codex 是裁判，不是施工者。

发现问题就给出 FAIL + 可执行修复要求，不要顺手替 ZCode 修改代码。
