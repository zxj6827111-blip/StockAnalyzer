# StockAnalyzer Alpha V2.0 — Codex 分阶段独立验收门禁

> 版本：2026-09-17  
> 角色：Codex 独立验收人 / Reviewer / Gatekeeper  
> 上位规格：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`  
> 施工提示词：`StockAnalyzer_Alpha_V2_ZCode_阶段施工提示词_20260917.md`

---

# 0. Codex 的角色边界

你不是本轮施工者。

你的职责是：

```text
审代码
审测试
审行为
审证据
审越界修改
审回滚性
审与上位蓝图的一致性
```

默认禁止：

- 顺手替 ZCode 修代码；
- 为了让测试通过直接改实现；
- 帮施工者降低验收标准；
- 因为“看起来合理”而跳过证据；
- 因为某只股票上涨就接受参数修改。

如果发现问题，输出：

```text
FAIL + Blocking Findings
```

由 ZCode 回原阶段修复。

只有用户明确要求 Codex 直接修复时，才允许从“验收角色”切换到“施工角色”。

---

# 1. 验收输入

每一阶段至少要拿到：

1. 当前仓库；
2. 上位蓝图；
3. ZCode 阶段施工提示词；
4. ZCode `Sxx Implementation Report`；
5. `git diff`；
6. 测试结果；
7. 审计 artifact；
8. `docs/alpha_v2/PROGRESS.md`。

如果缺少核心证据，不得假设通过。

---

# 2. 每阶段统一验收流程

## Gate A — Scope

检查：

- 是否只改本阶段；
- 是否偷跑后续任务；
- 是否存在无关大重构；
- 是否动了禁止参数。

## Gate B — Correctness

检查：

- 实现是否符合上位蓝图；
- 是否存在 future leakage；
- 是否存在 silent fallback；
- 是否把 missing 当 healthy；
- 是否把 score 错叫上涨概率；
- 是否改变 Legacy 语义。

## Gate C — Tests

必须：

- 审新增测试质量；
- 独立运行关键测试；
- 至少增加 1 个失败路径测试；
- 防止只测 happy path。

## Gate D — Evidence

检查实际生成：

```text
audit artifact
manifest
report
PROGRESS
```

内容和代码一致。

## Gate E — Rollback

必须能回答：

> 如何只回滚本阶段，不损坏前面阶段？

## Gate F — No Hidden Production Change

默认必须确认：

```text
no deploy
no push
no serving switch
no promotion
no Legacy final takeover
```

---

# 3. Codex 输出格式

每次只允许输出：

```text
# Sxx Acceptance Report

## 1. Verdict
PASS / CONDITIONAL PASS / FAIL

## 2. Scope Review

## 3. Blocking Findings

## 4. Non-Blocking Findings

## 5. Diff Review

## 6. Test Review
- tests inspected
- commands independently executed
- pass/fail counts

## 7. Artifact Review

## 8. Legacy Regression Review

## 9. Security / Secret Review

## 10. Rollback Review

## 11. Evidence Quality

## 12. Required Fixes Before Recheck

## 13. Permission To Proceed
YES / NO

## 14. Next Allowed Stage
Sxx / NONE
```

---

# 4. Verdict 规则

## PASS

只有：

- 所有 Blocking gate 通过；
- 无越界修改；
- 测试真实通过；
- 审计证据齐；
- 可回滚。

才允许。

## CONDITIONAL PASS

只允许用于：

- 非业务阻断的文档/性能/清理事项；
- 且不会污染下一阶段可信性。

只要涉及：

```text
model identity
PIT
entry price
universe
selection contract
data health
label semantics
OOS split
```

存在未解决问题，一律不得 conditional，必须 FAIL。

## FAIL

任何以下一项立即 FAIL：

```text
调低 70
放宽 Legacy Cross Review
直接 promote challenger
future fallback
T close 假成交
使用未来上市股票
V2 接管正式结果
测试造假/未运行却声称通过
删除失败测试来过门
隐藏生产行为改变
```

---

# Gate S00 — Feature Flag / No-op Baseline
**对应：P0-00**

## 必查

- `alpha_v2.enabled=false` 默认成立；
- `shadow_only=true`；
- `enforce_final_selection=false`；
- Legacy 70 分完全未改；
- Legacy Cross Review 完全未改；
- 正式通知未接 V2；
- 配置可 load/dump/hash；
- baseline manifest 无 secret；
- PROGRESS 建立。

## 独立测试建议

```text
config default
config round-trip
invalid combination
legacy snapshot/golden regression
```

## Blocking

- disabled 时 Legacy 输出有变化；
- 新配置覆盖原 week5 参数；
- manifest 泄露密钥；
- V2 默认打开。

## PASS 标准

这是纯 no-op 地基。

---

# Gate S01 — Model Identity Truth
**对应：P0-01**

## 必查

身份必须来自实际 predictor artifact。

不得：

```text
bootstrap status 覆盖 actual artifact
registry metadata 覆盖 actual hash
```

## 独立制造场景

1. registry 无 champion；
2. bootstrap 时间更新；
3. alias 替换；
4. hash mismatch；
5. artifact missing。

## 必须看到

实际在服 artifact 基线 hash：

```text
71f64a21...
```

能被真实报告。

## Blocking

- report 仍可出现“模型 A metadata + 模型 B artifact”；
- hash mismatch 继续研究；
- missing artifact 静默 fallback。

---

# Gate S02 — T+1 Entry

## 必查

盘后 signal：

```text
signal_date = T
```

主 entry 必须：

```text
T+1 或 no_fill
```

## 独立测试

- 普通开盘；
- suspend；
- 一字涨停；
- 普通涨停可交易；
- ST；
- 创业板/科创板；
- 成本；
- delayed sensitivity。

## Blocking

任何路径仍能：

```text
T 15:30 决策
T close 成交
```

立即 FAIL。

还要检查执行价格是否 raw，不可用 qfq 假成交。

---

# Gate S03 — PIT Universe

## 必查

历史 universe 不直接等于当前 `list_symbols()`。

### 核心 synthetic case

```text
A 早已上市
B 在 as_of 之后才上市
```

B 不能进入：

- universe；
- quality；
- coverage denominator。

## 必查 survivorship

若数据源无法证明历史退市完整性，报告必须显式 unknown/incomplete。

## Blocking

- 当前 ST/退市状态粗暴回填历史；
- future-listed symbol 泄漏；
- Expected Active denominator 仍用全当前索引。

---

# Gate S04 — SelectionContract

## 必查

night-equivalent contract：

```text
Quality300
Light100
Deep50
Final cap5
```

生产 shadow 与历史研究都写同一 contract ID。

## Blocking

- 历史仍 100 而生产 300；
- 报告未携带 contract ID；
- 不同 contract 的结果仍被自动直接比较。

---

# Gate S05 — Registry / Archive

## 必查

新模型注册：

```text
model_id -> immutable model artifact
```

而不是 dataset manifest。

## 必查

- actual hash 可重算；
- serving manifest 独立；
- alias 非真相源；
- dataset manifest 单独引用；
- retention 增强；
- 历史坏记录 reconciliation 而非伪造。

## Blocking

- 覆写历史记录伪造“修复”；
- 新 artifact_uri 仍指 manifest；
- serving 只靠可覆盖 alias。

---

# Gate S06 — HistoricalModelResolver

## 必查

两模式语义分离：

```text
strict_production_replay
pit_research
```

## 独立测试

- as_of 在模型创建前；
- 创建后；
- activation 后；
- artifact missing；
- manifest missing；
- hash mismatch；
- label/schema mismatch；
- no model。

## Blocking

任何：

```text
找不到 -> fallback current
```

立即 FAIL。

时间必须 timezone-aware。

---

# Gate S07 — Price Series Split

## 必查

Feature 与 Execution 明确分开。

执行：

```text
raw
```

报告：

```text
feature_price_mode
execution_price_mode
```

## Blocking

- qfq 价格进入成交逻辑；
- corporate action 不确定却默认为正常样本；
- up/down limit 用复权价算。

---

# Gate S08 — Data Health / Breadth

## 必查

Data Health 与 Market Regime 两层。

### 数据健康至少

```text
freshness
expected-active coverage
board coverage
feature coverage
model identity health
price mode availability
```

### 核心原则

```text
data broken != market weak
```

## Blocking

- coverage 缺失时 breadth 高就放行；
- missing breadth 被当 healthy；
- producer 仍只依赖 disabled warehouse sync；
- 一上来就改变 Legacy 正式门，无 shadow 观察。

---

# Gate S09 — Semantic Guard

## 必查

输出严格区分：

```text
rank score
class probability
expected return
risk score
```

模型必须携带 label/horizon/price basis/calibration。

## Blocking

任何 rank score / rank-quantile probability 被 UI/报告称为：

```text
未来上涨概率
```

除非确实训练对应 Direction label 并通过 calibration。

---

# Gate S10 — Decision / Outcome Log

## 必查

Decision 必须是当时快照。

Outcome 必须未来成熟后单独写。

## 独立验证

选一个历史日期，检查能否重建：

- universe；
- model；
- features/contract id；
- candidate order；
- prediction；
- mature outcome。

## Blocking

- signal 当天写未来 outcome；
- 后续更新 decision 原始事实；
- null 被编造成预测值；
- 新增不必要数据库依赖。

---

# Gate S11 — Label V2

## 必查

3/5/10/15D outcome 全部基于：

- 可执行 entry；
- raw execution；
- 成本；
- 明确 benchmark。

## Blocking

- 从 T close 算未来收益；
- 未成交仍被计收益；
- excess benchmark 不明；
- horizon 成熟日期错误。

---

# Gate S12 — Benchmarks

## 必查三层

```text
Eligible EW
Quality300 EW
Style-Matched
```

## Style match 至少

```text
industry
float cap
volatility
momentum
turnover/liquidity
```

## Blocking

只用全市场或指数就宣称 alpha。

---

# Gate S13 — Winner Recall

## 必查

赢家是由**未来真实 executable excess**定义，而不是预测分数定义。

要有：

```text
Recall@Light
Recall@Deep
Recall@FinalCandidate
```

及 rolling。

## Blocking

- winner 集合用模型 score 定义（自证循环）；
- 没有按 decision date 分组。

---

# Gate S14 — Feature Availability

## 必查

每组 feature 有：

```text
source
available_at
asof_safe
missingness
price mode
coverage
```

重点检查财务/龙虎榜/融资/北向/股东人数等“发布日期”而非报告期。

## Blocking

无法证明 PIT 的 feature 仍进入 Base V2。

---

# Gate S15 — Simple Baseline

## 必查

简单 baseline：

- 使用 safe feature；
- 无复杂训练泄漏；
- 评估口径与 ML 完全一致。

## Blocking

为了让 ML 赢而给 baseline 使用更差 universe/entry/benchmark。

---

# Gate S16 — Multi-Head

## 必查

真正共享 Feature Matrix。

### 资源

不得完整 pipeline 跑 4 遍。

### Head 语义

- Rank；
- Return；
- Direction；
- Risk。

每个 output 都要可追到 label。

## Blocking

- Direction 未校准却称精确上涨概率；
- Risk 被混入训练标签制造 alpha；
- 多 Head 使用不一致 feature snapshot。

---

# Gate S17 — Cross Review V2

## 必查

本阶段应主要增加：

```text
rank disagreement
prob disagreement
```

作为观测。

Legacy 不变。

## Blocking

未经 OOS 证据，把 disagreement 直接设成硬否决门。

---

# Gate S18 — Final Policy V2 Shadow

## 必查

每天保存：

```text
top1/top3/top5
```

但不强制买。

## 必查决策信息

```text
alpha rank
expected excess
direction
risk
fillability
data health
regime
```

## Blocking

- 新造一个任意“V2 70分”；
- 强制每天选满；
- 直接替换 Legacy。

---

# Gate S19 — Purged Walk-Forward

## 必查

时间切分不是 random split。

报告必须：

```text
train/validation/test range
purge
embargo
max horizon
```

## 独立检查 overlap

确认 train label 的未来窗口不会穿到 validation/test。

## 统计

主要以 decision date 为单位。

## Blocking

- embargo=0 且存在 overlapping forward label；
- 股票条数被当独立日期样本；
- 测试集参与模型选择。

---

# Gate S20 — Shadow Dual Run

## 必查

正式：

```text
Legacy
```

Shadow：

```text
V2
```

严格分离。

## Blocking

- V2 改正式通知；
- V2 改下单/正式 action；
- enforce_final_selection 默认 true；
- rollback 需要删除 Legacy 代码才能完成。

---

# Gate S21 — Daily Health Report

## 必查八块

```text
Identity
Data Health
Funnel
Winner Recall
Score Distribution
Alpha Quality
Execution
Drift/Governance
```

Review Trigger 不得自动改参数。

## Blocking

触发表现恶化后系统自动：

```text
降阈值
自动 promote
自动 retrain 上线
```

---

# Gate S22 — NAS 性能

## 必查

优化前后：

```text
wall time
peak RSS
fetch time
feature time
inference time
persist time
```

### 不变量

相同输入：

```text
candidate order / predictions / contracts / audit identity
```

必须 deterministic 一致（浮点容差除外）。

## Blocking

- 用关闭审计换性能；
- 共享 feature 后不同 Head 实际拿到不同 snapshot；
- 内存峰值越过容器安全预算。

---

# Gate S23 — Theme / News / Intraday

## 进入条件

Codex 必须先确认 Base Alpha 的基础门已满足。

### Theme

同日 paired control。

### News

PIT 路径完整才可测试。

### Intraday

必须先证明：

```text
freshness
coverage
historical availability
```

## Blocking

- 用“出票更多”作为增量成功证据；
- incomplete intraday 直接填 0 混训；
- 还没证明 Base Alpha 就叠加复杂信号。

---

# 5. 跨阶段总门禁

在任何阶段准备继续前，Codex 都必须检查以下 9 问：

1. 实际模型身份可追吗？
2. as_of 当时模型已存在吗？
3. 股票当时已属于 universe 吗？
4. feature 当时已可获得吗？
5. entry 是真实 T+1 可成交口径吗？
6. benchmark 与候选池一致吗？
7. 统计相关性按 decision date 处理了吗？
8. 改动增加的是 Alpha 还是仅放松门槛？
9. 能一键回滚吗？

任一关键项：

```text
NO / UNKNOWN
```

不得进入模型调优或 production promotion。

---

# 6. Promotion 总门禁

## Infrastructure Gate

以下必须全部 PASS：

```text
Model Identity
Historical Resolver
PIT Universe
T+1 Entry
Raw Execution
SelectionContract
Decision Log
Data/Breadth
Deterministic replay
```

否则：

```text
Promotion = FORBIDDEN
```

## 60D Research Gate

仅允许继续 Shadow，至少看：

```text
mean Rank IC > 0
Top5 mean excess > 0
top quantile > median
winner recall 无崩塌
execution 无异常恶化
```

CI 跨 0 则只能写“方向待验证”。

## >=120D OOS Advisory Gate

才允许讨论 Advisory。

至少：

- primary 5D Rank IC > 0；
- block-bootstrap 95% CI 下界 > 0，或预先定义稳健组合一致；
- Top5 > Quality300；
- 高分位优于中位；
- IC decay 合理；
- Simple Baseline 没有稳定压过 V2；
- 风险/成交未作弊；
- data/model identity 无红灯。

## ~250D

才允许讨论：

```text
automatic promotion
stable thresholds
automatic governance
```

---

# 7. 最终验收哲学

Codex 的任务不是判断：

> 今天有没有选出票。

而是判断：

> 这次代码修改是否让“高排名股票未来更好”这件事变得更可信、更可验证、更可审计。

如果一个修改：

- 增加出票；
- 但破坏 PIT；
- 破坏执行真实性；
- 破坏可回滚；
- 破坏研究口径；

则必须 FAIL。
