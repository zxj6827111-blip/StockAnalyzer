# StockAnalyzer Alpha V2.0 — PROGRESS

> 用途：记录 Alpha V2.0 改造过程中的施工进度、测试结果、审计工件、Codex 验收结果与回滚信息。  
> 维护原则：只追加，不覆盖历史。  
> 上位方案：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`

---

# 0. 当前总状态

```text
Project = StockAnalyzer Alpha V2.0
Current Batch = NOT_STARTED
Current Stage = NOT_STARTED
M1 Acceptance = PENDING
M2 Acceptance = LOCKED
Production Promotion = LOCKED
```

---

# 1. 批次定义

```text
M1 = S00 ～ S10
     Correctness Foundation

M2 = S11 ～ S23
     Alpha Research & Shadow
```

规则：

- M1 未经 Codex 验收 PASS，不得进入 M2；
- M2 工程实现 PASS，不代表研究证据已经成熟；
- 60D / 120D / 250D 研究门需要真实交易日和成熟 outcome；
- 未经明确授权，不得切换生产 serving model，不得启用正式 V2 Final。

---

# 2. M1 — Correctness Foundation

## Batch Status

```text
Status = NOT_STARTED
Starting HEAD = TBD
Ending HEAD = TBD
Codex Acceptance = PENDING
```

## Stage Matrix

| Stage | Task | ZCode Status | Tests | Audit Artifact | Codex Acceptance | Notes |
|---|---|---|---|---|---|---|
| S00 | Alpha V2 Feature Flag / No-op Baseline | NOT_STARTED | - | - | PENDING | |
| S01 | Model Identity Truth | NOT_STARTED | - | - | PENDING | |
| S02 | T+1 Entry Simulation | NOT_STARTED | - | - | PENDING | |
| S03 | Point-in-Time Historical Universe | NOT_STARTED | - | - | PENDING | |
| S04 | SelectionContract 300/100/50 | NOT_STARTED | - | - | PENDING | |
| S05 | Registry / Archive Governance | NOT_STARTED | - | - | PENDING | |
| S06 | HistoricalModelResolver | NOT_STARTED | - | - | PENDING | |
| S07 | Feature Price / Execution Price Split | NOT_STARTED | - | - | PENDING | |
| S08 | Data Health / Market Breadth Split | NOT_STARTED | - | - | PENDING | |
| S09 | Model / Label Semantic Guard | NOT_STARTED | - | - | PENDING | |
| S10 | Decision Log + Outcome Maturation | NOT_STARTED | - | - | PENDING | |

---

# 3. M2 — Alpha Research & Shadow

## Batch Status

```text
Status = LOCKED
Prerequisite = M1 Codex PASS
Starting HEAD = TBD
Ending HEAD = TBD
Codex Engineering Acceptance = PENDING
Research Evidence Status = LOCKED
```

## Stage Matrix

| Stage | Task | ZCode Status | Tests | Audit Artifact | Codex Acceptance | Research Status | Notes |
|---|---|---|---|---|---|---|---|
| S11 | Label V2 | LOCKED | - | - | PENDING | - | |
| S12 | Benchmark System | LOCKED | - | - | PENDING | - | |
| S13 | Winner Recall | LOCKED | - | - | PENDING | - | |
| S14 | Feature Availability / Leakage Audit | LOCKED | - | - | PENDING | - | |
| S15 | Simple Factor Baseline | LOCKED | - | - | PENDING | - | |
| S16 | Shared Feature Matrix + Multi-Head | LOCKED | - | - | PENDING | - | |
| S17 | Cross Review V2 | LOCKED | - | - | PENDING | - | |
| S18 | Final Decision Policy V2 Shadow | LOCKED | - | - | PENDING | - | |
| S19 | Purged Walk-Forward / OOS | LOCKED | - | - | PENDING | - | |
| S20 | Legacy vs V2 Shadow Dual Run | LOCKED | - | - | PENDING | AWAITING_DATA | |
| S21 | Daily Alpha Health Report | LOCKED | - | - | PENDING | AWAITING_DATA | |
| S22 | NAS Performance Hardening | LOCKED | - | - | PENDING | - | |
| S23 | Theme / News / Intraday Incremental Framework | LOCKED | - | - | PENDING | AWAITING_DATA | |

---

# 4. 研究证据门

> 本节只能根据真实成熟 outcome 更新，禁止凭代码实现直接标记 PASS。

| Gate | Required Evidence | Status | Evidence Window | Notes |
|---|---|---|---|---|
| 20D Alert Gate | >=20 mature decision dates | LOCKED | - | 仅用于发现明显失败 |
| 60D Research Gate | >=60 mature decision dates | LOCKED | - | 允许第一轮方向判断 |
| 120D Advisory Gate | >=120 clean OOS dates | LOCKED | - | 才允许讨论 Advisory |
| 250D Governance Gate | ~250 OOS trading days | LOCKED | - | 才讨论自动 promotion / 稳定阈值 |

---

# 5. 阶段记录模板

> ZCode 每完成一个 SXX，在本文件末尾追加一份，不得覆盖旧记录。

```markdown
## SXX — <Stage Name>

### Status
DONE / PARTIAL / BLOCKED

### Date
YYYY-MM-DD

### Batch
M1 / M2

### Starting HEAD
<commit>

### Ending HEAD / Working Tree
<commit or dirty state>

### Files Changed
- ...

### Behavior Changes
- ...

### Tests
Commands:
- `pytest ...`

Results:
- XX passed
- XX failed
- XX skipped

### Audit Artifacts
- `artifacts/alpha_v2/audit/...`

### Deviation From Blueprint
NONE / ...

### Deferred Findings
- ...

### Rollback
- ...

### ZCode Conclusion
DONE / PARTIAL / BLOCKED

### Codex Acceptance
PENDING / PASS / FAIL

### Codex Blocking Findings
NONE / ...

### Next Stage
LOCKED / SXX
```

---

# 6. 批次验收记录模板

## M1 Acceptance

```text
Engineering Verdict = PENDING
Codex Verdict = PENDING
Permission To Proceed To M2 = NO
Reviewed HEAD = TBD
Review Date = TBD
```

### Blocking Findings
- TBD

### Non-Blocking Findings
- TBD

---

## M2 Acceptance

```text
Engineering Verdict = PENDING
Codex Verdict = PENDING
Research Evidence Status = LOCKED
Production Promotion = LOCKED
Reviewed HEAD = TBD
Review Date = TBD
```

### Blocking Findings
- TBD

### Non-Blocking Findings
- TBD

---

# 7. Production Promotion 状态

```text
V2 Shadow = NOT_ENABLED
V2 Advisory = LOCKED
V2 Enforced Final Selection = LOCKED
Auto Promotion = DISABLED
```

任何生产切换必须独立记录：

```text
approved_by
approved_at
code_commit
model_id
artifact_hash
config_hash
rollback_point
```

不得只写“已上线”。

---

# 8. Deferred Findings 总表

| ID | Found At | Description | Severity | Owner | Target Stage | Status |
|---|---|---|---|---|---|---|
| DF-001 | - | - | - | - | - | OPEN |

---

# 9. 关键不变量

整个改造期间持续检查：

```text
Legacy threshold 70 未被擅自降低
Legacy Cross Review 未被擅自放宽
V2 未经授权不接管正式结果
Historical replay 无 future-model fallback
盘后策略无 T-close 假成交
历史 universe 无 future-listed symbol
Execution 使用 raw price
模型输出语义明确
0 只结果合法
所有阶段可审计、可回滚
```

---

# 10. 当前下一步

```text
NEXT ACTION:
Run ZCode CURRENT_BATCH = M1

AFTER M1:
Run Codex CURRENT_BATCH = M1

Only after Codex PASS:
Unlock M2
```
